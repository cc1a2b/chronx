"""Scanning working trees and turning dirty paths into content-addressed deltas."""

from __future__ import annotations

import os
import stat as statmod
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator

from .config import Config
from .db import Delta, ManifestEntry
from .store import ObjectStore


def is_ignored_rel(rel: str, cfg: Config) -> bool:
    parts = rel.split("/")
    if any(part in cfg.ignore_dirs for part in parts):
        return True
    base = parts[-1]
    return any(fnmatch(base, pat) for pat in cfg.ignore_globs)


def iter_files(root: Path, cfg: Config, sub: str = "") -> Iterator[tuple[str, os.stat_result]]:
    """Yield (rel_path, lstat) for every trackable regular file under root/sub.

    Prunes ignored directories, skips symlinks, special files, and files
    over the size cap. `rel_path` is always relative to `root` with '/'
    separators.
    """
    start = root / sub if sub else root
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in cfg.ignore_dirs)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if is_ignored_rel(rel, cfg):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            if st.st_size > cfg.max_file_size:
                continue
            yield rel, st


def scan_root(
    root: Path, store: ObjectStore, cfg: Config
) -> dict[str, ManifestEntry] | None:
    """Baseline scan: hash and store every trackable file.

    Returns the manifest, or None if the tree exceeds cfg.max_files
    (in which case nothing was persisted and the root should not be tracked).
    """
    manifest: dict[str, ManifestEntry] = {}
    for rel, st in iter_files(root, cfg):
        if len(manifest) >= cfg.max_files:
            return None
        stored = store.put_file(root / rel)
        if stored is None:
            continue
        digest, size = stored
        manifest[rel] = ManifestEntry(
            hash=digest, size=size, mtime=st.st_mtime, mode=st.st_mode
        )
    return manifest


def _expand_candidates(
    root: Path,
    candidates: set[str] | None,
    manifest: dict[str, ManifestEntry],
    cfg: Config,
) -> set[str]:
    """Resolve raw candidate rel-paths into concrete file rel-paths to examine.

    Directory candidates expand to the files under them; vanished candidates
    expand to every manifest entry they prefixed (a deleted/renamed directory).
    None means a full sweep: everything in the manifest plus everything on disk.
    """
    if candidates is None:
        expanded = set(manifest)
        expanded.update(rel for rel, _ in iter_files(root, cfg))
        return expanded

    expanded: set[str] = set()
    for rel in candidates:
        rel = rel.strip("/")
        if not rel or is_ignored_rel(rel, cfg):
            continue
        full = root / rel
        try:
            st = os.lstat(full)
        except OSError:
            st = None
        if st is not None and statmod.S_ISDIR(st.st_mode):
            expanded.update(r for r, _ in iter_files(root, cfg, sub=rel))
            prefix = rel + "/"
            expanded.update(k for k in manifest if k.startswith(prefix))
        elif st is None:
            expanded.add(rel)
            prefix = rel + "/"
            expanded.update(k for k in manifest if k.startswith(prefix))
        else:
            expanded.add(rel)
    return expanded


def compute_deltas(
    root: Path,
    candidates: set[str] | None,
    manifest: dict[str, ManifestEntry],
    store: ObjectStore,
    cfg: Config,
) -> tuple[list[Delta], dict[str, ManifestEntry], set[str]]:
    """Compare candidate paths against the manifest.

    Stores new blob content for added/modified files and returns
    (deltas, manifest_updates, manifest_deletes). The caller persists both
    the deltas (as an event) and the manifest mutations atomically.
    """
    deltas: list[Delta] = []
    updates: dict[str, ManifestEntry] = {}
    deletes: set[str] = set()
    full_sweep = candidates is None

    for rel in sorted(_expand_candidates(root, candidates, manifest, cfg)):
        entry = manifest.get(rel)
        full = root / rel
        try:
            st = os.lstat(full)
        except OSError:
            st = None
        is_reg = st is not None and statmod.S_ISREG(st.st_mode)

        if is_reg and st.st_size <= cfg.max_file_size:
            assert st is not None
            if (
                full_sweep
                and entry is not None
                and entry.size == st.st_size
                and entry.mtime == st.st_mtime
            ):
                continue  # cheap unchanged check for offline sweeps
            stored = store.put_file(full)
            if stored is None:
                continue  # vanished or unreadable mid-scan; leave manifest alone
            digest, size = stored
            new_entry = ManifestEntry(
                hash=digest, size=size, mtime=st.st_mtime, mode=st.st_mode
            )
            if entry is None:
                deltas.append(
                    Delta(rel, "A", None, digest, None, size, None, st.st_mode)
                )
                updates[rel] = new_entry
            elif digest != entry.hash:
                deltas.append(
                    Delta(
                        rel, "M", entry.hash, digest,
                        entry.size, size, entry.mode, st.st_mode,
                    )
                )
                updates[rel] = new_entry
            elif entry.mtime != st.st_mtime or entry.mode != st.st_mode:
                updates[rel] = new_entry  # metadata refresh, not a content delta
        elif is_reg and entry is not None:
            # Grew past the size cap: content no longer captured; stop tracking.
            assert st is not None
            deltas.append(
                Delta(rel, "M", entry.hash, None, entry.size, st.st_size, entry.mode, None)
            )
            deletes.add(rel)
        elif not is_reg and entry is not None:
            # Deleted (or replaced by a symlink/dir/special file).
            deltas.append(
                Delta(rel, "D", entry.hash, None, entry.size, None, entry.mode, None)
            )
            deletes.add(rel)

    return deltas, updates, deletes
