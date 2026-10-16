# agent-sessions

agent-sessions finds the sessions that AI coding agents (Claude Code, Codex, Grok,
Antigravity, Gemini CLI) saved on this machine and on every mounted drive, and lists them
in one place. From the list a session is resumed in a terminal with its own tool, copied
from another drive (for example a Windows partition) into this system's store so that it
can be resumed here, or carried on as a new session of another tool. It is a full-screen
terminal program (a TUI) and a command line over the same engine; the engine uses the
Python standard library only.

```
 agent-sessions                                2 drives · 9 sessions · 1 offline · scanned just now
 1 Claude 4  2 Codex 2  3 Grok 2  4 Antigravity 1  5 Gemini 0  0 All 9
────────────────────────────────────────────────────────────────────────────────────────────────────
 TITLE                                                   PROVIDER     PROJECT    AGE   WHERE
 Fix the first-open lag in the menu                      Claude       project-0  1h    Linux
 Portfolio landing page hero                             Claude       project-1  3d    Linux
 Rework the importer's roll back                         Codex        project-2  5d    Linux
 Pill content site copy pass                             Claude       project-3  9d    C:
 Benchmark the directory walk                            Grok         project-4  12d   Linux
 Lead generation scraper                                 Codex        project-5  2w    C:
 Invoice parser                                          Claude       project-6  4w    C: · offline
 Sketch the settings page                                Antigravity  project-7  5w    C:
 Why is the fan curve ignored                            Grok         project-8  2mo   Linux

────────────────────────────────────────────────────────────────────────────────────────────────────
 id       clf-0003                                       messages 12    updated  2026-10-03 12:00
 cwd      C:\Work\project-3
 local    /run/media/user/WINVOL/Work/project-3   (exists)
 store    /run/media/user/WINVOL/Users/someone/.claude
 note     -
 import   possible
────────────────────────────────────────────────────────────────────────────────────────────────────
 ↑↓ move  ←→ provider  Enter resume  o resume, keep open  i import  t import into folder  v view
 c continue  u undo import  m mount  / filter  r rescan  R full rescan  ? help  q quit
```

The screen above is the real program at 100 by 24 characters, drawn by Textual's headless
test driver with nine made-up sessions written for this page; they are not in the
repository, and no test pins the picture. The fourth row is the selected one; the selection
bar and the dimming of rows from other drives are colours and do not show in text.

## What it supports

| Provider | List | Resume | Import from another drive | Continue as a new session |
|---|---|---|---|---|
| Claude Code | yes | `claude --resume <id>` | yes: a plain copy | yes |
| Codex | yes | `codex resume <id>` | yes: a copy with the recorded folders rewritten | yes |
| Grok | yes | `grok --resume <id>` | yes: a copy, filed under the folder's name | yes |
| Antigravity (`agy`) | yes | `agy --conversation <id>` | only with `--experimental`: a plain copy | yes |
| Gemini CLI | yes | only where the real `gemini` program is installed | no | from it: yes; with it: where `gemini` is installed |

