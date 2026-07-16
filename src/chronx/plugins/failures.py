"""chronx failures — failure & retry analytics over your recorded history.

Read-only forensics that answer three questions only chronx can, because it
records every command's *exit code* alongside the filesystem changes it caused:

  * what breaks   — the exit-code distribution (with the usual shell-exit
                    annotations: 127 "command not found", 126 "not executable",
                    2 "usage", 130 "SIGINT", …) and the command lines that fail
                    the most, with their success count and failure rate
  * how often     — a headline: total commands, failures, failure rate, distinct
                    failing command lines, and the total wall-time burned inside
                    failed runs
  * how long      — retry/recovery analytics: each failure "streak" (a command
                    that goes red and stays red until the *same* command line
                    finally succeeds) is paired with its fix, giving a
                    time-to-green per streak, plus the commands still failing

Everything is scoped to the cwd's tracked root and its active timeline (branch),
derived from raw parameterized SQL over ``events`` (Row factory), and never
mutates the store. chronx-internal bookkeeping and external (command-less)
changes are excluded, and it degrades gracefully on an empty / all-green store.
"""

from __future__ import annotations

import collections

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# tunables + a smooth unicode bar (eighth-block resolution), as in `hotspots`
# --------------------------------------------------------------------------- #
_BAR_EIGHTHS = " ▏▎▍▌▋▊▉█"
_BAR_WIDTH = 18

# Well-known shell exit codes → a short human hint. 128+N is a fatal signal N;
# handled separately so every signal annotates even without a table entry.
_EXIT_HINTS: dict[int, str] = {
    1: "general error",
    2: "usage / syntax error",
    124: "timed out",
    125: "command itself failed to run",
    126: "not executable / permission denied",
    127: "command not found",
    128: "invalid exit argument",
}
_SIGNALS: dict[int, str] = {
    2: "SIGINT (Ctrl-C)", 3: "SIGQUIT", 6: "SIGABRT", 9: "SIGKILL (OOM?)",
    11: "SIGSEGV (segfault)", 13: "SIGPIPE", 15: "SIGTERM",
}


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _scope(root_id: int, branch_id: int | None) -> tuple[str, list[object]]:
    """WHERE fragment + params scoping ``events e`` to one root and — when the
    active branch is known — its active timeline. Also drops external
    (command-less) events and chronx's own bookkeeping. Falls back to root-only
    on a pre-branch store so the report still works."""
    where = ["e.root_id = ?"]
    params: list[object] = [root_id]
    if branch_id is not None:
        where.append("e.branch_id = ?")
        params.append(branch_id)
    # exclude external changes (NULL command) and chronx-internal events;
    # COALESCE guards a NULL session so the row is kept, not dropped.
    where.append("e.command IS NOT NULL")
    where.append("COALESCE(e.session, '') != 'chronx'")
    where.append("e.command NOT LIKE 'chronx %'")
    return " AND ".join(where), params


def _exit_hint(code: int) -> str:
    """A short human annotation for an exit code (empty when unremarkable)."""
    if code in _EXIT_HINTS:
        return _EXIT_HINTS[code]
    if 129 <= code <= 192:  # 128 + fatal signal number
        sig = code - 128
        name = _SIGNALS.get(sig)
        return f"killed by signal {sig}" + (f" · {name}" if name else "")
    return ""


def _human_dur(seconds: float) -> str:
    """Humanize a duration in seconds (companion to X.human_bytes)."""
    s = float(seconds)
    if s < 0:
        s = 0.0
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        m, sec = divmod(int(round(s)), 60)
        return f"{m}m {sec}s"
    if s < 86400:
        h, rem = divmod(int(round(s)), 3600)
        return f"{h}h {rem // 60}m"
    d, rem = divmod(int(round(s)), 86400)
    return f"{d}d {rem // 3600}h"


