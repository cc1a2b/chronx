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
CREATE TABLE IF NOT EXISTS marks (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    ts         REAL NOT NULL,
    root_id    INTEGER REFERENCES roots(id),
    created_at REAL NOT NULL
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
    session: str | None = None,
) -> list[sqlite3.Row]:
    """Most recent events, returned oldest-first (timeline order)."""
    where: list[str] = []
    params: list[object] = []
    if root_id is not None:
        where.append("root_id = ?")
        params.append(root_id)
    if changes_only:
        where.append("EXISTS (SELECT 1 FROM deltas d WHERE d.event_id = events.id)")
    if session is not None:
        where.append("session = ?")
        params.append(session)
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


def events_between(
    conn: sqlite3.Connection,
    *,
    since: float | None = None,
    until: float | None = None,
    root_id: int | None = None,
    session: str | None = None,
    changes_only: bool = False,
    limit: int = 2000,
) -> list[sqlite3.Row]:
    """Events in a time window, oldest-first."""
    where: list[str] = []
    params: list[object] = []
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    if until is not None:
        where.append("started_at <= ?")
        params.append(until)
    if root_id is not None:
        where.append("root_id = ?")
        params.append(root_id)
    if session is not None:
        where.append("session = ?")
        params.append(session)
    if changes_only:
        where.append("EXISTS (SELECT 1 FROM deltas d WHERE d.event_id = events.id)")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    return list(
        conn.execute(
            f"SELECT * FROM events{clause} ORDER BY id ASC LIMIT ?", params
        )
    )


def events_after(
    conn: sqlite3.Connection,
    after_id: int,
    *,
    root_id: int | None = None,
    changes_only: bool = False,
) -> list[sqlite3.Row]:
    """Events newer than a given id, oldest-first (for live tailing)."""
    where = ["id > ?"]
    params: list[object] = [after_id]
    if root_id is not None:
        where.append("root_id = ?")
        params.append(root_id)
    if changes_only:
        where.append("EXISTS (SELECT 1 FROM deltas d WHERE d.event_id = events.id)")
    return list(
        conn.execute(
            f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id ASC",
            params,
        )
    )


def list_sessions(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    """Per-session activity summary, most recently active first."""
    return list(
        conn.execute(
            "SELECT e.session AS session, COUNT(*) AS events,"
            " SUM(COALESCE(dc.n, 0)) AS changes,"
            " MIN(e.started_at) AS first_ts, MAX(e.started_at) AS last_ts,"
            " MAX(e.cwd) AS cwd"
            " FROM events e LEFT JOIN"
            "  (SELECT event_id, COUNT(*) AS n FROM deltas GROUP BY event_id) dc"
            "  ON dc.event_id = e.id"
            " GROUP BY e.session ORDER BY last_ts DESC LIMIT ?",
            (limit,),
        )
    )


def root_summaries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every root with its event and tracked-file counts."""
    return list(
        conn.execute(
            "SELECT r.*,"
            " (SELECT COUNT(*) FROM events e WHERE e.root_id = r.id) AS events,"
            " (SELECT COUNT(*) FROM manifest m WHERE m.root_id = r.id) AS files"
            " FROM roots r ORDER BY r.path"
        )
    )


def forget_root(conn: sqlite3.Connection, root_id: int) -> tuple[int, int]:
    """Erase a root and all its history. Returns (events, deltas) deleted."""
    with conn:
        deltas = conn.execute(
            "DELETE FROM deltas WHERE event_id IN"
            " (SELECT id FROM events WHERE root_id = ?)",
            (root_id,),
        ).rowcount
        events = conn.execute(
            "DELETE FROM events WHERE root_id = ?", (root_id,)
        ).rowcount
        conn.execute("UPDATE marks SET root_id = NULL WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM manifest WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))
    return events, deltas


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


# --- marks ------------------------------------------------------------------


def add_mark(
    conn: sqlite3.Connection, name: str, ts: float, root_id: int | None
) -> None:
    """Create a named point in time. Raises sqlite3.IntegrityError on dupes."""
    with conn:
        conn.execute(
            "INSERT INTO marks (name, ts, root_id, created_at) VALUES (?, ?, ?, ?)",
            (name, ts, root_id, time.time()),
        )


def get_mark(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    try:
        return conn.execute("SELECT * FROM marks WHERE name = ?", (name,)).fetchone()
    except sqlite3.OperationalError:  # pre-marks store opened read-only
        return None


def list_marks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return list(conn.execute("SELECT * FROM marks ORDER BY ts"))
    except sqlite3.OperationalError:
        return []


def delete_mark(conn: sqlite3.Connection, name: str) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM marks WHERE name = ?", (name,))
    return cur.rowcount > 0


# --- rollback ----------------------------------------------------------------


def paths_changed_since(
    conn: sqlite3.Connection,
    root_id: int,
    ts: float,
    *,
    path_prefix: str | None = None,
) -> list[str]:
    """Every path touched by an event after `ts` (they may differ from state@ts)."""
    sql = (
        "SELECT DISTINCT d.path FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND e.started_at > ?"
    )
    params: list[object] = [root_id, ts]
    if path_prefix:
        sql += " AND (d.path = ? OR d.path LIKE ? ESCAPE '\\')"
        params += [path_prefix, _like_escape(path_prefix) + "/%"]
    return [r["path"] for r in conn.execute(sql + " ORDER BY d.path", params)]


def first_delta_after(
    conn: sqlite3.Connection, root_id: int, path: str, ts: float
) -> sqlite3.Row | None:
    """The earliest delta touching `path` after `ts`; its before_* fields are
    exactly the file's state at `ts`."""
    return conn.execute(
        "SELECT d.* FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND d.path = ? AND e.started_at > ?"
        " ORDER BY e.id ASC LIMIT 1",
        (root_id, path, ts),
    ).fetchone()


# --- search ------------------------------------------------------------------


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_commands(
    conn: sqlite3.Connection,
    needle: str,
    *,
    root_id: int | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Events whose command contains `needle` (case-insensitive), newest first."""
    sql = "SELECT * FROM events WHERE command LIKE ? ESCAPE '\\'"
    params: list[object] = [f"%{_like_escape(needle)}%"]
    if root_id is not None:
        sql += " AND root_id = ?"
        params.append(root_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


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
