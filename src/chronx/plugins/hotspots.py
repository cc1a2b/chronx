"""chronx hotspots — code hotspot & change-coupling analysis.

Read-only forensics over your recorded history at *command* granularity —
something only chronx can do, because it knows exactly which files changed
together inside the same recorded command (not merely on the same day). For the
cwd's tracked root and its active timeline (branch) it reports:

  * most volatile files  — the paths that change in the most recorded events,
                           with their change count and total bytes churned, plus
                           a unicode bar scaled to the busiest file
  * change coupling      — unordered file PAIRS that keep changing together in
                           the same command, with a co-change count and a
                           coupling % (co-changes / min(changes(a), changes(b)));
                           this surfaces hidden dependencies ("touching X almost
                           always means touching Y")
  * churn over time      — a small sparkline of file-changes per time bucket

Everything is scoped to one root + one branch and derived entirely from raw,
parameterized SQL over ``events``/``deltas`` (Row factory). It never mutates the
store and degrades gracefully on an empty / single-file / untracked history.
"""

from __future__ import annotations

import collections
import itertools

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# tunables + unicode ramps for the little visualisations
# --------------------------------------------------------------------------- #
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"  # 0..8 eighths of a cell — smooth horizontal bars
_SPARK = "▁▂▃▄▅▆▇█"          # 8 discrete levels — the churn sparkline
_MAX_PAIR_PATHS = 50         # events touching more paths than this skip pairing
_SPARK_WIDTH = 30            # max buckets (columns) in the churn sparkline
_BAR_WIDTH = 16              # cells in the volatility bar


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _scope(root_id: int, branch_id: int | None) -> tuple[str, list[object]]:
    """WHERE fragment + params scoping ``events e`` to one root and (when known)
    its active branch. Falls back to root-only on a pre-branch store where the
    active branch is unknown, so the report still works."""
    if branch_id is None:
        return "e.root_id = ?", [root_id]
    return "e.root_id = ? AND e.branch_id = ?", [root_id, branch_id]


def _hbar(value: float, maximum: float, width: int = _BAR_WIDTH) -> str:
    """A smooth horizontal bar (eighth-block resolution) for ``value/maximum``."""
    if maximum <= 0 or value <= 0:
        return ""
    frac = min(1.0, value / maximum) * width
    full = int(frac)
    bar = "█" * full
    if full < width:  # add a partial cell for the remainder
        eighths = int(round((frac - full) * 8))
        if eighths:
            bar += _BAR_EIGHTHS[eighths]
    return bar


def _sparkline(values: list[int]) -> str:
    """An 8-level sparkline across ``values`` (empty input -> "")."""
    if not values:
        return ""
    hi = max(values)
    if hi <= 0:
        return _SPARK[0] * len(values)
    out: list[str] = []
    for v in values:
        if v <= 0:
            out.append(" ")  # a gap == no activity in that bucket
        else:
            out.append(_SPARK[int(round(v / hi * (len(_SPARK) - 1)))])
    return "".join(out)


