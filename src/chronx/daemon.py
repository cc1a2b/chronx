"""The chronx background daemon.

Listens on a FIFO for PRE/POST signals from instrumented shells, watches
the working directories those shells use (via watchdog), and records each
command together with exactly the file deltas it caused.

Attribution model
-----------------
- A recursive watch per working-directory "root" marks paths dirty as
  filesystem events arrive.
- PRE opens a command window. Dirt that predates the window (editor saves,
  cron jobs, ...) is swept into an "(external)" event first so it is never
  blamed on the command.
- POST closes the window; after a short settle delay the dirty paths inside
  the window are hashed against the manifest and become the command's deltas.
"""

from __future__ import annotations

import heapq
import logging
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import db as dbm
from .config import Config, Paths
from .ipc import PostMsg, PreMsg, SyncMsg, parse_line
from .snapshot import compute_deltas, is_ignored_rel, scan_root
from .store import ObjectStore

log = logging.getLogger("chronx.daemon")

_IGNORED_FS_EVENTS = frozenset({"opened", "closed_no_write"})


@dataclass
class Pending:
    """A command that has started (PRE seen) but not finished (no POST yet)."""

    session: str
    command: str
    cwd: str
    root_key: str
    root_id: int
    shell_ts: float
    started_mono: float


@dataclass
class RootState:
    root_id: int
    path: Path
    manifest: dict[str, dbm.ManifestEntry]


class _DirtyHandler(FileSystemEventHandler):
    """Watchdog callback: just timestamps dirty paths, nothing heavy."""

    def __init__(self, daemon: "Daemon") -> None:
        self._daemon = daemon

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _IGNORED_FS_EVENTS:
            return
        self._daemon.mark_dirty(os.fspath(event.src_path))
        dest = getattr(event, "dest_path", "")
        if dest:
            self._daemon.mark_dirty(os.fspath(dest))


