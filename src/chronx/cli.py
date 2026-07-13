"""The `chronx` command-line interface."""

from __future__ import annotations

import importlib.resources
import os
import sqlite3
import sys
from pathlib import Path

import click

from . import __version__
from . import daemon as daemonmod
from . import db as dbm
from .config import DEFAULT_CONFIG_TOML, Config, Paths
from .diffview import render_delta, stat_line
from .ops import (
    OpsError,
    apply_undo,
    blame_file,
    describe_command,
    find_undo_target,
    plan_undo,
    resolve_event,
)
from .store import HASH_ALGO, ObjectStore
from .when import fmt_ts

SHELLS = ("bash", "zsh")


def _paths() -> Paths:
    return Paths.from_env()


def _open_db(paths: Paths, *, readonly: bool = True) -> sqlite3.Connection:
    if not paths.db.exists():
        raise click.ClickException(
            f"no chronx store at {paths.home} — run `chronx init` first"
        )
    conn = dbm.connect(paths.db, readonly=readonly)
    if not readonly:
        dbm.init_db(conn)
    return conn


def _echo_diff_line(line: str) -> None:
    if line.startswith("+++") or line.startswith("---"):
        click.secho(line, bold=True)
    elif line.startswith("@@"):
        click.secho(line, fg="cyan")
    elif line.startswith("+"):
        click.secho(line, fg="green")
    elif line.startswith("-"):
        click.secho(line, fg="red")
    else:
        click.echo(line)


def _echo_event_header(row: sqlite3.Row) -> None:
    click.secho(f"event #{row['id']}", bold=True, nl=False)
    click.echo(f"  {fmt_ts(row['started_at'])}", nl=False)
    if row["exit_code"] is not None:
        color = "green" if row["exit_code"] == 0 else "red"
        click.echo("  exit ", nl=False)
        click.secho(str(row["exit_code"]), fg=color, nl=False)
    click.echo()
    click.echo(f"  cwd: {row['cwd']}")
    click.secho(f"  $ {describe_command(row)}", fg="yellow")


@click.group()
@click.version_option(__version__, prog_name="chronx")
def main() -> None:
    """Time-travel debugger for shell sessions.

    chronx records which files every shell command touched, so you can
    diff, blame, and undo your own workflow after the fact.
    """


# ------------------------------------------------------------------- init


@main.command()
def init() -> None:
    """Set up the ~/.chronx store and print the shell hook to source."""
    paths = _paths()
    paths.ensure()
    conn = dbm.connect(paths.db)
    dbm.init_db(conn)
    conn.close()
    if not paths.config.exists():
        paths.config.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    click.secho(f"chronx store ready at {paths.home}", fg="green")
    click.echo(f"  hashing:  {HASH_ALGO}")
    click.echo(f"  config:   {paths.config}")
    click.echo()

    shell = Path(os.environ.get("SHELL", "")).name
    ordered = [shell] if shell in SHELLS else []
    ordered += [s for s in SHELLS if s not in ordered]
    click.echo("Add the hook to your shell rc file (once), then restart the shell:")
    click.echo()
    for s in ordered:
        rc = "~/.bashrc" if s == "bash" else "~/.zshrc"
        marker = "  <- your shell" if s == shell else ""
        click.secho(f'  # {rc}{marker}', dim=True)
        click.echo(f'  eval "$(chronx hook {s})"')
        click.echo()
    click.echo("Then start the recorder:")
    click.echo()
    click.echo("  chronx daemon start")


@main.command()
@click.argument("shell", type=click.Choice(SHELLS))
def hook(shell: str) -> None:
    """Print the shell hook script for SHELL (bash or zsh)."""
    script = (
        importlib.resources.files("chronx") / "hooks" / f"chronx.{shell}"
    ).read_text(encoding="utf-8")
    click.echo(script, nl=False)


# ------------------------------------------------------------------ daemon


