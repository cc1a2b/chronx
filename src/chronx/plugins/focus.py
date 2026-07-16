"""chronx focus — cluster recorded work into FOCUS BLOCKS by idle gaps.

Read-only analytics over recorded commands that answers a question no other
chronx command surfaces: *what was my actual working rhythm?* Where ``sessions``
groups by shell session and ``activity`` paints a calendar heatmap, ``focus``
segments the raw event stream purely by **time gaps** between consecutive
commands.

A *focus block* is a maximal run of events where every consecutive pair started
less than ``--gap`` minutes apart (default 20). The moment an idle gap larger
than the threshold appears, the current block ends and the next command opens a
new one. This reconstructs the natural "sprints" of work — bursts of activity
separated by breaks, meetings, lunch, or sleep — independent of how many shells
were involved.

For each block it reports the wall-clock span, command count, how many commands
changed files, the distinct files touched (union of delta paths), and failures
(non-zero exit codes). The headline rolls those up into total focused time,
longest block, deepest-focus block (most commands), average block size, and
focused-vs-idle time.

Blocks are listed **most-recent-first**; ``--top`` limits how many are printed,
but every statistic is computed over the full history. Everything is scoped to
the tracked root containing the cwd and its active timeline (branch), derived
entirely from the read-only store, and degrades gracefully on an empty /
single-event / untracked history.

Imports only ``chronx.pluginlib`` (as X) plus the stdlib, so it stays decoupled
from ``cli.py`` and is auto-discovered from the filesystem (no reinstall).
"""

from __future__ import annotations

