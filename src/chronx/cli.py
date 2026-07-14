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
    bisect_history,
    blame_file,
    content_at,
    describe_command,
    find_undo_target,
    plan_rollback,
    plan_undo,
    prune,
    range_changes,
    record_and_run,
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


def _ensure_schema(paths: Paths) -> None:
    """Migrate a pre-branch store so read-only commands see the new schema."""
    try:
        ro = dbm.connect(paths.db, readonly=True)
    except sqlite3.Error:
        return
    try:
        try:
            row = ro.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            needs = row is None or int(row["value"]) < dbm.SCHEMA_VERSION
        except sqlite3.OperationalError:
            needs = True
    finally:
        ro.close()
    if not needs:
        return
    try:
        conn = dbm.connect(paths.db)
    except sqlite3.Error:
        return
    try:
        dbm.init_db(conn)
    finally:
        conn.close()


def _open_db(paths: Paths, *, readonly: bool = True) -> sqlite3.Connection:
    if not paths.db.exists():
        raise click.ClickException(
            f"no chronx store at {paths.home} — run `chronx init` first"
        )
    if readonly:
        _ensure_schema(paths)
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


@daemon.command()
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
def autostart(state: str | None) -> None:
    """Have instrumented shells start the daemon automatically.

    When on, each new hooked shell silently runs `chronx daemon start`
    in the background if no daemon is up."""
    paths = _paths()
    flag = paths.home / "autostart"
    if state is None:
        click.echo(f"autostart is {'on' if flag.exists() else 'off'}")
        return
    if state == "on":
        paths.ensure()
        flag.touch()
        click.secho("autostart on — new shells will bring the daemon up", fg="green")
    else:
        flag.unlink(missing_ok=True)
        click.secho("autostart off", fg="green")


# ------------------------------------------------------------------- watch


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
def watch(path: Path) -> None:
    """Start tracking PATH right now (baseline + watch), without waiting
    for a command to run there."""
    from .ipc import encode_watch, send_line
    from .ops import _wait_for_root_attach

    paths = _paths()
    resolved = path.resolve()
    if not send_line(paths.fifo, encode_watch(str(resolved))):
        raise click.ClickException(
            "the chronx daemon is not running (`chronx daemon start`)"
        )
    _wait_for_root_attach(paths, resolved, timeout=30.0)
    conn = _open_db(paths)
    try:
        root = dbm.root_for_path(conn, resolved)
        if root is None:
            raise click.ClickException(
                f"daemon did not attach {resolved} — it may exceed max_files "
                f"or be refused; check {paths.log}"
            )
        files = conn.execute(
            "SELECT COUNT(*) AS n FROM manifest WHERE root_id = ?", (root["id"],)
        ).fetchone()["n"]
        click.secho(f"watching {root['path']} ({files} file(s) in baseline)", fg="green")
    finally:
        conn.close()


# -------------------------------------------------------------------- diff


def _moment_ts(conn: sqlite3.Connection, spec: str) -> float:
    """Resolve one side of a range: mark, event id, 'now', or a time spec."""
    spec = spec.strip()
    if spec in ("", "now"):
        return time.time()
    row = dbm.get_mark(conn, spec)
    if row is not None:
        return float(row["ts"])
    stripped = spec.lstrip("#")
    if stripped.isdigit() and float(stripped) < 1e9:
        event = dbm.event_by_id(conn, int(stripped))
        if event is None:
            raise click.ClickException(f"no event with id {stripped}")
        return float(event["started_at"])
    ts = _parse_at(spec)
    assert ts is not None
    return ts


@main.command()
@click.argument("time", default="last")
@click.option("--stat", is_flag=True, help="Only list changed files, no content diff.")
def diff(time: str, stat: bool) -> None:
    """Show the filesystem changes at TIME — or between two moments.

    TIME is an event id (from blame/replay), 'last', a mark name, or a
    moment like '10m', '14:32', or '2026-07-13T14:32'.

    A range `A..B` (e.g. `good-state..now`, `12..15`, `1h..10m`) shows the
    NET difference between the two moments; changes that were undone inside
    the window cancel out.
    """
    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        if ".." in time:
            a_spec, _, b_spec = time.partition("..")
            if not a_spec:
                raise click.ClickException(
                    "a range needs a start, e.g. `chronx diff good-state..now`"
                )
            a_ts, b_ts = _moment_ts(conn, a_spec), _moment_ts(conn, b_spec)
            try:
                root, changes = range_changes(conn, Path.cwd(), a_ts, b_ts)
            except OpsError as exc:
                raise click.ClickException(str(exc)) from exc
            click.secho(f"{root}: {fmt_ts(min(a_ts, b_ts))} .. "
                        f"{fmt_ts(max(a_ts, b_ts))}", bold=True)
            if not changes:
                click.echo("  (no net changes between those moments)")
                return
            click.echo()
            for change in changes:
                d = change.as_delta()
                if stat:
                    click.echo("  " + stat_line(d))
                else:
                    for line in render_delta(store, d):
                        _echo_diff_line(line)
                    click.echo()
            return

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
@click.option("--session", "-s", default=None,
              help="Only events from this shell session (see `chronx sessions`).")
