"""chronx wrapped — a curated "your session, wrapped" highlight reel.

Think *Spotify Wrapped*, but for your shell: a shareable, personality-driven
recap of everything chronx has recorded, rendered as a colourful card with
box-drawing rules and emoji. Deliberately distinct from the dry tables of
``stats`` / ``summary`` / ``activity`` — this one is meant to be *fun*.

Everything shown is derived from the actual event log:

  * headline totals — commands run, active span, files changed, bytes written,
    store size;
  * 🏆 most-run command, 🔥 most-edited file, 🧊 a file you touched exactly once;
  * ⏰ peak hour of the day and 📅 the single busiest calendar day;
  * 💥 failure rate + the command that failed the most;
  * 🌱 files created vs 🗑️ deleted across all of history;
  * 🌿 how many timelines (branches) you explored;
  * ↩️ how many times you hit the panic button (undo / rollback);
  * 🌙 a night-owl / early-bird meter;
  * a closing personality "verdict" chosen from a small rule set.

Read-only: it imports ONLY ``chronx.pluginlib`` (as X) plus the stdlib, opens
the store read-only, never writes, and degrades gracefully on an empty store,
an untracked cwd, or a very thin history.
"""

from __future__ import annotations

import collections
import datetime
from pathlib import Path

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# tunables
# --------------------------------------------------------------------------- #
_RULE_WIDTH = 64          # inner width of the horizontal rules / banners
_LABEL_WIDTH = 22         # column width for the label text in stat rows
_FETCH_LIMIT = 10_000_000  # effectively "the whole history"
_THIN = 5                 # fewer events than this → the compact "come back" card

# Hour-of-day buckets for the night-owl / early-bird meter.
_NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4, 5})   # 22:00 – 05:59
_MORNING_HOURS = frozenset({6, 7, 8, 9, 10, 11})       # 06:00 – 11:59

# Commands (as recorded by ops.apply_undo / apply_rollback) that count as
# "hitting the panic button".
_PANIC_PREFIXES = ("chronx undo", "chronx rollback")


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
class _Reel:
    """Running aggregates accumulated over the events in scope.

    A single pass over every in-scope event feeds these counters/sums; the
    rendering step below reads them back out. Keeping them on one object avoids
    threading a dozen locals through the plotting code.
    """

    def __init__(self) -> None:
        self.n_events = 0                                   # every event
        self.n_commands = 0                                 # events with a command
        self.n_external = 0                                 # command IS NULL
        self.n_completed = 0                                # events with an exit_code
        self.n_failed = 0                                   # exit_code not in (None, 0)
        self.n_panic = 0                                    # undo / rollback events
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.bytes_written = 0                              # Σ after_size over deltas
        self.n_deltas = 0                                   # total file-change records
        self.n_created = 0                                  # 'A' deltas
        self.n_deleted = 0                                  # 'D' deltas
        self.cmd_counts: collections.Counter[str] = collections.Counter()
        self.fail_counts: collections.Counter[str] = collections.Counter()
        self.file_counts: collections.Counter[str] = collections.Counter()
        self.hour_counts: collections.Counter[int] = collections.Counter()
        self.day_counts: collections.Counter[datetime.date] = collections.Counter()

    def add_event(self, ev: "X.sqlite3.Row", deltas: list["X.dbm.Delta"]) -> None:
        self.n_events += 1

        ts = float(ev["started_at"])
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
        dt = datetime.datetime.fromtimestamp(ts)
        self.hour_counts[dt.hour] += 1
        self.day_counts[dt.date()] += 1

        command = ev["command"]
        if command is None:
            self.n_external += 1
        else:
            self.n_commands += 1
            self.cmd_counts[X.describe_command(ev)] += 1
            if command.startswith(_PANIC_PREFIXES):
                self.n_panic += 1

        exit_code = ev["exit_code"]
        if exit_code is not None:
            self.n_completed += 1
            if exit_code != 0:
                self.n_failed += 1
                self.fail_counts[X.describe_command(ev)] += 1

        for d in deltas:
            self.n_deltas += 1
            self.file_counts[d.path] += 1
            if d.after_size:
                self.bytes_written += int(d.after_size)
            if d.change == "A":
                self.n_created += 1
            elif d.change == "D":
                self.n_deleted += 1

    # -- derived read-outs ------------------------------------------------- #
    @property
    def failure_rate(self) -> float:
        """Fraction of completed commands that exited non-zero (0.0 if none)."""
        return self.n_failed / self.n_completed if self.n_completed else 0.0

    @property
    def night_frac(self) -> float:
        total = sum(self.hour_counts.values())
        if not total:
            return 0.0
        return sum(self.hour_counts[h] for h in _NIGHT_HOURS) / total

    @property
    def morning_frac(self) -> float:
        total = sum(self.hour_counts.values())
        if not total:
            return 0.0
        return sum(self.hour_counts[h] for h in _MORNING_HOURS) / total


