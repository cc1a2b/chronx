"""chronx monitor — a live dashboard that tails recording as it happens.

``chronx monitor`` is ``watch`` for your chronx timeline: it polls the store on
a fixed interval and prints every new event the daemon records the moment it
lands, alongside a running session counter (commands seen, files changed,
failures, elapsed time). Where ``chronx tail`` is a simple follower, this is the
richer live view — meant to be left running in a pane while you work.

Strictly read-only. Each poll opens a *fresh* read-only connection, reads, and
closes it — the command never holds a handle open across sleeps, so it never
blocks the daemon's writes. If the store or daemon disappears mid-run the loop
degrades to a dim ``waiting…`` and keeps polling until the store returns.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from chronx import pluginlib as X

# Per-change-kind colour for the ±A/M/D badge (chronx's usual A/M/D palette).
_A_COLOR = "green"
_M_COLOR = "yellow"
_D_COLOR = "red"


# --------------------------------------------------------------------- styling


def _clock(ts: float) -> str:
    """Wall-clock ``HH:MM:SS`` for one event's start time (machine-local zone)."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _duration(seconds: float) -> str:
    """A monotonic elapsed span rendered as zero-padded ``HH:MM:SS``."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _badge(counts: dict[str, int]) -> str:
    """A compact coloured ``+A ~M -D`` badge; a dim marker when nothing changed.

    Only nonzero kinds are shown, so a command that added one file reads
    ``+1`` rather than ``+1 ~0 -0``; a command that touched nothing (e.g. a
    failing shell command) reads a dim ``· no Δ``.
    """
    parts: list[str] = []
    if counts["A"]:
        parts.append(X.click.style(f"+{counts['A']}", fg=_A_COLOR, bold=True))
    if counts["M"]:
        parts.append(X.click.style(f"~{counts['M']}", fg=_M_COLOR, bold=True))
    if counts["D"]:
        parts.append(X.click.style(f"-{counts['D']}", fg=_D_COLOR, bold=True))
    return " ".join(parts) if parts else X.click.style("· no Δ", dim=True)


def _exit_mark(code: int | None) -> str:
    """Render an event's exit status: ``✓`` ok, ``✗N`` failed (red), ``·`` unknown."""
    if code is None:
        return X.click.style("·", dim=True)
    if code == 0:
        return X.click.style("✓", fg="green")
    return X.click.style(f"✗{code}", fg="red", bold=True)


def _command_str(event: X.sqlite3.Row) -> str:
    """The producing command as ``$ <cmd>`` (whitespace collapsed), or a dim
    ``(external change)`` for a change chronx recorded with no owning command."""
    cmd = event["command"]
    if cmd is None:
        return X.click.style("(external change)", dim=True)
    return X.click.style("$ ", dim=True) + " ".join(cmd.split())


# ------------------------------------------------------------------ registration


def register(main) -> None:
    @main.command()
    @X.click.option("--interval", default=1.0, show_default=True, help="Poll seconds.")
    @X.click.option("--stat", is_flag=True, help="Show changed-file lists.")
    @X.click.option(
        "--all-roots", is_flag=True, help="Watch every root, not just the cwd's."
    )
    def monitor(interval: float, stat: bool, all_roots: bool) -> None:
        """Live-tail events as the daemon records them, with a session counter."""
        # --- resolve what we're watching (any clean error happens pre-loop) ----
        conn = X.open_db()
        try:
            if all_roots:
                root_id: int | None = None
                branch_id: int | None = None
                where = "all roots"
            else:
                # ClickException here if the cwd isn't inside a tracked root.
                root = X.root_for_cwd(conn)
                root_id = int(root["id"])
                branch_id = X.active_branch_id(conn, root_id)
                where = str(root["path"])
            # Baseline: everything already in the store is "already seen".
            last_seen = X.dbm.max_event_id(conn)
        finally:
            conn.close()

        started = time.monotonic()
        seen_events = 0  # commands observed live this session
        seen_changes = 0  # A+M+D file changes across those commands
        seen_failures = 0  # commands with a nonzero exit code
        waiting = False  # True while the store is unreachable

        # Header + hints go to stderr, keeping stdout a clean, greppable log.
        X.click.echo(
            X.click.style(f"watching {where} — Ctrl-C to stop", bold=True), err=True
        )
        if last_seen == 0:
            X.click.echo(X.click.style("no events yet, waiting…", dim=True), err=True)

        def emit_event(active: X.sqlite3.Connection, event: X.sqlite3.Row) -> None:
            """Print one event line (+ optional stat) and fold it into the totals."""
            nonlocal seen_events, seen_changes, seen_failures
            eid = int(event["id"])
            counts = X.dbm.delta_counts(active, eid)
            nchanges = counts["A"] + counts["M"] + counts["D"]
            code = event["exit_code"]

            seen_events += 1
            seen_changes += nchanges
            if code not in (None, 0):
                seen_failures += 1

            X.click.echo(
                X.click.style(f"[{_clock(float(event['started_at']))}]", dim=True)
                + " "
                + X.click.style(f"#{eid}", fg="cyan", bold=True)
                + "  "
                + _badge(counts)
                + "  "
                + _exit_mark(code)
                + "  "
                + _command_str(event)
            )
            if stat and nchanges:
                for delta in X.dbm.deltas_for(active, eid):
                    X.click.echo("    " + X.stat_line(delta))

        def emit_footer() -> None:
            """A compact running status footer (to stderr, below the event log)."""
            X.click.echo(
                X.click.style(
                    f"── {seen_events} cmd · {seen_changes} Δ · {seen_failures} ✗"
                    f" · {_duration(time.monotonic() - started)} ──",
                    dim=True,
                ),
                err=True,
            )

        def note_waiting() -> None:
            """Enter the 'store unreachable' state, announcing it once."""
            nonlocal waiting
            if not waiting:
                X.click.echo(X.click.style("waiting…", dim=True), err=True)
                waiting = True

        # --- poll loop ---------------------------------------------------------
        try:
            while True:
                # Sleep first so a fresh baseline settles; also the point where a
                # Ctrl-C (SIGINT) unwinds cleanly as KeyboardInterrupt.
                time.sleep(max(interval, 0.05))

                # A brand-new read-only connection per poll — never held open
                # across the sleep, so the daemon's writer is never blocked.
                try:
                    conn = X.open_db()
                except (X.click.ClickException, X.sqlite3.Error, OSError):
                    note_waiting()  # store/daemon vanished — keep polling
                    continue

                try:
                    new = X.dbm.events_after(
                        conn,
                        last_seen,
                        root_id=root_id,
                        changes_only=False,
                        branch_id=branch_id,
                    )
                    for event in new:
                        emit_event(conn, event)
                    last_seen = X.dbm.max_event_id(conn)
                except X.sqlite3.Error:
                    note_waiting()  # store went away mid-read
                    continue
                finally:
                    conn.close()

                if waiting:
                    waiting = False  # store came back
                if new:
                    emit_footer()
                sys.stdout.flush()
                sys.stderr.flush()
        except KeyboardInterrupt:
            # Clean exit (0): break the current line, then a final summary.
            X.click.echo(err=True)
            X.click.echo(
                X.click.style(
                    f"── stopped — {seen_events} command(s), {seen_changes} change(s),"
                    f" {seen_failures} failure(s) over"
                    f" {_duration(time.monotonic() - started)} ──",
                    bold=True,
                ),
                err=True,
            )
