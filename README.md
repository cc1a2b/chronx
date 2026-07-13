# chronx

A **time-travel debugger for shell sessions**. Shell history records what you
typed — chronx also records what each command *did* to your files, so you can
scrub back through your own workflow, inspect any command's effects, and
safely revert them.

A lightweight background daemon snapshots working-directory state on every
command executed in an instrumented shell. Snapshots are content-addressed
diffs (git-style, deduplicated by blake3 hash) — never full copies.

```
$ tar xf release.tar --strip-components=1        # oops, extracted over my tree
$ chronx diff last                               # what did that just overwrite?
$ chronx blame src/config.py                     # which command clobbered this?
$ chronx undo                                    # put everything back
```

## Install

```sh
pipx install chronx
```

(or `pipx install .` from a checkout / `pip install chronx` into any env).

## Setup (once)

1. Create the store and print setup instructions:

   ```sh
   chronx init
   ```

2. Add the hook to your shell rc file and restart the shell:

   ```sh
   # ~/.bashrc
   eval "$(chronx hook bash)"

   # ~/.zshrc
   eval "$(chronx hook zsh)"

   # ~/.config/fish/config.fish
   chronx hook fish | source
   ```

3. Start the recorder:

   ```sh
   chronx daemon start
   ```

From then on, every command you run in an instrumented shell is recorded
together with the exact file changes it caused. The hook is fire-and-forget:
if the daemon isn't running it does nothing, and it never slows your prompt.

## Commands

| Command | What it does |
| --- | --- |
| `chronx init` | Set up `~/.chronx` and print the shell-hook instructions. |
| `chronx hook bash\|zsh` | Print the raw hook script (what `init` tells you to eval). |
| `chronx daemon start\|stop\|status` | Manage the background watcher. |
| `chronx replay` | Interactive TUI timeline — scrub with arrow keys; auto-follows the live session; `c` hides no-change commands. |
| `chronx log` | Quick plain-text timeline (`-n 50`, `--changes-only`, `--all-roots`). |
| `chronx diff <time>` | Show the filesystem changes at that moment (`last`, an event id, `10m`, `14:32`, an ISO date...). |
| `chronx blame <file>` | Which commands touched this file, and when. |
| `chronx cat <file>` | Print the file's recorded content at any moment (`--at 10m`, `--event 42 --before`). |
| `chronx restore <file>` | Put a single file back to its state at any moment (reversible, confirmed). |
| `chronx undo` | Revert the working dir to its state before the last command. |
| `chronx rollback <moment>` | Revert the **whole tree** to a mark, time, or event — one reversible event. |
| `chronx mark <name>` / `chronx marks` | Name the current moment; use the name anywhere a time is accepted. |
| `chronx search <pat>` | Grep command history; `-S <regex>` finds which command added/removed a line. |
| `chronx tail` | Follow the event stream live (`--stat` for file lists) — `tail -f` for your workflow. |
| `chronx sessions` | List recorded shell sessions; `chronx log -s <id>` filters to one. |
| `chronx report` | Shareable Markdown of a window/session: `chronx report --since 2h --full > session.md`. |
| `chronx stats` | Hottest files, noisiest commands, store size. |
| `chronx doctor` | Diagnose the pipeline: store, db, daemon, fifo, hooks, disk. |
| `chronx roots` | List tracked directories; `roots forget <path>` erases one's history. |
| `chronx fsck` | Verify blob integrity and that all referenced history is present. |
| `chronx gc` | Prune events older than `--keep-days` (default 30) and unreferenced blobs; `--dry-run` previews. |

### Marks & full rollback

```sh
chronx mark before-upgrade         # checkpoint this moment
./upgrade.sh && make migrate       # ... things go sideways ...
chronx rollback before-upgrade     # entire tree back to the checkpoint
```

Rollback reconstructs the tree state at that moment from history (files
changed since are restored, files created since are deleted, files deleted
since come back) and applies it as **one recorded event** — so `chronx undo`
reverts the rollback itself. `--path src/` limits the blast radius,
`--dry-run` shows the plan.