import collections
import datetime

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# rendering tunables
# --------------------------------------------------------------------------- #
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"  # 0..8 eighths of a cell — smooth horizontal bars
_BAR_WIDTH = 14             # cells in the per-block "focus depth" bar


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _human_dur(seconds: float) -> str:
    """Compact human duration: ``0s`` / ``38s`` / ``4m`` / ``4m 30s`` /
    ``1h 12m``. Seconds below a minute keep second precision; minute- and
    hour-scale spans drop trailing zero components for readability."""
    s = int(round(seconds if seconds > 0 else 0.0))
    if s < 60:
        return f"{s}s"
    minutes, secs = divmod(s, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


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


def _fmt_span(start_ts: float, end_ts: float) -> str:
    """``YYYY-MM-DD HH:MM–HH:MM`` for a block; appends ``(+Nd)`` when the block
    crosses midnight so a multi-day span is never silently collapsed."""
    start = datetime.datetime.fromtimestamp(start_ts)
    end = datetime.datetime.fromtimestamp(end_ts)
    span = f"{start:%Y-%m-%d} {start:%H:%M}–{end:%H:%M}"
    day_delta = (end.date() - start.date()).days
    if day_delta > 0:
        span += f" (+{day_delta}d)"
    return span


class _Block:
    """One focus block: a run of events with sub-``gap`` spacing.

    ``events`` is kept in chronological order, so ``first``/``last`` are just its
    endpoints. File and failure stats are accumulated once, up front."""

    __slots__ = ("events", "files", "changed", "fails")

    def __init__(self) -> None:
        self.events: list["X.sqlite3.Row"] = []
        self.files: set[str] = set()   # distinct paths touched in the block
        self.changed = 0               # #events that changed >=1 file
        self.fails = 0                 # #events with a non-zero exit code

    @property
    def first(self) -> float:
        return float(self.events[0]["started_at"])

    @property
    def last(self) -> float:
        return float(self.events[-1]["started_at"])

    @property
    def duration(self) -> float:
        # Span from the first to the last command's *start* (per spec).
        return self.last - self.first

    @property
    def ncmds(self) -> int:
        return len(self.events)


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--gap",
        type=float,
        default=20.0,
        show_default=True,
        help="Idle threshold in MINUTES: a larger gap between two consecutive "
        "commands starts a new focus block.",
    )
    @X.click.option(
        "--top",
        "-n",
        default=12,
        show_default=True,
        help="How many focus blocks to list (stats always cover all blocks).",
    )
    def focus(gap: float, top: int) -> None:
        """Focus blocks: cluster your commands into work sprints by idle gaps."""
        top = max(1, top)
        gap_seconds = max(0.0, gap) * 60.0
        conn = X.open_db()
        try:
            # Scope to the tracked root for the cwd; stay friendly (never raise)
            # when the cwd isn't tracked, mirroring `chronx churn`.
            root = X.dbm.root_for_path(conn, X.Path.cwd())
            if root is None:
                X.click.secho(
                    f"chronx isn't tracking {X.Path.cwd()} yet "
                    "(run `chronx init` here, then record some commands).",
                    fg="yellow",
                )
                return
            root_id = int(root["id"])
            active = X.active_branch_id(conn, root_id)

            # Oldest-first events for this root + active timeline, then sorted by
            # started_at (id order and time order can differ for backfilled work).
            events = X.dbm.recent_events(
                conn, root_id=root_id, branch_id=active, limit=10_000_000
            )
            events.sort(key=lambda e: float(e["started_at"]))

            # Header / scope line (mirrors churn / timings).
            X.click.secho("chronx focus", bold=True)
            scope = f"  {root['path']}"
            if active is not None:
                b = X.dbm.get_branch(conn, active)
                if b is not None:
                    scope += f"   [timeline: {b['name']}]"
            scope += f"   ·   gap threshold {_human_dur(gap_seconds)}"
            X.click.secho(scope, dim=True)

            if not events:
                X.click.secho("  no recorded work on this timeline yet.", fg="yellow")
                return

            # ---- cluster into blocks by idle gap ---------------------------- #
            blocks: list[_Block] = []
            prev_ts: float | None = None
            cur: _Block | None = None
            for ev in events:
                ts = float(ev["started_at"])
                if cur is None or (prev_ts is not None and ts - prev_ts > gap_seconds):
                    cur = _Block()
                    blocks.append(cur)
                cur.events.append(ev)
                prev_ts = ts

            # ---- per-block file / failure stats ----------------------------- #
            for blk in blocks:
                for ev in blk.events:
                    deltas = X.dbm.deltas_for(conn, int(ev["id"]))
                    if deltas:
                        blk.changed += 1
                        for d in deltas:
                            blk.files.add(d.path)
                    exit_code = ev["exit_code"]
                    if exit_code is not None and int(exit_code) != 0:
                        blk.fails += 1

            # ---- aggregate stats (over ALL blocks) -------------------------- #
            n_blocks = len(blocks)
            total_cmds = sum(b.ncmds for b in blocks)
            total_changed = sum(b.changed for b in blocks)
            total_fails = sum(b.fails for b in blocks)
            all_files: set[str] = set()
            for b in blocks:
                all_files |= b.files
            focused = sum(b.duration for b in blocks)
            # Idle time = the sum of the between-block gaps we split on.
            idle = sum(
                blocks[i + 1].first - blocks[i].last for i in range(n_blocks - 1)
            )
            longest = max(blocks, key=lambda b: b.duration)
            deepest = max(blocks, key=lambda b: b.ncmds)
            avg_cmds = total_cmds / n_blocks
            max_cmds = deepest.ncmds  # bar reference: "focus depth"

            # ---- headline --------------------------------------------------- #
            X.click.echo("")
            X.click.secho("headline", bold=True)
            X.click.echo(
                f"  {n_blocks} focus block{'s' if n_blocks != 1 else ''}"
                f"   ·   {total_cmds} command{'s' if total_cmds != 1 else ''}"
                f"   ·   {len(all_files)} file{'s' if len(all_files) != 1 else ''}"
                f" touched   ·   {total_changed} changed files"
                + (
                    "   ·   " + X.click.style(f"{total_fails} fails", fg="red")
                    if total_fails
                    else "   ·   0 fails"
                )
            )
            X.click.echo(
                f"  focused time {_human_dur(focused)}"
                f"   ·   idle between blocks {_human_dur(idle)}"
            )
            X.click.echo(
                f"  longest block {_human_dur(longest.duration)}"
                f" ({_fmt_span(longest.first, longest.last)})"
            )
            X.click.echo(
                f"  deepest focus {deepest.ncmds} cmds"
                f" ({_fmt_span(deepest.first, deepest.last)})"
                f"   ·   avg {avg_cmds:.1f} cmds/block"
            )

            # ---- per-block listing (most recent first) ---------------------- #
            ordered = list(reversed(blocks))  # newest block at the top
            shown = ordered[:top]
            X.click.echo("")
            X.click.secho(
                f"blocks (newest first, top {len(shown)} of {n_blocks})", bold=True
            )
            for blk in shown:
                fails_txt = f"{blk.fails} fails"
                if blk.fails:
                    fails_txt = X.click.style(fails_txt, fg="red")
                X.click.echo(
                    f"  {_fmt_span(blk.first, blk.last)}"
                    f"  ({_human_dur(blk.duration)})"
                    f"  {blk.ncmds} cmds · {len(blk.files)} files · {fails_txt}"
                    f"   {_hbar(blk.ncmds, max_cmds)}"
                )

            hidden = n_blocks - len(shown)
            if hidden > 0:
                X.click.secho(
                    f"  … and {hidden} more block(s) not shown (raise --top)",
                    dim=True,
                )
        finally:
            conn.close()
