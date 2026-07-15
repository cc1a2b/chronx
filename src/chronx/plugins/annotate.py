"""chronx annotate — line-level temporal blame across recorded history.

Like ``git blame``, but the "commits" are chronx events. For a file (at
``--at``, default its latest recorded version) every line of content is
attributed to the event that introduced that line, by walking the chronological
sequence of recorded versions of the path and diffing consecutive versions.

Read-only over the store: it never touches the working tree or the database.
"""

from __future__ import annotations

import difflib
import os
import time
from datetime import datetime

from chronx import pluginlib as X

# Bytes to sniff when deciding a blob is binary (matches diffview's heuristic).
_BINARY_SNIFF = 8192


class _Version:
    """One recorded content-state of a path, in chronological order.

    A plain ``__slots__`` class rather than a dataclass on purpose: the plugin
    loader ``exec``s this module without registering it in ``sys.modules``, and
    ``@dataclass`` under ``from __future__ import annotations`` would then fail
    resolving its own module. This stays dependency-free and safe.
    """

    __slots__ = ("event_id", "started_at", "command", "digest", "is_baseline")

    def __init__(
        self,
        event_id: int,  # 0 for the pre-history baseline snapshot
        started_at: float,  # event start time (baseline: when tracking began)
        command: str | None,  # producing command (None for baseline/external)
        digest: str,  # content hash of this version's bytes
        is_baseline: bool,
    ) -> None:
        self.event_id = event_id
        self.started_at = started_at
        self.command = command
        self.digest = digest
        self.is_baseline = is_baseline


# --------------------------------------------------------------------- helpers


def _owner_label(v: _Version) -> str:
    return "baseline" if v.is_baseline else f"#{v.event_id}"


