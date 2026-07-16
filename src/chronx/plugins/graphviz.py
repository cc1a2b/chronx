"""chronx graphviz — export the command/branch timeline as a Graphviz DOT graph.

Where the built-in ``chronx graph`` draws an ASCII lane diagram, this plugin
emits the same cross-timeline history as a Graphviz **DOT** document that renders
to SVG/PNG::

    chronx graphviz | dot -Tsvg -o timeline.svg

For the tracked root containing the cwd it walks *every* branch (so forks are
visible), emitting one node per event grouped into a ``subgraph cluster_*`` per
branch. Within a branch, events are chained oldest -> newest; each branch's
first shown event gets a dashed "fork" edge back to its parent timeline.

Node shape/colour encodes the event kind (merge / chronx-internal op / external
change / ordinary command) and a red border flags a non-zero exit code. Every
piece of recorded command text is escaped for DOT so nothing a user ran can
break the graph.

Strictly read-only over the store; imports only ``chronx.pluginlib`` (as X) and
the stdlib, so it is auto-discovered with no reinstall.
"""

from __future__ import annotations

from chronx import pluginlib as X

# Characters kept from a command before it is truncated in a node label.
_CMD_LIMIT = 30
# Fill colour used for chronx's own internal operations.
_OP_FILL = "gray90"


def _dot_str(text: str) -> str:
    """Escape ``text`` for safe inclusion inside a DOT double-quoted string.

    Backslash and double-quote are escaped, and any newline/carriage return is
    turned into a literal ``\\n`` so recorded command content can never break
    out of its string and corrupt the graph.
    """
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch in ("\n", "\r"):
            out.append("\\n")
        else:
            out.append(ch)
    return "".join(out)


def _short_cmd(text: str, limit: int = _CMD_LIMIT) -> str:
    """One-line, length-capped form of a command for a node label."""
    text = " ".join(text.split())  # collapse any embedded whitespace/newlines
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _kind(command: str | None) -> str:
    """Classify an event by its command: merge / op / external / cmd."""
    if command is None:
        return "external"
    if command.startswith("chronx merge"):
        return "merge"
    if command.startswith("chronx "):
        return "op"
    return "cmd"


def _failed(event: "X.sqlite3.Row") -> bool:
    """True when the event recorded a non-zero (non-None) exit code."""
    return event["exit_code"] not in (None, 0)


def _node_line(event: "X.sqlite3.Row") -> str:
    """Build the ``e<id> [...]`` node definition (no indentation) for an event.

    The three-line label is ``#<id>`` / short command / timestamp, and the
    shape, style and border colour encode kind + success as described above.
    """
    eid = int(event["id"])
    kind = _kind(event["command"])
    cmd = _short_cmd(X.describe_command(event))
    when = X.fmt_ts(event["started_at"])
    label = "\\n".join(
        (_dot_str(f"#{eid}"), _dot_str(cmd), _dot_str(when))
    )

    attrs: list[str] = [f'label="{label}"']
    styles: list[str] = []
    if kind == "merge":
        attrs.append("shape=diamond")
    elif kind == "external":
        attrs.append("shape=ellipse")
        styles.append("dashed")
    elif kind == "op":
        attrs.append("shape=box")
        styles.append("filled")
        attrs.append(f'fillcolor="{_OP_FILL}"')
    else:  # ordinary command
        attrs.append("shape=box")

    if _failed(event):
        attrs.append('color="red"')
        attrs.append("penwidth=2")
    if styles:
        attrs.append(f'style="{",".join(styles)}"')

    return f"e{eid} [{', '.join(attrs)}];"


