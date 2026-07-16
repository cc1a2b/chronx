"""chronx verify-store — a deep integrity & consistency audit of the store.

Where the built-in ``chronx fsck`` answers the narrow question "do the blobs on
disk decompress and re-hash, and is everything the log references present?",
``verify-store`` widens the lens to the *whole* store and prints a categorized
health report:

  1. blob integrity        every blob decompresses and re-hashes to its own name
                           (``--sample N`` re-hashes only the first N for speed)
  2. referential integrity every referenced hash exists on disk, and every
                           cross-table id (delta->event, *->root, event->branch)
                           resolves — dangling references are structural corruption
  3. manifest sanity       per root, every current manifest path points at a blob
                           that actually exists on disk
  4. delta well-formedness A / M / D rows carry the before/after hashes that their
                           change kind implies
  5. orphans / reclaimable blobs on disk that nothing references (``chronx gc``
                           reclaims them) and how many bytes they cost
  6. store metadata        recorded hash algorithm and schema version, flagged if
                           the schema predates this install

Each check prints PASS / WARN / FAIL; the command ends with an overall verdict
(HEALTHY / WARNINGS / CORRUPTION) and exits non-zero when anything FAILs.

Strictly read-only: opens the db read-only and never mutates the store. Built to
survive an empty *or* corrupt store — inspecting exactly that is the point — so
every query is defensive and a broken/missing table downgrades a check to WARN
rather than crashing.
"""

from __future__ import annotations

from chronx import pluginlib as X

# Hexdigests of the empty input for the two hash algorithms chronx can be built
# with. Hashing b"" lets us discover which algorithm THIS install computes and
# cross-check it against the algorithm recorded in the store's metadata.
_EMPTY_DIGESTS: dict[str, str] = {
    "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262": "blake3",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855": "sha256",
}

_MAX_EXAMPLES = 5   # how many offending rows/digests to name per check
_ROOT_SAMPLE = 50   # cap on roots inspected in the manifest-sanity section

_STATUS_FG = {"PASS": "green", "WARN": "yellow", "FAIL": "red", "INFO": "cyan"}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _short(digest: str) -> str:
    """A digest abbreviated for a human-readable report line."""
    return digest[:16] + "…" if len(digest) > 16 else digest


def _tag(status: str) -> str:
    """A padded, colored ``PASS`` / ``WARN`` / ``FAIL`` / ``INFO`` badge."""
    return X.click.style(f"{status:>4}", fg=_STATUS_FG.get(status, "white"), bold=True)


def _scalar(conn: "X.sqlite3.Connection", sql: str, params: tuple = ()) -> int | None:
    """Run a COUNT-style query and return its integer, or ``None`` if the query
    cannot run (missing table / corrupt db). Callers turn ``None`` into a WARN,
    so an unreadable store degrades gracefully instead of crashing."""
    try:
        row = conn.execute(sql, params).fetchone()
    except X.sqlite3.Error:
        return None
    return int(row[0]) if row is not None and row[0] is not None else 0


def _rows(conn: "X.sqlite3.Connection", sql: str, params: tuple = ()) -> list:
    """Best-effort row fetch; an empty list if the query cannot run."""
    try:
        return list(conn.execute(sql, params))
    except X.sqlite3.Error:
        return []


