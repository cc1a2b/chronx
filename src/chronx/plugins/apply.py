"""chronx apply — apply an external unified-diff patch and record it.

The companion to ``chronx format-patch``: where that turns a recorded event
*into* a patch, this takes an arbitrary unified diff (from ``format-patch``,
``git diff``, ``diff -u``, ...), applies it to the working tree, and records the
resulting file changes as ONE ordinary chronx event — so applying a patch is
tracked in history and fully reversible with ``chronx undo``.

How the changes are attributed:
  * We snapshot the recorded manifest *before* touching disk.
  * We hand the patch to the system tool (``git apply`` preferred, else
    ``patch``), running a DRY-RUN first — if it does not check out we abort and
    change nothing.
  * After the real apply we diff the live tree against that pre-apply manifest
    (``X.working_changes``) to learn exactly which files the patch touched, and
    fold those into a single recorded event.

Safety mirrors ``cherry_pick.py`` / ``ops.apply_merge``:
  * the daemon must be stopped (it would otherwise race the recorder);
  * the recorded event's ``before_*`` sides come from the pre-apply manifest,
    whose content blobs already live in the object store from prior history, so
    ``chronx undo`` can always restore the pre-patch state;
  * new on-disk content is written into the object store (``put_bytes``) so it
    too is content-addressed and recoverable;
  * the whole thing lands as one event and the daemon is told to resync so it
    does not double-record our own writes.

This module only touches the working tree through that safety net and never
imports ``chronx.cli``.
"""

from __future__ import annotations

import shutil
import subprocess
import time

from chronx import pluginlib as X


# --------------------------------------------------------------------- tooling


def _decode(raw: bytes) -> str:
    """Best-effort text from a subprocess byte stream (never raises)."""
    return raw.decode("utf-8", errors="replace").strip()


def _git_cmd(patch_path: X.Path, strip: int, reverse: bool, *, check: bool) -> list[str]:
    """``git apply`` argv. ``check=True`` is the dry-run that mutates nothing."""
    cmd = ["git", "apply", f"-p{strip}"]
    if reverse:
        cmd.append("--reverse")
    if check:
        cmd.append("--check")
    cmd.append(str(patch_path))
    return cmd


def _patch_cmd(patch_path: X.Path, strip: int, reverse: bool, *, dry: bool) -> list[str]:
    """``patch`` argv fallback. ``dry=True`` is the ``--dry-run`` check."""
    cmd = ["patch", f"-p{strip}"]
    if reverse:
        cmd.append("-R")
    if dry:
        cmd.append("--dry-run")
    cmd += ["-i", str(patch_path)]
    return cmd


def _run(cmd: list[str], cwd: X.Path) -> subprocess.CompletedProcess[bytes]:
    """Run a patch tool in ``cwd``, capturing output (bytes)."""
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True)


# ------------------------------------------------------------------ recording


def _build_event(
    root_path: X.Path,
    pre_manifest: dict[str, "X.dbm.ManifestEntry"],
    drift: list,
    store: "X.ObjectStore",
) -> tuple[list["X.dbm.Delta"], dict[str, "X.dbm.ManifestEntry"], set[str]]:
    """Turn the post-apply drift into deltas + manifest mutations.

    For each changed path we compare the pre-apply manifest state (the
    ``before`` side, whose blobs are already in the store) against the current
    on-disk content (the ``after`` side, which we snapshot into the store).
    """
    deltas: list[X.dbm.Delta] = []
    manifest_updates: dict[str, X.dbm.ManifestEntry] = {}
    manifest_deletes: set[str] = set()

    for change in drift:
        rel = change.rel
        full = root_path / rel

        # before = last recorded state (None if the file was untracked/absent).
        pre = pre_manifest.get(rel)
        before_hash = pre.hash if pre is not None else None
        before_size = pre.size if pre is not None else None
        before_mode = pre.mode if pre is not None else None

        # after = what the patch left on disk right now.
        cur_bytes, cur_st = X.current_file_state(full)

        if cur_bytes is None:
            # File is gone (or became non-regular) — record a deletion.
            if before_hash is None:
                continue  # never tracked and now absent: nothing to record
            deltas.append(X.dbm.Delta(
                rel, "D", before_hash, None,
                before_size, None, before_mode, None,
            ))
            manifest_deletes.add(rel)
            continue

        after_hash = X.hash_bytes(cur_bytes)
        if after_hash == before_hash:
            continue  # net no-op (e.g. patch reverted a pre-existing edit)

        # Snapshot the new content so the event is content-addressed.
        store.put_bytes(cur_bytes)
        after_size = len(cur_bytes)
        after_mode = cur_st.st_mode if cur_st is not None else None
        op = "M" if before_hash is not None else "A"
        deltas.append(X.dbm.Delta(
            rel, op, before_hash, after_hash,
            before_size, after_size, before_mode, after_mode,
        ))
        manifest_updates[rel] = X.dbm.ManifestEntry(
            hash=after_hash, size=after_size,
            mtime=cur_st.st_mtime if cur_st is not None else time.time(),
            mode=after_mode if after_mode is not None else 0o644,
        )

    return deltas, manifest_updates, manifest_deletes


# ------------------------------------------------------------------ command


