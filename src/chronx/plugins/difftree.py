"""chronx diff-tree <refA> <refB> — a STRUCTURAL comparison of two tree states.

Answers "what is different between these two points, or these two timelines?"
Unlike ``chronx diff A..B`` (the net *content* change along the active branch)
and ``chronx since <moment>`` (one moment → now), ``diff-tree`` compares any two
*arbitrary* refs by reconstructing each one's FULL recorded tree and diffing the
two trees path-by-path. Crucially, either ref may be a BRANCH TIP, so

    chronx diff-tree main experiment

tells you exactly how two timelines diverge — added / modified / deleted files —
without touching the working tree.

A ref is resolved the same way ``ls`` / ``checkout`` resolve one: a branch name
wins (its tip via ``branch_state_at``); otherwise it is a moment (mark, event id,
``now``, relative/ISO time) reconstructed on the active branch via ``state_at``.

Strictly read-only: the object store and database are only ever read, and the
command degrades gracefully on an empty store, binary blobs, or a missing blob.
"""

from __future__ import annotations

import time

from chronx import pluginlib as X

# git's A/M/D palette: added green, modified yellow, deleted red.
_CHANGE_COLOR = {"A": "green", "M": "yellow", "D": "red"}

# A resolved, present-only tree: relpath -> (blob digest, mode|None).
TreeState = dict


# --------------------------------------------------------------------------- #
# size lookup (state_at hands back only (hash, mode); sizes are recovered here)
# --------------------------------------------------------------------------- #
def _size_by_digest(conn: "X.sqlite3.Connection", root_id: int) -> dict[str, int]:
    """Map blob digest -> logical (uncompressed) size in one scan per table.

    A blob's size is any ``deltas.after_size`` that produced it, or the
    ``root_baseline.size`` of a file unchanged since tracking began. Identical
    content shares a digest so the map is naturally deduplicated; we keep the
    max defensively. Any SQL hiccup degrades to "size unknown" rather than a
    crash — sizes are cosmetic here, never load-bearing.
    """
    sizes: dict[str, int] = {}
    try:
        for r in conn.execute(
            "SELECT d.after_hash AS h, d.after_size AS s"
            " FROM deltas d JOIN events e ON e.id = d.event_id"
            " WHERE e.root_id = ? AND d.after_hash IS NOT NULL"
            "   AND d.after_size IS NOT NULL",
            (root_id,),
        ):
            h, s = r["h"], int(r["s"])
            if s > sizes.get(h, -1):
                sizes[h] = s
        for r in conn.execute(
            "SELECT hash AS h, size AS s FROM root_baseline WHERE root_id = ?",
            (root_id,),
        ):
            h, s = r["h"], int(r["s"])
            if s > sizes.get(h, -1):
                sizes[h] = s
    except X.sqlite3.Error:
        pass
    return sizes


# --------------------------------------------------------------------------- #
# ref resolution
# --------------------------------------------------------------------------- #
def _resolve(
    conn: "X.sqlite3.Connection", root_id: int, ref: str
) -> tuple[TreeState, str]:
    """Resolve REF to a present-only tree-state dict plus a human description.

    A branch name wins (its tip); otherwise REF is treated as a moment. Only
    present files (a non-``None`` hash) are kept — absent entries carry a
    ``None`` hash and would falsely register as "deleted" against them.
    An unknown ref surfaces as a clean ``ClickException`` from ``moment_ts``.
    """
    branch = X.dbm.branch_by_name(conn, root_id, ref)
    if branch is not None:
        state = X.branch_state_at(conn, branch, time.time())
        desc = ref  # the branch name itself
    else:
        ts = X.moment_ts(conn, ref)  # clean ClickException on a bad ref
        state = X.state_at(conn, root_id, ts)
        desc = X.fmt_ts(ts)
    present: TreeState = {
        rel: (h, m) for rel, (h, m) in state.items() if h is not None
    }
    return present, desc


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def _compare(a_state: TreeState, b_state: TreeState) -> list[tuple[str, str]]:
    """Structural A→B tree diff: sorted list of (relpath, change) pairs.

    Present only in B → ``A`` (added); present only in A → ``D`` (deleted);
    present in both with a different digest → ``M`` (modified); identical
    digests are unchanged and omitted.
    """
    diffs: list[tuple[str, str]] = []
    for rel in set(a_state) | set(b_state):
        a = a_state.get(rel)
        b = b_state.get(rel)
        if a is None:
            change = "A"
        elif b is None:
            change = "D"
        elif a[0] != b[0]:
            change = "M"
        else:
            continue  # same digest -> unchanged
        diffs.append((rel, change))
    diffs.sort(key=lambda d: d[0])
    return diffs


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def _size_str(size: int | None) -> str:
    """Human-readable size, or ``-`` for an absent side (added/deleted)."""
    return X.human_bytes(size) if size is not None else "-"


