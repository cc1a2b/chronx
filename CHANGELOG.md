# Changelog

## 0.2.8 — 2026-09-17

First release published to PyPI, so `pipx install chronx` now works from a
plain index rather than a checkout. No command or behaviour changes.

### Changed
- Releases are built and uploaded by GitHub Actions using PyPI trusted
  publishing (OIDC), with no API token stored anywhere. A tag push builds the
  sdist and wheel, checks the metadata, installs the wheel on Python 3.11,
  3.12 and 3.13, and only then publishes.
- The version is now read from `src/chronx/__init__.py` alone. It used to be
  written out in `pyproject.toml` as well, which meant the two could drift and
  a tag could ship a package claiming a different version; the release workflow
  now refuses to publish when the built artifacts disagree with the tag.
- License metadata uses the PEP 639 SPDX form (`license = "MIT"` plus
  `license-files`) instead of the deprecated `License ::` classifier, which
  PyPI no longer accepts for new projects.
- Added Changelog and Issues links to the PyPI project sidebar.

## 0.2.7 — 2026-07-16

### Added — 10 more commands (plugins)
- **`chronx from-git <repo>`** — import a git repository's history into chronx
  (one commit → one event), the inverse of `to-git`. Routes through the
  verified import path, so `from-git` then `to-git` round-trips.
- **`chronx apply <patch>`** — apply an external unified-diff patch to the tree
  and record it as one reversible event (companion to `format-patch`).
- **`chronx cat-file <ref>`** — low-level store inspector: dump a blob by digest,
  or a `<moment>:<path>` ref, like `git cat-file` (`-t`, `-s`, `--check`).
- **`chronx risky`** — safety audit of your command history (rm -rf, force-push,
  dd, pipe-to-shell, …), correlated with what actually got deleted.
- **`chronx focus`** — cluster commands into focused work-blocks by idle gaps.
- **`chronx heat <file>`** — per-line change-frequency map (which lines churn).
- **`chronx suggest`** — a shell coach: aliasable commands, undo-prone patterns,
  repeated failures, likely typos, uncheckpointed streaks.
- **`chronx impact <file>`** — one-file co-change: "when I change X, what else?".
- **`chronx wrapped`** — a curated "your session, wrapped" highlight reel.
- **`chronx mermaid`** — export the timeline as a GitHub-native Mermaid diagram
  (`gitGraph` / `flowchart`, `--fence` for pasting into Markdown).

## 0.2.6 — 2026-07-16

### Added — 10 more commands (plugins)
- **`chronx completion bash|zsh|fish`** — emit a native shell tab-completion
  script for chronx's now-90+ commands.
- **`chronx gen-docs`** — generate a full Markdown command reference from the
  live CLI (TOC, nested subcommands, per-command option tables).
- **`chronx graphviz`** — export the timeline/branch graph as Graphviz DOT
  (`chronx graphviz | dot -Tsvg`), with per-branch clusters and fork edges.
- **`chronx loc`** — total lines-of-code growth over the session (sparkline,
  `--by-ext` breakdown).
- **`chronx sizes <file>`** — one file's size evolution as a bar chart.
- **`chronx when <file> <pattern>`** — pinpoint the command where a file first
  started (or, `--gone`, stopped) matching a regex.
- **`chronx conflicts <other>`** — predict merge conflicts before merging (a
  read-only dry-run of `chronx merge`); exits non-zero when conflicts exist.
- **`chronx script`** — reconstruct a runnable, commented shell script from a
  recorded session (`--session`, `--since`, `--changed-only`, `--skip-failed`).
- **`chronx blame-stats <file>`** — aggregate line ownership: which command
  wrote how much of a file, ranked with percentages.
- **`chronx redo`** — re-apply the change most recently reverted by `undo`
  (reuses the undo safety net; itself reversible).

## 0.2.5 — 2026-07-16

### Added — 11 more commands (plugins)
- **`chronx ls [ref]`** — list files as they were at any moment/mark/event/branch
  (`-l` sizes+modes, `--tree`, `--path`).
- **`chronx sql [query]`** — a read-only SQL console over the store (writes are
  physically refused; `--schema`, `--tables`, `--json`).
- **`chronx since <moment>`** — the net cumulative diff since a moment, plus the
  commands responsible (`--stat`, `--commands`).
- **`chronx monitor`** — a live dashboard that tails recording in real time.
- **`chronx verify-store`** — a deep integrity + referential-consistency audit
  (blobs, foreign keys, manifest, delta shape, orphans, metadata) beyond `fsck`.
- **`chronx churn --by session|day|command|ext`** — line-churn report grouped by
  a dimension.
- **`chronx note add/show/list/rm`** — attach freeform notes to events via a
  sidecar (history stays pristine).
