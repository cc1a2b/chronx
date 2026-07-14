"""Portable export/import of recorded history (`chronx export` / `import`).

An export bundles one root's events, deltas, marks, manifest, and every blob
they reference into a single gzip-compressed tar archive:

    chronx-meta.json     metadata + full event/delta/mark/manifest tables
    blobs/<digest>       raw (uncompressed) content, one file per blob

Import verifies each blob re-hashes to its name, dedups against the local
store, appends the events under the target root (remapping the path with
`--as`), and returns a summary. Blobs are stored RAW in the archive so the
format never depends on the store's on-disk compression.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from . import db as dbm
from .config import Paths
from .store import HASH_ALGO, ObjectStore, hash_bytes

FORMAT = 1
META_NAME = "chronx-meta.json"
_HEX = re.compile(r"^[0-9a-f]{16,128}$")


class TransferError(RuntimeError):
    """User-facing export/import failure."""


@dataclass(frozen=True)
class ExportStats:
    root: str
    events: int
    deltas: int
    marks: int
    blobs: int
    bytes_written: int


@dataclass(frozen=True)
class ImportStats:
    root: str
    events: int
    deltas: int
    marks: int
    marks_renamed: int
    blobs_added: int
    blobs_skipped: int
    remapped: bool
    events_skipped: int = 0


# --------------------------------------------------------------------- export


def export_root(
    conn: sqlite3.Connection, store: ObjectStore, root_row: sqlite3.Row, out: Path
) -> ExportStats:
    root_id = int(root_row["id"])
    events = list(
        conn.execute(
            "SELECT * FROM events WHERE root_id = ? ORDER BY id", (root_id,)
        )
    )
    meta_events = []
    delta_total = 0
    for e in events:
        deltas = [dict(d) for d in dbm.deltas_for_rows(conn, int(e["id"]))]
        delta_total += len(deltas)
        meta_events.append(
            {
                "id": int(e["id"]),
                "session": e["session"],
                "cwd": e["cwd"],
                "command": e["command"],
                "started_at": e["started_at"],
                "finished_at": e["finished_at"],
                "exit_code": e["exit_code"],
                "deltas": deltas,
            }
        )
    marks = [
        {"name": m["name"], "ts": m["ts"]}
        for m in conn.execute(
            "SELECT * FROM marks WHERE root_id = ? ORDER BY ts", (root_id,)
        )
    ]
    manifest = [
        dict(r)
        for r in conn.execute(
            "SELECT path, hash, size, mtime, mode FROM manifest WHERE root_id = ?",
            (root_id,),
        )
    ]
    refs = dbm.root_referenced_hashes(conn, root_id)

    meta = {
        "format": FORMAT,
        "hash_algo": HASH_ALGO,
        "chronx_version": __version__,
        "exported_at": time.time(),
        "root": {"path": root_row["path"], "added_at": root_row["added_at"]},
        "events": meta_events,
        "marks": marks,
        "manifest": manifest,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    written_blobs = 0
    with tarfile.open(out, "w:gz") as tar:
        meta_bytes = json.dumps(meta).encode("utf-8")
        info = tarfile.TarInfo(META_NAME)
        info.size = len(meta_bytes)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(meta_bytes))
        for digest in sorted(refs):
            try:
                raw = store.get(digest)
            except (KeyError, ValueError):
                continue  # blob pruned/corrupt; history entry travels without content
            info = tarfile.TarInfo(f"blobs/{digest}")
            info.size = len(raw)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(raw))
            written_blobs += 1

    return ExportStats(
        root=root_row["path"],
        events=len(events),
        deltas=delta_total,
        marks=len(marks),
        blobs=written_blobs,
        bytes_written=out.stat().st_size,
    )


# --------------------------------------------------------------------- import


def _remap_cwd(cwd: str, old_root: str, new_root: str) -> str:
    old_root = old_root.rstrip("/")
    if cwd == old_root:
        return new_root
    if cwd.startswith(old_root + "/"):
        return new_root.rstrip("/") + cwd[len(old_root):]
    return new_root  # unrelated cwd (shouldn't happen); pin to the root


def import_archive(
    paths: Paths,
    archive: Path,
    *,
    as_path: Path | None = None,
    dedup: bool = False,
) -> ImportStats:
    """Import an exported archive into the local store. Daemon must be stopped
    (the caller enforces that).

    With `dedup`, events already present on the target root (matched on
    started_at + command) are skipped, so repeated imports/pulls of the same
    source are idempotent and effectively incremental.
    """
    if not archive.is_file():
        raise TransferError(f"{archive} does not exist")
    store = ObjectStore(paths.objects)

    try:
        tar = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise TransferError(f"cannot read archive: {exc}") from exc

    with tar:
        try:
            meta_member = tar.getmember(META_NAME)
        except KeyError:
            raise TransferError("not a chronx archive (no chronx-meta.json)") from None
        meta_file = tar.extractfile(meta_member)
        assert meta_file is not None
        try:
            meta = json.loads(meta_file.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TransferError(f"corrupt metadata: {exc}") from exc

        if int(meta.get("format", 0)) != FORMAT:
            raise TransferError(
                f"unsupported archive format {meta.get('format')} "
                f"(this chronx speaks {FORMAT})"
            )
        if meta.get("hash_algo") != HASH_ALGO:
            raise TransferError(
                f"archive hashed with {meta.get('hash_algo')!r}, local store uses "
                f"{HASH_ALGO!r}; cannot merge histories"
            )

        # 1. Blobs: verify + dedup into the local store.
        added = skipped = 0
        for member in tar:
            if not member.name.startswith("blobs/") or not member.isfile():
                continue
            digest = member.name[len("blobs/"):]
            if not _HEX.match(digest):
                continue
            if store.has(digest):
                skipped += 1
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            raw = fh.read()
            if hash_bytes(raw) != digest:
                raise TransferError(
                    f"blob {digest[:12]} failed integrity check; archive is corrupt"
                )
            store.put_bytes(raw)
            added += 1

    old_root = meta["root"]["path"]
    new_root = str((as_path or Path(old_root)).resolve())
    remapped = new_root != old_root

    conn = dbm.connect(paths.db)
    try:
        dbm.init_db(conn)
        root_id, created = dbm.ensure_root(conn, new_root)
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE root_id = ?", (root_id,)
        ).fetchone()["n"]
        if created:
            # ensure_root stamps added_at = now, which is AFTER every imported
            # event — that would make rollback/diff reject the imported window.
            # Anchor tracking-start to the origin's value (or the earliest event).
            origin_added = float(meta["root"].get("added_at") or time.time())
            earliest = min(
                (float(e["started_at"]) for e in meta["events"]),
                default=origin_added,
            )
            with conn:
                conn.execute(
                    "UPDATE roots SET added_at = ? WHERE id = ?",
                    (min(origin_added, earliest), root_id),
                )

        ev_count = delta_count = skipped_events = 0
        with conn:
            for e in meta["events"]:
                if dedup:
                    dup = conn.execute(
                        "SELECT 1 FROM events WHERE root_id = ? AND started_at = ?"
                        " AND command IS ? LIMIT 1",
                        (root_id, e["started_at"], e["command"]),
                    ).fetchone()
                    if dup is not None:
                        skipped_events += 1
                        continue
                cwd = _remap_cwd(e["cwd"], old_root, new_root) if remapped else e["cwd"]
                cur = conn.execute(
                    "INSERT INTO events (session, root_id, cwd, command, started_at,"
                    " finished_at, exit_code) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (e["session"], root_id, cwd, e["command"], e["started_at"],
                     e["finished_at"], e["exit_code"]),
                )
                new_event_id = int(cur.lastrowid)  # type: ignore[arg-type]
                ev_count += 1
                rows = [
                    (new_event_id, d["path"], d["change"], d["before_hash"],
                     d["after_hash"], d["before_size"], d["after_size"],
                     d["before_mode"], d["after_mode"])
                    for d in e["deltas"]
                ]
                delta_count += len(rows)
                if rows:
                    conn.executemany(
                        "INSERT INTO deltas (event_id, path, change, before_hash,"
                        " after_hash, before_size, after_size, before_mode, after_mode)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )

            # Manifest only for a fresh root; never clobber a tracked one.
            if existing == 0 and meta.get("manifest"):
                conn.executemany(
                    "INSERT OR REPLACE INTO manifest"
                    " (root_id, path, hash, size, mtime, mode)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [(root_id, m["path"], m["hash"], m["size"], m["mtime"], m["mode"])
                     for m in meta["manifest"]],
                )

            # Establish a 'main' timeline for a freshly-imported root, so
            # branches/graph/fork work and events are branch-attributed (the
            # export flattens the source's branches into one local timeline).
            if dbm.active_branch_id(conn, root_id) is None:
                added_at = conn.execute(
                    "SELECT added_at FROM roots WHERE id = ?", (root_id,)
                ).fetchone()["added_at"]
                mcur = conn.execute(
                    "INSERT INTO branches (root_id, name, parent_branch_id, base_ts,"
                    " created_at) VALUES (?, 'main', NULL, ?, ?)",
                    (root_id, added_at, time.time()),
                )
                main_id = int(mcur.lastrowid)  # type: ignore[arg-type]
                conn.execute(
                    "UPDATE events SET branch_id = ? WHERE root_id = ?"
                    " AND branch_id IS NULL",
                    (main_id, root_id),
                )
                conn.execute(
                    "UPDATE roots SET active_branch_id = ? WHERE id = ?",
                    (main_id, root_id),
                )
                # True baseline (state before the first event), derived from the
                # now-inserted manifest + deltas — NOT the imported manifest,
                # which is the latest state.
                baseline = dbm._compute_baseline(conn, root_id)
                if baseline:
                    conn.executemany(
                        "INSERT OR IGNORE INTO root_baseline"
                        " (root_id, path, hash, size, mode) VALUES (?, ?, ?, ?, ?)",
                        [(root_id, p, h, s, m) for p, h, s, m in baseline],
                    )

            marks_added = marks_renamed = 0
            for m in meta.get("marks", []):
                name = m["name"]
                for attempt in (name, f"{name}-imported", f"{name}-imported-2"):
                    try:
                        conn.execute(
                            "INSERT INTO marks (name, ts, root_id, created_at)"
                            " VALUES (?, ?, ?, ?)",
                            (attempt, m["ts"], root_id, time.time()),
                        )
                        marks_added += 1
                        if attempt != name:
                            marks_renamed += 1
                        break
                    except sqlite3.IntegrityError:
                        continue
    finally:
        conn.close()

    return ImportStats(
        root=new_root,
        events=ev_count,
        deltas=delta_count,
        marks=marks_added,
        marks_renamed=marks_renamed,
        blobs_added=added,
        blobs_skipped=skipped,
        remapped=remapped,
        events_skipped=skipped_events,
    )
