"""chronx stash — shelve un-recorded working-tree drift, like ``git stash``.

``chronx`` continuously records the commands you run and the file changes they
cause. Anything you edit while the daemon is stopped (or before it settles) is
*drift*: real changes on disk that are not part of any recorded event. This
plugin lets you set that drift aside — reverting the tree to the last recorded
snapshot — and restore it again later, on demand.

    chronx stash [-m MSG]     save current drift, revert the tree to clean
    chronx stash list         show the shelved stashes for this root
    chronx stash pop [NAME]    re-apply a stash (default: the most recent)
    chronx stash drop NAME     discard a stash without applying it

Unlike ``undo`` / ``rollback`` / ``cherry-pick``, a stash is NOT a recorded
chronx event: the daemon never sees it as history. Shelved content lives in the
content-addressed object store (so it is deduped and can never be lost), and
each stash is described by a small sidecar JSON under ``~/.chronx/stashes/``.
This keeps "work I haven't committed to the timeline yet" entirely separate
from the recorded timeline.

Safety mirrors the recording write-plugins (e.g. ``cherry_pick``):

  * the daemon must be stopped before we touch the tree (otherwise we would
    race the recorder);
  * every drifted file's current bytes are copied into the object store before
    the tree is reverted, so a stash can always be popped back;
  * all writes are atomic (temp file + ``os.replace`` via ``X.write_atomic``);
  * after mutating the tree we nudge a (re)started daemon to resync so it does
    not re-record our own writes.

Imports only ``chronx.pluginlib`` (as ``X``) and stdlib; never ``chronx.cli``.
"""

from __future__ import annotations

import json
import time

from chronx import pluginlib as X

# Sidecar records live here, one JSON file per stash, named ``<id>.json``.
_STASH_SUBDIR = "stashes"


# --------------------------------------------------------------- sidecar store


def _stash_dir() -> X.Path:
    """Directory holding stash sidecars for this store (created 0700 if absent)."""
    d = X.paths().home / _STASH_SUBDIR
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _next_stash_id(d: X.Path) -> int:
    """A fresh integer id for a new stash.

    Starts from the count of existing stashes (so ids climb monotonically) and
    bumps past any filename that already exists, so ids never collide even after
    earlier stashes have been popped or dropped.
    """
    candidate = sum(1 for _ in d.glob("*.json"))
    while (d / f"{candidate}.json").exists():
        candidate += 1
    return candidate


def _slugify(text: str) -> str:
    """A short, filesystem/CLI-friendly slug of a message (no ``re`` needed)."""
    out: list[str] = []
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " \t-_/.":
            out.append("-")
        # any other character is dropped
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:40]


def _ref(stash: dict) -> str:
    """The canonical ``stash@{N}`` handle for a stash (always unique)."""
    return f"stash@{{{stash.get('id', 0)}}}"


def _label(stash: dict) -> str:
    """Human name to print for a stash (its message-slug name, or the ref)."""
    return stash.get("name") or _ref(stash)


def _load_stashes(root_id: int, root_path: X.Path) -> list[dict]:
    """Every stash belonging to this root, most-recent first.

    Each returned dict is the parsed sidecar plus a private ``_file`` key that
    holds its :class:`Path`. Corrupt or unreadable sidecars are skipped rather
    than aborting the whole listing.
    """
    d = _stash_dir()
    rp = str(root_path)
    out: list[dict] = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # unreadable or not valid JSON — ignore it
        if not isinstance(data, dict):
            continue
        # Scope to the cwd's root: match on the stable root_id, with the
        # recorded path as a secondary fallback.
        if data.get("root_id") == root_id or data.get("root_path") == rp:
            data["_file"] = f
            out.append(data)
    out.sort(
        key=lambda s: (float(s.get("created_at", 0.0)), int(s.get("id", 0))),
        reverse=True,
    )
    return out


def _find_stash(stashes: list[dict], query: str) -> dict | None:
    """Resolve a user-supplied name / ``stash@{N}`` ref / bare id to a stash.

    ``stashes`` is newest-first, so the first match wins on ties.
    """
    q = query.strip()
    for s in stashes:
        if q == s.get("name") or q == _ref(s) or q == str(s.get("id")):
            return s
    return None


# ------------------------------------------------------------------- save


