"""chronx recover — bring deleted files back into the working tree.

Find every path that this timeline once recorded but that is now gone, and
restore each one's *last-recorded content* to disk. Think of it as an undelete:
you deleted ``notes.txt`` three commands ago and want it back, without hunting
for the exact event or replaying anything.

A file is *recoverable* when, on the cwd's root + active timeline:
  * it is absent from the current recorded tree state
    (``state_at`` says its hash is ``None`` / it is not there); and
  * some past delta captured content for it (a most-recent delta with a
    non-``None`` ``after_hash``); and
  * that content blob still lives in the object store (not pruned by ``gc``).

The whole recovery lands as ONE ordinary event on the active timeline, so it is
fully reversible with ``chronx undo``.

Safety mirrors ``cherry_pick.py`` / ``ops.apply_merge`` exactly:
  * the daemon must be stopped for the write path (it would otherwise race the
    recorder) — ``--list`` is pure read-only and never needs that;
  * every file's current on-disk content is snapshotted into the object store
    BEFORE we overwrite it, so the recover can always be undone;
  * blobs are written atomically (temp file in the same dir + ``os.replace``,
    via ``X.write_atomic``);
  * the writes land as one recorded event and the daemon is told to resync so
    it does not double-record our own writes.

This module only touches the working tree through that safety net and never
imports ``chronx.cli``.
"""

from __future__ import annotations

import fnmatch
import os
import time

from chronx import pluginlib as X


# --------------------------------------------------------------------- model


class _Recoverable:
    """One deleted path we can bring back, with its last-known recorded state.

    A plain ``__slots__`` class rather than a ``@dataclass`` — mirroring
    ``cherry_pick._Change``: plugins are exec'd as standalone modules and, under
    ``from __future__ import annotations``, ``@dataclass`` would try to resolve
    field annotations through the module globals and can blow up.
    """

    __slots__ = (
        "rel",       # path relative to the root
        "hash",      # last-recorded content blob digest (guaranteed in store)
        "mode",      # last-recorded file mode
        "size",      # last-recorded size (bytes), may be None if uncaptured
        "when_ts",   # epoch when it was deleted (or last touched, fallback)
        "when_cmd",  # command of that event, for context (may be None/external)
    )

    def __init__(
        self,
        rel: str,
        hash: str,
        mode: int | None,
        size: int | None,
        when_ts: float,
        when_cmd: str | None,
    ) -> None:
        self.rel = rel
        self.hash = hash
        self.mode = mode
        self.size = size
        self.when_ts = when_ts
        self.when_cmd = when_cmd


# ------------------------------------------------------------------- discovery


def _find_recoverable(
    conn, store: X.ObjectStore, root_id: int, branch_id: int | None
) -> list[_Recoverable]:
    """Every deleted-but-restorable path on this root + active timeline.

    Pure read: reconstructs the current recorded tree state, walks this
    timeline's delta history, and keeps paths that are gone now but whose last
    captured content is still a blob in the store.
    """
    # The full CURRENT recorded content of the tree (independent of disk). A
    # path is "present" only when it has a non-None hash here.
    current = X.state_at(conn, root_id, time.time())

    def present(rel: str) -> bool:
        entry = current.get(rel)
        return entry is not None and entry[0] is not None

    # Every delta this root + branch ever recorded, grouped per path in
    # chronological (event-id) order. ``branch_id IS ?`` matches an int branch
    # and, defensively, a NULL branch on a not-yet-migrated store.
    rows = conn.execute(
        "SELECT d.path AS path, d.after_hash AS after_hash,"
        " d.after_mode AS after_mode, d.after_size AS after_size,"
        " e.started_at AS started_at, e.command AS command"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND e.branch_id IS ?"
        " ORDER BY d.path, e.id",
        (root_id, branch_id),
    ).fetchall()

    history: dict[str, list] = {}
    for r in rows:
        history.setdefault(r["path"], []).append(r)

    recoverable: list[_Recoverable] = []
    for rel, deltas in history.items():
        if present(rel):
            continue  # still on record — this is not a deletion

        # Last-recorded content = most recent delta whose after_hash is set.
        # Deletion event = most recent delta whose after_hash is NULL.
        content = None
        deleted = None
        for r in deltas:  # ascending by event id
            if r["after_hash"] is not None:
                content = r
            else:
                deleted = r
        if content is None:
            continue  # never had captured content — nothing to bring back
        if not store.has(content["after_hash"]):
            continue  # blob pruned/missing — cannot restore it safely

        when = deleted if deleted is not None else deltas[-1]
        recoverable.append(
            _Recoverable(
                rel=rel,
                hash=content["after_hash"],
                mode=content["after_mode"],
                size=content["after_size"],
                when_ts=float(when["started_at"]),
                when_cmd=when["command"],
            )
        )

    recoverable.sort(key=lambda item: item.rel)
    return recoverable


def _matches(pattern: str, rel: str) -> bool:
    """True if `pattern` globs the full relative path or just the basename."""
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(rel), pattern)


def _size_str(size: int | None) -> str:
    return X.human_bytes(size) if size is not None else "?"


# ------------------------------------------------------------------ command


