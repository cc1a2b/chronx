"""chronx redo — re-apply the change most recently reverted by ``chronx undo``.

How undo works (see ``chronx/ops.py``): ``chronx undo`` reverts an event E and
records a NEW event U whose ``command`` is ``chronx undo #E`` and whose file
deltas are the *reverse* of E's. Re-applying E's original change is therefore
just *undoing that undo* — reverting U writes E's after-content back onto disk.

So ``chronx redo`` finds the most recent ``chronx undo`` event on the active
timeline and runs the ordinary undo machinery against it
(``ops.plan_undo`` + ``ops.apply_undo``). ``apply_undo`` already:

  * snapshots current content into the object store BEFORE touching anything,
  * writes atomically (temp file + ``os.replace``),
  * records the redo as ONE reversible event of its own, and
  * nudges the daemon to resync so it doesn't double-record our writes.

Because the redo lands as a normal event, ``chronx undo`` reverses it again —
giving a simple redo stack.

A guard keeps the classic ``exec → undo → redo`` cycle from looping: once an
undo has been redone, redoing again is a clean no-op ("nothing to redo") rather
than toggling the file back and forth forever.

This module only touches the working tree through that safety net; it never
imports ``chronx.cli``.
"""

from __future__ import annotations

import sqlite3

from chronx import pluginlib as X
from chronx.ops import apply_undo, plan_undo

# Every undo (and every redo, which is itself an undo of an undo) is recorded
# with a command of the form ``chronx undo #<event id>``.
_UNDO_PREFIX = "chronx undo "


def _undo_target_id(command: str | None) -> int | None:
    """The event id an undo reverted: ``chronx undo #123`` -> ``123``.

    Returns ``None`` when ``command`` is not a recognisable undo command.
    """
    if not command or not command.startswith(_UNDO_PREFIX):
        return None
    rest = command[len(_UNDO_PREFIX):].strip().lstrip("#")
    return int(rest) if rest.isdigit() else None


def register(main) -> None:
    @main.command()
    @X.click.option("--yes", "-y", is_flag=True,
                    help="Skip the confirmation prompt.")
    @X.click.option("--force", is_flag=True,
                    help="Also redo files that changed again since the undo.")
    def redo(yes: bool, force: bool) -> None:
        """Re-apply the change most recently reverted by `chronx undo`."""
        # WRITE feature: never run while the recorder is live — it would race
        # our writes and re-record them as a separate external change.
        X.require_daemon_stopped("redo")

        conn: sqlite3.Connection = X.open_db(readonly=False)
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # 1. Redo target = the newest undo event (with deltas) on the active
            #    timeline. `branch_id IS ?` matches both a real branch id and the
            #    legacy NULL branch. The EXISTS clause skips any no-op undo that
            #    changed nothing (there would be nothing to redo).
            undo_row = conn.execute(
                "SELECT * FROM events"
                " WHERE root_id = ? AND branch_id IS ?"
                "   AND command LIKE 'chronx undo %'"
                "   AND EXISTS (SELECT 1 FROM deltas d WHERE d.event_id = events.id)"
                " ORDER BY id DESC LIMIT 1",
                (root_id, branch_id),
            ).fetchone()
            if undo_row is None:
                X.click.echo("nothing to redo (no undo to reverse)")
                return

            # 2. Loop guard. A redo of undo #N is itself recorded as
            #    `chronx undo #N`, so it *also* matches the query above and would
            #    become the newest match after one redo. Detect that the newest
            #    match is already a redo (its target is itself an undo event) or
            #    that a later event already redid it — either way the change is
            #    already re-applied, so redoing again would just toggle it back.
            undo_id = int(undo_row["id"])
            target_id = _undo_target_id(undo_row["command"])
            target_ev = X.dbm.event_by_id(conn, target_id) if target_id else None
            already_a_redo = (
                target_ev is not None
                and str(target_ev["command"] or "").startswith(_UNDO_PREFIX)
            )
            redone_later = conn.execute(
                "SELECT 1 FROM events"
                " WHERE root_id = ? AND branch_id IS ? AND command = ? AND id > ?"
                " LIMIT 1",
                (root_id, branch_id, f"chronx undo #{undo_id}", undo_id),
            ).fetchone() is not None
            if already_a_redo or redone_later:
                X.click.echo("nothing to redo (already redone)")
                return

            # 3. Plan the redo = undo the undo. Reverting U re-applies E's change.
            try:
                plan = plan_undo(conn, store, undo_row)
            except X.OpsError as exc:
                raise X.click.ClickException(str(exc)) from exc

            # Describe what we're re-applying: the original event the undo
            # reverted (falling back to the undo command itself if it's gone).
            original = X.dbm.event_by_id(conn, target_id) if target_id else None
            label = X.describe_command(original if original is not None else undo_row)

            # 4. If every file changed again since the undo, a non-forced redo
            #    would skip them all. Fail early with a clear, actionable hint
            #    instead of the generic "all conflicted" error from apply_undo.
            conflicts = plan.conflicts
            if conflicts and not force and len(conflicts) == len(plan.steps):
                raise X.click.ClickException(
                    f"every file changed again since that undo "
                    f"({len(conflicts)} file(s)); re-run with --force to redo anyway"
                )

            # 5. Preview, then confirm unless --yes.
            X.click.secho(f're-apply "{label}" (undo of event #{undo_id}):', bold=True)
            for step in plan.steps:
                note = ""
                if step.conflict:
                    note = (
                        "  (changed since; forced)" if force
                        else "  (changed since; will skip)"
                    )
                X.click.echo(f"  {step.action:>7}  {step.rel}{note}")
            if not yes and not X.click.confirm("Apply this redo?", default=False):
                raise X.click.Abort()

            # 6. Apply through the shared undo machinery: it snapshots current
            #    content first, writes atomically, records ONE reversible event,
            #    and syncs the daemon. Skip conflicting files unless --force.
            try:
                backup_id, applied = apply_undo(
                    conn, store, X.paths(), plan, skip_conflicts=not force
                )
            except X.OpsError as exc:
                raise X.click.ClickException(str(exc)) from exc

            # 7. Report.
            X.click.secho(
                f'redid {len(applied)} file(s) — re-applied "{label}"; '
                f"recorded as event #{backup_id}",
                fg="green",
            )
            X.click.secho("  `chronx undo` reverses this redo again", dim=True)
        finally:
            conn.close()