def log_cmd(limit: int, all_roots: bool, changes_only: bool, session: str | None) -> None:
    """Print the recent event timeline (newest last), like a quick git log."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        root_id = None
        branch_id = None
        if not all_roots:
            root = dbm.root_for_path(conn, Path.cwd())
            root_id = int(root["id"]) if root is not None else None
            if root_id is not None and session is None:
                branch_id = dbm.active_branch_id(conn, root_id)
        rows = dbm.recent_events(
            conn, root_id=root_id, limit=limit, changes_only=changes_only,
            session=session, branch_id=branch_id,
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


# -------------------------------------------------------------- exec / rerun


@main.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
def exec_cmd(command: tuple[str, ...]) -> None:
    """Run COMMAND with recording, without needing shell hooks.

    For scripts, CI, cron, or uninstrumented shells:
    `chronx exec -- make deploy` records the command and exactly the file
    changes it causes, like any hooked interactive command.
    """
    paths = _paths()
    if not paths.db.exists():
        raise click.ClickException("run `chronx init` first")
    try:
        rc = record_and_run(paths, list(command), cwd=Path.cwd())
    except OpsError as exc:
        raise click.ClickException(str(exc)) from exc
    raise click.exceptions.Exit(rc)


@main.command()
@click.argument("event_id", type=int)
@click.option("--pristine", is_flag=True,
              help="First roll the tree back to just before the event ran.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def rerun(event_id: int, pristine: bool, yes: bool) -> None:
    """Re-execute a recorded command (optionally from its original pre-state).

    `chronx rerun 42 --pristine` = roll back to the moment before event #42,
    then run the same command again in its original directory — reproduce
    exactly what happened.
    """
    paths = _paths()
    conn = _open_db(paths, readonly=not pristine)
    store = ObjectStore(paths.objects)
    try:
        event = dbm.event_by_id(conn, event_id)
        if event is None:
            raise click.ClickException(f"no event with id {event_id}")
        if event["command"] is None:
            raise click.ClickException(
                f"event #{event_id} is an external change; there is no command to rerun"
            )
        cwd = Path(event["cwd"])
        if not cwd.is_dir():
            raise click.ClickException(f"original directory {cwd} no longer exists")

        _echo_event_header(event)
        if pristine:
            ts = float(event["started_at"]) - 1e-6
            try:
                plan = plan_rollback(
                    conn, store, cwd, ts, f"just before event #{event_id}"
                )
            except OpsError as exc:
                raise click.ClickException(str(exc)) from exc
            if plan.steps:
                click.echo()
                click.secho(
                    f"pristine: {len(plan.applicable)} file(s) will be rolled back "
                    f"to just before the event first",
                    fg="cyan",
                )
        click.echo()
        if not yes and not click.confirm(
            f"Re-run this command in {cwd}?", default=False
        ):
            click.echo("aborted")
            return
        if pristine and plan.steps:
            try:
                rollback_event, changed = apply_rollback(conn, store, paths, plan)
                click.secho(
                    f"rolled back {changed} file(s) (event #{rollback_event})",
                    fg="cyan",
                )
            except OpsError as exc:
                raise click.ClickException(str(exc)) from exc
        try:
            rc = record_and_run(paths, str(event["command"]), cwd=cwd)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        color = "green" if rc == 0 else "red"
        click.secho(f"command exited {rc} (recorded; see `chronx log`)", fg=color)
        raise click.exceptions.Exit(rc)
    finally:
        conn.close()


# ---------------------------------------------------------------- timelines


def _require_stopped_clean(paths: Paths, conn: sqlite3.Connection, action: str) -> Path:
    """Shared precondition for switching timelines: daemon down, tree clean."""
    from .snapshot import working_changes

    pid = daemonmod.daemon_pid(paths)
    if pid is not None:
        raise click.ClickException(
            f"daemon is running (pid {pid}) — stop it first: chronx daemon stop\n"
            f"({action} rewrites the working tree, which the daemon would record)"
        )
    root = dbm.root_for_path(conn, Path.cwd())
    if root is None:
        raise click.ClickException(f"{Path.cwd()} is not inside any tracked directory")
    drift = working_changes(
        Path(root["path"]), dbm.load_manifest(conn, int(root["id"])), Config.load(paths)
    )
    if drift:
        raise click.ClickException(
            f"working tree has {len(drift)} unrecorded change(s) (see `chronx status`) "
            f"— record or discard them before {action}"
        )
    return Path(root["path"])


@main.command()
@click.argument("name")
@click.option("--at", "-t", default=None,
              help="Fork at a past moment (mark/time/event) instead of now.")
def fork(name: str, at: str | None) -> None:
    """Fork a new timeline from the current one and switch to it.

    Experiment freely: `chronx fork try-rewrite`, hack away, then
    `chronx switch main` to return — both timelines are kept and isolated.
    Fork from the past with `--at` to explore an alternate history.
    """
    from .ops import fork_branch

    paths = _paths()
    conn = _open_db(paths, readonly=False)
    store = ObjectStore(paths.objects)
    try:
        _require_stopped_clean(paths, conn, "forking")
        try:
            result = fork_branch(
                conn, store, paths, Path.cwd(), name,
                at_ts=_parse_at(at) if at else None,
            )
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"forked timeline {name!r} at {fmt_ts(result.at)}", fg="green")
        if result.files_changed:
            click.echo(f"  reconstructed {result.files_changed} file(s) to the fork point")
        click.secho(f"  now recording on {name!r}; `chronx switch main` to go back", dim=True)
    finally:
        conn.close()


@main.command()
@click.argument("name")
def switch(name: str) -> None:
    """Switch to another timeline, reconstructing the working tree to its tip."""
    from .ops import switch_branch

    paths = _paths()
    conn = _open_db(paths, readonly=False)
    store = ObjectStore(paths.objects)
    try:
        _require_stopped_clean(paths, conn, "switching")
        try:
            result = switch_branch(conn, store, paths, Path.cwd(), name)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"switched to {name!r}", fg="green")
        if result.files_changed:
            click.echo(f"  reconstructed {result.files_changed} file(s)")
    finally:
        conn.close()


@main.command()
@click.argument("other")
@click.option("--allow-conflicts", is_flag=True,
              help="Apply the merge even with conflicts, writing conflict markers.")
@click.option("--dry-run", is_flag=True, help="Show the merge plan and stop.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def merge(other: str, allow_conflicts: bool, dry_run: bool, yes: bool) -> None:
    """Merge timeline OTHER into the current one (three-way merge).

    Brings the changes from another timeline into the active one, auto-merging
    files that changed on only one side (and non-overlapping edits within a
    file), and reporting genuine conflicts. Recorded as one reversible event.
    """
    from .ops import apply_merge, plan_merge

    paths = _paths()
    conn = _open_db(paths, readonly=dry_run)
    store = ObjectStore(paths.objects)
    try:
        active = None
        root = dbm.root_for_path(conn, Path.cwd())
        if root is not None:
            ab = dbm.active_branch(conn, int(root["id"]))
            active = ab["name"] if ab is not None else None
        if not dry_run:
            _require_stopped_clean(paths, conn, "merging")
        try:
            plan = plan_merge(conn, store, Path.cwd(), other)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc

        click.secho(
            f"merging {other!r} into {active or '(active)'} "
            f"(base: {plan.base_desc})", bold=True)
        if not plan.files:
            click.secho("already up to date — nothing to merge", fg="green")
            return
        verb = {"take-theirs": "take ", "add-theirs": "add  ", "merged": "merge",
                "delete": "del  ", "conflict": "CONFL"}
        for f in plan.files:
            style = {"fg": "red"} if f.resolution == "conflict" else (
                {"fg": "cyan"} if f.resolution == "merged" else {})
            click.echo("  " + click.style(f"{verb[f.resolution]} {f.rel}", **style))
        n_conf = len(plan.conflicts)
        n_change = len(plan.changes)
        click.echo()
        click.secho(f"{n_change} file(s) to update, {n_conf} conflict(s)", dim=True)

        if dry_run:
            click.secho("dry run: nothing was changed", dim=True)
            return
        if n_conf and not allow_conflicts:
            raise click.ClickException(
                f"{n_conf} conflict(s) — resolve by hand, or re-run with "
                "--allow-conflicts to write conflict markers into the files")
        if not yes and not click.confirm("Apply the merge?", default=False):
            click.echo("aborted")
            return
        try:
            event_id, changed = apply_merge(
                conn, store, paths, plan, active or "active",
                allow_conflicts=allow_conflicts)
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"merged {other!r}: {changed} file(s) updated "
                    f"(event #{event_id})", fg="green")
        if n_conf and allow_conflicts:
            click.secho(f"  {n_conf} file(s) contain conflict markers — resolve them, "
                        "then run a command to record the resolution", fg="yellow")
        click.secho(f"  undo this merge with `chronx undo --event {event_id}`", dim=True)
    finally:
        conn.close()


@main.command(name="branches")
def branches_cmd() -> None:
    """List the timelines for this directory."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        root = dbm.root_for_path(conn, Path.cwd())
        if root is None:
            raise click.ClickException(f"{Path.cwd()} is not inside any tracked directory")
        active = dbm.active_branch_id(conn, int(root["id"]))
        rows = dbm.list_branches(conn, int(root["id"]))
        if not rows:
            click.echo("no timelines yet")
            return
        for b in rows:
            mark = "*" if int(b["id"]) == active else " "
            tip = fmt_ts(b["tip_ts"]) if b["tip_ts"] else "(no commands yet)"
            parent = ""
            if b["parent_branch_id"]:
                p = dbm.get_branch(conn, int(b["parent_branch_id"]))
                parent = f"  forked from {p['name']!r} @ {fmt_ts(b['base_ts'])}" if p else ""
            colored = click.style(b["name"], fg="green" if mark == "*" else None,
                                  bold=mark == "*")
            click.echo(f" {mark} {colored}  {b['events']} cmd(s), tip {tip}{parent}")
    finally:
        conn.close()


