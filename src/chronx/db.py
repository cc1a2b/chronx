"""SQLite event log: commands, the file deltas they caused, and manifests.

Tables
------
roots     directories chronx tracks (one row per watched working dir)
events    one row per executed command (or external/undo change)
deltas    per-event file changes: path + before/after blob hashes
manifest  current known state of each tracked root (path -> hash/size/mtime)
meta      store metadata (schema version, hash algorithm)

The daemon is the only writer during normal operation; CLI commands read
concurrently via WAL. `chronx undo` also writes (a backup event) and then
tells the daemon to resync its in-memory manifest.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .store import HASH_ALGO

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roots (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT,
    root_id     INTEGER NOT NULL REFERENCES roots(id),
    cwd         TEXT NOT NULL,
    command     TEXT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    exit_code   INTEGER
);
CREATE TABLE IF NOT EXISTS deltas (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    path        TEXT NOT NULL,
    change      TEXT NOT NULL CHECK (change IN ('A', 'M', 'D')),
    before_hash TEXT,
    after_hash  TEXT,
    before_size INTEGER,
    after_size  INTEGER,
    before_mode INTEGER,
    after_mode  INTEGER
);
CREATE TABLE IF NOT EXISTS manifest (
    root_id INTEGER NOT NULL REFERENCES roots(id),
    path    TEXT NOT NULL,
    hash    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL NOT NULL,
    mode    INTEGER NOT NULL,
    PRIMARY KEY (root_id, path)
);
CREATE INDEX IF NOT EXISTS idx_events_root_time ON events(root_id, started_at);
CREATE INDEX IF NOT EXISTS idx_deltas_event ON deltas(event_id);
CREATE INDEX IF NOT EXISTS idx_deltas_path ON deltas(path);
"""

EXTERNAL_COMMAND = None  # events.command for changes not caused by a shell command


@dataclass(frozen=True)
class ManifestEntry:
    hash: str
    size: int
    mtime: float
    mode: int


@dataclass(frozen=True)
class Delta:
    path: str  # relative to the root
    change: str  # 'A' added, 'M' modified, 'D' deleted
    before_hash: str | None
    after_hash: str | None
    before_size: int | None = None
    after_size: int | None = None
    before_mode: int | None = None
    after_mode: int | None = None


class StoreMismatch(RuntimeError):
    """The on-disk store was created with a different hash algorithm."""


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key = 'hash_algo'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('hash_algo', ?), ('schema_version', ?)",
                (HASH_ALGO, str(SCHEMA_VERSION)),
            )
        elif row["value"] != HASH_ALGO:
            raise StoreMismatch(
                f"store was created with {row['value']!r} but this install hashes "
                f"with {HASH_ALGO!r}; delete the store or install the matching extra"
            )


# --- roots -----------------------------------------------------------------


