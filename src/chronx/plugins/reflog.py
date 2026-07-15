"""chronx reflog — a chronological log of chronx's own recovery points.

Like ``git reflog``: chronx's state-changing operations (undo, rollback,
restore, merge, cherry-pick) each record *themselves* as an event whose command
starts with ``chronx `` and whose session is ``chronx``. Every one of those
events is a safety net — the operation snapshotted the tree before touching
anything and recorded its effect as an event of its own — so each is a point
you can recover from.

``chronx reflog`` lists those operations newest-first for the tracked root
containing the cwd, so after a bad ``undo`` or ``rollback`` you can read off the
event id to recover from (``chronx undo --event <id>`` or
``chronx checkout <id> <dir>``). ``--all`` widens the log to every recorded
event, with the recovery points still highlighted by their coloured label.

Strictly read-only over the store.
"""

from __future__ import annotations

from chronx import pluginlib as X

# Known chronx recovery operations, matched by command prefix. Each maps to a
# short label and a colour so the operation kind is scannable at a glance.
_OP_STYLES: tuple[tuple[str, str, str], ...] = (
    ("chronx undo", "undo", "cyan"),
    ("chronx rollback", "rollback", "yellow"),
    ("chronx restore", "restore", "green"),
    ("chronx merge", "merge", "magenta"),
    ("chronx cherry-pick", "cherry-pick", "blue"),
)

# Width of the bracketed label column ("[cherry-pick]" is the widest label).
_LABEL_W = len("[cherry-pick]")


def _classify(command: str | None) -> tuple[str, str | None]:
    """Map a command to its ``(label, colour)`` from its recovery-op prefix.

    Anything that is not one of the five known chronx operations is tagged
    ``other`` with no colour (rendered dim) — this is what ordinary commands
    surfaced by ``--all`` fall into.
    """
    cmd = command or ""
    for prefix, label, color in _OP_STYLES:
        if cmd.startswith(prefix):
            return label, color
    return "other", None


def _is_recovery(command: str | None, session: str | None) -> bool:
    """True for a chronx-internal safety event (i.e. an actual recovery point)."""
    return session == "chronx" or (command or "").startswith("chronx ")


def _count(n: int, sign: str, color: str) -> str:
    """A single ±count, coloured when non-zero and dim when zero."""
    text = f"{sign}{n}"
    return X.click.style(text, fg=color) if n else X.click.style(text, dim=True)


def _pad(styled: str, raw: str, width: int) -> str:
    """Left-align ``styled`` (which carries ANSI codes) to ``width`` columns,
    measuring with its ANSI-free ``raw`` form so alignment survives styling."""
    return styled + " " * max(1, width - len(raw))


def _counts_field(counts: dict[str, int]) -> str:
    """Render an event's delta counts as a padded, coloured ``+A ~M -D`` field."""
    a, m, d = counts.get("A", 0), counts.get("M", 0), counts.get("D", 0)
    raw = f"+{a} ~{m} -{d}"
    styled = " ".join(
        (_count(a, "+", "green"), _count(m, "~", "yellow"), _count(d, "-", "red"))
    )
    return _pad(styled, raw, 11)


def _label_field(label: str, color: str | None) -> str:
    """Render the bracketed operation label, padded to a fixed column width."""
    raw = f"[{label}]"
    styled = (
        X.click.style(raw, fg=color, bold=True)
        if color
        else X.click.style(raw, dim=True)
    )
    return _pad(styled, raw, _LABEL_W)


def _short(text: str, limit: int = 80) -> str:
    """Collapse whitespace and clip an over-long description to one tidy line."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def register(main) -> None:
    @main.command()
    @X.click.option(
        "--all", "show_all", is_flag=True, help="Include ordinary commands too."
    )
    @X.click.option(
        "--limit",
        "-n",
        default=40,
        show_default=True,
        help="Maximum number of entries to show.",
    )
    def reflog(show_all: bool, limit: int) -> None:
        """Log chronx's own recovery points (undo/rollback/restore/merge/...).

        Like ``git reflog``: chronx's state-changing operations each record
        themselves as a reversible event. After a bad undo or rollback, read
        off the event id here to recover from it. ``--all`` shows every event.
        """
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])

            # Scope to the cwd's root across ALL timelines — a recovery point
            # may sit on a branch you have since left, and you still want to be
            # able to find it. Default to only chronx-internal safety events;
            # --all widens to every recorded event. Newest first, capped by -n.
            where = "root_id = ?"
            if not show_all:
                where += " AND (session = 'chronx' OR command LIKE 'chronx %')"
            rows = conn.execute(
                "SELECT id, session, command, started_at FROM events"
                f" WHERE {where} ORDER BY id DESC LIMIT ?",
                (root_id, int(limit)),
            ).fetchall()

            if not rows:
                if show_all:
                    # Store is fine, this root simply has no history yet.
                    X.click.secho(
                        f"no events recorded under {root['path']} yet", fg="yellow"
                    )
                    return
                # No recovery points. If ordinary history exists, point at
                # --all so an empty result doesn't look like a broken store.
                total = conn.execute(
                    "SELECT COUNT(*) AS n FROM events WHERE root_id = ?", (root_id,)
                ).fetchone()["n"]
                X.click.secho(
                    "no chronx operations recorded — nothing to recover from yet",
                    fg="yellow",
                )
                if total:
                    X.click.secho(
                        f"  ({total} ordinary event(s) recorded — "
                        "`chronx reflog --all` to see them)",
                        dim=True,
                    )
                return

            # Header: what this list is and how to read it.
            scope = "events" if show_all else "recovery points"
            X.click.secho(f"chronx reflog — {scope} under {root['path']}", bold=True)
            X.click.secho(
                "  chronx's own state-changing ops (undo/rollback/restore/merge/"
                "cherry-pick) are reversible — each is a point you can recover from.",
                dim=True,
            )
            X.click.echo("")

            # One line per event: #id  time  ±counts  [label]  description.
            recoveries = 0
            for r in rows:
                command = r["command"]
                if _is_recovery(command, r["session"]):
                    recoveries += 1
                label, color = _classify(command)
                counts = X.dbm.delta_counts(conn, int(r["id"]))

                id_raw = f"#{r['id']}"
                X.click.echo(
                    _pad(X.click.style(id_raw, bold=True), id_raw, 6)
                    + "  "
                    + X.click.style(X.fmt_ts(r["started_at"]), fg="cyan")
                    + "  "
                    + _counts_field(counts)
                    + "  "
                    + _label_field(label, color)
                    + "  "
                    + _short(X.describe_command(r))
                )

            # Summary + how-to-recover footer.
            X.click.echo("")
            if show_all:
                X.click.secho(
                    f"{len(rows)} event(s) shown · {recoveries} recovery point(s)",
                    dim=True,
                )
            else:
                X.click.secho(f"{len(rows)} recovery point(s) shown", dim=True)
            X.click.secho(
                "recover with `chronx undo --event <id>` "
                "or `chronx checkout <id> <dir>`",
                fg="green",
            )
        finally:
            conn.close()
