"""chronx activity — a GitHub-style contribution heatmap of recorded work.

Read-only report over the event log. For the cwd's tracked root (its active
timeline) or every root with ``--all-roots``, it buckets events by calendar
day over the last N days and renders:

  * an ASCII contribution heatmap (7 weekday rows x week columns, shaded by
    that day's event count, with month labels and a legend);
  * an hour-of-day punchcard (horizontal bars, "when you work");
  * insight stats (total, active days, busiest day/hour, longest streak,
    average per active day).

Everything is derived from ``events.started_at`` (unix epoch); times are
bucketed in the machine's local zone via ``datetime.fromtimestamp``. The
command never writes and degrades gracefully on an empty or tiny store.
"""

from __future__ import annotations

import collections
import datetime
from pathlib import Path

from chronx import pluginlib as X

# --- rendering constants -----------------------------------------------------

# Shade ramp: index 0 is "no activity" (blank); 1..4 climb from light to full.
LEVELS = " ░▒▓█"
# Green gradient for the four active levels (GitHub-ish). click.echo strips
# these codes automatically when stdout is not a TTY, so alignment is safe.
CELL_STYLE: dict[int, dict[str, object]] = {
    1: {"fg": "green", "dim": True},
    2: {"fg": "green"},
    3: {"fg": "bright_green"},
    4: {"fg": "bright_green", "bold": True},
}
# Sub-cell block glyphs (1/8ths) for smooth horizontal bars.
BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MARGIN = 4  # width of the left weekday-label gutter ("Mon " etc.)
BAR_WIDTH = 42  # max width of an hour bar, in cells


