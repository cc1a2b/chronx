"""chronx churn — a code-churn / productivity report over recorded work.

Read-only over the store. Scopes to the tracked root containing the cwd and its
active timeline (branch), then totals *line churn* (unified-diff +/- lines)
across recorded events, grouped by a chosen dimension:

  * session  — per recording session: #commands, files, +/- churn, time span
  * day      — per calendar day: #commands, +/- churn (a churn trend)
  * command  — per command string: the most churn-heavy commands
  * ext      — per file extension: "where did my edits land" (e.g. .py vs .md)

Answers "how much did I change, grouped by X". Line churn per delta is counted
by re-rendering the unified diff (headers and ``@@`` markers excluded, capped
per file) and is cached by (before_hash, after_hash) so identical content is
only diffed once.

Imports only ``chronx.pluginlib`` (as X) plus the stdlib, so it stays decoupled
from ``cli.py`` and is auto-discovered from the filesystem (no reinstall).
"""

from __future__ import annotations

import collections
import datetime
import os
from pathlib import Path
from typing import Callable

from chronx import pluginlib as X

# Per-file diff cap when counting +/- lines. Binary / oversize / unavailable
# blobs render as ``@@ ... @@`` markers only, so they contribute 0 churn.
_DIFF_CAP = 4000
# Width (in blocks) of the fully-scaled churn bar.
_BAR_WIDTH = 24
# Width of the (truncated) group-key column.
_KEY_WIDTH = 34
_BLOCK = "█"  # █


# --------------------------------------------------------------------------- #
# per-group accumulator
# --------------------------------------------------------------------------- #
class _Group:
    """Running totals for one group (session / day / command / extension)."""

    __slots__ = ("ids", "files", "ins", "dels", "first", "last")

    def __init__(self) -> None:
        self.ids: set[int] = set()      # distinct event ids -> #commands
        self.files: set[str] = set()    # distinct touched paths
        self.ins = 0                    # inserted (+) lines
        self.dels = 0                   # deleted (-) lines
        self.first: float | None = None  # earliest started_at
        self.last: float | None = None   # latest started_at

    @property
    def churn(self) -> int:
        return self.ins + self.dels

    def touch(self, ts: float) -> None:
        self.first = ts if self.first is None else min(self.first, ts)
        self.last = ts if self.last is None else max(self.last, ts)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _ext_of(path: str) -> str:
    """Extension label for a path, e.g. ``.py``; ``(no ext)`` when there is none."""
    ext = os.path.splitext(path)[1]
    return ext if ext else "(no ext)"


def _dur(seconds: float) -> str:
    """Compact human duration for a time span (``0s``, ``42s``, ``3m``, ``2h 5m``)."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" if s == 0 else f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h" if m == 0 else f"{h}h {m}m"


def _truncate(text: str, width: int) -> str:
    """Truncate to ``width`` columns with an ellipsis when it overflows."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _pad(styled: str, raw: str, width: int, *, right: bool = False) -> str:
    """Pad a (possibly ANSI-styled) value to ``width`` using its raw length,
    so escape codes never throw the alignment off."""
    fill = " " * max(0, width - len(raw))
    return (fill + styled) if right else (styled + fill)


def _ins_cell(n: int) -> tuple[str, str]:
    """(raw, styled) for an insertions count — green, dimmed at zero."""
    raw = f"+{n}"
    return raw, (X.click.style(raw, fg="green") if n else X.click.style(raw, dim=True))


def _del_cell(n: int) -> tuple[str, str]:
    """(raw, styled) for a deletions count — red, dimmed at zero."""
    raw = f"-{n}"
    return raw, (X.click.style(raw, fg="red") if n else X.click.style(raw, dim=True))


def _net_cell(n: int) -> tuple[str, str]:
    """(raw, styled) for a net (ins - del) figure — signed, green/red/dim."""
    raw = f"+{n}" if n > 0 else str(n)
    if n > 0:
        return raw, X.click.style(raw, fg="green")
    if n < 0:
        return raw, X.click.style(raw, fg="red")
    return raw, X.click.style(raw, dim=True)


