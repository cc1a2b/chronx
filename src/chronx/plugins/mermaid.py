"""chronx mermaid — export the command/branch timeline as a Mermaid diagram.

Where ``chronx graph`` draws an ASCII lane diagram and ``chronx graphviz`` emits
Graphviz DOT, this plugin emits **Mermaid** — the diagram format GitHub renders
*natively* inside Markdown (READMEs, issues, PRs, discussions) with no external
tooling. Paste the output into a fenced ```` ```mermaid ```` block and it draws.

Two flavours, chosen with ``--kind``:

``gitgraph`` (default)
    A Mermaid ``gitGraph`` — the prettiest view for simple fork/merge histories.
    The tracked root's trunk becomes mermaid's ``main``; forks turn into
    ``branch``/``checkout`` pairs and ``chronx merge`` events turn into ``merge``
    nodes. gitGraph is a linear, stateful format, so events are replayed in
    global chronological order while tracking the current branch (HEAD).

``flowchart``
    A Mermaid ``flowchart TD`` — one node per event, chained per branch inside a
    ``subgraph``, with dashed *fork* edges from parent to child. More robust for
    tangled histories; merge / failed / external nodes get a ``classDef`` style.

Every scrap of recorded command text is escaped for Mermaid (double quotes
become single quotes, newlines collapse, long commands truncate) so nothing a
user ran can break the diagram. Strictly read-only over the store; imports only
``chronx.pluginlib`` (as X) plus the stdlib, so it is auto-discovered with no
reinstall.
"""

from __future__ import annotations

from chronx import pluginlib as X

# Characters kept from a command before it is truncated in a node/commit label.
_CMD_LIMIT = 28


# --------------------------------------------------------------------- escaping


def _esc_label(text: str) -> str:
    """Escape ``text`` for use inside a Mermaid double-quoted label.

    Mermaid ids/labels are quoted strings, so the one thing that must never
    appear is a raw double quote — replace it with a single quote. Newlines,
    carriage returns and tabs are collapsed to single spaces so a label can
    never span lines and break the diagram.
    """
    return " ".join(text.replace('"', "'").split())


def _esc_comment(text: str) -> str:
    """Sanitise free text for a ``%% ...`` Mermaid comment line (single line)."""
    return " ".join(text.replace('"', "'").split())


def _short_cmd(text: str, limit: int = _CMD_LIMIT) -> str:
    """One-line, length-capped, escaped form of a command for a label."""
    text = _esc_label(text)
    if len(text) > limit:
        return text[: limit - 1] + "…"  # ellipsis
    return text


def _label(event: "X.sqlite3.Row") -> str:
    """``#<id> <short cmd>`` label for an event (already Mermaid-safe)."""
    return f"#{int(event['id'])} {_short_cmd(X.describe_command(event))}"


# ------------------------------------------------------------------- classifying


def _is_merge(command: "str | None") -> bool:
    return command is not None and command.startswith("chronx merge")


def _kind(command: "str | None") -> str:
    """Classify an event by its command: merge / external / op / cmd."""
    if command is None:
        return "external"
    if _is_merge(command):
        return "merge"
    if command.startswith("chronx "):
        return "op"
    return "cmd"


def _failed(event: "X.sqlite3.Row") -> bool:
    """True when the event recorded a non-zero (non-None) exit code."""
    return event["exit_code"] not in (None, 0)


def _merge_source(command: str) -> "str | None":
    """The branch name a ``chronx merge <name> ...`` command merges *from*."""
    parts = command.split()
    # parts == ["chronx", "merge", "<name>", "--flag", ...]; first non-flag
    # token after "merge" is the source branch.
    for tok in parts[2:]:
        if not tok.startswith("-"):
            return tok
    return None


# ------------------------------------------------------------ mermaid identifiers


def _safe_branch_token(name: str, bid: int) -> str:
    """A bare-token branch name safe to use in gitGraph ``branch``/``merge``.

    gitGraph branch names are unquoted tokens, so restrict to a conservative
    charset and fall back to ``b<id>`` if nothing survives.
    """
    kept = [c if (c.isalnum() or c in "_./-") else "_" for c in name]
    token = "".join(kept).strip("_")
    return token or f"b{bid}"


def _gitgraph_names(branches: "list[X.sqlite3.Row]", trunk_id: int) -> "dict[int, str]":
    """Map each branch id to a unique gitGraph branch token."""
    names: dict[int, str] = {}
    used: set[str] = set()
    for b in branches:
        bid = int(b["id"])
        token = _safe_branch_token(str(b["name"]), bid)
        if token in used:  # keep tokens unique across branches
            token = f"{token}_{bid}"
        used.add(token)
        names[bid] = token
    return names


# ------------------------------------------------------------------- grouping


