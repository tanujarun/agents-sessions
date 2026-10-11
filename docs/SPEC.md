# agent-sessions — spec

## Goal

One place that finds every saved AI coding-agent session on this machine, on
every mounted drive, lists them per provider, and resumes one in a terminal.
Entry point is the Omarchy SAO menu (`tanuj.menu`). Function first; looks later.

Definition of "session": one resumable conversation saved on disk by an agent
CLI (a Claude Code transcript, a Codex rollout, a Grok session folder, an
Antigravity conversation, a Gemini CLI chat).

## Non-goals (this phase)

- Visual design of the menu rows beyond readable text.
- Readers for opencode, cursor-agent, copilot, crush: installed, but no session
  exists on any drive to test against. `status` reports them as "no reader".
- Resuming a session in place inside a store on another drive.
- Cloud/web sessions (claude.ai, ChatGPT), desktop-app chat history.
- The Windows front end. It comes later; the core must not block it (see Portability).

## Stack

Python 3 (system is 3.14; code must run on 3.11+), **standard library only**.
Tests with `unittest`. Menu glue is QML/JS inside the existing `tanuj.menu`
plugin plus one JSONC entry. No daemon.

## Facts already established (2026-10-12)

Mounted data filesystems: `/` + `/home` (btrfs), and four NTFS volumes under
`/run/media/tanuj/`: `22EEA841EEA80F5B` (the Windows system drive, `C:`),
`No Other Way`, `Folded`, `Zephyro`.

Stores found so far:

| Provider | Linux store (native) | Other drives |
|---|---|---|
| Claude Code | `~/.claude/projects/<enc-cwd>/<uuid>.jsonl` (4 project dirs) | `…/Users/arunt/.claude/projects` (13 project dirs, 37 transcripts, Windows cwd such as `F:\AI Projects\Website Works`) |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (3), names in `~/.codex/session_index.jsonl` | `…/Users/arunt/.codex/sessions` (2) |
| Grok | `~/.grok/sessions/<urlencoded-cwd>/<id>/` (~20) | `…/Users/arunt/.grok/sessions/F%3A%5CAI%20Projects%5CWebsite%20Works/…` |
| Antigravity | `~/.gemini/antigravity-cli/conversations/<uuid>.db` (+ `brain/`, `conversation_summaries.db`) | `…/Users/arunt/.gemini/antigravity-cli`, `…/antigravity-ide` |
| Gemini CLI | none yet | `…/Users/arunt/.gemini/tmp/<project>/…`, `…/.gemini/history` |

Project-level `.claude` folders exist on several drives (settings, not
sessions). A copied profile may sit anywhere (for example
`Zephyro/AI Projects/Website Works/Archive/07 Alt device snapshot/Alt Device Files/.claude`),
so discovery must not assume profile locations only.

Existing launch convention (bar launchers):
`cd <dir> && omarchy-launch-tui --app-id=org.omarchy.<tool> <tool> <args>` with
`<tool>` in `claude`, `codex`, `grok`, `agy`. Resume flags per tool are **not
yet verified**; task 2 verifies them from each tool's `--help`.

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
bin/agent-sessions   thin launcher (symlinked to ~/.local/bin by install.sh)
menu/                the Menu.qml provider snippet + JSONC rows + apply script
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
   `/home/*/`, plus every store remembered in the index).
3. Deep pass: walk each mount completely with `os.scandir`, not following
   symlinks or crossing mounts, looking for directories whose name is in the
   union of `dir_names`, then confirming with `is_store`. Prune list (never
   descended): `Windows`, `Program Files*`, `ProgramData`, `$RECYCLE.BIN`,
   `System Volume Information`, `node_modules`, `.git`, `site-packages`,
   `SteamLibrary`, `XboxGames`, `/proc`, `/sys`, `/dev`, `/run` (except the
   mounts under `/run/media`), `/tmp`, `/var`, `/usr`, `/nix`, caches. The prune
   list is data, in one place. A store is never searched for nested stores.
4. Unreadable directories are counted and reported, never fatal.
5. Results are deterministic: sorted, same input gives byte-identical `list --json`.

The deep pass is the slow part. It runs on `scan --deep`, when the set of
mounts changed since the last deep pass, or when the last one is older than
24 h (then in the background, detached). `scan` without `--deep` does steps
1-2 and re-parses only files whose mtime/size changed. A mount that vanished
keeps its sessions out of the listing but its stores stay remembered.

