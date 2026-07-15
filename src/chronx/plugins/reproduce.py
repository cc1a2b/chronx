"""chronx reproduce — re-run a recorded command in a fresh, isolated checkout
of its exact PRE-state and compare what it does NOW against what chronx
recorded THEN.

A determinism / reproducibility probe. It answers "does this command still do
the same thing?" — catching environment drift, non-determinism, and flaky
builds. Nothing about your working tree or the store is mutated: the command
runs inside a throwaway temp directory reconstructed purely from recorded
content, and the store is opened read-only.

The flow mirrors ``chronx rerun``, but fully sandboxed:

  1. resolve the event and its recorded deltas (the "expected" effect);
  2. reconstruct the FULL tree state just before the event into a temp dir;
  3. re-run the recorded command there (captured, optionally time-limited);
  4. re-scan the temp dir and diff the ACTUAL file changes against the
     recorded ones, per path, plus the exit code;
  5. print a verdict and exit non-zero if anything diverged.

Read-only over the store; the only writes go into a temp dir this command
creates and (unless ``--keep``) removes. It never imports ``chronx.cli``.
"""

from __future__ import annotations

import os
import shutil
import stat as statmod
import subprocess
import tempfile

from chronx import pluginlib as X


# --------------------------------------------------------------------- helpers


def _short(digest: str | None) -> str:
    """Compact, stable rendering of a blob hash (a dash means 'absent')."""
    return digest[:10] if digest else "-"


def _fmt_exit(code: int | None) -> str:
    """Human rendering of a recorded/observed exit code (``?`` = unknown)."""
    return "?" if code is None else str(code)


def _materialize(state: dict, store: "X.ObjectStore", dest: str) -> None:
    """Write every present file of a full tree ``state`` into ``dest``.

    ``state`` maps rel_path -> (hash|None, mode|None); a None hash means the
    file was absent at that moment and is simply skipped. Mirrors
    ``checkout.py``: make parent dirs, write the blob's raw bytes, restore mode.
    A blob that vanished between the pre-check and now surfaces as a clean
    ClickException rather than a traceback.
    """
    for rel, (digest, mode) in sorted(state.items()):
        if digest is None:
            continue
        full = X.Path(dest) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = store.get(digest)
        except (KeyError, ValueError) as exc:
            raise X.click.ClickException(
                f"blob for {rel} is unavailable ({exc}); run `chronx fsck`"
            ) from exc
        full.write_bytes(data)
        if mode is not None:
            try:
                os.chmod(full, statmod.S_IMODE(mode))
            except OSError:
                pass


def _scan_tree(root: str) -> dict:
    """Content hash of every regular file under ``root``, keyed by rel path.

    Paths use ``/`` separators to line up with recorded delta paths. Symlinks
    and other non-regular entries are ignored (we only diff regular file
    content, per the reproduce contract); unreadable files are skipped.
    """
    out: dict = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            try:
                data = X.Path(full).read_bytes()
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = X.hash_bytes(data)
    return out


def _row(glyph: str, color: str, path: str, detail: str, width: int) -> None:
    """Emit one aligned comparison row: '  <glyph> <path>  <detail>'."""
    X.click.echo(
        X.click.style(f"  {glyph} ", fg=color)
        + X.click.style(f"{path:<{width}}", fg=color)
        + "  "
        + X.click.style(detail, dim=True)
    )


# ------------------------------------------------------------------ command