def _do_save(message: str | None) -> None:
    """Shelve current drift and revert the working tree to the recorded state."""
    # A write op: never run while the recorder is live (it would race our writes).
    X.require_daemon_stopped("stash")

    conn = X.open_db(readonly=True)
    store = X.ObjectStore(X.paths().objects)
    try:
        root = X.root_for_cwd(conn)  # clean ClickException if cwd is untracked
        root_id = int(root["id"])
        root_path = X.Path(root["path"])

        # 1. What has drifted from the last recorded snapshot? (in-memory hash;
        #    nothing is stored by this comparison).
        cfg = X.Config.load(X.paths())
        manifest = X.dbm.load_manifest(conn, root_id)
        drift = X.working_changes(root_path, manifest, cfg)
        if not drift:
            X.click.echo("no local changes to stash")
            return

        # 2. The clean tree we will revert to: the FULL last-recorded state of
        #    the active timeline.
        recorded = X.state_at(conn, root_id, time.time())

        # 3. Capture each drifted file's CURRENT bytes into the object store and
        #    describe how to restore it later. A file that exists now becomes a
        #    "present" entry (its content is saved); a drift that is a deletion
        #    (recorded but now absent) becomes a deletion to re-apply on pop.
        entries: list[dict] = []
        for c in drift:
            full = root_path / c.rel
            cur_bytes, cur_st = X.current_file_state(full)
            if cur_bytes is not None:
                digest = store.put_bytes(cur_bytes)  # dedup + safety copy of drift
                entries.append(
                    {
                        "rel": c.rel,
                        "present": True,
                        "hash": digest,
                        "mode": cur_st.st_mode if cur_st is not None else None,
                        "size": len(cur_bytes),
                    }
                )
            else:
                entries.append(
                    {"rel": c.rel, "present": False, "hash": None,
                     "mode": None, "size": 0}
                )

        # 4. Make sure every blob we need to CLEAN the tree is present before we
        #    touch anything — fail loudly, name the file, change nothing.
        for c in drift:
            tgt = recorded.get(c.rel)
            if tgt is not None and tgt[0] is not None and not store.has(tgt[0]):
                raise X.click.ClickException(
                    f"blob for {c.rel} is missing from the object store; "
                    "cannot revert to a clean tree (run `chronx fsck`)"
                )

        # 5. Persist the sidecar FIRST, so the drift stays recoverable even if
        #    the revert below is interrupted.
        d = _stash_dir()
        stash_id = _next_stash_id(d)
        slug = _slugify(message) if message else ""
        name = slug or f"stash@{{{stash_id}}}"
        record = {
            "id": stash_id,
            "name": name,
            "message": message,
            "created_at": time.time(),
            "root_path": str(root_path),
            "root_id": root_id,
            "entries": entries,
        }
        sidecar = d / f"{stash_id}.json"
        X.write_atomic(
            sidecar, json.dumps(record, indent=2).encode("utf-8"), mode=0o600
        )

        # 6. Revert the working tree to the recorded snapshot: restore each
        #    drifted path to its recorded content, or delete it if it was never
        #    recorded (a brand-new, added file). Driven purely off `recorded`,
        #    so it is correct regardless of the drift's change code.
        for c in drift:
            full = root_path / c.rel
            tgt = recorded.get(c.rel)
            if tgt is None or tgt[0] is None:
                try:
                    full.unlink(missing_ok=True)
                except OSError as exc:
                    raise X.click.ClickException(
                        f"could not remove {c.rel}: {exc}"
                    ) from exc
            else:
                blob = store.get(tgt[0])
                try:
                    X.write_atomic(full, blob, tgt[1])
                except OSError as exc:
                    raise X.click.ClickException(
                        f"could not restore {c.rel}: {exc}"
                    ) from exc

        # 7. Nudge a (re)started daemon so it doesn't re-record our revert.
        X.send_sync(root_path)

        X.click.secho(f"saved {name} ({len(entries)} file(s))", fg="green")
        if name != _ref(record):
            X.click.secho(f"  ref: {_ref(record)}", dim=True)
        X.click.secho("  restore with `chronx stash pop`", dim=True)
    finally:
        conn.close()


# ------------------------------------------------------------------- list


def _do_list() -> None:
    """Print the stashes saved for the current root (read-only)."""
    conn = X.open_db(readonly=True)
    try:
        root = X.root_for_cwd(conn)
        stashes = _load_stashes(int(root["id"]), X.Path(root["path"]))
    finally:
        conn.close()

    if not stashes:
        X.click.echo("no stashes")
        return

    for s in stashes:
        ref = _ref(s)
        when = X.fmt_ts(float(s.get("created_at", 0.0)))
        n = len(s.get("entries", []))
        name = s.get("name", ref)
        line = ref
        if name and name != ref:  # a message-derived slug name
            line += f"  ({name})"
        line += f"  {when}  {n} file(s)"
        X.click.echo(line)
        msg = s.get("message")
        if msg:
            X.click.secho(f"    {msg}", dim=True)


# ------------------------------------------------------------------- pop


