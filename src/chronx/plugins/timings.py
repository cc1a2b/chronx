"""chronx timings — where does your time go: wall-clock duration analytics.

Read-only analytics over recorded commands, answering a question no other
chronx command surfaces: *how long did things actually take?* Every event
stores ``started_at`` and ``finished_at`` (unix epochs); their difference is
the wall-clock duration of that command. For the cwd's tracked root and its
active timeline (branch) this reports:

  * headline          — commands timed, total wall time spent running them,
                        the busiest single command line and its share of total
  * slowest runs      — the single longest individual commands, by duration
  * by command        — identical command strings grouped, ranked by the TOTAL
                        wall time they consumed (what really eats the day), with
                        runs / total / avg / max and a proportional bar
  * distribution      — every duration bucketed into ranges, with counts + bars

chronx-internal operations (``session = 'chronx'`` or ``command LIKE
'chronx %'``) run in ~0s and external changes (``command IS NULL``) have no
command to time, so both are excluded by default; ``--all-commands`` includes
them. Everything is scoped to one root + one branch and derived entirely from
raw, parameterized SQL over ``events`` (Row factory). It never mutates the
store and degrades gracefully on an empty / single-event / untracked history.
"""

from __future__ import annotations

import collections

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# rendering tunables
# --------------------------------------------------------------------------- #
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"  # 0..8 eighths of a cell — smooth horizontal bars
_BAR_WIDTH = 20             # cells in the proportional bars
_CMD_WIDTH = 46             # command strings are truncated to this for display

# Duration histogram: (label, lower-inclusive bound, upper-exclusive bound) in
# seconds. The last bucket's upper bound is +inf so everything lands somewhere.
_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<100ms", 0.0, 0.1),
    ("100ms-1s", 0.1, 1.0),
    ("1-10s", 1.0, 10.0),
    ("10-60s", 10.0, 60.0),
    (">60s", 60.0, float("inf")),
)


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _scope(root_id: int, branch_id: int | None) -> tuple[str, list[object]]:
    """WHERE fragment + params scoping ``events`` to one root and (when known)
    its active branch. Falls back to root-only on a pre-branch store where the
    active branch is unknown, so the report still works."""
    if branch_id is None:
        return "root_id = ?", [root_id]
    return "root_id = ? AND branch_id = ?", [root_id, branch_id]


def _fmt_dur(seconds: float) -> str:
    """Humanize a wall-clock duration: 950ms / 2.4s / 1m 12s / 1h 3m."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    if seconds < 3600.0:
        minutes = int(seconds // 60)
        secs = int(round(seconds - minutes * 60))
        if secs == 60:  # rounding can carry into the next minute
            minutes += 1
            secs = 0
        return f"{minutes}m {secs}s"
    hours = int(seconds // 3600)
    minutes = int(round((seconds - hours * 3600) / 60))
    if minutes == 60:  # rounding can carry into the next hour
        hours += 1
        minutes = 0
    return f"{hours}h {minutes}m"


def _hbar(value: float, maximum: float, width: int = _BAR_WIDTH) -> str:
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


def _trunc(text: str, width: int = _CMD_WIDTH) -> str:
    """Single-line, length-capped rendering of a (possibly multi-line) command."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