### Finding the culprit

```sh
chronx search 'pip install'        # grep your command history + effects
chronx search -S 'timeout *= *30'  # pickaxe: which command changed this line?
```

### Single-file time travel

`chronx blame src/config.py` tells you event `#42` clobbered the file;
`chronx restore src/config.py -e 42 --before` puts back the content from just
before that command ran. `chronx cat` is the read-only version — pipe an old
state anywhere without touching the working tree. Both accept `--at '14:32'`
style moments too.

### `chronx undo` safety

Undo shows the full plan and asks for confirmation. Before touching anything
it snapshots the current content of every affected file and records the
revert as an event of its own — so **undo is itself undoable** (the command
prints the event id that re-applies the change). Files that changed *again*
after the target event are flagged as conflicts and skipped unless you pass
`--force`. Use `--event <id>` to revert a specific event rather than the
last one.

## How it works

```
 shell hook ──PRE/POST──▶ named pipe ──▶ chronx daemon
                                            │
                          watchdog marks dirty paths per watched root
                                            │
                          on POST: hash dirty files (blake3), compare
                          with the manifest, store changed blobs
                                            │
              ~/.chronx/objects/  (content-addressed, zlib, dedup'd)
              ~/.chronx/chronx.db (sqlite: command ↔ file deltas ↔ time)
```

- The hook sends `PRE` (command string + cwd) before each command and `POST`
  (exit code) after it, over `~/.chronx/daemon.fifo`.
- The daemon recursively watches each working directory it sees ("root"),
  doing a one-time baseline scan on first contact.
- Filesystem events between `PRE` and `POST` define the set of *candidate*
  paths; each is re-hashed and compared against the manifest, so only real
  content changes become deltas — attributed to exactly that command.
- Changes made *between* commands (editor saves, cron, other tools) are
  recorded too, as `(external change)` events, so they are never mis-blamed
  on your next command.
- Blobs are stored once per unique content (`objects/<blake3[:2]>/<rest>`),
  zlib-compressed. Unchanged files cost nothing, ever.

## Storage layout

```
~/.chronx/
├── chronx.db      # events, deltas, manifests (sqlite, WAL)
├── objects/       # content-addressed blob store
├── config.toml    # tunables (size cap, ignores, settle delay)
├── daemon.fifo    # shell-hook -> daemon signal pipe
├── daemon.pid
└── daemon.log
```

## Configuration

`~/.chronx/config.toml` (created by `chronx init`, read at daemon start):

| Key | Default | Meaning |
| --- | --- | --- |
| `max_file_size` | 8 MiB | Files larger than this are never snapshotted. |
| `settle_ms` | 200 | Wait after a command before collecting its changes. |
| `max_files` | 50000 | Refuse to track directories bigger than this. |
| `allow_home_root` | false | Refuse to watch `$HOME` / `/` directly. |
| `extra_ignore_dirs` | `[]` | Additional directory names to ignore. |
| `extra_ignore_globs` | `[]` | Additional basename globs to ignore. |

`.git`, `node_modules`, `__pycache__`, virtualenvs, build dirs, editor swap
files, etc. are ignored by default. Set `CHRONX_HOME` to relocate the store.

Per-project rules go in `<root>/.chronxignore` (read when the daemon starts
tracking that directory): one pattern per line, `#` comments; lines ending in
`/` prune directories by name anywhere in the tree, anything else is a glob
matched against file basenames.

## Limitations

- Tracks regular files only: symlinks, sockets, and empty directories are not
  recorded; files over the size cap are ignored (and stop being tracked if
  they grow past it).
- Two commands running simultaneously in the *same* directory race for
  attribution; the first to finish claims the change.
- chronx is a workflow debugger, not a backup system — the object store lives
  on the same disk as your files.

## Uninstall

```sh
chronx daemon stop
pipx uninstall chronx
rm -rf ~/.chronx        # removes all recorded history
```

...and remove the `eval "$(chronx hook ...)"` line from your rc file.
