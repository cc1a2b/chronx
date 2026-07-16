"""Stable helper surface for chronx feature plugins (``chronx/plugins/*.py``).

Each plugin module defines ``register(main)`` and adds one or more click
commands to the ``main`` group. Plugins import ONLY from here (plus stdlib and
the read-only chronx modules re-exported below) so they never depend on
``cli.py`` — which keeps them independent and conflict-free.

Everything here is import-safe and does not touch ``cli.py``.
"""

from __future__ import annotations

import re as _re
import sqlite3
from pathlib import Path

import click

from . import daemon as _daemonmod
from . import db as dbm
from .config import Config, Paths
from .diffview import render_delta, stat_line
from .ipc import encode_sync as _encode_sync
from .ipc import send_line as _send_line
from .config import load_root_ignore
from .ops import OpsError, branch_state_at, describe_command, state_at
from .snapshot import is_ignored_rel, working_changes
from .store import ObjectStore, hash_bytes
from .when import WhenParseError, fmt_ts, parse_when

__all__ = [
    "click", "sqlite3", "Path", "dbm", "Config", "Paths", "ObjectStore",
    "hash_bytes", "render_delta", "stat_line", "describe_command", "OpsError",
    "branch_state_at", "state_at", "fmt_ts", "working_changes",
    "is_ignored_rel", "load_root_ignore",
    "paths", "open_db", "human_bytes", "parse_at", "moment_ts",
    "require_daemon_stopped", "root_for_cwd", "active_branch_id",
    "current_file_state", "write_atomic", "send_sync",
]


def paths() -> Paths:
    return Paths.from_env()


def _ensure_schema(p: Paths) -> None:
    try:
        ro = dbm.connect(p.db, readonly=True)
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
        conn = dbm.connect(p.db)
    except sqlite3.Error:
        return
    try:
        dbm.init_db(conn)
    finally:
        conn.close()


def open_db(*, readonly: bool = True) -> sqlite3.Connection:
    """Open the chronx store. Read-only by default. Raises ClickException if
    the store is missing."""
    p = paths()
    if not p.db.exists():
        raise click.ClickException(
            f"no chronx store at {p.home} — run `chronx init` first"
        )
    if readonly:
        _ensure_schema(p)
    conn = dbm.connect(p.db, readonly=readonly)
    if not readonly:
        dbm.init_db(conn)
    return conn


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def parse_at(spec: str | None) -> float | None:
    if spec is None:
        return None
    try:
        return parse_when(spec)
    except WhenParseError as exc:
        raise click.ClickException(str(exc)) from exc


def moment_ts(conn: sqlite3.Connection, spec: str) -> float:
    """Resolve a mark name, event id (#n or n), 'now', or a time spec to epoch."""
    import time as _time

    spec = spec.strip()
    if spec in ("", "now"):
        return _time.time()
    row = dbm.get_mark(conn, spec)
    if row is not None:
        return float(row["ts"])
    stripped = spec.lstrip("#")
    if stripped.isdigit() and float(stripped) < 1e9:
        event = dbm.event_by_id(conn, int(stripped))
        if event is None:
            raise click.ClickException(f"no event with id {stripped}")
        return float(event["started_at"])
    ts = parse_at(spec)
    assert ts is not None
    return ts


def root_for_cwd(conn: sqlite3.Connection) -> sqlite3.Row:
    """The tracked root containing the cwd, or a ClickException."""
    root = dbm.root_for_path(conn, Path.cwd())
    if root is None:
        raise click.ClickException(
            f"{Path.cwd()} is not inside any tracked directory"
        )
    return root


def active_branch_id(conn: sqlite3.Connection, root_id: int) -> int | None:
    return dbm.active_branch_id(conn, root_id)


def require_daemon_stopped(action: str = "this") -> None:
    pid = _daemonmod.daemon_pid(paths())
    if pid is not None:
        raise click.ClickException(
            f"daemon is running (pid {pid}) — stop it first: chronx daemon stop\n"
            f"({action} would race with the recorder)"
        )


def current_file_state(path):
    """(bytes|None, os.stat_result|None) for a path.

    bytes is None if the path is absent, a non-regular file, or unreadable.
    """
    import os
    import stat as _stat

    try:
        st = os.lstat(path)
    except OSError:
        return None, None
    if not _stat.S_ISREG(st.st_mode):
        return None, st
    try:
        return Path(path).read_bytes(), st
    except OSError:
        return None, st


def write_atomic(path, data: bytes, mode: int | None = None) -> None:
    """Atomically write bytes to `path` (temp file + os.replace), making parents."""
    import os
    import stat as _stat
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".chronx-plugin-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, _stat.S_IMODE(mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def send_sync(root_path) -> None:
    """Tell a running daemon to resync its manifest for `root_path` after a write."""
    _send_line(paths().fifo, _encode_sync(str(root_path)))


_VALID_NAME = _re.compile(r"^[A-Za-z][\w.-]*$")


def valid_name(name: str) -> bool:
    return bool(_VALID_NAME.match(name)) and name not in ("last", "now")
