"""``chronx from-git`` — import a git repository's history into chronx.

The inverse of ``chronx to-git``: instead of replaying recorded history into
a git repo, this reads an existing git repo and lands its commits in the local
chronx store, one git commit → one chronx event. Afterwards the repo's history
is a first-class chronx timeline you can ``log`` / ``blame`` / ``diff`` /
``checkout`` / ``to-git`` like anything chronx recorded itself.

Design — reuse the VERIFIED import path
---------------------------------------
Rather than hand-writing database rows (fragile, and easy to desync from the
schema), this plugin parses the git repo into the exact gzip-tar *export
archive* that :func:`chronx.transfer.import_archive` already knows how to
consume, then hands that archive to ``import_archive``. All integrity checks,
baseline derivation, branch/timeline setup and blob dedup are therefore done by
the same code that powers ``chronx export``/``import``/``pull``.

The mapping is:

* The **first** commit's full tree becomes the root *baseline* (via the
  archive's top-level manifest reverse-applied through the deltas — exactly how
  a normal export encodes "state before the first event").
* Every **subsequent** commit becomes one event whose deltas are the
  path-level diff of that commit's tree against the previous commit's tree
  (added → ``A``, removed → ``D``, content-changed → ``M``).
* The top-level manifest is the **latest** commit's tree (the final state).
* Every blob referenced by any commit tree is packed once (deduped by digest).

Only the git repo is read; the sole writer is ``import_archive``, so the daemon
must be stopped first (enforced up front).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import tempfile
import time

from chronx import pluginlib as X
from chronx.store import HASH_ALGO
from chronx.transfer import FORMAT, META_NAME, TransferError, import_archive

# git tree entry modes we cannot represent as plain content blobs.
_GIT_SUBMODULE = "160000"
_GIT_SYMLINK = "120000"

# Marker stored on each imported event so the origin is obvious in `chronx log`.
_SESSION = "git"


# --------------------------------------------------------------------- git I/O


class _GitError(Exception):
    """Internal: a git invocation failed (surfaced as a clean ClickException)."""


def _git(repo: "X.Path", *args: str) -> bytes:
    """Run ``git -C <repo> <args...>`` and return raw stdout bytes.

    Raises :class:`_GitError` if git is missing or the command exits non-zero,
    carrying git's stderr so the caller can build a friendly message.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
        )
    except FileNotFoundError as exc:  # git not installed / not on PATH
        raise _GitError("git is not installed or not on PATH") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise _GitError(err or f"git {args[0]} failed")
    return proc.stdout


def _is_git_repo(repo: "X.Path") -> bool:
    try:
        _git(repo, "rev-parse", "--git-dir")
        return True
    except _GitError:
        return False


def _has_commits(repo: "X.Path", rev: str) -> bool:
    """True if REV resolves to a commit (i.e. the branch/HEAD is born)."""
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        return True
    except _GitError:
        return False


# ------------------------------------------------------------------ tree model


def _list_commits(repo: "X.Path", rev: str) -> list[tuple[str, float, str]]:
    """Return ``(sha, author_epoch, subject)`` for REV, OLDEST commit first.

    Uses NUL-separated fields so subjects with odd characters stay intact; the
    author identity (``%an <%ae>``) is requested per the documented format but
    is not needed for events, so it is parsed and discarded.
    """
    out = _git(
        repo, "log", "--reverse",
        "--format=%H%x00%an <%ae>%x00%at%x00%s", rev,
    )
    commits: list[tuple[str, float, str]] = []
    for line in out.decode("utf-8", "surrogateescape").split("\n"):
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) < 4:
            continue  # malformed record; skip defensively
        sha, _author, epoch, subject = parts[0], parts[1], parts[2], parts[3]
        try:
            ts = float(int(epoch))
        except ValueError:
            ts = time.time()
        commits.append((sha, ts, subject))
    return commits


