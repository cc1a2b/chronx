"""chronx sql — a power-user, read-only SQL console over the chronx store.

Exposes the underlying sqlite store to anyone who wants to slice history with
raw SQL: ``chronx sql "SELECT ..."`` runs an arbitrary query and prints an
aligned text table (or ``--json`` for machine consumption). ``--schema`` dumps
each table's CREATE statement plus a few example queries; ``--tables`` lists
every table with its row count.

READ-ONLY, TWO WAYS. The connection comes from ``X.open_db()``, which opens the
db with ``mode=ro`` — a genuinely read-only sqlite handle, so any write (even a
sneaky ``WITH ... INSERT``) raises ``OperationalError`` and never touches the
store. On top of that we reject anything whose first keyword is not
SELECT/WITH/EXPLAIN/PRAGMA up front, so the common "oops I typed UPDATE" case
gets a clean, immediate error instead of a sqlite one.

Store schema (as documented by ``--schema`` at runtime):
    meta(key, value)
    roots(id, path, added_at, active_branch_id)
    branches(id, root_id, name, parent_branch_id, base_ts, created_at)
    root_baseline(root_id, path, hash, size, mode)
    events(id, session, root_id, cwd, command, started_at, finished_at,
           exit_code, branch_id)
    deltas(id, event_id, path, change, before_hash, after_hash,
           before_size, after_size, before_mode, after_mode)
    manifest(root_id, path, hash, size, mtime, mode)
    marks(id, name, ts, root_id, created_at)

Imports only ``chronx.pluginlib`` (as X) plus the stdlib, so it stays decoupled
from ``cli.py`` and is auto-discovered from the filesystem (no reinstall).
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from chronx import pluginlib as X

# Statement kinds we permit. Everything else (INSERT/UPDATE/DELETE/DROP/...) is
# rejected before we ever hand the text to sqlite.
_ALLOWED = ("SELECT", "WITH", "EXPLAIN", "PRAGMA")

# Copy-paste-ready examples, reused by both --schema's footer and the bare
# ``chronx sql`` usage hint. (label, sql).
_EXAMPLES: "list[tuple[str, str]]" = [
    (
        "top-changed paths",
        "SELECT path, COUNT(*) AS changes FROM deltas "
        "GROUP BY path ORDER BY changes DESC LIMIT 10",
    ),
    (
        "slowest commands (wall-clock seconds)",
        "SELECT command, finished_at - started_at AS secs FROM events "
        "WHERE finished_at IS NOT NULL ORDER BY secs DESC LIMIT 10",
    ),
    (
        "events per day",
        "SELECT strftime('%Y-%m-%d', started_at, 'unixepoch') AS day, "
        "COUNT(*) AS n FROM events GROUP BY day ORDER BY day",
    ),
]

_MAX_COL = 60  # cap any column's display width; longer cells are truncated.


def _leading_keyword(sql: str) -> str:
    """First SQL keyword (upper-cased) after skipping leading whitespace and
    both comment styles (``-- line`` and ``/* block */``). Returns "" if the
    statement is empty/all-comments."""
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):  # line comment → skip to newline
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
        elif sql.startswith("/*", i):  # block comment → skip past */
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            break
    start = i
    while i < n and (sql[i].isalpha() or sql[i] == "_"):
        i += 1
    return sql[start:i].upper()


def _cell(v: Any) -> str:
    """Raw string for a table cell. NULL renders as empty (matches the sqlite
    shell); every other value is shown verbatim via ``str`` — no timestamp
    prettifying, so output is predictable."""
    return "" if v is None else str(v)


def _clip(s: str, width: int) -> str:
    """Truncate ``s`` to ``width`` chars, marking the cut with an ellipsis."""
    return s if len(s) <= width else s[: width - 1] + "…"


def _user_tables(conn: "X.sqlite3.Connection") -> "list[X.sqlite3.Row]":
    """(name, sql) for every user table, skipping sqlite_* internals."""
    return list(
        conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    )


def _print_examples() -> None:
    """Dim, copy-pasteable example queries (shared by --schema and the hint)."""
    X.click.echo(X.click.style("example queries:", dim=True))
    for label, sql in _EXAMPLES:
        X.click.echo(X.click.style(f"  # {label}", dim=True))
        X.click.echo(X.click.style(f'  chronx sql "{sql}"', dim=True))


def _emit_schema(conn: "X.sqlite3.Connection", as_json: bool) -> None:
    """--schema: each table's CREATE statement, plus example queries."""
    tables = _user_tables(conn)
    if as_json:
        X.click.echo(
            json.dumps(
                [{"name": r["name"], "sql": r["sql"]} for r in tables],
                indent=2,
                default=str,
            )
        )
        return
    if not tables:
        X.click.echo("(store has no tables yet)")
        return
    for i, row in enumerate(tables):
        if i:
            X.click.echo("")  # blank line between definitions
        X.click.echo(X.click.style(f"-- table: {row['name']}", dim=True))
        # sql is the exact CREATE statement recorded by sqlite.
        X.click.echo((row["sql"] or "").strip() + ";")
    X.click.echo("")
    _print_examples()


