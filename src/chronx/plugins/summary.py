"""chronx summary — a high-level digest of "what happened" over a time window.

Read-only over the store. Scopes to the tracked root containing the cwd and its
active timeline (branch), resolves a WINDOW into a [since, until] range, and
prints labeled sections: a headline count, change totals (+/- lines), the top
changed files, the busiest commands, and an activity sparkline.

Imports only ``chronx.pluginlib`` (as X) plus the stdlib, so it stays decoupled
from ``cli.py`` and is auto-discovered from the filesystem (no reinstall).
"""

from __future__ import annotations

import time
from pathlib import Path

from chronx import pluginlib as X

# Ramp used to draw the activity sparkline (low -> high).
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
# Buckets across the window for the sparkline.
_SPARK_SLOTS = 20
# Per-file diff cap when counting +/- lines (binary/oversize files self-skip).
_DIFF_CAP = 2000


def _resolve_window(conn: "X.sqlite3.Connection", window: str) -> tuple[float, float, str]:
    """Turn a WINDOW spec into ``(since_ts, until_ts, label)``.

    A ``A..B`` string is a range (each side resolved independently, then
    ordered); anything else is a single spec meaning "since then until now".
    Each side is resolved with ``X.moment_ts`` (mark name, event id, 'now', or
    a relative/absolute time spec).
    """
    now = time.time()
    left, sep, right = window.partition("..")
    if sep == "..":
        a = X.moment_ts(conn, left.strip() or "now")
        b = X.moment_ts(conn, right.strip() or "now")
        since, until = sorted((a, b))
    else:
        since = X.moment_ts(conn, window.strip() or "24h")
        until = now
    return since, until, window


def _count_diff(store: "X.ObjectStore", delta: "X.dbm.Delta") -> tuple[int, int]:
    """Return ``(insertions, deletions)`` for one delta via the diff renderer.

    Counts unified-diff body lines starting with '+'/'-', excluding the
    '+++'/'---' file headers. Binary / oversize / unavailable blobs render as
    ``@@ ... @@`` markers only, so they contribute nothing here.
    """
    ins = dels = 0
    for line in X.render_delta(store, delta, max_lines=_DIFF_CAP):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            ins += 1
        elif line.startswith("-"):
            dels += 1
    return ins, dels


def _sparkline(counts: list[int]) -> str:
    """A unicode bar sparkline for per-slot event counts (empty slots blank)."""
    if not counts:
        return ""
    peak = max(counts)
    if peak <= 0:
        return " " * len(counts)
    cells: list[str] = []
    for c in counts:
        if c <= 0:
            cells.append(" ")
        else:
            idx = round(c / peak * (len(_SPARK_BLOCKS) - 1))
            cells.append(_SPARK_BLOCKS[idx])
    return "".join(cells)


def _num(n: int, color: str) -> str:
    """A count, colored when non-zero and dimmed when it is exactly zero."""
    return X.click.style(str(n), fg=color) if n else X.click.style("0", dim=True)


def _pad_after(styled: str, raw: str, width: int) -> str:
    """Left-align a *styled* value to ``width`` columns using its raw length
    (so ANSI escapes never throw the alignment off)."""
    return styled + " " * max(1, width - len(raw))