def _collect(conn: "X.sqlite3.Connection",
             targets: list[tuple["X.sqlite3.Row", int | None]]) -> _Reel:
    """Fold every event of every (root, branch) target into one :class:`_Reel`."""
    reel = _Reel()
    for root, branch_id in targets:
        events = X.dbm.recent_events(
            conn, root_id=int(root["id"]), limit=_FETCH_LIMIT, branch_id=branch_id
        )
        for ev in events:
            reel.add_event(ev, X.dbm.deltas_for(conn, int(ev["id"])))
    return reel


# --------------------------------------------------------------------------- #
# personality verdict — small ordered rule set, first match wins
# --------------------------------------------------------------------------- #
def _verdict(reel: _Reel) -> tuple[str, str]:
    """Pick a playful ``(emoji, label)`` verdict derived from the data."""
    n_files = len(reel.file_counts)
    # High failure rate (with enough evidence) → chaos gremlin.
    if reel.n_completed >= 4 and reel.failure_rate >= 0.30:
        return ("💣", "Move fast and break things")
    # Lots of panic-button presses (absolute or proportional) → cautious.
    if reel.n_panic >= 3 or (
        reel.n_commands and reel.n_panic / reel.n_commands >= 0.15
    ):
        return ("🧹", "The careful refactorer")
    # Most work happens deep in the night.
    if reel.night_frac >= 0.40:
        return ("🦉", "Nocturnal hacker")
    # Most work happens in the morning.
    if reel.morning_frac >= 0.50:
        return ("🐦", "The early bird")
    # Few commands, but each touched many files → surgical.
    if 0 < reel.n_commands <= 40 and n_files >= 3 * reel.n_commands:
        return ("🎯", "Surgical striker")
    # Sheer volume.
    if reel.n_commands >= 100:
        return ("🏭", "The command-line machine")
    return ("🚀", "Steady & productive")


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #
def _rule(char: str = "━") -> str:
    """A full-width horizontal rule, dimmed."""
    return X.click.style(char * _RULE_WIDTH, dim=True)


def _banner(text: str, *, fg: str = "bright_magenta") -> None:
    """A centred, bold line between two heavy rules — used for title & verdict."""
    echo = X.click.echo
    echo(_rule())
    echo("  " + X.click.style(text, fg=fg, bold=True))
    echo(_rule())


def _row(emoji: str, label: str, value: str) -> None:
    """One aligned ``emoji  label…………  value`` stat line.

    Every row leads with a single emoji + two spaces, so the label column starts
    at a consistent offset; ``value`` follows a fixed-width label field. ANSI
    styling is only ever applied to ``value`` (never counted for padding).
    """
    X.click.echo(f"  {emoji}  {label:<{_LABEL_WIDTH}}{value}")


def _num(n: int, fg: str) -> str:
    """A count, coloured when non-zero, dimmed when exactly zero."""
    return X.click.style(str(n), fg=fg) if n else X.click.style("0", dim=True)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many if many is not None else one + "s")


