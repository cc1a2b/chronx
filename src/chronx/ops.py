"""Command logic for diff, blame, and undo (kept out of the click layer)."""

from __future__ import annotations

import os
import shlex
import sqlite3
import stat as statmod
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import db as dbm
from .config import Paths
from .ipc import encode_post, encode_pre, encode_sync, send_line
from .store import ObjectStore, hash_bytes
from .when import fmt_ts

UNDO_SESSION = "chronx"


class OpsError(RuntimeError):
    """User-facing operation failure."""


# --------------------------------------------------------------- event lookup


def resolve_event(
    conn: sqlite3.Connection, spec: str, cwd: Path
) -> sqlite3.Row:
    """Resolve 'last', an event id, or a time spec (epoch float) to an event.

    Time strings are parsed by the caller (when.parse_when) so this only
    deals with 'last', integer ids, and pre-parsed floats via `#<ts>`.
    """
    from .when import WhenParseError, parse_when

    root = dbm.root_for_path(conn, cwd)
    root_id = int(root["id"]) if root is not None else None

    if spec == "last":
        row = dbm.last_event(conn, root_id=root_id)
        if row is None and root_id is not None:
            row = dbm.last_event(conn)
        if row is None:
            raise OpsError("no events recorded yet — is the daemon running?")
        return row

    stripped = spec.lstrip("#")
    if stripped.isdigit() and float(stripped) < 1e9:
        row = dbm.event_by_id(conn, int(stripped))
        if row is None:
            raise OpsError(f"no event with id {stripped}")
        return row

    mark = dbm.get_mark(conn, spec)
    if mark is not None:
        ts = float(mark["ts"])
    else:
        try:
            ts = parse_when(spec)
        except WhenParseError as exc:
            raise OpsError(str(exc)) from exc
    row = dbm.event_at(conn, ts, root_id=root_id)
    if row is None and root_id is not None:
        row = dbm.event_at(conn, ts)
    if row is None:
        raise OpsError("no events recorded yet — is the daemon running?")
    return row


def describe_command(row: sqlite3.Row) -> str:
    return row["command"] if row["command"] is not None else "(external change)"


# --------------------------------------------------------------------- blame


@dataclass(frozen=True)
class BlameEntry:
    event_id: int
    started_at: float
    change: str
    command: str
    session: str | None
    exit_code: int | None


def blame_file(conn: sqlite3.Connection, file: Path) -> tuple[str, list[BlameEntry]]:
    """Return (path relative to its root, events touching it, newest first)."""
    resolved = file.resolve()
    root = dbm.root_for_path(conn, resolved)
    if root is None:
        raise OpsError(
            f"{file} is not inside any tracked directory "
            "(run a command there from an instrumented shell first)"
        )
    rel = os.path.relpath(resolved, root["path"]).replace(os.sep, "/")
    rows = dbm.events_touching(conn, int(root["id"]), rel)
    entries = [
        BlameEntry(
            event_id=int(r["id"]),
            started_at=float(r["started_at"]),
            change=r["change"],
            command=describe_command(r),
            session=r["session"],
            exit_code=r["exit_code"],
        )
        for r in rows
    ]
    return rel, entries


# ---------------------------------------------------------------------- undo


@dataclass(frozen=True)
class UndoStep:
    """One file to touch when reverting an event."""

    rel: str
    action: str  # 'restore' (write before-content) or 'remove' (was added)
    target_hash: str | None  # blob to write, None for remove
    target_mode: int | None
    target_size: int | None
    current_hash: str | None  # what's on disk right now (None = absent)
    current_size: int | None
    current_mode: int | None
    conflict: bool  # disk state doesn't match what the event left behind


@dataclass(frozen=True)
class UndoPlan:
    event: sqlite3.Row
    root: Path
    root_id: int
    steps: list[UndoStep]

    @property
    def conflicts(self) -> list[UndoStep]:
        return [s for s in self.steps if s.conflict]


def _current_state(path: Path) -> tuple[bytes | None, os.stat_result | None]:
    try:
        st = os.lstat(path)
    except OSError:
        return None, None
    if not statmod.S_ISREG(st.st_mode):
        return None, st
    try:
        return path.read_bytes(), st
    except OSError:
        return None, st