def ensure_root(conn: sqlite3.Connection, path: str) -> tuple[int, bool]:
    """Return (root_id, created)."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO roots (path, added_at) VALUES (?, ?)",
            (path, time.time()),
        )
        created = cur.rowcount == 1
    row = conn.execute("SELECT id FROM roots WHERE path = ?", (path,)).fetchone()
    return int(row["id"]), created


def get_roots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM roots ORDER BY path"))


def root_for_path(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    """The most specific tracked root containing `path`, if any."""
    resolved = path.resolve()
    best: sqlite3.Row | None = None
    for row in get_roots(conn):
        root = Path(row["path"])
        if resolved == root or root in resolved.parents:
            if best is None or len(str(root)) > len(str(best["path"])):
                best = row
    return best


# --- manifest ---------------------------------------------------------------


def load_manifest(conn: sqlite3.Connection, root_id: int) -> dict[str, ManifestEntry]:
    return {
        row["path"]: ManifestEntry(
            hash=row["hash"], size=row["size"], mtime=row["mtime"], mode=row["mode"]
        )
        for row in conn.execute("SELECT * FROM manifest WHERE root_id = ?", (root_id,))
    }


def apply_manifest(
    conn: sqlite3.Connection,
    root_id: int,
    updates: dict[str, ManifestEntry],
    deletes: set[str],
) -> None:
    """Persist manifest mutations. Caller manages the transaction."""
    if deletes:
        conn.executemany(
            "DELETE FROM manifest WHERE root_id = ? AND path = ?",
            [(root_id, p) for p in deletes],
        )
    if updates:
        conn.executemany(
            "INSERT INTO manifest (root_id, path, hash, size, mtime, mode)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (root_id, path) DO UPDATE SET"
            " hash = excluded.hash, size = excluded.size,"
            " mtime = excluded.mtime, mode = excluded.mode",
            [(root_id, p, e.hash, e.size, e.mtime, e.mode) for p, e in updates.items()],
        )


# --- events / deltas ---------------------------------------------------------


def record_event(
    conn: sqlite3.Connection,
    *,
    session: str | None,
    root_id: int,
    cwd: str,
    command: str | None,
    started_at: float,
    finished_at: float | None,
    exit_code: int | None,
    deltas: list[Delta],
    manifest_updates: dict[str, ManifestEntry] | None = None,
    manifest_deletes: set[str] | None = None,
) -> int:
    """Insert an event, its deltas, and manifest changes in one transaction."""
    with conn:
        cur = conn.execute(
            "INSERT INTO events (session, root_id, cwd, command, started_at,"
            " finished_at, exit_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session, root_id, cwd, command, started_at, finished_at, exit_code),
        )
        event_id = int(cur.lastrowid)  # type: ignore[arg-type]
        if deltas:
            conn.executemany(
                "INSERT INTO deltas (event_id, path, change, before_hash, after_hash,"
                " before_size, after_size, before_mode, after_mode)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event_id,
                        d.path,
                        d.change,
                        d.before_hash,
                        d.after_hash,
                        d.before_size,
                        d.after_size,
                        d.before_mode,
                        d.after_mode,
                    )
                    for d in deltas
                ],
            )
        apply_manifest(conn, root_id, manifest_updates or {}, manifest_deletes or set())
    return event_id


def event_by_id(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def deltas_for(conn: sqlite3.Connection, event_id: int) -> list[Delta]:
    return [
        Delta(
            path=r["path"],
            change=r["change"],
            before_hash=r["before_hash"],
            after_hash=r["after_hash"],
            before_size=r["before_size"],
            after_size=r["after_size"],
            before_mode=r["before_mode"],
            after_mode=r["after_mode"],
        )
        for r in conn.execute(
            "SELECT * FROM deltas WHERE event_id = ? ORDER BY path", (event_id,)
        )
    ]


def delta_counts(conn: sqlite3.Connection, event_id: int) -> dict[str, int]:
    counts = {"A": 0, "M": 0, "D": 0}
    for row in conn.execute(
        "SELECT change, COUNT(*) AS n FROM deltas WHERE event_id = ? GROUP BY change",
        (event_id,),
    ):
        counts[row["change"]] = row["n"]
    return counts


def last_event(
    conn: sqlite3.Connection,
    *,
    root_id: int | None = None,
    with_deltas_only: bool = False,
) -> sqlite3.Row | None:
    sql = "SELECT e.* FROM events e"
    where: list[str] = []
    params: list[object] = []
    if with_deltas_only:
        sql += " JOIN deltas d ON d.event_id = e.id"
    if root_id is not None:
        where.append("e.root_id = ?")
        params.append(root_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def event_at(
    conn: sqlite3.Connection, ts: float, *, root_id: int | None = None
) -> sqlite3.Row | None:
    """Latest event started at or before `ts` (else the earliest event)."""
    scope = "" if root_id is None else " AND root_id = ?"
    params: list[object] = [ts] + ([root_id] if root_id is not None else [])
    row = conn.execute(
        f"SELECT * FROM events WHERE started_at <= ?{scope}"
        " ORDER BY started_at DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is not None:
        return row
    scope = "" if root_id is None else " WHERE root_id = ?"
    return conn.execute(
        f"SELECT * FROM events{scope} ORDER BY started_at ASC, id ASC LIMIT 1",
        [root_id] if root_id is not None else [],
    ).fetchone()


def recent_events(
    conn: sqlite3.Connection,
    *,
    root_id: int | None = None,
    limit: int = 500,
    changes_only: bool = False,
) -> list[sqlite3.Row]:
    """Most recent events, returned oldest-first (timeline order)."""
    where: list[str] = []
    params: list[object] = []
    if root_id is not None:
        where.append("root_id = ?")
        params.append(root_id)
    if changes_only:
        where.append("EXISTS (SELECT 1 FROM deltas d WHERE d.event_id = events.id)")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = list(
        conn.execute(
            f"SELECT * FROM events{clause} ORDER BY id DESC LIMIT ?",
            params,
        )
    )
    rows.reverse()
    return rows


def max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
    return int(row["m"])


def manifest_entry(
    conn: sqlite3.Connection, root_id: int, path: str
) -> ManifestEntry | None:
    row = conn.execute(
        "SELECT * FROM manifest WHERE root_id = ? AND path = ?", (root_id, path)
    ).fetchone()
    if row is None:
        return None
    return ManifestEntry(
        hash=row["hash"], size=row["size"], mtime=row["mtime"], mode=row["mode"]
    )


def last_delta_for_path(
    conn: sqlite3.Connection,
    root_id: int,
    rel_path: str,
    *,
    at: float | None = None,
    event_id: int | None = None,
) -> sqlite3.Row | None:
    """The most recent delta touching rel_path (optionally at/before a time,
    or within one specific event)."""
    sql = (
        "SELECT d.*, e.id AS event_id, e.started_at AS started_at,"
        " e.command AS command"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND d.path = ?"
    )
    params: list[object] = [root_id, rel_path]
    if event_id is not None:
        sql += " AND e.id = ?"
        params.append(event_id)
    if at is not None:
        sql += " AND e.started_at <= ?"
        params.append(at)
    sql += " ORDER BY e.id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def referenced_hashes(conn: sqlite3.Connection) -> set[str]:
    """Every blob digest still reachable from deltas or manifests."""
    refs: set[str] = set()
    for column in ("before_hash", "after_hash"):
        refs.update(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT {column} FROM deltas WHERE {column} IS NOT NULL"
            )
        )
    refs.update(r[0] for r in conn.execute("SELECT DISTINCT hash FROM manifest"))
    return refs


def events_touching(
    conn: sqlite3.Connection, root_id: int, rel_path: str
) -> list[sqlite3.Row]:
    """Events whose deltas include this path, newest first, with change kind."""
    return list(
        conn.execute(
            "SELECT e.*, d.change AS change, d.before_hash AS before_hash,"
            " d.after_hash AS after_hash"
            " FROM deltas d JOIN events e ON e.id = d.event_id"
            " WHERE e.root_id = ? AND d.path = ?"
            " ORDER BY e.id DESC",
            (root_id, rel_path),
        )
    )