def register(main) -> None:
    @main.command("apply")
    @X.click.argument("patch")
    @X.click.option("--reverse", "-R", is_flag=True,
                    help="Apply the patch in reverse (undo it).")
    @X.click.option("-p", "strip", type=int, default=1, show_default=True,
                    metavar="N", help="Strip N leading path components (like -pN).")
    @X.click.option("--yes", "-y", is_flag=True,
                    help="Skip the confirmation prompt.")
    def apply(patch: str, reverse: bool, strip: int, yes: bool) -> None:
        """Apply an external unified-diff PATCH and record it as one event.

        PATCH is a file containing a unified diff (from ``chronx format-patch``,
        ``git diff``, ``diff -u`` ...). It is applied to the working tree with
        ``git apply`` (falling back to ``patch``) and the resulting file changes
        are recorded as a single, reversible event — undo it with ``chronx
        undo``. The patch is dry-run first; if it does not apply cleanly nothing
        on disk is touched.
        """
        # 1. Never race the recorder — this is a write path.
        X.require_daemon_stopped("apply")

        # Resolve the patch to an absolute path so it survives the cwd switch to
        # the root when we invoke the tool.
        patch_path = X.Path(patch).expanduser()
        if not patch_path.is_absolute():
            patch_path = (X.Path.cwd() / patch_path)
        patch_path = patch_path.resolve()
        if not patch_path.is_file():
            raise X.click.ClickException(f"patch file not found: {patch}")

        # 2. Pick a patch tool up front so a missing one fails before any work.
        use_git = shutil.which("git") is not None
        if not use_git and shutil.which("patch") is None:
            raise X.click.ClickException(
                "no patch tool available — install `git` or `patch`"
            )
        tool = "git apply" if use_git else "patch"

        conn = X.open_db(readonly=False)
        store = X.ObjectStore(X.paths().objects)
        try:
            # 3. Resolve the root and snapshot its pre-apply recorded state.
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])
            root_path = X.Path(root["path"])
            pre_manifest = X.dbm.load_manifest(conn, root_id)
            cfg = X.Config.load(X.paths())

            # 4. Dry-run: confirm the patch applies cleanly, changing nothing.
            if use_git:
                check = _run(_git_cmd(patch_path, strip, reverse, check=True), root_path)
                apply_cmd = _git_cmd(patch_path, strip, reverse, check=False)
            else:
                check = _run(_patch_cmd(patch_path, strip, reverse, dry=True), root_path)
                apply_cmd = _patch_cmd(patch_path, strip, reverse, dry=False)
            if check.returncode != 0:
                detail = _decode(check.stderr) or _decode(check.stdout) or \
                    f"{tool} could not apply the patch"
                raise X.click.ClickException(
                    f"patch does not apply (tree unchanged):\n{detail}"
                )

            # 5. Report the clean check and (unless --yes) confirm.
            X.click.secho(
                f"patch {patch_path.name} applies cleanly via {tool}"
                f"{' (reversed)' if reverse else ''}.", bold=True,
            )
            if not yes:
                if not X.click.confirm(
                    "Apply this patch to the working tree and record it?",
                    default=False,
                ):
                    raise X.click.Abort()

            # 6. Apply for real. Even if the tool reports trouble mid-way we still
            #    attribute whatever actually landed on disk (step 7).
            result = _run(apply_cmd, root_path)
            apply_err = _decode(result.stderr) or _decode(result.stdout)

            # 7. Attribute: diff the live tree against the pre-apply manifest to
            #    learn exactly which files the patch touched, then fold them into
            #    deltas + manifest mutations.
            drift = X.working_changes(root_path, pre_manifest, cfg)
            deltas, manifest_updates, manifest_deletes = _build_event(
                root_path, pre_manifest, drift, store
            )

            # 8. Nothing changed: distinguish a genuine no-op from a failure.
            if not deltas:
                if result.returncode != 0:
                    raise X.click.ClickException(
                        f"patch failed and changed nothing:\n"
                        f"{apply_err or f'{tool} exited {result.returncode}'}"
                    )
                X.click.secho("patch applied but changed nothing.", fg="yellow")
                return

            # 9. Record the application as ONE event on the active timeline —
            #    the handle `chronx undo` reverts.
            now = time.time()
            command = f"chronx apply {patch_path.name}" + (" --reverse" if reverse else "")
            new_id = X.dbm.record_event(
                conn,
                session="chronx",
                root_id=root_id,
                cwd=str(root_path),
                command=command,
                started_at=now,
                finished_at=now,
                exit_code=0,
                deltas=deltas,
                manifest_updates=manifest_updates,
                manifest_deletes=manifest_deletes,
            )

            # 10. Nudge a (re)started daemon to resync so it does not re-record
            #     our own writes as a separate external change.
            X.send_sync(root_path)

            # 11. Report. Surface a partial-apply warning if the tool complained.
            if result.returncode != 0:
                X.click.secho(
                    f"warning: {tool} reported errors but some hunks applied:\n"
                    f"{apply_err}", fg="yellow",
                )
            X.click.secho(
                f"applied {patch_path.name}: {len(deltas)} file(s) changed",
                fg="green",
            )
            X.click.echo(f"  recorded as event #{new_id}")
            X.click.secho(
                f"  undo with `chronx undo --event {new_id}`", dim=True,
            )
        except X.OpsError as exc:
            # Surface logical failures as clean CLI errors.
            raise X.click.ClickException(str(exc)) from exc
        finally:
            conn.close()