def _stat_line(
    rel: str, change: str, a_size: int | None, b_size: int | None
) -> str:
    """A ``git diff --stat``-like line, colored by change kind.

    ``<A/M/D>  <path>  (<sizeA> -> <sizeB>)`` — the whole line takes the
    change's color (added green, modified yellow, deleted red).
    """
    line = f"{change}  {rel}  ({_size_str(a_size)} -> {_size_str(b_size)})"
    return X.click.style(line, fg=_CHANGE_COLOR.get(change))


def _style_diff_line(line: str) -> str:
    """Colorize one unified-diff line by its leading character.

    ``---``/``+++`` file headers are bold and tested before ``-``/``+`` so a
    header is never mistaken for a change line; ``@@`` hunk / binary / missing /
    truncation markers are cyan; additions green, deletions red.
    """
    if line.startswith(("---", "+++")):
        return X.click.style(line, bold=True)
    if line.startswith("@@"):
        return X.click.style(line, fg="cyan")
    if line.startswith("+"):
        return X.click.style(line, fg="green")
    if line.startswith("-"):
        return X.click.style(line, fg="red")
    return line


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command("diff-tree")
    @X.click.argument("ref_a")
    @X.click.argument("ref_b", default="now")
    @X.click.option(
        "--patch", "-p", is_flag=True, help="Show content diffs, not just status."
    )
    @X.click.option(
        "--name-only", is_flag=True, help="List only the differing paths."
    )
    def diff_tree(ref_a: str, ref_b: str, patch: bool, name_only: bool) -> None:
        """Structurally compare two moments OR branch tips (what differs).

        REF_A and REF_B are each a branch name (its tip), a mark, an event id
        (``#42``/``42``), ``now``, or a time spec. REF_B defaults to ``now``
        (the current active tip). Use ``--name-only`` for a bare path list, or
        ``-p/--patch`` for a real unified diff of every differing file.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # Untracked cwd / empty store -> clean ClickException from these.
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])

            # Resolve both refs (branch tip or moment). Unknown ref -> clean
            # ClickException raised inside _resolve/moment_ts.
            a_state, desc_a = _resolve(conn, root_id, ref_a)
            b_state, desc_b = _resolve(conn, root_id, ref_b)

            diffs = _compare(a_state, b_state)

            # Identical trees: report and exit 0.
            if not diffs:
                X.click.secho(
                    f"no differences between {desc_a} and {desc_b}", fg="green"
                )
                return

            # --name-only: bare, pipe-friendly path list, nothing else.
            if name_only:
                for rel, _change in diffs:
                    X.click.echo(rel)
                return

            X.click.secho(f"diff-tree  {desc_a}  ..  {desc_b}", bold=True)

            sizes = _size_by_digest(conn, root_id)
            counts = {"A": 0, "M": 0, "D": 0}

            for rel, change in diffs:
                counts[change] += 1
                a_hash = a_state[rel][0] if rel in a_state else None
                b_hash = b_state[rel][0] if rel in b_state else None
                a_size = sizes.get(a_hash) if a_hash is not None else None
                b_size = sizes.get(b_hash) if b_hash is not None else None

                if patch:
                    a_mode = a_state[rel][1] if rel in a_state else None
                    b_mode = b_state[rel][1] if rel in b_state else None
                    # Delta with A as the "before" side and B as the "after"
                    # side; render_delta handles /dev/null headers for A/D and
                    # emits a @@ marker for binary / missing / truncated blobs
                    # (so it never crashes on them).
                    delta = X.dbm.Delta(
                        rel, change, a_hash, b_hash,
                        a_size, b_size, a_mode, b_mode,
                    )
                    for line in X.render_delta(store, delta):
                        X.click.echo(_style_diff_line(line))
                    X.click.echo()
                else:
                    X.click.echo(_stat_line(rel, change, a_size, b_size))

            # Footer summary (both stat and patch views).
            X.click.echo()
            X.click.secho(
                f"{len(diffs)} file(s) differ "
                f"({counts['A']} added, {counts['M']} modified, "
                f"{counts['D']} deleted) between {desc_a} and {desc_b}",
                dim=True,
            )
        finally:
            conn.close()