def plan_undo(
    conn: sqlite3.Connection, store: ObjectStore, event: sqlite3.Row
) -> UndoPlan:
    """Work out exactly what reverting `event` would do, without touching disk."""
    root_row = conn.execute(
        "SELECT * FROM roots WHERE id = ?", (event["root_id"],)
    ).fetchone()
    if root_row is None:
        raise OpsError(f"event #{event['id']} references an unknown root")
    root = Path(root_row["path"])
    deltas = dbm.deltas_for(conn, int(event["id"]))
    if not deltas:
        raise OpsError(f"event #{event['id']} changed no files; nothing to undo")

    steps: list[UndoStep] = []
    for d in deltas:
        full = root / d.path
        data, st = _current_state(full)
        cur_hash = hash_bytes(data) if data is not None else None
        cur_size = len(data) if data is not None else None
        cur_mode = st.st_mode if st is not None else None

        if d.change == "A":
            action, target = "remove", None
        else:  # M or D -> put the before-content back
            if d.before_hash is None:
                raise OpsError(
                    f"event #{event['id']} has no captured content for {d.path}"
                )
            if not store.has(d.before_hash):
                raise OpsError(
                    f"blob for {d.path} is missing from the object store; "
                    "cannot undo safely"
                )
            action, target = "restore", d.before_hash

        # Conflict = the file changed again after this event, so reverting
        # would also wipe out that later change.
        if d.after_hash is None:
            expected_present = False if d.change == "D" else None  # None = unknown
        else:
            expected_present = True
        if expected_present is None:
            conflict = True  # content wasn't captured (size cap); can't verify
        elif expected_present:
            conflict = cur_hash != d.after_hash
        else:
            conflict = data is not None or (st is not None)

        steps.append(
            UndoStep(
                rel=d.path,
                action=action,
                target_hash=target,
                target_mode=d.before_mode,
                target_size=d.before_size,
                current_hash=cur_hash,
                current_size=cur_size,
                current_mode=cur_mode,
                conflict=conflict,
            )
        )
    return UndoPlan(event=event, root=root, root_id=int(root_row["id"]), steps=steps)


def _write_atomic(path: Path, data: bytes, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".chronx-undo-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, statmod.S_IMODE(mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_undo(
    conn: sqlite3.Connection,
    store: ObjectStore,
    paths: Paths,
    plan: UndoPlan,
    *,
    skip_conflicts: bool,
) -> tuple[int, list[str]]:
    """Execute an undo plan.

    Safety: before any file is touched, its current content is stored in the
    object store and the whole revert is recorded as a new event — so the undo
    itself can be undone. Returns (backup_event_id, applied_rel_paths).
    """
    steps = [s for s in plan.steps if not (s.conflict and skip_conflicts)]
    if not steps:
        raise OpsError("nothing to do (all files conflicted and were skipped)")

    # 1. Snapshot current state of every file we are about to touch.
    for step in steps:
        full = plan.root / step.rel
        data, _ = _current_state(full)
        if data is not None:
            store.put_bytes(data)

    # 2. Apply, collecting the deltas the undo itself causes.
    applied: list[str] = []
    backup_deltas: list[dbm.Delta] = []
    manifest_updates: dict[str, dbm.ManifestEntry] = {}
    manifest_deletes: set[str] = set()

    for step in steps:
        full = plan.root / step.rel
        if step.action == "remove":
            try:
                full.unlink(missing_ok=True)
            except OSError as exc:
                raise OpsError(f"could not remove {step.rel}: {exc}") from exc
            if step.current_hash is not None:
                backup_deltas.append(
                    dbm.Delta(
                        step.rel, "D", step.current_hash, None,
                        step.current_size, None, step.current_mode, None,
                    )
                )
            manifest_deletes.add(step.rel)
        else:
            assert step.target_hash is not None
            data = store.get(step.target_hash)
            try:
                _write_atomic(full, data, step.target_mode)
            except OSError as exc:
                raise OpsError(f"could not restore {step.rel}: {exc}") from exc
            st = os.lstat(full)
            change = "M" if step.current_hash is not None else "A"
            if step.current_hash != step.target_hash:
                backup_deltas.append(
                    dbm.Delta(
                        step.rel, change, step.current_hash, step.target_hash,
                        step.current_size, len(data), step.current_mode, st.st_mode,
                    )
                )
            manifest_updates[step.rel] = dbm.ManifestEntry(
                hash=step.target_hash, size=len(data), mtime=st.st_mtime, mode=st.st_mode
            )
        applied.append(step.rel)

    # 3. Record the revert as an event of its own (this is the redo handle).
    now = time.time()
    backup_event_id = dbm.record_event(
        conn,
        session=UNDO_SESSION,
        root_id=plan.root_id,
        cwd=str(plan.root),
        command=f"chronx undo #{plan.event['id']}",
        started_at=now,
        finished_at=now,
        exit_code=0,
        deltas=backup_deltas,
        manifest_updates=manifest_updates,
        manifest_deletes=manifest_deletes,
    )

    # 4. Tell a running daemon to resync so it doesn't double-record this.
    send_line(paths.fifo, encode_sync(str(plan.root)))
    return backup_event_id, applied


