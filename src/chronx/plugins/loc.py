"""chronx loc — how big did my codebase get, over time?

A read-only report over the event log + object store. For the cwd's root and its
active timeline it reconstructs the *total lines of code* (summed line counts of
every tracked text file) at a series of points across the recorded session and
renders the growth curve:

  * a headline — starting LOC, current LOC, net change, and peak;
  * a unicode sparkline of total LOC over the sampled points, scaled between the
    min and max seen;
  * one row per sampled point — ``#<event> <time>  <LOC> lines  (<+/- delta>)``.

With ``--by-ext`` it instead breaks the *current* (latest) total down by file
extension — the "80% of my code is .py" view.

EFFICIENCY: a blob's content is immutable, so its line count is cached by digest
(``digest -> linecount``) and computed at most once via ``store.get`` + counting
``\\n``. The whole command is therefore O(distinct blobs), not O(events x files).
Undecodable / binary blobs count as 0 lines but still count as a present file.

Read-only: opens the db read-only and never mutates the store. Degrades
gracefully on an empty store, an untracked cwd, or a session with no events.
"""

from __future__ import annotations

import collections
import os

from chronx import pluginlib as X

# Sparkline ramp for the growth curve (low -> high).
_BLOCKS = "▁▂▃▄▅▆▇█"
# Width, in cells, of the --by-ext proportional bars.
_BAR_WIDTH = 24
# Upper bound on events fetched; ample for a single session's timeline.
_EVENT_LIMIT = 100_000

# A cache mapping a blob digest to its line count. Content-addressed blobs are
# immutable, so this is safe to share across every sampled state in one run.
LineCache = dict[str, int]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _line_count(store: "X.ObjectStore", cache: LineCache, digest: str) -> int:
    """Line count of the blob ``digest`` (number of ``\\n``), memoised.

    A missing blob or one whose bytes are not valid UTF-8 (i.e. binary) counts
    as 0 lines — it is still a real file, just not text we can measure.
    """
    cached = cache.get(digest)
    if cached is not None:
        return cached
    lines = 0
    try:
        data = store.get(digest)
        # Decode strictly: undecodable => treat as binary => 0 lines.
        lines = data.decode("utf-8").count("\n")
    except (KeyError, ValueError, UnicodeDecodeError, OSError):
        lines = 0
    cache[digest] = lines
    return lines


def _total_loc(
    state: dict[str, tuple[str | None, int | None]],
    store: "X.ObjectStore",
    cache: LineCache,
) -> int:
    """Sum of line counts over every present (hash-bearing) file in ``state``."""
    total = 0
    for digest, _mode in state.values():
        if digest is not None:  # None => file absent/deleted at this point
            total += _line_count(store, cache, digest)
    return total


def _sample_indices(n: int, steps: int) -> list[int]:
    """Up to ``steps`` evenly spaced indices into ``range(n)`` (incl. first/last).

    When there are no more events than sample points we keep them all; otherwise
    we spread the samples so the curve stays cheap to compute.
    """
    steps = max(1, steps)
    if n <= steps:
        return list(range(n))
    # Even spread anchored at both ends; dedup guards rounding collisions.
    return sorted({round(i * (n - 1) / (steps - 1)) for i in range(steps)})


def _sparkline(values: list[int]) -> str:
    """A unicode bar sparkline scaled between the min and max of ``values``."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    top = len(_BLOCKS) - 1
    return "".join(
        _BLOCKS[round((v - lo) / span * top) if span > 0 else 0] for v in values
    )


def _signed(n: int) -> str:
    """A `+N` / `-N` / `±0` delta label."""
    if n > 0:
        return f"+{n}"
    if n < 0:
        return str(n)
    return "±0"


def _bar(value: int, max_value: int, width: int) -> str:
    """A proportional block bar; a tiny nonzero value still shows one cell."""
    if value <= 0 or max_value <= 0:
        return ""
    cells = round(value / max_value * width)
    return "█" * max(1, cells)


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--steps", default=20, show_default=True,
        help="Max sample points along the timeline.",
    )
    @X.click.option(
        "--by-ext", is_flag=True,
        help="Break the current total down by file extension.",
    )
    def loc(steps: int, by_ext: bool) -> None:
        """Track total lines-of-code growth over the session."""
        style, echo, secho = X.click.style, X.click.echo, X.click.secho
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd untracked
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # Only content-changing events move the LOC curve.
            events = X.dbm.recent_events(
                conn, root_id=root_id, branch_id=branch_id,
                changes_only=True, limit=_EVENT_LIMIT,
            )

            # ---- header --------------------------------------------------
            bname = "-"
            if branch_id is not None:
                branch = X.dbm.get_branch(conn, branch_id)
                if branch is not None:
                    bname = branch["name"]
            secho("chronx loc", bold=True, nl=False)
            echo("  ·  " + style(root["path"], bold=True)
                 + style(f"  [{bname}]", fg="magenta"))

            if not events:
                echo("")
                echo("No recorded changes yet on this timeline.")
                echo(style("Run commands under `chronx exec` (or the shell "
                           "hook) first.", dim=True))
                return

            cache: LineCache = {}

            # ---- --by-ext: break the latest total down by extension ------
            if by_ext:
                latest = X.state_at(conn, root_id, float(events[-1]["started_at"]))
                # ext -> [lines, files]
                by: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
                for path, (digest, _mode) in latest.items():
                    if digest is None:
                        continue
                    ext = os.path.splitext(path)[1].lower() or "(no ext)"
                    rec = by[ext]
                    rec[0] += _line_count(store, cache, digest)
                    rec[1] += 1
                echo("")
                if not by:
                    echo("No tracked files present at the latest point.")
                    return
                total = sum(r[0] for r in by.values())
                rows = sorted(by.items(), key=lambda kv: (kv[1][0], kv[0]),
                              reverse=True)
                max_lines = max(r[0] for _e, r in rows) or 1
                secho(f"current total: {total} line(s) across "
                      f"{sum(r[1] for _e, r in rows)} file(s)", bold=True)
                for ext, (lines, files) in rows:
                    bar = style(_bar(lines, max_lines, _BAR_WIDTH), fg="cyan")
                    pct = f"{lines / total * 100:4.0f}%" if total else "   -"
                    echo(f"  {ext:<10} {lines:>7} lines  {files:>4} file(s)  "
                         f"{pct}  {bar}")
                return

            # ---- growth curve over the sampled points --------------------
            idxs = _sample_indices(len(events), steps)
            samples: list[tuple[X.sqlite3.Row, int]] = []
            for i in idxs:
                ev = events[i]
                state = X.state_at(conn, root_id, float(ev["started_at"]))
                samples.append((ev, _total_loc(state, store, cache)))

            locs = [loc_n for _ev, loc_n in samples]
            start, current, peak = locs[0], locs[-1], max(locs)
            net = current - start

            echo("")
            secho(
                f"start {start}  →  current {current}   "
                f"net {_signed(net)}   peak {peak}",
                bold=True,
            )
            echo("  " + style(_sparkline(locs), fg="green")
                 + style(f"   {len(samples)} sample(s) of {len(events)} "
                         "changing event(s)", dim=True))
            echo("")

            # One row per sampled point, with the delta from the previous sample.
            prev: int | None = None
            for ev, loc_n in samples:
                delta = "" if prev is None else f"  ({_signed(loc_n - prev)})"
                echo(f"  #{int(ev['id']):<5} {X.fmt_ts(float(ev['started_at']))}"
                     f"  {loc_n:>7} lines" + style(delta, dim=True))
                prev = loc_n
        finally:
            conn.close()
