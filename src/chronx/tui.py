"""`chronx replay` — a textual TUI timeline of the recorded session.

Left: every recorded event in time order; scrub with the cursor.
Right: the files that event touched (Tab to focus, pick one for its diff)
above the diff pane. `/` filters commands, `u` undoes the selected event,
`m` drops a mark at the current moment — all without leaving the timeline.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from . import db as dbm
from .config import Paths
from .diffview import render_delta, stat_line
from .ops import OpsError, apply_undo, describe_command, plan_undo
from .store import ObjectStore
from .when import fmt_ts

_MAX_LINES_PER_FILE = 200
_MAX_TOTAL_LINES = 2000
_MARK_NAME = re.compile(r"^[A-Za-z][\w.-]*$")


def _diff_text(line: str) -> Text:
    if line.startswith(("---", "+++")):
        return Text(line, style="bold")
    if line.startswith("@@"):
        return Text(line, style="cyan")
    if line.startswith("+"):
        return Text(line, style="green")
    if line.startswith("-"):
        return Text(line, style="red")
    return Text(line)


class ConfirmScreen(ModalScreen[bool]):
    """y/n modal used for undo confirmation."""

    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt, id="dialog-prompt")
            with Horizontal(id="dialog-buttons"):
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (n)", variant="primary", id="no")

    @on(Button.Pressed, "#yes")
    def _on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _on_no(self) -> None:
        self.dismiss(False)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    """One-line text input modal (filter, mark name)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-prompt")
            yield Input(value=self._value, placeholder=self._placeholder, id="dialog-input")

    def on_mount(self) -> None:
        self.query_one("#dialog-input", Input).focus()

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReplayApp(App[None]):
    TITLE = "chronx replay"
    CSS = """
    #timeline {
        width: 55%;
        border: solid $primary;
    }
    #right {
        width: 45%;
    }
    #files {
        height: 9;
        border: solid $secondary;
    }
    #detail-scroll {
        border: solid $secondary;
        padding: 0 1;
    }
    ConfirmScreen, PromptScreen {
        align: center middle;
    }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    #dialog-buttons {
        height: auto;
        align-horizontal: center;
    }
    #dialog-buttons Button {
        margin: 1 2 0 2;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "filter_cmds", "Filter"),
        Binding("u", "undo_selected", "Undo event"),
        Binding("m", "make_mark", "Mark now"),
        Binding("c", "toggle_changes", "Changes only"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "go_start", show=False),
        Binding("G", "go_end", "Latest", show=False),
    ]

    def __init__(self, paths: Paths, *, limit: int = 500, all_roots: bool = False) -> None:
        super().__init__()
        self._paths = paths
        self._limit = limit
        self._all_roots = all_roots
        self._changes_only = False
        self._filter: str | None = None
        self._row_ids: list[int] = []
        self._last_max_id = -1
        self._current_event_id: int | None = None
        self._current_deltas: dict[str, dbm.Delta] = {}
        self._conn: sqlite3.Connection | None = None
        self._store = ObjectStore(paths.objects)

    # ------------------------------------------------------------- lifecycle

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="timeline", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield DataTable(id="files", cursor_type="row")
                with VerticalScroll(id="detail-scroll"):
                    yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self._conn = dbm.connect(self._paths.db, readonly=True)
        table = self.query_one("#timeline", DataTable)
        table.add_columns("#", "time", "Δ", "exit", "command")
        files = self.query_one("#files", DataTable)
        files.add_columns("", "file", "bytes")
        self._load_events()
        table.focus()
        self.set_interval(2.0, self._poll)

    def on_unmount(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def _poll(self) -> None:
        if self._conn is None:
            return
        current = dbm.max_event_id(self._conn)
        if current != self._last_max_id:
            self._load_events(keep_cursor=True)

    # ------------------------------------------------------------------ data

    def _scope_root_id(self) -> int | None:
        assert self._conn is not None
        if self._all_roots:
            return None
        root = dbm.root_for_path(self._conn, Path.cwd())
        return int(root["id"]) if root is not None else None

    def _load_events(self, keep_cursor: bool = False) -> None:
        assert self._conn is not None
        table = self.query_one("#timeline", DataTable)
        prev_id: int | None = None
        was_at_end = True
        if keep_cursor and self._row_ids:
            idx = table.cursor_row
            if 0 <= idx < len(self._row_ids):
                prev_id = self._row_ids[idx]
                was_at_end = idx == len(self._row_ids) - 1
        table.clear()
        self._row_ids = []
        self._last_max_id = dbm.max_event_id(self._conn)
        rows = dbm.recent_events(
            self._conn,
            root_id=self._scope_root_id(),
            limit=self._limit,
            changes_only=self._changes_only,
        )
        if self._filter:
            needle = self._filter.lower()
            rows = [r for r in rows if needle in describe_command(r).lower()]

        badges = []
        if self._changes_only:
            badges.append("changes only")
        if self._filter:
            badges.append(f"/{self._filter}/")
        suffix = f" [{', '.join(badges)}]" if badges else ""

        if not rows:
            self._show_files([])
            self._set_detail(
                Text(
                    "Nothing matches." if (self._filter or self._changes_only) else
                    "No events recorded yet.\n\n"
                    "Make sure the daemon is running (`chronx daemon start`)\n"
                    "and your shell sources the hook (`chronx init`).",
                    style="dim",
                )
            )
            self.sub_title = "0 events" + suffix
            return

        ids = [int(r["id"]) for r in rows]
        counts: dict[int, int] = {i: 0 for i in ids}
        marks = ",".join("?" * len(ids))
        for row in self._conn.execute(
            f"SELECT event_id, COUNT(*) AS n FROM deltas"
            f" WHERE event_id IN ({marks}) GROUP BY event_id",
            ids,
        ):
            counts[int(row["event_id"])] = int(row["n"])

        for r in rows:
            event_id = int(r["id"])
            n = counts[event_id]
            exit_code = r["exit_code"]
            command = describe_command(r)
            table.add_row(
                str(event_id),
                fmt_ts(r["started_at"]),
                Text(str(n), style="magenta bold") if n else Text("·", style="dim"),
                Text("-", style="dim")
                if exit_code is None
                else Text(str(exit_code), style="green" if exit_code == 0 else "red"),
                Text(
                    command if len(command) <= 120 else command[:117] + "...",
                    style="yellow" if r["command"] is not None else "dim italic",
                ),
                key=str(event_id),
            )
            self._row_ids.append(event_id)
        self.sub_title = f"{len(rows)} events (latest {self._limit})" + suffix
        if prev_id is not None and not was_at_end and prev_id in self._row_ids:
            table.move_cursor(row=self._row_ids.index(prev_id))
        else:
            table.move_cursor(row=table.row_count - 1)

    # ------------------------------------------------------------------ view

    def _set_detail(self, text: Text) -> None:
        self.query_one("#detail", Static).update(text)
        self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def _show_files(self, deltas: list[dbm.Delta]) -> None:
        files = self.query_one("#files", DataTable)
        files.clear()
        self._current_deltas = {d.path: d for d in deltas}
        style = {"A": "green", "M": "yellow", "D": "red"}
        for d in deltas:
            size = d.after_size if d.after_size is not None else d.before_size
            files.add_row(
                Text(d.change, style=style[d.change]),
                d.path,
                Text(str(size if size is not None else "?"), style="dim"),
                key=d.path,
            )

    def _event_header(self, event: sqlite3.Row) -> Text:
        out = Text()
        out.append(f"event #{event['id']}", style="bold")
        out.append(f"  {fmt_ts(event['started_at'])}\n")
        if event["exit_code"] is not None:
            style = "green" if event["exit_code"] == 0 else "red"
            out.append("exit ", style="dim")
            out.append(f"{event['exit_code']}\n", style=style)
        out.append(f"cwd {event['cwd']}\n", style="dim")
        out.append(f"$ {describe_command(event)}\n\n", style="yellow bold")
        return out

    def _show_event(self, event_id: int) -> None:
        assert self._conn is not None
        event = dbm.event_by_id(self._conn, event_id)
        if event is None:
            return
        self._current_event_id = event_id
        deltas = dbm.deltas_for(self._conn, event_id)
        self._show_files(deltas)
        out = self._event_header(event)
        if not deltas:
            out.append("(no filesystem changes)", style="dim")
            self._set_detail(out)
            return
        total = 0
        for d in deltas:
            for line in render_delta(self._store, d, max_lines=_MAX_LINES_PER_FILE):
                out.append_text(_diff_text(line))
                out.append("\n")
                total += 1
                if total >= _MAX_TOTAL_LINES:
                    out.append("... output truncated ...", style="dim")
                    self._set_detail(out)
                    return
            out.append("\n")
        self._set_detail(out)

    def _show_file(self, path: str) -> None:
        assert self._conn is not None
        if self._current_event_id is None:
            return
        delta = self._current_deltas.get(path)
        event = dbm.event_by_id(self._conn, self._current_event_id)
        if delta is None or event is None:
            return
        out = self._event_header(event)
        out.append(stat_line(delta) + "\n\n", style="bold")
        for line in render_delta(self._store, delta, max_lines=_MAX_TOTAL_LINES):
            out.append_text(_diff_text(line))
            out.append("\n")
        self._set_detail(out)

    # ---------------------------------------------------------------- events

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        if message.row_key is None or message.row_key.value is None:
            return
        control = message.control
        if control is not None and control.id == "files":
            if control.has_focus:
                self._show_file(str(message.row_key.value))
            return
        self._show_event(int(message.row_key.value))

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        if (
            message.control is not None
            and message.control.id == "files"
            and message.row_key is not None
            and message.row_key.value is not None
        ):
            self._show_file(str(message.row_key.value))

    # --------------------------------------------------------------- actions

    def _selected_event_id(self) -> int | None:
        table = self.query_one("#timeline", DataTable)
        idx = table.cursor_row
        if 0 <= idx < len(self._row_ids):
            return self._row_ids[idx]
        return None

    def action_undo_selected(self) -> None:
        event_id = self._selected_event_id()
        if event_id is None or self._conn is None:
            return
        event = dbm.event_by_id(self._conn, event_id)
        if event is None:
            return
        try:
            plan = plan_undo(self._conn, self._store, event)
        except OpsError as exc:
            self.notify(str(exc), severity="warning", timeout=5)
            return
        conflicts = len(plan.conflicts)
        n = len(plan.steps) - conflicts
        prompt = f"Undo event #{event_id}: revert {n} file(s)"
        if conflicts:
            prompt += f", skipping {conflicts} conflicted"
        prompt += "?\n\nThe undo is recorded and reversible."

        def _apply(confirmed: bool | None) -> None:
            if not confirmed:
                return
            try:
                write_conn = dbm.connect(self._paths.db)
                try:
                    fresh = dbm.event_by_id(write_conn, event_id)
                    fresh_plan = plan_undo(write_conn, self._store, fresh)
                    backup_id, applied = apply_undo(
                        write_conn, self._store, self._paths, fresh_plan,
                        skip_conflicts=True,
                    )
                finally:
                    write_conn.close()
            except OpsError as exc:
                self.notify(str(exc), severity="error", timeout=6)
                return
            self.notify(
                f"reverted {len(applied)} file(s); recorded as event #{backup_id}",
                timeout=5,
            )
            self._load_events(keep_cursor=True)

        self.push_screen(ConfirmScreen(prompt), _apply)

    def action_make_mark(self) -> None:
        def _create(name: str | None) -> None:
            if not name:
                return
            if not _MARK_NAME.match(name) or name in ("last", "now"):
                self.notify("invalid mark name", severity="warning", timeout=4)
                return
            try:
                write_conn = dbm.connect(self._paths.db)
                try:
                    root = dbm.root_for_path(write_conn, Path.cwd())
                    dbm.add_mark(
                        write_conn, name, time.time(),
                        int(root["id"]) if root else None,
                    )
                finally:
                    write_conn.close()
            except sqlite3.IntegrityError:
                self.notify(f"mark {name!r} already exists", severity="warning", timeout=4)
                return
            self.notify(f"marked now as {name!r} — `chronx rollback {name}`", timeout=5)

        self.push_screen(
            PromptScreen("Name this moment", placeholder="e.g. before-refactor"),
            _create,
        )

    def action_filter_cmds(self) -> None:
        def _apply(value: str | None) -> None:
            if value is None:
                return
            self._filter = value.strip() or None
            self._load_events()

        self.push_screen(
            PromptScreen(
                "Filter commands (empty clears)",
                placeholder="substring, e.g. make",
                value=self._filter or "",
            ),
            _apply,
        )

    def action_refresh(self) -> None:
        self._load_events()

    def action_toggle_changes(self) -> None:
        self._changes_only = not self._changes_only
        self._load_events()

    def action_cursor_down(self) -> None:
        self.query_one("#timeline", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#timeline", DataTable).action_cursor_up()

    def action_go_start(self) -> None:
        self.query_one("#timeline", DataTable).move_cursor(row=0)

    def action_go_end(self) -> None:
        table = self.query_one("#timeline", DataTable)
        table.move_cursor(row=table.row_count - 1)