def register(main: "X.click.Group") -> None:
    @main.command()
    @X.click.argument("window", required=False, default="24h")
    def summary(window: str) -> None:
        """Digest what happened over a time WINDOW (default 24h).

        WINDOW is either a single spec meaning "since then until now"
        (e.g. 24h, 2h, 2026-07-14, a mark name, an event id), or a range
        'A..B'. Scoped to the current directory's active timeline.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            # Scope to the tracked root for the cwd; stay friendly (no raise)
            # when the cwd isn't tracked or the store is otherwise empty.
            root = X.dbm.root_for_path(conn, Path.cwd())
            if root is None:
                X.click.secho(
                    f"chronx isn't tracking {Path.cwd()} yet "
                    "(run `chronx init` here, then record some commands).",
                    fg="yellow",
                )
                return
            root_id = int(root["id"])
            active = X.active_branch_id(conn, root_id)

            since, until, label = _resolve_window(conn, window)
            events = X.dbm.events_between(
                conn, since=since, until=until,
                root_id=root_id, branch_id=active,
            )

            span = f"{X.fmt_ts(since)}  ..  {X.fmt_ts(until)}"
            X.click.secho(f"chronx summary  {label}", bold=True)
            X.click.secho(f"  {span}", dim=True)

            if not events:
                X.click.secho(f"  no activity in {label}", fg="yellow")
                return

            # ---- single pass: headline, per-file, per-command, buckets ----
            n_total = len(events)
            n_changed = n_failed = n_external = 0
            total_ins = total_del = 0
            files: dict[str, list[int]] = {}   # path -> [changes, ins, del]
            cmds: dict[str, int] = {}          # command -> files changed
            span_s = until - since
            buckets = [0] * _SPARK_SLOTS

            for ev in events:
                if ev["command"] is None:
                    n_external += 1
                if ev["exit_code"] not in (None, 0):
                    n_failed += 1

                # Activity slot for this event.
                if span_s > 0:
                    slot = int((ev["started_at"] - since) / span_s * _SPARK_SLOTS)
                    buckets[min(_SPARK_SLOTS - 1, max(0, slot))] += 1
                else:
                    buckets[0] += 1

                deltas = X.dbm.deltas_for(conn, int(ev["id"]))
                if not deltas:
                    continue
                n_changed += 1
                cmd = X.describe_command(ev)
                cmds[cmd] = cmds.get(cmd, 0) + len(deltas)
                for d in deltas:
                    ins, dels = _count_diff(store, d)
                    total_ins += ins
                    total_del += dels
                    rec = files.setdefault(d.path, [0, 0, 0])
                    rec[0] += 1
                    rec[1] += ins
                    rec[2] += dels

            n_files = len(files)

            # ---- 1. headline ---------------------------------------------
            head = "  ·  ".join((
                X.click.style(str(n_total), bold=True) + " total",
                _num(n_changed, "magenta") + " changed files",
                _num(n_failed, "red") + " failed",
                _num(n_external, "cyan") + " external",
            ))
            X.click.echo()
            X.click.echo("  commands   " + head)

            # ---- 2. change totals ----------------------------------------
            if n_files:
                change_s = (
                    X.click.style(f"+{total_ins}", fg="green")
                    + " / "
                    + X.click.style(f"-{total_del}", fg="red")
                    + f" across {n_files} file(s)"
                )
            else:
                change_s = X.click.style("no file changes", dim=True)
            X.click.echo("  changes    " + change_s)

            # ---- 3. top changed files ------------------------------------
            if files:
                X.click.echo()
                X.click.secho("  top files", bold=True)
                ranked = sorted(
                    files.items(),
                    key=lambda kv: (-kv[1][0], -(kv[1][1] + kv[1][2]), kv[0]),
                )
                for path, (cnt, ins, dels) in ranked[:8]:
                    cnt_s = X.click.style(f"×{cnt}".rjust(4), fg="magenta", bold=True)
                    stat_raw = f"+{ins}/-{dels}"
                    stat_s = (
                        X.click.style(f"+{ins}", fg="green")
                        + "/"
                        + X.click.style(f"-{dels}", fg="red")
                    )
                    X.click.echo(f"    {cnt_s}  {_pad_after(stat_s, stat_raw, 12)}{path}")

            # ---- 4. busiest commands (by files changed) ------------------
            X.click.echo()
            X.click.secho("  busiest commands", bold=True)
            if cmds:
                for cmd, nf in sorted(cmds.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
                    nf_s = X.click.style(f"×{nf}".rjust(4), fg="magenta", bold=True)
                    shown = cmd if len(cmd) <= 80 else cmd[:77] + "..."
                    external = cmd == "(external change)"
                    cmd_s = X.click.style(shown, fg=None if external else "yellow",
                                          dim=external)
                    X.click.echo(f"    {nf_s}  {cmd_s}")
            else:
                X.click.secho("    (no file changes in this window)", dim=True)

            # ---- 5. activity sparkline -----------------------------------
            X.click.echo()
            X.click.secho("  activity", bold=True)
            X.click.echo("    " + X.click.style(_sparkline(buckets), fg="cyan"))
            X.click.secho(
                f"    oldest → newest · {n_total} event(s) over {_SPARK_SLOTS} slots",
                dim=True,
            )
        finally:
            conn.close()
