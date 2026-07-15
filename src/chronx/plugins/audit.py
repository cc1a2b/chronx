"""chronx audit — scan every recorded file version for leaked secrets.

chronx keeps the *content* of files across time, so a secret that was written
into a file and later deleted still lives in the content-addressed object
store even though it is gone from the working tree. An ordinary secret scanner
only inspects the files that exist right now and misses those entirely.

``chronx audit`` is the opposite: it scans *every distinct blob* that ever
represented file content on the current root's active timeline (all delta
after-hashes plus the tracking baseline), decodes the text ones, and matches
them against a set of well-known secret patterns. Each hit is mapped back to
where and when it appeared, redacted, and — most usefully — flagged when the
secret is no longer in the working tree but is still recoverable from history.

Strictly read-only: the store is opened read-only and nothing is ever written
or mutated. Exits non-zero when any secret is found.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from chronx import pluginlib as X


# --- secret patterns --------------------------------------------------------


class _Pattern(NamedTuple):
    name: str
    regex: "re.Pattern[str]"
    group: int  # capture group holding the secret value (0 == whole match)


# Ordered most-specific first: a given literal is attributed to its narrowest
# rule (see the cross-pattern value de-dup in ``_scan_text``).
_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}"), 0),
    _Pattern(
        "Private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
        0,
    ),
    _Pattern("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), 0),
    _Pattern("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    _Pattern("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), 0),
    _Pattern(
        "Hard-coded secret assignment",
        re.compile(
            r"(?i)(?:password|passwd|secret|api[_-]?key|token|access[_-]?key)"
            r"\s*[:=]\s*['\"]?([A-Za-z0-9/+_\-]{12,})"
        ),
        1,
    ),
    _Pattern("High-entropy hex string", re.compile(r"\b[A-Fa-f0-9]{32,}\b"), 0),
)

# Blobs larger than this are almost never hand-authored secrets and scanning
# them wastes time; skip (they are simply not counted as scanned).
_MAX_SCAN_BYTES = 8 * 1024 * 1024


class _Finding(NamedTuple):
    path: str
    pattern: str
    redacted: str
    event_id: int | None  # None == the tracking baseline
    ts: float | None
    command: str
    status: str  # human note about whether it is still live
    gone: bool  # True when no longer the current content of the tree


# --- helpers ----------------------------------------------------------------


def _redact(secret: str) -> str:
    """First 4 + last 4 chars kept, the middle masked with bullets.

    The result is recognizable but unusable, and never the full secret.
    """
    s = secret.strip()
    if len(s) <= 2:
        return "•" * len(s)
    if len(s) <= 8:  # too short for 4+4 without overlap
        return s[0] + "•" * (len(s) - 2) + s[-1]
    middle = len(s) - 8
    return s[:4] + "•" * min(middle, 12) + s[-4:]


def _load_blob(store: "X.ObjectStore", digest: str) -> bytes | None:
    """Fetch a blob's raw bytes, tolerating a missing or corrupt object."""
    try:
        return store.get(digest)
    except (KeyError, ValueError, OSError):
        return None


def _scan_text(text: str, store: "X.ObjectStore") -> list[tuple[str, str]]:
    """Return de-duplicated (pattern name, redacted secret) hits for one blob.

    A literal is reported once, under the most specific pattern that matches
    it (patterns are tried specific-first), so an AWS key is never also listed
    as a generic assignment.
    """
    hits: list[tuple[str, str]] = []
    seen_values: set[str] = set()
    for pat in _PATTERNS:
        for m in pat.regex.finditer(text):
            value = m.group(pat.group) or ""
            if not value or value in seen_values:
                continue
            if pat.name.startswith("High-entropy"):
                # A bare hex token that is literally one of our own blob
                # digests is a chronx artifact, not a user secret.
                try:
                    if store.has(value.lower()):
                        continue
                except Exception:
                    pass
            seen_values.add(value)
            hits.append((pat.name, _redact(value)))
    return hits


