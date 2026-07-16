"""chronx open — comfortably view a recorded file version, or diff two moments.

``chronx cat`` dumps a historical version straight to stdout; ``chronx open``
is the *human* front end for the same data. It resolves FILE's recorded content
at a moment in time and either

  * opens it in your ``$PAGER`` (``less`` by default) under a friendly
    ``<basename>@<when>`` name so you can scroll it like any other file, or
  * with ``--vs OTHER`` prints a colorized unified diff between the file at two
    arbitrary moments (a mark, an event id, ``now``, ``1h`` ago, …), letting you
    compare *any* two points in its history — not just "latest vs previous".

Everything the command reads comes from the content-addressed store and the
sqlite index; it never touches the working copy. The only thing it ever writes
is a throw-away temp file handed to the pager, always removed in a ``finally``.
Binary blobs are never streamed to a pager or diffed as text — they get a short
note plus a hexdump instead — and a missing/absent version degrades to a clean
message rather than a crash.

Read-only over the store. Imports only ``chronx.pluginlib`` (as X) plus the
stdlib, so it stays decoupled from ``cli.py`` and is auto-discovered from the
filesystem (no reinstall).
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import tempfile

from chronx import pluginlib as X

# Only inspect the head of a blob for the NUL-byte binary test (git's cutoff).
_BINARY_SNIFF = 8000
# How many leading bytes to hexdump when refusing to page a binary blob.
_HEX_LIMIT = 64


# --------------------------------------------------------------------- helpers


def _looks_binary(data: bytes) -> bool:
    """Classic heuristic: a NUL byte in the first chunk means "binary".

    Cheap, matches git's own sniff, and keeps us from ever shovelling raw
    binary into a pager or trying to diff it as UTF-8 text.
    """
    return b"\x00" in data[:_BINARY_SNIFF]


def _to_lines(data: "bytes | None") -> "list[str]":
    """Decode a blob (or a missing side → empty) into keepends diff lines.

    ``errors="replace"`` keeps a stray non-UTF-8 byte from raising; callers
    already refuse to diff anything that sniffs as binary, so this only ever
    smooths over the rare lone bad byte in otherwise-textual content.
    """
    if data is None:
        return []
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def _hexdump(data: bytes, limit: int = _HEX_LIMIT) -> "list[str]":
    """A tiny ``hexdump -C`` style view of the first ``limit`` bytes."""
    chunk = data[:limit]
    lines: list[str] = []
    for off in range(0, len(chunk), 16):
        row = chunk[off : off + 16]
        hex_part = " ".join(f"{b:02x}" for b in row)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{off:08x}  {hex_part:<47}  {ascii_part}")
    if len(data) > limit:
        lines.append(f"... ({X.human_bytes(len(data))} total)")
    return lines


def _style_diff_line(line: str) -> str:
    """Colorize one unified-diff line by its leading character (git's palette).

    ``---``/``+++`` file headers are tested before ``-``/``+`` so a header is
    never mistaken for a deletion or addition.
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


def _stdout_is_tty() -> bool:
    """Whether stdout is an interactive terminal (so paging makes sense)."""
    try:
        return os.isatty(1)
    except OSError:
        return False


def _pager_argv() -> "list[str] | None":
    """The pager command as an argv list, or ``None`` if none is usable.

    Honors ``$PAGER`` (default ``less``), supports simple embedded flags
    (``PAGER="less -R"``), and returns ``None`` when the pager is empty or not
    on ``PATH`` — callers then fall back to plain stdout.
    """
    raw = os.environ.get("PAGER", "less").strip()
    if not raw:
        return None
    argv = raw.split()
    if shutil.which(argv[0]) is None:
        return None
    return argv


def _resolve(
    conn: "X.sqlite3.Connection",
    store: "X.ObjectStore",
    root_id: int,
    rel: str,
    when: str,
) -> "tuple[float, bytes | None]":
    """Resolve ``rel``'s recorded content at moment ``when``.

    Returns ``(epoch_ts, data)`` where ``data`` is ``None`` if the file was not
    present at that moment (absent, deleted, or never recorded on this
    timeline). Raises a clean ClickException for an unknown moment (via
    ``moment_ts``) or an unavailable blob.
    """
    ts = X.moment_ts(conn, when)  # clean ClickException on an unparseable spec
    entry = X.state_at(conn, root_id, ts).get(rel)
    if not entry or entry[0] is None:
        return ts, None
    digest = entry[0]
    try:
        return ts, store.get(digest)
    except (KeyError, ValueError) as exc:
        raise X.click.ClickException(
            f"{rel}: recorded content unavailable (blob {digest[:12]}: {exc})"
        ) from exc


# ------------------------------------------------------------------- rendering


def _emit_binary_note(rel: str, ts: float, data: bytes) -> None:
    """Refuse to page a binary blob: note (stderr) + hexdump head (stdout)."""
    X.click.secho(
        f"{rel} @ {X.fmt_ts(ts)} looks binary ({X.human_bytes(len(data))}); "
        "not paging. First bytes:",
        fg="yellow",
        err=True,
    )
    for line in _hexdump(data):
        X.click.echo(line)


def _page(argv: "list[str]", rel: str, when: str, data: bytes) -> None:
    """Write ``data`` to a temp ``<basename>@<when>`` file and open the pager.

    The file lives in a private temp dir removed in ``finally`` no matter how
    the pager exits; if the pager fails to launch we degrade to raw stdout.
    """
    tmpdir = tempfile.mkdtemp(prefix="chronx-open-")
    try:
        # Friendly title for the pager; strip separators so it stays one file.
        name = f"{os.path.basename(rel)}@{when}".replace(os.sep, "-").replace(
            "/", "-"
        )
        tmpfile = os.path.join(tmpdir, name or "file")
        with open(tmpfile, "wb") as fh:
            fh.write(data)
        try:
            subprocess.run([*argv, tmpfile])
        except OSError:
            X.click.echo(data, nl=False)  # pager vanished mid-flight → stdout
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _do_view(
    conn: "X.sqlite3.Connection",
    store: "X.ObjectStore",
    root_id: int,
    rel: str,
    when: str,
    pager: bool,
) -> None:
    """Single-version view: page the content, or stream it to stdout."""
    ts, data = _resolve(conn, store, root_id, rel, when)
    if data is None:
        X.click.secho(f"{rel} did not exist at {when}", fg="yellow", err=True)
        return

    # Context on stderr so stdout stays pure, pipeable file content.
    X.click.secho(f"{rel} @ {X.fmt_ts(ts)}", bold=True, err=True)

    if _looks_binary(data):
        _emit_binary_note(rel, ts, data)
        return

    # Page only when asked AND attached to a terminal AND a pager exists;
    # otherwise write the raw bytes straight to stdout.
    argv = _pager_argv() if (pager and _stdout_is_tty()) else None
    if argv is None:
        X.click.echo(data, nl=False)
        return
    _page(argv, rel, when, data)


def _do_diff(
    conn: "X.sqlite3.Connection",
    store: "X.ObjectStore",
    root_id: int,
    rel: str,
    when: str,
    other: str,
) -> None:
    """Unified diff of ``rel`` between two arbitrary moments (colorized)."""
    ts_a, data_a = _resolve(conn, store, root_id, rel, when)
    ts_b, data_b = _resolve(conn, store, root_id, rel, other)

    X.click.secho(
        f"{rel} @ {X.fmt_ts(ts_a)}  vs  @ {X.fmt_ts(ts_b)}", bold=True, err=True
    )

    a_missing, b_missing = data_a is None, data_b is None
    if a_missing and b_missing:
        X.click.secho(
            f"{rel} did not exist at {when} or {other}", fg="yellow", err=True
        )
        return
    # One side missing is still a useful diff (whole file added/removed); note it
    # and treat the absent side as empty content below.
    if a_missing:
        X.click.secho(
            f"note: {rel} did not exist at {when} (treated as empty)",
            fg="yellow",
            err=True,
        )
    if b_missing:
        X.click.secho(
            f"note: {rel} did not exist at {other} (treated as empty)",
            fg="yellow",
            err=True,
        )

    # A text diff of binary bytes is meaningless — refuse either present side.
    if (not a_missing and _looks_binary(data_a)) or (
        not b_missing and _looks_binary(data_b)
    ):
        X.click.secho(
            f"{rel}: binary content — cannot show a text diff "
            f"between {when} and {other}",
            fg="yellow",
            err=True,
        )
        return

    if (data_a or b"") == (data_b or b""):
        X.click.echo(f"no differences between {when} and {other}")
        return

    diff = difflib.unified_diff(
        _to_lines(data_a),
        _to_lines(data_b),
        fromfile=f"{rel}@{when}",
        tofile=f"{rel}@{other}",
    )
    for line in diff:
        X.click.echo(_style_diff_line(line.rstrip("\n")))


# ---------------------------------------------------------------- registration


def register(main) -> None:  # type: ignore[no-untyped-def]
    @main.command("open")
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.argument("when", default="now")
    @X.click.option(
        "--vs",
        default=None,
        metavar="MOMENT",
        help="Second moment: show a unified diff of <when> against it.",
    )
    @X.click.option(
        "--pager/--no-pager",
        default=True,
        help="Page single-version output (default); --no-pager writes to stdout.",
    )
    def open_cmd(file, when, vs, pager):  # type: ignore[no-untyped-def]
        """View FILE's recorded content at WHEN in your pager, or diff two moments.

        WHEN (default ``now``) is any moment spec — a mark, an event id, ``now``,
        or a time like ``1h`` / ``14:32``. With ``--vs OTHER`` a colorized
        unified diff of FILE between WHEN and OTHER is printed instead, so you
        can compare any two points (e.g. ``chronx open app.py 1h --vs now`` or
        ``chronx open app.py mark1 --vs mark2``).
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)  # clean ClickException if cwd untracked
            root_id = int(root["id"])
            # Root-relative, forward-slash key matching how deltas are stored.
            rel = os.path.relpath(file.resolve(), root["path"]).replace(
                os.sep, "/"
            )

            if vs is not None:
                _do_diff(conn, store, root_id, rel, when, vs)
            else:
                _do_view(conn, store, root_id, rel, when, pager)
        finally:
            conn.close()
