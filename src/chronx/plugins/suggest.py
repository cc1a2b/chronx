"""chronx suggest — a personal shell coach over your recorded workflow.

Read-only analytics that turn your own recorded history into *actionable
advice*. Unlike ``hotspots``/``timings``/``failures`` (which report), ``suggest``
prescribes: for the cwd's tracked root and its active timeline (branch) it makes
a single ordered pass over the recorded ``events`` and derives concrete
suggestions to work faster and safer — each one a category, a finding backed by
real numbers, and a specific thing to do next:

  1. aliasable commands   — exact command lines you retype a lot (≥N times);
                            wrap them in a shell alias/function
  2. slow commands        — command lines whose *median* wall-clock duration is
                            high AND that you run often; cache / optimise them
  3. failing commands     — command lines that failed repeatedly (exit ≠ 0),
                            whether or not they ever went green; review them
  4. undo-prone commands  — command lines frequently followed by a chronx
                            undo/rollback/restore; test before running them
  5. long / uncheckpointed— very long one-liners (script them) and long streaks
                            of changes with no ``chronx mark`` (checkpoint them)
  6. likely typo fixes    — a command that is ~identical to the one just before
                            it (difflib ratio) run seconds apart; enable
                            completion so you stop re-typing near-duplicates

Everything is scoped to one root + one branch, derived from the raw ``events``
rows (chronx-internal bookkeeping and external command-less changes are handled
appropriately per category), never mutates the store, uses only stdlib +
``difflib`` (no external deps), and degrades gracefully: an untracked cwd or a
missing store yields a clean ClickException, and a too-thin history yields a
friendly "keep working and check back" message instead of a crash.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# tunables — deliberately conservative so suggestions only fire on real signal
# --------------------------------------------------------------------------- #
_MIN_REPEATS = 3          # #1 alias: how often an exact command must recur
_MIN_FAILS = 2            # #3 fail: how many times a command must fail to flag
_SLOW_MIN_RUNS = 3        # #2 slow: must also be run "often" to matter
_SLOW_MIN_MEDIAN = 1.0    # #2 slow: median duration (s) to count as "slow"
_LONG_CMD_LEN = 120       # #5: command-line length that reads as a script
_NOMARK_STREAK = 12       # #5: change streak with no `chronx mark` worth flagging
_TYPO_RATIO = 0.85        # #6: difflib similarity above which two cmds "match"
_TYPO_WINDOW = 90.0       # #6: max seconds between a command and its "fix"
_MAX_EVENTS = 5000        # cap how far back we scan (recent behaviour is enough)
_CMD_WIDTH = 64           # command strings are truncated to this for display
_DEFAULT_CAP = 5          # default examples shown per suggestion

# Chronx-internal reversals whose *preceding* real command is "undo-prone".
_REVERSAL_PREFIXES = ("chronx undo", "chronx rollback", "chronx restore")

# Stable tiebreak ordering when two suggestions have equal impact.
_RANK = {"alias": 0, "slow": 1, "fail": 2, "undo": 3, "long": 4, "typo": 5, "mark": 6}


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _is_internal(session: object, command: object) -> bool:
    """True for chronx's own bookkeeping events (undo/rollback/restore/merge or
    any ``chronx …`` line) — these are never user commands to coach on."""
    if session == "chronx":
        return True
    return isinstance(command, str) and command.startswith("chronx ")


def _is_reversal(command: object) -> bool:
    """True when the command is a chronx undo/rollback/restore event."""
    return isinstance(command, str) and command.startswith(_REVERSAL_PREFIXES)


def _median(values: list[float]) -> float:
    """Median of a non-empty list (0.0 for empty), no external deps."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _fmt_dur(seconds: float) -> str:
    """Humanize a wall-clock duration: 950ms / 2.4s / 1m 12s / 1h 3m."""
    s = max(0.0, float(seconds))
    if s < 1.0:
        return f"{s * 1000:.0f}ms"
    if s < 60.0:
        return f"{s:.1f}s"
    if s < 3600.0:
        m, sec = divmod(int(round(s)), 60)
        return f"{m}m {sec}s"
    h, rem = divmod(int(round(s)), 3600)
    return f"{h}h {rem // 60}m"