def _hbar(value: float, maximum: float, width: int = _BAR_WIDTH) -> str:
    """A smooth horizontal bar (eighth-block resolution) for ``value/maximum``."""
    if maximum <= 0 or value <= 0:
        return ""
    frac = min(1.0, value / maximum) * width
    full = int(frac)
    bar = "█" * full
    if full < width:
        eighths = int(round((frac - full) * 8))
        if eighths:
            bar += _BAR_EIGHTHS[eighths]
    return bar


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--top", "-n", default=10, show_default=True,
        help="How many rows to show in the failing-command / still-failing lists.",
    )
    def failures(top: int) -> None:
        """Failure & retry analytics: what breaks, how often, time-to-fix."""
        top = max(1, top)
        conn = X.open_db()
        try:
            root = X.root_for_cwd(conn)  # ClickException if cwd is untracked
            root_id = int(root["id"])
            branch = X.active_branch_id(conn, root_id)
            where, params = _scope(root_id, branch)

            # header (root + active timeline, mirroring `hotspots` / `du`)
            header = f"failures  {root['path']}"
            if branch is not None:
                b = X.dbm.get_branch(conn, branch)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)

            # one ordered pass over every in-scope command event
            rows = list(
                conn.execute(
                    "SELECT e.id AS id, e.command AS command, "
                    "       e.started_at AS started_at, "
                    "       e.finished_at AS finished_at, e.exit_code AS exit_code "
                    "FROM events e "
                    f"WHERE {where} "
                    "ORDER BY e.id ASC",
                    params,
                )
            )
            if not rows:
                X.click.echo("")
                X.click.echo("  (no commands recorded on this timeline yet)")
                return

            total = len(rows)
            # A failure is a KNOWN non-zero exit; NULL exit == outcome unknown.
            failed = [r for r in rows if r["exit_code"] not in (None, 0)]
            n_failed = len(failed)
            burned = sum(
                float(r["finished_at"]) - float(r["started_at"])
                for r in failed
                if r["finished_at"] is not None
            )
            distinct_fail_cmds = {r["command"] for r in failed}

            # ---- §1 headline --------------------------------------------- #
            rate = (n_failed / total * 100.0) if total else 0.0
            X.click.echo("")
            X.click.echo(
                "  "
                + X.click.style(str(total), bold=True) + " commands"
                + "  ·  "
                + X.click.style(f"{n_failed} failed", fg="red" if n_failed else None,
                                bold=bool(n_failed))
                + X.click.style(f" ({rate:.1f}%)", fg="red" if n_failed else None)
                + "  ·  "
                + X.click.style(str(len(distinct_fail_cmds)), bold=True)
                + " distinct failing"
                + "  ·  "
                + X.click.style(_human_dur(burned), fg="yellow")
                + " burned in failed runs"
            )

            if n_failed == 0:
                X.click.echo("")
                X.click.secho("  ✓ no failed commands — everything green", fg="green")
                return

            # ---- §2 exit-code distribution ------------------------------- #
            by_code: "collections.Counter[int]" = collections.Counter(
                int(r["exit_code"]) for r in failed
            )
            X.click.echo("")
            X.click.secho("exit-code distribution", bold=True)
            max_c = max(by_code.values())
            code_w = max(len(str(c)) for c in by_code)
            cnt_w = max(len(str(n)) for n in by_code.values())
            for code, cnt in sorted(by_code.items(), key=lambda kv: (-kv[1], kv[0])):
                hint = _exit_hint(code)
                bar = _hbar(cnt, max_c)
                line = (
                    f"  {code:>{code_w}}  "
                    + X.click.style(f"×{cnt:<{cnt_w}}", fg="red", bold=True)
                    + f"  {bar:<{_BAR_WIDTH}}"
                )
                if hint:
                    line += "  " + X.click.style(hint, dim=True)
                X.click.echo(line)

            # ---- §3 most-failing commands -------------------------------- #
            # group by exact command string: fails, oks (exit 0), rate.
            fails_by: "collections.Counter[str]" = collections.Counter()
            oks_by: "collections.Counter[str]" = collections.Counter()
            for r in rows:
                code = r["exit_code"]
                if code is None:
                    continue
                if int(code) == 0:
                    oks_by[r["command"]] += 1
                else:
                    fails_by[r["command"]] += 1

            X.click.echo("")
            X.click.secho(
                f"most-failing commands (top {min(top, len(fails_by))} "
                f"of {len(fails_by)})",
                bold=True,
            )
            X.click.secho("  fails   ok    rate   command", dim=True)
            ranked = sorted(
                fails_by.items(),
                key=lambda kv: (
                    -kv[1],
                    -(kv[1] / (kv[1] + oks_by[kv[0]])),  # tie-break: worse rate
                    kv[0],
                ),
            )[:top]
            for cmd, f in ranked:
                ok = oks_by[cmd]
                denom = f + ok
                r_pct = (f / denom * 100.0) if denom else 100.0
                shown = cmd if len(cmd) <= 60 else cmd[:57] + "..."
                X.click.echo(
                    "  "
                    + X.click.style(f"{f:>5}", fg="red", bold=True)
                    + X.click.style(f"  {ok:>3}", fg="green" if ok else None,
                                    dim=not ok)
                    + f"  {r_pct:5.1f}%  "
                    + X.click.style(shown, fg="yellow")
                )

            # ---- §4 recovery / time-to-fix ------------------------------- #
            # Walk events in id order. A command that fails opens a "streak"
            # (first-fail time + attempt count); the next SUCCESS of that same
            # command closes it, yielding a time-to-green. Streaks left open at
            # the end are commands that are still red.
            pending: dict[str, list[float]] = {}  # cmd -> [first_ts, attempts]
            recovered: list[tuple[str, float, int]] = []  # (cmd, secs, attempts)
            for r in rows:
                code = r["exit_code"]
                if code is None:
                    continue
                cmd = r["command"]
                ts = float(r["started_at"])
                if int(code) != 0:
                    st = pending.get(cmd)
                    if st is None:
                        pending[cmd] = [ts, 1]
                    else:
                        st[1] += 1
                elif cmd in pending:
                    first_ts, attempts = pending.pop(cmd)
                    recovered.append((cmd, ts - first_ts, attempts))

            X.click.echo("")
            X.click.secho("recovery — time until green again", bold=True)
            n_streaks = len(recovered) + len(pending)
            if recovered:
                secs = sorted(d for _c, d, _a in recovered)
                median = secs[len(secs) // 2]
                worst = secs[-1]
                X.click.echo(
                    f"  {_plural(n_streaks, 'failure streak')} · "
                    + X.click.style(f"{len(recovered)} recovered", fg="green")
                    + f" (median {_human_dur(median)}, worst {_human_dur(worst)}) · "
                    + X.click.style(f"{len(pending)} still failing",
                                    fg="red" if pending else None)
                )
            else:
                X.click.echo(
                    f"  {_plural(n_streaks, 'failure streak')} · "
                    + X.click.style("0 recovered", dim=True)
                    + " · "
                    + X.click.style(f"{len(pending)} still failing",
                                    fg="red" if pending else None)
                )
            if pending:
                X.click.secho("  still failing (never went green):", dim=True)
                for cmd, (first_ts, attempts) in sorted(
                    pending.items(), key=lambda kv: (-kv[1][1], kv[0])
                )[:top]:
                    shown = cmd if len(cmd) <= 52 else cmd[:49] + "..."
                    X.click.echo(
                        "    "
                        + X.click.style("✗ ", fg="red")
                        + X.click.style(shown, fg="yellow")
                        + X.click.style(
                            f"  ({_plural(attempts, 'attempt')}, since "
                            f"{X.fmt_ts(first_ts)})",
                            dim=True,
                        )
                    )
        finally:
            conn.close()
