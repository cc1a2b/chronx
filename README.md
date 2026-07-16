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
| `chronx replay` | Interactive TUI: scrub the timeline, Tab into the files pane for per-file diffs, `/` filter, `u` undo, `m` mark — live-follows the session. |
| `chronx serve` | Live web UI in the browser: timeline, colorized diffs, blame, search, stats (read-only). |
| `chronx watch <path>` | Start tracking a directory now, before any command runs there. |
| `chronx log` | Quick plain-text timeline (`-n 50`, `--changes-only`, `--all-roots`). |
| `chronx diff <time>` | Changes at a moment (`last`, an event id, a mark, `10m`, `14:32`) — or NET changes between two: `chronx diff good-state..now`. |
| `chronx exec -- <cmd>` | Run + record a command without shell hooks (scripts, CI, cron). |
| `chronx rerun <event>` | Re-execute a recorded command; `--pristine` reproduces its original pre-state first. |
| `chronx blame <file>` | Which commands touched this file, and when. |
| `chronx cat <file>` | Print the file's recorded content at any moment (`--at 10m`, `--event 42 --before`). |
| `chronx restore <file>` | Put a single file back to its state at any moment (reversible, confirmed). |
| `chronx undo` | Revert the working dir to its state before the last command. |
| `chronx rollback <moment>` | Revert the **whole tree** to a mark, time, or event — one reversible event. |
| `chronx bisect --good <m> -- <test>` | Binary-search history for the command that broke your test. |
| `chronx fork <name>` / `switch <name>` | Alternate timelines: branch history, experiment, switch between realities. |
| `chronx merge <name>` | Three-way merge another timeline into the current one. |
| `chronx branches` | List the timelines for this directory. |
| `chronx graph` | Cross-timeline commit graph (à la `git log --graph`); also in the web UI. |
| `chronx checkout <ref> <dir>` | Materialize the full tree at any moment/mark/event/branch into a fresh dir. |
| `chronx grep <regex>` | Search **every past version** of every file (incl. deleted ones). |
| `chronx annotate <file>` | Line-level temporal blame — which event introduced each line. |
| `chronx find <glob>` | Every path that ever existed + its lifetime (created → deleted). |
| `chronx audit` | Scan all history for leaked secrets — catches secrets committed then deleted. |
| `chronx du` | Storage analytics: dedup ratio, biggest blobs, per-timeline attribution. |
| `chronx activity` | Contribution heatmap + punchcard of your recorded sessions. |
| `chronx summary [window]` | Digest of what changed over a time window. |
| `chronx format-patch <ref>` | Export an event as a `git apply`-compatible patch. |
| `chronx cherry-pick <event>` | Apply one event's changes onto the current tree (reversible). |
| `chronx whatchanged <file>` | Full diff history of one file over time (`git log -p`). |
| `chronx line-history <file> <pat>` | Trace a line's lifecycle: added → removed → re-added. |
| `chronx hotspots` | Volatile files + change-coupling (which files change together). |
| `chronx recover [<glob>]` | Bring back deleted files (reversible). |
| `chronx stash` / `pop` | Shelve un-recorded working-tree drift and restore it later. |
| `chronx archive <ref> -o <f>` | `git archive` for time — tar/zip of the tree at any moment. |
| `chronx reproduce <event>` | Re-run a command in isolation and check it still does the same thing. |
| `chronx cast -o <f.html>` | Self-contained, shareable HTML replay of a session. |
| `chronx dump` | Export history as JSON for `jq` / external tooling. |
| `chronx reflog` | Log of chronx's own operations = recovery points. |
| `chronx ls [ref]` | List files as they were at any moment (`-l`, `--tree`). |
| `chronx diff-tree <a> <b>` | Structural diff between any two moments or branch tips. |
| `chronx since <moment>` | Net cumulative diff since a moment + the commands responsible. |
| `chronx open <file> [when] --vs <o>` | View/diff a file's historical versions in your pager. |
| `chronx sql [query]` | Read-only SQL console over the store. |
| `chronx churn --by …` | Line-churn report by session/day/command/extension. |
| `chronx timings` | Wall-clock duration analytics — slowest commands, time by command. |
| `chronx failures` | Failed-command analysis: exit codes, worst offenders, time-to-fix. |
| `chronx note add/list` | Attach freeform notes to events (sidecar; history stays pristine). |
| `chronx monitor` | Live dashboard tailing recording in real time. |
| `chronx verify-store` | Deep integrity + referential-consistency audit (beyond fsck). |
| `chronx mark <name>` / `chronx marks` | Name the current moment; use the name anywhere a time is accepted. |
| `chronx search <pat>` | Grep command history; `-S <regex>` finds which command added/removed a line. |
| `chronx tail` | Follow the event stream live (`--stat` for file lists) — `tail -f` for your workflow. |
| `chronx sessions` | List recorded shell sessions; `chronx log -s <id>` filters to one. |
| `chronx report` | Shareable Markdown of a window/session: `chronx report --since 2h --full > session.md`. |
| `chronx status` | Working-tree changes not yet recorded (drift vs the last snapshot). |
| `chronx export` / `import` | Move recorded history between machines; `import <a> --as .` re-attaches it here. |
| `chronx serve` + `chronx pull <url>` | Pull a teammate's recorded session over HTTP (idempotent, incremental). |
| `chronx to-git <dir>` | Replay your session into a real git repo — one commit per command. |
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

