"""chronx whatchanged — the complete change history of ONE file over time.

Like ``git log -p <file>``, but the "commits" are chronx events. For the
current root's active timeline this lists every recorded event that touched a
single path, newest-first by default, and for each one prints a header plus
either a one-line stat (default) or the full unified diff (``-p``/``--patch``).

It answers "how did this file get to be the way it is?" — including changes
that a plain ``git log`` never saw, because chronx records every command's file
effects regardless of whether they were ever committed. Deleted files work too:
the path need not exist on disk, since the history lives entirely in the store.

Strictly read-only: the object store and database are only ever read.
"""

from __future__ import annotations

import os

from chronx import pluginlib as X

# Per-change kind -> colour used for the single-letter A/M/D marker in headers.
_CHANGE_COLOR = {"A": "green", "M": "yellow", "D": "red"}


# --------------------------------------------------------------------- styling


def _style_diff_line(line: str) -> str:
    """Colorize one unified-diff line by its leading character (git's palette).

    File headers (``---``/``+++``) are bold, hunk/marker headers (``@@``) cyan,
    additions green, deletions red, context lines untouched. ``---``/``+++`` are
    tested *before* ``-``/``+`` so a header is never mistaken for a deletion or
    addition. ``render_delta`` also emits ``@@ ... @@`` notes for binary,
    truncated, or unavailable blobs; those land in the cyan branch and are
    simply printed as-is.
    """
    if line.startswith(("---", "+++")):
        return X.click.style(line, bold=True)
    if line.startswith("@@"):
        return X.click.style(line, fg="cyan")
    if line.startswith("+"):
        return X.click.style(line, fg="green")
    if line.startswith("-"):
        return X.click.style(line, fg="red")
    return line


def _change_mark(change: str) -> str:
    """The A/M/D change letter, coloured by kind."""
    return X.click.style(change, fg=_CHANGE_COLOR.get(change), bold=True)


def _command_str(row: "X.sqlite3.Row") -> str:
    """Render the producing command for a header line.

    A real command is shown ``$ <command>`` with internal whitespace collapsed
    so a multi-line command stays on one header line; changes chronx recorded
    without an owning command are shown as ``(external change)``.
    """
    cmd = row["command"]
    if cmd is None:
        return X.click.style("(external change)", dim=True)
    return X.click.style("$ ", dim=True) + " ".join(cmd.split())


def _header(row: "X.sqlite3.Row") -> str:
    """One header line: ``#<id>  <time>  <A/M/D>  $ <command>``."""
    eid = X.click.style(f"#{int(row['event_id'])}", fg="green", bold=True)
    when = X.fmt_ts(float(row["started_at"]))
    return f"{eid}  {when}  {_change_mark(row['change'])}  {_command_str(row)}"


# ----------------------------------------------------------------- data access


def _history_rows(
    conn: "X.sqlite3.Connection", root_id: int, rel: str, branch_id: int | None
) -> list["X.sqlite3.Row"]:
    """Every event that touched ``rel`` on this root's active timeline.

    Joined deltas+events for the path, newest-first by event id. When the store
    predates branching (``branch_id is None``) the branch filter is dropped so
    old single-timeline stores still read correctly.
    """
    sql = (
        "SELECT e.id AS event_id, e.started_at AS started_at, e.command AS command,"
        " d.change AS change, d.before_hash AS before_hash, d.after_hash AS after_hash,"
        " d.before_size AS before_size, d.after_size AS after_size,"
        " d.before_mode AS before_mode, d.after_mode AS after_mode"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE d.path = ? AND e.root_id = ?"
    )
    params: list[object] = [rel, root_id]
    if branch_id is not None:
        sql += " AND e.branch_id = ?"
        params.append(branch_id)
    sql += " ORDER BY e.id DESC"
    return list(conn.execute(sql, params))


