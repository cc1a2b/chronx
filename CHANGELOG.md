# Changelog

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