def _do_pop(name: str | None) -> None:
    """Re-apply a stash onto the tree and discard it (default: most recent)."""
    # A write op — hold off the recorder while we rewrite files.
    X.require_daemon_stopped("stash")

    conn = X.open_db(readonly=True)
    store = X.ObjectStore(X.paths().objects)
    try:
        root = X.root_for_cwd(conn)
        root_path = X.Path(root["path"])
        stashes = _load_stashes(int(root["id"]), root_path)
    finally:
        conn.close()

    if not stashes:
        X.click.echo("no stashes to pop")
        return
    if name is None:
        target = stashes[0]  # newest
    else:
        target = _find_stash(stashes, name)
        if target is None:
            avail = ", ".join(_ref(s) for s in stashes)
            raise X.click.ClickException(
                f"no stash named {name!r} here (available: {avail})"
            )

    entries = target.get("entries", [])

    # Verify every blob we need is present BEFORE touching the tree or sidecar,
    # so a missing blob is a clean error that changes nothing.
    for e in entries:
        if e.get("present") and not store.has(e["hash"]):
            raise X.click.ClickException(
                f"blob for {e['rel']} is missing from the object store; "
                "cannot restore this stash (run `chronx fsck`)"
            )

    # Re-apply the drift. Present entries overwrite whatever is on disk now
    # (last-write-wins, like `git stash pop` onto a dirty tree); deletion
    # entries remove the file again.
    for e in entries:
        full = root_path / e["rel"]
        if e.get("present"):
            blob = store.get(e["hash"])
            try:
                X.write_atomic(full, blob, e.get("mode"))
            except OSError as exc:
                raise X.click.ClickException(
                    f"could not write {e['rel']}: {exc}"
                ) from exc
        else:
            try:
                full.unlink(missing_ok=True)
            except OSError as exc:
                raise X.click.ClickException(
                    f"could not remove {e['rel']}: {exc}"
                ) from exc

    # pop = apply + discard: drop the sidecar, then resync the daemon.
    sidecar = target.get("_file")
    if sidecar is not None:
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass  # applied fine; a lingering sidecar is harmless
    X.send_sync(root_path)

    X.click.secho(
        f"restored {_label(target)} ({len(entries)} file(s))", fg="green"
    )


# ------------------------------------------------------------------- drop


def _do_drop(name: str) -> None:
    """Discard a stash without applying it (metadata only; no tree writes)."""
    conn = X.open_db(readonly=True)
    try:
        root = X.root_for_cwd(conn)
        stashes = _load_stashes(int(root["id"]), X.Path(root["path"]))
    finally:
        conn.close()

    if not stashes:
        X.click.echo("no stashes to drop")
        return
    target = _find_stash(stashes, name)
    if target is None:
        avail = ", ".join(_ref(s) for s in stashes)
        raise X.click.ClickException(
            f"no stash named {name!r} here (available: {avail})"
        )

    sidecar = target.get("_file")
    if sidecar is not None:
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            raise X.click.ClickException(f"could not drop stash: {exc}") from exc
    X.click.secho(f"dropped {_label(target)}", fg="yellow")


# --------------------------------------------------------------- registration


def register(main) -> None:
    @main.group("stash", invoke_without_command=True)
    @X.click.option("-m", "--message", default=None,
                    help="Description for this stash (saved with a bare "
                         "`chronx stash`).")
    @X.click.pass_context
    def stash(ctx, message: str | None) -> None:
        """Shelve un-recorded working-tree drift, like `git stash`.

        With no subcommand this behaves as `save`: it captures the current drift
        (changes on disk the daemon hasn't recorded), reverts the tree to the
        last recorded snapshot, and lets you restore the drift later with `pop`.
        """
        if ctx.invoked_subcommand is None:
            _do_save(message)

    @stash.command("save")
    @X.click.option("-m", "--message", default=None,
                    help="Description for this stash.")
    def stash_save(message: str | None) -> None:
        """Save current drift and revert the tree to the recorded snapshot."""
        _do_save(message)

    @stash.command("list")
    def stash_list() -> None:
        """List the stashes saved for the current root."""
        _do_list()

    @stash.command("pop")
    @X.click.argument("name", required=False)
    def stash_pop(name: str | None) -> None:
        """Re-apply a stash (default: the most recent) and discard it.

        NAME may be a message slug, a `stash@{N}` ref, or the bare id N.
        Re-applied files overwrite whatever is on disk now.
        """
        _do_pop(name)

    @stash.command("drop")
    @X.click.argument("name")
    def stash_drop(name: str) -> None:
        """Discard a stash (NAME) without applying it."""
        _do_drop(name)