def _bar(ins: int, dels: int, peak: int) -> str:
    """A churn bar scaled to ``peak``: green insertions then red deletions."""
    churn = ins + dels
    if peak <= 0 or churn <= 0:
        return ""
    units = max(1, round(churn / peak * _BAR_WIDTH))
    green = round(ins / churn * units) if churn else 0
    red = units - green
    return (
        X.click.style(_BLOCK * green, fg="green")
        + X.click.style(_BLOCK * red, fg="red")
    )


# --------------------------------------------------------------------------- #
# key selectors
# --------------------------------------------------------------------------- #
def _event_key(by: str) -> Callable[["X.sqlite3.Row"], str]:
    """Return a function mapping an event Row -> its group key, for the whole-event
    groupings (session / day / command). ``ext`` groups per-delta instead."""
    if by == "session":
        return lambda ev: (ev["session"] or "(no session)")
    if by == "command":
        return lambda ev: X.describe_command(ev)
    # day
    return lambda ev: datetime.datetime.fromtimestamp(
        float(ev["started_at"])
    ).date().isoformat()


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--by",
        type=X.click.Choice(["session", "day", "command", "ext"]),
        default="session",
        show_default=True,
        help="Dimension to group churn by.",
    )
    @X.click.option(
        "--since",
        default=None,
        help="Only count work at/after this moment (a mark, event id, 'now', "
        "or a time spec like 24h / 2026-07-14).",
    )
    @X.click.option(
        "--top", default=15, show_default=True, help="How many groups to show."
    )
    def churn(by: str, since: str | None, top: int) -> None:
        """Line-churn report: how much you changed, grouped by session/day/command/ext."""
        top = max(0, top)
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # Scope to the tracked root for the cwd; stay friendly (never raise)
            # when the cwd isn't tracked or the store is otherwise empty.
            root = X.dbm.root_for_path(conn, Path.cwd())
            if root is None:
                X.click.secho(
                    f"chronx isn't tracking {Path.cwd()} yet "
                    "(run `chronx init` here, then record some commands).",
                    fg="yellow",
                )
                return
            root_id = int(root["id"])
            active = X.active_branch_id(conn, root_id)

            # Optional lower time bound.
            since_ts = X.moment_ts(conn, since) if since else None

            # Oldest-first events for this root + active timeline. recent_events
            # has no time filter, so we pull a generous window and trim below.
            events = X.dbm.recent_events(
                conn, root_id=root_id, branch_id=active, limit=1_000_000
            )
            if since_ts is not None:
                events = [e for e in events if float(e["started_at"]) >= since_ts]

            # Header / scope line.
            X.click.secho(f"chronx churn  by {by}", bold=True)
            scope = f"  {root['path']}"
            if active is not None:
                b = X.dbm.get_branch(conn, active)
                if b is not None:
                    scope += f"   [timeline: {b['name']}]"
            if since_ts is not None:
                scope += f"   since {X.fmt_ts(since_ts)}"
            X.click.secho(scope, dim=True)

            if not events:
                where = " in this window" if since_ts is not None else ""
                X.click.secho(f"  no recorded work{where}.", fg="yellow")
                return

            # ---- aggregate --------------------------------------------------
            # Cache line counts by content pair so identical deltas (same
            # before/after blob) are only diffed once.
            diff_cache: dict[tuple[str | None, str | None], tuple[int, int]] = {}

            def count(delta: "X.dbm.Delta") -> tuple[int, int]:
                key = (delta.before_hash, delta.after_hash)
                hit = diff_cache.get(key)
                if hit is not None:
                    return hit
                ins = dels = 0
                for line in X.render_delta(store, delta, max_lines=_DIFF_CAP):
                    if line.startswith(("+++", "---")):
                        continue  # unified-diff file headers, not churn
                    if line.startswith("+"):
                        ins += 1
                    elif line.startswith("-"):
                        dels += 1
                    # ``@@`` marker lines (binary / truncated / unchanged) start
                    # with '@' and are naturally ignored -> binary churn is 0.
                diff_cache[key] = (ins, dels)
                return ins, dels

            groups: dict[str, _Group] = collections.defaultdict(_Group)
            key_of = _event_key(by) if by != "ext" else None
            g_ids: set[int] = set()      # distinct events overall (for total)
            g_files: set[str] = set()    # distinct paths overall (for total)
            total_ins = total_del = 0

            for ev in events:
                eid = int(ev["id"])
                ts = float(ev["started_at"])
                g_ids.add(eid)
                deltas = X.dbm.deltas_for(conn, eid)

                if by == "ext":
                    # Each delta lands in its own extension group.
                    for d in deltas:
                        ins, dels = count(d)
                        total_ins += ins
                        total_del += dels
                        g_files.add(d.path)
                        grp = groups[_ext_of(d.path)]
                        grp.ids.add(eid)
                        grp.files.add(d.path)
                        grp.ins += ins
                        grp.dels += dels
                        grp.touch(ts)
                else:
                    # Whole event (and all its deltas) land in one group; the
                    # event is counted even when it changed nothing.
                    assert key_of is not None
                    grp = groups[key_of(ev)]
                    grp.ids.add(eid)
                    grp.touch(ts)
                    for d in deltas:
                        ins, dels = count(d)
                        total_ins += ins
                        total_del += dels
                        g_files.add(d.path)
                        grp.files.add(d.path)
                        grp.ins += ins
                        grp.dels += dels

            if not groups:
                X.click.secho("  no line churn recorded.", fg="yellow")
                return

            # ---- rank + render ---------------------------------------------
            ranked = sorted(
                groups.items(), key=lambda kv: (-kv[1].churn, -len(kv[1].ids), kv[0])
            )
            peak = max((g.churn for _, g in ranked), default=0)
            shown = ranked[:top] if top else []

            # column header
            X.click.echo()
            X.click.secho(
                f"  {'group':<{_KEY_WIDTH}} {'cmds':>5} {'files':>6} "
                f"{'+ins':>7} {'-del':>7} {'net':>7}  churn",
                bold=True,
            )

            def row(label: str, g: _Group, *, trailing: str = "") -> None:
                ins_raw, ins_s = _ins_cell(g.ins)
                del_raw, del_s = _del_cell(g.dels)
                net_raw, net_s = _net_cell(g.ins - g.dels)
                line = (
                    f"  {_truncate(label, _KEY_WIDTH):<{_KEY_WIDTH}} "
                    f"{len(g.ids):>5} {len(g.files):>6} "
                    f"{_pad(ins_s, ins_raw, 7, right=True)} "
                    f"{_pad(del_s, del_raw, 7, right=True)} "
                    f"{_pad(net_s, net_raw, 7, right=True)}  "
                    f"{_bar(g.ins, g.dels, peak)}"
                )
                if trailing:
                    line += X.click.style(f"  {trailing}", dim=True)
                X.click.echo(line)

            for label, g in shown:
                trailing = ""
                if by == "session" and g.first is not None and g.last is not None:
                    trailing = _dur(g.last - g.first)
                row(label, g, trailing=trailing)

            hidden = len(ranked) - len(shown)
            if hidden > 0:
                X.click.secho(
                    f"  … and {hidden} more group(s) not shown (raise --top)",
                    dim=True,
                )

            # ---- total row --------------------------------------------------
            total = _Group()
            total.ids = g_ids
            total.files = g_files
            total.ins = total_ins
            total.dels = total_del
            X.click.secho("  " + "-" * (_KEY_WIDTH + 36), dim=True)
            # Reuse the same layout for the TOTAL line (scaled to itself so the
            # bar always renders full-width as the reference).
            ins_raw, ins_s = _ins_cell(total_ins)
            del_raw, del_s = _del_cell(total_del)
            net_raw, net_s = _net_cell(total_ins - total_del)
            X.click.echo(
                f"  {'TOTAL':<{_KEY_WIDTH}} "
                f"{len(g_ids):>5} {len(g_files):>6} "
                f"{_pad(ins_s, ins_raw, 7, right=True)} "
                f"{_pad(del_s, del_raw, 7, right=True)} "
                f"{_pad(net_s, net_raw, 7, right=True)}  "
                f"{_bar(total_ins, total_del, total_ins + total_del)}"
            )
        finally:
            conn.close()