def _clip(text: str, width: int = 34) -> str:
    """Trim an over-long value (command / path) to keep the card tidy."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _fmt_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).date().isoformat()


# --------------------------------------------------------------------------- #
# card sections
# --------------------------------------------------------------------------- #
def _headline(reel: _Reel) -> None:
    """The "by the numbers" block: totals + active span + store size."""
    echo, style = X.click.echo, X.click.style
    n_days = 0
    if reel.first_ts is not None and reel.last_ts is not None:
        d0 = datetime.datetime.fromtimestamp(reel.first_ts).date()
        d1 = datetime.datetime.fromtimestamp(reel.last_ts).date()
        n_days = (d1 - d0).days + 1

    echo("")
    _row("📊", "Commands run",
         style(str(reel.n_commands), fg="cyan", bold=True)
         + (style(f"  (+{reel.n_external} external)", dim=True)
            if reel.n_external else ""))
    _row("📁", "Files changed",
         _num(reel.n_deltas, "magenta")
         + style(f"  across {len(reel.file_counts)} "
                 f"{_plural(len(reel.file_counts), 'file')}", dim=True))
    _row("✍️", "Bytes written", style(X.human_bytes(reel.bytes_written), fg="green"))
    if reel.first_ts is not None and reel.last_ts is not None:
        span = f"{_fmt_date(reel.first_ts)} → {_fmt_date(reel.last_ts)}"
        _row("🗓️", "Active span",
             span + style(f"  ({n_days} {_plural(n_days, 'day')})", dim=True))

    # Whole-store footprint (all roots) — a fun "how big did it get" number.
    try:
        count, total = X.ObjectStore(X.paths().objects).disk_usage()
    except OSError:
        count, total = 0, 0
    _row("💾", "Store size",
         style(X.human_bytes(total), fg="yellow")
         + style(f"  ({count} {_plural(count, 'blob')})", dim=True))


def _highlights(reel: _Reel, conn: "X.sqlite3.Connection",
                targets: list[tuple["X.sqlite3.Row", int | None]]) -> None:
    """The curated highlight rows (only the ones the data supports)."""
    echo, style = X.click.echo, X.click.style
    echo("")

    # 🏆 most-run command
    if reel.cmd_counts:
        cmd, n = reel.cmd_counts.most_common(1)[0]
        _row("🏆", "Most-run command",
             style(_clip(cmd), fg="yellow")
             + style(f"  ×{n}", fg="bright_yellow", bold=True))

    # 🔥 most-edited file + 🧊 a file touched exactly once
    if reel.file_counts:
        path, n = reel.file_counts.most_common(1)[0]
        _row("🔥", "Most-edited file",
             style(_clip(path), fg="red")
             + style(f"  {n} {_plural(n, 'change')}", fg="bright_red", bold=True))
        once = sorted(p for p, c in reel.file_counts.items() if c == 1)
        if once:
            _row("🧊", "Touched just once", style(_clip(once[0]), dim=True))

    # ⏰ peak hour + 📅 busiest day
    if reel.hour_counts:
        hour, hn = max(reel.hour_counts.items(), key=lambda kv: (kv[1], -kv[0]))
        _row("⏰", "Peak hour",
             style(f"{hour:02d}:00", fg="cyan")
             + style(f"  ({hn} {_plural(hn, 'command')})", dim=True))
    if reel.day_counts:
        day, dn = max(reel.day_counts.items(),
                      key=lambda kv: (kv[1], -kv[0].toordinal()))
        _row("📅", "Busiest day",
             style(day.isoformat(), fg="cyan")
             + style(f"  ({dn} {_plural(dn, 'command')})", dim=True))

    # 💥 failure rate + worst offender
    if reel.n_completed:
        pct = reel.failure_rate * 100.0
        fg = "red" if pct >= 25 else ("yellow" if pct else "green")
        tail = ""
        if reel.fail_counts:
            worst, wn = reel.fail_counts.most_common(1)[0]
            tail = style(f"  (worst: {_clip(worst, 22)} ×{wn})", dim=True)
        _row("💥", "Failure rate",
             style(f"{pct:.0f}%", fg=fg, bold=True)
             + style(f"  {reel.n_failed}/{reel.n_completed}", dim=True) + tail)

    # 🌱 created vs 🗑️ deleted
    if reel.n_created or reel.n_deleted:
        _row("🌱", "Created vs deleted",
             _num(reel.n_created, "green") + style(" created", dim=True)
             + "  ·  " + _num(reel.n_deleted, "red") + style(" deleted", dim=True))

    # 🌿 timelines explored (only when there's more than one)
    n_branches = sum(len(X.dbm.list_branches(conn, int(r["id"])))
                     for r, _b in targets)
    if n_branches > 1:
        _row("🌿", "Timelines explored",
             style(str(n_branches), fg="green")
             + style(f"  {_plural(n_branches, 'branch', 'branches')} spun up",
                     dim=True))

    # ↩️ panic-button presses (undo / rollback)
    if reel.n_panic:
        _row("↩️", "Panic-button presses",
             style(str(reel.n_panic), fg="magenta", bold=True)
             + style(f"  undo/rollback {_plural(reel.n_panic, 'event')}", dim=True))

    # 🌙 night-owl / early-bird meter
    if reel.hour_counts:
        night = round(reel.night_frac * 100)
        morning = round(reel.morning_frac * 100)
        if night > morning:
            tag = style("🌙 night owl", fg="blue")
        elif morning > night:
            tag = style("🐦 early bird", fg="yellow")
        else:
            tag = style("balanced", dim=True)
        _row("🌗", "Body-clock",
             f"{night}% "
             + style("late-night", fg="blue")
             + f" · {morning}% " + style("morning", fg="yellow")
             + "  " + tag)


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option("--all-roots", is_flag=True,
                    help="Wrap up every tracked root together, not just the cwd's.")
    def wrapped(all_roots: bool) -> None:
        """🎁  Your chronx history, wrapped — a fun, shareable highlight reel."""
        echo, style = X.click.echo, X.click.style
        conn = X.open_db()  # ClickException if there's no store at all
        try:
            roots = X.dbm.get_roots(conn)
            if not roots:
                echo("Nothing to wrap up yet. 🎁")
                echo(style("Record some work with `chronx exec` (or the shell "
                           "hook), then come back.", dim=True))
                return

            # Resolve which (root, active-branch) pairs to summarise + a label.
            if all_roots:
                targets = [(r, X.active_branch_id(conn, int(r["id"])))
                           for r in roots]
                scope = f"all roots ({len(roots)})"
            else:
                root = X.dbm.root_for_path(conn, Path.cwd())
                if root is None:
                    echo(f"{Path.cwd()} isn't a tracked chronx root. 🎁")
                    echo(style(f"Try `chronx wrapped --all-roots` to wrap all "
                               f"{len(roots)} tracked {_plural(len(roots), 'root')}, "
                               "or cd into a project.", dim=True))
                    return
                branch_id = X.active_branch_id(conn, int(root["id"]))
                branch = (X.dbm.get_branch(conn, branch_id)
                          if branch_id is not None else None)
                bname = branch["name"] if branch is not None else "-"
                targets = [(root, branch_id)]
                scope = f"{Path(root['path']).name}  ·  [{bname}]"

            reel = _collect(conn, targets)

            # --- title banner --------------------------------------------- #
            _banner("🎁  YOUR CHRONX, WRAPPED")
            echo("  " + style(scope, fg="bright_cyan"))

            if reel.n_events == 0:
                echo("")
                echo("  No recorded activity here yet. 🎁")
                echo(style("  Run some commands under chronx, then come back for "
                           "your wrapped.", dim=True))
                echo(_rule())
                return

            # --- the numbers ---------------------------------------------- #
            _headline(reel)

            # --- thin history: a smaller card + gentle nudge -------------- #
            if reel.n_events < _THIN:
                echo("")
                echo("  " + style("🌱 Just getting started — ", fg="green")
                     + style("come back after more work for the full wrapped "
                             "(top commands, hotspots, your body-clock & a "
                             "personality verdict).", dim=True))
                echo(_rule())
                return

            # --- the full highlight reel ---------------------------------- #
            _highlights(reel, conn, targets)

            # --- closing personality verdict ------------------------------ #
            emoji, label = _verdict(reel)
            echo("")
            _banner(f"{emoji}  Your vibe: “{label}”", fg="bright_green")
        finally:
            conn.close()