def _group_by_branch(
    events: "list[X.sqlite3.Row]",
    branches: "list[X.sqlite3.Row]",
    branch_by_id: "dict[int, X.sqlite3.Row]",
) -> "tuple[dict[int | None, list[X.sqlite3.Row]], list[int | None]]":
    """Bucket events by branch id (unknown/None branch -> None), plus an order.

    Returns ``(by_branch, ordered_keys)`` where ordered_keys is branches in
    creation order (those that actually have shown events), with the None group
    last. Mirrors the graphviz plugin's grouping so both stay consistent.
    """
    by_branch: dict[int | None, list["X.sqlite3.Row"]] = {}
    for e in events:
        bid = e["branch_id"]
        key = int(bid) if bid is not None and int(bid) in branch_by_id else None
        by_branch.setdefault(key, []).append(e)
    for evs in by_branch.values():
        evs.sort(key=lambda r: int(r["id"]))

    ordered_keys: list[int | None] = [
        int(b["id"]) for b in branches if int(b["id"]) in by_branch
    ]
    if None in by_branch:
        ordered_keys.append(None)
    return by_branch, ordered_keys


def _trunk_id(branches: "list[X.sqlite3.Row]") -> "int | None":
    """The trunk branch id: the root branch (no parent), else the first made."""
    for b in branches:
        if b["parent_branch_id"] is None:
            return int(b["id"])
    return int(branches[0]["id"]) if branches else None


# ------------------------------------------------------------------- gitGraph


def _build_gitgraph(
    branches: "list[X.sqlite3.Row]",
    events: "list[X.sqlite3.Row]",
    comment: str,
) -> "list[str]":
    """Render the timeline as a Mermaid ``gitGraph`` (body lines, no fence)."""
    if not branches or not events:
        # Empty store / no history: a minimal but valid gitGraph.
        return [f"%% {comment}", "gitGraph", "  commit"]

    trunk = _trunk_id(branches)
    branch_by_id = {int(b["id"]): b for b in branches}
    names = _gitgraph_names(branches, int(trunk))  # type: ignore[arg-type]
    trunk_name = names[int(trunk)]  # type: ignore[index]

    lines: list[str] = []
    # If the trunk isn't literally "main", rename mermaid's default branch so
    # commits/checkouts against the trunk resolve. Uses single quotes only, so
    # it stays free of double quotes.
    if trunk_name != "main":
        lines.append(
            "%%{init: {'gitGraph': {'mainBranchName': '" + trunk_name + "'}}}%%"
        )
    lines.append(f"%% {comment}")
    lines.append("gitGraph")

    created: set[int] = {int(trunk)}  # type: ignore[arg-type]
    current: int = int(trunk)  # type: ignore[arg-type]

    def checkout(bid: int) -> None:
        nonlocal current
        if current != bid:
            lines.append(f"  checkout {names[bid]}")
            current = bid

    def create_branch(bid: int) -> None:
        """Emit branch/checkout for a not-yet-seen non-trunk branch."""
        nonlocal current
        b = branch_by_id[bid]
        parent = b["parent_branch_id"]
        # Fork off the parent lane when we already have it; else off HEAD.
        if parent is not None and int(parent) in created:
            checkout(int(parent))
        lines.append(f"  branch {names[bid]}")
        lines.append(f"  checkout {names[bid]}")
        created.add(bid)
        current = bid

    for e in events:
        raw_bid = e["branch_id"]
        bid = int(raw_bid) if raw_bid is not None and int(raw_bid) in branch_by_id else None

        if bid is not None:
            if bid not in created:
                create_branch(bid)
            else:
                checkout(bid)
        # Unknown/None-branch events attach to the current HEAD as commits.

        cmd = e["command"]
        if _is_merge(cmd):
            src_name = _merge_source(cmd) if cmd is not None else None
            # Resolve the source to a *created*, different mermaid branch.
            src_id = None
            if src_name is not None:
                for b in branches:
                    if str(b["name"]) == src_name:
                        src_id = int(b["id"])
                        break
            if src_id is not None and src_id in created and src_id != current:
                lines.append(f'  merge {names[src_id]} id: "{_label(e)}"')
                continue
            # Fall back to an ordinary commit so output stays valid.
        lines.append(f'  commit id: "{_label(e)}"')

    return lines


# ------------------------------------------------------------------- flowchart


def _flow_node(event: "X.sqlite3.Row") -> str:
    """``e<id>["<label>"]`` node definition for a flowchart."""
    return f'e{int(event["id"])}["{_label(event)}"]'