- **`chronx open <file> [when] [--vs other]`** — view a historical version in
  your pager, or diff any two moments of a file.
- **`chronx timings`** — wall-clock duration analytics (slowest runs, time by
  command, distribution).
- **`chronx diff-tree <refA> <refB>`** — structural tree comparison between any
  two moments *or branch tips* (`-p`, `--name-only`).
- **`chronx failures`** — failed-command analysis: exit-code distribution,
  most-failing commands, and time-to-fix.

### Fixed
- `range_changes` (used by `chronx diff A..B` and `chronx since`) is now scoped
  to the active timeline, so it no longer leaks changes from other branches.

### Changed
- `chronx.pluginlib` re-exports `is_ignored_rel` / `load_root_ignore`;
  `last_delta_for_path` gained a `branch_id` filter.

## 0.2.4 — 2026-07-15

### Added — 10 more commands (plugins)
- **`chronx whatchanged <file>`** — the complete change history of one file as a
  series of diffs over time (`git log -p <file>`).
- **`chronx hotspots`** — code hotspot + **change-coupling** analysis: most
  volatile files and which files tend to change together.
- **`chronx dump`** — export history as structured, metadata-only JSON for
  `jq` / external tooling (`--pretty`, `--since`, `--all-roots`).
- **`chronx reflog`** — a log of chronx's own operations (undo/rollback/merge/
  cherry-pick), i.e. reversible recovery points, à la `git reflog`.
- **`chronx archive <ref> -o <file>`** — `git archive` for time: a tar.gz/zip of
  the working tree at any moment/mark/event/branch, openable without chronx.
- **`chronx line-history <file> <pattern>`** — trace a line's lifecycle across
  history: when it was added, removed, changed, or re-added.
- **`chronx cast -o <file.html>`** — a self-contained, offline HTML "replay" of a
  session (timeline + colorized diffs), shareable, no server required.
- **`chronx reproduce <event>`** — re-run a recorded command in an isolated
  checkout of its pre-state and compare the effects to what was recorded — a
  determinism / reproducibility check. Never touches your working tree.
- **`chronx recover [<glob>]`** — bring back deleted files (restore last-recorded
  content), as one reversible event.
- **`chronx stash` / `pop` / `list` / `drop`** — shelve un-recorded working-tree
  drift and restore it later, like `git stash` (sidecar-based; not a recorded
  event).

### Changed
- `chronx.pluginlib` gained `working_changes`, `current_file_state`,
  `write_atomic`, and `send_sync` helpers for write-capable plugins.

## 0.2.3 — 2026-07-15

### Added — a plugin system and 10 new commands
- **Plugin architecture**: features now live as self-contained modules under
  `chronx/plugins/`, auto-discovered from the filesystem and loaded defensively
  (a broken plugin is skipped, never fatal). They use a stable helper surface
  (`chronx.pluginlib`) and never touch the core CLI.
- **`chronx checkout <ref> <dir>`** — materialize the full tree at any moment /
  mark / event / branch tip into a fresh directory (a time-travel worktree),
  without touching your live tree.
- **`chronx grep <regex>`** — temporal content search: find a pattern in *any
  recorded version of any file*, including versions later changed or deleted.
- **`chronx annotate <file>`** — line-level temporal blame (`git blame` across
  chronx history), attributing each line to the event that introduced it.
- **`chronx du`** — storage analytics: dedup/compression ratios, largest blobs
  and what references them, per-timeline attribution.
- **`chronx activity`** — a GitHub-style contribution heatmap + hour punchcard
  and streak/insight stats over your recorded sessions.
- **`chronx audit`** — scan *every* recorded file version for leaked secrets
  (AWS/GitHub/Slack/Google keys, private keys, high-entropy tokens). Catches
  secrets that were committed then deleted; redacts matches; exits non-zero.
- **`chronx format-patch <ref>`** — export an event (or `A..B` range) as a
  `git apply` / `git am` / `patch -p1`-compatible unified diff.
- **`chronx find <glob>`** — every path that ever existed matching a glob, with
  its lifetime (created → deleted), even for files long gone.
- **`chronx summary [window]`** — a digest of what changed over a time window:
  command/failure counts, insertions/deletions, hottest files, busiest commands.
- **`chronx cherry-pick <event>`** — apply one recorded event's file changes
  onto the current tree (across timelines), recorded as a reversible event.

### Fixed
- **`chronx exec`** now blocks until its event is finalized before returning, so
  scripted/CI callers can rely on the recording being complete — and rapid
  successive `exec` calls no longer collapse into one event.

### Known limitation
- A file created *and* deleted within a single command is not captured: the
  recorder reads content at settle time (post-command), when the transient file
  is already gone.

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