def find_undo_target(conn: sqlite3.Connection, cwd: Path) -> sqlite3.Row:
    """Latest event *with deltas* for the root containing cwd."""
    root = dbm.root_for_path(conn, cwd)
    root_id = int(root["id"]) if root is not None else None
    row = dbm.last_event(conn, root_id=root_id, with_deltas_only=True)
    if row is None:
        raise OpsError(
            "no file-changing events recorded"
            + (f" under {root['path']}" if root is not None else "")
        )
    return row


# ------------------------------------------------------------ cat / restore


@dataclass(frozen=True)
class HistoricContent:
    """A file's recorded state at some moment."""

    root: Path
    root_id: int
    rel: str
    digest: str | None  # None = the file did not exist at that moment
    mode: int | None
    size: int | None
    source: str  # human description of where this state comes from


def _resolve_tracked(conn: sqlite3.Connection, file: Path) -> tuple[sqlite3.Row, str]:
    resolved = file.resolve()
    root = dbm.root_for_path(conn, resolved)
    if root is None:
        raise OpsError(f"{file} is not inside any tracked directory")
    rel = os.path.relpath(resolved, root["path"]).replace(os.sep, "/")
    return root, rel


def content_at(
    conn: sqlite3.Connection,
    file: Path,
    *,
    at: float | None = None,
    event_id: int | None = None,
    before: bool = False,
) -> HistoricContent:
    """The recorded state of `file` after (or before) an event or moment.

    Selector: `event_id` pins one event; otherwise `at` (default now) picks
    the last event touching the file at/before that time. `before=True`
    returns the state just before the selected event instead of after it.
    """
    root, rel = _resolve_tracked(conn, file)
    root_id = int(root["id"])
    root_path = Path(root["path"])

    row = dbm.last_delta_for_path(conn, root_id, rel, at=at, event_id=event_id)
    if row is not None:
        digest = row["before_hash"] if before else row["after_hash"]
        mode = row["before_mode"] if before else row["after_mode"]
        size = row["before_size"] if before else row["after_size"]
        side = "before" if before else "after"
        cmd = row["command"] if row["command"] is not None else "(external change)"
        return HistoricContent(
            root=root_path, root_id=root_id, rel=rel, digest=digest,
            mode=mode, size=size,
            source=f"{side} event #{row['event_id']} ($ {cmd})",
        )

    if event_id is not None:
        raise OpsError(f"event #{event_id} did not touch {rel}")
    entry = dbm.manifest_entry(conn, root_id, rel)
    if entry is None:
        raise OpsError(
            f"no recorded content for {rel} (never snapshotted — "
            "ignored, oversized, or created before tracking began)"
        )
    return HistoricContent(
        root=root_path, root_id=root_id, rel=rel, digest=entry.hash,
        mode=entry.mode, size=entry.size,
        source="baseline snapshot (file unchanged since tracking began)",
    )


