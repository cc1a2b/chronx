"""Content-addressed blob store (git-style) under ~/.chronx/objects.

Blobs are keyed by the blake3 hash of their raw content and stored
zlib-compressed at objects/<first 2 hex chars>/<remaining hex>. Identical
content is stored exactly once, so unchanged files never cost anything
no matter how many snapshots reference them.
"""

from __future__ import annotations

import os
import tempfile
import zlib
from pathlib import Path
from typing import Callable, Iterator

try:
    from blake3 import blake3 as _hasher

    HASH_ALGO = "blake3"
except ImportError:  # pragma: no cover - blake3 is a hard dep, but stay usable
    from hashlib import sha256 as _hasher  # type: ignore[assignment]

    HASH_ALGO = "sha256"

_new_hash: Callable = _hasher


def hash_bytes(data: bytes) -> str:
    return _new_hash(data).hexdigest()


class ObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def has(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def path_for(self, digest: str) -> Path:
        """On-disk location of a blob (may not exist)."""
        return self._path(digest)

    def put_bytes(self, data: bytes) -> str:
        """Store raw content; return its digest. No-op if already present."""
        digest = hash_bytes(data)
        target = self._path(digest)
        if target.is_file():
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(zlib.compress(data, 1))
            os.chmod(tmp, 0o400)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return digest

    def put_file(self, path: Path) -> tuple[str, int] | None:
        """Store a file's content; return (digest, size), or None if unreadable."""
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return self.put_bytes(data), len(data)

    def get(self, digest: str) -> bytes:
        """Return the raw content for a digest.

        Raises KeyError if the blob is missing, ValueError if corrupt.
        """
        try:
            packed = self._path(digest).read_bytes()
        except FileNotFoundError:
            raise KeyError(digest) from None
        try:
            return zlib.decompress(packed)
        except zlib.error as exc:
            raise ValueError(f"corrupt object {digest}: {exc}") from exc

    def count(self) -> int:
        return sum(1 for _ in self.iter_blobs())

    def iter_blobs(self) -> "Iterator[tuple[str, Path, int]]":
        """Yield (digest, path, stored_size) for every blob on disk."""
        try:
            shards = sorted(os.scandir(self.root), key=lambda e: e.name)
        except FileNotFoundError:
            return
        for shard in shards:
            if not shard.is_dir(follow_symlinks=False) or len(shard.name) != 2:
                continue
            with os.scandir(shard.path) as it:
                for entry in it:
                    if entry.name[:1] == "." or not entry.is_file(follow_symlinks=False):
                        continue
                    yield (
                        shard.name + entry.name,
                        Path(entry.path),
                        entry.stat().st_size,
                    )

    def disk_usage(self) -> tuple[int, int]:
        """(blob count, total stored bytes)."""
        n = total = 0
        for _digest, _path, size in self.iter_blobs():
            n += 1
            total += size
        return n, total

    def delete(self, digest: str) -> int:
        """Remove a blob; return the bytes freed (0 if it wasn't there)."""
        path = self._path(digest)
        try:
            size = path.stat().st_size
            path.chmod(0o600)  # blobs are stored read-only
            path.unlink()
            return size
        except OSError:
            return 0