def register(main: "X.click.Group") -> None:
    @main.command()
    @X.click.option(
        "--output",
        "-o",
        type=X.click.Path(path_type=X.Path),
        default=None,
        help="Write the DOT graph to FILE instead of stdout.",
    )
    @X.click.option(
        "--limit",
        "-n",
        default=300,
        show_default=True,
        help="Max number of most-recent events (across all branches) to include.",
    )
    @X.click.option(
        "--rankdir",
        type=X.click.Choice(["TB", "LR"]),
        default="TB",
        show_default=True,
        help="Graph direction: top-to-bottom or left-to-right.",
    )
    def graphviz(output: "X.Path | None", limit: int, rankdir: str) -> None:
        """Export the commit/branch timeline as a Graphviz DOT graph.

        Renders every branch of the current directory's tracked root as boxed
        lanes with fork edges. Pipe it to Graphviz, e.g.
        ``chronx graphviz | dot -Tsvg -o timeline.svg``.
        """
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # clean ClickException if cwd untracked
            root_id = int(root["id"])

            branches = X.dbm.list_branches(conn, root_id)
            branch_by_id = {int(b["id"]): b for b in branches}
            # Most-recent `limit` events across ALL branches, oldest-first.
            events = X.dbm.recent_events(conn, root_id=root_id, limit=int(limit))

            lines: list[str] = [
                "digraph chronx {",
                f"  rankdir={rankdir};",
                '  node [fontname="monospace"];',
                '  edge [fontname="monospace"];',
            ]

            if not events:
                # Empty store / no history: still emit a valid, minimal graph.
                lines.append(f"  // no events recorded for {root['path']} yet")
                lines.append("}")
            else:
                # Group the shown events by branch. Events with no (or an
                # unknown) branch_id fall into the None group, rendered outside
                # any cluster.
                by_branch: dict[int | None, list["X.sqlite3.Row"]] = {}
                for e in events:
                    bid = e["branch_id"]
                    key = (
                        int(bid)
                        if bid is not None and int(bid) in branch_by_id
                        else None
                    )
                    by_branch.setdefault(key, []).append(e)
                for evs in by_branch.values():
                    evs.sort(key=lambda r: int(r["id"]))

                # Deterministic order: branches by creation order, None group last.
                ordered_keys: list[int | None] = [
                    int(b["id"]) for b in branches if int(b["id"]) in by_branch
                ]
                if None in by_branch:
                    ordered_keys.append(None)

                # --- nodes: one cluster per branch, bare nodes for the None group.
                for key in ordered_keys:
                    evs = by_branch[key]
                    if key is None:
                        for e in evs:
                            lines.append("  " + _node_line(e))
                        continue
                    name = _dot_str(branch_by_id[key]["name"])
                    lines.append(f"  subgraph cluster_{key} {{")
                    lines.append(f'    label="{name}";')
                    for e in evs:
                        lines.append("    " + _node_line(e))
                    lines.append("  }")

                # --- edges: chain each branch older -> newer.
                for key in ordered_keys:
                    evs = by_branch[key]
                    for prev, cur in zip(evs, evs[1:]):
                        lines.append(
                            f"  e{int(prev['id'])} -> e{int(cur['id'])};"
                        )

                # --- fork edges: dashed edge from the parent timeline to each
                # branch's first shown event. Anchor on the parent event that is
                # newest at or before the fork point; if none of those is shown,
                # fall back to the earliest shown parent event so the fork still
                # visibly descends from the parent lane.
                for key in ordered_keys:
                    if key is None:
                        continue
                    branch = branch_by_id[key]
                    parent = branch["parent_branch_id"]
                    if parent is None:
                        continue
                    parent_evs = by_branch.get(int(parent))
                    if not parent_evs:
                        continue  # parent has no shown node to anchor on
                    base = float(branch["base_ts"])
                    child_first = by_branch[key][0]
                    candidates = [
                        pe for pe in parent_evs if float(pe["started_at"]) <= base
                    ]
                    if candidates:
                        anchor = max(
                            candidates,
                            key=lambda r: (float(r["started_at"]), int(r["id"])),
                        )
                    else:
                        anchor = parent_evs[0]
                    label = _dot_str(f"fork @ {X.fmt_ts(base)}")
                    lines.append(
                        f"  e{int(anchor['id'])} -> e{int(child_first['id'])} "
                        f'[style=dashed, label="{label}"];'
                    )

                lines.append("}")

            dot = "\n".join(lines) + "\n"
            if output is not None:
                # Atomic write (creates parent dirs); confirmation to stderr so
                # stdout stays clean when the two are separated.
                X.write_atomic(output, dot.encode("utf-8"))
                X.click.echo(f"wrote DOT graph to {output}", err=True)
                X.click.echo(
                    f"# render: dot -Tsvg {output} -o timeline.svg", err=True
                )
            else:
                X.click.echo(dot, nl=False)
                X.click.echo(
                    "# render: chronx graphviz | dot -Tsvg -o timeline.svg",
                    err=True,
                )
        finally:
            conn.close()
