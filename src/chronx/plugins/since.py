"""chronx since <moment> — the NET effect of everything since a moment.

Answers "what has changed since I started this task / since this morning /
since mark X?". Unlike ``chronx diff`` between two specific events, this shows
the *cumulative* net difference from ``<moment>`` all the way to *now*: every
recorded change on the current directory's active timeline is collapsed so that
a file created and then deleted inside the window cancels out and never shows,
and a file touched five times shows a single before→after diff.

    chronx since <moment>            full net diffs (git-style, colorized)
    chronx since <moment> --stat     one A/M/D line per file + a totals line
    chronx since <moment> --commands also list the commands responsible

``<moment>`` is anything :func:`pluginlib.moment_ts` understands — a mark name,
an event id (``#42`` or ``42``), ``now``, or a natural time ("this morning",
"2h ago"). The net diff comes from :func:`ops.range_changes`, which reconstructs
the tree state at the moment and at now and diffs the two.

Strictly read-only: the object store and database are only ever read.
"""

from __future__ import annotations

import time

from chronx import pluginlib as X
from chronx.ops import range_changes

# Per-change kind -> colour used for the single-letter A/M/D marker (git's palette).
_CHANGE_COLOR = {"A": "green", "M": "yellow", "D": "red"}


# --------------------------------------------------------------------- styling


def _style_diff_line(line: str) -> str:
    """Colorize one unified-diff line by its leading character.

    File headers (``---``/``+++``) are bold, hunk/marker headers (``@@``) cyan,
    additions green, deletions red, context lines untouched. ``---``/``+++`` are
    tested *before* ``-``/``+`` so a header is never mistaken for a change line.
    ``render_delta`` also emits ``@@ ... @@`` notes for binary, truncated, or
    unavailable blobs; those land in the cyan branch and print as-is.
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


def _stat_line(delta: "X.dbm.Delta") -> str:
    """Colorized one-line stat: coloured A/M/D marker + path + size delta.

    Reuses :func:`pluginlib.stat_line` (``"M  path  (312 -> 340 bytes)"``) and
    only recolours the leading change letter, so the format stays consistent
    with the rest of chronx.
    """
    line = X.stat_line(delta)
    return _change_mark(line[:1]) + line[1:]


def _command_line(row: "X.sqlite3.Row") -> str:
    """``#id  time  $ command`` for one event (external changes shown dim).

    A recorded command is shown ``$ <command>`` with internal whitespace
    collapsed so a multi-line command stays on one line; changes chronx captured
    without an owning command render as a dim ``(external change)``.
    """
    eid = X.click.style(f"#{int(row['id'])}", fg="green", bold=True)
    when = X.click.style(X.fmt_ts(float(row["started_at"])), fg="cyan")
    cmd = row["command"]
    if cmd is None:
        body = X.click.style("(external change)", dim=True)
    else:
        body = X.click.style("$ ", dim=True) + " ".join(cmd.split())
    return f"{eid}  {when}  {body}"


# ------------------------------------------------------------------- rendering


def _counts(changes: list) -> dict[str, int]:
    """Tally net changes by kind (every RangeChange is exactly one of A/M/D)."""
    out = {"A": 0, "M": 0, "D": 0}
    for change in changes:
        out[change.as_delta().change] += 1
    return out


def _emit_stat(changes: list) -> None:
    """One coloured stat line per net change, then a totals line."""
    for change in changes:
        X.click.echo("  " + _stat_line(change.as_delta()))
    c = _counts(changes)
    X.click.echo()
    X.click.secho(
        f"{len(changes)} file(s) changed  "
        f"(+{c['A']} added, ~{c['M']} modified, -{c['D']} deleted)",
        dim=True,
    )


def _emit_diffs(store: "X.ObjectStore", changes: list) -> None:
    """The full colorized unified diff for each net change.

    ``render_delta`` degrades gracefully on binary or missing blobs (it emits a
    ``@@ ... @@`` marker instead of a body), so this never crashes on them.
    """
    for change in changes:
        for line in X.render_delta(store, change.as_delta()):
            X.click.echo(_style_diff_line(line))
        X.click.echo()


def _emit_commands(
    conn: "X.sqlite3.Connection",
    root_id: int,
    branch_id: int | None,
    a_ts: float,
    b_ts: float,
) -> None:
    """List the events in the window so the user sees WHAT caused the net change.

    Scoped to the cwd's root and active timeline, file-changing events only,
    oldest-first (as ``events_between`` returns them).
    """
    events = X.dbm.events_between(
        conn,
        since=a_ts,
        until=b_ts,
        root_id=root_id,
        branch_id=branch_id,
        changes_only=True,
    )
    X.click.echo()
    if not events:
        X.click.secho("  (no recorded commands in this window)", dim=True)
        return
    X.click.secho(f"commands responsible ({len(events)}):", bold=True)
    for row in events:
        X.click.echo("  " + _command_line(row))


# ------------------------------------------------------------------ registration


def register(main) -> None:
    @main.command()
    @X.click.argument("moment")
    @X.click.option(
        "--stat", is_flag=True, help="Names + counts only (no diff bodies)."
    )
    @X.click.option(
        "--commands", "-c", is_flag=True, help="Also list the commands responsible."
    )
    def since(moment: str, stat: bool, commands: bool) -> None:  # type: ignore[no-untyped-def]
        """Show the NET effect of everything since MOMENT (cumulative diff to now).

        MOMENT is a mark name, an event id (``#42``/``42``), ``now``, or a time
        spec ("this morning", "2h ago"). Files created and then deleted inside
        the window cancel out; each surviving file shows a single before→after
        change. ``--stat`` prints one line per file; ``--commands`` also lists
        the commands that produced the change.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # Untracked cwd / empty store -> clean ClickException from these.
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # Resolve the moment (mark / #id / time). Unknown moment -> a clean
            # ClickException raised inside moment_ts. "now" is the upper bound.
            a_ts = X.moment_ts(conn, moment)
            b_ts = time.time()

            # Net diff of the whole window (created-then-deleted cancels out).
            # range_changes tolerates a future / after-latest moment by simply
            # finding no events; guard its untracked-cwd OpsError just in case.
            try:
                _root_path, changes = range_changes(conn, X.Path.cwd(), a_ts, b_ts)
            except X.OpsError as exc:
                raise X.click.ClickException(str(exc)) from exc

            X.click.secho(f"since {X.fmt_ts(a_ts)} ({moment})  ..  now", bold=True)

            if not changes:
                X.click.secho(f"no net changes since {moment}", fg="yellow")
                return

            if stat:
                _emit_stat(changes)
            else:
                _emit_diffs(store, changes)

            if commands:
                _emit_commands(conn, root_id, branch_id, a_ts, b_ts)
        finally:
            conn.close()
