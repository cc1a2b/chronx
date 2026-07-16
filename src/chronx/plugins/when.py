"""chronx when — pinpoint the command that introduced (or removed) a match.

Content archaeology: given a file and a regular expression, walk every version
of that file chronx ever recorded (on the current root's active timeline) and
find the exact event WHERE the file first *started* matching the pattern — the
transition from not-matching -> matching. With ``--gone`` it finds the reverse,
the match -> not transition: when a string/line/bug was *removed*.

This answers "which command introduced this bug/line/string?" (or "which one
finally deleted it?"). chronx stores, per path, a baseline snapshot (the file's
content when tracking began) followed by one content version per event that
wrote the file; this scans that ordered sequence, evaluating the regex against
each version, and reports the boundary where the match state flips.

Whether a version matches is a near-monotone predicate, so the transition could
be found by binary search; but recorded versions of one file are few and content
can legitimately toggle (introduced, removed, re-introduced). A single ordered
linear scan therefore both finds the first transition efficiently AND surfaces
*every* toggle — strictly more useful than a bisection that assumes monotonicity.

Read-only over the store: it never touches the working tree or the database.
"""

from __future__ import annotations

import re

from chronx import pluginlib as X

# Leading bytes sniffed for a NUL when deciding a blob is binary (matches the
# heuristic used by chronx's other content-reading plugins, e.g. annotate/grep).
_BINARY_SNIFF = 8192
# Over-long content/command lines are clipped so the report stays scannable.
_MAX_LINE_LEN = 200


# Plain __slots__ class (not @dataclass) on purpose, mirroring the sibling
# plugins: the loader exec's these modules without a normal import, and a
# dataclass under ``from __future__ import annotations`` can fail to resolve its
# own module. Staying dependency-free keeps the plugin robust.
class _Version:
    """One recorded content-state of a path, in chronological order.

    ``digest`` is the content hash of this version's bytes, or ``None`` for a
    deletion event (modelled as empty content). ``event_id`` is 0 for the
    pre-history baseline snapshot.
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


class _State:
    """A readable version's evaluated match state.

    ``matches`` is whether the regex hit this version's content; ``snippet`` is
    the first matching line (for quoting), or ``None`` when it did not match.
    """

    __slots__ = ("ver", "matches", "snippet")

    def __init__(self, ver: _Version, matches: bool, snippet: str | None) -> None:
        self.ver = ver
        self.matches = matches
        self.snippet = snippet


class _Transition:
    """A flip in match state between two consecutive readable versions.

    ``kind`` is 'appear' (not-matching -> matching; ``ver`` produced the match)
    or 'disappear' (matching -> not; ``ver`` removed it, ``prev`` last matched).
    ``snippet`` quotes the matching line involved.
    """

    __slots__ = ("kind", "ver", "prev", "snippet")

    def __init__(
        self, kind: str, ver: _Version, prev: _Version | None, snippet: str | None
    ) -> None:
        self.kind = kind
        self.ver = ver
        self.prev = prev
        self.snippet = snippet


# --------------------------------------------------------------------- helpers


def _label(v: _Version) -> str:
    """Human label for a version: ``baseline`` or ``#<event id>``."""
    return "baseline" if v.is_baseline else f"#{v.event_id}"


def _clip(text: str) -> str:
    """Clip an over-long single line for display."""
    text = text.rstrip("\n")
    return text if len(text) <= _MAX_LINE_LEN else text[:_MAX_LINE_LEN] + " …"


def _command_line(v: _Version) -> str:
    """A single-line ``$ command`` rendering (baseline/external handled)."""
    if v.is_baseline:
        return "(snapshot at tracking start)"
    if v.command is None:
        return "(external change)"
    return "$ " + _clip(" ".join(v.command.split()))


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


def _decode_version(store, digest: str | None) -> tuple[str | None, str | None]:
    """Decode a version's blob into utf-8 text.

    Returns ``(content, None)`` on success, else ``(None, reason)`` where reason
    is 'missing' (blob absent/corrupt) or 'binary' (NUL byte or non-utf8). A
    ``None`` digest is a deletion, modelled as readable empty content.
    """
    if digest is None:
        return "", None
    try:
        data = store.get(digest)
    except (KeyError, ValueError):
        return None, "missing"
    if b"\x00" in data[:_BINARY_SNIFF]:
        return None, "binary"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "binary"


def _first_match_line(content: str, regex: re.Pattern[str]) -> str | None:
    """The first line of ``content`` that the regex hits, for quoting.

    Falls back to the matched text itself when the match spans lines / has no
    single line (e.g. a multi-line or whitespace-crossing pattern).
    """
    for line in content.splitlines():
        if regex.search(line):
            return line
    m = regex.search(content)
    return m.group(0) if m else None


