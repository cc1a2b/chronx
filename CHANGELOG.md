# Changelog

## 0.2.2 — 2026-07-14

### Added — networked history
- **`chronx pull <url>`** — pull recorded history from a remote `chronx serve`
  into the local store (`chronx pull http://host:7373 --as .`). Re-pulls are
  idempotent and effectively incremental: events already present (matched on
  timestamp + command) are skipped; blobs dedup as always.
- `chronx serve` gained a read-only **`/bundle`** endpoint that streams a
  `.chronx` export archive for a root (what `pull` fetches). The server stays
  read-only; the client side reuses the import path with its daemon-stopped
  guard.
- `chronx import` learned `dedup` (used by pull) to skip already-present events.

## 0.2.1 — 2026-07-14

### Added
- **`chronx graph`** — a cross-timeline commit graph (like `git log --graph`).
  Each branch gets a colored lane; events lay out newest-first with fork
  points annotated and merges/external/internal events distinctly marked. Now
  you can *see* your alternate timelines. Also surfaced in the web UI (`chronx
  serve` → the **graph** button) via a new read-only `/api/graph` endpoint.

## 0.2.0 — 2026-07-14

### Added — timeline merging
- **`chronx merge <other>`** — merge another timeline into the active one with
  a real **three-way merge**: it finds the common-ancestor state (lowest
  common ancestor branch + fork point), then per file takes the side that
  changed, or content-merges non-overlapping edits (via `git merge-file` /
  `diff3`), and reports genuine conflicts. Auto-merges apply as one reversible
  event; conflicts abort by default (or `--allow-conflicts` writes conflict
  markers). `--dry-run` previews the plan.

  This completes the branching model from 0.1.9: fork → diverge → merge back.

## 0.1.9 — 2026-07-14

### Added — alternate timelines (branching)
- **`chronx fork <name> [--at <moment>]`** — fork a new timeline off the
  current one and switch to it; new commands record on the new branch. Fork
  from the past with `--at` to explore an alternate history.
- **`chronx switch <name>`** — switch timelines, reconstructing the working
  tree to that branch's tip. Both timelines are kept and fully isolated.
- **`chronx branches`** — list a directory's timelines (active marker, tip
  time, fork point). **`chronx branch-delete <name>`** removes one.
- Recording, `log`, `diff last`, `blame`, `undo`, `rollback`, `bisect`, and
  `to-git` are now timeline-aware: they operate on the active branch.

### Internal
- Schema v2 with an automatic, idempotent migration: existing single-timeline
  stores get a `main` branch and an immutable baseline snapshot; behavior is
  unchanged until you fork. Branch state is reconstructed as base-snapshot +
  forward delta replay, so any timeline can be materialized from any point.

## 0.1.8 — 2026-07-14

### Added
- **`chronx serve`** — a live web UI over your recorded history. An embedded,
  dependency-free HTTP server (stdlib only) serves a single-page dashboard:
  a live-updating timeline, colorized per-command unified diffs, click-through
  file blame, command search + `S:` content pickaxe, and a stats view. Reads
  the store READ-ONLY (a fresh connection per request), binds to localhost by
  default, and never touches the recording/undo/rollback write-paths.

## 0.1.7 — 2026-07-14

### Added
- **`chronx to-git <dir>`** — replay a directory's entire recorded history
  into a brand-new git repository: one commit per command (author date = when
  it ran, message = the command, with `chronx-event`/`cwd`/`exit` trailers),
  preceded by a "chronx baseline" commit holding the full tree at tracking
  start. Turns an ad-hoc shell session into real, reviewable git history you
  can `git log` / `git blame` / `git bisect` / push to a remote.

  Implemented by synthesizing a `git fast-import` stream directly: chronx's
  per-command deltas map onto fast-import `M`/`D` ops and the content-addressed
  blob store onto fast-import blob marks (deduped by hash). The chronx store is
  only read; nothing in the recording pipeline is touched.

## 0.1.6 — 2026-07-14

### Added
- **`chronx bisect --good <moment> -- <test>`** — find the command that broke
  something, by binary search over recorded history. chronx reconstructs the
  working tree at each candidate moment (reusing the rollback engine), runs
  your test command there (exit 0 = good, non-zero = bad), and pinpoints the
  first event whose changes made it fail — `git bisect run`, but over shell
  history. Verifies the endpoints, restores the tree to its starting state
  afterwards, and requires a stopped daemon + clean tree (like git bisect).

## 0.1.5 — 2026-07-13