def restore_file(
    conn: sqlite3.Connection,
    store: ObjectStore,
    paths: Paths,
    target: HistoricContent,
) -> tuple[int, str]:
    """Write a historic state back to disk, with the same safety net as undo:
    current content is snapshotted first and the restore is recorded as its
    own event. Returns (backup_event_id, action description)."""
    full = target.root / target.rel
    data, st = _current_state(full)
    cur_hash = hash_bytes(data) if data is not None else None
    cur_size = len(data) if data is not None else None
    cur_mode = st.st_mode if st is not None else None

    if cur_hash == target.digest:
        raise OpsError(f"{target.rel} already matches that state; nothing to do")
    if data is not None:
        store.put_bytes(data)  # safety snapshot of what we're replacing

    manifest_updates: dict[str, dbm.ManifestEntry] = {}
    manifest_deletes: set[str] = set()
    if target.digest is None:
        if data is None and st is None:
            raise OpsError(f"{target.rel} already absent; nothing to do")
        try:
            full.unlink(missing_ok=True)
        except OSError as exc:
            raise OpsError(f"could not remove {target.rel}: {exc}") from exc
        change, after_hash, after_size, after_mode = "D", None, None, None
        action = "deleted (file did not exist at that moment)"
        manifest_deletes.add(target.rel)
    else:
        if not store.has(target.digest):
            raise OpsError(f"blob for {target.rel} is missing from the object store")
        blob = store.get(target.digest)
        try:
            _write_atomic(full, blob, target.mode)
        except OSError as exc:
            raise OpsError(f"could not write {target.rel}: {exc}") from exc
        new_st = os.lstat(full)
        change = "M" if cur_hash is not None else "A"
        after_hash, after_size, after_mode = target.digest, len(blob), new_st.st_mode
        action = f"restored to {target.source}"
        manifest_updates[target.rel] = dbm.ManifestEntry(
            hash=target.digest, size=len(blob),
            mtime=new_st.st_mtime, mode=new_st.st_mode,
        )

    now = time.time()
    backup_event_id = dbm.record_event(
        conn,
        session=UNDO_SESSION,
        root_id=target.root_id,
        cwd=str(target.root),
        command=f"chronx restore {target.rel} ({target.source})",
        started_at=now,
        finished_at=now,
        exit_code=0,
        deltas=[
            dbm.Delta(
                target.rel, change, cur_hash, after_hash,
                cur_size, after_size, cur_mode, after_mode,
            )
        ],
        manifest_updates=manifest_updates,
        manifest_deletes=manifest_deletes,
    )
    send_line(paths.fifo, encode_sync(str(target.root)))
    return backup_event_id, action


# ------------------------------------------------------------------ rollback


@dataclass(frozen=True)
class RollbackStep:
    rel: str
    action: str  # 'restore', 'delete', 'create'
    target_hash: str | None  # None = file did not exist at that moment
    target_mode: int | None
    target_size: int | None
    current_hash: str | None
    current_size: int | None
    current_mode: int | None
    blocked: str | None = None  # reason this step cannot be applied


@dataclass(frozen=True)
class RollbackPlan:
    root: Path
    root_id: int
    ts: float
    label: str  # human description of the target moment
    steps: list[RollbackStep]

    @property
    def applicable(self) -> list[RollbackStep]:
        return [s for s in self.steps if s.blocked is None]

    @property
    def blocked(self) -> list[RollbackStep]:
        return [s for s in self.steps if s.blocked is not None]


def plan_rollback(
    conn: sqlite3.Connection,
    store: ObjectStore,
    cwd: Path,
    ts: float,
    label: str,
    *,
    path_prefix: str | None = None,
) -> RollbackPlan:
    """Reconstruct the tree state at `ts` and plan the writes to get back there.

    For every path some event touched after `ts`, the earliest such delta's
    before-side IS the state at `ts` — restore that. Untouched paths are
    already at their `ts` state and never appear in the plan.
    """
    root_row = dbm.root_for_path(conn, cwd)
    if root_row is None:
        raise OpsError(f"{cwd} is not inside any tracked directory")
    root = Path(root_row["path"])
    root_id = int(root_row["id"])
    if ts < float(root_row["added_at"]):
        raise OpsError(
            f"{fmt_ts(ts)} is before chronx started tracking {root} "
            f"({fmt_ts(root_row['added_at'])}); nothing recorded that far back"
        )

    prefix = path_prefix.strip("/") if path_prefix else None
    steps: list[RollbackStep] = []
    for rel in dbm.paths_changed_since(conn, root_id, ts, path_prefix=prefix):
        first = dbm.first_delta_after(conn, root_id, rel, ts)
        if first is None:  # raced away; shouldn't happen
            continue
        target_hash = first["before_hash"]
        target_mode = first["before_mode"]
        target_size = first["before_size"]

        full = root / rel
        data, st = _current_state(full)
        cur_hash = hash_bytes(data) if data is not None else None
        cur_size = len(data) if data is not None else None
        cur_mode = st.st_mode if st is not None else None

        if cur_hash == target_hash and (target_hash is not None or st is None):
            continue  # changed after ts, but changed back — already correct

        blocked: str | None = None
        if target_hash is not None and not store.has(target_hash):
            blocked = "blob missing from object store (pruned by gc?)"
        if target_hash is None:
            action = "delete"
        elif cur_hash is None and st is None:
            action = "create"
        else:
            action = "restore"
        steps.append(
            RollbackStep(
                rel=rel, action=action,
                target_hash=target_hash, target_mode=target_mode,
                target_size=target_size,
                current_hash=cur_hash, current_size=cur_size, current_mode=cur_mode,
                blocked=blocked,
            )
        )
    return RollbackPlan(root=root, root_id=root_id, ts=ts, label=label, steps=steps)


