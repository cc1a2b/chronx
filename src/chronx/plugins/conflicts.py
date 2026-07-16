"""chronx conflicts <other> [into] — predict merge conflicts BEFORE merging.

A strictly read-only dry-run of ``chronx merge``'s conflict detection: it asks
"if I merged timeline <other> into <into> (default: the active branch), which
files would clash?" without touching the working tree, the object store, or the
database.

It reconstructs the same three tree states ``chronx merge`` compares —

    base   = common-ancestor state of the two timelines (``merge_base_state``)
    ours   = tip of <into>   (the branch we would merge INTO)
    theirs = tip of <other>  (the branch we would merge FROM)

— and classifies every path exactly the way ``ops.plan_merge`` does, except that
the three-way *content* merge is replaced by a read-only heuristic: instead of
running ``git merge-file`` (which would write merged blobs), it uses
``difflib`` to see whether the two sides edited *overlapping* line-regions of the
common base. Non-overlapping edits are reported as auto-mergeable; overlapping
edits, add/add clashes, edit/delete clashes, and binary changes are reported as
real CONFLICTs.

The command exits 1 when any conflict is predicted (so it is usable as a CI
gate) and 0 when <other> would merge cleanly. It never crashes on an empty
store, an untracked cwd, a single-branch repo, a missing blob, or binary data —
those all surface as clean click errors or graceful "conflict" classifications.
"""

from __future__ import annotations

import difflib
import sqlite3
import time

from chronx import pluginlib as X
from chronx.ops import merge_base_state

# Content larger than this is sniffed for NUL bytes only over the prefix; a NUL
# in the first chunk means "treat as binary, cannot line-merge" (mirrors the
# rest of chronx: diffview, format_patch, ops._three_way_merge).
_BINARY_SNIFF = 8192

# A resolved full tree state: relpath -> (blob hash | None, mode | None).
TreeState = dict


# --------------------------------------------------------------------------- #
# blob helpers
# --------------------------------------------------------------------------- #
def _blob(store: "X.ObjectStore", digest: str | None) -> bytes | None:
    """Raw content for a digest.

    ``None`` digest -> ``b""`` (an absent side reads as empty). A missing or
    corrupt blob -> ``None`` (the caller degrades that to a conservative
    "cannot compare -> conflict"), never a crash.
    """
    if digest is None:
        return b""
    try:
        return store.get(digest)
    except (KeyError, ValueError):
        return None


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def _lines(data: bytes) -> list[str]:
    """Decode a blob into lines (keeping ends) for ``difflib`` alignment."""
    return data.decode("utf-8", "replace").splitlines(keepends=True)


# --------------------------------------------------------------------------- #
# line-region overlap detection (the read-only stand-in for a real 3-way merge)
# --------------------------------------------------------------------------- #
def _changed_regions(
    base_lines: list[str], side_lines: list[str]
) -> tuple[list[tuple[int, int]], set[int]]:
    """How one side rewrote the base, in terms of base line positions.

    Returns ``(spans, inserts)`` where ``spans`` are half-open ``[i1, i2)``
    ranges of base lines that were replaced or deleted, and ``inserts`` are the
    base anchor positions at which pure insertions (``i1 == i2``) were made.
    'equal' opcodes carry no change and are skipped.
    """
    sm = difflib.SequenceMatcher(a=base_lines, b=side_lines, autojunk=False)
    spans: list[tuple[int, int]] = []
    inserts: set[int] = set()
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":  # i1 == i2: content added at anchor i1
            inserts.add(i1)
        else:  # 'replace' / 'delete': base region [i1, i2) is rewritten
            spans.append((i1, i2))
    return spans, inserts


