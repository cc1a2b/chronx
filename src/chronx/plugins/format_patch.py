"""chronx format-patch — export a recorded event as a git-appliable patch.

Turns one recorded event (or an id range) into a standard unified diff with
git-style ``diff --git`` / ``new file mode`` headers plus an mbox-style commit
header, so a recorded change can be replayed elsewhere with ``git apply``,
``git am``, or ``patch -p1``.

The chronx store and database are only read; nothing is written except the
requested patch (to stdout, or to a file with ``-o``).
"""

from __future__ import annotations

from email.utils import formatdate

from chronx import pluginlib as X

# git's canonical "zero" date printed at the top of format-patch/mbox output.
_MBOX_DATE = "Mon Sep 17 00:00:00 2001"
# NUL within this many leading bytes => treat the blob as binary (git's rule).
_BINARY_SNIFF = 8192
# Effectively "never truncate" — we want a complete, appliable patch.
_MAX_DIFF_LINES = 1_000_000


def _git_mode(mode: int | None) -> str:
    """git only records two file modes: 100755 (executable) or 100644."""
    return "100755" if (mode or 0) & 0o111 else "100644"


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def _load(store: "X.ObjectStore", digest: str | None) -> bytes | None:
    """Fetch a blob's bytes, or None if the digest is unset/absent/corrupt."""
    if digest is None:
        return None
    try:
        return store.get(digest)
    except (KeyError, ValueError):
        return None


def _section(store: "X.ObjectStore", delta: "X.dbm.Delta") -> list[str]:
    """Patch lines for one file delta.

    Emits a ``diff --git`` header (with ``new file``/``deleted file`` mode where
    applicable) followed by either a real text hunk (via ``render_delta``), a
    git-style ``Binary files ... differ`` stub, or a ``# skipped`` comment when
    the content cannot be reconstructed or there is nothing to apply. The result
    is always well-formed so the overall patch applies cleanly.
    """
    path = delta.path
    change = delta.change

    # Blobs this delta needs present to reconstruct a real hunk.
    need_before = change != "A" and delta.before_hash is not None
    need_after = change != "D" and delta.after_hash is not None
    before = _load(store, delta.before_hash) if need_before else None
    after = _load(store, delta.after_hash) if need_after else None

    # Content unavailable (pruned/corrupt blob): note it and skip.
    if (need_before and before is None) or (need_after and after is None):
        return [f"# skipped {path}: blob missing"]
    # A modification whose new content outgrew the snapshot cap (untracked).
    if change == "M" and delta.after_hash is None:
        return [f"# skipped {path}: content no longer tracked (grew past cap)"]

    header = [f"diff --git a/{path} b/{path}"]
    if change == "A":
        header.append(f"new file mode {_git_mode(delta.after_mode)}")
    elif change == "D":
        header.append(f"deleted file mode {_git_mode(delta.before_mode)}")

    # Binary (or non-UTF-8) content: a textual hunk would be meaningless, so
    # emit git's canonical stub line instead.
    if (before is not None and _is_binary(before)) or (
        after is not None and _is_binary(after)
    ):
        return header + [f"Binary files a/{path} and b/{path} differ"]
    try:
        (before or b"").decode("utf-8")
        (after or b"").decode("utf-8")
    except UnicodeDecodeError:
        return header + [f"Binary files a/{path} and b/{path} differ"]

    # Text: reuse chronx's own renderer for the ---/+++/@@/body lines.
    body = X.render_delta(store, delta, max_lines=_MAX_DIFF_LINES)

    # render_delta returns a pseudo "@@ ... @@" note (not a real hunk) only for
    # no-content cases we haven't special-cased above (an empty added/deleted
    # file, or a metadata-only change). A real hunk header starts with "@@ -";
    # anything else has nothing to apply, so skip it to keep the patch valid.
    if len(body) < 3 or not body[2].startswith("@@ -"):
        return [f"# skipped {path}: no textual change to apply"]
    return header + body


def _render_deltas(store: "X.ObjectStore", deltas: list["X.dbm.Delta"]) -> list[str]:
    out: list[str] = []
    for delta in deltas:
        out.extend(_section(store, delta))
    return out


def _mbox_header(subject: str, note: str, date_epoch: float | None = None) -> list[str]:
    """The commit-message preamble that lets ``git am`` ingest the patch too.

    Includes the standard ``From:``/``Date:`` mail headers ``git am`` needs for
    a valid author (without them it aborts with "empty ident name"). ``git
    apply``/``patch -p1`` ignore everything before the first diff, so the same
    output serves all three tools.
    """
    # Collapse any newlines so a multi-line command stays one valid Subject.
    subject = " ".join(subject.split()) or "(external change)"
    lines = [
        f"From chronx {_MBOX_DATE}",
        "From: chronx <chronx@localhost>",
    ]
    if date_epoch is not None:
        lines.append(f"Date: {formatdate(date_epoch, localtime=False)}")
    lines += [f"Subject: [chronx] {subject}", "", note, "---"]
    return lines


