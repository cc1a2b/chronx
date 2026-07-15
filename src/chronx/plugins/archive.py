"""chronx archive — export the working tree at any recorded point as a
portable tar/zip, à la ``git archive`` but across time.

Reconstructs the FULL tree state on the cwd's active timeline at a mark,
event, moment, or branch tip and packs every recorded file into a single
archive that anyone can open with standard ``tar``/``unzip`` — no chronx
install required. The object store and database are only ever read; the
sole thing written is the requested archive file.
"""

from __future__ import annotations

import io
import os  # noqa: F401  (part of the documented plugin stdlib surface)
import stat as statmod
import tarfile
import time
import zipfile

from chronx import pluginlib as X

# Permission bits used when a recorded file has no captured mode.
_FALLBACK_MODE = 0o644
# The zip format cannot encode timestamps earlier than this year.
_ZIP_MIN_YEAR = 1980


def _pick_format(output: "X.Path", explicit: str | None) -> str:
    """Resolve the archive format to one of ``tar.gz``, ``tar`` or ``zip``.

    An explicit ``--format`` wins (``tgz`` is treated as gzip tar); otherwise
    the ``-o`` filename extension decides (``.zip`` / ``.tar`` / ``.tar.gz`` /
    ``.tgz``), defaulting to gzip-compressed tar.
    """
    if explicit is not None:
        return "tar.gz" if explicit == "tgz" else explicit
    name = output.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar.gz"
    if name.endswith(".tar"):
        return "tar"
    return "tar.gz"


def _arcname(prefix: str | None, rel: str) -> str:
    """Path an entry gets inside the archive (``<prefix>/<rel>`` or ``<rel>``)."""
    return f"{prefix}/{rel}" if prefix else rel


def _resolve_state(
    conn: "X.sqlite3.Connection", root_id: int, ref: str
) -> tuple[dict[str, tuple[str | None, int | None]], str, float]:
    """Return ``(tree_state, human_desc, mtime_epoch)`` for REF on this root.

    REF is first tried as a branch name (→ that timeline's tip); otherwise it
    is a mark / event id / moment resolved on the active timeline. An unknown
    ref surfaces as a clean ``ClickException`` from ``moment_ts``.
    """
    branch = X.dbm.branch_by_name(conn, root_id, ref)
    if branch is not None:
        state = X.branch_state_at(conn, branch, time.time())
        # A reproducible mtime for a "tip" archive: the branch's latest event,
        # falling back to its fork point when the branch has no events yet.
        tip = X.dbm.last_event(conn, root_id=root_id, branch_id=int(branch["id"]))
        ts = float(tip["started_at"]) if tip is not None else float(branch["base_ts"])
        return state, f"timeline {ref!r} (tip @ {X.fmt_ts(ts)})", ts
    ts = X.moment_ts(conn, ref)  # raises ClickException on an unknown ref
    return X.state_at(conn, root_id, ts), f"{ref!r} @ {X.fmt_ts(ts)}", ts


def _write_tar(
    output: "X.Path",
    present: list[tuple[str, tuple[str, int | None]]],
    store: "X.ObjectStore",
    ts: float,
    prefix: str | None,
    *,
    gzip_it: bool,
) -> None:
    """Write PRESENT files into a (optionally gzip) tar with fixed metadata.

    Entry mtimes are pinned to the ref's integer timestamp and ownership is
    zeroed, so the logical archive is reproducible regardless of who or where
    it is produced. Parent directories are recreated implicitly on extraction.
    """
    mtime = int(ts)
    with tarfile.open(output, "w:gz" if gzip_it else "w") as tf:
        for rel, (digest, fmode) in present:
            data = store.get(digest)
            info = tarfile.TarInfo(_arcname(prefix, rel))
            info.size = len(data)
            info.mtime = mtime
            info.mode = statmod.S_IMODE(fmode) if fmode is not None else _FALLBACK_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))


def _write_zip(
    output: "X.Path",
    present: list[tuple[str, tuple[str, int | None]]],
    store: "X.ObjectStore",
    ts: float,
    prefix: str | None,
) -> None:
    """Write PRESENT files into a deflate zip, preserving unix permissions."""
    lt = time.localtime(int(ts))
    date_time = (
        (_ZIP_MIN_YEAR, 1, 1, 0, 0, 0) if lt.tm_year < _ZIP_MIN_YEAR else lt[:6]
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, (digest, fmode) in present:
            data = store.get(digest)
            info = zipfile.ZipInfo(_arcname(prefix, rel), date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            perm = statmod.S_IMODE(fmode) if fmode is not None else _FALLBACK_MODE
            # High 16 bits of external_attr carry the unix mode (flagged as a
            # regular file) so ``unzip`` restores executable/permission bits.
            info.external_attr = (statmod.S_IFREG | perm) << 16
            zf.writestr(info, data)


def register(main) -> None:
    @main.command()
    @X.click.argument("ref")
    @X.click.option(
        "--output", "-o", required=True,
        type=X.click.Path(path_type=X.Path),
        help="Archive file to write (format inferred from its extension).",
    )
    @X.click.option(
        "--prefix", default=None,
        help="Path prefix (a clean top-level dir) for every entry. "
             "Default: none — files sit at the archive root.",
    )
    @X.click.option(
        "--format", "fmt", default=None,
        type=X.click.Choice(["tar.gz", "tgz", "tar", "zip"]),
        help="Override the format inferred from -o (default: infer, else tar.gz).",
    )
    def archive(ref: str, output, prefix, fmt) -> None:  # type: ignore[no-untyped-def]
        """Export the working tree at REF as a portable tar/zip archive.

        REF is a branch name (its tip), a mark, an event id, or a moment
        ('10m', '14:32', ISO...). The archive reconstructs the complete
        recorded tree on the cwd's active timeline and opens with standard
        ``tar``/``unzip`` — no chronx needed. Read-only over the store.
        """
        conn = X.open_db()  # read-only; ClickException if there is no store
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            state, desc, ts = _resolve_state(conn, int(root["id"]), ref)

            # Every present file must have its blob on disk, or the archive
            # would be silently incomplete.
            missing = [
                rel for rel, (h, _m) in state.items()
                if h is not None and not store.has(h)
            ]
            if missing:
                raise X.click.ClickException(
                    f"{len(missing)} file(s) have missing blobs "
                    f"(first: {missing[0]}); run `chronx fsck`"
                )

            present = [
                (rel, (h, m))
                for rel, (h, m) in sorted(state.items())
                if h is not None
            ]
            chosen = _pick_format(output, fmt)
            norm_prefix = (prefix.strip("/") or None) if prefix else None
        finally:
            conn.close()  # the write below only needs the object store

        try:
            if chosen == "zip":
                _write_zip(output, present, store, ts, norm_prefix)
            else:
                _write_tar(
                    output, present, store, ts, norm_prefix,
                    gzip_it=(chosen == "tar.gz"),
                )
        except (KeyError, ValueError, OSError) as exc:
            # Never leave a half-written/corrupt archive behind.
            try:
                output.unlink()
            except OSError:
                pass
            raise X.click.ClickException(f"could not write archive: {exc}") from exc

        size = output.stat().st_size
        X.click.secho(f"archived {desc} → {output}", fg="green")
        X.click.echo(
            f"  {len(present)} file(s), {chosen} format, {X.human_bytes(size)}"
        )
        if not present:
            X.click.secho(
                "  (nothing recorded at this point — wrote a valid empty archive)",
                dim=True,
            )