def apply_rollback(
    conn: sqlite3.Connection,
    store: ObjectStore,
    paths: Paths,
    plan: RollbackPlan,
) -> tuple[int, int]:
    """Execute a rollback plan (applicable steps only) as ONE recorded event,
    so the whole rollback can itself be reverted with a single undo.
    Returns (backup_event_id, files_changed)."""
    steps = plan.applicable
    if not steps:
        raise OpsError("nothing to apply (already at that state, or all steps blocked)")

    for step in steps:  # safety snapshots before touching anything
        data, _ = _current_state(plan.root / step.rel)
        if data is not None:
            store.put_bytes(data)

    deltas: list[dbm.Delta] = []
    manifest_updates: dict[str, dbm.ManifestEntry] = {}
    manifest_deletes: set[str] = set()
    for step in steps:
        full = plan.root / step.rel
        if step.target_hash is None:
            try:
                full.unlink(missing_ok=True)
            except OSError as exc:
                raise OpsError(f"could not remove {step.rel}: {exc}") from exc
            if step.current_hash is not None:
                deltas.append(
                    dbm.Delta(
                        step.rel, "D", step.current_hash, None,
                        step.current_size, None, step.current_mode, None,
                    )
                )
            manifest_deletes.add(step.rel)
        else:
            blob = store.get(step.target_hash)
            try:
                _write_atomic(full, blob, step.target_mode)
            except OSError as exc:
                raise OpsError(f"could not restore {step.rel}: {exc}") from exc
            st = os.lstat(full)
            change = "M" if step.current_hash is not None else "A"
            deltas.append(
                dbm.Delta(
                    step.rel, change, step.current_hash, step.target_hash,
                    step.current_size, len(blob), step.current_mode, st.st_mode,
                )
            )
            manifest_updates[step.rel] = dbm.ManifestEntry(
                hash=step.target_hash, size=len(blob),
                mtime=st.st_mtime, mode=st.st_mode,
            )

    now = time.time()
    event_id = dbm.record_event(
        conn,
        session=UNDO_SESSION,
        root_id=plan.root_id,
        cwd=str(plan.root),
        command=f"chronx rollback to {plan.label}",
        started_at=now,
        finished_at=now,
        exit_code=0,
        deltas=deltas,
        manifest_updates=manifest_updates,
        manifest_deletes=manifest_deletes,
    )
    send_line(paths.fifo, encode_sync(str(plan.root)))
    return event_id, len(deltas)


# ------------------------------------------------------------------ bisect


@dataclass(frozen=True)
class BisectStep:
    event_id: int
    command: str
    started_at: float
    good: bool


@dataclass(frozen=True)
class BisectResult:
    culprit: sqlite3.Row | None  # first bad event (the regression), or None
    last_good_id: int | None
    candidates: int
    tests_run: int
    steps: list[BisectStep]


def state_at(
    conn: sqlite3.Connection, root_id: int, ts: float
) -> dict[str, tuple[str | None, int | None]]:
    """The full recorded content of every tracked file at time `ts`.

    Returns rel_path -> (blob_hash, mode); a hash of None means the file did
    not exist at that moment. Independent of current disk state, so it can
    materialize any historical point (not just "revert from latest").
    """
    from itertools import groupby

    state: dict[str, tuple[str | None, int | None]] = {}
    # Files that never changed sit at their baseline (still in the manifest).
    for row in conn.execute(
        "SELECT path, hash, mode FROM manifest WHERE root_id = ?", (root_id,)
    ):
        state[row["path"]] = (row["hash"], row["mode"])

    rows = conn.execute(
        "SELECT d.path AS path, d.after_hash AS after_hash, d.after_mode AS after_mode,"
        " d.before_hash AS before_hash, d.before_mode AS before_mode,"
        " e.started_at AS started_at"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? ORDER BY d.path, e.id",
        (root_id,),
    )
    for path, group in groupby(rows, key=lambda r: r["path"]):
        history = list(group)
        at_or_before = [r for r in history if r["started_at"] <= ts]
        if at_or_before:
            last = at_or_before[-1]
            state[path] = (last["after_hash"], last["after_mode"])
        else:
            first = history[0]  # state before the first-ever change = baseline
            state[path] = (first["before_hash"], first["before_mode"])
    return state


