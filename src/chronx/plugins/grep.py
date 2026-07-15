"""chronx grep — temporal content search across every recorded file version.

Search the content of EVERY version of every file ever recorded (for the
current root's active timeline) with a Python regex, and report *where* and
*when* each match occurred — including versions that were later overwritten or
deleted. This answers "did any file EVER contain X, and when?", which a plain
grep of the working tree can never tell you.

Read-only over the store. Work is deduplicated by blob hash: each distinct
content blob is decoded and searched exactly once, then mapped back to the
(path, event, time) versions that reference it as their content.
"""

from __future__ import annotations

import re

from chronx import pluginlib as X

# Display / memory caps (kept small so output stays scannable, à la ripgrep).
_LINES_PER_OCCURRENCE = 4  # matching lines shown beneath each file version
_STORED_LINES_PER_BLOB = 25  # matching lines remembered per unique blob
_MAX_LINE_LEN = 240  # over-long lines are clipped for display
_BINARY_SNIFF = 8192  # leading bytes inspected for a NUL (binary sniff)


# Plain classes (not @dataclass) on purpose: plugins are exec'd without being
# registered in sys.modules, and CPython's dataclass machinery dereferences
# sys.modules[cls.__module__] while scanning annotations — which would blow up.
class _BlobHit:
    """The matching lines found inside one unique content blob."""

    __slots__ = ("lines", "total")

    def __init__(self) -> None:
        self.lines: list[tuple[int, str]] = []  # (lineno, text), capped
        self.total: int = 0  # total matching lines (may exceed len(lines))


class _Occurrence:
    """One recorded version of a file whose content matched the pattern."""

    __slots__ = ("path", "ts", "digest", "event_id", "command")

    def __init__(
        self,
        path: str,
        ts: float,  # event time, or the root start time for a baseline version
        digest: str,
        event_id: int | None,  # None => the tree-at-tracking-start baseline
        command: str | None,
    ) -> None:
        self.path = path
        self.ts = ts
        self.digest = digest
        self.event_id = event_id
        self.command = command


def register(main) -> None:
    @main.command()
    @X.click.argument("pattern")
    @X.click.option(
        "-i", "--ignore-case", is_flag=True, help="Case-insensitive matching."
    )
    @X.click.option(
        "--limit", "-n", default=200, show_default=True,
        help="Max total (file, version) matches to report.",
    )
    def grep(pattern: str, ignore_case: bool, limit: int) -> None:
        """Search every recorded version of every file for a regex.

        PATTERN is a Python regular expression. Every distinct content blob on
        the current root's active timeline is searched once; each match is
        reported as the file, the event/time that recorded that version, and
        the matching line(s) — deleted and later-overwritten versions included.
        """
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise X.click.ClickException(f"bad regex: {exc}")

        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # 1) Every distinct content blob that ever existed on this root's
            #    active timeline: the after_hash of any delta + baseline hashes.
            candidates = _candidate_blobs(conn, root_id, branch_id)
            if not candidates:
                X.click.secho("no recorded file content to search", fg="yellow")
                return

            # 2) Grep each unique blob exactly once (dedup by content hash).
            blob_hits = _grep_blobs(store, regex, candidates)
            if not blob_hits:
                X.click.secho(
                    f"no recorded version ever matched /{pattern}/", fg="yellow"
                )
                return

            # 3) Map the matching blobs back to the (path, event, time) versions
            #    that reference them, grouped by file then time.
            occurrences = _occurrences(conn, root_id, branch_id, root, blob_hits)
            _report(regex, occurrences, blob_hits, limit)
        finally:
            conn.close()


def _candidate_blobs(
    conn: "X.sqlite3.Connection", root_id: int, branch_id: int | None
) -> set[str]:
    """Distinct content digests recorded for this root's active timeline.

    Every version of a file is either a delta's ``after_hash`` (the content it
    became) or a baseline hash (its content at tracking start), so this set
    covers everything that ever existed — including now-deleted content.
    """
    sql = (
        "SELECT DISTINCT d.after_hash AS h"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND d.after_hash IS NOT NULL"
    )
    params: list[object] = [root_id]
    if branch_id is not None:
        sql += " AND e.branch_id = ?"
        params.append(branch_id)
    blobs = {r["h"] for r in conn.execute(sql, params)}
    for r in conn.execute(
        "SELECT DISTINCT hash AS h FROM root_baseline WHERE root_id = ?", (root_id,)
    ):
        blobs.add(r["h"])
    return blobs


def _grep_blobs(
    store: "X.ObjectStore", regex: "re.Pattern[str]", digests: set[str]
) -> dict[str, _BlobHit]:
    """Search each unique blob once; return only those that matched."""
    hits: dict[str, _BlobHit] = {}
    for digest in digests:
        try:
            data = store.get(digest)
        except (KeyError, ValueError):
            continue  # blob missing or corrupt — skip, never crash
        if b"\x00" in data[:_BINARY_SNIFF]:
            continue  # looks binary
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # not valid text — nothing to grep
        hit = _BlobHit()
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hit.total += 1
                if len(hit.lines) < _STORED_LINES_PER_BLOB:
                    hit.lines.append((lineno, line))
        if hit.total:
            hits[digest] = hit
    return hits


