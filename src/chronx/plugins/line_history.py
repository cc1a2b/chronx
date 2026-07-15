"""chronx line-history — per-line archaeology across a file's recorded history.

Trace the *lifecycle* of the line(s) in a file that match a regular expression:
when such a line first appeared, when it was removed, when it changed, and when
it was re-added — across every version chronx ever recorded for the file (on the
current root's active timeline).

How it works: chronx stores, per path, a baseline snapshot (the file's content
when tracking began) followed by one content version per event that wrote the
file. This walks those versions oldest -> newest and, for each consecutive pair,
diffs the *set of matching lines*. A matching line present in the newer version
but not the older is reported as added (``+``); one present in the older but not
the newer is reported as removed (``-``). A line removed and later re-added thus
shows up as a ``-`` and then a later ``+``; a changed line (e.g.
``DEBUG = True`` -> ``DEBUG = False``) shows up as a ``-`` of the old text and a
``+`` of the new text at the same event.

Read-only over the store: it never touches the working tree or the database.
"""

from __future__ import annotations

import re

from chronx import pluginlib as X

# Leading bytes sniffed for a NUL when deciding a blob is binary (matches the
# heuristic used by chronx's other content-reading plugins, e.g. grep/annotate).
_BINARY_SNIFF = 8192
# Over-long content/command lines are clipped so the timeline stays scannable.
_MAX_LINE_LEN = 240


# Plain __slots__ classes (not @dataclass) on purpose, mirroring the sibling
# plugins: the loader exec's these modules specially, and staying dependency-free
# keeps them robust across Python versions.
class _Version:
    """One recorded content-state of a path, in chronological order.

    ``digest`` is the content hash of this version's bytes, or ``None`` for a
    deletion event (modelled as an empty version so the lines it removed are
    reported). ``event_id`` is 0 for the pre-history baseline snapshot.
    """

    __slots__ = ("event_id", "started_at", "command", "digest", "is_baseline")

    def __init__(
        self,
        event_id: int,
        started_at: float,
        command: str | None,
        digest: str | None,
        is_baseline: bool,
    ) -> None:
        self.event_id = event_id
        self.started_at = started_at
        self.command = command
        self.digest = digest
        self.is_baseline = is_baseline


class _Entry:
    """One timeline event: a matching line added/removed/present at a version."""

    __slots__ = ("ver", "sign", "line")

    def __init__(self, ver: _Version, sign: str, line: str) -> None:
        self.ver = ver  # the version at which the change was observed
        self.sign = sign  # '+' added, '-' removed, '=' present from baseline
        self.line = line  # the matching line's text


# --------------------------------------------------------------------- helpers


def _label(v: _Version) -> str:
    return "baseline" if v.is_baseline else f"#{v.event_id}"


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving de-duplication (matching lines are compared as a set,
    but first-seen order is kept for stable, readable output)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _clip(text: str) -> str:
    """Clip an over-long single line for display."""
    return text if len(text) <= _MAX_LINE_LEN else text[:_MAX_LINE_LEN] + " …"


def _oneline(command: str | None) -> str:
    """A single-line, length-clipped rendering of a command for the header."""
    if command is None:
        return "(external change)"
    return _clip(" ".join(command.split()))


def _relpath(file, root_path: str) -> str:
    """``file`` expressed relative to its root, using forward slashes.

    Falls back to the absolute posix path when ``file`` is outside the root (it
    then simply won't match any recorded path -> a clean "never recorded" note).
    """
    resolved = file.resolve()
    try:
        return resolved.relative_to(X.Path(root_path)).as_posix()
    except ValueError:
        return resolved.as_posix()


def _version_lines(store, digest: str | None) -> tuple[list[str] | None, str | None]:
    """Decode a version's blob into utf-8 lines.

    Returns ``(lines, None)`` on success, else ``(None, reason)`` where reason is
    'missing' (blob absent/corrupt) or 'binary' (NUL byte or non-utf8). A
    ``None`` digest is a deletion, modelled as an empty (readable) version.
    """
    if digest is None:
        return [], None
    try:
        data = store.get(digest)
    except (KeyError, ValueError):
        return None, "missing"
    if b"\x00" in data[:_BINARY_SNIFF]:
        return None, "binary"
    try:
        return data.decode("utf-8").splitlines(), None
    except UnicodeDecodeError:
        return None, "binary"


def _collect_versions(
    conn, root, root_id: int, rel: str, branch_id: int | None
) -> list[_Version]:
    """Chronological content-versions of ``rel`` on the active timeline.

    Baseline (if the path existed at tracking start) comes first as event 0,
    then every event that touched the path, ordered by event id. A modify/add
    contributes its ``after_hash`` as the version content; a delete contributes
    an empty version (``digest=None``) so its removed lines are reported.
    """
    versions: list[_Version] = []

    baseline = X.dbm.root_baseline(conn, root_id)  # path -> (hash, mode)
    if rel in baseline:
        bhash, _bmode = baseline[rel]
        versions.append(
            _Version(
                event_id=0,
                started_at=float(root["added_at"]),
                command=None,
                digest=bhash,
                is_baseline=True,
            )
        )

    # Every event that wrote this path, oldest first, scoped to the active
    # branch (when known) so the trace stays on the current timeline.
    sql = (
        "SELECT e.id AS event_id, e.started_at AS started_at, "
        "e.command AS command, d.after_hash AS after_hash "
        "FROM deltas d JOIN events e ON e.id = d.event_id "
        "WHERE d.path = ? AND e.root_id = ?"
    )
    params: list[object] = [rel, root_id]
    if branch_id is not None:
        sql += " AND e.branch_id = ?"
        params.append(branch_id)
    sql += " ORDER BY e.id ASC"

    for r in conn.execute(sql, params):
        versions.append(
            _Version(
                event_id=int(r["event_id"]),
                started_at=float(r["started_at"]),
                command=r["command"],
                digest=r["after_hash"],  # None for a deletion -> empty version
                is_baseline=False,
            )
        )
    return versions


