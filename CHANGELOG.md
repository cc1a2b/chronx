# Changelog

## 0.2.0 — 2026-07-13

### Added
- `chronx log` — quick plain-terminal timeline (`-n`, `--changes-only`, `--all-roots`).
- `chronx cat FILE` — print a file's recorded content at any moment
  (`--at TIME`, `--event ID`, `--before`).
- `chronx restore FILE` — single-file time travel with the same safety net as
  undo: current content is snapshotted first and the restore is recorded as a
  reversible event.
- `chronx gc` — prune events older than `--keep-days` and delete blobs nothing
  references anymore (`--dry-run` to preview). Requires the daemon stopped.
- `chronx daemon status` now reports compressed on-disk store size.
- Replay TUI: live auto-refresh (follows the session as you work) and a
  `c` keybinding to hide commands that changed no files.

### Fixed
- Rapid consecutive commands no longer risk having their file changes
  misattributed to an `(external change)` event: the pre-command ambient sweep
  now also respects commands still waiting in the settle queue.

## 0.1.0 — 2026-07-13

Initial release: recording daemon (watchdog + FIFO shell hooks for bash/zsh),
content-addressed blake3 object store, sqlite event log, `init`, `hook`,
`daemon`, `diff`, `blame`, `undo`, and the `replay` TUI.
