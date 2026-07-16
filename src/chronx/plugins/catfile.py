"""chronx cat-file — inspect a raw blob in the content-addressed store.

A low-level store inspector in the spirit of ``git cat-file``: given either a
blob digest or a ``<moment>:<path>`` reference, dump the exact recorded bytes,
report the blob's size, classify it as text/binary, or verify its integrity.

``<ref>`` is EITHER:
  (a) a blob digest — the full blake3 hex, or a unique prefix of >= 6 hex chars,
      resolved by scanning the object store for exactly one match; OR
  (b) a ``<moment>:<path>`` ref (e.g. ``HEAD:src/app.py``, ``mark1:conf.py``,
      ``10m:a.txt``, ``#5:a.txt``) — the moment picks a point in recorded
      history and PATH (relative to the tracked root) selects the file whose
      content at that moment we want.

Read-only over the store: it opens the database read-only and only ever reads
blobs; it never touches the working tree and never crashes on a missing or
corrupt blob (those surface as clean errors, or as ``CORRUPT``/``missing`` under
``--check``).

Imports only ``chronx.pluginlib`` (as X) plus the stdlib, so it stays decoupled
from ``cli.py`` and is auto-discovered from the filesystem (no reinstall).
"""

from __future__ import annotations

import os
import sys
import time

from chronx import pluginlib as X

# Shortest digest prefix we accept: fewer hex chars is too ambiguous to be a
# meaningful reference into the store.
_MIN_PREFIX = 6
# Bytes sniffed for a NUL when classifying a blob as text vs binary (matches the
# heuristic used by chronx's other content-reading plugins, e.g. grep/annotate).
_BINARY_SNIFF = 8192
# How many candidate digests to list when a prefix is ambiguous.
_MAX_LISTED = 10


def _is_hex(s: str) -> bool:
    """True if ``s`` is a non-empty run of lowercase hex digits."""
    return bool(s) and all(c in "0123456789abcdef" for c in s)


def _norm_relpath(path: str) -> str:
    """Normalise the PATH part of a ``<moment>:<path>`` ref to a root-relative,
    forward-slash key, as stored in the manifest (i.e. a ``state_at`` key)."""
    p = path.replace(os.sep, "/").replace("\\", "/")
    # Drop a leading './' and any leading slashes so it reads as root-relative.
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _resolve_moment_ts(conn: "X.sqlite3.Connection", moment: str) -> float:
    """Epoch for a moment spec, treating 'HEAD'/'now'/'' as *now*.

    Everything else (marks, ``#event`` ids, times, relatives) is delegated to
    the shared resolver, which raises a clean ClickException on a bad spec.
    """
    m = moment.strip()
    if m in ("", "HEAD", "head", "now"):
        return time.time()
    return X.moment_ts(conn, m)


def _digest_from_moment_path(
    conn: "X.sqlite3.Connection", ref: str
) -> "tuple[str, str, str]":
    """Resolve a ``<moment>:<path>`` ref to ``(digest, moment, path)``.

    Needs the cwd to be inside a tracked root (to scope ``state_at``). Raises a
    clean ClickException if the path was not a recorded file at that moment.
    """
    # Split on the LAST colon so time specs like '14:32' (and ISO stamps) stay
    # intact — real file paths do not contain colons.
    moment, path = ref.rsplit(":", 1)
    if not path.strip():
        raise X.click.ClickException(
            f"no file path in ref {ref!r} (expected <moment>:<path>)"
        )
    rel = _norm_relpath(path)

    root = X.root_for_cwd(conn)  # clean ClickException if cwd is untracked
    root_id = int(root["id"])
    ts = _resolve_moment_ts(conn, moment)

    state = X.state_at(conn, root_id, ts)  # relpath -> (hash|None, mode|None)
    entry = state.get(rel)
    if entry is None or entry[0] is None:
        raise X.click.ClickException(
            f"{rel} was not present at {moment!r} ({X.fmt_ts(ts)}) — "
            "not a recorded file at that moment on this timeline"
        )
    return entry[0], moment, path