@main.command()
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def branch_delete(name: str, yes: bool) -> None:
    """Delete a timeline and all of its recorded events (not the active one)."""
    paths = _paths()
    conn = _open_db(paths, readonly=False)
    try:
        root = dbm.root_for_path(conn, Path.cwd())
        if root is None:
            raise click.ClickException(f"{Path.cwd()} is not inside any tracked directory")
        branch = dbm.branch_by_name(conn, int(root["id"]), name)
        if branch is None:
            raise click.ClickException(f"no timeline named {name!r}")
        if int(branch["id"]) == dbm.active_branch_id(conn, int(root["id"])):
            raise click.ClickException(
                f"{name!r} is the active timeline — `chronx switch` away first"
            )
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE branch_id = ?", (branch["id"],)
        ).fetchone()["n"]
        if not yes and not click.confirm(
            f"Delete timeline {name!r} and its {n} event(s)?", default=False
        ):
            click.echo("aborted")
            return
        events, _ = dbm.delete_branch(conn, int(branch["id"]))
        click.secho(f"deleted timeline {name!r} ({events} event(s))", fg="green")
    finally:
        conn.close()


# ------------------------------------------------------------------ bisect


@main.command(context_settings={"ignore_unknown_options": True})
@click.option("--good", "good_spec", required=True,
              help="A moment where the test PASSES (mark, event id, time).")
