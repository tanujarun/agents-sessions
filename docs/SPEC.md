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
  api.py          the facade the CLI and the TUI call
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
The import journal (`imports.jsonl`) is provenance only.

### Provider adapter contract

```python
class Provider:
    name: str            # "claude"
    label: str           # "Claude"
    binary: str          # executable to resume with
    app_id: str          # omarchy-launch-tui --app-id value
    resumable: bool      # False when no usable binary exists (Gemini today)
    dir_names: set[str]  # directory names that may be a store root, for discovery
    id_pattern: re.Pattern   # the form of the tool's session ids, matched whole (a UUID for all five)
    def valid_id(self, session_id) -> bool                    # text that fullmatches id_pattern
    def native_store(self) -> Path | None
    def is_store(self, path: Path) -> bool                    # cheap signature check
    def candidates(self, store: Store) -> list[Candidate]     # cheap: names and stats only
    def parse(self, store: Store, c: Candidate) -> Session | None   # None = not a listed session
    def resume_argv(self, s: Session) -> list[str]            # for native sessions
    def transcript(self, s: Session) -> Iterator[Turn]       # (role, text, ts); user, assistant, tool-name turns
    def import_session(self, s: Session, pathmap) -> Session  # may raise NotSupported(reason)
    def read_back(self, plan) -> Session                      # the placed copy, parsed like any session
```

`id_pattern` is the one gate for an id: the index lists no session whose id
fails `valid_id`, `launch` resumes none and the importer copies none (an id
is text from a file on another drive, and it ends up in a command line and
in file names). `read_back` is how an import is verified: the copy in the
native store is parsed the way any session is, and must be the session that
was planned, made of everything that was put in place.

`Candidate` = `(id_hint, paths)`: `paths` are the files/dirs whose
`(path, mtime_ns, size)` decide whether a cached parse is still valid (for a
Codex thread: its rollout files and `session_index.jsonl`). The index calls
`candidates` on every scan and `parse` only for candidates whose signature
changed, so the live native Claude store (hundreds of MB) is not re-read each
time. `parse` fills the conversation fields (`id`, `title`, `cwd`, `started`,
`updated`, `messages`, `files`, `note`); the index fills the rest (`key`,
`store`, `origin`, `mount`, `mount_label`, `cwd_local`, `cwd_exists`,
`launchable`, `imported_as`).

Adapters never raise on a malformed file: `parse` skips it and records a
warning that `scan` reports. Adapter details per provider live in
`docs/FORMATS.md`; adapters implement what that file says and nothing guessed.

The CLI and the TUI both go through `api.py` (load index, scan, list, status,
verify, and later open / import / transcript); neither reaches into the
modules behind it.

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

The deep pass costs a few seconds warm (FORMATS.md "Walk cost"), more cold.
It runs on `scan --deep`, and `scan` chooses it by itself when the set of
mounts changed since the last deep pass or the last one is older than 24 h.
It runs inline (the TUI calls it from a worker and can cancel it); nothing is
detached. `scan` without a deep pass does steps 1-2 and re-parses only
candidates whose signature changed. A mount that vanished
keeps its sessions out of the listing but its stores stay remembered.

### Read-only rule

Nothing outside the native stores, `~/.cache/agent-sessions/` and
`~/.local/state/agent-sessions/` is ever written. **Every** SQLite file,
native ones too, is opened with `file:<urllib-quoted path>?mode=ro&immutable=1`:
plain `mode=ro` creates or rewrites `-shm`/`-wal` on these WAL databases.
Session files on other drives are opened with `os.O_NOATIME` where the
platform has it (fall back silently), so a scan does not update access times.

`fsutil.open_ro` reads regular files only and never waits: a named pipe, a
device, a socket or a directory under a session file's name raises
`NotRegularFile` before and after the open, and the open itself does not
block. A SQLite connection is hardened before any statement reads the file
(`query_only`, `trusted_schema` off, `cell_size_check` on, defensive mode
where the Python can set it), and a database whose schema holds a view, a
trigger or a virtual table is refused.

### Path map