def reconstruct_to(
    conn: sqlite3.Connection,
    store: ObjectStore,
    cwd: Path,
    ts: float,
    label: str,
) -> Path:
    """Silently rewrite the working tree to its FULL recorded state at `ts`.

    Records no event and sends no sync — a throwaway reconstruction for bisect,
    which restores the original state when done. Works from any current disk
    state (bisect jumps around), so it materializes the complete tree at `ts`
    rather than diffing against 'latest'. Requires the daemon stopped.
    """
    root_row = dbm.root_for_path(conn, cwd)
    if root_row is None:
        raise OpsError(f"{cwd} is not inside any tracked directory")
    root = Path(root_row["path"])
    desired = state_at(conn, int(root_row["id"]), ts)

    missing = [
        rel for rel, (h, _m) in desired.items()
        if h is not None and not store.has(h)
    ]
    if missing:
        raise OpsError(
            f"cannot reconstruct {label}: {len(missing)} file(s) have missing "
            f"blobs (first: {missing[0]}); run `chronx fsck`"
        )

    for rel, (target_hash, target_mode) in desired.items():
        full = root / rel
        if target_hash is None:
            try:
                full.unlink(missing_ok=True)
            except OSError as exc:
                raise OpsError(f"could not remove {rel}: {exc}") from exc
            continue
        data, st = _current_state(full)
        if st is not None and data is not None and hash_bytes(data) == target_hash:
            continue  # already correct on disk
        try:
            _write_atomic(full, store.get(target_hash), target_mode)
        except OSError as exc:
            raise OpsError(f"could not restore {rel}: {exc}") from exc
    return root


def _run_test(command: list[str], cwd: Path, timeout: float | None) -> int:
    shell = os.environ.get("SHELL") or "/bin/sh"
    argv = [shell, "-c", shlex.join(command)]
    try:
        return subprocess.run(argv, cwd=str(cwd), timeout=timeout).returncode
    except subprocess.TimeoutExpired as exc:
        raise OpsError(
            f"test command timed out after {timeout:g}s; raise --timeout or make "
            "the test terminate"
        ) from exc


def bisect_history(
    conn: sqlite3.Connection,
    store: ObjectStore,
    paths: Paths,
    cwd: Path,
    *,
    good_ts: float,
    bad_ts: float,
    test: list[str],
    verify: bool = True,
    timeout: float | None = None,
    on_test=None,
) -> BisectResult:
    """Binary-search the file-changing events in (good_ts, bad_ts] for the
    first one whose resulting tree state fails `test` (exit != 0).

    The working tree is reconstructed to each candidate state to run the test,
    then restored to the bad (starting) state on the way out.
    """
    if bad_ts <= good_ts:
        raise OpsError("the good moment must be earlier than the bad moment")
    root_row = dbm.root_for_path(conn, cwd)
    if root_row is None:
        raise OpsError(f"{cwd} is not inside any tracked directory")
    root_id = int(root_row["id"])

    candidates = [
        e
        for e in dbm.events_between(
            conn, since=good_ts, until=bad_ts, root_id=root_id, changes_only=True
        )
        if float(e["started_at"]) > good_ts
    ]
    if not candidates:
        raise OpsError(
            "no file-changing events between the good and bad moments — "
            "nothing could have changed the outcome"
        )

    steps: list[BisectStep] = []
    tests = 0

    def test_at(event: sqlite3.Row) -> bool:
        nonlocal tests
        reconstruct_to(
            conn, store, cwd, float(event["started_at"]), f"event #{event['id']}"
        )
        rc = _run_test(test, cwd, timeout)
        tests += 1
        good = rc == 0
        steps.append(
            BisectStep(int(event["id"]), describe_command(event),
                       float(event["started_at"]), good)
        )
        if on_test is not None:
            on_test(event, good)
        return good

    try:
        if verify:
            # Endpoints must actually be good/bad or the search is meaningless.
            reconstruct_to(conn, store, cwd, good_ts, "the good moment")
            if _run_test(test, cwd, timeout) != 0:
                raise OpsError("the test FAILS at the good moment; pick an earlier "
                               "--good or fix the test")
            tests += 1
            reconstruct_to(conn, store, cwd, bad_ts, "the bad moment")
            if _run_test(test, cwd, timeout) == 0:
                raise OpsError("the test PASSES at the bad moment; pick a later "
                               "--bad — the regression isn't in this window")
            tests += 1

        lo, hi = 0, len(candidates) - 1
        first_bad: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if test_at(candidates[mid]):
                lo = mid + 1
            else:
                first_bad = mid
                hi = mid - 1

        culprit = candidates[first_bad] if first_bad is not None else None
        last_good_id = (
            int(candidates[first_bad - 1]["id"])
            if first_bad is not None and first_bad > 0
            else None
        )
        return BisectResult(
            culprit=culprit,
            last_good_id=last_good_id,
            candidates=len(candidates),
            tests_run=tests,
            steps=steps,
        )
    finally:
        # Always leave the tree where we found it (the bad/starting state).
        try:
            reconstruct_to(conn, store, cwd, bad_ts, "the starting state")
        except OpsError:
            pass


