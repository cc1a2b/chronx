"""chronx cherry-pick — replay one recorded event onto the current tree.

Like ``git cherry-pick``: take the file changes a single recorded event made
(the *after* side of each of its deltas) and apply them onto the CURRENT
working tree, regardless of which timeline that event lives on. The application
is recorded as a NEW, ordinary event on the active timeline, so it is fully
reversible with ``chronx undo``.

Safety mirrors ``ops.apply_merge`` exactly:
  * the daemon must be stopped (we would otherwise race the recorder);
  * every file's current content is snapshotted into the object store BEFORE
    we overwrite/delete it, so the cherry-pick can always be undone;
  * blobs are written atomically (temp file in the same dir + ``os.replace``);
  * the whole thing lands as one recorded event and the daemon is told to
    resync so it does not double-record our own writes.

This module only touches the working tree through that safety net; it never
imports ``chronx.cli``.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time

from chronx import pluginlib as X
from chronx.ipc import encode_sync, send_line


# --------------------------------------------------------------------- helpers


def _current_state(path: X.Path) -> tuple[bytes | None, os.stat_result | None]:
    """Current on-disk content + stat of ``path`` (mirrors ops._current_state).

    Returns ``(None, None)`` if absent, ``(None, st)`` for a non-regular file
    (symlink/dir/etc.) or one we cannot read, and ``(bytes, st)`` otherwise.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None, None
    if not stat.S_ISREG(st.st_mode):
        return None, st
    try:
        return path.read_bytes(), st
    except OSError:
        return None, st