### Added
- **`chronx export` / `chronx import`** — move recorded history between
  machines. Export bundles a root's events, deltas, marks, manifest, and every
  referenced blob into one gzip-compressed archive; import verifies each blob
  re-hashes to its name, dedups against the local store, and re-attaches the
  history under `--as <dir>` (paths remapped, marks de-collided). Reproduce a
  colleague's debugging session locally, then `rollback` into it.
- **`chronx status`** — working-tree drift versus the last recorded state
  (like `git status` for un-snapshotted changes): catches edits made while the
  daemon was stopped or a command still in flight. Read-only — hashes in
  memory, writes nothing. `--stat` for a summary, `--all-roots` to sweep all.

## 0.1.4 — 2026-07-13

### Added
- **Replay TUI v2**: a files pane per event (Tab into it for per-file diffs),
  `/` command filter, `u` undoes the selected event (confirmed modal,
  recorded + reversible as always), `m` drops a named mark at the current
  moment — scrub, inspect, and revert without leaving the timeline.
- `chronx watch <path>` — start tracking a directory immediately (baseline +
  watch) without waiting for a command to run there.
- `chronx daemon autostart on|off` — opt-in: each new hooked shell silently
  brings the daemon up if it isn't running (bash, zsh, and fish hooks).

## 0.1.3 — 2026-07-13

### Added
- **Range diff**: `chronx diff A..B` shows the NET difference between any two
  moments (marks, event ids, times, `now`) — changes undone inside the window
  cancel out. Works with `--stat`.
- **`chronx exec -- CMD...`** — record a command without shell hooks: for
  scripts, CI, cron, or uninstrumented shells. Exit code is passed through.
- **`chronx rerun <event>`** — re-execute a recorded command in its original
  directory; `--pristine` first rolls the tree back to just before the event,
  reproducing the original conditions. The rerun is recorded like any command.

## 0.1.2 — 2026-07-13

### Added
- `chronx doctor` — end-to-end pipeline diagnostics: store, database
  integrity, daemon + fifo reader probe, recent log errors, hook installation
  per shell, whether *this* shell is instrumented, watchdog backend, disk space.
- `chronx tail` — follow the event stream live (`-n` backlog, `--stat` file
  lists, `-c` changes only), like `tail -f` for your workflow.
- `chronx sessions` — per-shell-session summary (commands, changes, time
  span); `chronx log -s <session>` filters the timeline.
- `chronx report` — shareable Markdown of a time window / session
  (`--since 2h --full > debug-session.md`), with stat lists and optional diffs.
- `chronx roots` / `chronx roots forget <path>` — list tracked directories,
  or erase one's history entirely (daemon stopped, confirmed, gc reclaims blobs).

## 0.1.1 — 2026-07-13

### Added
- **`chronx rollback <moment>`** — revert the whole working directory to its
  state at any past moment (a mark, a time, or an event id). Applied as one
  recorded event, so a rollback is itself reversible; `--path` limits scope,
  `--dry-run` previews.
- **`chronx mark <name>` / `chronx marks`** — name a moment; `diff` and
  `rollback` accept mark names anywhere they accept times.
- **`chronx search PATTERN`** — find commands by substring, or with `-S`
  (pickaxe) find the events whose file changes added/removed lines matching a
  regex: "which command changed this line?"
- `chronx cat FILE` / `chronx restore FILE` — read or write back any file's
  recorded state at a moment or event (`--at`/`--event`/`--before`), with the
  undo-grade safety net (snapshot first, recorded as a reversible event).
- `chronx log` — plain-terminal timeline with delta counts (`--changes-only`).
- `chronx gc` — prune events older than `--keep-days` and unreferenced blobs
  (`--dry-run`); refuses to run against a live daemon.
- `chronx fsck` — verify every blob re-hashes to its name and every referenced
  hash exists on disk.
- `chronx stats` — hottest files, noisiest commands, store size.
- **fish shell support**: `chronx hook fish | source`.
- Per-root `.chronxignore` (dir names ending in `/`, basename globs otherwise).
- Replay TUI: live auto-refresh and a `c` changes-only toggle.
- `chronx daemon status` reports compressed on-disk store size.

### Fixed
- Rapid consecutive commands no longer risk having their file changes
  misattributed to an `(external change)` event: the pre-command ambient sweep
  now also respects commands still waiting in the settle queue.

## 0.1.0 — 2026-07-13

Initial release: recording daemon (watchdog + FIFO shell hooks for bash/zsh),
content-addressed blake3 object store, sqlite event log, `init`, `hook`,
`daemon`, `diff`, `blame`, `undo`, and the `replay` TUI.