def _occurrences(
    conn: "X.sqlite3.Connection",
    root_id: int,
    branch_id: int | None,
    root: "X.sqlite3.Row",
    matched: dict[str, _BlobHit],
) -> list[_Occurrence]:
    """Every (path, event/baseline, time) version referencing a matched blob.

    We join deltas to events (for time + command) and scan root_baseline, then
    keep the rows whose content is in ``matched``. Membership is filtered in
    Python rather than via a SQL ``IN`` list so a pattern that matches many
    blobs can't blow past SQLite's bound-variable limit.
    """
    occ: list[_Occurrence] = []

    sql = (
        "SELECT d.path AS path, d.after_hash AS digest, e.id AS event_id,"
        " e.started_at AS ts, e.command AS command"
        " FROM deltas d JOIN events e ON e.id = d.event_id"
        " WHERE e.root_id = ? AND d.after_hash IS NOT NULL"
    )
    params: list[object] = [root_id]
    if branch_id is not None:
        sql += " AND e.branch_id = ?"
        params.append(branch_id)
    for r in conn.execute(sql, params):
        if r["digest"] in matched:
            occ.append(
                _Occurrence(
                    path=r["path"],
                    ts=float(r["ts"]),
                    digest=r["digest"],
                    event_id=int(r["event_id"]),
                    command=r["command"],
                )
            )

    # Baseline content has no event of its own; anchor it at the root's start
    # time so it sorts ahead of every event that touched the same file.
    base_ts = float(root["added_at"])
    for r in conn.execute(
        "SELECT path, hash AS digest FROM root_baseline WHERE root_id = ?", (root_id,)
    ):
        if r["digest"] in matched:
            occ.append(
                _Occurrence(
                    path=r["path"],
                    ts=base_ts,
                    digest=r["digest"],
                    event_id=None,
                    command=None,
                )
            )

    # Group by path; within a path the baseline (tree at tracking start) always
    # comes first, then events oldest-first. We can't lean on ts for that: the
    # root's added_at can land a hair *after* the first event's started_at.
    occ.sort(key=lambda o: (o.path, 0 if o.event_id is None else 1, o.ts, o.event_id or 0))
    return occ


def _report(
    regex: "re.Pattern[str]",
    occurrences: list[_Occurrence],
    blob_hits: dict[str, _BlobHit],
    limit: int,
) -> None:
    """Print occurrences grouped by file then time, honouring ``limit``."""
    shown = occurrences[: max(limit, 0)]
    truncated = len(occurrences) - len(shown)

    current_path: str | None = None
    for occ in shown:
        if occ.path != current_path:
            if current_path is not None:
                X.click.echo()  # blank line between files
            current_path = occ.path
            X.click.secho(occ.path, fg="magenta", bold=True)

        X.click.echo("  " + _header(occ))

        hit = blob_hits[occ.digest]
        for lineno, text in hit.lines[:_LINES_PER_OCCURRENCE]:
            X.click.echo("      " + _format_line(regex, lineno, text))
        extra = hit.total - min(_LINES_PER_OCCURRENCE, len(hit.lines))
        if extra > 0:
            X.click.secho(f"      … +{extra} more matching line(s)", dim=True)

    X.click.echo()
    files = len({o.path for o in shown})
    X.click.secho(
        f"{len(shown)} version(s) across {files} file(s) matched", fg="green"
    )
    if truncated > 0:
        X.click.secho(
            f"  ({truncated} more not shown — raise --limit)", dim=True
        )


def _header(occ: _Occurrence) -> str:
    """The one-line description of a single matching version."""
    if occ.event_id is None:
        return (
            X.click.style("baseline", fg="yellow")
            + X.click.style("  (tree at tracking start)", dim=True)
        )
    cmd = occ.command if occ.command is not None else "(external change)"
    return f"{X.click.style(f'#{occ.event_id}', fg='green')} {X.fmt_ts(occ.ts)}  {cmd}"


def _format_line(regex: "re.Pattern[str]", lineno: int, text: str) -> str:
    """A ``lineno: text`` row with matched spans highlighted."""
    display, clipped = (
        (text[:_MAX_LINE_LEN], True) if len(text) > _MAX_LINE_LEN else (text, False)
    )
    num = X.click.style(f"{lineno}:", fg="green")
    tail = X.click.style(" …", dim=True) if clipped else ""
    return f"{num} {_highlight(regex, display)}{tail}"


def _highlight(regex: "re.Pattern[str]", text: str) -> str:
    """Wrap every (non-empty) match in ``text`` with a highlight colour."""
    out: list[str] = []
    pos = 0
    for m in regex.finditer(text):
        start, end = m.span()
        if end == start:
            continue  # zero-width match — nothing to colour
        out.append(text[pos:start])
        out.append(X.click.style(text[start:end], fg="red", bold=True))
        pos = end
    out.append(text[pos:])
    return "".join(out)