def _delta_of(rel: str, row: "X.sqlite3.Row") -> "X.dbm.Delta":
    """Reconstruct the ``Delta`` for one history row (for stat/diff rendering)."""
    return X.dbm.Delta(
        path=rel,
        change=row["change"],
        before_hash=row["before_hash"],
        after_hash=row["after_hash"],
        before_size=row["before_size"],
        after_size=row["after_size"],
        before_mode=row["before_mode"],
        after_mode=row["after_mode"],
    )


# -------------------------------------------------------------------- rendering


def _emit_history(
    store: "X.ObjectStore",
    rel: str,
    rows: list["X.sqlite3.Row"],
    *,
    patch: bool,
    limit: int,
    reverse: bool,
) -> int:
    """Print the per-event history. Returns how many entries were shown.

    ``rows`` is newest-first. The newest ``limit`` are selected (as ``git log
    -n`` does), then displayed oldest-first when ``--reverse`` is given.
    """
    shown = rows[: max(limit, 0)]
    if reverse:
        shown = list(reversed(shown))

    order = "oldest first" if reverse else "newest first"
    X.click.secho(f"{rel} — {len(rows)} change(s), {order}", bold=True)
    X.click.echo()

    for row in shown:
        delta = _delta_of(rel, row)
        X.click.echo(_header(row))
        if patch:
            for line in X.render_delta(store, delta):
                X.click.echo("  " + _style_diff_line(line))
        else:
            X.click.echo("  " + X.stat_line(delta))
        X.click.echo()
    return len(shown)


def _emit_footer(
    rel: str,
    rows: list["X.sqlite3.Row"],
    *,
    in_baseline: bool,
    shown: int,
    limit: int,
) -> None:
    """Summarize: total changes, current existence, and origin (baseline?)."""
    total = len(rows)
    # ``rows`` is newest-first, so rows[0] is the most recent recorded state.
    # With no deltas the file only ever existed as its (unchanged) baseline.
    exists = (rows[0]["change"] != "D") if rows else in_baseline
    live = (
        f"{rel} currently exists"
        if exists
        else f"{rel} no longer exists (last change deleted it)"
    )
    origin = (
        "present in the baseline (tree at tracking start)"
        if in_baseline
        else "first appeared after tracking began"
    )

    X.click.secho(f"{total} change(s) to {rel}", bold=True)
    X.click.secho(f"  {live} · {origin}", dim=True)
    if total > shown:
        X.click.secho(
            f"  (showing the latest {shown} of {total} — raise -n to see more)",
            dim=True,
        )


# ------------------------------------------------------------------ registration


def register(main) -> None:
    @main.command()
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.option(
        "--patch", "-p", is_flag=True, help="Show full diffs, not just the stat."
    )
    @X.click.option(
        "--limit",
        "-n",
        default=30,
        show_default=True,
        help="Max number of changes to show (newest first).",
    )
    @X.click.option(
        "--reverse", is_flag=True, help="Oldest first (default: newest first)."
    )
    def whatchanged(file, patch: bool, limit: int, reverse: bool) -> None:  # type: ignore[no-untyped-def]
        """Show the complete change history of FILE, like ``git log -p <file>``.

        Lists every recorded event that touched FILE on the current directory's
        active timeline: a header per event plus a one-line stat, or the full
        unified diff with ``-p``. FILE need not still exist — a deleted file's
        history is reconstructed entirely from the store.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # Resolve FILE to a root-relative, forward-slash path key.
            rel = os.path.relpath(file.resolve(), root["path"]).replace(os.sep, "/")

            in_baseline = rel in X.dbm.root_baseline(conn, root_id)
            rows = _history_rows(conn, root_id, rel, branch_id)

            if not rows and not in_baseline:
                X.click.secho(
                    f"{rel}: never recorded — chronx has no history for this path "
                    "on the current timeline (created before tracking, ignored, "
                    "or the name/branch differs)",
                    fg="yellow",
                )
                return

            shown = _emit_history(
                store, rel, rows, patch=patch, limit=limit, reverse=reverse
            )
            _emit_footer(
                rel, rows, in_baseline=in_baseline, shown=shown, limit=limit
            )
        finally:
            conn.close()