@click.option("--bad", "bad_spec", default="last", show_default=True,
              help="A moment where the test FAILS (mark, event id, time).")
@click.option("--timeout", type=float, default=None,
              help="Abort if a single test run exceeds this many seconds.")
@click.option("--no-verify", is_flag=True,
              help="Skip checking that the endpoints really are good/bad.")
@click.option("--force", is_flag=True,
              help="Proceed even if the working tree has unrecorded changes.")
@click.argument("test", nargs=-1, required=True, type=click.UNPROCESSED)
def bisect(good_spec: str, bad_spec: str, timeout: float | None,
           no_verify: bool, force: bool, test: tuple[str, ...]) -> None:
    """Find the command that broke something, by binary search over history.

    Reconstructs the tree at candidate moments and runs TEST at each (exit 0 =
    good, non-zero = bad) to pinpoint the first event that made it fail:

        chronx bisect --good shipped -- pytest -x tests/

    The daemon must be stopped and the working tree clean; the tree is
    reconstructed during the search and restored to its starting state after.
    """
    from .snapshot import working_changes

    paths = _paths()
    pid = daemonmod.daemon_pid(paths)
    if pid is not None:
        raise click.ClickException(
            f"daemon is running (pid {pid}) — stop it first: chronx daemon stop\n"
            "(bisect reconstructs the working tree, which the daemon would record)"
        )
    conn = _open_db(paths, readonly=False)
    store = ObjectStore(paths.objects)
    cfg = Config.load(paths)
    try:
        root = dbm.root_for_path(conn, Path.cwd())
        if root is None:
            raise click.ClickException(
                f"{Path.cwd()} is not inside any tracked directory"
            )
        drift = working_changes(Path(root["path"]), dbm.load_manifest(conn, int(root["id"])), cfg)
        if drift and not force:
            raise click.ClickException(
                f"working tree has {len(drift)} unrecorded change(s) "
                "(see `chronx status`) — bisect would overwrite them. Record them "
                "first, or pass --force to discard them."
            )
        try:
            good_ts = _moment_ts(conn, good_spec)
            bad_ts = (
                float(resolve_event(conn, "last", Path.cwd())["started_at"])
                if bad_spec == "last"
                else _moment_ts(conn, bad_spec)
            )
        except (OpsError, click.ClickException) as exc:
            raise click.ClickException(str(exc)) from exc

        click.secho(
            f"bisecting between {fmt_ts(good_ts)} (good) and {fmt_ts(bad_ts)} (bad)",
            bold=True,
        )

        def _on_test(event: sqlite3.Row, good: bool) -> None:
            verdict = click.style("GOOD", fg="green") if good else click.style("BAD", fg="red")
            click.echo(f"  {verdict}  #{event['id']:<5} {describe_command(event)}")

        try:
            result = bisect_history(
                conn, store, paths, Path.cwd(),
                good_ts=good_ts, bad_ts=bad_ts, test=list(test),
                verify=not no_verify, timeout=timeout, on_test=_on_test,
            )
        except OpsError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo()
        click.secho(
            f"ran {result.tests_run} test(s) over {result.candidates} candidate event(s)",
            dim=True,
        )
        if result.culprit is None:
            click.secho(
                "no candidate event failed the test — the regression may predate "
                "--good, or come from outside the tracked tree",
                fg="yellow",
            )
            return
        click.echo()
        click.secho("first bad event (the regression):", bold=True)
        _echo_event_header(result.culprit)
        if result.last_good_id is not None:
            click.secho(f"\nlast good event was #{result.last_good_id}", dim=True)
        click.secho(
            f"inspect it with `chronx diff {result.culprit['id']}`, "
            f"undo it with `chronx undo --event {result.culprit['id']}`",
            dim=True,
        )
        click.secho("(working tree restored to its starting state)", dim=True)
    finally:
        conn.close()