class _Group:
    """Running aggregate for one distinct command string."""

    __slots__ = ("runs", "total", "worst")

    def __init__(self) -> None:
        self.runs = 0
        self.total = 0.0
        self.worst = 0.0

    def add(self, duration: float) -> None:
        self.runs += 1
        self.total += duration
        if duration > self.worst:
            self.worst = duration

    @property
    def avg(self) -> float:
        return self.total / self.runs if self.runs else 0.0


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--top", "-n", default=12, show_default=True,
        help="How many rows to show in the slowest-runs / by-command lists.",
    )
    @X.click.option(
        "--all-commands", is_flag=True,
        help="Include chronx-internal + external events (normally excluded).",
    )
    def timings(top: int, all_commands: bool) -> None:
        """Where your time goes: wall-clock duration analytics over commands."""
        top = max(1, top)
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            root_id = int(root["id"])
            branch = X.active_branch_id(conn, root_id)
            where, params = _scope(root_id, branch)

            # Only timed events count; internal/external are dropped by default.
            # `session IS NOT 'chronx'` is NULL-safe (unlike `!=`), so events
            # with a NULL session survive the filter.
            where += " AND finished_at IS NOT NULL"
            if not all_commands:
                where += (
                    " AND command IS NOT NULL"
                    " AND session IS NOT 'chronx'"
                    " AND command NOT LIKE 'chronx %'"
                )

            rows = list(
                conn.execute(
                    "SELECT id, command, started_at, finished_at "
                    f"FROM events WHERE {where} ORDER BY id",
                    params,
                )
            )

            # header (root + active timeline, mirroring `chronx hotspots`)
            header = f"timings  {root['path']}"
            if branch is not None:
                b = X.dbm.get_branch(conn, branch)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)

            # (id, command-label, started_at, duration); clamp negatives to 0.
            records: list[tuple[int, str, float, float]] = []
            for r in rows:
                dur = float(r["finished_at"]) - float(r["started_at"])
                records.append(
                    (int(r["id"]), X.describe_command(r), float(r["started_at"]),
                     dur if dur > 0 else 0.0)
                )

            if not records:
                hint = "" if all_commands else "  (try --all-commands)"
                X.click.echo("")
                X.click.echo(
                    "  no timed commands recorded on this timeline yet" + hint
                )
                return

            n = len(records)
            total_wall = sum(d for _id, _cmd, _ts, d in records)

            # ---- group identical command strings -------------------------- #
            groups: "collections.defaultdict[str, _Group]" = collections.defaultdict(
                _Group
            )
            for _id, cmd, _ts, d in records:
                groups[cmd].add(d)
            ranked = sorted(
                groups.items(), key=lambda kv: (kv[1].total, kv[1].runs), reverse=True
            )
            busiest_cmd, busiest = ranked[0]
            top_share = (busiest.total / total_wall * 100.0) if total_wall > 0 else 0.0

            # ---- headline ------------------------------------------------- #
            X.click.echo("")
            X.click.secho("headline", bold=True)
            X.click.echo(
                f"  {n} command{'s' if n != 1 else ''} timed"
                f"   ·   {_fmt_dur(total_wall)} total wall time"
                f"   ·   {len(ranked)} distinct command line"
                f"{'s' if len(ranked) != 1 else ''}"
            )
            X.click.echo(
                f"  busiest: {_fmt_dur(busiest.total)}"
                f" ({top_share:.0f}% of total, {busiest.runs} run"
                f"{'s' if busiest.runs != 1 else ''})"
                f"  $ {_trunc(busiest_cmd)}"
            )

            # ---- §1 slowest individual runs ------------------------------- #
            slow = sorted(records, key=lambda rec: rec[3], reverse=True)[:top]
            dur_w = max(len(_fmt_dur(d)) for _id, _cmd, _ts, d in slow)
            id_w = max(len(str(i)) for i, _cmd, _ts, _d in slow)
            X.click.echo("")
            X.click.secho(f"slowest runs (top {len(slow)} of {n})", bold=True)
            for i, cmd, ts, d in slow:
                X.click.echo(
                    f"  {_fmt_dur(d):>{dur_w}}  #{i:<{id_w}}  {X.fmt_ts(ts)}"
                    f"  $ {_trunc(cmd)}"
                )

            # ---- §2 by command (ranked by TOTAL time) --------------------- #
            shown = ranked[:top]
            max_total = shown[0][1].total  # ranked desc => first is the largest
            X.click.echo("")
            X.click.secho(
                f"by command (top {len(shown)} of {len(ranked)} by total time)",
                bold=True,
            )
            for cmd, g in shown:
                X.click.echo(
                    f"  {_hbar(g.total, max_total):<{_BAR_WIDTH}}"
                    f"  {_fmt_dur(g.total):>7}"
                    f"  runs {g.runs:<3}"
                    f"  avg {_fmt_dur(g.avg):>6}"
                    f"  max {_fmt_dur(g.worst):>6}"
                    f"  $ {_trunc(cmd)}"
                )

            # ---- §3 duration distribution --------------------------------- #
            counts = [0] * len(_BUCKETS)
            for _id, _cmd, _ts, d in records:
                for idx, (_label, lo, hi) in enumerate(_BUCKETS):
                    if lo <= d < hi:
                        counts[idx] += 1
                        break
            max_count = max(counts)
            label_w = max(len(label) for label, _lo, _hi in _BUCKETS)
            X.click.echo("")
            X.click.secho("distribution", bold=True)
            for (label, _lo, _hi), c in zip(_BUCKETS, counts):
                bar = _hbar(c, max_count) if max_count else ""
                X.click.echo(f"  {label:>{label_w}}  {c:>4}  {bar}")
        finally:
            conn.close()
