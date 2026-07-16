"""chronx impact — single-file change-impact / co-change analysis.

A read-only drill-down that answers a very concrete question from *one* file's
point of view: **"when I change THIS file, what else tends to change with it?"**

Where ``chronx hotspots`` ranks *global* co-change pairs across the whole
history, ``impact`` fixes one target file and reports its personal *blast
radius* on the cwd's tracked root + active timeline:

  * how many recorded events ever touched the target (its change count ``C``)
  * the OTHER files that changed inside those same commands, ranked by how many
    of the target's events also touched them, as ``co  co/C%  bar  path`` — e.g.
    "editing api.py, you also changed test_api.py 8/10 times (80%)"
  * the COMMANDS that most often changed the target, so you see *how* it is
    usually modified

Everything is derived from two parameterized SQL queries over
``events``/``deltas`` (Row factory) plus in-memory aggregation with
``collections.Counter``; no per-file subqueries in a loop. It never mutates the
store and degrades gracefully on an empty / untracked / never-recorded target.
"""

from __future__ import annotations

import collections
import os

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# tunables + a unicode ramp for the little coupling bars
# --------------------------------------------------------------------------- #
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"  # 0..8 eighths of a cell — smooth horizontal bars
_BAR_WIDTH = 20             # cells in a full (100%) coupling bar
_MAX_CMD_LEN = 100          # commands longer than this are clipped for display


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _scope(root_id: int, branch_id: int | None) -> tuple[str, list[object]]:
    """WHERE fragment + params scoping ``events e`` to one root and (when known)
    its active branch. Falls back to root-only on a pre-branch store where the
    active branch is unknown, so the report still works."""
    if branch_id is None:
        return "e.root_id = ?", [root_id]
    return "e.root_id = ? AND e.branch_id = ?", [root_id, branch_id]


def _bar(value: float, maximum: float, width: int = _BAR_WIDTH) -> str:
    """A smooth horizontal bar (eighth-block resolution) for ``value/maximum``."""
    if maximum <= 0 or value <= 0:
        return ""
    frac = min(1.0, value / maximum) * width
    full = int(frac)
    bar = "█" * full
    if full < width:  # add a partial cell for the remainder
        eighths = int(round((frac - full) * 8))
        if eighths:
            bar += _BAR_EIGHTHS[eighths]
    return bar


