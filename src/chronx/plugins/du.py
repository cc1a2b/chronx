"""chronx du — storage analytics: "where does my chronx store go?".

A read-only report over the object store + sqlite metadata. For the cwd's root
(or every root with ``--all-roots``) it explains where bytes go and how much
content-addressing + zlib compression save you:

  * physical store   — unique blobs and on-disk (compressed) bytes
  * storage efficiency — naive (every write, uncompressed) vs logical
                         (deduplicated) vs on-disk, and the savings ratios
  * largest blobs    — the biggest unique blobs and what references them
  * hottest paths    — the paths that churned the most bytes
  * timelines        — per-branch event / delta / distinct-blob counts

Blobs on disk are zlib-compressed, so ``iter_blobs`` sizes are *compressed*
sizes, while ``deltas.after_size`` / ``root_baseline.size`` are *logical*
(uncompressed) sizes. This command leans on both to tell the whole story.

Read-only: opens the db read-only and never mutates the store.
"""

from __future__ import annotations

from chronx import pluginlib as X


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _ratio(numer: int, denom: int) -> str:
    """A `2.34x` style savings ratio, or `n/a` when the denominator is zero."""
    return f"{numer / denom:.2f}x" if denom else "n/a"


def _logical_by_digest(conn: "X.sqlite3.Connection", root_id: int) -> dict[str, int]:
    """Map every distinct blob digest this root references -> its logical size.

    Sources are the ``after_hash``/``after_size`` of the root's deltas plus the
    ``hash``/``size`` of its baseline. Identical content shares a digest, so the
    dict is naturally deduplicated. Sizes for a given digest should agree; we
    keep the max defensively.
    """
    sizes: dict[str, int] = {}
    for r in conn.execute(
        "SELECT d.after_hash AS h, d.after_size AS s "
        "FROM deltas d JOIN events e ON e.id = d.event_id "
        "WHERE e.root_id = ? AND d.after_hash IS NOT NULL "
        "  AND d.after_size IS NOT NULL",
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


def _naive_total(conn: "X.sqlite3.Connection", root_id: int) -> int:
    """Bytes the root would cost WITHOUT dedup — every recorded write counted.

    = sum of ``after_size`` over all content-bearing deltas + baseline sizes.
    Repeated / duplicated content is intentionally counted every time.
    """
    d = conn.execute(
        "SELECT COALESCE(SUM(d.after_size), 0) AS t "
        "FROM deltas d JOIN events e ON e.id = d.event_id "
        "WHERE e.root_id = ? AND d.after_hash IS NOT NULL",
        (root_id,),
    ).fetchone()["t"]
    b = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS t FROM root_baseline WHERE root_id = ?",
        (root_id,),
    ).fetchone()["t"]
    return int(d) + int(b)


def _blob_refs(
    conn: "X.sqlite3.Connection", root_id: int, digest: str
) -> tuple[list[str], dict[str, float] | None]:
    """Return (sorted referencing paths, earliest introducing event | None).

    Paths come from both deltas (``after_hash``) and the baseline; the event is
    the earliest delta that produced this content (``None`` when the blob only
    appears in the baseline).
    """
    paths: set[str] = set()
    earliest: dict[str, float] | None = None
    for r in conn.execute(
        "SELECT d.path AS path, e.id AS eid, e.started_at AS ts "
        "FROM deltas d JOIN events e ON e.id = d.event_id "
        "WHERE e.root_id = ? AND d.after_hash = ? ORDER BY e.started_at",
        (root_id, digest),
    ):
        paths.add(r["path"])
        if earliest is None:
            earliest = {"eid": float(r["eid"]), "ts": float(r["ts"])}
    for r in conn.execute(
        "SELECT path FROM root_baseline WHERE root_id = ? AND hash = ?",
        (root_id, digest),
    ):
        paths.add(r["path"])
    return sorted(paths), earliest


def _fmt_ref(paths: list[str], event: dict[str, float] | None) -> str:
    """`path (+N more)  [#event, time]` for a blob's largest-list line."""
    head = paths[0] if paths else "(unreferenced)"
    if len(paths) > 1:
        head += f"  (+{len(paths) - 1} more path{'s' if len(paths) > 2 else ''})"
    if event is not None:
        head += f"  [#{int(event['eid'])} {X.fmt_ts(event['ts'])}]"
    else:
        head += "  [baseline]"
    return head