Foreign cwd -> local path. Drive letters are resolved by evidence, not
hard-coded: the Windows system volume is the mount holding `Users/` and
`Windows/`; other letters are matched by testing which mount contains the
recorded path (case-insensitive segment match). Letters are
resolved per source mount (the mount the foreign store is on), since two
Windows installs can assign letters differently. Unresolved or ambiguous
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
provenance appended to `~/.local/state/agent-sessions/imports.jsonl` (see
phase 2, "Import"); the copy is written to a temp name and moved into place.
The order is: stage every file, place the units, verify (`read_back`), append
(a line added to a file the tool owns), record. Every destination must be
contained: a path below the native store whose components are checked one by
one (`importer.contained`), whatever the adapter that planned it did. On any
failure before the record, what was placed is taken out again. A provider whose format
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

Text from a store is never printed as it is. Readable output replaces every
control character and every character that reorders text (the C0 and C1
controls, DEL, the bidirectional controls, U+2028, U+2029, U+FEFF) with a
space, one for one; `--json` output stays exact data and writes the same
characters as `\uXXXX` escapes (`textsafe`).

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

---

# Phase 2: production pass (2026-10-12)

Phase 1 above stays the base. Where this part says otherwise, this part wins.
Evidence for every format statement is in `docs/FORMATS.md` "Phase 2 findings";
the code review that drives the hardening is `docs/REVIEW.md`.

## Goals

1. Close the review findings (security against hostile drive contents, crash
   and concurrency safety).
2. Remove the limits of phase 1: sessions of an unmounted drive disappear;
   a session whose folder is gone cannot be imported; Grok paths with
   punctuation are refused; Antigravity cannot be imported; a session of one
   tool cannot be carried on in another; `D:` stays unresolved.
3. Make the project maintainable by someone else: packaging, enforced lint
   and types, CI, logs, documented output formats, user documentation.

## Non-goals

- Readers for opencode, cursor-agent, copilot, crush, and a working Gemini
  resume: no session data exists on any drive and three of the tools are not
  installed (FORMATS "Q6"). A reader is never written from guesses.
- Writing a store on another drive. The Windows front end.
- The visual redesign of the TUI.

## Threat model

Every byte of a **foreign store** (any store that is not the native one) is
hostile: file names, file types, sizes, ids, titles, paths, JSON, SQLite,
protobuf. The native stores, the cache, the state directory and the config
directory belong to the user: trusted, but possibly malformed. Rules that
follow, each enforced in one place and again at the point of use:

- A session id must match its provider's id grammar (`Provider.id_pattern`,
  a UUID for all five) to be listed, launched or imported.
- Nothing from a foreign store reaches a command line except a validated id
  and a directory that exists. No shell is ever used.
- Every import destination is a path below the native store (checked on
  its components, independently of the adapter). Symbolic links inside the
  native store are followed: the store is the user's own, and nothing from
  a foreign store can create a link there (an import writes regular files
  and directories only).
- Everything printed for a person passes `textsafe.clean` (control, C1 and
  bidi characters). `--json` output is exact data with those characters
  escaped.
- Reads never block and never allocate without a bound: regular files only,
  capped line and file sizes, a warning for what is skipped.
- What we keep holds titles, prompts and paths, and is for the user alone:
  the cache index and every file in the state directory (journal, log, lock)
  are created 0600, the directories made for them 0700, and a cache file
  with a wider mode is tightened when it is rewritten.

## Tool detection

On this machine every agent command in `~/.local/bin` except `agy` is a
wrapper that **installs the tool when it is missing** (`mise use -g … ;
exec mise x <spec> -- <tool>`). `shutil.which` therefore proves nothing.
`launch.tool_state(binary)` (platform layer) answers `installed`,
`not installed (the command on PATH would install it)` or `not found`, from
files only: a wrapper is recognised by its text, its `mise x "<spec>"` spec
is mapped to mise's install directory, and the tool counts as installed when
an executable of that name exists there. The answer feeds `launchable`,
`status`, `launch.plan` (which refuses to start a wrapper of a tool that is
not installed, so the browser never installs software) and Gemini's
`resumable`, which becomes "the real CLI is installed" instead of a constant.