class _Audit:
    """Prints categorized check results and remembers how many WARN/FAIL fired,
    so the caller can compute the final verdict and exit code."""

    def __init__(self) -> None:
        self.fail = 0
        self.warn = 0

    def section(self, title: str) -> None:
        X.click.echo("")
        X.click.secho(title, bold=True)

    def result(self, status: str, msg: str) -> None:
        """Record and print one check outcome (PASS/WARN/FAIL/INFO)."""
        if status == "FAIL":
            self.fail += 1
        elif status == "WARN":
            self.warn += 1
        X.click.echo(f"  {_tag(status)}  {msg}")

    def detail(self, msg: str) -> None:
        """A dim, un-counted continuation line (examples / notes)."""
        X.click.secho(f"        {msg}", dim=True)


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command("verify-store")
    @X.click.option(
        "--sample",
        type=int,
        default=None,
        help="Only re-hash the first N blobs (fast integrity spot-check).",
    )
    def verify_store(sample: int | None) -> None:
        """Deep integrity & consistency audit of the whole object store."""
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        audit = _Audit()
        try:
            # ---- one pass over the object store -----------------------------
            # Catalogue every on-disk digest (for the set-math the later sections
            # rely on) and, in the SAME scan, re-hash blobs to detect corruption.
            # --sample caps only the expensive re-hash to the first N blobs while
            # still recording every digest, so the referential/orphan math below
            # stays exact regardless of sampling.
            disk_size: dict[str, int] = {}
            corrupt: list[str] = []
            rehashed = 0
            for digest, _path, size in store.iter_blobs():
                disk_size[digest] = size
                if sample is None or rehashed < sample:
                    rehashed += 1
                    try:
                        data = store.get(digest)
                    except (KeyError, ValueError):
                        # missing mid-scan, or fails to zlib-decompress
                        corrupt.append(digest)
                        continue
                    if X.hash_bytes(data) != digest:
                        corrupt.append(digest)
            disk_digests: set[str] = set(disk_size)

            # ---- what the metadata references -------------------------------
            # referenced_hashes covers deltas + manifest; baseline hashes are a
            # separate source we fold in for a complete referential picture.
            try:
                referenced = X.dbm.referenced_hashes(conn)
            except X.sqlite3.Error:
                referenced = set()
            baseline_hashes = {
                r[0]
                for r in _rows(conn, "SELECT DISTINCT hash FROM root_baseline")
                if r[0] is not None
            }
            all_referenced = referenced | baseline_hashes

            try:
                roots = X.dbm.get_roots(conn)
            except X.sqlite3.Error:
                roots = []

            # ---- empty store shortcut ---------------------------------------
            if not disk_digests and not all_referenced and not roots:
                X.click.secho("store is empty (nothing to verify)", fg="green")
                return

            X.click.secho("chronx deep store audit", bold=True)
            total = len(disk_digests)
            X.click.echo(
                f"object store: {total} blob{'s' if total != 1 else ''}, "
                f"{X.human_bytes(sum(disk_size.values()))} on disk; "
                f"{len(all_referenced)} referenced hash"
                f"{'es' if len(all_referenced) != 1 else ''}"
            )

            # ================= 1. blob integrity =============================
            audit.section("1. blob integrity  (decompress + re-hash to own name)")
            if total == 0:
                audit.result("INFO", "no blobs on disk")
            elif corrupt:
                audit.result(
                    "FAIL",
                    f"{len(corrupt)} of {rehashed} re-hashed blob(s) are corrupt "
                    f"(do not decompress / re-hash to their name)",
                )
                for d in corrupt[:_MAX_EXAMPLES]:
                    audit.detail(f"corrupt: {_short(d)}")
                if len(corrupt) > _MAX_EXAMPLES:
                    audit.detail(f"... and {len(corrupt) - _MAX_EXAMPLES} more")
            else:
                audit.result("PASS", f"all {rehashed} re-hashed blob(s) verify")
            if sample is not None and rehashed < total:
                audit.detail(
                    f"sampled {rehashed} of {total} blobs; "
                    "run without --sample for a full re-hash"
                )

            # ================= 2. referential integrity ======================
            audit.section("2. referential integrity  (references resolve)")
            # (a) every referenced blob must exist on disk
            missing = sorted(all_referenced - disk_digests)
            if missing:
                audit.result(
                    "FAIL",
                    f"{len(missing)} referenced blob(s) missing from disk "
                    "(their history cannot be shown or restored)",
                )
                for d in missing[:_MAX_EXAMPLES]:
                    audit.detail(f"missing: {_short(d)}")
                if len(missing) > _MAX_EXAMPLES:
                    audit.detail(f"... and {len(missing) - _MAX_EXAMPLES} more")
            else:
                audit.result(
                    "PASS",
                    f"all {len(all_referenced)} referenced blob(s) present on disk",
                )

            # (b) dangling cross-table ids — structural corruption. Each tuple is
            #     (label, FROM/JOIN clause, "orphan" predicate).
            fk_checks = [
                ("delta -> event",
                 "deltas d LEFT JOIN events e ON e.id = d.event_id",
                 "e.id IS NULL"),
                ("event -> root",
                 "events x LEFT JOIN roots r ON r.id = x.root_id",
                 "r.id IS NULL"),
                ("manifest -> root",
                 "manifest m LEFT JOIN roots r ON r.id = m.root_id",
                 "r.id IS NULL"),
                ("branch -> root",
                 "branches b LEFT JOIN roots r ON r.id = b.root_id",
                 "r.id IS NULL"),
                ("baseline -> root",
                 "root_baseline rb LEFT JOIN roots r ON r.id = rb.root_id",
                 "r.id IS NULL"),
                ("event -> branch",
                 "events x LEFT JOIN branches b ON b.id = x.branch_id",
                 "x.branch_id IS NOT NULL AND b.id IS NULL"),
            ]
            for label, join, cond in fk_checks:
                n = _scalar(conn, f"SELECT COUNT(*) FROM {join} WHERE {cond}")
                if n is None:
                    audit.result("WARN", f"{label}: could not check (table missing?)")
                elif n:
                    audit.result("FAIL", f"{label}: {n} orphaned row(s)")
                else:
                    audit.result("PASS", f"{label}: no orphans")

            # ================= 3. manifest sanity ============================
            audit.section("3. manifest sanity  (current state resolves to blobs)")
            if not roots:
                audit.result("INFO", "no tracked roots")
            else:
                sample_roots = roots[:_ROOT_SAMPLE]
                any_missing = False
                for root in sample_roots:
                    rid = int(root["id"])
                    entries = _rows(
                        conn,
                        "SELECT path, hash FROM manifest WHERE root_id = ?",
                        (rid,),
                    )
                    miss = [e for e in entries if e["hash"] not in disk_digests]
                    if miss:
                        any_missing = True
                        audit.result(
                            "FAIL",
                            f"{root['path']}: {len(miss)} of {len(entries)} "
                            "manifest path(s) point at a missing blob",
                        )
                        for e in miss[:_MAX_EXAMPLES]:
                            audit.detail(f"{e['path']}  ->  {_short(e['hash'])}")
                if not any_missing:
                    audit.result(
                        "PASS",
                        f"every manifest entry across {len(sample_roots)} "
                        f"root(s) resolves to a stored blob",
                    )
                if len(roots) > _ROOT_SAMPLE:
                    audit.detail(f"checked first {_ROOT_SAMPLE} of {len(roots)} roots")

            # ================= 4. delta well-formedness ======================
            audit.section("4. delta well-formedness  (A/M/D hash shape)")
            # (kind, "malformed" predicate, why it's wrong)
            delta_rules = [
                ("A",
                 "change = 'A' AND (before_hash IS NOT NULL OR after_hash IS NULL)",
                 "add rows must have no before_hash and an after_hash"),
                ("D",
                 "change = 'D' AND after_hash IS NOT NULL",
                 "delete rows must have no after_hash"),
                ("M",
                 "change = 'M' AND (before_hash IS NULL OR after_hash IS NULL)",
                 "modify rows must have both before_hash and after_hash"),
            ]
            for kind, cond, why in delta_rules:
                n = _scalar(conn, f"SELECT COUNT(*) FROM deltas WHERE {cond}")
                if n is None:
                    audit.result("WARN", f"'{kind}' deltas: could not check")
                elif n:
                    # shape violations are suspicious but non-fatal -> WARN
                    audit.result("WARN", f"{n} malformed '{kind}' delta(s) — {why}")
                    for e in _rows(
                        conn,
                        f"SELECT id, path FROM deltas WHERE {cond} "
                        f"ORDER BY id LIMIT {_MAX_EXAMPLES}",
                    ):
                        audit.detail(f"delta #{e['id']}  {e['path']}")
                else:
                    audit.result("PASS", f"all '{kind}' deltas well-formed")

            # ================= 5. orphans / reclaimable ======================
            audit.section("5. orphans / reclaimable  (informational)")
            orphans = disk_digests - all_referenced
            if orphans:
                freed = sum(disk_size.get(d, 0) for d in orphans)
                audit.result(
                    "INFO",
                    f"{len(orphans)} unreferenced blob(s), {X.human_bytes(freed)} "
                    "on disk — reclaim with `chronx gc`",
                )
            else:
                audit.result("PASS", "no unreferenced blobs")

            # ================= 6. store metadata =============================
            audit.section("6. store metadata")
            meta = {r["key"]: r["value"] for r in _rows(conn, "SELECT key, value FROM meta")}

            algo = meta.get("hash_algo")
            install_algo = _EMPTY_DIGESTS.get(X.hash_bytes(b""))
            if algo is None:
                audit.result("WARN", "hash_algo not recorded in meta")
            elif install_algo is not None and algo != install_algo:
                audit.result(
                    "WARN",
                    f"hash_algo is {algo!r} but this install hashes with "
                    f"{install_algo!r} — every blob will look corrupt",
                )
            else:
                audit.result("PASS", f"hash_algo = {algo}")

            sv_raw = meta.get("schema_version")
            try:
                sv = int(sv_raw) if sv_raw is not None else None
            except (TypeError, ValueError):
                sv = None
            if sv is None:
                audit.result("WARN", "schema_version missing or unreadable")
            elif sv < X.dbm.SCHEMA_VERSION:
                audit.result(
                    "WARN",
                    f"schema_version {sv} < current {X.dbm.SCHEMA_VERSION} "
                    "(run any chronx command to migrate)",
                )
            else:
                audit.result("PASS", f"schema_version = {sv} (current)")

            # ================= final verdict =================================
            X.click.echo("")
            if audit.fail:
                X.click.secho(
                    f"verdict: CORRUPTION — {audit.fail} failure(s), "
                    f"{audit.warn} warning(s)",
                    fg="red",
                    bold=True,
                )
                raise X.click.exceptions.Exit(1)
            if audit.warn:
                X.click.secho(
                    f"verdict: WARNINGS — {audit.warn} warning(s), no failures",
                    fg="yellow",
                    bold=True,
                )
            else:
                X.click.secho(
                    "verdict: HEALTHY — all checks passed", fg="green", bold=True
                )
        finally:
            conn.close()