def _write_atomic(path: X.Path, data: bytes, mode: int | None) -> None:
    """Write ``data`` to ``path`` atomically (mirrors ops._write_atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".chronx-cherry-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, stat.S_IMODE(mode))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Change:
    """One file the cherry-pick will touch, with its target and current state.

    A plain ``__slots__`` class rather than a ``@dataclass``: plugins are exec'd
    outside ``sys.modules``, and ``@dataclass`` under ``from __future__ import
    annotations`` would try to resolve field annotations via
    ``sys.modules[__module__].__dict__`` and blow up.
    """

    __slots__ = (
        "rel",          # path relative to the root
        "target_hash",  # blob to write; None => the event deleted this file
        "target_mode",
        "cur_bytes",    # what's on disk right now (None = absent/unreadable)
        "cur_hash",
        "cur_size",
        "cur_mode",
    )

    def __init__(self, rel, target_hash, target_mode,
                 cur_bytes, cur_hash, cur_size, cur_mode):
        self.rel = rel
        self.target_hash = target_hash
        self.target_mode = target_mode
        self.cur_bytes = cur_bytes
        self.cur_hash = cur_hash
        self.cur_size = cur_size
        self.cur_mode = cur_mode


# ------------------------------------------------------------------ command


def register(main) -> None:
    @main.command("cherry-pick")
    @X.click.argument("event")
    @X.click.option("--yes", "-y", is_flag=True,
                    help="Skip the confirmation prompt.")
    def cherry_pick(event: str, yes: bool) -> None:
        """Apply the file changes of one recorded EVENT onto the working tree.

        EVENT is an event id (optionally written ``#N``). Its recorded *after*
        state for each file it changed is applied to the current tree — even if
        that event lives on a different timeline — and the result is recorded as
        a new, reversible event on the active timeline.
        """
        # 1. Refuse to run while the recorder is live (it would race our writes).
        X.require_daemon_stopped("cherry-pick")

        raw = event.lstrip("#")
        if not raw.isdigit():
            raise X.click.ClickException(f"invalid event id: {event!r}")
        source_id = int(raw)

        conn = X.open_db(readonly=False)
        store = X.ObjectStore(X.paths().objects)
        try:
            # 2. Resolve the source event and its recorded file changes.
            row = X.dbm.event_by_id(conn, source_id)
            if row is None:
                raise X.OpsError(f"no event with id {source_id}")
            source_deltas = X.dbm.deltas_for(conn, source_id)
            if not source_deltas:
                raise X.OpsError(
                    f"event #{source_id} changed no files; nothing to cherry-pick"
                )

            # 3. Everything applies relative to the root containing the cwd,
            #    regardless of where the source event was recorded.
            root = X.root_for_cwd(conn)
            root_path = X.Path(root["path"])

            # 4. Build the plan: for each source delta work out the target state
            #    and compare it against what is on disk right now.
            plan: list[_Change] = []
            for d in source_deltas:
                target_hash = d.after_hash  # None => the event deleted this file
                target_mode = d.after_mode

                # We must have the blob to reproduce the change; fail loudly and
                # name the file if it was pruned away.
                if target_hash is not None and not store.has(target_hash):
                    raise X.OpsError(
                        f"blob for {d.path} is missing from the object store; "
                        "cannot cherry-pick safely (run `chronx fsck`)"
                    )

                full = root_path / d.path
                cur_bytes, cur_st = _current_state(full)
                cur_hash = X.hash_bytes(cur_bytes) if cur_bytes is not None else None
                cur_size = len(cur_bytes) if cur_bytes is not None else None
                cur_mode = cur_st.st_mode if cur_st is not None else None

                if cur_hash == target_hash:
                    continue  # already in the target state — nothing to do

                plan.append(_Change(
                    rel=d.path,
                    target_hash=target_hash,
                    target_mode=target_mode,
                    cur_bytes=cur_bytes,
                    cur_hash=cur_hash,
                    cur_size=cur_size,
                    cur_mode=cur_mode,
                ))

            if not plan:
                raise X.OpsError("nothing to apply (already up to date)")

            # 5. Summarize and (unless --yes) confirm before touching disk.
            X.click.secho(
                f"cherry-pick event #{source_id} "
                f"($ {X.describe_command(row)}) onto the working tree:",
                bold=True,
            )
            for ch in plan:
                action = "delete" if ch.target_hash is None else "apply"
                X.click.echo(f"  {action:>6}  {ch.rel}")
            if not yes:
                if not X.click.confirm(
                    "Apply this cherry-pick?", default=False
                ):
                    raise X.click.Abort()

            # 6. Apply. Snapshot current content first (reversibility), then
            #    write/delete, collecting the deltas this cherry-pick causes.
            applied: list[X.dbm.Delta] = []
            manifest_updates: dict[str, X.dbm.ManifestEntry] = {}
            manifest_deletes: set[str] = set()
            for ch in plan:
                full = root_path / ch.rel
                if ch.cur_bytes is not None:
                    store.put_bytes(ch.cur_bytes)  # safety snapshot of what we replace

                if ch.target_hash is None:
                    # The source event deleted this file — delete it here too.
                    try:
                        full.unlink(missing_ok=True)
                    except OSError as exc:
                        raise X.click.ClickException(
                            f"could not remove {ch.rel}: {exc}"
                        ) from exc
                    if ch.cur_hash is not None:
                        applied.append(X.dbm.Delta(
                            ch.rel, "D", ch.cur_hash, None,
                            ch.cur_size, None, ch.cur_mode, None,
                        ))
                    manifest_deletes.add(ch.rel)
                else:
                    blob = store.get(ch.target_hash)
                    try:
                        _write_atomic(full, blob, ch.target_mode)
                    except OSError as exc:
                        raise X.click.ClickException(
                            f"could not write {ch.rel}: {exc}"
                        ) from exc
                    new_st = os.lstat(full)
                    change = "M" if ch.cur_hash is not None else "A"
                    applied.append(X.dbm.Delta(
                        ch.rel, change, ch.cur_hash, ch.target_hash,
                        ch.cur_size, len(blob), ch.cur_mode, new_st.st_mode,
                    ))
                    manifest_updates[ch.rel] = X.dbm.ManifestEntry(
                        hash=ch.target_hash, size=len(blob),
                        mtime=new_st.st_mtime, mode=new_st.st_mode,
                    )

            # 7. Guard: nothing actually changed on disk (defensive; the plan
            #    already excluded already-satisfied files).
            if not applied:
                raise X.OpsError("nothing to apply (already up to date)")

            # 8. Record the cherry-pick as its own event on the active timeline,
            #    which is the handle `chronx undo` reverts.
            now = time.time()
            new_id = X.dbm.record_event(
                conn,
                session="chronx",
                root_id=int(root["id"]),
                cwd=str(root_path),
                command=f"chronx cherry-pick #{source_id}",
                started_at=now,
                finished_at=now,
                exit_code=0,
                deltas=applied,
                manifest_updates=manifest_updates,
                manifest_deletes=manifest_deletes,
            )

            # 9. Nudge a (re)started daemon to resync so it does not re-record
            #    our own writes as a separate external change.
            send_line(X.paths().fifo, encode_sync(str(root_path)))

            # 10. Report.
            X.click.secho(
                f"cherry-picked event #{source_id}: "
                f"{len(applied)} file(s) changed", fg="green",
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