def _timeline(
    store, versions: list[_Version], regex: re.Pattern[str]
) -> tuple[list[_Entry], list[tuple[str, str]], list[str]]:
    """Walk versions oldest -> newest, emitting +/- entries per matching-line
    change.

    Returns ``(entries, skipped, current)`` where ``entries`` is the ordered
    timeline, ``skipped`` is ``(label, reason)`` for unreadable versions, and
    ``current`` is the matching lines present in the last readable version.
    """
    entries: list[_Entry] = []
    skipped: list[tuple[str, str]] = []
    prev_set: set[str] | None = None
    prev_order: list[str] = []

    for v in versions:
        lines, reason = _version_lines(store, v.digest)
        if lines is None:
            # Binary/missing version: it cannot serve as a diff basis, so skip
            # it. prev_* is NOT advanced — the next readable version is compared
            # to the last readable one (graceful degradation, à la `annotate`).
            skipped.append((_label(v), reason or "unreadable"))
            continue

        matching = _dedupe([ln for ln in lines if regex.search(ln)])
        matching_set = set(matching)

        if prev_set is None:
            # First readable version. Matching lines already in the baseline are
            # "present from baseline"; in any other first version they are new.
            sign = "=" if v.is_baseline else "+"
            for ln in matching:
                entries.append(_Entry(v, sign, ln))
        else:
            removed = [ln for ln in prev_order if ln not in matching_set]
            added = [ln for ln in matching if ln not in prev_set]
            # Removed-then-added renders a same-event change as `- old` / `+ new`.
            for ln in removed:
                entries.append(_Entry(v, "-", ln))
            for ln in added:
                entries.append(_Entry(v, "+", ln))

        prev_set = matching_set
        prev_order = matching

    return entries, skipped, prev_order


# ------------------------------------------------------------------- rendering


def _render(
    rel: str,
    pattern: str,
    entries: list[_Entry],
    skipped: list[tuple[str, str]],
    current: list[str],
) -> None:
    """Print the chronological timeline, then a summary footer."""
    X.click.secho(f"line-history of {rel} — lines matching /{pattern}/", bold=True)
    X.click.echo()

    # Lines already present when tracking began (not counted as events).
    baseline_lines = [e.line for e in entries if e.sign == "="]
    if baseline_lines:
        X.click.secho("present from baseline (tree at tracking start):", fg="yellow")
        for ln in baseline_lines:
            X.click.echo("    " + _clip(ln))
        X.click.echo()

    # The add/remove events, grouped by the event that produced them. Entries
    # for one event are contiguous (built per version, in event-id order).
    changes = [e for e in entries if e.sign != "="]
    last_eid: int | None = None
    for e in changes:
        if e.ver.event_id != last_eid:
            if last_eid is not None:
                X.click.echo()
            last_eid = e.ver.event_id
            head = X.click.style(f"#{e.ver.event_id}", fg="cyan", bold=True)
            cmd = X.click.style(_oneline(e.ver.command), dim=True)
            X.click.echo(f"{head}  {X.fmt_ts(e.ver.started_at)}  {cmd}")
        colour = "green" if e.sign == "+" else "red"
        X.click.secho(f"  {e.sign} {_clip(e.line)}", fg=colour)
    if changes:
        X.click.echo()

    if skipped:
        detail = ", ".join(f"{lbl} ({reason})" for lbl, reason in skipped)
        X.click.secho(
            f"skipped {len(skipped)} unreadable version(s): {detail}",
            fg="yellow",
            dim=True,
        )
        X.click.echo()

    n_events = len({e.ver.event_id for e in changes})
    X.click.secho(
        f"{n_events} event(s) touched lines matching /{pattern}/ in {rel}", fg="green"
    )
    X.click.echo(f"{len(current)} matching line(s) currently present in {rel}")


# ---------------------------------------------------------------- registration


def register(main) -> None:
    @main.command("line-history")
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.argument("pattern")
    @X.click.option(
        "-i", "--ignore-case", is_flag=True, help="Case-insensitive matching."
    )
    def line_history(file, pattern, ignore_case):  # type: ignore[no-untyped-def]
        """Trace when lines matching PATTERN appeared/changed/vanished in FILE."""
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise X.click.ClickException(f"bad regex: {exc}")

        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            rel = _relpath(file, root["path"])
            branch_id = X.active_branch_id(conn, root_id)

            versions = _collect_versions(conn, root, root_id, rel, branch_id)
            if not versions:
                X.click.secho(
                    f"no recorded version of {rel} — chronx has never captured "
                    "its content (created before tracking, ignored, or never "
                    "written here)",
                    fg="yellow",
                )
                return

            entries, skipped, current = _timeline(store, versions, regex)
            if not entries:
                readable = len(versions) - len(skipped)
                if readable == 0:
                    X.click.secho(
                        f"all {len(versions)} recorded version(s) of {rel} are "
                        "binary or unreadable — nothing to trace",
                        fg="yellow",
                    )
                else:
                    X.click.secho(
                        f"no recorded version of {rel} ever contained a line "
                        f"matching /{pattern}/",
                        fg="yellow",
                    )
                return

            _render(rel, pattern, entries, skipped, current)
        finally:
            conn.close()