def _trunc(text: str, width: int = _CMD_WIDTH) -> str:
    """Single-line, length-capped rendering of a (possibly multi-line) command."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _delta_event_ids(
    conn: "X.sqlite3.Connection", root_id: int, branch_id: int | None
) -> set[int]:
    """Ids of in-scope events that recorded at least one file change. One cheap
    indexed query; used to measure change streaks between checkpoints."""
    if branch_id is None:
        where, params = "e.root_id = ?", [root_id]
    else:
        where, params = "e.root_id = ? AND e.branch_id = ?", [root_id, branch_id]
    return {
        int(r["id"])
        for r in conn.execute(
            "SELECT DISTINCT e.id AS id FROM events e "
            f"JOIN deltas d ON d.event_id = e.id WHERE {where}",
            params,
        )
    }


# --------------------------------------------------------------------------- #
# a single suggestion block (impact drives ordering; rank breaks ties)
# --------------------------------------------------------------------------- #
@dataclass
class _Suggestion:
    impact: float
    rank: int
    title: str
    action: str
    evidence: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--top", "-n", "cap", default=_DEFAULT_CAP, show_default=True,
        help="Max examples shown per suggestion.",
    )
    def suggest(cap: int) -> None:
        """Coach yourself: actionable suggestions from your recorded workflow."""
        cap = max(1, cap)
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            root_id = int(root["id"])
            branch = X.active_branch_id(conn, root_id)

            # header (root + active timeline, mirroring hotspots/failures/du)
            header = f"suggest  {root['path']}"
            if branch is not None:
                b = X.dbm.get_branch(conn, branch)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)

            # oldest-first timeline of recent events for this root + branch
            events = X.dbm.recent_events(
                conn, root_id=root_id, limit=_MAX_EVENTS, branch_id=branch
            )

            # ---- one ordered pass over the timeline ----------------------- #
            cmd_counts: "collections.Counter[str]" = collections.Counter()
            durations: "collections.defaultdict[str, list[float]]" = (
                collections.defaultdict(list)
            )
            fail_counts: "collections.Counter[str]" = collections.Counter()
            ok_cmds: set[str] = set()
            undo_prone: "collections.Counter[str]" = collections.Counter()
            typo_pairs: list[tuple[str, str, float, float]] = []

            last_real: str | None = None          # for undo attribution
            prev_real: tuple[str, float] | None = None  # for typo detection
            n_real = 0

            for e in events:
                cmd = e["command"]
                session = e["session"]

                if _is_internal(session, cmd):
                    # a chronx reversal blames the last real command before it.
                    if _is_reversal(cmd) and last_real is not None:
                        undo_prone[last_real] += 1
                    continue
                if cmd is None:
                    # external (command-less) change: not a command to coach on.
                    continue

                # a genuine user command
                n_real += 1
                cmd_counts[cmd] += 1

                code = e["exit_code"]
                if code is not None:
                    if int(code) == 0:
                        ok_cmds.add(cmd)
                    else:
                        fail_counts[cmd] += 1

                fin = e["finished_at"]
                if fin is not None:
                    dur = float(fin) - float(e["started_at"])
                    if dur >= 0:
                        durations[cmd].append(dur)

                # near-duplicate of the *immediately preceding* real command,
                # run within a short window -> almost certainly a fixed typo.
                ts = float(e["started_at"])
                if prev_real is not None:
                    pcmd, pts = prev_real
                    gap = ts - pts
                    if pcmd != cmd and 0.0 <= gap <= _TYPO_WINDOW:
                        ratio = SequenceMatcher(None, pcmd, cmd).ratio()
                        if ratio > _TYPO_RATIO:
                            typo_pairs.append((pcmd, cmd, ratio, gap))
                prev_real = (cmd, ts)
                last_real = cmd

            # ---- build suggestions from the aggregates -------------------- #
            suggestions: list[_Suggestion] = []

            # 1. aliasable — most-repeated exact command strings (≥ N times)
            aliasable = sorted(
                ((c, n) for c, n in cmd_counts.items() if n >= _MIN_REPEATS),
                key=lambda kv: (-kv[1], kv[0]),
            )
            if aliasable:
                s = _Suggestion(
                    impact=float(aliasable[0][1]),
                    rank=_RANK["alias"],
                    title="Turn repeated commands into aliases",
                    action=(
                        "define a shell alias/function for the top one so you "
                        "type a short name instead of the whole line"
                    ),
                )
                for c, n in aliasable[:cap]:
                    s.evidence.append(f"ran {n}×   $ {_trunc(c)}")
                suggestions.append(s)

            # 2. slow — high median duration AND run often
            slow: list[tuple[str, float, int, float]] = []
            for c, ds in durations.items():
                if len(ds) >= _SLOW_MIN_RUNS:
                    med = _median(ds)
                    if med >= _SLOW_MIN_MEDIAN:
                        slow.append((c, med, len(ds), med * len(ds)))
            slow.sort(key=lambda t: -t[3])
            if slow:
                s = _Suggestion(
                    impact=float(slow[0][3]),  # total wall time wasted (seconds)
                    rank=_RANK["slow"],
                    title="Speed up (or cache) slow, frequent commands",
                    action=(
                        "add caching / incremental flags / memoisation — this is "
                        "where your wall-clock time actually goes"
                    ),
                )
                for c, med, runs, tot in slow[:cap]:
                    s.evidence.append(
                        f"median {_fmt_dur(med)} × {runs} runs "
                        f"≈ {_fmt_dur(tot)} spent   $ {_trunc(c)}"
                    )
                suggestions.append(s)

            # 3. failing — same command failed (exit ≠ 0) repeatedly
            failing = sorted(
                (
                    (c, f, c in ok_cmds)
                    for c, f in fail_counts.items()
                    if f >= _MIN_FAILS
                ),
                key=lambda t: (-t[1], t[0]),
            )
            if failing:
                s = _Suggestion(
                    impact=float(failing[0][1]),
                    rank=_RANK["fail"],
                    title="Fix commands that keep failing",
                    action=(
                        "review these with `chronx failures` (exit codes + "
                        "time-to-green); fix the root cause before re-running"
                    ),
                )
                for c, f, recovered in failing[:cap]:
                    tag = "eventually recovered" if recovered else "never went green"
                    s.evidence.append(
                        f"failed {_plural(f, 'time')} ({tag})   $ {_trunc(c)}"
                    )
                suggestions.append(s)

            # 4. undo-prone — command frequently reverted right afterwards
            undo = undo_prone.most_common()
            if undo:
                s = _Suggestion(
                    impact=float(undo[0][1]) * 2.0,  # each undo is expensive
                    rank=_RANK["undo"],
                    title="Test before running undo-prone commands",
                    action=(
                        "dry-run or back the file up first — or drop a "
                        "`chronx mark` before them so undo is cheap and precise"
                    ),
                )
                for c, n in undo[:cap]:
                    s.evidence.append(
                        f"followed by an undo/rollback {n}×   $ {_trunc(c)}"
                    )
                suggestions.append(s)

            # 5a. long one-liners — candidates to turn into a script
            long_cmds = sorted(
                ((c, cmd_counts[c]) for c in cmd_counts if len(c) > _LONG_CMD_LEN),
                key=lambda kv: (-len(kv[0]), -kv[1]),
            )
            if long_cmds:
                s = _Suggestion(
                    impact=1.5,  # informational: below the count-based findings
                    rank=_RANK["long"],
                    title="Script very long one-liners",
                    action=(
                        "move them into a small, named script (e.g. `chronx "
                        "script`) so they are reusable, reviewable and testable"
                    ),
                )
                for c, n in long_cmds[:cap]:
                    s.evidence.append(f"{len(c)} chars, ran {n}×   $ {_trunc(c)}")
                suggestions.append(s)

            # 5b. long uncheckpointed change streak — suggest `chronx mark`
            change_ids = _delta_event_ids(conn, root_id, branch)
            mark_ts = sorted(
                float(m["ts"])
                for m in X.dbm.list_marks(conn)
                if m["root_id"] == root_id
            )
            streak = max_streak = 0
            mi = 0
            for e in events:
                ts = float(e["started_at"])
                # any mark at/-before this event checkpoints the streak so far
                while mi < len(mark_ts) and mark_ts[mi] <= ts:
                    streak = 0
                    mi += 1
                if _is_internal(e["session"], e["command"]):
                    continue
                if e["id"] in change_ids:  # a real change-producing event
                    streak += 1
                    max_streak = max(max_streak, streak)
            if max_streak >= _NOMARK_STREAK:
                suggestions.append(
                    _Suggestion(
                        impact=float(max_streak) * 0.4,
                        rank=_RANK["mark"],
                        title="Checkpoint long change streaks",
                        action=(
                            "run `chronx mark <name>` at milestones so you can "
                            "jump back to a known-good point precisely"
                        ),
                        evidence=[
                            f"{max_streak} changes recorded in a row with no "
                            "`chronx mark` in between"
                        ],
                    )
                )

            # 6. typo fixes — near-duplicate of the command just before it
            if typo_pairs:
                shown = sorted(typo_pairs, key=lambda t: -t[2])[:cap]
                s = _Suggestion(
                    # each near-duplicate is a cheap re-type, so weight modestly
                    impact=float(len(typo_pairs)) * 0.5,
                    rank=_RANK["typo"],
                    title="Likely typo fixes — enable shell completion",
                    action=(
                        "install completion (`chronx completion`) so you stop "
                        "re-typing (and mistyping) near-duplicate commands"
                    ),
                )
                for a, b, ratio, gap in shown:
                    s.evidence.append(
                        f"{ratio * 100:.0f}% similar, {gap:.0f}s apart:  "
                        f"{_trunc(a, 28)}  →  {_trunc(b, 28)}"
                    )
                suggestions.append(s)

            # ---- render (ordered by impact, then stable category rank) ---- #
            if not suggestions:
                X.click.echo("")
                X.click.secho(
                    "  not enough history yet for suggestions — keep working "
                    "and check back",
                    fg="green",
                )
                return

            X.click.echo("")
            X.click.echo(
                f"  {_plural(len(suggestions), 'suggestion')} from "
                f"{_plural(n_real, 'recorded command')}:"
            )
            for s in sorted(suggestions, key=lambda x: (-x.impact, x.rank)):
                X.click.echo("")
                X.click.secho(f"💡 {s.title}", fg="yellow", bold=True)
                for line in s.evidence:
                    X.click.echo(f"     {line}")
                X.click.secho(f"   → {s.action}", fg="green")
        finally:
            conn.close()
