"""chronx note — attach freeform human notes to recorded events.

Recorded history is immutable: chronx never rewrites the events or file blobs it
captured. But often you want to remember *why* a command was run, what broke, or
a TODO to follow up on — context that belongs alongside an event without
polluting it. ``chronx note`` layers that context on top as an annotation.

Notes live in a single sidecar file, ``$CHRONX_HOME/notes.json`` — NOT the
sqlite store, NOT the working tree, NOT the object store. It maps each annotated
event id to ``{"text", "created_at", "root_id"}``. Because it never touches
recorded history, ``chronx log`` and friends stay pristine, and this command is
safe to run with the daemon up.

Notes are scoped to the tracked root containing the cwd (via the stored
``root_id``), so ``list`` and ``rm`` only ever see the current project's notes
even though the sidecar is shared across every root.

Subcommands:
  * ``add  <event> <text...>`` — attach/overwrite a note on an event.
  * ``show <event>``           — event header + its note text.
  * ``list``                   — every annotated event in this root, newest-first.
  * ``rm   <event>``           — remove a note (``--yes`` skips the prompt).
"""

from __future__ import annotations

import json
import time

from chronx import pluginlib as X


def _notes_file() -> "X.Path":
    """Path to the shared sidecar JSON (may not exist yet)."""
    return X.paths().home / "notes.json"


def _load_notes() -> dict[str, dict]:
    """The sidecar as a dict. Missing file is ``{}``; a corrupt file is treated
    as ``{}`` after a warning so a bad sidecar never wedges the command."""
    try:
        raw = _notes_file().read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:  # unreadable — surface cleanly rather than crash.
        raise X.click.ClickException(f"cannot read notes: {exc}") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        X.click.secho(
            "warning: notes.json is corrupt — ignoring it and starting fresh",
            fg="yellow",
            err=True,
        )
        return {}
    # A non-object top level (list, string, ...) is just as unusable as corrupt.
    if not isinstance(data, dict):
        X.click.secho(
            "warning: notes.json is not a JSON object — ignoring it",
            fg="yellow",
            err=True,
        )
        return {}
    return data


def _save_notes(notes: dict[str, dict]) -> None:
    """Persist the whole sidecar atomically (small file, rewrite in full)."""
    blob = json.dumps(notes, indent=2, sort_keys=True).encode() + b"\n"
    X.write_atomic(_notes_file(), blob)


def _parse_event_id(raw: str) -> int:
    """Parse ``123`` or ``#123`` into an int, or a clean ClickException."""
    text = raw.strip().lstrip("#")
    if not text.isdigit():
        raise X.click.ClickException(f"{raw!r} is not a valid event id")
    return int(text)


def _require_event(conn, root_id: int, event_id: int):
    """Fetch the event, requiring it to live in the cwd's root.

    Returns the event row, or raises a clean ClickException for an unknown event
    (including one recorded under a *different* tracked directory).
    """
    event = X.dbm.event_by_id(conn, event_id)
    if event is None or int(event["root_id"]) != root_id:
        raise X.click.ClickException(f"no event #{event_id} in this project")
    return event


def _header(event) -> str:
    """One-line event header: ``#id  time  $ command``, coloured like the log."""
    id_txt = X.click.style(f"#{event['id']}", bold=True)
    when = X.click.style(X.fmt_ts(event["started_at"]), fg="cyan")
    cmd = X.click.style(f"$ {X.describe_command(event)}", fg="green")
    return f"{id_txt}  {when}  {cmd}"