"Resume" is for a session in this system's own store. "Import" copies a session of a store
on another drive into that store. "Continue" starts a new session with a first message
that points the tool at an exported transcript of the old one. None of the resume, import
and continue paths has been run against a real tool yet: see
[What has not been tried on a real tool](#what-has-not-been-tried-on-a-real-tool).

Not supported, and why:

- **opencode, cursor-agent, copilot, crush.** There is no reader for them. No session of
  any of them existed on any drive to write one from, and a reader is not written from
  guesses. `status` lists the ones that are installed under "installed tools with no
  reader". [docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) says how one is added.
- **Importing a Gemini chat.** The real Gemini CLI was not installed on the machine this
  was built on, and the one chat found had no resumable content.
- **Antigravity IDE conversations** (a store named `antigravity-ide`). The only such store
  found was empty, so its conversation format was never seen. One would be listed with the
  note "list only" and is not resumed or imported.
- **Resuming a session in place on another drive.** Nothing is ever written to a store
  that is not this system's own; a tool that resumed there would write to it.
- **Web and desktop-app history** (claude.ai, ChatGPT, Claude Desktop). Only what the
  command-line tools saved is read.
- **Windows and macOS.** The engine is written to be portable, but the two platform
  modules (`mounts.py`, `launch.py`) have a Linux implementation only.

## Requirements

- Linux.
- Python 3.11, 3.12, 3.13 or 3.14. Every command but the TUI needs nothing else. The
  checkout's launcher, run by an older Python, says `agent-sessions needs Python 3.11 or
  newer` and exits 1.
- The TUI needs [Textual](https://textual.textualize.io/) 8.2.8 (pinned in
  `requirements-tui.txt`). `install.sh` puts it into a `.venv` inside the checkout;
  nothing is installed system-wide.
- Optional, each used only when it is there: a terminal emulator to open a session in
  (see `AGENT_SESSIONS_TERMINAL` below); `udisksctl` for `mount`; `notify-send` for a
  desktop notification when a session cannot be resumed; the agent tools themselves, to
  resume or continue with.

## Install

From a checkout (what the author uses):

```sh
git clone <this repository> agent-sessions
cd agent-sessions
./install.sh
```

`install.sh` creates `.venv` in the checkout with the pinned Textual, links
`~/.local/bin/agent-sessions` to `bin/agent-sessions`, and copies the desktop launcher to
`~/.local/share/applications/agent-sessions.desktop` (`$XDG_DATA_HOME/applications` when that
variable is an absolute path; a relative one is ignored with a warning, as the XDG
specification says; `HOME` must be an absolute path). Besides those three, the tools it runs
write two things: pip keeps its download cache (`~/.cache/pip`, unless pip is configured
otherwise), and `update-desktop-database`, where it exists, rewrites `mimeinfo.cache` in the
applications directory. It can be run again at any time.

It does not take over what is not its own. A link at `~/.local/bin/agent-sessions` that
points to another program is left, and the script stops before it makes anything; a file
there that is not a link is never replaced. A desktop launcher you edited (another terminal
in its `Exec` line, say) is kept, and the script says so; one an earlier version installed
is replaced. `./install.sh --force` replaces both and says what it replaced.

It stops with one line when `python3` is older than 3.11 or has no `venv` module with pip
(`PYTHON=/path/to/python ./install.sh` uses another interpreter), and it warns when
`~/.local/bin` is not on `PATH`. `./install.sh --dev` also installs the development tools.

The desktop launcher starts the TUI with `omarchy-launch-tui`, a program of the Omarchy
desktop. On another desktop the launcher does nothing useful (`install.sh` says so); start
`agent-sessions` from a terminal there.

With pip, into an environment of your choice (the project is not published on a package
index; install it from a checkout or from a wheel built from one):

```sh
python3 -m venv ~/.venvs/agent-sessions
~/.venvs/agent-sessions/bin/pip install '/path/to/agent-sessions[tui]'
~/.venvs/agent-sessions/bin/agent-sessions --version
```

Without `[tui]` every command works except the TUI, which then says how to get Textual.

## Uninstall

```sh
./uninstall.sh            # the link in ~/.local/bin and the desktop launcher
./uninstall.sh --purge    # also the program's own cache, state and configuration
```

`uninstall.sh` removes `~/.local/bin/agent-sessions` only when it is a link into this
checkout, and the desktop launcher only when it is the file this checkout installs (or one
an earlier version installed, byte for byte). `--purge` asks first (`--yes` skips the
question; without a terminal and without `--yes` it removes nothing) and removes
`~/.cache/agent-sessions`, `~/.local/state/agent-sessions` and `~/.config/agent-sessions`.
When `AGENT_SESSIONS_STATE` or `AGENT_SESSIONS_CACHE` is set, it also takes the program's own
files out of the directory each names, by name and nothing else (the log and its rotations,
the lock files, the journal, the exported transcripts; the index, an index kept aside as
`.unusable`, the temp files of a save that was cut short), and removes that directory only
when nothing else is left in it. It never touches a session
store, so imported sessions stay where they are; undo them with `agent-sessions unimport`
before purging if you want them gone, because the journal that `unimport` reads is in the
state directory. The checkout and its `.venv` are left; delete the directory to remove them.

For a pip installation: `pip uninstall agent-sessions`.

## Quick start

```sh
agent-sessions scan              # find the stores and read the sessions
agent-sessions list              # every session, newest first
agent-sessions                   # the same list as a full-screen browser
```

The first `scan` walks every mounted drive, which takes a few seconds on a warm cache
and longer on a cold one. Later scans look at the known stores only and read just the
sessions that changed; a full walk is made again when the set of mounted drives changed or
the last one is older than a day.

In the browser: arrow keys to move, Enter to resume the selected session in a new terminal
window, `v` to read it, `?` for every key, `q` to quit. `q` leaves at once, also during a
scan or a mount (the mount is cancelled); an import or an undo under way is finished first,
and the status line says so. `agent-sessions tui` needs a terminal on stdin, stdout and
stderr: without one it prints one line and exits 2.

## The TUI keys

| Key | What it does |
|---|---|
| `↑` `↓` `k` `j`, PgUp, PgDn, Home, End, `g`, `G` | move the selection |
| `←` `→` `h` `l`, Tab, Shift-Tab, `1` to `5`, `0` | switch provider (`0` is all of them) |
| Enter | a session of this system: resume it in a new terminal window and quit. A session of another drive: a prompt with what can be done with it. A session whose drive is not mounted: offer to mount the drive |
| `o` | the same as Enter, and the browser stays open |
| `i` | import the selected session of another drive (asks first) |
| `t` | import it into a folder that you type (Tab completes directory names) |
| `c` | continue the session as a new session: choose the tool, the folder, then confirm |
| `u` | undo the import of the selected session (asks, with what would be removed) |
| `m` | mount the drive of the selected session |
| `v` | read the transcript in a pager (`q` or Esc returns) |
| `/` | filter the list by title, project and folder; Esc clears the filter |
| `r`, `R` | rescan the known stores; rescan every drive completely (Esc cancels) |
| `?` | the key help |
| `q`, Ctrl-C | quit |

The mouse works too: click a tab or a row, scroll with the wheel, double-click to resume.

## The command line by task

A session is named by its **key** (the last column of `agent-sessions list --keys`), by its
id, or by a start of its key that only one session has. A key is
`<provider>:<store>:<id>`, and all the sessions of one store share `<provider>:<store>`, so a
start has to reach into the id: `claude:1a2b3c4d` alone names every session of that store and
is refused as ambiguous, `claude:1a2b3c4d:7f3e` names one. The examples use such starts; put
a key of your own listing in their place. Every command has a `--help` with its options and
more examples. `scan`, `list` and `status` take `--json`; the shapes are in
[docs/JSON.md](docs/JSON.md).

**Look around**

```sh
agent-sessions list --provider claude --keys   # one provider, with the keys
agent-sessions list --mounted                  # without sessions of drives that are not mounted
agent-sessions show claude:1a2b3c4d:7f3e       # what is known about a session, and its transcript
agent-sessions status                          # drives, stores, counts, tools, drive letters, warnings
agent-sessions tui                             # the browser (also what no command does on a terminal)
```

**Resume a session of this system**

```sh
agent-sessions open claude:1a2b3c4d:7f3e --dry-run  # print the folder and the command, start nothing
agent-sessions open claude:1a2b3c4d:7f3e            # in a new terminal window
agent-sessions open claude:1a2b3c4d:7f3e --here     # in this terminal
```

**Bring a session over from another drive, and take it back**

```sh
agent-sessions import claude:1a2b3c4d:7f3e --dry-run   # what would be copied and rewritten
agent-sessions import claude:1a2b3c4d:7f3e --open      # import, then resume the copy
agent-sessions import codex:5e6f7a8b:0199 --to ~/Work/site    # its own folder is gone: use this one
agent-sessions import antigravity:9c0d1e2f:3b8a --experimental
agent-sessions unimport claude:1a2b3c4d:7f3e --dry-run # what an undo would remove and leave
agent-sessions unimport claude:1a2b3c4d:7f3e           # asks, then removes the copy
```

**Carry a session on somewhere else**

```sh
agent-sessions export claude:1a2b3c4d:7f3e -o ~/notes/session.md   # the transcript as Markdown
agent-sessions export claude:1a2b3c4d:7f3e --format json           # one JSON object per turn
agent-sessions continue gemini:2c4e6a8b:5d1f --with claude --dry-run
agent-sessions continue codex:5e6f7a8b:0199 --in ~/Work/site       # its own folder is gone
```

`continue` starts the tool with a first message, and the tool sends that message to its
model at once. `--dry-run` shows the folder, the exact command and the transcript file,
and starts nothing.

`export -o FILE` writes the file for you alone (0600) and never over an existing file
without `--force`. It is refused, forced or not, inside a session store (any store the
index knows, any tool's own store on this system, and a store on a drive mounted since the
last scan) and inside the program's own cache, state or configuration directory; `-o ''`
is a usage error.

**Drives**

```sh
agent-sessions mount C:                  # mount a remembered drive (the desktop asks for the password), then scan
agent-sessions forget Zephyro            # drop a drive that is not mounted, and its sessions, from the index
agent-sessions map                       # the drive letters and what each stands for
agent-sessions map D: /run/media/me/Data # say by hand what D: is
agent-sessions map --remove D:
```

**Maintenance**

```sh
agent-sessions scan --deep               # walk every mounted drive completely
agent-sessions verify                    # count the sessions on disk without the index and compare
```

Exit status: 0 success, 1 failure (one line on stderr says why), 2 wrong usage, 130 after
Ctrl-C (143 and 129 after SIGTERM and SIGHUP). An import that is interrupted is rolled back;
a second Ctrl-C or signal waits until that is done. An import killed outright (or cut off by
a power failure) is settled by the next import or undo into the same store: kept and
recorded when its copy is whole, taken out otherwise. An undo that could not remove
everything exits 1, says what it left, and leaves the import standing; run it again.

## Concepts

- **Store.** The directory one tool keeps its sessions in, such as `~/.claude`.
- **Native store.** The store the tool installed on this system reads and writes:
  `~/.claude`, `~/.codex`, `~/.grok`, `~/.gemini/antigravity-cli`, `~/.gemini`. Its
  sessions are **native** and show `Linux` in the WHERE column.
- **Foreign.** A session in any other store, typically under `Users/<name>/` on a Windows
  partition. It can be read, viewed, exported and continued where it is. To resume it with
  its tool it has to be imported. Nothing is ever written to a foreign store.
- **Skipped stores.** Some stores are found and counted (`status`, "skipped") but not
  listed: Claude Desktop's history (a path with `local-agent-mode-sessions`), any store below
  a `tests/fixtures` directory (sample stores a project keeps for its tests), and every store
  inside the agent-sessions checkout the program runs from (fake homes its own tests and
  trials leave).
- **Offline.** A session of a drive that was scanned earlier and is not mounted now. It is
  listed from the index, with `offline` in the WHERE column. Its files cannot be read until
  the drive is mounted (`mount`, or `m` in the browser).
- **Import and undo.** An import copies a session's files into the native store, rewrites
  the recorded folders where the tool needs that to resume, and records every file it
  created in a journal. It never overwrites anything, and a failure or an interrupt takes
  out what was placed. `unimport` removes exactly what the journal lists, and refuses when
  the copy was used since (unless forced), so no new turns are lost by accident.
- **Continue as new.** For a session that cannot be resumed where it is wanted (another
  tool, a provider that cannot be imported, a folder that is gone): the transcript is
  exported to a file, and a tool is started as a new session with one fixed first message
  that names the file.
- **Drive letters.** A Windows session records its folder as `F:\Work\site`. Which mounted
  drive `F:` is, is worked out from evidence (which mount really holds that path) for each
  Windows installation separately, never assumed. `map` shows the result and lets you set
  a letter by hand; a letter that cannot be resolved leaves the session without a local
  folder, and an import then needs `--to`.

## Files it reads and writes

Read, and never written: every session store on every mounted drive. Files on other drives
are opened without updating their access time, and SQLite databases are read in a way that
creates no `-wal` or `-shm` file beside them.

Written:

| Path | What | Mode |
|---|---|---|
| `~/.cache/agent-sessions/index.json` | the index: stores, parsed sessions, remembered drives | file 0600, directory 0700 |
| `~/.cache/agent-sessions/index.lock` | held by a scan from reading the index to saving it | 0600 |
| `~/.cache/agent-sessions/index.json.unusable` | an index this version could not use, kept aside before a new one is saved | as it was |
| `~/.local/state/agent-sessions/imports.jsonl` | the import journal: two records per import (it began; it was done or rolled back) and one per undo | 0600, directory 0700 |
| `~/.local/state/agent-sessions/agent-sessions.log` (and `.1` to `.3`) | the log, rotated at 1 MB | 0600 |
| `~/.local/state/agent-sessions/agent-sessions.lock` | held while an import or an undo runs | 0600 |
| `~/.local/state/agent-sessions/handoff/<provider>-<id>.md` | the transcript `continue` exports; the next `continue` that writes one removes those older than 30 days | 0600, directory 0700 |
| `~/.config/agent-sessions/pathmap.json` | drive letters set by hand; written by `map` only | 0600 |
| the native stores | the files of an imported session, only by `import`; removed only by `unimport` | as the import creates them |
| the file `export -o` names | a transcript; never inside a session store, never over an existing file without `--force` | 0600 |

The cache, state and configuration directories are 0700: made so, and tightened to 0700
when they exist with a wider mode (a directory you named with `AGENT_SESSIONS_CACHE` for the
index file itself is left alone).

While an import runs it stages its files in a directory `.agent-sessions-import-<pid>-<time>`
at the root of the native store; it is removed when the import ends, and one that a killed
import left behind is swept by a later import into the same store once it is over an hour
old.

Environment variables:

| Variable | Effect |
|---|---|
| `AGENT_SESSIONS_TERMINAL` | the command that opens a terminal window, for example `foot -e`; the tool's command is appended. Without it: `omarchy-launch-tui`, else `xdg-terminal-exec`, else `$TERMINAL`, else the first installed of alacritty, kitty, foot, ghostty, wezterm, gnome-terminal, konsole, xterm |
| `TERMINAL` | your terminal, used as described above |
| `AGENT_SESSIONS_MOTION` | `0` stills the browser's cathode-ray motion (the boot log, the raster sweep, the typed-in target, rolling digits, interlaced screens, moving static, the hazard band's beat); on by default |
| `AGENT_SESSIONS_TRUST_TOOLS` | comma-separated tool commands (`claude,codex`) you vouch for: started even when their script on `PATH` names an installer this program cannot follow |
| `AGENT_SESSIONS_NOTIFY` | `0` switches the desktop notification of a failed resume off |
| `AGENT_SESSIONS_HOME` | use this directory as the home: native stores, cache, state and configuration are looked for below it. For tests and trials. `verify` walks the mounted drives only, so with a home outside them (under `/tmp`, which no walk enters) it reports that home's stores and sessions as differences |
| `AGENT_SESSIONS_CACHE` | the cache directory (or the index file itself, when the value ends in `.json`) |
| `AGENT_SESSIONS_STATE` | the state directory |
| `AGENT_SESSIONS_MOUNTS` | a JSON list of mounts that replaces the system's mount table. For tests |
| `NO_COLOR` | the command line prints no colour of its own. The TUI is drawn by Textual, which reads `NO_COLOR` and then draws without colour; under Python 3.14 `--help` is coloured on a terminal by Python's argparse, which honours `NO_COLOR` too (checked for both on 3.14) |
| `MISE_DATA_DIR`, `XDG_DATA_HOME` | read to find where mise installed a tool (see troubleshooting) |

To try the program without touching your own cache and state:

```sh
tmp=$(mktemp -d)
AGENT_SESSIONS_CACHE=$tmp/cache AGENT_SESSIONS_STATE=$tmp/state agent-sessions scan
```

## Logs and troubleshooting

The log is `~/.local/state/agent-sessions/agent-sessions.log` (`agent-sessions status`
prints its path): scans, imports and undos, every command that was started, mounts,
warnings, and the traceback of any unexpected error. `agent-sessions --debug <command>` adds
DEBUG lines (each store scanned, each session parsed or taken from the cache, waits for the
scan lock, each tool's state, how a launch chose its window and folder) and copies the log
to stderr, where an unexpected error then shows its traceback. `agent-sessions --debug`
works for the browser too: the copy for stderr is held back while the screen is in use and
printed when the browser quits. An unexpected error prints one line,
`internal error: <type>: <message> (details in <log>)`, and exits 1.

- **`the drive <name> is not mounted`.** The session is listed from the index, but its
  files are on a drive that is not mounted. `agent-sessions mount <name>` (or `m` in the
  browser) mounts it with `udisksctl`; the desktop asks for the password when one is
  needed. `status` shows each remembered drive as `mounted`, `attached, not mounted` or
  `not attached`, or as `not mounted` alone when its volume id is not known (such a drive
  cannot be mounted from here). Without `udisksctl`, mount the drive from your file manager
  and run `agent-sessions scan`.
- **`<tool> is not installed (the command on PATH would install it)`.** The command of that
  name is a wrapper script that downloads and installs the tool when it is first run
  (`mise use … ; exec mise x …`). agent-sessions never starts such a wrapper, so that
  browsing sessions cannot install software. Run the tool yourself once to install it;
  `status` shows the state of every tool.
- **`<tool> is unknown: a script that may install the tool`.** The command is a script that
  names an installer (`mise`, `npx`, `npm`, `pnpm`, `pipx`, `uv`, `uvx`, `bunx`) in a way
  agent-sessions cannot follow, so it is not started. If you know the tool is installed,
  vouch for it: `AGENT_SESSIONS_TRUST_TOOLS=claude,codex agent-sessions`.
- **`no terminal was found to open a window with`.** None of the known terminals is on
  `PATH`. Set `AGENT_SESSIONS_TERMINAL` to the command that starts yours, or use `--here`
  to run the tool in the current terminal.
- **A session's folder is gone.** `open` says `<folder> is gone; <tool> can only resume
  from it` for a tool that finds a session by its folder, and starts in the home directory
  with a note for the others. `import` says `the project folder … was not found on any
  mounted drive`: pass `--to DIR`. `continue` says `the session's folder is gone`: pass
  `--in DIR`.
- **`import` is refused.** The line says why. The usual reasons: the session's folder is
  not on this machine (`--to DIR`); the provider is Antigravity (`--experimental`, after
  reading the warning) or Gemini (not possible; use `continue`); the drive is not mounted.
  A session that is imported already is not a refusal: the key of the copy is printed and
  nothing is copied. `agent-sessions import <key> --dry-run` shows what an import would do
  without writing.
- **A drive letter shows as `unresolved` in `map`.** No mounted drive holds the folders
  recorded under that letter. Mount the drive, or say what it is: `agent-sessions map D: <dir>`.
- **The list looks wrong or stale.** `agent-sessions scan --deep`, then
  `agent-sessions verify`, which counts the sessions on disk independently of the index
  and names every difference. A store that appeared after the last deep scan (a new
  project, a drive mounted since) is not a difference: `verify` lists it as `stale` and
  advises `scan --deep` first.
- **`the cache could not be used`.** An index this version cannot read is moved to
  `index.json.unusable` and a new one is built by the scan; the sessions of drives that are
  not mounted come back when those drives are scanned again. Entries of the index that are
  damaged are left out one by one, and `scan` says how many.

## Safety

- Every byte of a store on another drive is treated as hostile: ids are checked against a
  fixed form, nothing from such a store reaches a command line except a checked id and a
  folder that exists, and no shell is ever used.
- Nothing outside the paths in the table above is written, and never a store on another
  drive. An import writes only below the native store and overwrites nothing.
- Text from a session is cleaned of terminal control sequences, and of characters that
  reorder or hide text, before it is shown.
- The cache, journal, log and exported transcripts hold titles, prompts and paths; they are
  created for the user alone.
- When a session is resumed or continued, a model reads the old transcript. agent-sessions
  cannot make that safe; an imported or continued session deserves the same care as any
  file from the drive it came from.

The threat model, each defence with the module and the test that covers it, and what is
not defended are in [docs/SECURITY.md](docs/SECURITY.md).

## What has not been tried on a real tool

The automated tests run everything up to the point where an agent tool would start: they
build the command, write the files, and check both. They never start an agent tool, never
send anything to a model, never mount a drive and never write a real store; the rules of
the project forbid it (`CLAUDE.md`), because each of these steps costs money, changes the
owner's real sessions, or needs the desktop password. The formats were read from the
stores and from the tools' own files ([docs/FORMATS.md](docs/FORMATS.md) marks every claim
as verified or inferred). So the following were **never run**, and are owed by the owner,
one supervised run each:

- [ ] One real resume of a native session from the browser (Enter on a `Linux` row): a
      terminal window opens in the session's folder and the tool shows the old turns.
- [ ] One real import and resume per provider:
  - [ ] Claude: `import <key> --open` of a session from the Windows drive.
  - [ ] Codex: the same. Open question: whether Codex accepts a session file that has no
        row in its own database.
  - [ ] Grok: the same. (`grok export` of an imported session was confirmed in a sandbox
        without network; `grok --resume` was not run.)
- [ ] One mount from the browser (`m` on an offline row): the desktop's password prompt
      appears, the drive is mounted, the list refreshes. `udisksctl mount` was never run
      by this project.
- [ ] One "continue as new" (`c`): the tool starts, sends the first message without a key
      press, reads the transcript file and reports where the work stood.
- [ ] One Antigravity experimental import (`import <key> --experimental --open`): the old
      turns are on the screen. If `agy` cannot read the copy it starts an empty
      conversation without an error, which is why this import is behind a flag.

Until a line is ticked, treat that path as built to the documented format and untested in
use. The questions each run answers are listed in docs/FORMATS.md, "Open questions".

## Development

```sh
make dev         # .venv with Textual, ruff, mypy, coverage, build; the project installed editable
make test        # the engine suite, on the system Python, no venv needed
make test-tui    # the TUI suite under .venv (about 4 minutes)
make lint        # ruff check, ruff format --check, compileall
make typecheck   # mypy; for the package four checks are on (see pyproject.toml)
make coverage    # every suite in one run, then the report; fails under 94 %
make check       # lint, typecheck and both suites
make format      # apply the formatter
make dist        # an sdist and a wheel built from it, in dist/
make dist-check  # install each into a venv of its own and run agent-sessions --version
```