class _TreeReader:
    """Fetches commit trees and their blob contents, caching by git blob sha.

    As a side effect it accumulates every distinct blob (keyed by chronx digest)
    into ``blobs`` so the archive packs each unique content exactly once.
    """

    def __init__(self, repo: "X.Path") -> None:
        self._repo = repo
        self._blob_cache: dict[str, bytes] = {}  # git sha -> raw content
        self.blobs: dict[str, bytes] = {}         # chronx digest -> raw content

    def _blob_bytes(self, git_sha: str) -> bytes:
        cached = self._blob_cache.get(git_sha)
        if cached is None:
            cached = _git(self._repo, "cat-file", "blob", git_sha)
            self._blob_cache[git_sha] = cached
        return cached

    def _add_blob(self, raw: bytes) -> tuple[str, int]:
        digest = X.hash_bytes(raw)
        if digest not in self.blobs:
            self.blobs[digest] = raw
        return digest, len(raw)

    def tree(self, commit_sha: str) -> dict[str, tuple[str, int, int]]:
        """Full recursive tree of COMMIT as ``path -> (digest, size, mode)``.

        Submodules and symlinks are skipped (they are not plain content).
        Git octal modes (e.g. ``100644``) become chronx's ``st_mode``-style int.
        """
        out = _git(
            self._repo, "ls-tree", "-r", "-z",
            "--format=%(objectmode) %(objectname) %(path)", commit_sha,
        )
        tree: dict[str, tuple[str, int, int]] = {}
        for record in out.split(b"\x00"):
            if not record:
                continue
            try:
                mode_b, sha_b, path_b = record.split(b" ", 2)
            except ValueError:
                continue  # not an entry line; ignore
            mode_str = mode_b.decode("ascii", "replace")
            if mode_str in (_GIT_SUBMODULE, _GIT_SYMLINK):
                continue
            path = path_b.decode("utf-8", "surrogateescape")
            raw = self._blob_bytes(sha_b.decode("ascii", "replace"))
            digest, size = self._add_blob(raw)
            tree[path] = (digest, size, int(mode_str, 8))
        return tree


def _diff_trees(
    prev: dict[str, tuple[str, int, int]],
    cur: dict[str, tuple[str, int, int]],
) -> list[dict[str, object]]:
    """Path-level diff of two trees → chronx delta dicts (sorted by path).

    A path only in ``cur`` is added (``A``); only in ``prev`` is deleted
    (``D``); present in both with a different digest is modified (``M``).
    """
    deltas: list[dict[str, object]] = []
    for path in sorted(set(prev) | set(cur)):
        before = prev.get(path)
        after = cur.get(path)
        if before is None and after is not None:
            ah, asz, am = after
            deltas.append({
                "path": path, "change": "A",
                "before_hash": None, "after_hash": ah,
                "before_size": None, "after_size": asz,
                "before_mode": None, "after_mode": am,
            })
        elif before is not None and after is None:
            bh, bsz, bm = before
            deltas.append({
                "path": path, "change": "D",
                "before_hash": bh, "after_hash": None,
                "before_size": bsz, "after_size": None,
                "before_mode": bm, "after_mode": None,
            })
        elif before is not None and after is not None and before[0] != after[0]:
            bh, bsz, bm = before
            ah, asz, am = after
            deltas.append({
                "path": path, "change": "M",
                "before_hash": bh, "after_hash": ah,
                "before_size": bsz, "after_size": asz,
                "before_mode": bm, "after_mode": am,
            })
    return deltas


# ------------------------------------------------------------- archive builder


def _build_archive(
    repo: "X.Path", commits: list[tuple[str, float, str]], target: "X.Path"
) -> tuple[bytes, dict[str, bytes], int]:
    """Build the export-archive metadata + blob set from COMMITS.

    Returns ``(meta_json_bytes, blobs, event_count)``. The first commit is the
    baseline; each later commit is one event carrying its tree diff. The
    top-level manifest is the latest commit's tree (final state).
    """
    reader = _TreeReader(repo)
    first_sha, first_ts, _first_subject = commits[0]

    prev_tree = reader.tree(first_sha)   # baseline tree (state before events)
    latest_tree = prev_tree              # updated as we walk forward
    latest_ts = first_ts

    events: list[dict[str, object]] = []
    cwd = str(target)
    for idx, (sha, ts, subject) in enumerate(commits[1:], start=1):
        cur_tree = reader.tree(sha)
        events.append({
            "id": idx,
            "session": _SESSION,
            "cwd": cwd,
            "command": subject,
            "started_at": ts,
            "finished_at": ts,
            "exit_code": 0,
            "deltas": _diff_trees(prev_tree, cur_tree),
        })
        prev_tree = cur_tree
        latest_tree = cur_tree
        latest_ts = ts

    # Top-level manifest == final (latest commit) tree state.
    manifest = [
        {"path": path, "hash": digest, "size": size,
         "mtime": latest_ts, "mode": mode}
        for path, (digest, size, mode) in sorted(latest_tree.items())
    ]

    meta = {
        "format": FORMAT,
        "hash_algo": HASH_ALGO,
        "chronx_version": "from-git",
        "exported_at": time.time(),
        "root": {"path": str(target), "added_at": first_ts},
        "events": events,
        "marks": [],
        "manifest": manifest,
    }
    # ensure_ascii keeps the JSON bytes pure-ASCII, so any surrogate-escaped
    # path is emitted as a \uXXXX escape that import_archive round-trips safely.
    return json.dumps(meta).encode("utf-8"), reader.blobs, len(events)