# ---------------------------------------------------------------- range diff


@dataclass(frozen=True)
class RangeChange:
    """Net difference of one file between two moments."""

    rel: str
    a_hash: str | None
    b_hash: str | None
    a_size: int | None
    b_size: int | None
    a_mode: int | None
    b_mode: int | None

    def as_delta(self) -> dbm.Delta:
        if self.a_hash is None:
            change = "A"
        elif self.b_hash is None:
            change = "D"
        else:
            change = "M"
        return dbm.Delta(
            self.rel, change, self.a_hash, self.b_hash,
            self.a_size, self.b_size, self.a_mode, self.b_mode,
        )


def range_changes(
    conn: sqlite3.Connection, cwd: Path, a_ts: float, b_ts: float
) -> tuple[Path, list[RangeChange]]:
    """Net file changes between two moments (state@a vs state@b).

    A file that changed and changed back inside the window nets to nothing
    and is omitted.
    """
    if b_ts < a_ts:
        a_ts, b_ts = b_ts, a_ts
    root_row = dbm.root_for_path(conn, cwd)
    if root_row is None:
        raise OpsError(f"{cwd} is not inside any tracked directory")
    root_id = int(root_row["id"])

    changes: list[RangeChange] = []
    for rel in conn.execute(
        "SELECT DISTINCT d.path FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND e.started_at > ? AND e.started_at <= ?"
        " ORDER BY d.path",
        (root_id, a_ts, b_ts),
    ):
        rel = rel["path"]
        first = dbm.first_delta_after(conn, root_id, rel, a_ts)
        last = dbm.last_delta_for_path(conn, root_id, rel, at=b_ts)
        if first is None or last is None:  # defensive; window query implies both
            continue
        a_hash, b_hash = first["before_hash"], last["after_hash"]
        if a_hash == b_hash:
            continue  # net zero inside the window
        changes.append(
            RangeChange(
                rel=rel,
                a_hash=a_hash, b_hash=b_hash,
                a_size=first["before_size"], b_size=last["after_size"],
                a_mode=first["before_mode"], b_mode=last["after_mode"],
            )
        )
    return Path(root_row["path"]), changes


# ------------------------------------------------------------- exec / rerun


def _wait_for_root_attach(paths: Paths, cwd: Path, *, timeout: float = 5.0) -> None:
    """Block until the daemon is watching cwd's root (or give up quietly).

    A command's changes can only be attributed if the watch and baseline
    exist BEFORE the command writes anything. Interactive hooks can't wait,
    but `chronx exec` can — making fresh-root recording deterministic.
    """
    deadline = time.monotonic() + timeout
    try:
        conn = dbm.connect(paths.db, readonly=True)
    except sqlite3.Error:
        return
    try:
        root_row = None
        while time.monotonic() < deadline:
            root_row = dbm.root_for_path(conn, cwd)
            if root_row is not None:
                break
            time.sleep(0.05)
        if root_row is None:
            return
        # Root row appears before the baseline scan finishes; wait briefly
        # for manifest rows too (an empty dir legitimately never gets any).
        sub_deadline = min(deadline, time.monotonic() + 2.0)
        while time.monotonic() < sub_deadline:
            n = conn.execute(
                "SELECT COUNT(*) FROM manifest WHERE root_id = ?",
                (root_row["id"],),
            ).fetchone()[0]
            if n:
                return
            time.sleep(0.05)
    finally:
        conn.close()