def _collect_digests(
    conn: "X.sqlite3.Connection", root_id: int, branch_id: int | None
) -> set[str]:
    """Every distinct blob digest that ever held file content on this timeline.

    All delta ``after_hash`` values for the root's active branch, plus the
    immutable tracking baseline (shared across branches).
    """
    digests: set[str] = set()
    if branch_id is not None:
        rows = conn.execute(
            "SELECT DISTINCT d.after_hash AS h"
            " FROM deltas d JOIN events e ON e.id = d.event_id"
            " WHERE e.root_id = ? AND e.branch_id = ? AND d.after_hash IS NOT NULL",
            (root_id, branch_id),
        )
    else:  # pre-branch store: fall back to root scope
        rows = conn.execute(
            "SELECT DISTINCT d.after_hash AS h"
            " FROM deltas d JOIN events e ON e.id = d.event_id"
            " WHERE e.root_id = ? AND d.after_hash IS NOT NULL",
            (root_id,),
        )
    digests.update(r["h"] for r in rows if r["h"])
    try:
        for r in conn.execute(
            "SELECT DISTINCT hash AS h FROM root_baseline WHERE root_id = ?",
            (root_id,),
        ):
            if r["h"]:
                digests.add(r["h"])
    except X.sqlite3.Error:
        pass  # baseline table absent on a very old store
    return digests


def _current_state(conn: "X.sqlite3.Connection", root_id: int) -> dict[str, str]:
    """Path -> current blob hash for the live working tree (the manifest)."""
    current: dict[str, str] = {}
    try:
        for r in conn.execute(
            "SELECT path, hash FROM manifest WHERE root_id = ?", (root_id,)
        ):
            current[r["path"]] = r["hash"]
    except X.sqlite3.Error:
        pass
    return current


def _locations_for(
    conn: "X.sqlite3.Connection",
    root_id: int,
    branch_id: int | None,
    root: "X.sqlite3.Row",
    digest: str,
) -> list[tuple[str, int | None, float | None, str]]:
    """Where a blob appeared: (path, event id, started_at, command).

    Event occurrences come from ``deltas.after_hash``; baseline occurrences are
    labelled with a synthetic (None event, tracking-start time) location.
    """
    locs: list[tuple[str, int | None, float | None, str]] = []
    if branch_id is not None:
        rows = conn.execute(
            "SELECT e.id AS eid, e.started_at AS ts, e.command AS cmd, d.path AS path"
            " FROM deltas d JOIN events e ON e.id = d.event_id"
            " WHERE d.after_hash = ? AND e.root_id = ? AND e.branch_id = ?"
            " ORDER BY e.id",
            (digest, root_id, branch_id),
        )
    else:
        rows = conn.execute(
            "SELECT e.id AS eid, e.started_at AS ts, e.command AS cmd, d.path AS path"
            " FROM deltas d JOIN events e ON e.id = d.event_id"
            " WHERE d.after_hash = ? AND e.root_id = ?"
            " ORDER BY e.id",
            (digest, root_id),
        )
    for r in rows:
        cmd = r["cmd"] if r["cmd"] is not None else "(external change)"
        locs.append((r["path"], int(r["eid"]), float(r["ts"]), cmd))

    try:
        base_ts: float | None = float(root["added_at"])
    except (KeyError, IndexError, TypeError, ValueError):
        base_ts = None
    try:
        for r in conn.execute(
            "SELECT path FROM root_baseline WHERE root_id = ? AND hash = ?",
            (root_id, digest),
        ):
            locs.append((r["path"], None, base_ts, "(tracking baseline)"))
    except X.sqlite3.Error:
        pass
    return locs