def register(main) -> None:
    @main.group("note", invoke_without_command=True)
    @X.click.pass_context
    def note(ctx: "X.click.Context") -> None:
        """Attach freeform notes to recorded events.

        Notes are annotations layered on top of history via a sidecar file, so
        the recorded events and ``chronx log`` stay untouched. Scoped to the
        tracked directory containing the cwd.
        """
        if ctx.invoked_subcommand is None:
            X.click.echo(ctx.get_help())

    @note.command("add")
    @X.click.argument("event")
    @X.click.argument("text", nargs=-1, required=True)
    def note_add(event: str, text: tuple[str, ...]) -> None:
        """Attach (or overwrite) a note on EVENT.

        TEXT may be several words (they are joined) or a single quoted string.
        """
        event_id = _parse_event_id(event)
        body = " ".join(text).strip()
        if not body:
            raise X.click.ClickException("note text is empty")

        conn = X.open_db()
        try:
            root_id = int(X.root_for_cwd(conn)["id"])
            _require_event(conn, root_id, event_id)  # validates existence + scope
        finally:
            conn.close()

        notes = _load_notes()
        existed = str(event_id) in notes
        notes[str(event_id)] = {
            "text": body,
            "created_at": time.time(),
            "root_id": root_id,
        }
        _save_notes(notes)
        X.click.secho(f"{'updated' if existed else 'noted'} #{event_id}", fg="green")

    @note.command("show")
    @X.click.argument("event")
    def note_show(event: str) -> None:
        """Print EVENT's header and its note text (or that it has none)."""
        event_id = _parse_event_id(event)
        conn = X.open_db()
        try:
            root_id = int(X.root_for_cwd(conn)["id"])
            row = _require_event(conn, root_id, event_id)
        finally:
            conn.close()

        entry = _load_notes().get(str(event_id))
        # A note keyed here always belongs to this root (event ids are unique),
        # but guard the stored root_id too for good measure.
        if entry is None or int(entry.get("root_id", -1)) != root_id:
            X.click.secho(f"no note on #{event_id}", fg="yellow")
            return

        X.click.echo(_header(row))
        X.click.echo(f"    {entry.get('text', '')}")

    @note.command("list")
    def note_list() -> None:
        """List every annotated event in this project, newest-first."""
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])

            # Join sidecar notes to their events; keep only this root's notes
            # and drop any whose event has since vanished (recover/archive).
            rows: list[tuple] = []
            for key, entry in _load_notes().items():
                if not isinstance(entry, dict):
                    continue
                if int(entry.get("root_id", -1)) != root_id:
                    continue
                if not key.lstrip("#").isdigit():
                    continue
                event = X.dbm.event_by_id(conn, int(key.lstrip("#")))
                if event is None or int(event["root_id"]) != root_id:
                    continue
                rows.append((event, entry.get("text", "")))
        finally:
            conn.close()

        if not rows:
            X.click.secho(f"no notes under {root['path']} yet", fg="yellow")
            return

        # Newest-first by event start time (ties broken by id).
        rows.sort(key=lambda r: (r[0]["started_at"], r[0]["id"]), reverse=True)

        X.click.secho(f"notes under {root['path']}", bold=True)
        X.click.echo("")
        for event, body in rows:
            X.click.echo(_header(event))
            X.click.echo(f"    {body}")
        X.click.echo("")
        X.click.secho(f"{len(rows)} note(s)", dim=True)

    @note.command("rm")
    @X.click.argument("event")
    @X.click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
    def note_rm(event: str, yes: bool) -> None:
        """Remove the note on EVENT."""
        event_id = _parse_event_id(event)
        conn = X.open_db()
        try:
            root_id = int(X.root_for_cwd(conn)["id"])
        finally:
            conn.close()

        notes = _load_notes()
        entry = notes.get(str(event_id))
        # Scope the removal to this root: a note for an event in another tracked
        # directory is invisible (and untouchable) from here.
        if entry is None or int(entry.get("root_id", -1)) != root_id:
            X.click.secho(f"no note on #{event_id}", fg="yellow")
            return

        if not yes and not X.click.confirm(
            f"Remove note on #{event_id}?", default=False
        ):
            raise X.click.Abort()

        del notes[str(event_id)]
        _save_notes(notes)
        X.click.secho(f"removed note on #{event_id}", fg="green")
