"""chronx blame-stats — aggregate line-ownership of a file across history.

Where ``chronx annotate`` shows *per line* which event introduced it, this
command rolls those same per-line attributions up into *per event* totals:
"which recorded command wrote how much of this file?" — like the summary
counts of ``git blame``. For the current (or ``--at``) version of a file every
line is attributed to the event that introduced it (identical walk + diff to
annotate), then events are ranked by how many lines they own.

Read-only over the store: it never touches the working tree or the database.
"""

from __future__ import annotations

import collections
import difflib
import os
import time
from datetime import datetime

from chronx import pluginlib as X

# Bytes to sniff when deciding a blob is binary (matches annotate/diffview).
_BINARY_SNIFF = 8192
# Width, in cells, of the bar drawn for the top owner; others scale to it.
_BAR_WIDTH = 24


class _Version:
    """One recorded content-state of a path, in chronological order.

    A plain ``__slots__`` class (not a dataclass) on purpose: the plugin loader
    ``exec``s this module without registering it in ``sys.modules``, so a
    ``@dataclass`` under ``from __future__ import annotations`` would fail to
    resolve its own module. This stays dependency-free and safe.
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
    """Short id for an owner: 'baseline' or '#<event id>'."""
    return "baseline" if v.is_baseline else f"#{v.event_id}"


def _command_of(v: _Version) -> str:
    """Human description of what produced a version."""
    if v.is_baseline:
        return "snapshot at tracking start"
    return v.command if v.command is not None else "(external change)"


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
    # by the active branch keeps the tally confined to the current timeline;
    # when branch_id is unknown (pre-migration store) we skip that filter.
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
    versions are skipped as a diff basis (graceful degradation). This is the
    same algorithm as ``annotate``; blame-stats only differs in how it renders.
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


def _bar(count: int, top: int) -> str:
    """Unicode block bar for ``count`` lines, scaled so the top owner fills it."""
    if top <= 0:
        return ""
    filled = int(round(count / top * _BAR_WIDTH))
    # Any owner with lines gets at least one visible cell.
    if count > 0:
        filled = max(1, filled)
    return "█" * filled


def _render(
    rel: str,
    target: _Version,
    owners: list[_Version],
) -> None:
    """Print a ranked table of how many lines each event owns."""
    total = len(owners)

    # Tally lines per owning event, keeping one representative _Version each.
    counts: "collections.Counter[int]" = collections.Counter()
    rep: dict[int, _Version] = {}
    for o in owners:
        counts[o.event_id] += 1
        rep.setdefault(o.event_id, o)

    # Rank: most lines first; ties broken by event id (baseline == 0 leads).
    ranked = sorted(
        counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    top_count = ranked[0][1]

    # Headline.
    X.click.secho(
        f"{rel}: {total} line(s) from {len(ranked)} "
        f"command(s) (as of {_owner_label(target)} @ {_short_ts(target.started_at)})",
        bold=True,
    )
    X.click.echo()

    # Align the id column (e.g. 'baseline' vs '#12').
    id_width = max(len(_owner_label(rep[eid])) for eid, _ in ranked)

    for eid, n in ranked:
        v = rep[eid]
        pct = n / total * 100 if total else 0.0
        lines_cell = X.click.style(f"{n:>5}", fg="green")
        pct_cell = X.click.style(f"{pct:5.1f}%", dim=True)
        bar_cell = X.click.style(_bar(n, top_count), fg="cyan")
        label = X.click.style(f"{_owner_label(v):<{id_width}}", fg="cyan", bold=True)
        when = X.click.style(_short_ts(v.started_at), dim=True)
        cmd = _command_of(v)
        X.click.echo(f"{lines_cell}  {pct_cell}  {bar_cell}  {label} {when}  $ {cmd}")

    # One-line takeaway: the dominant author of the file.
    top_eid, top_n = ranked[0]
    top_v = rep[top_eid]
    top_pct = top_n / total * 100 if total else 0.0
    X.click.echo()
    X.click.secho(
        f"most of this file ({top_pct:.0f}%) was written by "
        f"{_owner_label(top_v)} $ {_command_of(top_v)}",
        dim=True,
    )


# ---------------------------------------------------------------- registration


def register(main) -> None:
    @main.command("blame-stats")
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.option(
        "--at",
        "-t",
        default=None,
        help="Analyze the version at this moment (mark, event id, '10m', "
        "'14:32', ISO, or epoch). Default: the latest recorded version.",
    )
    def blame_stats(file, at):  # type: ignore[no-untyped-def]
        """Rank recorded commands by how many lines of FILE each one owns."""
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

            # The target must be readable text to attribute its lines.
            target_lines, reason = _version_lines(store, target.digest)
            if reason == "missing":
                raise X.click.ClickException(
                    f"the recorded content of {rel} is missing from the object "
                    "store (run `chronx fsck`)"
                )
            if reason == "binary" or target_lines is None:
                X.click.echo(f"{rel}: binary file; cannot blame")
                return
            if not target_lines:
                X.click.secho(f"{rel}: empty file (no lines to blame)", dim=True)
                return

            owners = _blame(store, walk, target, target_lines)
            _render(rel, target, owners)
        finally:
            conn.close()