### Read-only rule

Nothing outside the native stores, `~/.cache/agent-sessions/` and
`~/.local/state/agent-sessions/` is ever written. SQLite files in foreign
stores are opened with `file:…?mode=ro&immutable=1` so no `-wal`/`-shm` is
created on the drive. Native SQLite files are opened `mode=ro` too.

### Path map

Foreign cwd -> local path. Drive letters are resolved by evidence, not
hard-coded: the Windows system volume is the mount holding `Users/` and
`Windows/`; other letters are matched by testing which mount contains the
recorded path (case-insensitive segment match). Unresolved or ambiguous
letters stay unmapped (`cwd_local = null`), and `status` lists them. A manual
override file `~/.config/agent-sessions/pathmap.json` (`{"F:": "/run/media/…"}`)
wins over evidence.

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
cannot be imported safely raises `NotSupported` with the reason, and the menu
shows that reason. After import the foreign row shows as imported and the
native copy is launchable.

### CLI contract

```
agent-sessions scan [--deep] [--json]          refresh index; print summary
agent-sessions list [--provider P] [--json]    sessions, newest first
agent-sessions menu <provider>                 TSV rows for the menu (below)
agent-sessions open <key> [--dry-run]          native: resume. foreign: chooser. --dry-run prints the command
agent-sessions import <key> [--open]           import; optionally resume after
agent-sessions show <key>                      plain-text transcript to stdout
agent-sessions status                          mounts, stores, counts per provider,
                                               unmapped drive letters, tools with no reader, warnings
agent-sessions verify                          compare index against a brute-force walk; exit 1 on mismatch
```

`menu <provider>` prints one row per session, tab separated:
`label \t key \t "" \t description \t icon`. It must return in under 300 ms
from the cache and start a background `scan` when the cache is stale. Exit
codes: 0 ok, 1 failure, 2 usage.

## Menu surface (function-first)

```
Menu root
└─ Sessions                      (extensions/omarchy-menu.jsonc: "sessions")
   ├─ Claude        ▸  provider "sessions-claude"
   ├─ Codex         ▸  provider "sessions-codex"
   ├─ Grok          ▸  provider "sessions-grok"
   ├─ Antigravity   ▸  provider "sessions-antigravity"
   ├─ Gemini        ▸  provider "sessions-gemini"
   └─ Rescan drives     action: agent-sessions scan --deep (notifies when done)
```

Row inside a provider: label `<title>  ·  <project folder>  ·  <age>`;
description (shown while searching) `<full cwd>  ·  <drive label or "Linux">`.
Native rows use the provider glyph; foreign rows use a drive glyph and end with
`  ·  <drive label>`; imported foreign rows end with `  ·  imported`.
Order: newest first, native and foreign mixed. Empty provider: one inert row
`No sessions found`.

Activating a native row resumes it. Activating a foreign row opens a chooser
(Omarchy's own picker) with: `Import to Linux and resume`, `Import to Linux`,
`View transcript`. `View transcript` opens `agent-sessions show` in a terminal
pager.

Menu.qml change is limited to: the five provider specs, and letting provider
rows carry the optional 4th (description) and 5th (icon) TSV columns. Existing
providers must behave exactly as before.

## Portability (Windows variant later)

Core modules (`model`, `discover`, `pathmap`, `index`, `providers`, `importer`,
`cli`) use `pathlib`/`os` only, no POSIX-only calls, no shelling out. Everything
platform-specific sits behind `mounts.py` and `launch.py`, each a small
interface with a Linux implementation. Path mapping is written direction-neutral
(foreign <-> local), so Windows can map `/home/tanuj/...` the other way.

## Definition of done (this phase)

1. `agent-sessions scan --deep` finds every store on every mounted drive;
   `verify` passes; two consecutive scans give identical `list --json`.
2. Counts match an independent brute-force count for each provider.
3. Every native session of the five providers resumes from the menu in its
   project directory.
4. A foreign Claude and a foreign Codex session import and then resume on
   Linux; other providers import or state exactly why not.
5. Unmounting a drive, an unreadable folder or a corrupt session file does not
   break listing.
6. Unit tests pass; the menu's existing entries work as before.