def _bucketed(rows: list["X.sqlite3.Row"], buckets: int) -> list[int]:
    """Distribute per-event ``(ts, n)`` rows (ascending ts) into ``buckets``
    equal time bins spanning first..last event; return the summed ``n`` per bin.
    When every event shares a timestamp, everything lands in the last bin."""
    counts = [0] * buckets
    if not rows:
        return counts
    first = float(rows[0]["ts"])
    span = float(rows[-1]["ts"]) - first
    for r in rows:
        if span <= 0:
            idx = buckets - 1
        else:
            idx = min(buckets - 1, int((float(r["ts"]) - first) / span * buckets))
        counts[idx] += int(r["n"])
    return counts


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--top", "-n", default=15, show_default=True,
        help="How many rows to show in the volatile-files / coupled-pairs lists.",
    )
    def hotspots(top: int) -> None:
        """Code hotspots & change coupling from your recorded history."""
        top = max(1, top)
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            root_id = int(root["id"])
            branch = X.active_branch_id(conn, root_id)
            where, params = _scope(root_id, branch)

            # header (root + active timeline, mirroring `chronx du`)
            header = f"hotspots  {root['path']}"
            if branch is not None:
                b = X.dbm.get_branch(conn, branch)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)

            # ---- per-path volatility -------------------------------------- #
            # COUNT(DISTINCT e.id): "number of changing events" for each path.
            # Computed over ALL paths — it drives §1 and the coupling %% below.
            vol = list(
                conn.execute(
                    "SELECT d.path AS path, COUNT(DISTINCT e.id) AS changes, "
                    "       COALESCE(SUM(d.after_size), 0) AS churn "
                    "FROM deltas d JOIN events e ON e.id = d.event_id "
                    f"WHERE {where} "
                    "GROUP BY d.path",
                    params,
                )
            )
            if not vol:
                X.click.echo("  (no file changes recorded on this timeline yet)")
                return
            changes_by_path = {r["path"]: int(r["changes"]) for r in vol}

            # ---- §1 most volatile files ----------------------------------- #
            top_vol = sorted(
                vol,
                key=lambda r: (int(r["changes"]), int(r["churn"])),
                reverse=True,
            )[:top]
            max_changes = int(top_vol[0]["changes"])  # >= 1 (vol is non-empty)
            X.click.echo("")
            X.click.secho(
                f"most volatile files (top {len(top_vol)} of {len(vol)})", bold=True
            )
            for r in top_vol:
                ch = int(r["changes"])
                X.click.echo(
                    f"  {ch:>4} change{'s' if ch != 1 else ' '}"
                    f"  {X.human_bytes(int(r['churn'])):>10} churned"
                    f"  {_hbar(ch, max_changes):<{_BAR_WIDTH}}  {r['path']}"
                )

            # ---- §2 change coupling (files that change together) ---------- #
            # Pull (event, path) rows in event order and group per event in
            # Python; itertools.combinations turns each event's path SET into
            # unordered pairs. Huge events are skipped (capped) to stay cheap.
            cur = conn.execute(
                "SELECT e.id AS eid, d.path AS path "
                "FROM deltas d JOIN events e ON e.id = d.event_id "
                f"WHERE {where} "
                "ORDER BY e.id",
                params,
            )
            pair_counts: "collections.Counter[tuple[str, str]]" = collections.Counter()
            multi_file_events = 0  # events that touched >= 2 distinct paths
            skipped_big = 0        # of those, how many exceeded the pair cap
            for _eid, group in itertools.groupby(cur, key=lambda r: r["eid"]):
                paths = {r["path"] for r in group}
                if len(paths) < 2:
                    continue
                multi_file_events += 1
                if len(paths) > _MAX_PAIR_PATHS:
                    skipped_big += 1
                    continue
                for a, b in itertools.combinations(sorted(paths), 2):
                    pair_counts[(a, b)] += 1

            X.click.echo("")
            X.click.secho("change coupling (files that change together)", bold=True)
            if not pair_counts:
                if multi_file_events == 0:
                    X.click.echo(
                        "  (no command ever changed two files at once — "
                        "nothing to couple)"
                    )
                else:
                    X.click.echo("  (no coupled pairs)")
            else:
                top_pairs = pair_counts.most_common(top)
                cw = max(len(str(c)) for _p, c in top_pairs)  # co-count col width
                X.click.secho(
                    "  co-change  coupling  pair "
                    "(coupling% = co-changes / min(changes of either file))",
                    dim=True,
                )
                for (a, b), co in top_pairs:
                    # min(changes(a), changes(b)) is the true per-file change
                    # count, so co <= denom and the percentage is <= 100%.
                    denom = min(
                        changes_by_path.get(a, co), changes_by_path.get(b, co)
                    )
                    pct = (co / denom * 100.0) if denom else 0.0
                    X.click.echo(f"  {co:>{cw}}x       {pct:5.1f}%   {a}  <->  {b}")
            if skipped_big:
                X.click.secho(
                    f"  (note: {skipped_big} event(s) touching >{_MAX_PAIR_PATHS}"
                    " files were skipped for pairing)",
                    dim=True,
                )

            # ---- §3 churn over time (sparkline) --------------------------- #
            spark_rows = list(
                conn.execute(
                    "SELECT e.started_at AS ts, COUNT(d.id) AS n "
                    "FROM deltas d JOIN events e ON e.id = d.event_id "
                    f"WHERE {where} "
                    "GROUP BY e.id ORDER BY e.started_at ASC, e.id ASC",
                    params,
                )
            )
            if spark_rows:
                n_events = len(spark_rows)
                buckets = min(_SPARK_WIDTH, max(1, n_events))
                counts = _bucketed(spark_rows, buckets)
                total = sum(int(r["n"]) for r in spark_rows)
                first_ts = float(spark_rows[0]["ts"])
                last_ts = float(spark_rows[-1]["ts"])
                X.click.echo("")
                X.click.secho("churn over time", bold=True)
                X.click.echo(f"  {_sparkline(counts)}")
                span = X.fmt_ts(first_ts)
                if last_ts > first_ts:
                    span += f"  ..  {X.fmt_ts(last_ts)}"
                X.click.echo(
                    f"  {total} file-change{'s' if total != 1 else ''} across "
                    f"{n_events} event{'s' if n_events != 1 else ''}   [{span}]"
                )
        finally:
            conn.close()
