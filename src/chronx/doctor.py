"""`chronx doctor` — diagnose the whole recording pipeline end to end."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db as dbm
from .config import Config, Paths
from .store import HASH_ALGO, ObjectStore

OK, WARN, FAIL = "ok", "warn", "fail"

_RC_FILES = {
    "bash": "~/.bashrc",
    "zsh": "~/.zshrc",
    "fish": "~/.config/fish/config.fish",
}


@dataclass(frozen=True)
class Check:
    status: str  # ok | warn | fail
    label: str
    detail: str


def _fifo_has_reader(fifo: Path) -> str:
    """'ok' someone is reading, 'none' no reader, 'missing' no fifo."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            return "none"
        return "missing"
    os.close(fd)
    return "ok"


def run_checks(paths: Paths) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    # --- store ---------------------------------------------------------
    if not paths.home.is_dir():
        add(Check(FAIL, "store", f"{paths.home} does not exist — run `chronx init`"))
        return checks
    add(Check(OK, "store", str(paths.home)))

    if HASH_ALGO == "blake3":
        add(Check(OK, "hashing", "blake3"))
    else:
        add(Check(WARN, "hashing",
                  f"blake3 unavailable, using {HASH_ALGO} (pip install blake3)"))

    try:
        Config.load(paths)
        add(Check(OK, "config", str(paths.config)))
    except Exception as exc:  # defensive: load() shouldn't raise, but doctor reports
        add(Check(WARN, "config", f"unreadable ({exc}); defaults in effect"))

    try:
        digest = ObjectStore(paths.objects).put_bytes(b"chronx-doctor-probe")
        add(Check(OK, "objects", f"writable ({digest[:12]}...)"))
    except OSError as exc:
        add(Check(FAIL, "objects", f"cannot write blobs: {exc}"))

    if paths.db.exists():
        try:
            conn = dbm.connect(paths.db, readonly=True)
            try:
                verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
                if verdict == "ok":
                    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                    roots = conn.execute("SELECT COUNT(*) FROM roots").fetchone()[0]
                    add(Check(OK, "database", f"healthy ({n} events, {roots} roots)"))
                else:
                    add(Check(FAIL, "database", f"quick_check: {verdict}"))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            add(Check(FAIL, "database", f"cannot open: {exc}"))
    else:
        add(Check(FAIL, "database", "missing — run `chronx init`"))

    # --- daemon --------------------------------------------------------
    from . import daemon as daemonmod

    pid = daemonmod.daemon_pid(paths)
    if pid is None:
        add(Check(WARN, "daemon", "not running — start with `chronx daemon start`"))
    else:
        add(Check(OK, "daemon", f"running (pid {pid})"))
    reader = _fifo_has_reader(paths.fifo)
    if reader == "ok":
        add(Check(OK, "signal pipe", "daemon is listening on the fifo"))
    elif pid is None:
        add(Check(OK if reader == "missing" else WARN, "signal pipe",
                  "absent (expected while the daemon is stopped)"))
    else:
        add(Check(FAIL, "signal pipe",
                  f"daemon alive but fifo {reader} — restart the daemon"))

    try:
        tail = paths.log.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]
        errors = [ln for ln in tail if " ERROR " in ln or "Traceback" in ln]
        if errors:
            add(Check(WARN, "daemon log", f"{len(errors)} recent error line(s): "
                      f"{errors[-1][:100]}"))
        else:
            add(Check(OK, "daemon log", "no recent errors"))
    except OSError:
        add(Check(OK, "daemon log", "no log yet"))

    # --- shell hooks ---------------------------------------------------
    installed: list[str] = []
    for shell, rc in _RC_FILES.items():
        try:
            if "chronx hook" in Path(rc).expanduser().read_text(encoding="utf-8"):
                installed.append(shell)
        except OSError:
            continue
    if installed:
        add(Check(OK, "shell hooks", "installed in: " + ", ".join(installed)))
    else:
        add(Check(WARN, "shell hooks",
                  "not found in any rc file — run `chronx init` for instructions"))
    if os.environ.get("CHRONX_SESSION"):
        add(Check(OK, "this shell", "instrumented (CHRONX_SESSION is set)"))
    else:
        add(Check(WARN, "this shell",
                  "not instrumented — source the hook or open a new shell"))

    # --- environment ---------------------------------------------------
    try:
        from importlib.metadata import version as _pkg_version

        from watchdog.observers import Observer

        try:
            wd_version = _pkg_version("watchdog")
        except Exception:
            wd_version = "unknown version"
        add(Check(OK, "watchdog", f"{wd_version} ({Observer.__name__})"))
    except ImportError as exc:
        add(Check(FAIL, "watchdog", f"not importable: {exc}"))

    usage = shutil.disk_usage(paths.home)
    free_pct = usage.free / usage.total * 100
    status = OK if usage.free > 1 << 30 else WARN
    add(Check(status, "disk",
              f"{usage.free / (1 << 30):.1f} GiB free ({free_pct:.0f}%)"))
    return checks