def _shade_level(count: int, max_count: int) -> int:
    """Map a day's event count to a 1..4 shade level (0 handled by caller).

    Linear over 1..max_count so the busiest day is full and singletons stay
    light; guards the degenerate all-equal case (max_count == 1).
    """
    if count <= 0:
        return 0
    if max_count <= 1:
        return 1
    return 1 + min(3, (count - 1) * 4 // max_count)


def _hbar(value: int, max_value: int, width: int) -> str:
    """A proportional bar of block glyphs, with 1/8-cell resolution."""
    if value <= 0 or max_value <= 0:
        return ""
    eighths = round(value / max_value * width * 8)
    full, rem = divmod(eighths, 8)
    bar = "█" * full
    if rem:
        bar += BAR_EIGHTHS[rem]
    return bar or BAR_EIGHTHS[1]  # ensure a tiny nonzero value is still visible


def _longest_streak(days: set[datetime.date]) -> int:
    """Longest run of consecutive calendar days present in the set."""
    longest = current = 0
    prev: datetime.date | None = None
    for day in sorted(days):
        current = current + 1 if prev is not None and (day - prev).days == 1 else 1
        longest = max(longest, current)
        prev = day
    return longest


def _fetch_stamps(
    conn, root_id: int, branch_id: int | None, cutoff_ts: float
) -> list[float]:
    """Every event start time for a root/branch at or after ``cutoff_ts``.

    Raw read-only SQL (the connection already yields ``sqlite3.Row``). When the
    root has no active branch (e.g. a pre-branch store), fall back to all of the
    root's events rather than filtering by a NULL branch.
    """
    sql = "SELECT started_at FROM events WHERE root_id = ? AND started_at >= ?"
    params: list[object] = [root_id, cutoff_ts]
    if branch_id is not None:
        sql += " AND branch_id = ?"
        params.append(branch_id)
    return [float(r["started_at"]) for r in conn.execute(sql, params)]


def _month_header(grid_start: datetime.date, weeks: int) -> str:
    """A month-label row aligned above the week columns (>=1 space apart)."""
    cells = [" "] * (MARGIN + weeks + 4)
    last_end = -1
    prev_month: int | None = None
    for col in range(weeks):
        week_date = grid_start + datetime.timedelta(days=col * 7)
        if week_date.month == prev_month:
            continue
        prev_month = week_date.month
        pos = MARGIN + col
        if pos <= last_end:  # not enough room since the previous label
            continue
        label = week_date.strftime("%b")
        for i, ch in enumerate(label):
            if pos + i < len(cells):
                cells[pos + i] = ch
        last_end = pos + len(label)
    return "".join(cells).rstrip()


def register(main) -> None:
    @main.command()
    @X.click.option("--days", default=90, show_default=True,
                    help="How many days back to include.")
    @X.click.option("--all-roots", is_flag=True,
                    help="Combine activity across every tracked root.")
    def activity(days: int, all_roots: bool) -> None:
        """Contribution heatmap + punchcard of recorded activity."""
        style, echo = X.click.style, X.click.echo
        days = max(1, int(days))
        end_date = datetime.date.today()
        start_date = end_date - datetime.timedelta(days=days - 1)
        cutoff_ts = datetime.datetime.combine(
            start_date, datetime.time.min
        ).timestamp()

        conn = X.open_db()
        try:
            roots = X.dbm.get_roots(conn)
            if not roots:
                echo("No recorded activity yet.")
                echo(style("Run commands under `chronx exec` (or the shell "
                           "hook) first.", dim=True))
                return

            # Resolve which (root, active-branch) pairs to summarize.
            if all_roots:
                targets = [(r, X.active_branch_id(conn, int(r["id"])))
                           for r in roots]
                scope = style(f"all roots ({len(roots)})", bold=True)
            else:
                root = X.dbm.root_for_path(conn, Path.cwd())
                if root is None:
                    echo(f"{Path.cwd()} is not a tracked chronx root.")
                    echo(style(f"Use --all-roots to see all {len(roots)} "
                               "tracked root(s), or cd into a project.",
                               dim=True))
                    return
                branch_id = X.active_branch_id(conn, int(root["id"]))
                branch = (X.dbm.get_branch(conn, branch_id)
                          if branch_id is not None else None)
                bname = branch["name"] if branch is not None else "-"
                targets = [(root, branch_id)]
                scope = style(root["path"], bold=True) + style(
                    f"  [{bname}]", fg="magenta")

            stamps: list[float] = []
            for root, branch_id in targets:
                stamps.extend(
                    _fetch_stamps(conn, int(root["id"]), branch_id, cutoff_ts)
                )

            # --- header ---------------------------------------------------
            echo(style("chronx activity", bold=True) + "  ·  " + scope)
            echo(style(f"last {days} day(s)   {start_date} → {end_date}",
                       dim=True))
            echo("")

            if not stamps:
                echo(f"No activity in the last {days} day(s).")
                echo(style("Try a wider window, e.g. `chronx activity "
                           "--days 365`.", dim=True))
                return

            # --- bucket by day and hour -----------------------------------
            day_counts: collections.Counter[datetime.date] = (
                collections.Counter())
            hour_counts: collections.Counter[int] = collections.Counter()
            for ts in stamps:
                dt = datetime.datetime.fromtimestamp(ts)
                day_counts[dt.date()] += 1
                hour_counts[dt.hour] += 1

            total = len(stamps)
            max_day = max(day_counts.values())

            # --- heatmap grid ---------------------------------------------
            # Pad out to whole Monday..Sunday weeks so columns stay aligned.
            grid_start = start_date - datetime.timedelta(
                days=start_date.weekday())
            grid_end = end_date + datetime.timedelta(days=6 - end_date.weekday())
            weeks = (grid_end - grid_start).days // 7 + 1

            echo(_month_header(grid_start, weeks))
            for row in range(7):
                cells: list[str] = []
                for col in range(weeks):
                    day = grid_start + datetime.timedelta(days=col * 7 + row)
                    if day < start_date or day > end_date:
                        cells.append(" ")  # outside the window: blank pad
                        continue
                    count = day_counts.get(day, 0)
                    if count == 0:
                        cells.append(style("·", dim=True))  # quiet day
                    else:
                        lvl = _shade_level(count, max_day)
                        cells.append(style(LEVELS[lvl], **CELL_STYLE[lvl]))
                echo(f"{WEEKDAYS[row]:<3} " + "".join(cells))

            swatches = "".join(
                style(LEVELS[lvl], **CELL_STYLE[lvl]) for lvl in range(1, 5))
            echo(" " * MARGIN + style("less ", dim=True) + swatches
                 + style(" more", dim=True))

            # --- hour-of-day punchcard ------------------------------------
            echo("")
            echo(style("When you work  (by hour of day)", bold=True))
            max_hour = max(hour_counts.values())
            for hour in range(24):
                count = hour_counts.get(hour, 0)
                bar = style(_hbar(count, max_hour, BAR_WIDTH), fg="cyan")
                suffix = f" {count}" if count else ""
                echo(f"{hour:02d}  {bar}{suffix}")

            # --- insight stats --------------------------------------------
            active_days = len(day_counts)
            busy_day, busy_day_n = max(
                day_counts.items(), key=lambda kv: (kv[1], -kv[0].toordinal()))
            busy_hour, busy_hour_n = max(
                hour_counts.items(), key=lambda kv: (kv[1], -kv[0]))
            streak = _longest_streak(set(day_counts))

            echo("")
            echo(style("Insights", bold=True))

            def stat(label: str, value: str) -> None:
                echo(f"  {label:<18}{value}")

            stat("total commands", str(total))
            stat("active days", f"{active_days} of {days}")
            stat("busiest day", f"{busy_day.isoformat()} ({busy_day_n})")
            stat("busiest hour", f"{busy_hour:02d}:00 ({busy_hour_n})")
            stat("longest streak",
                 f"{streak} day" + ("" if streak == 1 else "s"))
            stat("avg / active day", f"{total / active_days:.1f}")
        finally:
            conn.close()