- **Layout.** `src/agent_sessions/` is the engine and the command line;
  `src/agent_sessions/tui/` is the only package that imports Textual, and its view logic
  (`view.py`) is plain Python. [filemap.md](filemap.md) describes every file.
- **Tests.** `tests/` is `unittest`, one module per subject (named in `filemap.md`).
  `tests/fixtures/` holds sanitised copies of real stores. `tests/test_tui_app.py` drives
  the app with Textual's headless pilot and a fake engine, and skips itself where Textual
  is not installed.
- **Python versions.** The code targets 3.11 and is run on 3.11 to 3.14 by
  `.github/workflows/ci.yml`, which also runs the coverage floor and a package job
  (`make dist`, `make dist-check`), each job with a time limit. The sdist carries no tests:
  they need the checkout around them. Locally:
  `mise exec python@3.11 -- python -m unittest discover -s tests`.
- **Project rules.** `CLAUDE.md` is binding for every change, and the tests are held to it:
  a test never touches a real store, cache, state or configuration (it uses temp
  directories and the `AGENT_SESSIONS_*` overrides), never starts a terminal or an agent
  tool (`launch.starter` is replaced), never mounts anything (`mounts.starter` is
  replaced), and never sends input to the desktop. Several rules are themselves tests:
  no engine module imports the TUI, only the output helpers of `cli.py` write to a
  stream, no engine message names a command-line flag, every `--json` shape matches
  `docs/JSON.md`, and `filemap.md` lists every module.

## Design documents

| File | What is in it |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | goals, architecture, the CLI and TUI contracts, the threat model; phase 1 and the production pass |
| [docs/FORMATS.md](docs/FORMATS.md) | each provider's on-disk format, resume command and import recipe, with the evidence for every claim, and the open questions |
| [docs/JSON.md](docs/JSON.md) | every `--json` output, field by field |
| [docs/SECURITY.md](docs/SECURITY.md) | what is defended, where, proven by which test; what is not |
| [docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) | the adapter contract and the steps to add a reader |
| [docs/REVIEW.md](docs/REVIEW.md) | the production-readiness review (findings R-01 to R-14) that the hardening answers |
| [docs/PLAN.md](docs/PLAN.md) | the task list and its state |
| [CHANGELOG.md](CHANGELOG.md) | what changed between versions |
| [filemap.md](filemap.md) | where everything is |

## License

No license has been chosen yet. Until the owner adds one, the usual default applies: the
code is published for reading, and no permission to use, copy or distribute it is granted.
