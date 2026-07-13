"""`chronx replay` — a textual TUI timeline of the recorded session.

Left: every recorded event in time order. Move the cursor to scrub through
your workflow; the right pane shows exactly what each command changed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from . import db as dbm
from .config import Paths
from .diffview import render_delta
from .ops import describe_command
from .store import ObjectStore
from .when import fmt_ts

_MAX_LINES_PER_FILE = 200
_MAX_TOTAL_LINES = 2000


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


class ReplayApp(App[None]):
    TITLE = "chronx replay"
    CSS = """
    #timeline {
        width: 55%;
        border: solid $primary;
    }
    #detail-scroll {
        width: 45%;
        border: solid $secondary;
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("j", "cursor_down", "Older/newer", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "go_start", "Oldest", show=False),
        Binding("G", "go_end", "Latest"),
    ]

    def __init__(self, paths: Paths, *, limit: int = 500, all_roots: bool = False) -> None:
        super().__init__()
        self._paths = paths
        self._limit = limit
        self._all_roots = all_roots
        self._conn: sqlite3.Connection | None = None
        self._store = ObjectStore(paths.objects)

    # ------------------------------------------------------------- lifecycle

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="timeline", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail-scroll"):
                yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self._conn = dbm.connect(self._paths.db, readonly=True)
        table = self.query_one("#timeline", DataTable)
        table.add_columns("#", "time", "Δ", "exit", "command")
        self._load_events()
        table.focus()

    def on_unmount(self) -> None:
        if self._conn is not None:
            self._conn.close()

    # ------------------------------------------------------------------ data

    def _scope_root_id(self) -> int | None:
        assert self._conn is not None
        if self._all_roots:
            return None
        root = dbm.root_for_path(self._conn, Path.cwd())
        return int(root["id"]) if root is not None else None

    def _load_events(self) -> None:
        assert self._conn is not None
        table = self.query_one("#timeline", DataTable)
        table.clear()
        rows = dbm.recent_events(
            self._conn, root_id=self._scope_root_id(), limit=self._limit
        )
        if not rows:
            self._set_detail(
                Text(
                    "No events recorded yet.\n\n"
                    "Make sure the daemon is running (`chronx daemon start`)\n"
                    "and your shell sources the hook (`chronx init`).",
                    style="dim",
                )
            )
            self.sub_title = "0 events"
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
        self.sub_title = f"{len(rows)} events (latest {self._limit})"
        table.move_cursor(row=table.row_count - 1)

    # ------------------------------------------------------------------ view

    def _set_detail(self, text: Text) -> None:
        self.query_one("#detail", Static).update(text)
        self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

    def _show_event(self, event_id: int) -> None:
        assert self._conn is not None
        event = dbm.event_by_id(self._conn, event_id)
        if event is None:
            return
        out = Text()
        out.append(f"event #{event_id}", style="bold")
        out.append(f"  {fmt_ts(event['started_at'])}\n", style="")
        if event["exit_code"] is not None:
            style = "green" if event["exit_code"] == 0 else "red"
            out.append("exit ", style="dim")
            out.append(f"{event['exit_code']}\n", style=style)
        out.append(f"cwd {event['cwd']}\n", style="dim")
        out.append(f"$ {describe_command(event)}\n\n", style="yellow bold")

        deltas = dbm.deltas_for(self._conn, event_id)
        if not deltas:
            out.append("(no filesystem changes)", style="dim")
            self._set_detail(out)
            return

        total = 0
        for d in deltas:
            lines = render_delta(self._store, d, max_lines=_MAX_LINES_PER_FILE)
            for line in lines:
                out.append_text(_diff_text(line))
                out.append("\n")
                total += 1
                if total >= _MAX_TOTAL_LINES:
                    out.append("... output truncated ...", style="dim")
                    self._set_detail(out)
                    return
            out.append("\n")
        self._set_detail(out)

    # ---------------------------------------------------------------- events

    def on_data_table_row_highlighted(self, message: DataTable.RowHighlighted) -> None:
        if message.row_key is not None and message.row_key.value is not None:
            self._show_event(int(message.row_key.value))

    # --------------------------------------------------------------- actions

    def action_refresh(self) -> None:
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