# ------------------------------------------------------------------ doctor


@main.command()
def doctor() -> None:
    """Diagnose the recording pipeline: store, daemon, pipe, hooks, disk."""
    from .doctor import FAIL, OK, run_checks

    checks = run_checks(_paths())
    hard_fail = False
    for c in checks:
        if c.status == OK:
            icon = click.style("✓", fg="green")
        elif c.status == FAIL:
            icon, hard_fail = click.style("✗", fg="red"), True
        else:
            icon = click.style("!", fg="yellow")
        click.echo(f" {icon} {c.label:<12} {c.detail}")
    if hard_fail:
        raise click.exceptions.Exit(1)


# -------------------------------------------------------------------- tail


@main.command()
@click.option("--backlog", "-n", default=5, show_default=True,
              help="Recent events to print before following.")
@click.option("--stat", is_flag=True, help="List changed files under each event.")
@click.option("--changes-only", "-c", is_flag=True,
              help="Hide commands that changed no files.")
@click.option("--all-roots", is_flag=True,
              help="Follow every tracked directory, not just the current one.")
def tail(backlog: int, stat: bool, changes_only: bool, all_roots: bool) -> None:
    """Follow the event stream live, like `tail -f` for your workflow."""
    paths = _paths()
    conn = _open_db(paths)

    def emit(r: sqlite3.Row) -> None:
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
        click.echo(
            f"#{r['id']:<5} {fmt_ts(r['started_at'])}  {delta_s} {exit_s}  "
            + click.style(describe_command(r), fg="yellow" if r["command"] else None,
                          dim=r["command"] is None)
        )
        if stat and total:
            for d in dbm.deltas_for(conn, int(r["id"])):
                click.echo("       " + stat_line(d))

    try:
        root_id = None
        if not all_roots:
            root = dbm.root_for_path(conn, Path.cwd())
            root_id = int(root["id"]) if root is not None else None
        rows = dbm.recent_events(
            conn, root_id=root_id, limit=backlog, changes_only=changes_only
        )
        last_id = dbm.max_event_id(conn)
        for r in rows:
            emit(r)
        click.secho("--- following (Ctrl-C to stop) ---", dim=True, err=True)
        while True:
            time.sleep(0.5)
            fresh = dbm.events_after(
                conn, last_id, root_id=root_id, changes_only=changes_only
            )
            newest = dbm.max_event_id(conn)
            if newest > last_id:
                last_id = newest
            for r in fresh:
                emit(r)
    except KeyboardInterrupt:
        click.echo()
    finally:
        conn.close()


