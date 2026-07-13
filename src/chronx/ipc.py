"""Shell-hook <-> daemon protocol over a named pipe (FIFO).

The shell hook writes one line per signal; lines under PIPE_BUF (4 KiB)
are atomic, so concurrent shells can share the pipe safely. Variable-width
fields (cwd, command) are base64-encoded so the framing survives any bytes
the shell throws at us, including tabs and newlines inside commands.

Line formats (tab-separated):
    PRE   <session> <epoch> <cwd_b64> <command_b64>
    POST  <session> <epoch> <exit_code>
    SYNC  <root_b64>            # reload manifest + drop dirty state for root
"""

from __future__ import annotations

import base64
import binascii
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PreMsg:
    session: str
    ts: float
    cwd: str
    command: str


@dataclass(frozen=True)
class PostMsg:
    session: str
    ts: float
    exit_code: int | None


@dataclass(frozen=True)
class SyncMsg:
    root: str


Message = PreMsg | PostMsg | SyncMsg


def _b64decode(field: str) -> str | None:
    field = field.strip()
    if not field:
        return ""
    pad = -len(field) % 4
    try:
        return base64.b64decode(field + "=" * pad).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def _float(field: str) -> float:
    try:
        return float(field)
    except ValueError:
        return time.time()


def parse_line(line: str) -> Message | None:
    parts = line.rstrip("\r\n").split("\t")
    kind = parts[0] if parts else ""
    if kind == "PRE" and len(parts) >= 5:
        cwd = _b64decode(parts[3])
        cmd = _b64decode(parts[4])
        if cwd is None or cmd is None:
            return None
        return PreMsg(session=parts[1], ts=_float(parts[2]), cwd=cwd, command=cmd)
    if kind == "POST" and len(parts) >= 4:
        try:
            exit_code: int | None = int(parts[3])
        except ValueError:
            exit_code = None
        return PostMsg(session=parts[1], ts=_float(parts[2]), exit_code=exit_code)
    if kind == "SYNC" and len(parts) >= 2:
        root = _b64decode(parts[1])
        if not root:
            return None
        return SyncMsg(root=root)
    return None


def encode_sync(root: str) -> str:
    return "SYNC\t" + base64.b64encode(root.encode("utf-8")).decode("ascii")


def send_line(fifo: Path, line: str) -> bool:
    """Best-effort, non-blocking write of one protocol line. True on success."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False  # no daemon reading (ENXIO) or no fifo
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
