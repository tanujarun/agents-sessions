# agent-sessions — spec

## Goal

One place that finds every saved AI coding-agent session on this machine, on
every mounted drive, lists them per provider, and resumes one in a terminal.
The interface is a full-screen terminal app (TUI) in the manner of btop or the
agent CLIs themselves. Function first; looks later.

Definition of "session": one resumable conversation saved on disk by an agent
CLI (a Claude Code transcript, a Codex rollout, a Grok session folder, an
Antigravity conversation, a Gemini CLI chat).

## Non-goals (this phase)

- Visual polish of the TUI beyond a clear, readable layout.
- Any change to the Omarchy shell or the SAO menu (`tanuj.menu`).
- Readers for opencode, cursor-agent, copilot, crush: installed, but no session
  exists on any drive to test against. `status` reports them as "no reader".
- Resuming a session in place inside a store on another drive.
- Cloud/web sessions (claude.ai, ChatGPT), desktop-app chat history.
- The Windows front end. It comes later; the core must not block it (see Portability).

## Stack

Python 3 (system is 3.14; code must run on 3.11+). The engine (everything but
`tui/`) is **standard library only** and runs on the system Python. The TUI
uses **Textual** (cross-platform, styleable, so the later look-and-feel work
and the Windows variant build on it), installed by `install.sh` into a
project-local `.venv`; nothing is installed system-wide. Tests with `unittest`
(TUI tests use Textual's headless `run_test()` pilot). No daemon.

## Facts already established (2026-10-12)

Mounted data filesystems: `/` + `/home` (btrfs), and four NTFS volumes under
`/run/media/tanuj/`: `22EEA841EEA80F5B` (the Windows system drive, `C:`),
`No Other Way`, `Folded`, `Zephyro`.

Stores, formats, counts, drive letters and resume commands are in
`docs/FORMATS.md` (task 2, accepted). Where this spec and FORMATS.md disagree
on a format detail, FORMATS.md wins. Summary: native stores `~/.claude`,
`~/.codex`, `~/.grok`, `~/.gemini/antigravity-cli`; foreign stores of all five
providers under `/run/media/tanuj/22EEA841EEA80F5B/Users/arunt/`; none on the
other three volumes today. Drive letters `C:` = `22EEA841EEA80F5B`, `D:` =
`No Other Way`, `F:` = `Zephyro`.

Provider capabilities that follow from FORMATS.md:

| Provider | List | Resume (native) | Import |
|---|---|---|---|
| Claude | yes | `claude --resume <id>` | copy-only |
| Codex | yes | `codex resume <id>` | copy + rewrite cwd in two record types |
| Grok | yes | `grok --resume <id>` | only when the mapped cwd is `[A-Za-z0-9/ ]`, else NotSupported |
| Antigravity | yes | `agy --conversation <id>` | NotSupported |
| Gemini | yes | not launchable: the real CLI is not installed | NotSupported |

Existing launch convention (bar launchers):
`cd <dir> && omarchy-launch-tui --app-id=org.omarchy.<tool> <tool> <args>` with
`<tool>` in `claude`, `codex`, `grok`, `agy`.

## Architecture

```
src/agent_sessions/
  model.py        Session, Store dataclasses; stable session key
  mounts.py       enumerate real mounted filesystems (platform layer)
  discover.py     find stores on the mounts; prune list; signatures
  pathmap.py      foreign path -> local path (drive letters, other homes)
  index.py        cache: stores + parsed sessions, incremental by mtime/size
  providers/      one adapter per provider + registry
  importer.py     copy a foreign session into the native store
  launch.py       build and run the resume command (platform layer)
  cli.py          argparse entry
  tui/            the Textual front end (app.py, view.py, app.tcss); the only
                  package that imports textual
bin/agent-sessions   thin launcher (symlinked to ~/.local/bin by install.sh)
share/               agent-sessions.desktop (installed by install.sh)
tests/               unittest; fixtures are sanitised real files
```

### Terms

- **Store**: one provider data root (e.g. a `.claude` dir with `projects/`).
- **Native store**: the store the installed Linux tool itself reads and writes
  (`~/.claude`, `~/.codex`, `~/.grok`, `~/.gemini/antigravity-cli`, `~/.gemini`).
- **Foreign store**: any other store, on any mount (including one under `/home`).

### Session model

`provider`, `id` (the tool's own id), `key` (`<provider>:<store-hash8>:<id>`,
stable across scans), `title`, `cwd` (as recorded), `cwd_local` (mapped path or
null), `cwd_exists`, `started`, `updated` (ISO 8601 UTC), `messages` (count or
null), `store` (root path), `origin` (`native` | `foreign`), `mount`
(mountpoint), `mount_label`, `files` (paths belonging to the session),
`launchable` (bool), `imported_as` (native key or null), `note` (why not
launchable, etc.).

Title rule: the tool's own title/summary when it has one, else the first real
user prompt, one line, whitespace collapsed, capped at 80 chars.

A **listed session** is a top-level conversation with at least one real user
prompt or one assistant message (FORMATS.md "Rules common to all providers").
Sub-agent transcripts, empty sessions, bookkeeping files and non-interactive
runs are not sessions and are skipped silently; `status` counts them.

`imported_as` is derived, not stored: a foreign session is "imported" when a
session with the same provider and id exists in the native store.
`imports.json` is provenance only.

### Provider adapter contract

```python
class Provider:
    name: str            # "claude"
    label: str           # "Claude"
    binary: str          # executable to resume with
    app_id: str          # omarchy-launch-tui --app-id value
    dir_names: set[str]  # directory names that may be a store root, for discovery
    def native_store(self) -> Path | None
    def is_store(self, path: Path) -> bool            # cheap signature check
    def list_sessions(self, store: Store) -> list[Session]
    def resume_argv(self, s: Session) -> list[str]    # for native sessions
    def import_session(self, s: Session, pathmap) -> Session   # may raise NotSupported(reason)
```

Adapters never raise on a malformed file: they skip it and record a warning
that `scan` reports. Adapter details per provider live in `docs/FORMATS.md`
(written by task 2); adapters implement what that file says and nothing guessed.

### Discovery (the "all mounted drives, consistently" requirement)

1. `mounts`: real filesystems only (skip proc/sys/tmpfs/overlay/squashfs/fuse
   portals, `/boot`, and btrfs subvolumes of an already-listed device that are
   not `/` or `/home`). Paths with spaces and escaped mountinfo (`\040`) handled.
2. Fast pass: check the known store locations (native stores, `<mount>/Users/*/`,
   `/home/*/`, including `.gemini/antigravity-cli` and `.gemini/antigravity-ide`
   under each, plus every store remembered in the index).
3. Deep pass: walk each mount completely with `os.scandir`, not following
   symlinks or crossing mounts, looking for directories whose name is in the
   union of `dir_names`, then confirming with `is_store`. Prune list (never
   descended): `Windows`, `Program Files*`, `ProgramData`, `$RECYCLE.BIN`,
   `System Volume Information`, `node_modules`, `.git`, `site-packages`,
   `SteamLibrary`, `XboxGames`, `_cacache`, `npm-cache`, `.cargo`, `.rustup`,
   `.pnpm-store`, `.npm`, `__pycache__`, `.venv`, `miniforge`, the path
   `AppData/Local/Temp` (never a bare `Temp`; never `AppData` or `Packages`), `/proc`, `/sys`, `/dev`, `/run` (except the
   mounts under `/run/media`), `/tmp`, `/var`, `/usr`, `/nix`, caches. The prune
   list is data, in one place. A confirmed store is not descended, with one
   exception: a `.gemini` directory is always descended (it holds the
   Antigravity stores), store or not. A Claude store whose path has a segment
   `local-agent-mode-sessions` (Claude Desktop history) is skipped and counted
   in `status`. Two stores that resolve to the same files (hard-linked
   mirrors) must not double-list: drop a session whose provider and id are
   already listed from a store of the same origin.
4. Unreadable directories are counted and reported, never fatal.
5. Results are deterministic: sorted, same input gives byte-identical `list --json`.

The deep pass is the slow part. It runs on `scan --deep`, when the set of
mounts changed since the last deep pass, or when the last one is older than
24 h (then in the background, detached). `scan` without `--deep` does steps
1-2 and re-parses only files whose mtime/size changed. A mount that vanished
keeps its sessions out of the listing but its stores stay remembered.

### Read-only rule

Nothing outside the native stores, `~/.cache/agent-sessions/` and
`~/.local/state/agent-sessions/` is ever written. **Every** SQLite file,
native ones too, is opened with `file:<urllib-quoted path>?mode=ro&immutable=1`:
plain `mode=ro` creates or rewrites `-shm`/`-wal` on these WAL databases.
Session files on other drives are opened with `os.O_NOATIME` where the
platform has it (fall back silently), so a scan does not update access times.

### Path map

Foreign cwd -> local path. Drive letters are resolved by evidence, not
hard-coded: the Windows system volume is the mount holding `Users/` and
`Windows/`; other letters are matched by testing which mount contains the
recorded path (case-insensitive segment match). Unresolved or ambiguous
letters stay unmapped (`cwd_local = null`), and `status` lists them. A manual
override file `~/.config/agent-sessions/pathmap.json` (`{"F:": "/run/media/…"}`)
wins over evidence. Normalise before matching: strip a `\\?\` prefix, accept
`/` and `\`, decode `file://` URIs (drop the slash before a drive letter),
compare the letter and every segment case-insensitively and return the
on-disk spelling, and apply the MSIX redirect for `AppData\Roaming\Claude`
(FORMATS.md "Drive letters"). A path whose folder is gone maps to `null`
with `cwd_exists = false`; never substitute a parent that looks close.

### Launch

Only for `origin == native`. Runs detached:
`cd <cwd> && omarchy-launch-tui --app-id=<app_id> <resume_argv>`. If the
recorded cwd no longer exists, behaviour follows FORMATS.md for that tool
(some tools scope resume to the cwd); when it cannot work, `open` fails with a
clear message and a desktop notification instead of opening the wrong thing.

### Import

Foreign -> native, explicit only. Copies the session's files into the native
store, rewriting recorded working directories through the path map where the
tool needs it to resume. Rules: never overwrite an existing native session;
idempotent (a second import reports the existing copy); source untouched;
provenance appended to `~/.local/state/agent-sessions/imports.json`; the copy
is written to a temp name and renamed into place. A provider whose format
cannot be imported safely raises `NotSupported` with the reason, and the TUI
shows that reason. After import the foreign row shows as imported and the
native copy is launchable.

### CLI contract

```
agent-sessions [tui]                           open the TUI (default when run with no subcommand on a terminal)
agent-sessions scan [--deep] [--json]          refresh index; print summary
agent-sessions list [--provider P] [--json]    sessions, newest first
agent-sessions open <key> [--dry-run]          native: resume. foreign: exit 1 with "import first". --dry-run prints the command
agent-sessions import <key> [--open]           import; optionally resume after
agent-sessions show <key>                      plain-text transcript to stdout
agent-sessions status                          mounts, stores, counts per provider,
                                               unmapped drive letters, tools with no reader, warnings
agent-sessions verify                          compare index against a brute-force walk; exit 1 on mismatch
```

Exit codes: 0 ok, 1 failure, 2 usage. The engine functions behind these
commands are importable; the TUI calls them directly, not through a subprocess.

## TUI (function-first)

Started by `agent-sessions` with no arguments, or from the desktop launcher
(`share/agent-sessions.desktop`, which runs
`omarchy-launch-tui --app-id=org.omarchy.sessions agent-sessions`).
`bin/agent-sessions` re-executes itself under `.venv/bin/python` for the TUI
when the venv exists; if Textual is missing it says how to install it and
exits 1. Every other subcommand runs on the system Python with no venv.

```
 agent-sessions                         5 drives · 63 sessions · scanned 2m ago
 1 Claude 41 │ 2 Codex 5 │ 3 Grok 22 │ 4 Antigravity 3 │ 5 Gemini 4 │ 0 All
────────────────────────────────────────────────────────────────────────────────
 TITLE                                   PROJECT          AGE   WHERE
▸Fix the first-open lag in the menu      tanuj            2d    Linux
 Portfolio landing page hero             Website Works    5d    Linux
 Pill content site copy pass             Pill Content     3w    C: (Windows)
 Lead generation scraper                 Lead Generation  2mo   C: · imported
 …
────────────────────────────────────────────────────────────────────────────────
 id       0c7e…                      messages 214     updated 2026-10-10 17:58
 cwd      F:\AI Projects\Website Works
 local    /run/media/tanuj/Zephyro/AI Projects/Website Works   (exists)
 store    /run/media/tanuj/22EEA841EEA80F5B/Users/arunt/.claude
────────────────────────────────────────────────────────────────────────────────
 ↑↓ move  ←→ provider  Enter resume  o resume, keep open  i import  v view
 / filter  r rescan  R full rescan  ? help  q quit
```

Regions, top to bottom: status line; provider tabs with counts (`0` = all
providers); column header; session list (scrolls, selection bar); detail pane
for the selected row; key hints. Below 80 columns the PROJECT column goes
first, then the detail pane; never crash on a small or resized terminal.

Rows: newest first, native and foreign mixed. WHERE is `Linux` for native
sessions, the drive (letter when known, else mount label) for foreign ones,
plus `· imported` once imported. Foreign rows are dimmed. Empty provider: one
centred line `No sessions found`.

Keys:

| Key | Action |
|---|---|
| `↑` `↓` `k` `j`, PgUp/PgDn, Home/End, `g`/`G` | move selection |
| `←` `→` `h` `l`, Tab/Shift-Tab, `1`-`5`, `0` | switch provider |
| Enter | native: resume in a new terminal window with the tool's own app-id, in the session's directory, then quit the TUI. Foreign: open the prompt below |
| `o` | same as Enter but the TUI stays open |
| `i` | foreign: import to Linux (asks `y/n` first); status line reports the result |
| `v` | view the transcript in a built-in scrolling pager (`q`/Esc returns) |
| `/` | filter the current list by substring over title, project and cwd; Esc clears |
| `r` | quick rescan (known stores); `R` full rescan of every drive |
| `?` | key help overlay |
| `q`, Ctrl-C | quit |

Foreign-row prompt (one line over the key hints):
`On <drive>: [i] import and resume  [I] import only  [v] view  [Esc] cancel`.
A foreign session already imported resumes its native copy on Enter.

Behaviour:

- Start shows the cached index at once, then runs a quick scan in a background
  thread and refreshes the list in place, keeping the selection on the same
  session key. A full rescan shows progress in the status line (`scanning
  Zephyro… 41k dirs`) and can be cancelled with Esc.
- No blocking work on the UI thread; scans, imports and transcript loads run
  in Textual workers.
- Errors (cwd gone, tool not installed, import refused) appear in the status
  line in plain words; the TUI never exits on an error.
- The terminal is always restored on exit, including on an exception.

The TUI holds no logic of its own: lists, filters, import and launch are
engine calls, so another front end can replace it. All colours, borders and
spacing live in `tui/app.tcss`, none in Python, so the look can be reworked
later without touching behaviour. Mouse works too: click a tab or row, wheel
scrolls, double-click resumes.

## Portability (Windows variant later)

Core modules (`model`, `discover`, `pathmap`, `index`, `providers`, `importer`,
`cli`) use `pathlib`/`os` only, no POSIX-only calls, no shelling out. Everything
platform-specific sits behind `mounts.py` and `launch.py`, each a small
interface with a Linux implementation. `tui/` is the only Textual user and no
engine module imports it. Textual runs on Windows unchanged. Path mapping is written direction-neutral
(foreign <-> local), so Windows can map `/home/tanuj/...` the other way.

## Definition of done (this phase)

1. `agent-sessions scan --deep` finds every store on every mounted drive;
   `verify` passes; two consecutive scans give identical `list --json`.
2. Counts match an independent brute-force count for each provider.
3. Every native session of a provider whose tool is installed (Claude, Codex,
   Grok, Antigravity) resumes from the TUI in its project directory.
4. A foreign Claude and a foreign Codex session import and then resume on
   Linux (a supervised live test by the user; FORMATS.md "Open questions");
   other providers import or state exactly why not.
5. Unmounting a drive, an unreadable folder or a corrupt session file does not
   break listing.
6. Unit tests pass; the TUI survives resize, an empty index and a missing drive.