def _emit_tables(conn: "X.sqlite3.Connection", as_json: bool) -> None:
    """--tables: every user table with a guarded COUNT(*)."""
    counts: "list[tuple[str, int | None]]" = []
    for row in _user_tables(conn):
        name = row["name"]
        try:
            # name is a trusted schema identifier; quote it defensively anyway.
            n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except X.sqlite3.Error:
            n = None  # unreadable/broken table → report "?" rather than crash
        counts.append((name, n))

    if as_json:
        X.click.echo(
            json.dumps(
                [{"name": nm, "rows": n} for nm, n in counts],
                indent=2,
                default=str,
            )
        )
        return
    if not counts:
        X.click.echo("(store has no tables yet)")
        return
    width = max(len(nm) for nm, _ in counts)
    for nm, n in counts:
        shown = "?" if n is None else str(n)
        X.click.echo(f"{nm.ljust(width)}  {shown}")
    X.click.echo(X.click.style(f"({len(counts)} tables)", dim=True))


def _emit_query_text(
    cols: "Sequence[str]", rows: "Sequence[Sequence[Any]]", truncated: bool, limit: int
) -> None:
    """Aligned text table: header, rule, rows, and a row-count footer."""
    ncols = len(cols)
    # Pre-stringify every cell once; compute per-column width capped at _MAX_COL.
    str_rows = [[_cell(r[i]) for i in range(ncols)] for r in rows]
    widths = []
    for i, col in enumerate(cols):
        w = len(col)
        for sr in str_rows:
            w = max(w, len(sr[i]))
        widths.append(min(w, _MAX_COL))

    def render(vals: "Sequence[str]") -> str:
        cells = (_clip(vals[i], widths[i]).ljust(widths[i]) for i in range(ncols))
        return "  ".join(cells).rstrip()

    X.click.echo(render(list(cols)))
    X.click.echo("  ".join("-" * w for w in widths).rstrip())
    for sr in str_rows:
        X.click.echo(render(sr))

    n = len(str_rows)
    footer = f"({n} row{'' if n == 1 else 's'})"
    if truncated:
        footer = (
            f"({n} rows shown — truncated at --limit {limit}; "
            "raise with -n or -n 0 for all)"
        )
    X.click.echo(X.click.style(footer, dim=True))


def register(main) -> None:  # type: ignore[no-untyped-def]
    @main.command()
    @X.click.argument("query", required=False)
    @X.click.option("--json", "as_json", is_flag=True, help="JSON output.")
    @X.click.option(
        "--schema", is_flag=True, help="Show the table schemas and exit."
    )
    @X.click.option("--tables", is_flag=True, help="List tables with row counts.")
    @X.click.option(
        "--limit", "-n", default=200, show_default=True, help="Max rows shown."
    )
    def sql(
        query: "str | None",
        as_json: bool,
        schema: bool,
        tables: bool,
        limit: int,
    ) -> None:
        """Read-only SQL console over the chronx store."""
        conn = X.open_db()  # mode=ro handle — writes physically cannot happen
        try:
            # Metadata modes take precedence and short-circuit.
            if schema:
                _emit_schema(conn, as_json)
                return
            if tables:
                _emit_tables(conn, as_json)
                return

            # Bare `chronx sql` with nothing to do → a short usage hint (exit 0).
            if not query or not query.strip():
                X.click.echo(
                    "chronx sql — read-only SQL console over the chronx store"
                )
                X.click.echo(
                    "usage: chronx sql [QUERY]   "
                    "(only SELECT/WITH/EXPLAIN/PRAGMA)"
                )
                X.click.echo(
                    "  --schema   show table definitions   "
                    "--tables  list tables + row counts"
                )
                X.click.echo("")
                _print_examples()
                return

            # Gatekeep: reject obvious non-read statements before hitting sqlite.
            if _leading_keyword(query) not in _ALLOWED:
                raise X.click.ClickException(
                    "read-only: only SELECT/WITH/EXPLAIN/PRAGMA queries "
                    "are allowed"
                )

            # Execute. Any sqlite error (bad SQL, a write blocked by mode=ro,
            # a missing table) becomes a clean, single-line message.
            try:
                cur = conn.execute(query)
            except X.sqlite3.Error as exc:
                raise X.click.ClickException(str(exc)) from exc

            # Statements with no result set (some PRAGMAs) have no description.
            if cur.description is None:
                X.click.echo("[]" if as_json else "(0 rows)")
                return

            cols = [d[0] for d in cur.description]

            # Respect --limit; fetch one extra row to detect truncation.
            # -n 0 (or negative) means "no cap".
            if limit and limit > 0:
                rows = cur.fetchmany(limit + 1)
                truncated = len(rows) > limit
                if truncated:
                    rows = rows[:limit]
            else:
                rows = cur.fetchall()
                truncated = False

            if as_json:
                objs = [dict(zip(cols, tuple(r))) for r in rows]
                X.click.echo(json.dumps(objs, indent=2, default=str))
                return

            if not rows:
                # Still show the header so the columns are visible, then note it.
                _emit_query_text(cols, [], False, limit)
                return
            _emit_query_text(cols, rows, truncated, limit)
        finally:
            conn.close()
