"""The `chronx` command-line interface."""

from __future__ import annotations

import importlib.resources
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import click

from . import __version__
from . import daemon as daemonmod
from . import db as dbm
from .config import DEFAULT_CONFIG_TOML, Config, Paths
from .diffview import render_delta, stat_line
from .ops import (
    OpsError,
    apply_rollback,
    apply_undo,
    blame_file,
    content_at,
    describe_command,
    find_undo_target,
    plan_rollback,
    plan_undo,
    prune,
    resolve_event,
    restore_file,
)
from .store import HASH_ALGO, ObjectStore, hash_bytes
from .when import WhenParseError, fmt_ts, parse_when


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def _parse_at(at: str | None) -> float | None:
    if at is None:
        return None
    try:
        return parse_when(at)
    except WhenParseError as exc:
        raise click.ClickException(str(exc)) from exc

SHELLS = ("bash", "zsh", "fish")
_RC_FILES = {"bash": "~/.bashrc", "zsh": "~/.zshrc", "fish": "~/.config/fish/config.fish"}


def _hook_line(shell: str) -> str:
    if shell == "fish":
        return "chronx hook fish | source"
    return f'eval "$(chronx hook {shell})"'


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
        marker = "  <- your shell" if s == shell else ""
        click.secho(f"  # {_RC_FILES[s]}{marker}", dim=True)
        click.echo(f"  {_hook_line(s)}")
        click.echo()
    click.echo("Then start the recorder:")
    click.echo()
    click.echo("  chronx daemon start")


@main.command()
@click.argument("shell", type=click.Choice(SHELLS))
def hook(shell: str) -> None:
    """Print the shell hook script for SHELL (bash, zsh, or fish)."""
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
        blobs, stored = ObjectStore(paths.objects).disk_usage()
        click.echo(f"store:  {paths.home}")
        click.echo(f"  events:  {events}")
        click.echo(f"  objects: {blobs} ({_human_bytes(stored)} compressed)")
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

    TIME is an event id (from blame/replay), 'last', a mark name, or a
    moment like '10m', '14:32', or '2026-07-13T14:32'.
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


# --------------------------------------------------------------------- log


@main.command("log")
@click.option("--limit", "-n", default=30, show_default=True,
              help="How many recent events to show.")
@click.option("--all-roots", is_flag=True,
              help="Every tracked directory, not just the current one.")
@click.option("--changes-only", "-c", is_flag=True,
              help="Hide commands that changed no files.")
def log_cmd(limit: int, all_roots: bool, changes_only: bool) -> None:
    """Print the recent event timeline (newest last), like a quick git log."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        root_id = None
        if not all_roots:
            root = dbm.root_for_path(conn, Path.cwd())
            root_id = int(root["id"]) if root is not None else None
        rows = dbm.recent_events(
            conn, root_id=root_id, limit=limit, changes_only=changes_only
        )
        if not rows:
            click.echo("no events recorded" + ("" if all_roots else " for this directory"))
            return
        for r in rows:
            counts = dbm.delta_counts(conn, int(r["id"]))
            total = sum(counts.values())
            delta_s = (
                click.style(f"±{total:<3}", fg="magenta", bold=True)
                if total
                else click.style("·   ", dim=True)
            )
            exit_code = r["exit_code"]
            exit_s = (
                click.style(" - ", dim=True)
                if exit_code is None
                else click.style(f"{exit_code:>3}", fg="green" if exit_code == 0 else "red")
            )
            cmd = describe_command(r)
            cmd = cmd if len(cmd) <= 100 else cmd[:97] + "..."
            cmd_s = click.style(cmd, fg="yellow" if r["command"] else None,
                                dim=r["command"] is None)
            click.echo(
                f"#{r['id']:<5} {fmt_ts(r['started_at'])}  {delta_s} {exit_s}  {cmd_s}"
            )
    finally:
        conn.close()


# --------------------------------------------------------------------- cat


@main.command()
@click.argument("file", type=click.Path(path_type=Path))
@click.option("--at", "-t", default=None,
              help="Moment to read at ('10m', '14:32', ISO...). Default: latest snapshot.")
@click.option("--event", "-e", "event_id", type=int, default=None,
              help="Read the state at a specific event instead of a time.")
@click.option("--before", is_flag=True,
              help="State just BEFORE the selected event, not after it.")
def cat(file: Path, at: str | None, event_id: int | None, before: bool) -> None:
    """Print FILE's recorded content at a moment in time (to stdout)."""
    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        try:
            state = content_at(
                conn, file, at=_parse_at(at), event_id=event_id, before=before
            )
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        if state.digest is None:
            raise click.ClickException(
                f"{state.rel} did not exist {state.source}"
            )
        try:
            data = store.get(state.digest)
        except (KeyError, ValueError) as exc:
            raise click.ClickException(f"blob unavailable: {exc}") from exc
        click.echo(f"# {state.rel} — {state.source}", err=True)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    finally:
        conn.close()