# ---------------------------------------------------------------- sessions


@main.command()
@click.option("--limit", "-n", default=20, show_default=True)
def sessions(limit: int) -> None:
    """List recorded shell sessions, most recently active first."""
    paths = _paths()
    conn = _open_db(paths)
    try:
        rows = dbm.list_sessions(conn, limit=limit)
        if not rows:
            click.echo("no sessions recorded")
            return
        current = os.environ.get("CHRONX_SESSION")
        for r in rows:
            sid = r["session"] or "(external)"
            mark = "*" if current and r["session"] == current else " "
            click.echo(
                f" {mark} {sid:<24} {r['events']:>4} cmd(s), ±{r['changes'] or 0:<4} "
                f"{fmt_ts(r['first_ts'])} → {fmt_ts(r['last_ts'])}"
            )
            click.secho(f"      {r['cwd']}", dim=True)
        if current:
            click.secho(f"\n* = this shell ({current}); "
                        f"filter with `chronx log -s <session>`", dim=True)
    finally:
        conn.close()


# ------------------------------------------------------------------ report


@main.command()
@click.option("--since", default="1h", show_default=True,
              help="Start of the window ('30m', '14:32', a mark, ISO...).")
@click.option("--until", default=None, help="End of the window (default: now).")
@click.option("--session", "-s", default=None, help="Only this shell session.")
@click.option("--full", is_flag=True, help="Include unified diffs, not just file lists.")
@click.option("--all-roots", is_flag=True)
def report(since: str, until: str | None, session: str | None,
           full: bool, all_roots: bool) -> None:
    """Write a shareable Markdown report of what you did (and what it changed).

    Pipe it wherever: `chronx report --since 2h --full > debug-session.md`.
    """
    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)

    def moment(spec: str) -> float:
        row = dbm.get_mark(conn, spec)
        if row is not None:
            return float(row["ts"])
        ts = _parse_at(spec)
        assert ts is not None
        return ts

    try:
        since_ts = moment(since)
        until_ts = moment(until) if until else None
        root_id = None
        root_label = "all tracked directories"
        if not all_roots:
            root = dbm.root_for_path(conn, Path.cwd())
            if root is not None:
                root_id, root_label = int(root["id"]), str(root["path"])
        rows = dbm.events_between(
            conn, since=since_ts, until=until_ts, root_id=root_id, session=session
        )
        window = f"{fmt_ts(since_ts)} → {fmt_ts(until_ts) if until_ts else 'now'}"
        click.echo(f"# chronx report\n\n- scope: `{root_label}`\n- window: {window}")
        if session:
            click.echo(f"- session: `{session}`")
        changing = [r for r in rows if sum(dbm.delta_counts(conn, r['id']).values())]
        click.echo(f"- {len(rows)} command(s), {len(changing)} changed files\n")
        for r in rows:
            deltas = dbm.deltas_for(conn, int(r["id"]))
            exit_s = "" if r["exit_code"] is None else f" · exit {r['exit_code']}"
            click.echo(
                f"## #{r['id']} · {fmt_ts(r['started_at'])}{exit_s}\n\n"
                f"```console\n$ {describe_command(r)}\n```\n"
            )
            if not deltas:
                click.echo("_no filesystem changes_\n")
                continue
            for d in deltas:
                click.echo(f"- `{stat_line(d)}`")
            click.echo()
            if full:
                for d in deltas:
                    click.echo("```diff")
                    for line in render_delta(store, d, max_lines=200):
                        click.echo(line)
                    click.echo("```\n")
    finally:
        conn.close()


# ------------------------------------------------------------------ status


@main.command()
@click.option("--stat", is_flag=True, help="Only list changed files, no content diff.")
@click.option("--all-roots", is_flag=True,
              help="Check every tracked directory, not just the current one.")
