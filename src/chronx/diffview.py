"""Rendering deltas as unified diffs (shared by the CLI and the TUI)."""

from __future__ import annotations

import difflib
from pathlib import Path

from .db import Delta
from .snapshot import WorkingChange
from .store import ObjectStore

MAX_DIFF_LINES = 400
_BINARY_SNIFF = 8192


def _load(store: ObjectStore, digest: str | None) -> bytes | None:
    if digest is None:
        return None
    try:
        return store.get(digest)
    except (KeyError, ValueError):
        return None


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def _describe_side(digest: str | None, data: bytes | None) -> str:
    if digest is None:
        return "(absent)"
    if data is None:
        return f"(blob {digest[:12]} missing)"
    return f"{len(data)} bytes"


def change_word(change: str) -> str:
    return {"A": "added", "M": "modified", "D": "deleted"}[change]


def render_delta(
    store: ObjectStore, delta: Delta, *, max_lines: int = MAX_DIFF_LINES
) -> list[str]:
    """Unified-diff lines for one file delta.

    Lines use standard prefixes (---/+++/@@/+/-/space) so callers can
    colorize them however they like.
    """
    before = _load(store, delta.before_hash)
    after = _load(store, delta.after_hash)

    header = [
        f"--- a/{delta.path}" if delta.change != "A" else "--- /dev/null",
        f"+++ b/{delta.path}" if delta.change != "D" else "+++ /dev/null",
    ]

    if delta.change == "M" and delta.after_hash is None:
        return header + [
            f"@@ {delta.path}: grew past the snapshot size cap; content no longer tracked "
            f"({delta.before_size} -> {delta.after_size} bytes) @@"
        ]

    if (delta.before_hash and before is None) or (delta.after_hash and after is None):
        return header + [
            f"@@ blob unavailable: {_describe_side(delta.before_hash, before)} -> "
            f"{_describe_side(delta.after_hash, after)} @@"
        ]

    if (before and _is_binary(before)) or (after and _is_binary(after)):
        return header + [
            f"@@ binary file: {_describe_side(delta.before_hash, before)} -> "
            f"{_describe_side(delta.after_hash, after)} @@"
        ]

    try:
        before_lines = (before or b"").decode("utf-8").splitlines(keepends=True)
        after_lines = (after or b"").decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return header + [
            f"@@ binary (non-utf8) file: {_describe_side(delta.before_hash, before)} -> "
            f"{_describe_side(delta.after_hash, after)} @@"
        ]

    body: list[str] = []
    for line in difflib.unified_diff(
        before_lines, after_lines, fromfile="", tofile="", n=3, lineterm=""
    ):
        if line.startswith(("---", "+++")):
            continue
        body.append(line.rstrip("\n"))
        if len(body) >= max_lines:
            body.append(f"@@ ... diff truncated at {max_lines} lines @@")
            break
    if not body:
        body = ["@@ content unchanged (metadata-only) @@"]
    return header + body


def render_working_change(
    store: ObjectStore, root: Path, change: WorkingChange,
    *, max_lines: int = MAX_DIFF_LINES,
) -> list[str]:
    """Unified diff for a live drift: before from the store, after read from disk."""
    after: bytes | None = None
    if change.change != "D":
        try:
            after = (root / change.rel).read_bytes()
        except OSError:
            after = None
    delta = Delta(
        change.rel, change.change, change.before_hash,
        None if after is None else "live", change.before_size, change.after_size,
        None, None,
    )
    # Reuse the core renderer for headers/binary handling by feeding live bytes.
    before = _load(store, change.before_hash)
    header = [
        f"--- a/{change.rel}" if change.change != "A" else "--- /dev/null",
        f"+++ b/{change.rel}" if change.change != "D" else "+++ /dev/null",
    ]
    if change.change != "A" and before is None:
        return header + [f"@@ base blob {change.before_hash and change.before_hash[:12]} "
                         "unavailable @@"]
    if (before and _is_binary(before)) or (after and _is_binary(after)):
        return header + ["@@ binary file @@"]
    try:
        before_lines = (before or b"").decode("utf-8").splitlines(keepends=True)
        after_lines = (after or b"").decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return header + ["@@ binary (non-utf8) file @@"]
    body: list[str] = []
    for line in difflib.unified_diff(before_lines, after_lines, n=3, lineterm=""):
        if line.startswith(("---", "+++")):
            continue
        body.append(line.rstrip("\n"))
        if len(body) >= max_lines:
            body.append(f"@@ ... diff truncated at {max_lines} lines @@")
            break
    _ = delta
    return header + (body or ["@@ no textual change @@"])


def working_stat_line(change: WorkingChange) -> str:
    if change.change == "A":
        detail = f"(+{change.after_size or 0} bytes, unrecorded)"
    elif change.change == "D":
        detail = f"(-{change.before_size or 0} bytes, still on record)"
    else:
        detail = f"({change.before_size or 0} -> {change.after_size or '?'} bytes)"
    return f"{change.change}  {change.rel}  {detail}"


def stat_line(delta: Delta) -> str:
    """One-line summary, e.g. 'M  src/app.py  (312 -> 340 bytes)'."""
    if delta.change == "A":
        detail = f"(+{delta.after_size or 0} bytes)"
    elif delta.change == "D":
        detail = f"(-{delta.before_size or 0} bytes)"
    else:
        detail = f"({delta.before_size or 0} -> {delta.after_size or '?'} bytes)"
    return f"{delta.change}  {delta.path}  {detail}"