# ----------------------------------------------------------------- restore


@main.command()
@click.argument("file", type=click.Path(path_type=Path))
@click.option("--at", "-t", default=None,
              help="Moment to restore to ('10m', '14:32', ISO...).")
@click.option("--event", "-e", "event_id", type=int, default=None,
              help="Restore the state at a specific event.")
@click.option("--before", is_flag=True,
              help="State just BEFORE the selected event (undo that event for this file).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def restore(
    file: Path, at: str | None, event_id: int | None, before: bool, yes: bool
) -> None:
    """Restore a single FILE to its state at a moment in time.

    Typical flow: `chronx blame FILE` to find the event that broke it, then
    `chronx restore FILE -e <id> --before`. Like undo, the current content
    is snapshotted first, so a restore is always reversible.
    """
    paths = _paths()
    conn = _open_db(paths, readonly=False)
    store = ObjectStore(paths.objects)
    try:
        try:
            state = content_at(
                conn, file, at=_parse_at(at), event_id=event_id, before=before
            )
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc

        click.secho(f"restore {state.rel}", bold=True)
        click.echo(f"  to:   {state.source}")
        if state.digest is None:
            click.secho("  the file did not exist then — it will be DELETED", fg="red")
        else:
            click.echo(f"  size: {state.size} bytes, blob {state.digest[:12]}")
        if not yes and not click.confirm("Proceed?", default=False):
            click.echo("aborted")
            return
        try:
            backup_id, action = restore_file(conn, store, paths, state)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"{state.rel}: {action}", fg="green")
        click.secho(
            f"recorded as event #{backup_id} (restore it to go back)", dim=True
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------- gc


@main.command()
@click.option("--keep-days", default=30.0, show_default=True, type=float,
              help="Keep events newer than this many days.")
@click.option("--dry-run", is_flag=True, help="Report what would be freed, change nothing.")
def gc(keep_days: float, dry_run: bool) -> None:
    """Prune old events and delete blobs nothing references anymore.

    The daemon must be stopped first (it caches manifests and dedups
    against the object store).
    """
    paths = _paths()
    pid = daemonmod.daemon_pid(paths)
    if pid is not None and not dry_run:
        raise click.ClickException(
            f"daemon is running (pid {pid}) — stop it first: chronx daemon stop"
        )
    conn = _open_db(paths, readonly=dry_run)
    try:
        stats = prune(
            conn, ObjectStore(paths.objects), keep_days=keep_days, dry_run=dry_run
        )
    finally:
        conn.close()
    verb = "would delete" if dry_run else "deleted"
    click.echo(
        f"{verb} {stats.events_deleted} event(s), {stats.deltas_deleted} delta row(s), "
        f"{stats.blobs_deleted} blob(s) ({_human_bytes(stats.bytes_freed)})"
    )
    click.echo(f"kept {stats.blobs_kept} referenced blob(s)")
    if dry_run:
        click.secho("dry run: nothing was changed", dim=True)


# ------------------------------------------------------------- mark / marks

_MARK_NAME = re.compile(r"^[A-Za-z][\w.-]*$")


@main.command()
@click.argument("name")
@click.option("--at", "-t", default=None,
              help="Moment to mark ('10m', '14:32'...). Default: now.")
@click.option("--delete", "-d", "delete_", is_flag=True, help="Delete the mark instead.")
def mark(name: str, at: str | None, delete_: bool) -> None:
    """Name the current moment so you can diff/rollback to it later.

    `chronx mark before-refactor` ... hack hack hack ...
    `chronx rollback before-refactor` puts everything back.
    """
    paths = _paths()
    conn = _open_db(paths, readonly=False)
    try:
        if delete_:
            if dbm.delete_mark(conn, name):
                click.secho(f"mark {name!r} deleted", fg="green")
            else:
                raise click.ClickException(f"no mark named {name!r}")
            return
        if not _MARK_NAME.match(name) or name in ("last", "now"):
            raise click.ClickException(
                "mark names must start with a letter and use only letters, "
                "digits, '.', '-', '_' (and not be 'last'/'now')"
            )
        ts = _parse_at(at) if at is not None else time.time()
        root = dbm.root_for_path(conn, Path.cwd())
        try:
            dbm.add_mark(conn, name, ts, int(root["id"]) if root else None)
        except sqlite3.IntegrityError:
            raise click.ClickException(
                f"mark {name!r} already exists (delete it with `chronx mark -d {name}`)"
            ) from None
        click.secho(f"marked {fmt_ts(ts)} as {name!r}", fg="green")
        click.secho(f"  chronx diff {name}   /   chronx rollback {name}", dim=True)
    finally:
        conn.close()


@main.command()
def marks() -> None:
    """List named marks."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        rows = dbm.list_marks(conn)
        if not rows:
            click.echo("no marks (create one with `chronx mark <name>`)")
            return
        for r in rows:
            click.echo(f"  {r['name']:<24} {fmt_ts(r['ts'])}")
    finally:
        conn.close()


# ---------------------------------------------------------------- rollback


@main.command()
@click.argument("moment")
@click.option("--path", "path_prefix", default=None,
              help="Only roll back files under this prefix (relative to the root).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--dry-run", is_flag=True, help="Show the plan and stop.")
def rollback(moment: str, path_prefix: str | None, yes: bool, dry_run: bool) -> None:
    """Revert the whole working directory to its state at MOMENT.

    MOMENT is a mark name, a time ('10m', '14:32', ISO...), or an event id
    (meaning: the state just after that event). Untouched files are left
    alone; everything is applied as ONE recorded event, so a rollback is
    itself reversible with `chronx undo`.
    """
    paths = _paths()
    conn = _open_db(paths, readonly=dry_run)
    store = ObjectStore(paths.objects)
    try:
        mark_row = dbm.get_mark(conn, moment)
        if mark_row is not None:
            ts = float(mark_row["ts"])
            label = f"mark {moment!r} ({fmt_ts(ts)})"
        elif moment.lstrip("#").isdigit() and float(moment.lstrip("#")) < 1e9:
            event = dbm.event_by_id(conn, int(moment.lstrip("#")))
            if event is None:
                raise click.ClickException(f"no event with id {moment}")
            ts = float(event["started_at"])
            label = f"after event #{event['id']} ({fmt_ts(ts)})"
        else:
            ts = _parse_at(moment) or time.time()
            label = fmt_ts(ts)

        try:
            plan = plan_rollback(conn, store, Path.cwd(), ts, label,
                                 path_prefix=path_prefix)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc

        if not plan.steps:
            click.secho(f"already at the state of {label}; nothing to do", fg="green")
            return
        click.secho(f"rolling back {plan.root} to {label}:", bold=True)
        shown = 0
        for step in plan.steps:
            if shown >= 40:
                click.secho(f"  ... and {len(plan.steps) - shown} more", dim=True)
                break
            shown += 1
            verb = {"restore": "restore", "delete": "delete ", "create": "recreate"}[
                step.action
            ]
            line = f"  {verb} {step.rel}"
            if step.blocked:
                click.secho(f"{line}   [SKIPPED: {step.blocked}]", fg="red")
            else:
                click.echo(line)
        if plan.blocked:
            click.secho(
                f"{len(plan.blocked)} file(s) cannot be rolled back and will be "
                "skipped (see above)",
                fg="yellow",
            )
        if dry_run:
            click.secho("dry run: nothing was changed", dim=True)
            return
        if not yes and not click.confirm(
            f"Roll back {len(plan.applicable)} file(s)?", default=False
        ):
            click.echo("aborted")
            return
        try:
            event_id, changed = apply_rollback(conn, store, paths, plan)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"rolled back {changed} file(s) to {label}", fg="green")
        click.secho(
            f"recorded as event #{event_id}; `chronx undo --event {event_id}` "
            "reverts the rollback",
            dim=True,
        )
    finally:
        conn.close()


# ------------------------------------------------------------------ search


@main.command()
@click.argument("pattern")
@click.option("--content", "-S", is_flag=True,
              help="Pickaxe: search lines ADDED or REMOVED by commands (regex).")
@click.option("--limit", "-n", default=50, show_default=True,
              help="Max results (or, with -S, max file-changing events scanned).")
@click.option("--all-roots", is_flag=True,
              help="Search every tracked directory, not just the current one.")
def search(pattern: str, content: bool, limit: int, all_roots: bool) -> None:
    """Find commands by name, or find WHICH command touched a line (-S).

    `chronx search 'npm install'` greps command strings.
    `chronx search -S 'DEBUG *= *True'` finds the events whose file changes
    added or removed lines matching the regex — "when did this line change?"
    """
    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        root_id = None
        if not all_roots:
            root = dbm.root_for_path(conn, Path.cwd())
            root_id = int(root["id"]) if root is not None else None

        if not content:
            rows = dbm.search_commands(conn, pattern, root_id=root_id, limit=limit)
            if not rows:
                click.echo("no matching commands")
                return
            for r in reversed(rows):
                counts = dbm.delta_counts(conn, int(r["id"]))
                total = sum(counts.values())
                click.echo(
                    f"#{r['id']:<5} {fmt_ts(r['started_at'])}  "
                    f"{'±' + str(total) if total else '·':<4}  "
                    + click.style(describe_command(r), fg="yellow")
                )
            return

        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise click.ClickException(f"bad regex: {exc}") from exc
        events = dbm.recent_events(
            conn, root_id=root_id, limit=limit, changes_only=True
        )
        hits = 0
        for event in reversed(events):  # newest first
            matched: list[tuple[str, list[str]]] = []
            for d in dbm.deltas_for(conn, int(event["id"])):
                lines = [
                    line
                    for line in render_delta(store, d, max_lines=2000)
                    if line[:1] in "+-"
                    and not line.startswith(("+++", "---"))
                    and rx.search(line[1:])
                ]
                if lines:
                    matched.append((d.path, lines[:5]))
            if not matched:
                continue
            hits += 1
            click.secho(
                f"#{event['id']}  {fmt_ts(event['started_at'])}  "
                f"$ {describe_command(event)}",
                bold=True,
            )
            for path, lines in matched:
                click.echo(f"  {path}:")
                for line in lines:
                    _echo_diff_line("    " + line)
            click.echo()
        if not hits:
            click.echo(
                f"no added/removed lines match /{pattern}/ in the last "
                f"{len(events)} file-changing event(s); raise -n to scan further back"
            )
    finally:
        conn.close()


# -------------------------------------------------------------------- fsck


@main.command()
def fsck() -> None:
    """Verify store integrity: every blob decompresses and re-hashes to its
    name, and every hash the event log references actually exists on disk."""
    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        refs = dbm.referenced_hashes(conn)
    finally:
        conn.close()

    present: set[str] = set()
    corrupt: list[str] = []
    checked = 0
    for digest, _path, _size in store.iter_blobs():
        present.add(digest)
        checked += 1
        try:
            if hash_bytes(store.get(digest)) != digest:
                corrupt.append(digest)
        except (KeyError, ValueError):
            corrupt.append(digest)
    missing = sorted(refs - present)
    orphans = len(present - refs)

    click.echo(f"checked {checked} blob(s): {len(corrupt)} corrupt")
    for d in corrupt[:10]:
        click.secho(f"  corrupt: {d}", fg="red")
    click.echo(f"referenced hashes: {len(refs)}, missing from disk: {len(missing)}")
    for d in missing[:10]:
        click.secho(f"  missing: {d[:16]}... (history for it cannot be shown/restored)",
                    fg="red")
    click.echo(f"unreferenced blobs: {orphans} (reclaim with `chronx gc`)")
    if corrupt or missing:
        raise click.exceptions.Exit(1)
    click.secho("store is healthy", fg="green")


# ------------------------------------------------------------------- stats


@main.command()
@click.option("--all-roots", is_flag=True,
              help="Aggregate every tracked directory, not just the current one.")
@click.option("--top", default=10, show_default=True, help="Rows per leaderboard.")
def stats(all_roots: bool, top: int) -> None:
    """Where does your churn actually go? Hot files and noisy commands."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        scope = ""
        params: list[object] = []
        if not all_roots:
            root = dbm.root_for_path(conn, Path.cwd())
            if root is not None:
                scope = " AND e.root_id = ?"
                params = [int(root["id"])]
        totals = conn.execute(
            f"SELECT COUNT(*) AS n, SUM(command IS NULL) AS ext,"
            f" SUM(exit_code IS NOT NULL AND exit_code != 0) AS failed,"
            f" MIN(started_at) AS first, MAX(started_at) AS last"
            f" FROM events e WHERE 1=1{scope}",
            params,
        ).fetchone()
        if not totals["n"]:
            click.echo("no events recorded" + ("" if all_roots else " for this directory"))
            return
        changing = conn.execute(
            f"SELECT COUNT(DISTINCT e.id) AS n FROM events e"
            f" JOIN deltas d ON d.event_id = e.id WHERE 1=1{scope}",
            params,
        ).fetchone()["n"]
        click.secho("events", bold=True)
        click.echo(
            f"  {totals['n']} total, {changing} changed files, "
            f"{totals['ext'] or 0} external, {totals['failed'] or 0} failed"
        )
        click.echo(f"  from {fmt_ts(totals['first'])} to {fmt_ts(totals['last'])}")

        click.secho("\nhottest files (by number of changes)", bold=True)
        for r in conn.execute(
            f"SELECT d.path, COUNT(*) AS n FROM deltas d"
            f" JOIN events e ON e.id = d.event_id WHERE 1=1{scope}"
            f" GROUP BY d.path ORDER BY n DESC, d.path LIMIT ?",
            params + [top],
        ):
            click.echo(f"  {r['n']:>4}  {r['path']}")

        click.secho("\nnoisiest commands (by files changed)", bold=True)
        for r in conn.execute(
            f"SELECT e.command AS command, COUNT(*) AS n FROM deltas d"
            f" JOIN events e ON e.id = d.event_id WHERE e.command IS NOT NULL{scope}"
            f" GROUP BY e.command ORDER BY n DESC LIMIT ?",
            params + [top],
        ):
            cmd = r["command"] if len(r["command"]) <= 80 else r["command"][:77] + "..."
            click.echo(f"  {r['n']:>4}  {click.style(cmd, fg='yellow')}")

        blobs, stored = ObjectStore(paths.objects).disk_usage()
        click.secho("\nstore", bold=True)
        click.echo(f"  {blobs} unique blob(s), {_human_bytes(stored)} compressed")
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
