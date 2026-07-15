"""chronx dump — export recorded history as structured JSON for external tools.

Serialises the chronx store into a single JSON document so history can be fed
to ``jq``, analytics, or any other tooling. The export is *metadata only*: it
carries blob **hashes and sizes** but never blob CONTENT, so the output stays
small and safe to share.

For the tracked root containing the cwd (or every root with ``--all-roots``) it
emits each root's branches, marks, and full event log; every event carries its
file deltas (path + change kind + before/after hashes and sizes). Output is
deterministic — roots ordered by id, events by id, deltas by path — so the same
store always produces byte-identical JSON.

Read-only over the store. Imports only ``chronx.pluginlib`` (as X) plus the
stdlib, so it stays decoupled from ``cli.py`` and is auto-discovered from the
filesystem (no reinstall).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from chronx import pluginlib as X


def _chronx_version() -> str | None:
    """Best-effort installed chronx version (pure stdlib; None if unavailable)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("chronx")
        except PackageNotFoundError:
            return None
    except Exception:  # pragma: no cover - metadata machinery should not fail
        return None


def _rows(
    conn: "X.sqlite3.Connection", sql: str, params: "tuple[Any, ...]" = ()
) -> "list[X.sqlite3.Row]":
    """Run a read query, returning [] instead of raising on a broken/old store.

    Keeps the exporter from ever crashing on an empty or pre-migration store:
    a missing table surfaces as OperationalError, which we treat as "no rows".
    """
    try:
        return list(conn.execute(sql, params))
    except X.sqlite3.OperationalError:
        return []


def _root_json(row: "X.sqlite3.Row") -> "dict[str, Any]":
    """The raw ``roots`` columns, in a fixed key order (deterministic output)."""
    return {
        "id": row["id"],
        "path": row["path"],
        "added_at": row["added_at"],
        "active_branch_id": row["active_branch_id"],
    }


def _branch_json(row: "X.sqlite3.Row") -> "dict[str, Any]":
    """One branch's fields plus its derived event count / tip time.

    ``events`` and ``tip_ts`` come from ``list_branches`` (they aid analysis and
    are cheap); ``tip_ts`` is NULL for a branch that has no events yet.
    """
    keys = row.keys()
    return {
        "id": row["id"],
        "root_id": row["root_id"],
        "name": row["name"],
        "parent_branch_id": row["parent_branch_id"],
        "base_ts": row["base_ts"],
        "created_at": row["created_at"] if "created_at" in keys else None,
        "events": row["events"] if "events" in keys else None,
        "tip_ts": row["tip_ts"] if "tip_ts" in keys else None,
    }


def _mark_json(row: "X.sqlite3.Row") -> "dict[str, Any]":
    """One mark: its name, epoch, and a human-readable ISO timestamp."""
    return {
        "name": row["name"],
        "ts": row["ts"],
        "ts_iso": X.fmt_ts(row["ts"]),
    }


def _delta_json(delta: "X.dbm.Delta") -> "dict[str, Any]":
    """One file delta — hashes/sizes only, never blob content."""
    return {
        "path": delta.path,
        "change": delta.change,
        "before_hash": delta.before_hash,
        "after_hash": delta.after_hash,
        "before_size": delta.before_size,
        "after_size": delta.after_size,
    }


def _event_json(
    conn: "X.sqlite3.Connection", row: "X.sqlite3.Row"
) -> "dict[str, Any]":
    """One event with its deltas (ordered by path). ``command`` is NULL for an
    external/undo change; ``started_at_iso`` mirrors ``started_at`` for humans."""
    event_id = int(row["id"])
    try:
        deltas = X.dbm.deltas_for(conn, event_id)  # already ORDER BY path
    except X.sqlite3.OperationalError:
        deltas = []
    return {
        "id": event_id,
        "session": row["session"],
        "cwd": row["cwd"],
        "command": row["command"],
        "started_at": row["started_at"],
        "started_at_iso": X.fmt_ts(row["started_at"]),
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "branch_id": row["branch_id"],
        "deltas": [_delta_json(d) for d in deltas],
    }


