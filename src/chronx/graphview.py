"""Cross-timeline commit graph (lane layout), shared by the CLI and web UI.

Every branch gets a stable lane (column). Events across all branches are laid
out newest-first; a branch's lane is drawn as a vertical line between its
newest and oldest event, with its node (`*`/merge `◆`/external `◦`) on that
lane per row and a "forked from …" annotation at its base. This is a
deliberately robust layout — straight lanes plus fork/merge annotations —
rather than git's diagonal edge routing, so it never mis-renders.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db as dbm
from .ops import describe_command
from .when import fmt_ts

LANE_COLORS = ("cyan", "green", "yellow", "magenta", "blue", "bright_red", "bright_cyan")


@dataclass(frozen=True)
class GraphRow:
    lanes: list[bool]          # which lane columns are live on this row
    node_lane: int             # column carrying this event's node
    kind: str                  # 'merge' | 'external' | 'op' | 'cmd'
    event: sqlite3.Row
    branch_name: str
    annot: str                 # e.g. "forked from main @ …"


@dataclass(frozen=True)
class Graph:
    branch_names: list[str]    # index = lane
    rows: list[GraphRow]


def _kind(row: sqlite3.Row) -> str:
    cmd = row["command"]
    if cmd is None:
        return "external"
    if cmd.startswith("chronx merge "):
        return "merge"
    if cmd.startswith("chronx "):
        return "op"
    return "cmd"


def build_graph(
    conn: sqlite3.Connection, root_id: int, *, limit: int = 300
) -> Graph:
    branches = dbm.list_branches(conn, root_id)
    lane = {int(b["id"]): i for i, b in enumerate(branches)}
    names = [b["name"] for b in branches]
    parent = {int(b["id"]): b["parent_branch_id"] for b in branches}
    base_ts = {int(b["id"]): float(b["base_ts"]) for b in branches}
    bname = {int(b["id"]): b["name"] for b in branches}

    events = list(reversed(dbm.recent_events(conn, root_id=root_id, limit=limit)))
    if not events:
        return Graph(branch_names=names, rows=[])

    # First (newest) and last (oldest) row index per branch → the lane extent.
    newest: dict[int, int] = {}
    oldest: dict[int, int] = {}
    for i, e in enumerate(events):
        bid = e["branch_id"]
        if bid is None:
            continue
        bid = int(bid)
        newest.setdefault(bid, i)
        oldest[bid] = i

    n_lanes = (max(lane.values()) + 1) if lane else 1
    lane_branch = {i: bid for bid, i in lane.items()}

    rows: list[GraphRow] = []
    for i, e in enumerate(events):
        bid = int(e["branch_id"]) if e["branch_id"] is not None else None
        node_lane = lane.get(bid, 0) if bid is not None else 0
        lanes = [False] * n_lanes
        for col in range(n_lanes):
            cbid = lane_branch.get(col)
            if cbid is not None and cbid in newest and newest[cbid] <= i <= oldest[cbid]:
                lanes[col] = True
        lanes[node_lane] = True
        annot = ""
        if (
            bid is not None
            and i == oldest.get(bid)
            and parent.get(bid) is not None
        ):
            pid = int(parent[bid])
            annot = f"forked from {bname.get(pid, '?')} @ {fmt_ts(base_ts[bid])}"
        rows.append(GraphRow(
            lanes=lanes, node_lane=node_lane, kind=_kind(e), event=e,
            branch_name=bname.get(bid, "?") if bid is not None else "?", annot=annot,
        ))
    return Graph(branch_names=names, rows=rows)


_NODE = {"merge": "◆", "external": "◦", "op": "◍", "cmd": "●"}


def render_plain(graph: Graph) -> list[str]:
    """Monochrome rows: '<lane graphics>  #id time command' (for the web UI)."""
    out: list[str] = []
    for r in graph.rows:
        cells = []
        for col, live in enumerate(r.lanes):
            cells.append(_NODE[r.kind] if col == r.node_lane else ("│" if live else " "))
        graphics = " ".join(cells)
        e = r.event
        cmd = describe_command(e)
        suffix = f"   ← {r.annot}" if r.annot else ""
        out.append(f"{graphics}  #{e['id']} {fmt_ts(e['started_at']).split(' ')[1]} "
                   f"{cmd}{suffix}")
    return out