def _locate(
    conn: "X.sqlite3.Connection",
    root_id: int,
    branch_id: int | None,
    root: "X.sqlite3.Row",
    hits_by_digest: dict[str, list[tuple[str, str]]],
    current: dict[str, str],
) -> list[_Finding]:
    """Expand each secret-bearing blob into concrete findings (per location)."""
    findings: list[_Finding] = []
    for digest, hits in hits_by_digest.items():
        for path, event_id, ts, command in _locations_for(
            conn, root_id, branch_id, root, digest
        ):
            cur_hash = current.get(path)
            if cur_hash is None:
                status = "removed: path is no longer in the working tree"
                gone = True
            elif cur_hash != digest:
                status = "removed: no longer this file's content"
                gone = True
            else:
                status = "LIVE: still the current content of this file"
                gone = False
            for pattern, redacted in hits:
                findings.append(
                    _Finding(path, pattern, redacted, event_id, ts, command,
                             status, gone)
                )
    return findings


def _short(cmd: str, limit: int = 72) -> str:
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= limit else cmd[: limit - 1] + "…"


def _report(findings: list[_Finding]) -> None:
    """Print grouped, styled findings and a summary line."""
    by_path: dict[str, list[_Finding]] = {}
    for f in findings:
        by_path.setdefault(f.path, []).append(f)

    total = len(findings)
    versions = {(f.path, f.event_id) for f in findings}

    X.click.secho(
        f"\n⚠  {total} secret finding(s) across {len(versions)} file "
        f"version(s), in {len(by_path)} file(s):\n",
        fg="red",
        bold=True,
    )
    for path in sorted(by_path):
        X.click.secho(f"■ {path}", fg="red", bold=True)
        # baseline (event id None) first, then chronological, then by pattern
        for f in sorted(by_path[path], key=lambda x: (x.event_id or 0, x.pattern)):
            where = f"event #{f.event_id}" if f.event_id is not None else "baseline"
            X.click.echo(
                "    "
                + X.click.style(f.pattern, fg="red", bold=True)
                + "  "
                + X.click.style(f.redacted, fg="yellow")
            )
            X.click.echo(
                "      "
                + X.click.style(f"{where} · {X.fmt_ts(f.ts)}", fg="cyan")
                + X.click.style(f"  $ {_short(f.command)}", dim=True)
            )
            if f.gone:
                X.click.secho("      ↳ " + f.status
                              + " (still recoverable from history)", fg="yellow")
            else:
                X.click.secho("      ↳ " + f.status, fg="red", bold=True)
        X.click.echo("")

    X.click.secho(
        f"{total} finding(s) in recorded history — rotate these secrets.",
        fg="red",
        bold=True,
    )


# --- command ----------------------------------------------------------------


def register(main) -> None:
    @main.command()
    @X.click.option(
        "--current-only",
        is_flag=True,
        help="Only scan versions that are the current content of an existing "
        "file (like an ordinary working-tree scanner).",
    )
    def audit(current_only: bool) -> None:
        """Scan every recorded file version for leaked secrets.

        chronx keeps file content across time, so this surfaces secrets that
        were committed and later deleted — still in history and invisible
        to a scanner that only sees the working tree. Scans the current root's
        active timeline and exits non-zero if anything is found.
        """
        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            branch_id = X.active_branch_id(conn, root_id)

            current = _current_state(conn, root_id)

            # 1. every distinct blob digest that ever held file content
            digests = _collect_digests(conn, root_id, branch_id)
            if current_only:
                digests &= set(current.values())

            # 2. scan each unique text blob exactly once
            hits_by_digest: dict[str, list[tuple[str, str]]] = {}
            scanned = 0
            for digest in digests:
                data = _load_blob(store, digest)
                if data is None or len(data) > _MAX_SCAN_BYTES:
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # binary / non-text blob
                scanned += 1
                hits = _scan_text(text, store)
                if hits:
                    hits_by_digest[digest] = hits

            if not hits_by_digest:
                X.click.secho(
                    "no secrets found in recorded history "
                    f"({scanned} file version(s) scanned)",
                    fg="green",
                )
                return

            # 3. map each secret-bearing blob back to where/when it appeared
            findings = _locate(
                conn, root_id, branch_id, root, hits_by_digest, current
            )
            _report(findings)
            raise X.click.exceptions.Exit(1)
        finally:
            conn.close()