# --------------------------------------------------------------------------- #
# per-root report
# --------------------------------------------------------------------------- #
def _report_root(
    conn: "X.sqlite3.Connection",
    disk_by_digest: dict[str, int],
    root: "X.sqlite3.Row",
    top: int,
) -> None:
    root_id = int(root["id"])

    # header, annotated with the active timeline if there is one
    header = f"root  {root['path']}"
    active_id = X.dbm.active_branch_id(conn, root_id)
    if active_id is not None:
        active = X.dbm.get_branch(conn, active_id)
        if active is not None:
            header += f"   [active timeline: {active['name']}]"
    X.click.secho(header, fg="cyan", bold=True)

    logical = _logical_by_digest(conn, root_id)
    if not logical:
        X.click.echo("  (no recorded content yet)")
        return

    # ---- storage efficiency: naive vs logical vs on-disk --------------------
    logical_total = sum(logical.values())
    naive_total = _naive_total(conn, root_id)
    physical_total = sum(disk_by_digest.get(d, 0) for d in logical)
    missing = sum(1 for d in logical if d not in disk_by_digest)

    X.click.secho("  storage efficiency", bold=True)
    X.click.echo(
        f"    naive   (every write, uncompressed): {X.human_bytes(naive_total):>10}"
    )
    X.click.echo(
        f"    logical (deduplicated):              {X.human_bytes(logical_total):>10}"
        f"   {len(logical)} unique blob{'s' if len(logical) != 1 else ''}"
    )
    ondisk_line = (
        f"    on disk (dedup + compressed):        {X.human_bytes(physical_total):>10}"
    )
    if missing:
        ondisk_line += f"   ({missing} blob(s) missing on disk)"
    X.click.echo(ondisk_line)
    X.click.secho(
        f"    content-addressing saves {_ratio(naive_total, logical_total)},"
        f" compression {_ratio(logical_total, physical_total)},"
        f" overall {_ratio(naive_total, physical_total)}",
        dim=True,
    )

    # ---- largest unique blobs by logical size -------------------------------
    top_blobs = sorted(logical.items(), key=lambda kv: kv[1], reverse=True)[:top]
    if top_blobs:
        X.click.secho(f"  largest blobs (top {len(top_blobs)} by logical size)", bold=True)
        for digest, size in top_blobs:
            paths, event = _blob_refs(conn, root_id, digest)
            X.click.echo(f"    {X.human_bytes(size):>10}  {_fmt_ref(paths, event)}")

    # ---- hottest paths by storage churn -------------------------------------
    churn = list(
        conn.execute(
            "SELECT d.path AS path, COUNT(*) AS changes, "
            "       COALESCE(SUM(d.after_size), 0) AS total "
            "FROM deltas d JOIN events e ON e.id = d.event_id "
            "WHERE e.root_id = ? "
            "GROUP BY d.path ORDER BY total DESC, changes DESC LIMIT ?",
            (root_id, top),
        )
    )
    if churn:
        X.click.secho(f"  hottest paths by churn (top {len(churn)})", bold=True)
        for r in churn:
            changes = int(r["changes"])
            X.click.echo(
                f"    {X.human_bytes(int(r['total'])):>10}  "
                f"{changes:>4} change{'s' if changes != 1 else ' '}  {r['path']}"
            )

    # ---- per-timeline (branch) breakdown ------------------------------------
    X.click.secho("  timelines  (events / deltas / distinct blobs)", bold=True)
    printed = False
    for b in X.dbm.list_branches(conn, root_id):
        bid = int(b["id"])
        stats = conn.execute(
            "SELECT COUNT(*) AS deltas, COUNT(DISTINCT d.after_hash) AS blobs "
            "FROM deltas d JOIN events e ON e.id = d.event_id "
            "WHERE e.branch_id = ?",
            (bid,),
        ).fetchone()
        mark = "*" if active_id == bid else " "
        X.click.echo(
            f"    {mark} {str(b['name']):<16} "
            f"events={int(b['events']):<4} "
            f"deltas={int(stats['deltas']):<4} "
            f"blobs={int(stats['blobs'])}"
        )
        printed = True

    # events recorded before/outside any branch (branch_id IS NULL)
    null_stats = conn.execute(
        "SELECT COUNT(DISTINCT e.id) AS events, COUNT(d.id) AS deltas, "
        "       COUNT(DISTINCT d.after_hash) AS blobs "
        "FROM events e LEFT JOIN deltas d ON d.event_id = e.id "
        "WHERE e.root_id = ? AND e.branch_id IS NULL",
        (root_id,),
    ).fetchone()
    if int(null_stats["events"]) > 0:
        X.click.echo(
            f"      {'(unbranched)':<16} "
            f"events={int(null_stats['events']):<4} "
            f"deltas={int(null_stats['deltas']):<4} "
            f"blobs={int(null_stats['blobs'])}"
        )
        printed = True
    if not printed:
        X.click.echo("    (none)")


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--all-roots", is_flag=True, help="Report every tracked root, not just the cwd's."
    )
    @X.click.option(
        "--top", default=12, show_default=True,
        help="How many rows to show in the largest-blobs / hottest-paths lists.",
    )
    def du(all_roots: bool, top: int) -> None:
        """Storage analytics: where your chronx object store goes."""
        top = max(0, top)
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # One scan of the store: digest -> on-disk (compressed) size. This is
            # exactly the data behind store.disk_usage(), reused for per-root
            # physical totals below (blobs are global / shared across roots).
            disk_by_digest = {dg: size for dg, _path, size in store.iter_blobs()}
            store_count = len(disk_by_digest)
            store_total = sum(disk_by_digest.values())

            X.click.secho("chronx storage analytics", bold=True)
            X.click.echo(
                f"object store (global): {store_count} blob"
                f"{'s' if store_count != 1 else ''}, "
                f"{X.human_bytes(store_total)} on disk (compressed)"
            )

            roots = X.dbm.get_roots(conn)
            if not roots:
                X.click.echo("")
                X.click.echo("no tracked roots yet — run `chronx init` in a project")
                return

            targets = roots if all_roots else [X.root_for_cwd(conn)]
            for root in targets:
                X.click.echo("")
                _report_root(conn, disk_by_digest, root, top)
        finally:
            conn.close()
