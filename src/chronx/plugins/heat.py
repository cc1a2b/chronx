"""chronx heat — a per-LINE "temperature" map of a file across its history.

Where ``chronx blame-stats`` answers *who* wrote each line (ownership) and
``chronx hotspots`` answers *which files* churn (file-level volatility), ``heat``
zooms all the way in: for every line of the CURRENT file it counts *how many
times that line was rewritten* over the recorded history of the path. Lines that
keep getting rewritten (hot) are the unstable, bug-prone ones; lines that have
sat untouched since they were introduced are cold.

The count is built by the same chronological version-walk that ``annotate`` uses
(baseline then each recorded delta, branch-scoped, oldest-first), but instead of
attributing one owner per line it accumulates a *change counter* per surviving
line via ``difflib.SequenceMatcher`` opcodes.

Read-only over the store: it never touches the working tree or the database.
"""

from __future__ import annotations

import difflib
import os

from chronx import pluginlib as X

# Bytes to sniff when deciding a blob is binary (matches annotate/blame-stats).
_BINARY_SNIFF = 8192
# Width, in cells, of the little intensity bar drawn in the gutter.
_BAR_WIDTH = 5
# Partial-fill ramp for the last cell of a bar (index 0 == empty).
_RAMP = " ░▒▓█"
# Truncate rendered line text to this many display columns.
_MAX_TEXT = 100
# Cap rows in full-file mode so a huge file can't flood the terminal.
_MAX_ROWS = 400


class _Version:
    """One recorded content-state of a path, in chronological order.

    A plain ``__slots__`` class (not a dataclass) on purpose: the plugin loader
    ``exec``s this module without registering it in ``sys.modules``, so a
    ``@dataclass`` under ``from __future__ import annotations`` would fail to
    resolve its own module. This stays dependency-free and safe.
    """

    __slots__ = ("event_id", "digest", "is_baseline")

    def __init__(self, event_id: int, digest: str, is_baseline: bool) -> None:
        self.event_id = event_id  # 0 for the pre-history baseline snapshot
        self.digest = digest  # content hash of this version's bytes
        self.is_baseline = is_baseline


# --------------------------------------------------------------------- helpers