def _short_ts(ts: float) -> str:
    """Compact local time, e.g. '07-14 10:05'."""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _version_lines(
    store: "X.ObjectStore", digest: str
) -> tuple[list[str] | None, str | None]:
    """Decode a version's blob into utf-8 lines.

    Returns ``(lines, None)`` on success, else ``(None, reason)`` where reason
    is 'missing' (blob absent/corrupt) or 'binary' (NUL byte or non-utf8).
    """
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
    conn: "X.sqlite3.Connection",
    root: "X.sqlite3.Row",
    root_id: int,
    rel: str,
    branch_id: int | None,
) -> list[_Version]:
    """Chronological content-versions of ``rel`` on the active timeline.

    Baseline (if the path existed at tracking start) comes first as event 0,
    then every event delta that gave the path new content, ordered by event id.
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

    # Every event that wrote new content to this path, oldest first. Filtering
    # by the active branch keeps blame confined to the current timeline; when
    # branch_id is unknown (pre-migration read-only store) we skip that filter.
    sql = (
        "SELECT e.id AS event_id, e.started_at AS started_at, e.command AS command, "
        "d.after_hash AS after_hash "
        "FROM deltas d JOIN events e ON e.id = d.event_id "
        "WHERE d.path = ? AND e.root_id = ? AND d.after_hash IS NOT NULL"
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
                digest=r["after_hash"],
                is_baseline=False,
            )
        )
    return versions


def _select_target(versions: list[_Version], at_ts: float) -> int | None:
    """Index of the last version whose event started at or before ``at_ts``."""
    target_idx: int | None = None
    for i, v in enumerate(versions):
        if v.started_at <= at_ts:
            target_idx = i
    return target_idx


def _blame(
    store: "X.ObjectStore",
    walk: list[_Version],
    target: _Version,
    target_lines: list[str],
) -> list[_Version]:
    """Attribute each of ``target_lines`` to the version that introduced it.

    Walks versions oldest -> target, aligning with ``difflib.SequenceMatcher``:
    'equal' spans carry the previous owner forward; anything else (replace /
    insert) is credited to the current version. Undecodable intermediate
    versions are skipped as a diff basis (graceful degradation).
    """
    prev_lines: list[str] | None = None
    owners: list[_Version] = []
    for v in walk:
        if v is target:
            lines = target_lines
        else:
            lines, _reason = _version_lines(store, v.digest)
            if lines is None:
                continue  # missing/binary mid-history version — cannot diff it
        if prev_lines is None:
            # First readable version: it owns all of its lines outright.
            owners = [v] * len(lines)
        else:
            sm = difflib.SequenceMatcher(a=prev_lines, b=lines, autojunk=False)
            new_owners: list[_Version] = [v] * len(lines)
            for tag, i1, _i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    for k in range(j2 - j1):
                        new_owners[j1 + k] = owners[i1 + k]
            owners = new_owners
        prev_lines = lines
    return owners


def _render(
    rel: str, target: _Version, target_lines: list[str], owners: list[_Version]
) -> None:
    """Print one blame row per line, plus a compact legend of cited events."""
    labels = [_owner_label(o) for o in owners]
    width = max((len(lbl) for lbl in labels), default=len("baseline"))

    X.click.secho(
        f"{rel} @ {_owner_label(target)} — {len(target_lines)} line(s), "
        "each credited to the event that introduced it:",
        bold=True,
    )
    X.click.echo()

    for line, owner, label in zip(target_lines, owners, labels):
        meta = X.click.style(f"{label:<{width}}", fg="cyan", dim=True)
        when = X.click.style(_short_ts(owner.started_at), dim=True)
        sep = X.click.style("│", dim=True)
        # The code itself stays in the default colour so it reads cleanly.
        X.click.echo(f"{meta} {when} {sep} {line}")

    # Legend: what each cited #id actually was (baseline first, then by id).
    seen_ids: set[int] = set()
    cited: list[_Version] = []
    for o in owners:
        if o.event_id not in seen_ids:
            seen_ids.add(o.event_id)
            cited.append(o)
    cited.sort(key=lambda v: v.event_id)

    X.click.echo()
    for v in cited:
        if v.is_baseline:
            desc = "snapshot at tracking start"
        else:
            desc = v.command if v.command is not None else "(external change)"
        X.click.secho(
            f"  {_owner_label(v):<{width}} {_short_ts(v.started_at)}  {desc}",
            dim=True,
        )


# ---------------------------------------------------------------- registration


def register(main) -> None:
    @main.command()
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.option(
        "--at",
        "-t",
        default=None,
        help="Blame the version at this moment (mark, event id, '10m', "
        "'14:32', ISO, or epoch). Default: the latest recorded version.",
    )
    def annotate(file, at):  # type: ignore[no-untyped-def]
        """Line-by-line, attribute each line of FILE to the event that wrote it."""
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            rel = os.path.relpath(file.resolve(), root["path"]).replace(os.sep, "/")
            branch_id = X.active_branch_id(conn, root_id)

            versions = _collect_versions(conn, root, root_id, rel, branch_id)
            if not versions:
                raise X.click.ClickException(
                    f"no recorded version of {rel} — chronx has never captured its "
                    "content (created before tracking, ignored, or oversized)"
                )

            at_ts = X.moment_ts(conn, at) if at else time.time()
            target_idx = _select_target(versions, at_ts)
            if target_idx is None:
                raise X.click.ClickException(
                    f"{rel} had no recorded version at or before {X.fmt_ts(at_ts)}"
                )

            walk = versions[: target_idx + 1]
            target = walk[-1]

            # The target must be readable text to blame it line by line.
            target_lines, reason = _version_lines(store, target.digest)
            if reason == "missing":
                raise X.click.ClickException(
                    f"the recorded content of {rel} is missing from the object "
                    "store (run `chronx fsck`)"
                )
            if reason == "binary" or target_lines is None:
                X.click.echo(f"{rel}: binary file; cannot annotate")
                return
            if not target_lines:
                X.click.secho(f"{rel}: empty file (no lines to annotate)", dim=True)
                return

            owners = _blame(store, walk, target, target_lines)
            _render(rel, target, target_lines, owners)
        finally:
            conn.close()