Terminal: `omarchy-launch-tui` when present; else `xdg-terminal-exec`; else
`$TERMINAL -e`; else the first of a table of known terminals. The override
`AGENT_SESSIONS_TERMINAL` (a command; the tool's argv is appended) wins.

## Offline drives

- `Mount` gains `uuid` and `fs_label`, read from `/dev/disk/by-uuid` and
  `/dev/disk/by-label` (FORMATS "Q4"); the override env accepts them too.
- The cache keeps a `volumes` table: every mount path that has a remembered
  store, with label, UUID, filesystem, drive letters as last resolved, and
  when it was last seen. Cache version 2; a version 1 cache is migrated.
- Sessions of a remembered store on a volume that is not mounted are
  **listed**, from the cached parse, with `offline: true`, no `cwd_local`,
  not launchable, note `drive not mounted`. WHERE shows `C: · offline`.
  They are ordered and deduplicated like any other session, and an offline
  session that was imported still resumes its native copy.
- Reading a transcript, importing and continuing need the files: they fail
  with `the drive <label> is not mounted`.
- `mounts.volume_state(uuid)`: `mounted`, `attached` (the device is there),
  `absent`. `mounts.mount_volume(uuid)` runs `udisksctl mount --block-device
  /dev/disk/by-uuid/<uuid>` with no terminal attached (the desktop's polkit
  agent asks for the password; these internal volumes need one), waits up to
  120 s, and returns the mount point read back from the mount table. Failure
  gives the tool's own message. `subprocess` is allowed in `mounts.py` for
  this one call.
- A volume that comes back at another mount path: its remembered stores at
  the old path are dropped (the deep pass that a changed mount set triggers
  finds them at the new one).
- CLI: `mount <drive>` (label, letter, mount path or UUID), then a scan;
  `forget <drive>` drops a volume that is not mounted from the cache.
- `verify` compares mounted volumes only.

## Drive letters

- Adapters may add evidence: `Provider.path_evidence(store) -> list[str]`.
  Claude returns the keys of the `projects` object of the `.claude.json`
  beside the store, which resolves `D:` here (FORMATS "Q7").
- `map` prints the letters; `map D: <dir>` and `map --remove D:` edit
  `~/.config/agent-sessions/pathmap.json` (written atomically, validated).

## Import

- `import <key> [--to DIR] [--experimental] [--open] [--dry-run]`.
- **A chosen folder** (`--to`, absolute, must exist): replaces the mapped
  folder, and is the only way to import a session whose folder is gone or
  not recorded. Per provider (FORMATS "Q8"): Claude changes the destination
  directory only; Codex rewrites the same four cwd fields (a value equal to
  the recorded cwd becomes the folder, a value below it the same path below
  the folder, anything else goes through the path map); Grok uses the group
  name and `info.cwd` of the folder's real path. The record keeps both the
  recorded cwd and the chosen folder.
- **Grok**: the group directory is `quote(cwd, safe="")`; over 255 bytes it
  is `<slug>-<16 hex of BLAKE3(cwd)>` (FORMATS "Q1"). BLAKE3 is implemented
  for inputs up to 1,024 bytes (one chunk) and checked against the published
  test vectors; a longer path is refused. The old `[A-Za-z0-9/ ]` limit goes.
- **Antigravity**: copy-only, exactly as FORMATS "Q5" specifies, and only
  with `--experimental` (the TUI asks a second time and says why): one
  resume by hand has not been done yet, and the tool starts a new
  conversation without an error when it cannot read a copy. No folder is
  needed.
- **Gemini**: still refused (no CLI, no resumable data).
- The journal is `imports.jsonl` in the state directory: one JSON object per
  line, appended under a lock; a record lists every file and directory the
  import created with size and modification time. An old `imports.json` is
  read, never rewritten.
- `unimport <key> [--force] [--dry-run]`: removes what the last import of
  that session created, and nothing else. Refused when any of those files
  differs from the record (the session was used since) unless `--force`;
  directories are removed only when empty; a line appended to a file the
  tool owns (Codex `session_index.jsonl`) is left and reported; extra files
  a tool creates beside an imported conversation (Antigravity `-wal`,
  `-shm`, `presence/<id>.lock`) are removed only with `--force`. The undo is
  itself a journal record.
- `api.import_options(session)` tells a front end what is possible:
  `possible`, `reason`, `needs_folder`, `experimental`, `warning`.

## Continue as a new session

For a session that cannot be resumed where it is wanted (another provider's
tool, an unimportable provider, a folder that is gone):

- `export <key> [-o FILE] [--format md|json]`: the transcript as Markdown
  (a header with title, tool, id, folder, dates; then `## User` /
  `## Assistant` sections and one line per tool call) or as JSON lines.
- `continue <key> [--with TOOL] [--in DIR] [--dry-run] [--here]`: writes the
  Markdown export to `<state>/handoff/<provider>-<id>.md` (mode 0600,
  replaced atomically; files older than 30 days are swept), then starts
  `TOOL` (default: the session's own tool) as a **new** interactive session
  in `DIR` (default: the session's folder when it exists; otherwise `--in`
  is required) with a fixed first prompt that names the file and says it is
  a record, not instructions. `Provider.new_session_argv(prompt)` gives the
  command (FORMATS "Q3": `claude <p>`, `codex <p>`, `grok <p>`,
  `agy --prompt-interactive <p>`, `gemini -i <p>`). The prompt holds no text
  from the session; the path is built from the validated id.
- The first prompt is sent as soon as the tool starts: a model call the user
  asked for. The CLI says so in `--dry-run`, the TUI before it starts.

## CLI additions

```
agent-sessions --version | --debug
agent-sessions mount <drive>            agent-sessions forget <drive>
agent-sessions map [LETTER: DIR | --remove LETTER:]
agent-sessions import <key> [--to DIR] [--experimental] [--open] [--dry-run]
agent-sessions unimport <key> [--force] [--dry-run]
agent-sessions export <key> [-o FILE] [--format md|json]
agent-sessions continue <key> [--with TOOL] [--in DIR] [--dry-run] [--here]
```

`list` gains `--mounted` (leave offline sessions out). Exit codes: 0 ok,
1 failure, 2 usage, 130 interrupted (Ctrl-C; SIGTERM and SIGHUP end a
command the same way, with 128 + the signal: 143, 129). An interrupt prints
one line and no traceback, and an import that was under way is rolled back
first. An exception nobody planned for prints `internal error: <Type>:
<message> (details in <log>)` and exits 1; `--debug` shows the traceback.
Every `--json` shape is documented in `docs/JSON.md` and pinned by a test.

## Logs

`<state>/agent-sessions.log`, rotating (1 MB, 3 files), mode 0600: scans
(summary), imports and undos (plan and result), launches (the command),
mounts, warnings, and every unexpected exception with its traceback. A
failure the user sees names the log. `--debug` also logs to stderr.

## TUI additions

- Offline rows: dimmed like foreign rows, WHERE `C: · offline`; the status
  line counts them (`135 sessions · 29 offline`).
- Enter on an offline row that has no native copy: prompt
  `<drive> is not mounted: [m] mount  [Esc] cancel`. `m` anywhere on such a
  row mounts; the status line shows `waiting for the password prompt…`,
  then the result, then a scan.
- The foreign-row prompt is built from `import_options`:
  `[i] import and resume  [I] import only  [t] import into another folder
  [c] continue as new  [v] view  [Esc] cancel`, leaving out what is not
  possible and showing the reason for it on the line above. An experimental
  import asks again with its warning.
- `t` and `c` open a one-line input for a folder: `~` expands, Tab completes
  directory names, Enter accepts only an existing directory, Esc cancels.
- `c` (any row that is not offline): choose the tool with `←` `→` among the
  installed ones (the session's own first), then the folder; the last line
  says `starts <Tool> and sends a first message`. Enter starts it.
- `u` on a row with an import record: `Remove the imported copy? y/n`; a
  refusal shows its reason.
- `?` lists all of it. Styling stays in `app.tcss`; the look is unchanged.

## Packaging

`pip install .[tui]` gives a working `agent-sessions` (the entry point
starts the TUI itself; `cli.py` may import `agent_sessions.tui` lazily for
that and nothing else may). `install.sh` keeps the checkout flow;
`uninstall.sh` removes what it installed. `README.md`, `CHANGELOG.md`,
`docs/JSON.md`, `docs/SECURITY.md` (threat model and what is and is not
defended). Version 0.2.0.

## Definition of done (phase 2)

1. `make check` is clean on Python 3.11 and 3.14; coverage of the engine is
   not lower than before.
2. Every review finding has a regression test that fails on the old code.
3. The real machine still lists the same 135 sessions with `verify` passing;
   with the Windows volume unmounted the 29 sessions stay listed as offline.
4. In a throwaway home: every importable foreign session imports and undoes
   to a byte-identical store; the Grok import is confirmed by `grok export`
   in the network-less sandbox; the five sessions without a folder import
   with `--to`.
5. No test and no worker starts an agent tool interactively, sends anything
   to a model, mounts or unmounts a drive, or writes a real store.
6. Still owed by the user, listed in the README: one real resume, one real
   import and resume per provider, one mount from the browser, one
   "continue as new", one Antigravity experimental import.