def _version_lines(
    store: "X.ObjectStore", digest: str
) -> tuple[list[str] | None, str | None]:
    """Decode a version's blob into utf-8 lines.

    Returns ``(lines, None)`` on success, else ``(None, reason)`` where reason
    is 'missing' (blob absent/corrupt) or 'binary' (NUL byte or non-utf8).
    Never raises — graceful degradation is the whole point here.
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
        versions.append(_Version(event_id=0, digest=bhash, is_baseline=True))

    # Every event that wrote new content to this path, oldest first. Filtering
    # by the active branch keeps the map confined to the current timeline; when
    # branch_id is unknown (pre-migration read-only store) we skip that filter.
    sql = (
        "SELECT e.id AS event_id, d.after_hash AS after_hash "
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
                digest=r["after_hash"],
                is_baseline=False,
            )
        )
    return versions


def _step(prev_lines: list[str], prev_heat: list[int], lines: list[str]) -> list[int]:
    """Carry per-line heat from one version to the next via diff opcodes.

    ``prev_heat`` is aligned to ``prev_lines``; the returned list is aligned to
    ``lines`` (the newer version):

    * 'equal'   — the line survived unchanged, so its heat is copied forward.
    * 'replace' — the region was rewritten: new lines inherit the (hottest)
                  heat of the region they replaced, plus one for this rewrite.
                  When the replacement is 1:1 the carry is positional/exact.
    * 'insert'  — brand-new lines with no predecessor: heat starts at one.
    * 'delete'  — dropped lines simply take their heat with them.
    """
    heat = [0] * len(lines)
    sm = difflib.SequenceMatcher(a=prev_lines, b=lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                heat[j1 + k] = prev_heat[i1 + k]
        elif tag == "replace":
            if (i2 - i1) == (j2 - j1):
                # 1:1 (or n:n) rewrite — carry each line's own heat exactly.
                for k in range(j2 - j1):
                    heat[j1 + k] = prev_heat[i1 + k] + 1
            else:
                # Size-changing rewrite — every new line inherits the hottest
                # heat of the replaced block, so churn is never undercounted.
                base = max(prev_heat[i1:i2], default=0)
                for j in range(j1, j2):
                    heat[j] = base + 1
        elif tag == "insert":
            for j in range(j1, j2):
                heat[j] = 1
        # 'delete': the removed lines contribute nothing to the new version.
    return heat


def _heatmap(
    store: "X.ObjectStore",
    walk: list[_Version],
    target: _Version,
    target_lines: list[str],
) -> list[int]:
    """Per-line change counts for ``target_lines``, aligned one-to-one.

    Walks versions oldest -> target. The first *readable* version seeds the
    counters: baseline lines start cold (0 — they were introduced, not changed),
    while a file first seen via an event starts at 1 (that event created it).
    Undecodable intermediate versions are skipped as a diff basis.
    """
    prev_lines: list[str] | None = None
    prev_heat: list[int] = []
    heat: list[int] = []
    for v in walk:
        if v is target:
            lines: list[str] | None = target_lines
        else:
            lines, _reason = _version_lines(store, v.digest)
            if lines is None:
                continue  # missing/binary mid-history version — cannot diff it
        if prev_lines is None:
            heat = [0] * len(lines) if v.is_baseline else [1] * len(lines)
        else:
            heat = _step(prev_lines, prev_heat, lines)
        prev_lines = lines
        prev_heat = heat
    return heat


# ---------------------------------------------------------------- presentation


def _style_for(ratio: float) -> dict[str, object]:
    """click.style kwargs picking a colour by heat intensity (0..1)."""
    if ratio <= 0.0:
        return {"dim": True}
    if ratio >= 0.67:
        return {"fg": "red", "bold": True}
    if ratio >= 0.34:
        return {"fg": "yellow"}
    return {"fg": "green"}


def _bar(heat: int, mx: int) -> str:
    """A small unicode intensity bar for ``heat`` scaled so ``mx`` fills it."""
    if mx <= 0:
        return " " * _BAR_WIDTH
    frac = heat / mx * _BAR_WIDTH
    full = int(frac)
    cells = "█" * min(full, _BAR_WIDTH)
    if full < _BAR_WIDTH:
        idx = int((frac - full) * (len(_RAMP) - 1))
        cells += _RAMP[idx] + " " * (_BAR_WIDTH - full - 1)
    return cells[:_BAR_WIDTH]


def _clip(text: str) -> str:
    """Expand tabs and truncate over-long line text for a tidy gutter."""
    text = text.replace("\t", "    ")
    if len(text) > _MAX_TEXT:
        return text[: _MAX_TEXT - 1] + "…"
    return text


def _headline(rel: str, lines: list[str], heat: list[int]) -> None:
    """Summary line: size, hottest line, cold count, and mean heat."""
    n = len(lines)
    mx = max(heat) if heat else 0
    hottest = heat.index(mx) + 1 if heat else 0  # 1-based; first hottest wins
    never = sum(1 for h in heat if h == 0)
    mean = (sum(heat) / n) if n else 0.0
    X.click.secho(
        f"{rel}: {n} line(s), hottest line changed {mx} time(s) (L{hottest}), "
        f"{never} line(s) never changed since introduction",
        bold=True,
    )
    X.click.secho(f"mean heat {mean:.2f} per line", dim=True)


def _render_full(lines: list[str], heat: list[int]) -> None:
    """Print the whole file with a heat gutter: `<heat> <bar> │ <text>`."""
    mx = max(heat) if heat else 0
    hw = len(str(mx))  # heat-column width
    total = len(lines)
    shown = min(total, _MAX_ROWS)
    for i in range(shown):
        h = heat[i]
        ratio = (h / mx) if mx else 0.0
        style = _style_for(ratio)
        heat_cell = X.click.style(f"{h:>{hw}}", **style)  # type: ignore[arg-type]
        bar_style = style if h else {"dim": True}
        bar_cell = X.click.style(_bar(h, mx), **bar_style)  # type: ignore[arg-type]
        sep = X.click.style("│", dim=True)
        X.click.echo(f"{heat_cell} {bar_cell} {sep} {_clip(lines[i])}")
    if shown < total:
        X.click.secho(
            f"... ({total - shown} more line(s) hidden; use --top N for the "
            "hottest only)",
            dim=True,
        )


def _render_top(lines: list[str], heat: list[int], top: int) -> None:
    """Print only the ``top`` hottest lines: `<heat>  L<lineno>: <text>`."""
    mx = max(heat) if heat else 0
    order = sorted(range(len(lines)), key=lambda i: (-heat[i], i))[:top]
    hw = len(str(mx))
    for i in order:
        h = heat[i]
        ratio = (h / mx) if mx else 0.0
        heat_cell = X.click.style(f"{h:>{hw}}", **_style_for(ratio))  # type: ignore[arg-type]
        loc = X.click.style(f"L{i + 1}", fg="cyan", dim=True)
        X.click.echo(f"{heat_cell}  {loc}: {_clip(lines[i])}")


# ---------------------------------------------------------------- registration


def register(main) -> None:  # type: ignore[no-untyped-def]
    @main.command()
    @X.click.argument("file", type=X.click.Path(path_type=X.Path))
    @X.click.option(
        "--top",
        "-n",
        type=int,
        default=None,
        help="Show only the N hottest lines instead of the whole file.",
    )
    def heat(file, top):  # type: ignore[no-untyped-def]
        """Per-line change-frequency map of FILE: which lines churn the most."""
        conn = X.open_db()  # ClickException if there is no store at all
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            root_id = int(root["id"])
            rel = os.path.relpath(file.resolve(), root["path"]).replace(os.sep, "/")
            branch_id = X.active_branch_id(conn, root_id)

            versions = _collect_versions(conn, root_id, rel, branch_id)
            if not versions:
                raise X.click.ClickException(
                    f"no recorded version of {rel} — chronx has never captured its "
                    "content (created before tracking, ignored, or oversized)"
                )

            # The current content is the last recorded version; it must be
            # readable text for a per-line map to mean anything.
            target = versions[-1]
            target_lines, reason = _version_lines(store, target.digest)
            if reason == "missing":
                raise X.click.ClickException(
                    f"the recorded content of {rel} is missing from the object "
                    "store (run `chronx fsck`)"
                )
            if reason == "binary" or target_lines is None:
                X.click.echo(f"{rel}: binary file; cannot heat-map")
                return
            if not target_lines:
                X.click.secho(f"{rel}: empty file (no lines to heat-map)", dim=True)
                return

            heat_counts = _heatmap(store, versions, target, target_lines)

            _headline(rel, target_lines, heat_counts)
            X.click.echo()
            if top is not None:
                _render_top(target_lines, heat_counts, max(1, top))
            else:
                _render_full(target_lines, heat_counts)
        finally:
            conn.close()