def _write_archive(meta_bytes: bytes, blobs: dict[str, bytes]) -> "X.Path":
    """Write the gzip-tar archive to a private tempfile and return its path."""
    fd, tmp = tempfile.mkstemp(prefix="chronx-fromgit-", suffix=".tar.gz")
    os.close(fd)
    tmp_path = X.Path(tmp)
    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            info = tarfile.TarInfo(META_NAME)
            info.size = len(meta_bytes)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(meta_bytes))
            for digest, raw in blobs.items():
                info = tarfile.TarInfo(f"blobs/{digest}")
                info.size = len(raw)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(raw))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


# ---------------------------------------------------------------------- command


def register(main) -> None:
    @main.command(name="from-git")
    @X.click.argument(
        "repo", type=X.click.Path(exists=True, file_okay=False, path_type=X.Path)
    )
    @X.click.option(
        "--as", "as_dir", default=None,
        type=X.click.Path(path_type=X.Path),
        help="Root path to record the history under "
             "(default: the repo's own path).",
    )
    @X.click.option(
        "--branch", default=None,
        help="Branch/ref to import (default: current HEAD).",
    )
    @X.click.option(
        "--limit", type=int, default=None,
        help="Import at most N commits (the most recent N, oldest-first).",
    )
    def from_git(repo, as_dir, branch, limit) -> None:  # type: ignore[no-untyped-def]
        """Import a git repository's history into chronx.

        REPO is a path to a git repository. Each commit becomes one chronx
        event (the first commit seeds the baseline), so afterwards you can
        ``chronx log`` / ``blame`` / ``diff`` / ``checkout`` / ``to-git`` the
        imported history. Only the repo is read; nothing in it is modified.
        """
        # A write into the store — must not race the recorder.
        X.require_daemon_stopped("from-git")

        repo = repo.resolve()
        if not _is_git_repo(repo):
            raise X.click.ClickException(f"{repo} is not a git repository")

        rev = branch or "HEAD"
        if not _has_commits(repo, rev):
            if branch:
                raise X.click.ClickException(
                    f"branch {branch!r} not found in {repo} "
                    f"(or it has no commits)"
                )
            X.click.echo(f"{repo} has no commits yet — nothing to import.")
            return

        try:
            commits = _list_commits(repo, rev)
        except _GitError as exc:
            raise X.click.ClickException(str(exc)) from exc
        if not commits:
            X.click.echo(f"{repo} has no commits on {rev} — nothing to import.")
            return

        # --limit: keep the most recent N, but preserve oldest-first ordering.
        if limit is not None and limit >= 0 and len(commits) > limit:
            commits = commits[-limit:] if limit else []
        if not commits:
            X.click.echo("nothing to import (--limit 0).")
            return

        target = (as_dir.resolve() if as_dir else repo)

        try:
            meta_bytes, blobs, event_count = _build_archive(repo, commits, target)
        except _GitError as exc:
            raise X.click.ClickException(str(exc)) from exc

        archive_path = _write_archive(meta_bytes, blobs)
        try:
            stats = import_archive(X.paths(), archive_path, as_path=target)
        except TransferError as exc:
            raise X.click.ClickException(f"import failed: {exc}") from exc
        finally:
            archive_path.unlink(missing_ok=True)

        X.click.secho(
            f"imported {len(commits)} commit(s) as {stats.events} event(s) "
            f"into {stats.root}",
            fg="green",
        )
        X.click.echo(
            f"  {stats.blobs_added} blob(s) added"
            + (f", {stats.blobs_skipped} already present"
               if stats.blobs_skipped else "")
        )
        if event_count == 0:
            X.click.secho(
                "  (single commit — recorded as the baseline, no events)",
                dim=True,
            )
        X.click.echo("  next: chronx log")