When you know *what* broke but not *which command* did it, let chronx find it
automatically — binary search over history, running your test at each step:

```sh
chronx daemon stop                 # bisect drives the tree itself
chronx bisect --good shipped -- pytest -x tests/test_api.py
#   GOOD  #41  ./refactor.sh
#   BAD   #47  ./optimize-queries.sh
#   ...
#   first bad event (the regression):
#   event #45  $ sed -i 's/LIMIT 100/LIMIT 10/' query.sql
```

chronx reconstructs the tree at each candidate moment from recorded blobs,
runs the test, and restores your starting state when done. `--good` is a
moment the test passed (a mark, event id, or time); `--bad` defaults to the
latest event.

### Alternate timelines

chronx isn't limited to one line of history. Fork the timeline, try a risky
approach on a separate branch, and switch back and forth — the working tree
is reconstructed each time, and both realities are kept:

```sh
chronx daemon stop                 # switching rewrites the tree
chronx fork try-async              # branch off "main", start experimenting
# ... hack, run commands, they record on try-async ...
chronx switch main                 # tree snaps back to main's state
chronx branches                    # see both timelines
```

`chronx fork <name> --at 1h` forks from a *past* moment, so you can explore
what might have happened if you'd taken a different path an hour ago.

When an experiment works out, bring it back with a three-way merge:

```sh
chronx switch main
chronx merge try-async             # auto-merges non-conflicting changes
```

chronx finds the common-ancestor state, takes whichever side changed each
file, content-merges non-overlapping edits within a file, and reports real
conflicts (which you can force in with `--allow-conflicts` to get the usual
`<<<<<<<`/`>>>>>>>` markers). The merge is one reversible event.

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

## Pull a session from another machine

chronx history is networked. On the machine that recorded it:

```sh
chronx serve --host 0.0.0.0        # expose read-only history
```

On yours:

```sh
chronx daemon stop
chronx pull http://their-host:7373 --as ./debug-repro
chronx log        # their commands
chronx graph      # their timelines
chronx rollback shipped   # reconstruct their tree at any point
```

Re-pull anytime — only new events are added (idempotent, incremental). The
server side is strictly read-only; the pull imports into your local store.

## Turn a session into git history

Debugged for hours with no commits? Replay the whole thing into a real git
repository — one commit per command:

```sh
chronx to-git /tmp/session.git
git -C /tmp/session.git log --stat     # every command, as a commit
git -C /tmp/session.git blame config.py
```

chronx synthesizes a `git fast-import` stream from its recorded deltas (a
"chronx baseline" commit for the starting tree, then one commit per command
with the command as the message). The result is ordinary git — browse it,
bisect it, or `git push` it to GitHub. Your chronx store is only read.

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
- The very first hooked command in a brand-new directory may have its changes
  folded into the baseline snapshot (the watch is only set up when chronx
  first sees the directory). `chronx exec` doesn't have this race — it waits
  for the watch before running.
- A file created *and* deleted within a single command is not captured: the
  recorder reads content at settle time (after the command), when the transient
  file no longer exists.

## Extending chronx (plugins)

Feature commands live as self-contained modules under `chronx/plugins/`, each
exposing `register(main)` and using the stable `chronx.pluginlib` helper
surface. They are auto-discovered from the filesystem and loaded defensively
(a broken plugin is skipped, never fatal), so dropping a new `.py` in that
directory adds a command with no reinstall. Most of the commands above
(`checkout`, `grep`, `annotate`, `find`, `audit`, `du`, `activity`, `summary`,
`format-patch`, `cherry-pick`) are built this way.
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
