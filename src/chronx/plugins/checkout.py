"""chronx checkout — materialize the full tree state at any point into a dir.

A time-travel worktree: reconstruct the complete recorded state at a moment,
mark, event, or branch tip into a fresh directory, without touching your live
working tree. Read-only over the store; writes only into the target dir.
"""

from __future__ import annotations

import os
import stat as statmod

from chronx import pluginlib as X


def register(main) -> None:
    @main.command()
    @X.click.argument("ref")
    @X.click.argument("target", type=X.click.Path(path_type=X.Path))
    @X.click.option("--force", is_flag=True,
                    help="Write into TARGET even if it already has files.")
    def checkout(ref: "X.Path", target, force: bool) -> None:  # type: ignore[valid-type]
        """Reconstruct the tree at REF into TARGET.

        REF is a branch name, a mark, an event id, or a moment ('10m',
        '14:32', ISO...). TARGET must be empty or new (unless --force).
        """
        target = target.resolve()
        if target.exists() and any(target.iterdir()) and not force:
            raise X.click.ClickException(
                f"{target} is not empty (use --force to write into it anyway)"
            )

        conn = X.open_db()
        store = X.ObjectStore(X.paths().objects)
        try:
            root = X.root_for_cwd(conn)
            root_id = int(root["id"])

            branch = X.dbm.branch_by_name(conn, root_id, ref)
            if branch is not None:
                import time as _t

                state = X.branch_state_at(conn, branch, _t.time())
                desc = f"tip of timeline {ref!r}"
            else:
                ts = X.moment_ts(conn, ref)
                state = X.state_at(conn, root_id, ts)
                desc = f"{X.fmt_ts(ts)}"

            missing = [
                rel for rel, (h, _m) in state.items()
                if h is not None and not store.has(h)
            ]
            if missing:
                raise X.click.ClickException(
                    f"{len(missing)} file(s) have missing blobs (first: {missing[0]});"
                    " run `chronx fsck`"
                )

            target.mkdir(parents=True, exist_ok=True)
            written = 0
            for rel, (digest, mode) in sorted(state.items()):
                if digest is None:
                    continue
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                data = store.get(digest)
                dest.write_bytes(data)
                if mode is not None:
                    try:
                        os.chmod(dest, statmod.S_IMODE(mode))
                    except OSError:
                        pass
                written += 1

            X.click.secho(
                f"checked out {desc} → {target}", fg="green"
            )
            X.click.echo(f"  {written} file(s) reconstructed")
            X.click.secho(
                "  (a standalone snapshot; your working tree is untouched)", dim=True
            )
        finally:
            conn.close()