class Daemon:
    def __init__(self, paths: Paths, cfg: Config) -> None:
        self.paths = paths
        self.cfg = cfg
        self.store = ObjectStore(paths.objects)
        self.conn = dbm.connect(paths.db)
        dbm.init_db(self.conn)

        self.dirty: dict[str, float] = {}  # abs path -> monotonic stamp
        self.dirty_lock = threading.Lock()
        self.pending: dict[str, Pending] = {}  # session -> in-flight command
        self.roots: dict[str, RootState] = {}  # str(resolved path) -> state
        self.refused_roots: set[str] = set()
        self._finalize_q: list[tuple[float, int, Pending, PostMsg]] = []
        self._seq = 0
        self._stop = threading.Event()
        self._handler = _DirtyHandler(self)
        self.observer = Observer()

    # ------------------------------------------------------------------ dirty

    def mark_dirty(self, path: str) -> None:
        home = str(self.paths.home)
        if path == home or path.startswith(home + os.sep):
            return
        parts = path.split(os.sep)
        if any(p in self.cfg.ignore_dirs for p in parts):
            return
        with self.dirty_lock:
            self.dirty[path] = time.monotonic()

    def _claim_dirty(self, root: Path, since: float | None) -> set[str]:
        """Pop dirty paths under `root` stamped at/after `since` (None = all).

        Returns candidate paths relative to the root.
        """
        prefix = str(root) + os.sep
        root_s = str(root)
        claimed: list[str] = []
        with self.dirty_lock:
            for path in list(self.dirty):
                if path != root_s and not path.startswith(prefix):
                    continue
                if since is not None and self.dirty[path] < since:
                    continue
                del self.dirty[path]
                claimed.append(path)
        rels: set[str] = set()
        for path in claimed:
            if path == root_s:
                continue
            rel = os.path.relpath(path, root_s).replace(os.sep, "/")
            if not is_ignored_rel(rel, self.cfg):
                rels.add(rel)
        return rels

    # ------------------------------------------------------------------ roots

    def _attach_root(self, cwd: str) -> RootState | None:
        try:
            p = Path(cwd).resolve(strict=True)
        except OSError:
            return None
        if not p.is_dir():
            return None
        key = str(p)
        for rs in self.roots.values():
            if p == rs.path or rs.path in p.parents:
                return rs
        if key in self.refused_roots:
            return None
        if not self.cfg.allow_home_root and p in (Path.home(), Path("/")):
            log.warning("refusing to track %s (set allow_home_root to override)", p)
            self.refused_roots.add(key)
            return None

        root_id, created = dbm.ensure_root(self.conn, key)
        if created:
            log.info("baseline scan of new root %s", key)
            manifest = scan_root(p, self.store, self.cfg)
            if manifest is None:
                log.warning(
                    "refusing to track %s: more than %d files", key, self.cfg.max_files
                )
                self.refused_roots.add(key)
                return None
            with self.conn:
                dbm.apply_manifest(self.conn, root_id, manifest, set())
            log.info("baseline complete: %d files", len(manifest))
        else:
            manifest = dbm.load_manifest(self.conn, root_id)
            rs_tmp = RootState(root_id=root_id, path=p, manifest=manifest)
            self._record_external(rs_tmp, candidates=None)  # offline catch-up
            manifest = rs_tmp.manifest

        rs = RootState(root_id=root_id, path=p, manifest=manifest)
        self.roots[key] = rs
        self.observer.schedule(self._handler, key, recursive=True)
        log.info("watching %s (root %d, %d files)", key, root_id, len(rs.manifest))
        return rs

    # ------------------------------------------------------------------ events

    def _record_external(self, rs: RootState, candidates: set[str] | None) -> None:
        """Fold ambient (non-command) changes into an '(external)' event."""
        deltas, updates, deletes = compute_deltas(
            rs.path, candidates, rs.manifest, self.store, self.cfg
        )
        if not deltas and not updates and not deletes:
            return
        rs.manifest.update(updates)
        for rel in deletes:
            rs.manifest.pop(rel, None)
        if deltas:
            now = time.time()
            event_id = dbm.record_event(
                self.conn,
                session=None,
                root_id=rs.root_id,
                cwd=str(rs.path),
                command=dbm.EXTERNAL_COMMAND,
                started_at=now,
                finished_at=now,
                exit_code=None,
                deltas=deltas,
                manifest_updates=updates,
                manifest_deletes=deletes,
            )
            log.info(
                "external change on %s: %d delta(s) (event %d)",
                rs.path,
                len(deltas),
                event_id,
            )
        else:
            with self.conn:
                dbm.apply_manifest(self.conn, rs.root_id, updates, deletes)

    def _handle_pre(self, msg: PreMsg) -> None:
        rs = self._attach_root(msg.cwd)
        if rs is None:
            return
        stale = self.pending.pop(msg.session, None)
        if stale is not None:
            # Lost the POST for the previous command (shell killed, write
            # dropped); finalize it now so its dirt isn't misattributed.
            self._finalize(stale, PostMsg(msg.session, time.time(), None))
        if not any(p.root_id == rs.root_id for p in self.pending.values()):
            self._record_external(rs, candidates=self._claim_dirty(rs.path, None))
        self.pending[msg.session] = Pending(
            session=msg.session,
            command=msg.command,
            cwd=msg.cwd,
            root_key=str(rs.path),
            root_id=rs.root_id,
            shell_ts=msg.ts,
            started_mono=time.monotonic(),
        )

    def _handle_post(self, msg: PostMsg) -> None:
        pending = self.pending.pop(msg.session, None)
        if pending is None:
            return  # daemon started mid-command, or duplicate POST
        due = time.monotonic() + self.cfg.settle_seconds
        self._seq += 1
        heapq.heappush(self._finalize_q, (due, self._seq, pending, msg))

    def _handle_sync(self, msg: SyncMsg) -> None:
        try:
            key = str(Path(msg.root).resolve())
        except OSError:
            return
        rs = self.roots.get(key)
        if rs is None:
            return
        self._claim_dirty(rs.path, None)  # discard: db already reflects reality
        rs.manifest = dbm.load_manifest(self.conn, rs.root_id)
        log.info("manifest resynced for %s", key)

    def _finalize(self, pending: Pending, post: PostMsg) -> None:
        rs = self.roots.get(pending.root_key)
        if rs is None:
            return
        candidates = self._claim_dirty(rs.path, pending.started_mono - 0.05)
        deltas, updates, deletes = compute_deltas(
            rs.path, candidates, rs.manifest, self.store, self.cfg
        )
        rs.manifest.update(updates)
        for rel in deletes:
            rs.manifest.pop(rel, None)
        event_id = dbm.record_event(
            self.conn,
            session=pending.session,
            root_id=rs.root_id,
            cwd=pending.cwd,
            command=pending.command,
            started_at=pending.shell_ts,
            finished_at=post.ts,
            exit_code=post.exit_code,
            deltas=deltas,
            manifest_updates=updates,
            manifest_deletes=deletes,
        )
        log.debug(
            "event %d: %r (%d delta(s))", event_id, pending.command[:80], len(deltas)
        )

    def _process_due(self) -> None:
        now = time.monotonic()
        while self._finalize_q and self._finalize_q[0][0] <= now:
            _, _, pending, post = heapq.heappop(self._finalize_q)
            try:
                self._finalize(pending, post)
            except Exception:
                log.exception("failed to finalize command %r", pending.command[:80])

    # ------------------------------------------------------------------ loop

    def _dispatch(self, line: str) -> None:
        msg = parse_line(line)
        if msg is None:
            log.warning("unparseable message: %r", line[:200])
            return
        try:
            if isinstance(msg, PreMsg):
                self._handle_pre(msg)
            elif isinstance(msg, PostMsg):
                self._handle_post(msg)
            else:
                self._handle_sync(msg)
        except Exception:
            log.exception("error handling %s", type(msg).__name__)

    def _request_stop(self, signum: int, _frame: object) -> None:
        log.info("received signal %d, shutting down", signum)
        self._stop.set()

    def run(self) -> None:
        self.paths.ensure()
        write_pidfile(self.paths)
        if self.paths.fifo.exists():
            self.paths.fifo.unlink()
        os.mkfifo(self.paths.fifo, 0o600)
        # O_RDWR keeps a writer open so reads see EAGAIN (not EOF) when idle,
        # and shell-side opens never block.
        fifo_fd = os.open(self.paths.fifo, os.O_RDWR | os.O_NONBLOCK)

        signal.signal(signal.SIGTERM, self._request_stop)
        signal.signal(signal.SIGINT, self._request_stop)

        sel = selectors.DefaultSelector()
        sel.register(fifo_fd, selectors.EVENT_READ)
        self.observer.start()
        log.info("chronx daemon started (pid %d, store %s)", os.getpid(), self.paths.home)

        buffer = b""
        try:
            while not self._stop.is_set():
                timeout = 0.5
                if self._finalize_q:
                    timeout = min(
                        timeout, max(0.0, self._finalize_q[0][0] - time.monotonic())
                    )
                for _key, _mask in sel.select(timeout):
                    try:
                        chunk = os.read(fifo_fd, 65536)
                    except BlockingIOError:
                        continue
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if raw:
                            self._dispatch(raw.decode("utf-8", errors="replace"))
                self._process_due()
        finally:
            # Flush anything still in flight so short-lived sessions aren't lost.
            while self._finalize_q:
                _, _, pending, post = heapq.heappop(self._finalize_q)
                try:
                    self._finalize(pending, post)
                except Exception:
                    log.exception("failed to finalize during shutdown")
            self.observer.stop()
            self.observer.join(timeout=5)
            sel.close()
            os.close(fifo_fd)
            self.conn.close()
            self.paths.fifo.unlink(missing_ok=True)
            remove_pidfile(self.paths)
            log.info("chronx daemon stopped")