def _build_flowchart(
    branches: "list[X.sqlite3.Row]",
    events: "list[X.sqlite3.Row]",
    comment: str,
) -> "list[str]":
    """Render the timeline as a Mermaid ``flowchart TD`` (body lines, no fence)."""
    lines: list[str] = [f"%% {comment}", "flowchart TD"]

    if not events:
        lines.append('  empty["(no events recorded)"]')
        return lines

    branch_by_id = {int(b["id"]): b for b in branches}
    by_branch, ordered_keys = _group_by_branch(events, branches, branch_by_id)

    # --- nodes: one subgraph per branch, bare nodes for the None group.
    for key in ordered_keys:
        evs = by_branch[key]
        if key is None:
            for e in evs:
                lines.append("  " + _flow_node(e))
            continue
        title = _esc_label(str(branch_by_id[key]["name"]))
        lines.append(f'  subgraph b{key}["{title}"]')
        for e in evs:
            lines.append("    " + _flow_node(e))
        lines.append("  end")

    # --- chain edges: older -> newer within each branch/group.
    for key in ordered_keys:
        evs = by_branch[key]
        for prev, cur in zip(evs, evs[1:]):
            lines.append(f"  e{int(prev['id'])} --> e{int(cur['id'])}")

    # --- fork edges: dashed edge from the parent lane to each branch's first
    # shown event. Anchor on the parent event newest at/before the fork; if none
    # is shown, fall back to the parent's earliest shown event.
    for key in ordered_keys:
        if key is None:
            continue
        branch = branch_by_id[key]
        parent = branch["parent_branch_id"]
        if parent is None:
            continue
        parent_evs = by_branch.get(int(parent))
        if not parent_evs:
            continue
        base = float(branch["base_ts"])
        child_first = by_branch[key][0]
        candidates = [pe for pe in parent_evs if float(pe["started_at"]) <= base]
        if candidates:
            anchor = max(candidates, key=lambda r: (float(r["started_at"]), int(r["id"])))
        else:
            anchor = parent_evs[0]
        lines.append(
            f"  e{int(anchor['id'])} -. fork .-> e{int(child_first['id'])}"
        )

    # --- styling: colour merge / failed / external nodes. Each node gets at
    # most one class (failed wins, then merge, then external).
    buckets: dict[str, list[int]] = {"failed": [], "merge": [], "external": []}
    for e in events:
        eid = int(e["id"])
        if _failed(e):
            buckets["failed"].append(eid)
        elif _kind(e["command"]) == "merge":
            buckets["merge"].append(eid)
        elif _kind(e["command"]) == "external":
            buckets["external"].append(eid)

    if any(buckets.values()):
        lines.append(
            "  classDef merge fill:#efe3ff,stroke:#7c3aed,color:#1a1a1a;"
        )
        lines.append(
            "  classDef failed fill:#ffe3e3,stroke:#e5484d,"
            "stroke-width:2px,color:#1a1a1a;"
        )
        lines.append(
            "  classDef external fill:#eef1f4,stroke:#8895a7,"
            "stroke-dasharray:4 3,color:#1a1a1a;"
        )
        for cls, ids in buckets.items():
            if ids:
                lines.append(f"  class {','.join('e' + str(i) for i in ids)} {cls}")

    return lines


# --------------------------------------------------------------------- command


def register(main: "X.click.Group") -> None:
    @main.command()
    @X.click.option(
        "--output",
        "-o",
        type=X.click.Path(path_type=X.Path),
        default=None,
        help="Write the Mermaid diagram to FILE instead of stdout.",
    )
    @X.click.option(
        "--fence/--no-fence",
        default=False,
        help="Wrap the output in a ```mermaid code fence (paste-ready for GitHub).",
    )
    @X.click.option(
        "--limit",
        "-n",
        default=300,
        show_default=True,
        help="Max number of most-recent events (across all branches) to include.",
    )
    @X.click.option(
        "--kind",
        type=X.click.Choice(["gitgraph", "flowchart"]),
        default="gitgraph",
        show_default=True,
        help="gitgraph (prettiest for forks/merges) or flowchart (robust).",
    )
    def mermaid(
        output: "X.Path | None", fence: bool, limit: int, kind: str
    ) -> None:
        """Export the timeline as a Mermaid diagram (renders in GitHub Markdown).

        gitGraph maps the trunk to mermaid's ``main`` with branch/merge nodes;
        flowchart draws one node per event with per-branch subgraphs and dashed
        fork edges. Use ``--fence`` to get a paste-ready ```mermaid block.
        """
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # clean ClickException if cwd untracked
            root_id = int(root["id"])

            branches = X.dbm.list_branches(conn, root_id)
            # Most-recent `limit` events across ALL branches, oldest-first.
            events = X.dbm.recent_events(conn, root_id=root_id, limit=int(limit))

            comment = _esc_comment(
                f"chronx timeline for {root['path']} "
                f"- {len(events)} event(s), {len(branches)} branch(es)"
            )
            if kind == "flowchart":
                body = _build_flowchart(branches, events, comment)
            else:
                body = _build_gitgraph(branches, events, comment)

            if fence:
                body = ["```mermaid", *body, "```"]
            text = "\n".join(body) + "\n"

            if output is not None:
                X.write_atomic(output, text.encode("utf-8"))
                X.click.echo(f"wrote Mermaid diagram to {output}", err=True)
                X.click.echo(
                    "# renders in GitHub Markdown (```mermaid block) "
                    "or at https://mermaid.live",
                    err=True,
                )
            else:
                X.click.echo(text, nl=False)
                X.click.echo(
                    "# paste into a GitHub README/issue as a ```mermaid block, "
                    "or render at https://mermaid.live",
                    err=True,
                )
        finally:
            conn.close()
