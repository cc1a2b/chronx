"""chronx ls — list the tree exactly as it was at any recorded moment.

``ls`` for time travel: see which files existed (and, with ``-l``, how big
they were and their modes) at any mark, event id, relative/ISO moment, or
branch tip — the "look before you leap" companion to ``checkout`` / ``archive``
/ ``rollback``.

Purely read-only: opens the store read-only and never mutates anything. It
reconstructs the FULL recorded tree at the requested ref via the same
``state_at`` / ``branch_state_at`` helpers the write commands use, so what you
see here is precisely what a checkout at that ref would materialize.
"""

from __future__ import annotations

import stat as statmod
import time

from chronx import pluginlib as X

# A nested {name: subtree} dict; a file is an empty leaf dict.
Tree = dict


# --------------------------------------------------------------------------- #
# size lookup
# --------------------------------------------------------------------------- #
def _size_by_digest(conn: "X.sqlite3.Connection", root_id: int) -> dict[str, int]:
    """Map blob digest -> logical (uncompressed) size in ONE scan per table.

    ``state_at`` hands back only (hash, mode) per path, so sizes are recovered
    here: a blob's size is any ``deltas.after_size`` that produced it, or the
    ``root_baseline.size`` of a file unchanged since tracking began. Identical
    content shares a digest, so the dict is naturally deduplicated; sizes for a
    given digest should agree — we keep the max defensively.
    """
    sizes: dict[str, int] = {}
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
    return sizes


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _style_name(name: str, mode: int | None) -> str:
    """Green for executables (any exec bit set), plain otherwise."""
    if mode is not None and statmod.S_IMODE(mode) & 0o111:
        return X.click.style(name, fg="green")
    return name


def _mode_str(mode: int | None) -> str:
    """Four-digit octal permission bits, or ``????`` when unknown."""
    return f"{statmod.S_IMODE(mode):04o}" if mode is not None else "????"


def _build_tree(rels: list[str]) -> Tree:
    """Fold a sorted list of relative paths into a nested directory tree.

    Directories are implicit — derived from the ``/`` prefixes of the file
    paths. Every leaf (empty child dict) is therefore a file.
    """
    tree: Tree = {}
    for rel in rels:
        node = tree
        for part in rel.split("/"):
            node = node.setdefault(part, {})
    return tree


def _render_tree(
    node: Tree, path_prefix: str, indent: str, modes: dict[str, int | None]
) -> None:
    """Print a unicode box-drawing tree; dirs bold-blue, files exec-colored."""
    names = sorted(node)
    for i, name in enumerate(names):
        last = i == len(names) - 1
        connector = "└── " if last else "├── "
        child = node[name]
        full = path_prefix + name
        if child:  # non-empty subtree -> directory
            X.click.echo(
                indent + connector + X.click.style(name + "/", fg="blue", bold=True)
            )
            _render_tree(
                child, full + "/", indent + ("    " if last else "│   "), modes
            )
        else:  # leaf -> file
            X.click.echo(indent + connector + _style_name(name, modes.get(full)))


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command("ls")
    @X.click.argument("ref", default="now")
    @X.click.option("--long", "-l", is_flag=True, help="Show mode and size columns.")
    @X.click.option("--tree", "as_tree", is_flag=True, help="Indented directory tree.")
    @X.click.option(
        "--path", "path_prefix", default=None, metavar="P",
        help="Only list entries at or under this path prefix.",
    )
    def ls_cmd(
        ref: str, long: bool, as_tree: bool, path_prefix: str | None
    ) -> None:
        """List files as they were at REF (mark, event id, moment, or branch)."""
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])

            # 1. Resolve the ref. A branch name wins (its tip); otherwise treat
            #    REF as a moment (mark / #event / 'now' / relative / ISO).
            branch = X.dbm.branch_by_name(conn, root_id, ref)
            if branch is not None:
                state = X.branch_state_at(conn, branch, time.time())
                desc = f"timeline {ref!r} (tip)"
            else:
                ts = X.moment_ts(conn, ref)  # clean ClickException on a bad ref
                state = X.state_at(conn, root_id, ts)
                desc = X.fmt_ts(ts)

            # 2. Keep only present files (absent entries carry a None hash).
            present: dict[str, tuple[str, int | None]] = {
                rel: (h, m) for rel, (h, m) in state.items() if h is not None
            }

            # Optional path-prefix filter (the file itself or anything under it).
            if path_prefix:
                pref = path_prefix.strip("/")
                present = {
                    rel: v for rel, v in present.items()
                    if rel == pref or rel.startswith(pref + "/")
                }

            modes: dict[str, int | None] = {rel: m for rel, (_h, m) in present.items()}

            # Header: <root path> @ <resolved description>.
            X.click.secho(f"{root['path']} @ {desc}", bold=True)

            if not present:
                X.click.echo("(empty tree)")
                return

            sizes = _size_by_digest(conn, root_id)  # built once, up front
            rels = sorted(present)

            total = 0
            for rel in rels:
                sz = sizes.get(present[rel][0])
                if sz is not None:
                    total += sz

            # 3. Render the chosen view.
            if as_tree:
                _render_tree(_build_tree(rels), "", "", modes)
            elif long:
                for rel in rels:
                    _h, m = present[rel]
                    sz = sizes.get(_h)
                    size_str = X.human_bytes(sz) if sz is not None else "?"
                    X.click.echo(
                        f"{_mode_str(m):>5}  {size_str:>9}  {_style_name(rel, m)}"
                    )
            else:
                for rel in rels:
                    X.click.echo(_style_name(rel, present[rel][1]))

            # 4. Footer: file count + total of the sizes we could resolve.
            n = len(rels)
            X.click.secho(
                f"{n} file(s), total {X.human_bytes(total)}", dim=True
            )
        finally:
            conn.close()
