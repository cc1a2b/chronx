"""chronx risky — a safety audit of the *dangerous commands* in your history.

Where ``audit`` hunts leaked secrets in recorded file *content* and ``failures``
studies *exit codes*, ``risky`` judges the *command strings themselves*: it
replays your recorded command history and flags the destructive / irreversible
things you ran — ``rm -rf``, ``git reset --hard``, ``git push --force``, pipe-to-
shell installers, ``dd``/``mkfs``, fork bombs, ``DROP TABLE`` — so you can review
what nearly (or actually) went wrong on this timeline.

Each risk pattern carries a human name and a severity (HIGH / MEDIUM / LOW). A
command that trips several patterns is reported at its *highest* severity. Every
flagged command is then correlated with what it actually did, using the recorded
deltas: if it deleted files we say so (and float it to the top), and if it exited
non-zero we note that too.

Strictly read-only: the store is opened read-only and never mutated. Scans the
cwd's tracked root on its active timeline (branch), skipping external
(command-less) changes and chronx's own bookkeeping. Exits non-zero only when at
least one HIGH-severity command is found, so it is usable as a CI gate.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import NamedTuple

from chronx import pluginlib as X

# --------------------------------------------------------------------------- #
# severity model
# --------------------------------------------------------------------------- #
_HIGH, _MEDIUM, _LOW = 3, 2, 1
_SEV_NAME: dict[int, str] = {_HIGH: "HIGH", _MEDIUM: "MEDIUM", _LOW: "LOW"}
# rendering per the spec: HIGH red, MEDIUM yellow, LOW dim.
_SEV_FG: dict[int, str | None] = {_HIGH: "red", _MEDIUM: "yellow", _LOW: None}
_SEV_DIM: dict[int, bool] = {_HIGH: False, _MEDIUM: False, _LOW: True}
_MIN_SEVERITY: dict[str, int] = {"low": _LOW, "medium": _MEDIUM, "high": _HIGH}


class _Risk(NamedTuple):
    """A single dangerous-command signature."""

    name: str
    severity: int
    regex: "re.Pattern[str]"
    # When True the match only counts if the event actually *modified* an
    # existing tracked file (delta M > 0). This distinguishes a truncating
    # redirect that clobbered real content from one that merely created a new
    # file (``echo x > new.txt``), which is not dangerous.
    needs_modify: bool = False


# Ordered most-representative first *within* each severity: when a command trips
# several patterns at its top severity, the first listed supplies the name.
_RISKS: tuple[_Risk, ...] = (
    # ---- HIGH: destructive / irreversible -------------------------------- #
    _Risk(  # rm carrying BOTH a recursive and a force flag (-rf / -fr / -r -f)
        "recursive force delete (rm -rf)", _HIGH,
        re.compile(r"\brm\b(?=.*\s-[a-z]*r)(?=.*\s-[a-z]*f)"),
    ),
    _Risk(  # a forceful rm aimed at /, ~, or a glob — the classic footgun
        "force delete of / ~ or glob", _HIGH,
        re.compile(r"\brm\b(?=.*\s-[a-z]*f).*\s(?:/(?:\s|$)|~|\S*\*)"),
    ),
    _Risk(
        "sudo rm (privileged delete)", _HIGH,
        re.compile(r"\bsudo\s+(?:-\S+\s+)*rm\b"),
    ),
    _Risk(
        "git push --force", _HIGH,
        re.compile(r"\bgit\b.*\bpush\b.*(?:--force(?:-with-lease)?|(?<!\w)-f\b)"),
    ),
    _Risk(
        "git reset --hard", _HIGH,
        re.compile(r"\bgit\b.*\breset\b.*--hard\b"),
    ),
    _Risk(
        "git clean -f (delete untracked)", _HIGH,
        re.compile(r"\bgit\b.*\bclean\b.*\s-[a-z]*f"),
    ),
    _Risk(
        "find -delete", _HIGH,
        re.compile(r"\bfind\b.*\s-delete\b"),
    ),
    _Risk(
        "dd (raw disk write)", _HIGH,
        re.compile(r"\bdd\s+[a-z]*="),
    ),
    _Risk(
        "mkfs (format filesystem)", _HIGH,
        re.compile(r"\bmkfs"),
    ),
    _Risk(
        "shred (secure erase)", _HIGH,
        re.compile(r"\bshred\b"),
    ),
    _Risk(
        "truncate", _HIGH,
        re.compile(r"\btruncate\b"),
    ),
    _Risk(
        "write to /dev/ device", _HIGH,
        re.compile(r">\s*/dev/"),
    ),
    _Risk(
        "chmod -R 777 (world-writable)", _HIGH,
        re.compile(r"\bchmod\b(?=.*(?:\s-[a-z]*R|--recursive))(?=.*777)"),
    ),
    _Risk(
        "chown -R (recursive ownership)", _HIGH,
        re.compile(r"\bchown\b.*(?:\s-[a-z]*R|--recursive)"),
    ),
    _Risk(
        "pipe-to-shell (curl | sh)", _HIGH,
        re.compile(r"\b(?:curl|wget)\b.*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"),
    ),
    _Risk(
        "fork bomb", _HIGH,
        re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"),
    ),
    _Risk(
        "SQL DROP TABLE/DATABASE", _HIGH,
        re.compile(r"(?i)\bdrop\s+(?:table|database)\b"),
    ),
    # ---- MEDIUM: reversible-ish but data-losing -------------------------- #
    _Risk(  # a bare recursive OR force rm, not caught above (no force-on-root)
        "delete (rm -r / rm -f)", _MEDIUM,
        re.compile(r"\brm\b(?=.*\s-[a-z]*[rf])"),
    ),
    _Risk(
        "git checkout/restore (discard changes)", _MEDIUM,
        re.compile(r"\bgit\b.*\bcheckout\b.*\s--(?:\s|$)|\bgit\b.*\brestore\b"),
    ),
    _Risk(
        "git stash drop/clear", _MEDIUM,
        re.compile(r"\bgit\b.*\bstash\b.*\b(?:drop|clear)\b"),
    ),
    _Risk(
        "kill -9 (SIGKILL)", _MEDIUM,
        re.compile(r"\bkill\b.*(?:-9\b|-(?:s\s*)?(?:SIG)?KILL\b)"),
    ),
    _Risk(
        "pkill / killall", _MEDIUM,
        re.compile(r"\b(?:pkill|killall)\b"),
    ),
    _Risk(
        "mv to /dev/null", _MEDIUM,
        re.compile(r"\bmv\b.*\s/dev/null\b"),
    ),
    _Risk(
        "docker prune", _MEDIUM,
        re.compile(r"\bdocker\b.*\b(?:system|image|volume|container)?\s*prune\b"),
    ),
    _Risk(
        "wipe node_modules / npm ci", _MEDIUM,
        re.compile(r"\bnpm\s+ci\b|\brm\b.*node_modules"),
    ),
    _Risk(  # a single truncating redirect that clobbered existing content
        "truncating redirect (> file)", _MEDIUM,
        re.compile(r"(?<![>&\d=<])>(?![>&=])\s*(?!/dev/)[~.]?[\w./~-]+"),
        needs_modify=True,
    ),
    _Risk(
        "chmod (permission change)", _MEDIUM,
        re.compile(r"\bchmod\b"),
    ),
    _Risk(
        "force / assume-yes flag", _MEDIUM,
        re.compile(r"(?<!\w)-y\b|--force\b|--assume-yes\b|--yes\b"),
    ),
    # ---- LOW: worth a glance --------------------------------------------- #
    _Risk(
        "sudo (privileged)", _LOW,
        re.compile(r"\bsudo\b"),
    ),
    _Risk(
        "eval", _LOW,
        re.compile(r"\beval\b"),
    ),
    _Risk(
        "export assignment", _LOW,
        re.compile(r"\bexport\s+\w+="),
    ),
    _Risk(
        "background job (&)", _LOW,
        re.compile(r"(?<!&)&(?!&)\s*(?:;|$)"),
    ),
    _Risk(
        "history -c (clear shell history)", _LOW,
        re.compile(r"\bhistory\s+-c\b"),
    ),
)


class _Finding(NamedTuple):
    event_id: int
    ts: float
    severity: int
    risk: str
    command: str
    deleted: int          # files this command deleted (delta D count)
    exit_code: int | None  # non-zero exit, else None


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _short(cmd: str, limit: int = 72) -> str:
    """Collapse whitespace and clip a command for one-line display."""
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= limit else cmd[: limit - 1] + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _classify(command: str, modified: int) -> tuple[int, str] | None:
    """Return (severity, risk-name) for a command, or None if it is benign.

    All patterns are tried; conditional ``needs_modify`` patterns are dropped
    unless the event modified an existing tracked file. The reported severity is
    the highest matched, and the name is the first pattern (in table order) at
    that severity.
    """
    matched = [
        r
        for r in _RISKS
        if r.regex.search(command) and not (r.needs_modify and modified == 0)
    ]
    if not matched:
        return None
    severity = max(r.severity for r in matched)
    name = next(r.name for r in matched if r.severity == severity)
    return severity, name


# --------------------------------------------------------------------------- #
# command registration
# --------------------------------------------------------------------------- #
def register(main) -> None:
    @main.command()
    @X.click.option(
        "--top", "-n", default=20, show_default=True,
        help="Show at most this many findings (most severe first).",
    )
    @X.click.option(
        "--min-severity", "min_severity",
        type=X.click.Choice(["low", "medium", "high"]), default="low",
        show_default=True, help="Only report findings at or above this severity.",
    )
    def risky(top: int, min_severity: str) -> None:
        """Safety audit: flag the dangerous / destructive commands you ran.

        Scans the recorded command history of the cwd's tracked root (active
        timeline) against a set of risk patterns and reports each dangerous
        command with what it actually did. Exits non-zero if any HIGH-severity
        command is found, so it can gate CI.
        """
        top = max(1, top)
        threshold = _MIN_SEVERITY[min_severity]
        conn = X.open_db()  # read-only; ClickException if there is no store
        try:
            root = X.root_for_cwd(conn)  # ClickException if the cwd is untracked
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            # header — mirror `failures` / `hotspots`: root + active timeline
            header = f"risky  {root['path']}"
            if branch_id is not None:
                b = X.dbm.get_branch(conn, branch_id)
                if b is not None:
                    header += f"   [timeline: {b['name']}]"
            X.click.secho(header, fg="cyan", bold=True)

            # one ordered pass over every in-scope command event
            events = X.dbm.recent_events(
                conn, root_id=root_id, limit=10_000_000, branch_id=branch_id
            )
            findings: list[_Finding] = []
            for ev in events:
                command = ev["command"]
                # external (command-less) changes have nothing to judge; the
                # user running chronx itself is not what this audit is about.
                if command is None or command.startswith("chronx "):
                    continue
                counts = X.dbm.delta_counts(conn, int(ev["id"]))
                verdict = _classify(command, counts.get("M", 0))
                if verdict is None:
                    continue
                severity, risk = verdict
                if severity < threshold:
                    continue
                exit_code = ev["exit_code"]
                failed = exit_code is not None and int(exit_code) != 0
                findings.append(
                    _Finding(
                        event_id=int(ev["id"]),
                        ts=float(ev["started_at"]),
                        severity=severity,
                        risk=risk,
                        command=command,
                        deleted=counts.get("D", 0),
                        exit_code=int(exit_code) if failed else None,
                    )
                )

            if not findings:
                X.click.echo("")
                X.click.secho(
                    "no risky commands found in recorded history", fg="green"
                )
                return

            # ---- headline ------------------------------------------------ #
            by_sev: "Counter[int]" = Counter(f.severity for f in findings)
            total_deletions = sum(f.deleted for f in findings)
            X.click.echo("")
            X.click.echo(
                "  "
                + X.click.style(_plural(len(findings), "risky command"), bold=True)
                + ": "
                + X.click.style(f"{by_sev[_HIGH]} high", fg="red",
                                bold=bool(by_sev[_HIGH]))
                + ", "
                + X.click.style(f"{by_sev[_MEDIUM]} medium", fg="yellow",
                                bold=bool(by_sev[_MEDIUM]))
                + ", "
                + X.click.style(f"{by_sev[_LOW]} low", dim=True)
                + X.click.style(
                    f"   ({_plural(total_deletions, 'deletion')} across them)",
                    dim=True,
                )
            )

            # ---- findings, most-severe first (deletions/failures float up) --
            ordered = sorted(
                findings,
                key=lambda f: (
                    -f.severity,
                    -(1 if (f.deleted or f.exit_code is not None) else 0),
                    -f.event_id,
                ),
            )
            shown = ordered[:top]
            if len(shown) < len(findings):
                X.click.secho(
                    f"  (showing top {len(shown)} of {len(findings)})", dim=True
                )

            last_sev: int | None = None
            for f in shown:
                if f.severity != last_sev:
                    X.click.echo("")
                    X.click.secho(
                        _SEV_NAME[f.severity],
                        fg=_SEV_FG[f.severity],
                        dim=_SEV_DIM[f.severity],
                        bold=f.severity != _LOW,
                    )
                    last_sev = f.severity
                # annotate with what the command actually did
                notes = ""
                if f.deleted:
                    notes += X.click.style(
                        f"  (deleted {_plural(f.deleted, 'file')})",
                        fg="red", bold=True,
                    )
                if f.exit_code is not None:
                    notes += X.click.style(f"  (exited {f.exit_code})", fg="red")
                X.click.echo(
                    "  "
                    + X.click.style(f"#{f.event_id}", fg="cyan")
                    + " "
                    + X.click.style(X.fmt_ts(f.ts), dim=True)
                    + "  "
                    + X.click.style(f"[{f.risk}]",
                                    fg=_SEV_FG[f.severity],
                                    dim=_SEV_DIM[f.severity], bold=f.severity == _HIGH)
                    + "  "
                    + X.click.style(f"$ {_short(f.command)}",
                                    fg="yellow", dim=f.severity == _LOW)
                    + notes
                )

            # CI gate: fail only when something HIGH-severity was recorded.
            if by_sev[_HIGH]:
                raise X.click.exceptions.Exit(1)
        finally:
            conn.close()