def _resolve_digest_prefix(store: "X.ObjectStore", ref: str) -> str:
    """Resolve a full digest or a unique >= 6-hex-char prefix to a full digest.

    Raises a clean ClickException if the ref is not hex, is too short, or matches
    zero / multiple blobs (a few ambiguous matches are listed).
    """
    prefix = ref.strip().lower()
    if not _is_hex(prefix):
        raise X.click.ClickException(
            f"{ref!r} is neither a hex blob digest nor a <moment>:<path> ref"
        )
    # A full digest already on disk needs no scan.
    if store.has(prefix):
        return prefix
    if len(prefix) < _MIN_PREFIX:
        raise X.click.ClickException(
            f"digest prefix {prefix!r} is too short "
            f"(need at least {_MIN_PREFIX} hex chars)"
        )
    matches = [d for (d, _p, _s) in store.iter_blobs() if d.startswith(prefix)]
    if not matches:
        raise X.click.ClickException(f"no blob matches digest prefix {prefix!r}")
    if len(matches) > 1:
        shown = "\n".join(f"  {d[:16]}…" for d in sorted(matches)[:_MAX_LISTED])
        extra = len(matches) - _MAX_LISTED
        more = "" if extra <= 0 else f"\n  … and {extra} more"
        raise X.click.ClickException(
            f"ambiguous digest prefix {prefix!r} matches {len(matches)} blobs:\n"
            f"{shown}{more}"
        )
    return matches[0]


def _load_blob(store: "X.ObjectStore", digest: str) -> bytes:
    """Raw bytes for a digest, mapping store errors to clean ClickExceptions."""
    try:
        return store.get(digest)
    except KeyError:
        raise X.click.ClickException(
            f"blob {digest} is missing from the object store (run `chronx fsck`)"
        )
    except ValueError:
        raise X.click.ClickException(
            f"blob {digest} is corrupt (failed to decompress)"
        )


def _do_check(store: "X.ObjectStore", digest: str) -> None:
    """Print ``ok`` / ``CORRUPT`` / ``missing`` for a digest; exit non-zero on
    anything but ``ok``.

    A blob is corrupt if it fails to decompress OR if the bytes it decompresses
    to no longer hash back to their own address (silent bit-rot).
    """
    try:
        raw = store.get(digest)
    except KeyError:
        X.click.echo(f"missing {digest}")
        raise SystemExit(1)
    except ValueError:
        X.click.echo(f"CORRUPT {digest}")
        raise SystemExit(1)
    if X.hash_bytes(raw) == digest:
        X.click.echo(f"ok {digest}")
    else:
        X.click.echo(f"CORRUPT {digest}")
        raise SystemExit(1)


def register(main) -> None:  # type: ignore[no-untyped-def]
    @main.command("cat-file")
    @X.click.argument("ref")
    @X.click.option(
        "-t", "--type", "show_type", is_flag=True,
        help="Print whether the blob is text or binary, not its content.",
    )
    @X.click.option(
        "-s", "--size", "show_size", is_flag=True,
        help="Print the blob's logical size in bytes, not its content.",
    )
    @X.click.option(
        "--check", is_flag=True,
        help="Verify the blob decompresses and re-hashes to its digest.",
    )
    def cat_file(ref: str, show_type: bool, show_size: bool, check: bool) -> None:
        """Inspect a raw blob in the content-addressed store.

        REF is a blob digest (full or a unique prefix of >= 6 hex chars), or a
        <moment>:<path> ref such as HEAD:src/app.py, mark1:conf.py, 10m:a.txt,
        or #5:a.txt. With no option the blob's RAW bytes are written to stdout.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # A colon means the <moment>:<path> form; a bare token is a digest.
            # (Blob digests are pure hex, so they never contain a ':'.)
            if ":" in ref:
                digest, moment, path = _digest_from_moment_path(conn, ref)
                # Provenance header on stderr keeps stdout pure for the content.
                X.click.echo(f"# {path} @ {moment} -> {digest}", err=True)
            else:
                digest = _resolve_digest_prefix(store, ref)
        finally:
            # Resolution is all the DB is needed for; the rest is blob-only.
            conn.close()

        # Mode precedence: --check, then --type, then --size, else dump content.
        if check:
            _do_check(store, digest)
            return
        if show_type:
            raw = _load_blob(store, digest)
            kind = "binary" if b"\x00" in raw[:_BINARY_SNIFF] else "text"
            X.click.echo(f"{kind} {digest}")
            return
        if show_size:
            raw = _load_blob(store, digest)
            X.click.echo(str(len(raw)))
            return
        # Default: write the exact recorded bytes to stdout (binary-safe).
        raw = _load_blob(store, digest)
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