def _collect_versions(
    conn, root, root_id: int, rel: str, branch_id: int | None
) -> list[_Version]:
    """Chronological content-versions of ``rel`` on the active timeline.

    Baseline (if the path existed at tracking start) comes first as event 0,
    then every event that wrote the path, ordered by event id. A modify/add
    contributes its ``after_hash``; a delete contributes ``None`` (empty
    content) so the removal is observed as a not-matching version.
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
    # branch (when known) so the scan stays on the current timeline.
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


def _evaluate(
    store, versions: list[_Version], regex: re.Pattern[str]
) -> tuple[list[_State], list[tuple[str, str]]]:
    """Evaluate the regex against every version.

    Returns ``(states, skipped)``. ``states`` holds one entry per *readable*
    version (in order); ``skipped`` is ``(label, reason)`` for binary/missing
    versions, which cannot be evaluated and are treated as "not matching" —
    they are excluded from the state sequence so they never fabricate a
    transition (graceful degradation, à la annotate/line-history).
    """
    states: list[_State] = []
    skipped: list[tuple[str, str]] = []
    for v in versions:
        content, reason = _decode_version(store, v.digest)
        if content is None:
            skipped.append((_label(v), reason or "unreadable"))
            continue
        if regex.search(content):
            states.append(_State(v, True, _first_match_line(content, regex)))
        else:
            states.append(_State(v, False, None))
    return states, skipped


def _transitions(states: list[_State]) -> list[_Transition]:
    """Ordered match-state flips across consecutive readable versions.

    The first readable version that matches counts as an 'appear' (the file went
    from having no recorded content -> matching content at that event, or was
    already matching in the baseline snapshot).
    """
    transitions: list[_Transition] = []
    prev: _State | None = None
    for st in states:
        if prev is None:
            if st.matches:
                transitions.append(_Transition("appear", st.ver, None, st.snippet))
        elif st.matches and not prev.matches:
            transitions.append(_Transition("appear", st.ver, None, st.snippet))
        elif prev.matches and not st.matches:
            transitions.append(
                _Transition("disappear", st.ver, prev.ver, prev.snippet)
            )
        prev = st
    return transitions


# ------------------------------------------------------------------- rendering


def _skip_note(skipped: list[tuple[str, str]]) -> None:
    if not skipped:
        return
    detail = ", ".join(f"{lbl} ({reason})" for lbl, reason in skipped)
    X.click.secho(
        f"note: skipped {len(skipped)} unreadable version(s): {detail}",
        fg="yellow",
        dim=True,
    )


def _toggle_note(rel: str, pattern: str, transitions: list[_Transition]) -> None:
    """When a file toggled more than once, say so and list every transition."""
    if len(transitions) <= 1:
        return
    X.click.echo()
    X.click.secho(
        f"{rel} matched/unmatched /{pattern}/ {len(transitions)} times "
        "— showing the first transition above; full history:",
        fg="yellow",
        dim=True,
    )
    for t in transitions:
        verb = "matched" if t.kind == "appear" else "removed"
        arrow = X.click.style("+" if t.kind == "appear" else "-", dim=True)
        head = X.click.style(f"{_label(t.ver):>10}", fg="cyan", dim=True)
        X.click.echo(f"  {arrow} {head}  {X.fmt_ts(t.ver.started_at)}  {verb}")


def _report_appear(rel: str, pattern: str, transitions: list[_Transition]) -> None:
    """Default mode: report the event that first introduced the match."""
    if not transitions:
        X.click.secho(
            f"{rel} never matched /{pattern}/ in its recorded history",
            fg="yellow",
        )
        return

    first = transitions[0]
    v = first.ver
    if v.is_baseline:
        X.click.secho(
            f"{rel} matched /{pattern}/ from the baseline "
            f"(present at tracking start, {X.fmt_ts(v.started_at)})",
            fg="green",
            bold=True,
        )
    else:
        X.click.secho(
            f"first matched /{pattern}/ at {_label(v)}  {X.fmt_ts(v.started_at)}",
            fg="green",
            bold=True,
        )
        X.click.secho(f"    {_command_line(v)}", dim=True)
    if first.snippet is not None:
        X.click.echo("    ┆ " + _clip(first.snippet))

    _toggle_note(rel, pattern, transitions)


def _report_gone(
    rel: str, pattern: str, states: list[_State], transitions: list[_Transition]
) -> None:
    """``--gone`` mode: report the event that removed the match."""
    appeared = any(t.kind == "appear" for t in transitions)
    removals = [t for t in transitions if t.kind == "disappear"]

    if not appeared:
        X.click.secho(
            f"{rel} never matched /{pattern}/ in its recorded history "
            "(nothing to remove)",
            fg="yellow",
        )
        return
    if not removals:
        # It matched and, once matching, never stopped -> still present.
        X.click.secho(
            f"{rel} still matches /{pattern}/ (never removed)",
            fg="green",
            bold=True,
        )
        return

    first = removals[0]
    if first.prev is not None:
        X.click.secho(
            f"last present at {_label(first.prev)}  {X.fmt_ts(first.prev.started_at)}",
            fg="yellow",
        )
        if first.snippet is not None:
            X.click.echo("    ┆ " + _clip(first.snippet))
    X.click.secho(
        f"removed at {_label(first.ver)}  {X.fmt_ts(first.ver.started_at)}",
        fg="red",
        bold=True,
    )
    X.click.secho(f"    {_command_line(first.ver)}", dim=True)

    _toggle_note(rel, pattern, transitions)


# ---------------------------------------------------------------- registration


def register(main) -> None:
    @main.command()
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.argument("pattern")
    @X.click.option(
        "-i", "--ignore-case", "ignore_case", is_flag=True, help="Case-insensitive."
    )
    @X.click.option(
        "--gone",
        is_flag=True,
        help="Find when the file STOPPED matching instead of started.",
    )
    def when(file, pattern, ignore_case, gone):  # type: ignore[no-untyped-def]
        """Pinpoint the command WHERE FILE first matched (or, --gone, stopped
        matching) PATTERN — content archaeology over the file's versions."""
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise X.click.ClickException(f"bad regex: {exc}")

        conn = X.open_db()  # read-only; raises a clean ClickException if absent
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
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

            states, skipped = _evaluate(store, versions, regex)
            if not states:
                X.click.secho(
                    f"all {len(versions)} recorded version(s) of {rel} are binary "
                    "or unreadable — nothing to scan",
                    fg="yellow",
                )
                _skip_note(skipped)
                return

            transitions = _transitions(states)
            if gone:
                _report_gone(rel, pattern, states, transitions)
            else:
                _report_appear(rel, pattern, transitions)
            _skip_note(skipped)
        finally:
            conn.close()
