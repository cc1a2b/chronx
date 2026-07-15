"""chronx find — locate every recorded path matching a glob, dead or alive.

A forensic lookup over history: find EVERY file path that ever existed in the
store matching a glob — including files that were created and later deleted —
then report each one's lifetime (when it was created, how many times it
changed, when it was last touched) and whether it still exists today.

Answers "what happened to that file I deleted?". Scoped to the tracked root
containing the cwd, on that root's active timeline. Read-only over the store.
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any

from chronx import pluginlib as X


def _matches(path: str, pattern: str) -> bool:
    """True if PATTERN matches the full relative path or just its basename.

    Matching the basename too means ``*.py`` finds ``src/pkg/a.py`` — the
    intuitive behaviour for a shell-style glob typed without directory parts.
    """
    base = path.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern)


def register(main) -> None:
    @main.command()
    @X.click.argument("pattern")
    @X.click.option(
        "--deleted", is_flag=True, help="Only files that no longer exist."
    )
    def find(pattern: str, deleted: bool) -> None:
        """Find every recorded path matching a glob, dead or alive.

        PATTERN is an fnmatch glob tested against each path and its basename,
        so ``*.py`` matches ``src/app.py``. Reports each path's lifetime and
        whether it still exists; ``--deleted`` shows only vanished files.
        """
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # Walk every delta on this root's active timeline, oldest first,
            # so the first/last row seen per path is its first/last change.
            # A NULL branch_id (unmigrated store) means "scope by root only".
            where = "e.root_id = ?"
            params: list[object] = [root_id]
            if branch_id is not None:
                where += " AND e.branch_id = ?"
                params.append(branch_id)
            rows = conn.execute(
                "SELECT d.path AS path, d.change AS change,"
                " e.id AS ev_id, e.started_at AS started_at, e.command AS command"
                " FROM deltas d JOIN events e ON e.id = d.event_id"
                f" WHERE {where}"
                " ORDER BY d.path, e.id",
                params,
            )

            # Per-path lifetime: first touch, last touch, and change count.
            records: dict[str, dict[str, Any]] = {}
            for r in rows:
                rec = records.get(r["path"])
                if rec is None:
                    records[r["path"]] = {"first": r, "last": r, "changes": 1}
                else:
                    rec["last"] = r
                    rec["changes"] += 1

            # Baseline files (present at `chronx init`) may never appear in a
            # delta; fold them in so a tracked-but-untouched file is findable.
            for base_path in X.dbm.root_baseline(conn, root_id):
                records.setdefault(
                    base_path, {"first": None, "last": None, "changes": 0}
                )

            # Current existence: a path present with a real hash right now.
            state = X.state_at(conn, root_id, time.time())

            def currently_exists(path: str) -> bool:
                entry = state.get(path)
                return entry is not None and entry[0] is not None

            # Filter to the glob, then (optionally) to only-vanished files.
            matched = sorted(p for p in records if _matches(p, pattern))
            if deleted:
                matched = [p for p in matched if not currently_exists(p)]

            if not matched:
                scope = "deleted paths" if deleted else "recorded paths"
                X.click.secho(
                    f"no {scope} match {pattern!r} "
                    f"in {root['path']}", fg="yellow"
                )
                return

            exist_count = 0
            deleted_count = 0
            for path in matched:
                rec = records[path]
                alive = currently_exists(path)
                first = rec["first"]
                last = rec["last"]

                # Path line: green (alive) or struck-through red (gone), plus a
                # trailing status tag.
                if alive:
                    exist_count += 1
                    X.click.echo(
                        X.click.style(path, fg="green")
                        + "  "
                        + X.click.style("[exists]", fg="green", dim=True)
                    )
                else:
                    deleted_count += 1
                    if last is not None:
                        tag = (
                            f"[deleted @ #{last['ev_id']} "
                            f"{X.fmt_ts(last['started_at'])}]"
                        )
                    else:
                        tag = "[deleted]"
                    X.click.echo(
                        X.click.style(path, fg="red", strikethrough=True)
                        + "  "
                        + X.click.style(tag, fg="red")
                    )

                # Dim lifetime line beneath the path.
                if first is not None and last is not None:
                    detail = (
                        f"    created #{first['ev_id']}({first['change']}) "
                        f"{X.fmt_ts(first['started_at'])}"
                        f" · {rec['changes']} change(s)"
                        f" · last #{last['ev_id']}({last['change']}) "
                        f"{X.fmt_ts(last['started_at'])}"
                    )
                else:
                    # Baseline file never touched by any recorded command.
                    detail = "    tracked from baseline · 0 change(s)"
                X.click.secho(detail, dim=True)

            # Summary of the displayed set.
            X.click.echo(
                f"\n{len(matched)} path(s) matched "
                f"({exist_count} currently exist, {deleted_count} deleted)"
            )
        finally:
            conn.close()