@main.group(invoke_without_command=True)
@click.pass_context
def daemon(ctx: click.Context) -> None:
    """Start/stop the background watcher."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(status)


@daemon.command()
def start() -> None:
    """Start the daemon in the background."""
    paths = _paths()
    if not paths.db.exists():
        raise click.ClickException("run `chronx init` first")
    try:
        pid = daemonmod.spawn(paths)
    except daemonmod.AlreadyRunning as exc:
        click.echo(f"daemon already running (pid {exc.pid})")
        return
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"daemon started (pid {pid}), log: {paths.log}", fg="green")


@daemon.command()
@click.option("--force", is_flag=True, help="SIGKILL instead of a graceful SIGTERM.")
def stop(force: bool) -> None:
    """Stop a running daemon."""
    paths = _paths()
    try:
        stopped = daemonmod.stop(paths, force=force)
    except TimeoutError as exc:
        raise click.ClickException(f"{exc} (try --force)") from exc
    if stopped:
        click.secho("daemon stopped", fg="green")
    else:
        click.echo("daemon is not running")


@daemon.command()
def status() -> None:
    """Show daemon and store status."""
    paths = _paths()
    pid = daemonmod.daemon_pid(paths)
    if pid is not None:
        click.secho(f"daemon: running (pid {pid})", fg="green")
    else:
        click.secho("daemon: not running", fg="yellow")
    if not paths.db.exists():
        click.echo(f"store:  not initialized ({paths.home})")
        return
    conn = _open_db(paths)
    try:
        events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        roots = dbm.get_roots(conn)
        click.echo(f"store:  {paths.home}")
        click.echo(f"  events:  {events}")
        click.echo(f"  objects: {ObjectStore(paths.objects).count()}")
        click.echo(f"  roots:   {len(roots)}")
        for r in roots:
            click.echo(f"    {r['path']}")
    finally:
        conn.close()


@daemon.command(hidden=True)
def run() -> None:
    """Run the daemon in the foreground (used internally by `start`)."""
    paths = _paths()
    try:
        daemonmod.run_foreground(paths)
    except daemonmod.AlreadyRunning as exc:
        raise click.ClickException(str(exc)) from exc


# -------------------------------------------------------------------- diff


@main.command()
@click.argument("time", default="last")
@click.option("--stat", is_flag=True, help="Only list changed files, no content diff.")
def diff(time: str, stat: bool) -> None:
    """Show the filesystem changes at TIME.

    TIME is an event id (from blame/replay), 'last', or a moment like
    '10m', '14:32', or '2026-07-13T14:32'.
    """
    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        try:
            event = resolve_event(conn, time, Path.cwd())
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        _echo_event_header(event)
        deltas = dbm.deltas_for(conn, int(event["id"]))
        if not deltas:
            click.echo("\n  (no filesystem changes)")
            return
        click.echo()
        for d in deltas:
            if stat:
                click.echo("  " + stat_line(d))
            else:
                for line in render_delta(store, d):
                    _echo_diff_line(line)
                click.echo()
    finally:
        conn.close()


# ------------------------------------------------------------------- blame


@main.command()
@click.argument("file", type=click.Path(path_type=Path))
def blame(file: Path) -> None:
    """Show which commands touched FILE, and when."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        try:
            rel, entries = blame_file(conn, file)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        if not entries:
            click.echo(f"{rel}: no recorded changes")
            return
        click.secho(f"{rel} — {len(entries)} recorded change(s), newest first:", bold=True)
        click.echo()
        for i, e in enumerate(entries):
            mark = "*" if i == 0 else " "
            exit_s = "" if e.exit_code is None else f"  exit {e.exit_code}"
            click.echo(
                f" {mark} #{e.event_id:<5} {fmt_ts(e.started_at)}  {e.change}{exit_s}"
            )
            click.secho(f"       $ {e.command}", fg="yellow")
        click.echo()
        click.secho(
            f"last touched by event #{entries[0].event_id} "
            f"(`chronx diff {entries[0].event_id}` to inspect)",
            dim=True,
        )
    finally:
        conn.close()


# -------------------------------------------------------------------- undo


@main.command()
@click.option("--event", "event_id", type=int, default=None,
              help="Undo a specific event instead of the last one.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--force", is_flag=True,
              help="Also revert files that changed again after the event.")
def undo(event_id: int | None, yes: bool, force: bool) -> None:
    """Revert the working directory to its state before the last command.

    Before touching anything, the current state of every affected file is
    snapshotted and the revert is recorded as an event of its own — so an
    undo can always be undone.
    """
    paths = _paths()
    conn = _open_db(paths, readonly=False)
    store = ObjectStore(paths.objects)
    try:
        try:
            if event_id is not None:
                event = dbm.event_by_id(conn, event_id)
                if event is None:
                    raise OpsError(f"no event with id {event_id}")
            else:
                event = find_undo_target(conn, Path.cwd())
            plan = plan_undo(conn, store, event)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc

        _echo_event_header(plan.event)
        click.echo()
        click.secho("undoing this event would:", bold=True)
        for step in plan.steps:
            verb = "delete " if step.action == "remove" else "restore"
            line = f"  {verb} {step.rel}"
            if step.conflict:
                click.secho(line + "   [CONFLICT: changed again since]", fg="red")
            else:
                click.echo(line)

        conflicted = plan.conflicts
        if conflicted and not force:
            click.echo()
            click.secho(
                f"{len(conflicted)} file(s) changed again after this event; "
                "they will be SKIPPED. Use --force to revert them too.",
                fg="yellow",
            )
        if not any(not s.conflict for s in plan.steps) and not force:
            raise click.ClickException(
                "every file conflicts; nothing would be reverted (use --force)"
            )

        if not yes:
            click.echo()
            if not click.confirm("Proceed?", default=False):
                click.echo("aborted")
                return

        try:
            backup_id, applied = apply_undo(
                conn, store, paths, plan, skip_conflicts=not force
            )
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo()
        click.secho(f"reverted {len(applied)} file(s):", fg="green")
        for rel in applied:
            click.echo(f"  {rel}")
        click.secho(
            f"\nthe revert was recorded as event #{backup_id}; "
            f"`chronx undo --event {backup_id}` re-applies the command's changes.",
            dim=True,
        )
    finally:
        conn.close()


# ------------------------------------------------------------------ replay


@main.command()
@click.option("--limit", default=500, show_default=True,
              help="How many recent events to load.")
@click.option("--all-roots", is_flag=True,
              help="Show every tracked directory, not just the current one.")
def replay(limit: int, all_roots: bool) -> None:
    """Interactive timeline of your session (TUI). Scrub with arrow keys."""
    paths = _paths()
    conn = _open_db(paths)
    conn.close()  # existence check only; the TUI opens its own connection
    from .tui import ReplayApp  # deferred: textual import is slow

    ReplayApp(paths, limit=limit, all_roots=all_roots).run()


if __name__ == "__main__":
    main()