def _regions_overlap(
    base_lines: list[str], ours_lines: list[str], theirs_lines: list[str]
) -> bool:
    """True if ours and theirs edited overlapping regions of the common base.

    A pragmatic read-only proxy for "``git merge-file`` would report a
    conflict": disjoint edits merge automatically, colliding edits do not.
    Collision is any of — both sides touched the same base line; both inserted
    at the same anchor (competing insertions); or one side inserted strictly
    inside a region the other side replaced.
    """
    o_spans, o_ins = _changed_regions(base_lines, ours_lines)
    t_spans, t_ins = _changed_regions(base_lines, theirs_lines)

    o_touched: set[int] = set()
    for i1, i2 in o_spans:
        o_touched.update(range(i1, i2))
    t_touched: set[int] = set()
    for i1, i2 in t_spans:
        t_touched.update(range(i1, i2))
    if o_touched & t_touched:
        return True  # both edited/deleted the same base line

    if o_ins & t_ins:
        return True  # competing insertions at the same point

    # An insertion landing inside the other side's replaced span collides.
    for point in o_ins:
        if any(i1 < point < i2 for i1, i2 in t_spans):
            return True
    for point in t_ins:
        if any(i1 < point < i2 for i1, i2 in o_spans):
            return True
    return False


# --------------------------------------------------------------------------- #
# per-path classification (mirrors ops.plan_merge)
# --------------------------------------------------------------------------- #
# Human-readable explanation for each conflict reason code.
_WHY = {
    "overlapping": "overlapping edits to the same region",
    "add-vs-add": "both timelines created it with different content",
    "edit-vs-delete": "one side edited it, the other deleted it",
    "binary": "binary file changed on both sides",
    "unavailable": "content missing from the object store (cannot compare)",
}


def _classify_content(
    base_h: str | None, ours_h: str, theirs_h: str, store: "X.ObjectStore"
) -> tuple[str, str]:
    """Both sides changed a still-present file differently: conflict or auto?

    Reads the three versions and falls back to a conservative CONFLICT whenever
    a real content comparison is impossible (missing blob or binary data).
    """
    base_b = _blob(store, base_h)
    ours_b = _blob(store, ours_h)
    theirs_b = _blob(store, theirs_h)
    if base_b is None or ours_b is None or theirs_b is None:
        return "conflict", "unavailable"
    if _is_binary(base_b) or _is_binary(ours_b) or _is_binary(theirs_b):
        return "conflict", "binary"
    if _regions_overlap(_lines(base_b), _lines(ours_b), _lines(theirs_b)):
        return "conflict", "overlapping"
    return "auto", ""


def _classify(
    base_h: str | None, ours_h: str | None, theirs_h: str | None,
    store: "X.ObjectStore",
) -> tuple[str, str]:
    """Classify one path into a category, mirroring ``ops.plan_merge`` logic.

    Categories: ``identical`` / ``keep`` (no-ops, hidden), ``incoming`` (would
    take theirs — clean), ``auto`` (both changed, non-overlapping), ``conflict``.
    Returns ``(category, reason)`` where ``reason`` is a ``_WHY`` key for
    conflicts and a resolution word ("take-theirs"/"delete") for incoming.
    """
    if ours_h == theirs_h:
        return "identical", ""      # already the same on both tips
    if theirs_h == base_h:
        return "keep", ""           # only ours changed -> merge keeps ours (no-op)
    if ours_h == base_h:
        # only theirs changed -> the merge would take theirs (or delete)
        return "incoming", "delete" if theirs_h is None else "take-theirs"

    # Both sides changed this path differently.
    if ours_h is None or theirs_h is None:
        # One side deleted a file the other modified (base is present, since a
        # None side equal to a None base was already handled above).
        return "conflict", "edit-vs-delete"
    if base_h is None:
        # Neither side inherited it from the base — both created it, differently.
        return "conflict", "add-vs-add"
    return _classify_content(base_h, ours_h, theirs_h, store)