# ---------------------------------------------------------------- lifecycle


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int) -> None:
        super().__init__(f"daemon already running (pid {pid})")
        self.pid = pid


def read_pid(paths: Paths) -> int | None:
    try:
        return int(paths.pidfile.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_pid(paths: Paths) -> int | None:
    """Pid of a *live* daemon, or None (cleans up a stale pidfile)."""
    pid = read_pid(paths)
    if pid is None:
        return None
    if pid_alive(pid):
        return pid
    paths.pidfile.unlink(missing_ok=True)
    paths.fifo.unlink(missing_ok=True)
    return None


def write_pidfile(paths: Paths) -> None:
    existing = daemon_pid(paths)
    if existing is not None:
        raise AlreadyRunning(existing)
    fd = os.open(paths.pidfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))


def remove_pidfile(paths: Paths) -> None:
    if read_pid(paths) == os.getpid():
        paths.pidfile.unlink(missing_ok=True)


def setup_logging(paths: Paths, *, foreground: bool) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(paths.log, encoding="utf-8")]
    if foreground and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def run_foreground(paths: Paths) -> None:
    paths.ensure()
    setup_logging(paths, foreground=True)
    Daemon(paths, Config.load(paths)).run()


def spawn(paths: Paths) -> int:
    """Start the daemon detached in the background; return its pid."""
    existing = daemon_pid(paths)
    if existing is not None:
        raise AlreadyRunning(existing)
    paths.ensure()
    with open(paths.log, "ab") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "-m", "chronx", "daemon", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
            cwd="/",
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        pid = daemon_pid(paths)
        if pid is not None and paths.fifo.exists():
            return pid
        if proc.poll() is not None:
            raise RuntimeError(
                f"daemon exited immediately (status {proc.returncode}); "
                f"see {paths.log}"
            )
        time.sleep(0.05)
    raise RuntimeError(f"daemon did not come up within 5s; see {paths.log}")


def stop(paths: Paths, *, force: bool = False, wait: float = 6.0) -> bool:
    """Stop a running daemon. Returns True if one was stopped."""
    pid = daemon_pid(paths)
    if pid is None:
        return False
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            paths.pidfile.unlink(missing_ok=True)
            paths.fifo.unlink(missing_ok=True)
            return True
        time.sleep(0.05)
    raise TimeoutError(f"daemon (pid {pid}) did not stop within {wait:.0f}s")