def status(stat: bool, all_roots: bool) -> None:
    """Show working-tree changes not yet recorded (drift vs the last snapshot).

    Catches edits made while the daemon was stopped, or an in-flight command's
    changes. Read-only: nothing is hashed to the store or recorded.
    """
    from .snapshot import working_changes
    from .diffview import render_working_change, working_stat_line

    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    cfg = Config.load(paths)
    try:
        if all_roots:
            roots_list = dbm.get_roots(conn)
        else:
            root = dbm.root_for_path(conn, Path.cwd())
            if root is None:
                raise click.ClickException(
                    f"{Path.cwd()} is not inside any tracked directory"
                )
            roots_list = [root]

        any_drift = False
        for root in roots_list:
            manifest = dbm.load_manifest(conn, int(root["id"]))
            changes = working_changes(Path(root["path"]), manifest, cfg)
            if not changes:
                continue
            any_drift = True
            click.secho(f"{root['path']}", bold=True)
            for ch in changes:
                if stat:
                    click.echo("  " + working_stat_line(ch))
                else:
                    for line in render_working_change(store, Path(root["path"]), ch):
                        _echo_diff_line(line)
                    click.echo()
            if stat:
                click.echo()
        if not any_drift:
            click.secho("working tree matches the last recorded state", fg="green")
        else:
            click.secho(
                "these changes are not yet recorded — run a command (or "
                "`chronx exec`) to capture them, or start the daemon",
                dim=True,
            )
    finally:
        conn.close()


# ------------------------------------------------------------------ to-git


@main.command("to-git")
@click.argument("target", type=click.Path(path_type=Path))
@click.option("--root", "root_path", type=click.Path(path_type=Path), default=None,
              help="Which tracked root to export (default: the one containing cwd).")
@click.option("--branch", default="main", show_default=True,
              help="Branch name for the generated history.")
@click.option("--no-checkout", is_flag=True,
              help="Leave the new repo bare of a working tree (history only).")
def to_git_cmd(target: Path, root_path: Path | None, branch: str,
               no_checkout: bool) -> None:
    """Replay a directory's recorded history into a new git repo at TARGET.

    Turns an ad-hoc shell session into real, reviewable git history — one
    commit per command (author date = when it ran, message = the command) —
    that you can `git log`, `git blame`, `git bisect`, or push to a remote.
    """
    from .gitexport import GitExportError, to_git

    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        anchor = (root_path or Path.cwd()).resolve()
        row = dbm.root_for_path(conn, anchor)
        if row is None:
            raise click.ClickException(f"{anchor} is not inside any tracked directory")
        try:
            stats = to_git(
                conn, store, row, target, branch=branch, checkout=not no_checkout
            )
        except GitExportError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"wrote git repo at {stats.target}", fg="green")
        click.echo(
            f"  {stats.commits} commit(s) on '{stats.branch}' "
            f"({stats.events} command(s) + baseline), {stats.blobs} blob(s)"
        )
        click.secho(
            f"  explore: git -C {stats.target} log --stat", dim=True
        )
    finally:
        conn.close()


# ---------------------------------------------------------- export / import


@main.command("export")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None,
              help="Archive path (default: <root-name>-<n>ev.chronx in cwd).")
@click.option("--root", "root_path", type=click.Path(path_type=Path), default=None,
              help="Which tracked root to export (default: the one containing cwd).")
def export_cmd(output: Path | None, root_path: Path | None) -> None:
    """Bundle a directory's recorded history into a portable archive.

    Move a debugging session to another machine:
    `chronx export -o bug.chronx` there, `chronx import bug.chronx --as .` here.
    """
    from .transfer import TransferError, export_root

    paths = _paths()
    conn = _open_db(paths)
    store = ObjectStore(paths.objects)
    try:
        target = (root_path or Path.cwd()).resolve()
        row = dbm.root_for_path(conn, target)
        if row is None:
            raise click.ClickException(f"{target} is not inside any tracked directory")
        if output is None:
            name = Path(row["path"]).name or "root"
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE root_id = ?", (row["id"],)
            ).fetchone()["n"]
            output = Path.cwd() / f"{name}-{n}ev.chronx"
        try:
            stats = export_root(conn, store, row, output)
        except TransferError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"exported {stats.root}", fg="green")
        click.echo(
            f"  {stats.events} event(s), {stats.deltas} delta(s), {stats.marks} mark(s), "
            f"{stats.blobs} blob(s)"
        )
        click.echo(f"  -> {output}  ({_human_bytes(stats.bytes_written)})")
    finally:
        conn.close()


@main.command("import")
@click.argument("archive", type=click.Path(exists=True, path_type=Path))
@click.option("--as", "as_path", type=click.Path(path_type=Path), default=None,
              help="Map the history onto this local directory (default: original path).")
