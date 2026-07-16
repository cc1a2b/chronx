"""chronx sizes — chart ONE file's SIZE evolution over its recorded history.

A per-file growth graph. Where ``whatchanged`` shows *what* changed in a file,
``sizes`` shows *how big* it was at every recorded version — so you can watch a
file balloon and shrink across a session at a glance.

For the current directory's tracked root and its active timeline (branch) this
builds the chronological size series of a single path:

  * the file's baseline size (the tree at tracking start), if it was present,
    as the first point (labelled ``baseline``);
  * then, in event order, the size the file had after every recorded command
    that touched it — a deletion counts as size 0 (``· deleted``).

It then prints a headline (current / min / max / peak / net change) and a
horizontal bar chart, one bar per version, scaled to the largest size the file
ever reached, using eighth-block resolution for smooth bars.

Everything is derived from raw, parameterised SQL over ``root_baseline`` and
``deltas``/``events`` (sizes are logical/uncompressed byte counts). Binary files
are fine — only the size matters. Strictly read-only: the store is never
mutated, and an empty / untracked store degrades to a clean message.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from chronx import pluginlib as X

# 0..8 eighths of a cell — lets a bar end on a fractional block for smoothness.
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
# Distinct glyph used in place of a bar for a version where the file was deleted.
_DELETE_MARK = "·"


# --------------------------------------------------------------------------- #
# a single point on the size timeline
# --------------------------------------------------------------------------- #
class _Point(NamedTuple):
    """One recorded version of the file.

    ``tag`` is ``"baseline"`` for the tracking-start point, else ``"#<event>"``.
    ``when`` is a short timestamp (empty for the baseline, which has no time).
    ``size`` is the logical byte count at that version (0 when ``deleted``).
    """

    tag: str
    when: str
    size: int
    deleted: bool


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _short_ts(ts: object) -> str:
    """A compact ``MM-DD HH:MM`` stamp, or ``""`` when there is no time."""
    if ts is None:
        return ""
    # fmt_ts -> 'YYYY-MM-DD HH:MM:SS'; slice out the month-day + hour-minute.
    return X.fmt_ts(float(ts))[5:16]


def _hbar(value: float, maximum: float, width: int) -> str:
    """A smooth horizontal bar (eighth-block resolution) for ``value/maximum``.

    Returns ``""`` when there is nothing to draw (non-positive value or scale),
    so a zero-size version renders as an empty bar rather than crashing.
    """
    if maximum <= 0 or value <= 0 or width <= 0:
        return ""
    frac = min(1.0, value / maximum) * width
    full = int(frac)
    bar = "█" * full
    if full < width:  # top up with a single partial cell for the remainder
        eighths = int(round((frac - full) * 8))
        if eighths:
            bar += _BAR_EIGHTHS[eighths]
    return bar


def _signed(delta: int) -> str:
    """``+494 B`` / ``-495 B`` / ``±0 B`` — a human, signed byte delta."""
    if delta == 0:
        return "±0 B"
    return f"{'+' if delta > 0 else '-'}{X.human_bytes(abs(delta))}"


# --------------------------------------------------------------------------- #
# data access — build the chronological size series for one path
# --------------------------------------------------------------------------- #
def _series(
    conn: X.sqlite3.Connection, root_id: int, rel: str, branch_id: int | None
) -> tuple[list[_Point], bool]:
    """The size timeline of ``rel`` on this root's active branch, oldest-first.

    Starts with the ``root_baseline`` size (if the path is in the baseline),
    then appends one point per recorded delta in event-id order. A delete has a
    NULL ``after_size`` and becomes size 0. When ``branch_id`` is unknown
    (pre-branch store) the branch filter is dropped so old stores still read.

    Returns ``(points, in_baseline)``.
    """
    points: list[_Point] = []

    base = conn.execute(
        "SELECT size FROM root_baseline WHERE root_id = ? AND path = ?",
        (root_id, rel),
    ).fetchone()
    in_baseline = base is not None
    if in_baseline:
        points.append(_Point("baseline", "", int(base["size"]), False))

    sql = (
        "SELECT e.id AS eid, e.started_at AS ts, d.change AS change,"
        " d.after_size AS after_size"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE d.path = ? AND e.root_id = ?"
    )
    params: list[object] = [rel, root_id]
    if branch_id is not None:
        sql += " AND e.branch_id = ?"
        params.append(branch_id)
    sql += " ORDER BY e.id"

    for r in conn.execute(sql, params):
        deleted = r["change"] == "D" or r["after_size"] is None
        size = 0 if deleted else int(r["after_size"])
        points.append(_Point(f"#{int(r['eid'])}", _short_ts(r["ts"]), size, deleted))

    return points, in_baseline


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _emit_headline(rel: str, points: list[_Point]) -> None:
    """Current / min / max / peak / net-change summary line for the series."""
    current, first, last = points[-1], points[0], points[-1]
    sizes = [p.size for p in points]
    lo, hi = min(sizes), max(sizes)
    peak = max(points, key=lambda p: p.size)  # first version reaching the max
    net = last.size - first.size

    cur_str = "deleted" if current.deleted else X.human_bytes(current.size)
    X.click.secho(f"  now {cur_str}", bold=True, nl=False)
    X.click.echo(
        f"   ·  min {X.human_bytes(lo)}"
        f"  ·  max {X.human_bytes(hi)} (peak {peak.tag})"
        f"  ·  net {_signed(net)}"
        f"  over {len(points)} version{'s' if len(points) != 1 else ''}"
    )


def _emit_chart(points: list[_Point], width: int) -> None:
    """One horizontal bar per version, scaled to the file's all-time max size."""
    maxsize = max(p.size for p in points)  # 0 only if every version is empty
    tagw = max(len(p.tag) for p in points)
    whenw = max(len(p.when) for p in points)

    for p in points:
        if p.deleted:
            raw = _DELETE_MARK
            bar = X.click.style(raw, fg="red")
            value = X.click.style("deleted", fg="red")
        else:
            raw = _hbar(p.size, maxsize, width)
            bar = X.click.style(raw, fg="green")
            value = X.human_bytes(p.size)
        # Pad using the *raw* (unstyled) length so ANSI codes don't skew columns.
        pad = " " * max(0, width - len(raw))
        tag = X.click.style(f"{p.tag:<{tagw}}", fg="cyan")
        X.click.echo(f"  {tag}  {p.when:<{whenw}}  {bar}{pad}  {value}")


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.option(
        "--width", "-w", default=50, show_default=True, help="Chart width in columns."
    )
    def sizes(file, width: int) -> None:  # type: ignore[no-untyped-def]
        """Chart FILE's size evolution over its recorded history.

        Builds the chronological size series of FILE on the current directory's
        active timeline — its baseline size (if present) followed by the size
        after every recorded command that touched it — and prints a summary plus
        a horizontal bar chart. FILE need not still exist: a deleted file's
        history lives entirely in the store. Strictly read-only.
        """
        width = max(1, int(width))
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # clean ClickException if cwd is untracked
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # Resolve FILE to a root-relative, forward-slash path key.
            rel = os.path.relpath(file.resolve(), root["path"]).replace(os.sep, "/")

            points, in_baseline = _series(conn, root_id, rel, branch_id)
            if not points:
                X.click.secho(
                    f"{rel}: never recorded — chronx has no size history for this "
                    "path on the current timeline (created before tracking, "
                    "ignored, or the name/branch differs).",
                    fg="yellow",
                )
                return

            # header: which file, on which timeline
            header = f"sizes  {rel}"
            if branch_id is not None:
                b = X.dbm.get_branch(conn, branch_id)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)
            if len(points) == 1 and in_baseline:
                X.click.secho(
                    "  (only ever seen in the baseline — a single version)", dim=True
                )

            _emit_headline(rel, points)
            X.click.echo("")
            _emit_chart(points, width)
        finally:
            conn.close()