def register(main) -> None:
    @main.command()
    @X.click.argument("event", type=int)
    @X.click.option(
        "--timeout", type=float, default=None,
        help="Abort the re-run after N seconds (counts as a divergence).",
    )
    @X.click.option(
        "--keep", is_flag=True,
        help="Keep the temp checkout for inspection and print its path.",
    )
    def reproduce(event: int, timeout: float | None, keep: bool) -> None:
        """Re-run event EVENT in an isolated checkout; check it still reproduces.

        Reconstructs the exact tree state just before EVENT into a throwaway
        temp directory, re-runs the recorded command there, and compares the
        file changes it produces NOW against the ones chronx recorded THEN
        (and the exit code). Exits non-zero if anything diverged — a quick
        determinism / flakiness check over recorded history.
        """
        conn = X.open_db()  # read-only over the store
        store = X.ObjectStore(X.paths().objects)
        tmp: str | None = None
        try:
            # 1. Resolve the event and its recorded ("expected") effect.
            row = X.dbm.event_by_id(conn, event)
            if row is None:
                raise X.click.ClickException(f"no event with id {event}")
            command = row["command"]
            if command is None:
                raise X.click.ClickException(
                    f"event #{event} is an external change; "
                    "there is no command to reproduce"
                )
            root_id = int(row["root_id"])
            started_at = float(row["started_at"])
            recorded_exit = row["exit_code"]
            expected = {d.path: d for d in X.dbm.deltas_for(conn, event)}

            # The tree state JUST BEFORE the event ran (its pre-state), on the
            # event's root + active timeline. Paths are relative to the root.
            pre_state = X.state_at(conn, root_id, started_at - 1e-6)

            # 2. Guard: every present pre-state file must still have its blob,
            #    or the checkout would be silently incomplete.
            missing = [
                rel for rel, (h, _m) in pre_state.items()
                if h is not None and not store.has(h)
            ]
            if missing:
                raise X.click.ClickException(
                    f"{len(missing)} pre-state file(s) have missing blobs "
                    f"(first: {missing[0]}); run `chronx fsck`"
                )

            # 3. Materialize the pre-state into a fresh, isolated temp checkout.
            tmp = tempfile.mkdtemp(prefix="chronx-reproduce-")
            _materialize(pre_state, store, tmp)
            n_present = sum(1 for h, _m in pre_state.values() if h is not None)

            # The command originally ran from row["cwd"], which may be a
            # subdirectory of the root; run it in the matching subdir so
            # relative paths resolve exactly as they did then.
            run_dir = tmp
            root_row = conn.execute(
                "SELECT path FROM roots WHERE id = ?", (root_id,)
            ).fetchone()
            if root_row is not None:
                rel_cwd = os.path.relpath(str(row["cwd"]), str(root_row["path"]))
                if rel_cwd not in (".", "") and not rel_cwd.startswith(".."):
                    run_dir = os.path.join(tmp, rel_cwd)
                    os.makedirs(run_dir, exist_ok=True)

            # 4. Re-run the recorded command there — captured and time-limited.
            shell = os.environ.get("SHELL") or "/bin/sh"
            timed_out = False
            actual_exit: int | None = None
            try:
                proc = subprocess.run(
                    [shell, "-c", command],
                    cwd=run_dir,
                    timeout=timeout,
                    capture_output=True,
                )
                actual_exit = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True

            # 5. Re-scan the checkout and derive the ACTUAL changes vs pre-state.
            post = _scan_tree(tmp)
            pre_hashes = {rel: h for rel, (h, _m) in pre_state.items()}
            actual: dict = {}  # rel -> (change, after_hash)
            for rel in set(pre_hashes) | set(post):
                before = pre_hashes.get(rel)
                after = post.get(rel)
                if before == after:
                    continue  # untouched (or changed and changed back)
                if before is None:
                    actual[rel] = ("A", after)
                elif after is None:
                    actual[rel] = ("D", None)
                else:
                    actual[rel] = ("M", after)

            # 6. Compare recorded (expected) vs actual, per path. The key test
            #    is "same resulting content hash?" (deletions match on None).
            matches: list[str] = []
            differs: list[tuple] = []
            extra: list[tuple] = []
            missing_now: list[tuple] = []
            for rel in sorted(set(expected) | set(actual)):
                exp = expected.get(rel)
                act = actual.get(rel)
                if exp is not None and act is not None:
                    if exp.after_hash == act[1]:
                        matches.append(rel)
                    else:
                        differs.append((rel, exp, act))
                elif act is not None:
                    extra.append((rel, act))        # changed now, not then
                else:
                    missing_now.append((rel, exp))  # recorded then, not now

            exit_matches = (not timed_out) and (recorded_exit == actual_exit)
            diverged = bool(differs or extra or missing_now) or not exit_matches

            # 7. Report ------------------------------------------------------
            X.click.secho(f"reproduce event #{event}", bold=True)
            X.click.echo(f"  $ {X.describe_command(row)}")
            X.click.secho(
                f"  materialized {n_present}-file pre-state into an isolated "
                "checkout, re-ran the command",
                dim=True,
            )
            X.click.echo()

            # Exit-code line.
            if timed_out:
                X.click.echo(
                    f"  exit code:  recorded {_fmt_exit(recorded_exit)}"
                    f"  →  now timed out after {timeout:g}s   "
                    + X.click.style("✗", fg="red")
                )
            else:
                ok = X.click.style(
                    "✓" if exit_matches else "✗",
                    fg="green" if exit_matches else "red",
                )
                X.click.echo(
                    f"  exit code:  recorded {_fmt_exit(recorded_exit)}"
                    f"  →  now {_fmt_exit(actual_exit)}   {ok}"
                )
            X.click.echo()

            # Per-path comparison table.
            shown = (
                matches
                + [r for r, _e, _a in differs]
                + [r for r, _a in extra]
                + [r for r, _e in missing_now]
            )
            width = min(44, max((len(p) for p in shown), default=0))
            if not shown:
                X.click.secho(
                    "  (no tracked file changes, then or now)", dim=True
                )
            else:
                for rel in matches:
                    exp = expected[rel]
                    _row("✓", "green", rel,
                         f"reproduced identically ({exp.change} {_short(exp.after_hash)})",
                         width)
                for rel, exp, act in differs:
                    _row("✗", "red", rel,
                         f"DIFFERS: recorded {exp.change} {_short(exp.after_hash)}"
                         f"  now {act[0]} {_short(act[1])}",
                         width)
                for rel, act in extra:
                    _row("+", "yellow", rel,
                         f"changed now but NOT in the recording "
                         f"({act[0]} {_short(act[1])})",
                         width)
                for rel, exp in missing_now:
                    _row("−", "yellow", rel,
                         f"recorded {exp.change} {_short(exp.after_hash)} then, "
                         "but unchanged now",
                         width)

            X.click.echo()

            # 8. Verdict -----------------------------------------------------
            if not diverged:
                X.click.secho(
                    f"✓ reproduced identically "
                    f"({len(matches)} file(s) changed, exit "
                    f"{_fmt_exit(actual_exit)})",
                    fg="green", bold=True,
                )
            else:
                reasons: list[str] = []
                if differs:
                    reasons.append(f"{len(differs)} file(s) differ")
                if extra:
                    reasons.append(f"{len(extra)} changed now but not then")
                if missing_now:
                    reasons.append(f"{len(missing_now)} recorded then but not now")
                if timed_out:
                    reasons.append(f"timed out after {timeout:g}s")
                elif recorded_exit != actual_exit:
                    reasons.append(
                        f"exit {_fmt_exit(recorded_exit)} → "
                        f"{_fmt_exit(actual_exit)}"
                    )
                X.click.secho(
                    "✗ diverged: " + "; ".join(reasons),
                    fg="red", bold=True,
                )
                raise X.click.exceptions.Exit(1)
        finally:
            # 9. Always clean up the throwaway checkout, unless asked to keep it.
            if tmp is not None:
                if keep:
                    X.click.secho(f"  kept checkout: {tmp}", dim=True)
                else:
                    shutil.rmtree(tmp, ignore_errors=True)
            conn.close()