def import_cmd(archive: Path, as_path: Path | None) -> None:
    """Import history from a `chronx export` archive into the local store.

    The daemon must be stopped. Use `--as .` to attach the imported history
    to the current directory, then `chronx log` / `rollback` against it.
    """
    from .transfer import TransferError, import_archive

    paths = _paths()
    if not paths.db.exists():
        raise click.ClickException("run `chronx init` first")
    pid = daemonmod.daemon_pid(paths)
    if pid is not None:
        raise click.ClickException(
            f"daemon is running (pid {pid}) — stop it first: chronx daemon stop"
        )
    try:
        stats = import_archive(paths, archive, as_path=as_path)
    except TransferError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"imported into {stats.root}", fg="green")
    click.echo(
        f"  {stats.events} event(s), {stats.deltas} delta(s), "
        f"{stats.marks} mark(s), blobs +{stats.blobs_added} "
        f"({stats.blobs_skipped} already present)"
    )
    if stats.remapped:
        click.secho("  paths remapped to the target directory", dim=True)
    if stats.marks_renamed:
        click.secho(f"  {stats.marks_renamed} mark(s) renamed to avoid collisions",
                    dim=True)
    click.secho("  explore with `chronx log`, `chronx diff`, `chronx rollback`", dim=True)


# ------------------------------------------------------------------- roots


@main.group(invoke_without_command=True)
@click.pass_context
def roots(ctx: click.Context) -> None:
    """List or forget tracked directories."""
    if ctx.invoked_subcommand is not None:
        return
    paths = _paths()
    conn = _open_db(paths)
    try:
        rows = dbm.root_summaries(conn)
        if not rows:
            click.echo("no tracked directories yet")
            return
        for r in rows:
            click.echo(
                f"  {r['path']}\n"
                f"    {r['events']} event(s), {r['files']} tracked file(s), "
                f"since {fmt_ts(r['added_at'])}"
            )
    finally:
        conn.close()


@roots.command()
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def forget(path: Path, yes: bool) -> None:
    """Stop tracking PATH and erase its recorded history.

    Blobs shared with other roots survive; run `chronx gc` afterwards to
    reclaim the rest. The daemon must be stopped."""
    paths = _paths()
    pid = daemonmod.daemon_pid(paths)
    if pid is not None:
        raise click.ClickException(
            f"daemon is running (pid {pid}) — stop it first: chronx daemon stop"
        )
    conn = _open_db(paths, readonly=False)
    try:
        resolved = str(path.resolve())
        row = conn.execute("SELECT * FROM roots WHERE path = ?", (resolved,)).fetchone()
        if row is None:
            raise click.ClickException(f"{resolved} is not a tracked root "
                                       "(see `chronx roots`)")
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE root_id = ?", (row["id"],)
        ).fetchone()["n"]
        click.echo(f"forgetting {resolved}: erases {events} event(s) and its manifest")
        if not yes and not click.confirm("Proceed?", default=False):
            click.echo("aborted")
            return
        deleted_events, deleted_deltas = dbm.forget_root(conn, int(row["id"]))
        click.secho(
            f"forgot {resolved} ({deleted_events} events, {deleted_deltas} deltas); "
            "run `chronx gc` to reclaim blob space",
            fg="green",
        )
    finally:
        conn.close()


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


# ------------------------------------------------------------------- serve


@main.command()
@click.option("--port", "-p", default=7373, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address. Leave as localhost unless you mean to expose it.")
@click.option("--open", "open_browser", is_flag=True,
              help="Open the UI in your browser once it's up.")
def serve(port: int, host: str, open_browser: bool) -> None:
    """Serve a live web UI over your recorded history (read-only)."""
    from .webui import make_server

    paths = _paths()
    conn = _open_db(paths)
    try:
        root = dbm.root_for_path(conn, Path.cwd())
        default_root = int(root["id"]) if root is not None else None
    finally:
        conn.close()

    try:
        server = make_server(paths, host, port, default_root)
    except OSError as exc:
        raise click.ClickException(
            f"cannot bind {host}:{port} ({exc}); try another --port"
        ) from exc

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    click.secho(f"chronx web UI on {url}", fg="green")
    if host not in ("127.0.0.1", "localhost"):
        click.secho(
            "  ! bound to a non-local address — anyone who can reach it can read "
            "your recorded files", fg="yellow")
    click.secho("  read-only; Ctrl-C to stop", dim=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nstopped")
    finally:
        server.server_close()


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