def _parse_event_id(spec: str) -> int | None:
    """An event id from ``5`` or ``#5``; None if it isn't a plain id."""
    spec = spec.strip().lstrip("#")
    return int(spec) if spec.isdigit() else None


def _build_single(store: "X.ObjectStore", conn: "X.sqlite3.Connection", ref: str) -> list[str]:
    eid = _parse_event_id(ref)
    if eid is None:
        raise X.click.ClickException(
            f"invalid event ref {ref!r} (expected an id like 5, #5, or a range A..B)"
        )
    event = X.dbm.event_by_id(conn, eid)
    if event is None:
        raise X.click.ClickException(f"no event with id {eid}")

    header = _mbox_header(
        X.describe_command(event),
        f"Recorded by chronx as event #{eid} at {X.fmt_ts(event['started_at'])}.",
        event["started_at"],
    )
    body = _render_deltas(store, X.dbm.deltas_for(conn, eid))
    if not body:
        body = ["# (no file changes recorded for this event)"]
    return header + body


def _net_deltas(conn: "X.sqlite3.Connection", events: list["X.sqlite3.Row"]) -> list["X.dbm.Delta"]:
    """Fold every delta across ``events`` (id order) into one net delta per path.

    Keep the state entering the window (the first ``before_*`` seen) and the
    state leaving it (the last ``after_*``); the combined patch therefore moves
    a tree from its state before A straight to its state after B.
    """
    first: dict[str, "X.dbm.Delta"] = {}
    last: dict[str, "X.dbm.Delta"] = {}
    for event in events:
        for d in X.dbm.deltas_for(conn, int(event["id"])):
            first.setdefault(d.path, d)
            last[d.path] = d

    result: list["X.dbm.Delta"] = []
    for path in sorted(first):
        f, l = first[path], last[path]
        before_hash, after_hash = f.before_hash, l.after_hash
        if before_hash == after_hash:
            continue  # created-and-removed, or net no-op: nothing to emit
        change = "A" if before_hash is None else ("D" if after_hash is None else "M")
        result.append(
            X.dbm.Delta(
                path=path,
                change=change,
                before_hash=before_hash,
                after_hash=after_hash,
                before_size=f.before_size,
                after_size=l.after_size,
                before_mode=f.before_mode,
                after_mode=l.after_mode,
            )
        )
    return result


def _build_range(store: "X.ObjectStore", conn: "X.sqlite3.Connection", ref: str) -> list[str]:
    lo_s, _, hi_s = ref.partition("..")
    lo, hi = _parse_event_id(lo_s), _parse_event_id(hi_s)
    if lo is None or hi is None:
        raise X.click.ClickException(
            f"invalid range {ref!r} (expected A..B with numeric event ids)"
        )
    if lo > hi:
        lo, hi = hi, lo

    events = list(
        conn.execute(
            "SELECT * FROM events WHERE id >= ? AND id <= ? ORDER BY id", (lo, hi)
        )
    )
    if not events:
        raise X.click.ClickException(f"no events in range {lo}..{hi}")

    commands = [e["command"] for e in events if e["command"]]
    header = _mbox_header(
        f"events #{lo}..#{hi} ({len(events)} event(s), {len(commands)} command(s))",
        f"Combined net changes across chronx events #{lo}..#{hi} "
        f"({X.fmt_ts(events[0]['started_at'])} .. {X.fmt_ts(events[-1]['started_at'])}).",
        events[0]["started_at"],
    )
    body = _render_deltas(store, _net_deltas(conn, events))
    if not body:
        body = ["# (no net file changes across this range)"]
    return header + body


def register(main) -> None:
    @main.command("format-patch")
    @X.click.argument("ref")
    @X.click.option(
        "--output",
        "-o",
        type=X.click.Path(path_type=X.Path),
        default=None,
        help="Write the patch to FILE instead of stdout.",
    )
    def format_patch(ref: str, output) -> None:  # type: ignore[no-untyped-def]
        """Export event REF as a git-appliable unified diff patch.

        REF is an event id (``5`` or ``#5``), or a range ``A..B`` that combines
        the net file changes across events A..B (inclusive). The output applies
        with ``git apply``, ``git am``, or ``patch -p1``.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            if ".." in ref:
                lines = _build_range(store, conn, ref)
            else:
                lines = _build_single(store, conn, ref)
        finally:
            conn.close()

        text = "\n".join(lines) + "\n"
        if output is None:
            X.click.echo(text, nl=False)
        else:
            output.write_text(text, encoding="utf-8")
            X.click.echo(f"wrote {output} ({len(text)} bytes)", err=True)