def _plural(n: int, word: str) -> str:
    """``"1 time"`` / ``"3 times"`` style pluralisation."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def _oneline(command: str) -> str:
    """Collapse whitespace and clip a command string for one-line display."""
    flat = " ".join(command.split())
    return flat if len(flat) <= _MAX_CMD_LEN else flat[:_MAX_CMD_LEN] + " …"


def _relpath(file, root_path: str) -> str:
    """``file`` expressed relative to its root, using forward slashes.

    Mirrors the resolution used by ``chronx blame`` so the same argument names
    the same recorded path. A file outside the root simply won't match any
    recorded path -> a clean "never recorded" message.
    """
    return os.path.relpath(file.resolve(), root_path).replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.option(
        "--top", "-n", default=15, show_default=True,
        help="How many rows to show in the coupled-files / commands lists.",
    )
    def impact(file, top: int) -> None:  # type: ignore[no-untyped-def]
        """Co-change impact of FILE: what changes when you change FILE."""
        top = max(1, top)
        conn = X.open_db()  # ClickException if there's no store
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            root_id = int(root["id"])
            branch = X.active_branch_id(conn, root_id)
            where, params = _scope(root_id, branch)
            rel = _relpath(file, root["path"])

            # header (root + active timeline, mirroring `chronx hotspots`/`du`)
            header = f"impact  {root['path']}"
            if branch is not None:
                b = X.dbm.get_branch(conn, branch)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)

            # ---- Q1: the events that touched the target file -------------- #
            # One row per event (GROUP BY e.id), scoped to root + active branch.
            # This gives C (the change count) and, via describe_command, the
            # commands that changed the file — no separate query needed.
            target_events = list(
                conn.execute(
                    "SELECT e.id AS eid, e.command AS command, "
                    "       e.started_at AS started_at "
                    "FROM deltas d JOIN events e ON e.id = d.event_id "
                    f"WHERE {where} AND d.path = ? "
                    "GROUP BY e.id ORDER BY e.id",
                    params + [rel],
                )
            )
            change_count = len(target_events)  # == C

            X.click.echo("")
            if change_count == 0:
                # Never recorded on this timeline — friendly, not an error.
                X.click.secho(
                    f"{rel} was never recorded changing on this timeline "
                    "(created before tracking, ignored, or never written here).",
                    fg="yellow",
                )
                return

            # ---- Q2: co-changed paths (self-join on shared events) -------- #
            # For every event that touched the target, pull *all* paths that
            # event touched. A self-join keeps this to one query and avoids a
            # giant IN(...) list; we aggregate distinct co-events in Python.
            co_rows = conn.execute(
                "SELECT d2.event_id AS eid, d2.path AS path "
                "FROM deltas d1 "
                "JOIN deltas d2 ON d2.event_id = d1.event_id "
                "JOIN events e ON e.id = d1.event_id "
                f"WHERE {where} AND d1.path = ?",
                params + [rel],
            )
            # Reconstruct each target-event's path set, then count how many of
            # the target's events also touched each OTHER path. Using a set per
            # event dedupes defensively (co <= C, so the % is always <= 100).
            paths_by_event: dict[int, set[str]] = collections.defaultdict(set)
            for r in co_rows:
                paths_by_event[int(r["eid"])].add(r["path"])
            co_counter: "collections.Counter[str]" = collections.Counter()
            for paths in paths_by_event.values():
                for p in paths:
                    if p != rel:
                        co_counter[p] += 1

            # ---- coupling headline + table -------------------------------- #
            coupled = co_counter.most_common()  # most-coupled first
            shown = coupled[:top]
            if not coupled:
                X.click.secho(
                    f"{rel} changed {_plural(change_count, 'time')}:", bold=True
                )
                X.click.echo(
                    "  always changed alone — no co-change coupling"
                )
            else:
                extra = (
                    f" (of {len(coupled)})" if len(coupled) > len(shown) else ""
                )
                X.click.secho(
                    f"{rel} changed {_plural(change_count, 'time')}; its "
                    f"{_plural(len(shown), 'most-coupled file')}{extra}:",
                    bold=True,
                )
                cw = max(len(str(co)) for _p, co in shown)  # co-count col width
                X.click.secho(
                    "  co-change  coupling  (coupling% = co-changes / this "
                    "file's changes)",
                    dim=True,
                )
                for path, co in shown:
                    pct = co / change_count * 100.0
                    bar = _bar(co, change_count)
                    X.click.echo(
                        f"  {co:>{cw}}x       {pct:5.1f}%  "
                        f"{bar:<{_BAR_WIDTH}}  {path}"
                    )

            # ---- commands that most often changed the target -------------- #
            # Aggregated from Q1's events with a Counter (one vote per event).
            cmd_counter: "collections.Counter[str]" = collections.Counter(
                X.describe_command(r) for r in target_events
            )
            top_cmds = cmd_counter.most_common(top)
            X.click.echo("")
            X.click.secho(f"commands that changed {rel}", bold=True)
            ccw = max(len(str(c)) for _cmd, c in top_cmds)  # count col width
            for command, count in top_cmds:
                X.click.echo(f"  {count:>{ccw}}x  {_oneline(command)}")

            # ---- small time-span footer ----------------------------------- #
            first_ts = float(target_events[0]["started_at"])
            last_ts = float(target_events[-1]["started_at"])
            span = X.fmt_ts(first_ts)
            if last_ts > first_ts:
                span += f"  ..  {X.fmt_ts(last_ts)}"
            X.click.echo("")
            X.click.secho(f"  [{span}]", dim=True)
        finally:
            conn.close()