# --------------------------------------------------------------------------- #
# branch resolution
# --------------------------------------------------------------------------- #
def _resolve_branch(
    conn: sqlite3.Connection, root_id: int, name: str, role: str
) -> sqlite3.Row:
    """Look up a branch by name, or raise a helpful click error.

    ``role`` ("into"/"other") only shapes the message. A single-timeline repo
    gets a hint to fork; otherwise the available names are listed.
    """
    branch = X.dbm.branch_by_name(conn, root_id, name)
    if branch is not None:
        return branch
    all_branches = X.dbm.list_branches(conn, root_id)
    if len(all_branches) <= 1:
        raise X.click.ClickException(
            f"this root has only one timeline "
            f"({all_branches[0]['name'] if all_branches else 'main'!r}); "
            "there is nothing to merge — create another with `chronx fork <name>`"
        )
    names = ", ".join(b["name"] for b in all_branches)
    raise X.click.ClickException(
        f"no timeline named {name!r} to use as {role} (available: {names})"
    )


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.argument("other")
    @X.click.argument("into", required=False)
    def conflicts(other: str, into: str | None) -> None:
        """Predict which files would conflict if you merged OTHER into INTO.

        A read-only dry-run of ``chronx merge``: nothing is written. OTHER is the
        timeline you would merge FROM; INTO (default: the active timeline) is the
        one you would merge into. Exits 1 if any conflict is predicted (CI-safe),
        0 if OTHER would merge cleanly.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # Untracked cwd / empty store -> clean ClickException from these.
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])

            # Resolve INTO (default: active timeline) and OTHER.
            if into is None:
                into_branch = X.dbm.active_branch(conn, root_id)
                if into_branch is None:
                    raise X.click.ClickException(
                        "this root has no active timeline (store not initialized?)"
                    )
                into_name = into_branch["name"]
            else:
                into_branch = _resolve_branch(conn, root_id, into, "into")
                into_name = into
            other_branch = _resolve_branch(conn, root_id, other, "other")

            if int(into_branch["id"]) == int(other_branch["id"]):
                raise X.click.ClickException(
                    "cannot compare a timeline with itself"
                )

            # Reconstruct the three states the real merge would compare.
            now = time.time()
            base, base_desc = merge_base_state(conn, into_branch, other_branch)
            ours: TreeState = X.branch_state_at(conn, into_branch, now)
            theirs: TreeState = X.branch_state_at(conn, other_branch, now)

            conflict_rows: list[tuple[str, str]] = []   # (path, why-key)
            auto_rows: list[str] = []                    # paths
            incoming_rows: list[tuple[str, str]] = []    # (path, resolution)

            for rel in set(base) | set(ours) | set(theirs):
                base_h = base.get(rel, (None, None))[0]
                ours_h = ours.get(rel, (None, None))[0]
                theirs_h = theirs.get(rel, (None, None))[0]
                category, reason = _classify(base_h, ours_h, theirs_h, store)
                if category == "conflict":
                    conflict_rows.append((rel, reason))
                elif category == "auto":
                    auto_rows.append(rel)
                elif category == "incoming":
                    incoming_rows.append((rel, reason))
                # 'identical' / 'keep' are no-ops for the merge — not shown.

            conflict_rows.sort()
            auto_rows.sort()
            incoming_rows.sort()

            # Headline (always shown).
            X.click.secho(
                f"merging {other} into {into_name} (base: {base_desc}): "
                f"{len(conflict_rows)} conflict(s), {len(auto_rows)} auto-merge(s), "
                f"{len(incoming_rows)} clean incoming",
                bold=True,
            )

            if conflict_rows:
                X.click.echo()
                for rel, why in conflict_rows:
                    label = X.click.style("CONFLICT", fg="red", bold=True)
                    X.click.echo(f"  {label}  {rel}  — {_WHY.get(why, why)}")

            if auto_rows:
                X.click.echo()
                for rel in auto_rows:
                    label = X.click.style("auto", fg="cyan")
                    X.click.echo(
                        X.click.style(
                            f"  {label}      {rel}  — non-overlapping edits, "
                            "merges automatically",
                            dim=True,
                        )
                    )

            if incoming_rows:
                X.click.echo()
                for rel, resolution in incoming_rows:
                    verb = "delete" if resolution == "delete" else "take"
                    label = X.click.style(verb, fg="green")
                    detail = (
                        "removed on the other side"
                        if resolution == "delete"
                        else "changed only on the other side"
                    )
                    X.click.echo(f"  {label}    {rel}  — {detail}")

            X.click.echo()
            if conflict_rows:
                X.click.secho(
                    f"{len(conflict_rows)} conflict(s) — resolve them, or run "
                    f"`chronx merge {other}` and fix the markers",
                    fg="red",
                )
                raise X.click.exceptions.Exit(1)  # CI-usable non-zero exit
            if not (auto_rows or incoming_rows):
                X.click.secho(
                    f"already up to date — {other} has nothing to merge "
                    f"into {into_name}",
                    fg="green",
                )
            else:
                X.click.secho(
                    f"no conflicts — {other} merges cleanly into {into_name}",
                    fg="green",
                )
        finally:
            conn.close()