def record_and_run(
    paths: Paths,
    command: list[str] | str,
    *,
    cwd: Path,
    session: str | None = None,
) -> int:
    """Run a command while recording it through the daemon, exactly as an
    instrumented shell would (PRE before, POST with the exit code after).

    Strings run through $SHELL -c; argv lists run directly. Requires a
    running daemon — otherwise there is nothing to record into.
    """
    if isinstance(command, str):
        display = command
        argv = [os.environ.get("SHELL") or "/bin/sh", "-c", command]
    else:
        display = shlex.join(command)
        argv = command
    session = (
        session
        or os.environ.get("CHRONX_SESSION")
        or f"chronx-exec-{os.getpid()}"
    )

    if not send_line(paths.fifo, encode_pre(session, time.time(), str(cwd), display)):
        raise OpsError(
            "the chronx daemon is not running (`chronx daemon start`), "
            "so this command would not be recorded"
        )
    _wait_for_root_attach(paths, cwd)
    rc = 130  # if we die on the way, close the window as interrupted
    try:
        rc = subprocess.run(argv, cwd=str(cwd)).returncode
    except FileNotFoundError as exc:
        rc = 127
        raise OpsError(f"cannot run {display!r}: {exc}") from exc
    except KeyboardInterrupt:
        rc = 130
        raise
    finally:
        send_line(paths.fifo, encode_post(session, time.time(), rc))
    return rc


# ------------------------------------------------------------------ gc


@dataclass(frozen=True)
class PruneStats:
    events_deleted: int
    deltas_deleted: int
    blobs_deleted: int
    bytes_freed: int
    blobs_kept: int


def prune(
    conn: sqlite3.Connection,
    store: ObjectStore,
    *,
    keep_days: float,
    dry_run: bool,
) -> PruneStats:
    """Delete events older than keep_days, then any blob no longer referenced
    by a remaining delta or manifest. The daemon must be stopped (the caller
    enforces this): it caches manifests and dedups against the object store.
    """
    cutoff = time.time() - keep_days * 86400.0
    events = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE started_at < ?", (cutoff,)
    ).fetchone()["n"]
    deltas = conn.execute(
        "SELECT COUNT(*) AS n FROM deltas WHERE event_id IN"
        " (SELECT id FROM events WHERE started_at < ?)",
        (cutoff,),
    ).fetchone()["n"]

    if not dry_run and events:
        with conn:
            conn.execute(
                "DELETE FROM deltas WHERE event_id IN"
                " (SELECT id FROM events WHERE started_at < ?)",
                (cutoff,),
            )
            conn.execute("DELETE FROM events WHERE started_at < ?", (cutoff,))

    # Referenced set AFTER the deletion above (or as it would be, on dry runs).
    if dry_run:
        refs = set()
        for column in ("before_hash", "after_hash"):
            refs.update(
                r[0]
                for r in conn.execute(
                    f"SELECT DISTINCT {column} FROM deltas WHERE {column} IS NOT NULL"
                    " AND event_id IN (SELECT id FROM events WHERE started_at >= ?)",
                    (cutoff,),
                )
            )
        refs.update(r[0] for r in conn.execute("SELECT DISTINCT hash FROM manifest"))
    else:
        refs = dbm.referenced_hashes(conn)

    blobs_deleted = bytes_freed = blobs_kept = 0
    for digest, _path, size in store.iter_blobs():
        if digest in refs:
            blobs_kept += 1
            continue
        blobs_deleted += 1
        bytes_freed += size
        if not dry_run:
            store.delete(digest)

    if not dry_run:
        conn.execute("VACUUM")
    return PruneStats(
        events_deleted=events,
        deltas_deleted=deltas,
        blobs_deleted=blobs_deleted,
        bytes_freed=bytes_freed,
        blobs_kept=blobs_kept,
    )