def register(main) -> None:
    @main.command()
    @X.click.argument("pattern", default="*")
    @X.click.option("--yes", "-y", is_flag=True,
                    help="Skip the confirmation prompt.")
    @X.click.option("--list", "list_only", is_flag=True,
                    help="Only list recoverable files (read-only); do not restore.")
    def recover(pattern: str, yes: bool, list_only: bool) -> None:
        """Bring back deleted files, restoring their last-recorded content.

        Every path this timeline recorded but that is now gone is a candidate.
        With no PATTERN all recoverable files are restored; give a glob (matched
        against the path or its basename) to narrow it. The restore is recorded
        as one reversible event — undo it with `chronx undo`.
        """
        # Write path must never race the recorder. --list is pure read-only and
        # deliberately usable while the daemon runs.
        if not list_only:
            X.require_daemon_stopped("recover")

        conn = X.open_db(readonly=list_only)
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            root_path = X.Path(root["path"])
            branch_id = X.active_branch_id(conn, root_id)

            recoverable = _find_recoverable(conn, store, root_id, branch_id)
            matched = [item for item in recoverable if _matches(pattern, item.rel)]

            # Friendly, zero-exit outcomes when there is simply nothing to do.
            if not recoverable:
                X.click.echo("nothing to recover — no deleted files on record here")
                return
            if not matched:
                X.click.echo(
                    f"no recoverable files match {pattern!r} "
                    f"({len(recoverable)} recoverable — see `chronx recover --list`)"
                )
                return

            # --list: report the recoverable set and stop (read-only).
            if list_only:
                X.click.secho(
                    f"{len(matched)} recoverable file(s) "
                    "(deleted, content still on record):", bold=True,
                )
                width = max(len(item.rel) for item in matched)
                for item in matched:
                    X.click.echo(
                        f"  {item.rel:<{width}}  {_size_str(item.size):>9}"
                        f"  deleted {X.fmt_ts(item.when_ts)}"
                    )
                return

            # --- write path -------------------------------------------------
            # Summarize and (unless --yes) confirm before touching disk.
            X.click.secho(
                f"recover {len(matched)} deleted file(s) into the working tree:",
                bold=True,
            )
            for item in matched:
                X.click.echo(
                    f"  restore  {item.rel}  "
                    f"({_size_str(item.size)}, deleted {X.fmt_ts(item.when_ts)})"
                )
            if not yes:
                if not X.click.confirm("Recover these files?", default=False):
                    raise X.click.Abort()

            # Apply, MIRRORING cherry_pick: snapshot any current on-disk content
            # first (reversibility), then atomically write the last-known blob,
            # collecting the deltas this recover causes.
            applied: list[X.dbm.Delta] = []
            manifest_updates: dict[str, X.dbm.ManifestEntry] = {}
            for item in matched:
                full = root_path / item.rel
                cur_bytes, cur_st = X.current_file_state(full)
                cur_hash = X.hash_bytes(cur_bytes) if cur_bytes is not None else None
                cur_size = len(cur_bytes) if cur_bytes is not None else None
                cur_mode = cur_st.st_mode if cur_st is not None else None

                if cur_hash == item.hash:
                    continue  # already on disk with the recorded content

                if cur_bytes is not None:
                    store.put_bytes(cur_bytes)  # safety snapshot of what we replace

                blob = store.get(item.hash)
                try:
                    X.write_atomic(full, blob, item.mode)
                except OSError as exc:
                    raise X.click.ClickException(
                        f"could not write {item.rel}: {exc}"
                    ) from exc

                new_st = os.lstat(full)
                # 'A' because the file was absent; 'M' only if something else is
                # on disk there right now (present-but-different).
                change = "M" if cur_hash is not None else "A"
                applied.append(X.dbm.Delta(
                    item.rel, change, cur_hash, item.hash,
                    cur_size, len(blob), cur_mode, new_st.st_mode,
                ))
                manifest_updates[item.rel] = X.dbm.ManifestEntry(
                    hash=item.hash, size=len(blob),
                    mtime=new_st.st_mtime, mode=new_st.st_mode,
                )

            if not applied:
                raise X.OpsError(
                    "nothing to recover (matching files already present on disk "
                    "with their recorded content)"
                )

            # Record the recover as ONE event on the active timeline — the
            # handle `chronx undo` reverts.
            now = time.time()
            new_id = X.dbm.record_event(
                conn,
                session="chronx",
                root_id=root_id,
                cwd=str(root_path),
                command=f"chronx recover {pattern}",
                started_at=now,
                finished_at=now,
                exit_code=0,
                deltas=applied,
                manifest_updates=manifest_updates,
                manifest_deletes=set(),
            )

            # Nudge a (re)started daemon to resync so it does not re-record our
            # own writes as a separate external change.
            X.send_sync(root_path)

            X.click.secho(
                f"recovered {len(applied)} file(s)", fg="green",
            )
            X.click.echo(f"  recorded as event #{new_id}")
            X.click.secho(
                f"  undo with `chronx undo --event {new_id}`", dim=True,
            )
        except X.OpsError as exc:
            # Surface logical failures as clean CLI errors.
            raise X.click.ClickException(str(exc)) from exc
        finally:
            conn.close()
