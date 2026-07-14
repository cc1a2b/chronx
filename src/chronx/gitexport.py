"""`chronx to-git` — replay recorded history into a real git repository.

Each command becomes a commit (author date = when it ran, message = the
command), built from chronx's per-command deltas mapped straight onto a
`git fast-import` stream: chronx blobs become fast-import blob marks (deduped
by hash), added/modified files become `M` ops, deletions become `D` ops. An
initial "chronx baseline" commit carries the full tree state at tracking
start, so the resulting history is complete and its HEAD tree matches your
current working state.

The chronx store is only read; a brand-new git repo is written. Nothing in
the recording pipeline is touched.
"""

from __future__ import annotations

import shutil
import subprocess
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import db as dbm
from .ops import state_at
from .store import ObjectStore


@dataclass(frozen=True)
class GitExportStats:
    target: Path
    branch: str
    commits: int
    blobs: int
    events: int


class GitExportError(RuntimeError):
    pass


def git_identity() -> tuple[str, str]:
    def cfg(key: str) -> str:
        try:
            out = subprocess.run(
                ["git", "config", key], capture_output=True, text=True, timeout=5
            )
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return (cfg("user.name") or "chronx", cfg("user.email") or "chronx@localhost")


def _quote_path(path: str) -> str:
    if path and all(c not in path for c in '"\n\\') and not path.startswith(" "):
        return path
    esc = path.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{esc}"'


def _git_mode(mode: int | None) -> str:
    return "100755" if (mode or 0) & 0o111 else "100644"


def _commit_message(row: sqlite3.Row) -> str:
    command = row["command"] if row["command"] is not None else "(external change)"
    lines = [command, ""]
    trailer = [f"chronx-event: {row['id']}"]
    if row["cwd"]:
        trailer.append(f"chronx-cwd: {row['cwd']}")
    if row["exit_code"] is not None:
        trailer.append(f"chronx-exit: {row['exit_code']}")
    if row["session"]:
        trailer.append(f"chronx-session: {row['session']}")
    return "\n".join(lines + trailer) + "\n"


def _build_stream(
    conn: sqlite3.Connection,
    store: ObjectStore,
    root_row: sqlite3.Row,
    branch: str,
    author: tuple[str, str],
    stats: dict[str, int],
) -> Iterator[bytes]:
    """Yield a git fast-import stream for one root's history, updating stats."""
    root_id = int(root_row["id"])
    name, email = author
    ident = f"{name} <{email}>"
    marks: dict[str, int] = {}
    counter = 0
    buf: list[bytes] = []

    def blob(digest: str) -> int | None:
        nonlocal counter
        if digest in marks:
            return marks[digest]
        try:
            data = store.get(digest)
        except (KeyError, ValueError):
            return None  # blob pruned/corrupt: skip this file in this commit
        counter += 1
        marks[digest] = counter
        stats["blobs"] += 1
        buf.append(f"blob\nmark :{counter}\ndata {len(data)}\n".encode())
        buf.append(data)
        buf.append(b"\n")
        return counter

    def commit(ts: float, message: str, ops: list[str]) -> None:
        stats["commits"] += 1
        when = f"{int(ts)} +0000"
        msg_b = message.encode("utf-8")
        buf.append(
            (
                f"commit refs/heads/{branch}\n"
                f"author {ident} {when}\n"
                f"committer {ident} {when}\n"
                f"data {len(msg_b)}\n"
            ).encode("utf-8")
        )
        buf.append(msg_b)
        buf.append(b"\n")
        for op in ops:
            buf.append((op + "\n").encode("utf-8"))
        buf.append(b"\n")

    # Export the active timeline: baseline = its state at the fork point,
    # then one commit per event recorded on that branch.
    active = dbm.active_branch(conn, root_id)
    base_ts = float(active["base_ts"]) if active is not None else float(root_row["added_at"])
    branch_id = int(active["id"]) if active is not None else None

    # 1. Baseline commit: full tree at the branch's start.
    baseline = state_at(conn, root_id, base_ts)
    base_ops: list[str] = []
    for path, (digest, mode) in sorted(baseline.items()):
        if digest is None:
            continue
        mark = blob(digest)
        if mark is not None:
            base_ops.append(f"M {_git_mode(mode)} :{mark} {_quote_path(path)}")
    commit(base_ts, "chronx baseline\n\nchronx-event: 0\n", base_ops)
    yield from buf
    buf.clear()

    # 2. One commit per recorded event on this branch, deltas as M/D ops.
    ev_sql = "SELECT * FROM events WHERE root_id = ?"
    ev_params: list[object] = [root_id]
    if branch_id is not None:
        ev_sql += " AND branch_id = ?"
        ev_params.append(branch_id)
    ev_sql += " ORDER BY id"
    for event in conn.execute(ev_sql, ev_params):
        stats["events"] += 1
        ops: list[str] = []
        for d in dbm.deltas_for(conn, int(event["id"])):
            if d.change == "D":
                ops.append(f"D {_quote_path(d.path)}")
            elif d.after_hash is not None:
                mark = blob(d.after_hash)
                if mark is not None:
                    ops.append(
                        f"M {_git_mode(d.after_mode)} :{mark} {_quote_path(d.path)}"
                    )
        commit(float(event["started_at"]), _commit_message(event), ops)
        yield from buf
        buf.clear()


def to_git(
    conn: sqlite3.Connection,
    store: ObjectStore,
    root_row: sqlite3.Row,
    target: Path,
    *,
    branch: str = "main",
    author: tuple[str, str] | None = None,
    checkout: bool = True,
) -> GitExportStats:
    if shutil.which("git") is None:
        raise GitExportError("git is not installed or not on PATH")
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise GitExportError(f"{target} exists and is not empty")
    author = author or git_identity()

    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "init", "-q", str(target)], check=True, capture_output=True
        )
    except subprocess.CalledProcessError as exc:
        raise GitExportError(f"git init failed: {exc.stderr.decode(errors='replace')}") from exc

    stats = {"commits": 0, "blobs": 0, "events": 0}
    proc = subprocess.Popen(
        ["git", "-C", str(target), "fast-import", "--quiet", "--force"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    try:
        for chunk in _build_stream(conn, store, root_row, branch, author, stats):
            proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    _out, err = proc.communicate()
    if proc.returncode != 0:
        raise GitExportError(
            f"git fast-import failed: {err.decode(errors='replace')[:400]}"
        )

    if checkout:
        subprocess.run(
            ["git", "-C", str(target), "checkout", "-qf", branch],
            capture_output=True,
        )
    return GitExportStats(
        target=target,
        branch=branch,
        commits=stats["commits"],
        blobs=stats["blobs"],
        events=stats["events"],
    )