def _select_roots(
    conn: "X.sqlite3.Connection", all_roots: bool
) -> "list[X.sqlite3.Row]":
    """The roots to export, ordered by id.

    ``--all-roots`` exports every root. Otherwise export only the root
    containing the cwd; but a wholly empty store (no roots at all) is not an
    error — it exports as ``{"roots": []}``.
    """
    if all_roots:
        return _rows(conn, "SELECT * FROM roots ORDER BY id")

    # Scoped: an empty store is fine (→ []), an untracked cwd is a clean error.
    if not _rows(conn, "SELECT id FROM roots LIMIT 1"):
        return []
    try:
        root = X.dbm.root_for_path(conn, X.Path.cwd())
    except X.sqlite3.OperationalError:
        root = None
    if root is None:
        raise X.click.ClickException(
            f"{X.Path.cwd()} is not inside any tracked directory; "
            "pass --all-roots to export the whole store"
        )
    return [root]


def register(main) -> None:  # type: ignore[no-untyped-def]
    @main.command()
    @X.click.option(
        "--output",
        "-o",
        type=X.click.Path(path_type=X.Path),
        default=None,
        help="Write JSON to FILE instead of stdout.",
    )
    @X.click.option(
        "--all-roots",
        is_flag=True,
        help="Export every tracked root, not just the cwd's.",
    )
    @X.click.option("--pretty", is_flag=True, help="Indented JSON.")
    @X.click.option(
        "--since",
        default=None,
        metavar="SPEC",
        help="Only events at/after SPEC (mark, event id, time, or relative).",
    )
    def dump(
        output: "X.Path | None",
        all_roots: bool,
        pretty: bool,
        since: str | None,
    ) -> None:
        """Export recorded history as structured JSON (metadata, no blob content)."""
        conn = X.open_db()
        try:
            # Resolve --since once (mark / event id / 'now' / time / relative).
            # X.moment_ts raises a clean ClickException on an unparseable spec.
            since_ts = X.moment_ts(conn, since) if since else None

            roots_out: list[dict[str, Any]] = []
            newest_ts: float | None = None  # max started_at across everything

            for root_row in _select_roots(conn, all_roots):
                root_id = int(root_row["id"])

                # Branches (deterministic: re-sort by id despite list_branches
                # ordering by created_at); tolerate a pre-branch store.
                try:
                    branch_rows = X.dbm.list_branches(conn, root_id)
                except X.sqlite3.OperationalError:
                    branch_rows = []
                branch_rows = sorted(branch_rows, key=lambda r: int(r["id"]))

                # Marks belonging to this root, oldest first (name breaks ties).
                mark_rows = _rows(
                    conn,
                    "SELECT name, ts, root_id FROM marks WHERE root_id = ?"
                    " ORDER BY ts, name",
                    (root_id,),
                )

                # Every event on this root (all branches), oldest id first,
                # optionally filtered to started_at >= the --since moment.
                ev_sql = (
                    "SELECT id, session, cwd, command, started_at, finished_at,"
                    " exit_code, branch_id FROM events WHERE root_id = ?"
                )
                ev_params: list[Any] = [root_id]
                if since_ts is not None:
                    ev_sql += " AND started_at >= ?"
                    ev_params.append(since_ts)
                ev_sql += " ORDER BY id"
                event_rows = _rows(conn, ev_sql, tuple(ev_params))

                for ev in event_rows:
                    ts = ev["started_at"]
                    if ts is not None and (newest_ts is None or ts > newest_ts):
                        newest_ts = ts

                root_obj = _root_json(root_row)
                root_obj["branches"] = [_branch_json(b) for b in branch_rows]
                root_obj["marks"] = [_mark_json(m) for m in mark_rows]
                root_obj["events"] = [_event_json(conn, ev) for ev in event_rows]
                roots_out.append(root_obj)

            # Top-level document: version, newest-event time (omitted when there
            # are no events at all), then the roots array.
            doc: dict[str, Any] = {"chronx_version": _chronx_version()}
            if newest_ts is not None:
                doc["exported_at_epoch"] = newest_ts
            doc["roots"] = roots_out
        finally:
            conn.close()

        # Compact by default (single line); --pretty indents. Keep our own key
        # order (it encodes the required determinism), so never sort_keys.
        if pretty:
            text = json.dumps(doc, indent=2, ensure_ascii=True)
        else:
            text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True)

        if output is None:
            X.click.echo(text)  # JSON only → pipes cleanly to jq / json.tool
        else:
            output.write_text(text + "\n", encoding="utf-8")
            # One-line confirmation to stderr, never stdout.
            print(
                f"wrote {output} "
                f"({len(text) + 1} bytes, {len(roots_out)} root(s))",
                file=sys.stderr,
            )
