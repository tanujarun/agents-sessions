# Security

What agent-sessions treats as hostile, how it defends itself, where each defence is
implemented and which test proves it, and, in the second half, what it does **not** defend
against. The design decisions behind this are in [SPEC.md](SPEC.md) ("Threat model",
"Read-only rule"); the review that found the first set of problems is
[REVIEW.md](REVIEW.md) (R-01 to R-14).

Paths below are relative to the repository root; modules are under `src/agent_sessions/`,
tests under `tests/`.

## Threat model

The program reads session stores from every mounted drive: a Windows partition, a USB
stick, a synced folder, an extracted archive. Whoever wrote those files is not the user.

- **Hostile: every byte of a foreign store.** A foreign store is any store that is not the
  one the tool installed on this system uses, wherever it lies (a second store under
  `/home` is foreign too). File names, file types, sizes, session ids, titles, folder
  paths, JSON, SQLite databases and protobuf are all attacker-chosen.
- **Trusted, but possibly malformed: what belongs to the user.** The native stores
  (`~/.claude`, `~/.codex`, `~/.grok`, `~/.gemini`), the cache
  (`~/.cache/agent-sessions`), the state directory (`~/.local/state/agent-sessions`), the
  configuration (`~/.config/agent-sessions`) and the command line. They are parsed
  defensively (a damaged file does not crash a listing), but nothing in them is assumed to
  be an attack.
- **What an attacker would want:** to run a command or pass an option to an agent tool
  when the user resumes a session; to make the program write outside the native store or
  over an existing session; to hang or exhaust a scan; to forge what the terminal shows; to
  read the user's session history through files the program leaves behind.
- **Not an attacker in this model:** another process of the same user, or root. See "What
  is not defended".

## Defences

Each rule is enforced in one place and checked again at the point of use.

### 1. Session ids have a fixed grammar

A session id comes from inside a foreign file and ends up as an element of a command line
and in file names. An id must match its provider's `id_pattern` whole (a UUID for all five
providers) to be listed, resumed or imported.

- Implemented: `providers/base.py` (`Provider.id_pattern`, `valid_id`), `model.py`
  (`UUID_PATTERN`); enforced by `index.py` (no session with another id is listed, fresh or
  from the cache), `launch.py` (`plan`, `handoff_file`), `importer.py` (`check`).
- Proven by: `tests/test_session_ids.py` (the grammar, the index, `launch.plan`,
  `importer.check`), `tests/test_textsafe.py` (titles that fall back to an id).
- Closes REVIEW R-01 (an id such as `--config=…` reaching `codex resume`).

### 2. Command lines are argument lists, checked before they run

No shell is used anywhere: a tool is started with `subprocess.Popen` on a list, or with
`os.execvp` for `--here`. Before a command may start (`launch._check`): the tool behind its
first word is really installed (see 9); no element that came from session data, and not the
first prompt either, starts with `-`; nothing holds a NUL. The folder the tool starts in
must be an absolute path of a directory that exists; a parent that "looks close" is never
substituted. `subprocess` appears in two modules only: `launch.py`, and `mounts.py` for the
one fixed `udisksctl mount --block-device /dev/disk/by-uuid/<uuid>` call, whose UUID is
checked against a pattern and against the attached block devices first.

- Implemented: `launch.py` (`plan`, `plan_new`, `_check`, `_directory`, `_detached`),
  `mounts.py` (`mount_volume`, `UUID_PATTERN`).
- Proven by: `tests/test_session_ids.py` (`LaunchRefusesTest`), `tests/test_launch.py`
  (`PlanTest.test_a_relative_recorded_folder_is_never_started_in`: the "absolute path" half),
  `tests/test_continue.py`, `tests/test_volumes.py` (the exact mount command and the
  refusals before anything is run).

### 3. The first prompt of "continue" is fixed

`continue` starts a tool with a first message. That message is a constant
(`launch.FIRST_PROMPT`) with two things put in: the label of the tool the session was made
with, and the path of the exported transcript, which is built from the adapter's name and
an id that passed the grammar. No text of the session is in the command line. `launch`
holds that structurally: it makes the prompt itself (`plan_new` takes no prompt), and it
builds the adapter's command twice with two different prompts and refuses it unless exactly
one whole element differs.

- Implemented: `launch.py` (`FIRST_PROMPT`, `handoff_file`, `plan_new`); each adapter's
  `new_session_argv`.
- Proven by: `tests/test_continue.py` (every adapter's command, "the prompt and nothing
  else from outside the adapter", an adapter that cuts or changes the prompt it is given, a
  hostile session).
- Limit: the prompt tells the model the file is a record, not instructions. That is a
  label, not a barrier; see "What is not defended".

### 4. An import cannot leave the native store, and overwrites nothing

Every destination of an import is checked independently of the adapter that planned it:
it must be a path below the native store, component by component, with no `..`, no
absolute part and no empty name (`importer.contained`, `below`). Names taken from the
source (a Claude side directory, a Codex rollout name, a Grok group) are checked where they
are taken. Symbolic links inside the native store are followed on purpose: the store is
the user's own, and an import creates regular files and directories only, so nothing from
a foreign store can plant a link there. Files are staged first, then put in place with
`os.link`, which fails instead of replacing when the name exists, even when it appears at
the last moment; a directory is renamed into a name that must not exist. The placed copy is
then read back with the same parser that lists sessions and must be the planned session,
made of exactly the planned files; otherwise everything is taken out again. A copy that
nests more than 64 directories deep, or that needs a path or a name longer than the
filesystem allows at its destination or in the staging directory, is refused before anything
is written (`importer.fits`, in `check` and again in `execute`): a hostile tree cannot make the
roll back itself fail (on Python 3.11, `shutil.rmtree` of a tree 1,900 deep raised
`RecursionError` and left the copy in place: audit A-05). The steps of the roll back run each
on its own, so one that fails does not keep the others from running.

- Implemented: `importer.py` (`contained`, `below`, `check`, `fits`, `_place`, `execute`,
  `_wind_up`), `providers/base.py` (`read_back`).
- Proven by: `tests/test_import_safety.py` (containment, adapter names, `DepthAndLengthTest`,
  the read-back and its roll back), `tests/test_import_placing.py` (never renamed over; names
  that appear at the last moment), `tests/test_importer.py` (collisions, idempotence).
- Closes REVIEW R-04.

### 5. An undo removes only what its record lists

`unimport` works from the journal record of the import, which lists every file and
directory the import created with size and modification time. Every path must again pass
`importer.below` for this native store, whatever the journal says; a file that differs
from the record (the copy was used since) stops the undo unless it is forced; a directory
goes only when it is empty; a line appended to a file the tool owns is left and reported.
While the files a tool makes beside a copy it has opened exist (Antigravity's `-wal`, `-shm`
and lock file), a plain undo is refused before anything is removed; a forced one removes them
with the copy. An undo that could not remove everything is recorded as incomplete
(`"complete": false`): the import stays standing, the command exits 1, and the next undo
finishes it (audit A-06). The plan an undo shows before its question is made under the lock
(audit A-08).

- Implemented: `unimport.py` (`plan`, `undo`), `journal.py` (`find_record`, `not_undone`,
  `removed_already`).
- Proven by: `tests/test_unimport.py` (byte-identical store after import and undo, a
  journal that names paths outside the store or is damaged, what is left and why;
  `ExtrasTest` for the extras, `UsedSinceTest` for an undo that could not remove
  everything, `LockAndRecordTest` for two undos of one import).

### 6. Nothing from a store reaches the terminal unfiltered

Everything printed for a person passes `textsafe.clean`: the C0 and C1 control characters,
DEL, U+2028, U+2029, the lone surrogates U+D800 to U+DFFF, and every format character
(Unicode category Cf: the bidirectional controls, the zero-width characters U+200B to U+200D
and U+2060, the soft hyphen, U+FEFF, the tag characters) with the rest of the tag block
U+E0000 to U+E007F each become a space. A lone surrogate is what Python reads a byte that is
not UTF-8 as, and in the C or POSIX locale a stream wrote it back as that raw byte (0x9B is
the 8-bit CSI): the command line now encodes its output itself (`backslashreplace`) and
writes bytes (audit A-03). The format characters draw nothing and can hide text, the tag
characters a whole line of ASCII (audit A-27); the Markdown export replaces them too and its
header says how many there were. `--json` output stays exact data and writes the same
characters as `\uXXXX` escapes; it is always valid UTF-8 (ASCII with `\u` escapes when stdout
is not UTF-8). In `cli.py` only the output helpers write to a stream (`_out`, `_err`, `_json`,
`_json_line`, `_progress`, and `_ask` for a question) and there is no `print`; the log's
formatter cleans every message and traceback, and the log is written with
`backslashreplace`; the desktop notification is cleaned and can never begin with `-`.

The TUI cleans through `view.clean` (which is `textsafe.clean`). Its two inputs, the folder
and the filter, are `SafeInput`: they draw `view.clean(value)`, a paste included, and the
value itself stays what was typed. No name that `clean` would change is put into them: a
suggested folder with such a name is not offered (the input starts at `~/` and says why) and
Tab leaves such names out of its completions and counts them (audit A-12). Every field of a
session and every engine answer the TUI shows is drawn cleaned, at several screen sizes
(audit A-15). A search's snippets are cleaned where they are cut (`search._hit`), and the text
the search keeps never leaves the process: it is held in memory, read through `transcript`
and its bounds, and written nowhere.

- Implemented: `textsafe.py`; `cli.py` (output helpers, `_write`); `tui/view.py` (`clean`,
  `complete_folder`); `tui/app.py` (`SafeInput`, `MainScreen._open_folder`); `logs.py`
  (`SafeFormatter`); `launch.py` (`_notice`); `providers/base.py` (`clean_title`);
  `export.py` (the count of invisible characters).
- Proven by: `tests/test_textsafe.py` (the character set, the JSON round trip, every CLI
  command on stores full of escape sequences, `LocaleTest` for the C, C.UTF-8 and POSIX
  locales, the rule that only the helpers write, the notification), `tests/test_logs.py`,
  `tests/test_tui_view.py` (the `Hostile*` classes: every row, detail and prompt value),
  `tests/test_tui_app.py` (`HostileTest`: hostile text in every `Session` field and every
  engine answer, asserted on what the screen draws, the folder and filter inputs included;
  `SearchTest`: a hostile message in the search results), `tests/test_search.py` (snippets).
- Closes REVIEW R-03 and R-09, audit A-03, A-12, A-15, A-27.

### 7. Reads are bounded and never wait

`fsutil.open_ro` reads regular files only. The file type is checked before the open (a
device is not even opened) and again on the open descriptor (the name can be given to
something else in between), and the open itself does not block: a named pipe, a device, a
socket or a directory named like a session file raises `NotRegularFile`. One JSONL line is
read up to 16 MiB and a longer one is dropped piece by piece without being held; a line, or
a JSON file read whole, with more than 1,000,000 brackets, braces and commas is skipped
before it is parsed (`JSONL_STRUCTURE_MAX`: what parsing costs goes by those, not by
bytes); at most 2 GiB of any one file is read (`FILE_READ_MAX`: a sparse file of a petabyte
costs 4 kB on disk and was read for days, audit A-02), and the session is listed from what
was read; files read whole have size caps; titles, folders and notes are cut; JSON nesting
that exhausts the recursion limit is caught (`RecursionError` with `ValueError`); a time
outside 1990 to 2100 is not a time. The scan's cancel event is looked at inside a file, every
4 MiB and while a database loads. The caps are listed in [FORMATS.md](FORMATS.md) ("Size
caps"). A file that fails any of this is skipped with a warning and never stops a scan.
Records of the wrong JSON type are treated the same way.

- Implemented: `fsutil.py` (`open_ro`, `listdir_ro`, `subdirs`), `providers/base.py`
  (`read_lines`, `iter_jsonl`, `JSONL_LINE_MAX`, `JSONL_STRUCTURE_MAX`, `FILE_READ_MAX`,
  `cancellable`, `read_small`, `load_json`, `parse_ts`), `index.py` (`CWD_MAX`, `NOTE_MAX`),
  each adapter.
- Proven by: `tests/test_hostile_files.py` (pipes, devices and directories; whole scans in
  a child process under a timeout; a 200 MB line under a memory ceiling, measured as the
  scanning child's own peak; size caps; deep JSON), `tests/test_read_bounds.py` (the byte
  budget, sparse files, dense lines, the cancel inside a file, large databases),
  `tests/test_hostile_values.py` (text that used to cost minutes of regular-expression time),
  `tests/test_wrong_types.py`, `tests/test_unreadable.py`.
- Closes REVIEW R-02 and R-05, audit A-02 (the engine's part) and A-16.

### 8. SQLite files are read without side effects, hardened, and refused when odd

Every database, native ones included, goes through `providers.base.open_sqlite_ro`: the
file is read through `open_ro` into an in-memory connection, up to 256 MiB
(`SQLITE_MEMORY_MAX`); a larger one, and anything that is not a regular file, is not opened
at all. SQLite never opens a store's file itself, so no `-wal` or `-shm` file is created or
touched beside it and no access time moves (the in-place `immutable=1` open for files over
64 MB moved it: audit A-16). Before the file's bytes are loaded the connection is set to
`query_only`, `trusted_schema` off and `cell_size_check` on, with defensive mode where the
Python build can set it, and a value or a row may be 16 MiB at most (`SQLITE_LIMIT_LENGTH`,
`SQLITE_VALUE_MAX`). A database whose schema holds a view, a trigger or a virtual table is
refused; a truncated or scrambled one is one skipped candidate with a warning.

- Implemented: `providers/base.py` (`open_sqlite_ro`).
- Proven by: `tests/test_sqlite_hardening.py`, `tests/test_base_helpers.py` (no sidecar
  files, access times unchanged), `tests/test_read_bounds.py` (`LargeDatabaseTest`).
- Closes REVIEW R-08.

### 9. A tool is started only when it is installed

On the machine this was built on, most agent commands on `PATH` are wrappers that download
and install the tool on first use. `launch.tool_state` reads files only (at most 64 KiB of a
command) and fails closed for scripts:

- A `#!` script with a `mise x` or `mise exec` call anywhere in it (after `exec` or
  `env -u VAR`, inside an `if`, after a `;`, with options, `"${@}"`, a continued line, CRLF, a
  byte that is not UTF-8) is a wrapper; every such call is followed, and the tool is installed
  only when mise's install directory holds it for every spec of every call.
- A `#!` script that names an installer as a command (`mise`, `npx`, `npm`, `pnpm`, `pipx`,
  `uv`, `uvx`, `bunx`) and has no such call, or one that cannot be followed, is `unknown: a
  script that may install the tool`: not launchable and never started. The refusal names
  `AGENT_SESSIONS_TRUST_TOOLS`, a comma-separated list of commands the user vouches for,
  which are then started as installed.
- A program, and a script that names no installer, is installed.

A command that would install something is never started, so browsing sessions cannot install
software. (The first version knew one exact wrapper text and took every other script for a
program; Omarchy's own `hermes` wrapper was one: audit A-09.)

- Implemented: `launch.py` (`tool_state`, `_inspect`, `_wrapper`, `INSTALLERS`,
  `trusted_tools`, `_check`).
- Proven by: `tests/test_tools.py` (`WrapperTextTest`: the variants the audit tried, the
  unknown state, the trusted list).

### 10. No traces on other drives, no writes outside our own places

Files on other drives are opened with `O_NOATIME` where the platform has it, directories
are listed the same way, and nothing is ever written to a foreign store. The program
writes only: the native stores (by `import`, undone by `unimport`), its cache, state and
configuration directories, and the one file `export -o` names. That file is refused, forced
or not, when it lies inside a session store the index knows, below any provider's native
store (whether it exists yet or not), below a directory on the way up that a provider's
`is_store` accepts (a store on a drive mounted since the last scan: audit A-10), or inside
the program's own state, configuration or cache directory (audit N-21); paths are compared
by real path. It is created with `O_EXCL` under a new temp name, then linked into place, so
nothing that already lies in the target directory is written through.

- Implemented: `fsutil.py` (`open_ro`, `listdir_ro`), `export.py` (`write`), `api.py`
  (`export_file`, `_in_a_store`).
- Proven by: `tests/test_base_helpers.py`, `tests/test_importer.py` (source times),
  `tests/test_export.py` (no write into a store, nothing replaced unless asked),
  `tests/test_letters.py` (`pathmap.json`: validated, atomic, private).

### 11. What we keep is private

The index, the journal, the log, the lock file and the exported transcripts hold titles,
prompts and paths. Files are created 0600 and the directories made for them 0700
(`fsutil.open_private`, `make_private_dir`); a cache file with a wider mode is tightened
when it is rewritten and a log when it is opened; our own directories (state, handoff,
configuration, cache) that exist with a wider mode are tightened to 0700 when they are used
(`fsutil.keep_private`: a real directory this user owns; a link is not followed; audit
A-25); imported files and directories are 0600 and 0700, and a file that an import has to
create for a line it appends is created 0600 (one that exists keeps its mode). The lock file
is never opened through a symbolic link (`O_NOFOLLOW`) and must be a regular file, so the
holder's process number is never written through a link into another file; the journal is
read and appended to as a regular file only, and opening it never waits, so a named pipe
under its name neither holds a listing nor swallows a record (audit A-18).

- Implemented: `fsutil.py` (`open_private`, `make_private_dir`, `keep_private`,
  `_open_lock`, `open_regular`), `index.py`, `journal.py`, `logs.py`, `export.py`,
  `importer.py`.
- Proven by: `tests/test_journal.py` (`LockTest`: a link, a named pipe and a directory under
  the lock's name; `JournalTest`: a named pipe under the journal's name), `tests/test_logs.py`,
  `tests/test_index.py`, `tests/test_export.py`, `tests/test_continue.py`,
  `tests/test_importer.py`.
- Closes REVIEW R-10, audit A-18.

### 12. Imports and undos are serialised and roll back

An import and an undo each run under one lock on the state directory (`flock`; re-entrant
in one thread, with a timeout that names the holder). The journal is append-only, one
`write` per record on an appending descriptor, then `fsync`; a torn last line is skipped
when it is read. On any failure before the record is written (a failed read-back, a full
disk, a source that disappears, Ctrl-C, SIGTERM, SIGHUP) everything the import placed is
removed again and the staging directory goes. After the first signal every further SIGINT,
SIGTERM or SIGHUP is held off until the roll back is done (audit A-04: a second Ctrl-C cut it
short). A process killed outright cannot roll back: before its first unit is placed, each
import writes a `begin` line naming everything that is going to appear in the store, and the
next import or undo of that store settles a `begin` that has no `import` or `rollback` after
it (`importer.recover`): forward when the copy reads back whole as the planned session,
otherwise back, never removing a whole copy. A staging directory left by a killed process
is swept by a later import.

- Implemented: `fsutil.py` (`lock`), `journal.py` (`unsettled`), `importer.py` (`execute`,
  `recover`, `sweep_staging`), `unimport.py` (`undo`), `cli.py` (`_signals`).
- Proven by: `tests/test_journal.py` (threads and real processes), `tests/test_import_placing.py`
  (two real processes importing the same and different sessions), `tests/test_interrupts.py`
  (a child signalled in the middle of a copy, the source disappearing, the disk filling up;
  `SecondSignalTest`; `KilledImportTest`: a child killed with SIGKILL with everything
  staged, between two units and before the record; `RecoverTest`),
  `tests/test_unimport.py` (the lock, with real processes).
- Closes REVIEW R-06, audit A-04.

### 13. The current directory is not on `sys.path`

The browser is often started in a folder of another drive. `python -m` and `python -c` put
the current directory first on `sys.path`, so a `json.py` or `textual.py` there ran as the
user when the checkout's launcher started the TUI (audit A-01). The launcher starts the venv's
Python with `-P` and a two-line bootstrap that puts the checkout's `src` on its path, and
leaves the environment as it is (no `PYTHONPATH` for the tools the TUI starts to inherit).
Every other command runs with `src` in the place of the launcher's directory.

- Implemented: `bin/agent-sessions` (`BOOTSTRAP`, `run_tui`, `use_src`), which starts
  `tui/__main__.py` (`main`).
- Proven by: `tests/test_entry.py` (`LauncherTest`: a stub venv Python records its arguments
  and environment; `LauncherOnATerminalTest`: the real launcher in a private pty, started from
  a directory holding a planted `textual.py`, `json.py`, `logging.py`, `rich.py` and
  `argparse.py`, runs none of them).

## What is not defended

Stated plainly, so that nobody assumes more than is there.

1. **A model reads the transcript.** When an imported session is resumed, or a session is
   continued, the agent tool gives the old conversation to its model. A transcript from a
   hostile drive can contain text written to steer that model (prompt injection), and the
   tool may then act on it with the user's permissions. This is inherent in resuming
   someone else's session and is not something this program can filter. The fixed first
   prompt of `continue` only *labels* the exported file as a record; a model may still
   follow what the file says. Treat a session from a drive you do not trust like a script
   from that drive.
2. **Live behaviour of the agent tools on imported data is untested.** No test, and no run
   by the authors, has started a real tool on an imported or continued session (README,
   "What has not been tried on a real tool"). How each tool's own parser copes with a
   hostile file that passed our reader is unknown; our checks constrain what we do, not
   what the tool does afterwards.
3. **Markdown export does not escape Markdown.** `export` (and so the transcript file of
   `continue`) replaces what `textsafe.clean` replaces (controls, lone surrogates, format
   characters) and nothing else. A turn's text can contain headings, links,
   HTML or a line that looks like the next `## User` section, so a rendered export can be
   made to misrepresent who said what. The JSON lines export is exact data and has no such
   ambiguity.
4. **Notification text is not markup-escaped.** The body given to `notify-send` is cleaned
   of control characters and cannot be read as an option, but characters a notification
   daemon treats as markup (`<b>`, `<a href>`) are passed through. The text can quote a
   session's folder name.
5. **`open_ro` follows a symbolic link to a regular file.** The regular-file rule is about
   the file that is finally opened. A link in a foreign store, named like a session file
   and pointing at a regular file elsewhere, is read as if it were that session file: it is
   listed, and `show`, `export` and `continue` read it, if what it points to parses as a
   session. An import does not copy it: it copies regular files only and refuses the session
   (`… is not a regular file`; this item said more than was true until audit A-25). Directory
   walks do not follow links and do not cross mounts.
6. **The native stores, the cache, the state and the configuration are trusted.** A process
   of the same user that can write them can make the program list, resume or remove other
   things: a forged journal record still cannot name a path outside the native store, and
   a cached session still has to pass the id grammar, but beyond those two checks their
   content is believed. `pathmap.json` can point a drive letter anywhere. Symbolic links
   inside the native store are followed.
7. **Time-of-check gaps that remain.** None is reachable from a foreign store alone; each
   needs another process acting on this system between a check and its use:
   - *Folders.* The folder of a resume, of `import --to` and of `continue --in` is checked
     when the command is planned, and used a moment later when the tool is started in it.
     A folder replaced in between is not noticed.
   - *The tool.* `tool_state` reads the wrapper and the install directory when the command
     is planned. The command itself names the tool by its bare name, and the terminal
     launcher finds it on `PATH` when it runs: what is installed is established, not which
     file is executed.
   - *Undo.* An undo compares each file with its record (size and modification time, not a
     hash) and then removes it, under our lock. The agent tool does not take that lock: a
     turn it writes between the comparison and the removal is lost, and a change that keeps
     size and modification time is not seen.
   - *Import containment and collisions.* Destinations are checked before the copy is
     staged. Placement cannot overwrite (the kernel refuses the link), but a directory of
     the native store that another process turns into a link after the check is followed.
   - *Export.* Whether the target lies inside a session store is decided from its real path
     before the file is written; a directory swapped for a link into a store after that is
     not detected.
   - *The parse cache.* A session file is read again only when its size or modification
     time changed. A file whose content changes while both stay the same keeps its cached
     title and counts until the next change.
   - *Mounting.* The volume id is checked against the attached devices, then `udisksctl`
     resolves `/dev/disk/by-uuid/<uuid>` itself.
8. **Volume.** Every single read is bounded, but the number of files is not: a drive with
   millions of session-shaped files makes a scan slow and the index large. A full rescan
   can be cancelled in the TUI and interrupted on the command line.
9. **Confidentiality from root and from other processes of the same user.** Modes 0600 and
   0700 keep other users out. The exported transcripts in the state directory stay until a
   later real `continue` removes the ones older than 30 days (nothing else sweeps them); the
   cache keeps titles and folder paths of sessions on drives that are no longer attached
   until `forget` or `uninstall.sh --purge`.
10. **The tools' own data.** A session copied in by an import is from then on an ordinary
    session of that tool. What the tool writes next to it, indexes from it or sends
    elsewhere is the tool's business.
11. **Variation selectors are not replaced.** U+FE00 to U+FE0F and U+E0100 to U+E01EF are
    not format characters (their category is Mn) and `textsafe.clean` leaves them in. A run of
    them after a visible character draws nothing and can carry hidden bytes, into the screen,
    the Markdown export and the transcript file `continue` gives to a model, as the tag
    characters could before audit A-27.
12. **A terminal that is not in UTF-8 mode.** Output is UTF-8. Every C1 control is replaced,
    but the bytes 0x80 to 0x9F also occur as continuation bytes of ordinary characters (`Û`
    is `C3 9B`, and 0x9B is the 8-bit CSI). A terminal that reads the output as Latin-1 or
    another 8-bit set and acts on C1 bytes (xterm, rxvt or the Linux console in such a locale)
    can act on them. In the C and POSIX locales the command line writes no raw byte of a
    surrogate any more (audit A-03), but it still writes UTF-8 for text that is not ASCII.
13. **A resumed or continued tool loads the folder's own configuration.** `open` of an
    imported session, and `continue`, start the tool in the session's folder, which can be a
    folder of the foreign drive (a Windows project mapped through a drive letter). The agent
    tool then reads that folder's own agent configuration as it would in any project:
    `CLAUDE.md`, `.claude/settings.json` (whose hooks run commands), `.mcp.json` (whose
    servers are programs), `AGENTS.md`, and the like. Whoever wrote the drive chose those
    files. Nothing of this program reads or checks them (audit N-14). Use `--to` or `--in`
    with a folder of your own for a session from a drive you do not trust.

## Reporting a problem

No security contact has been set up yet; report to the owner of the repository. For
anything that lets a file on a drive run a command, write outside the paths named above or
forge what the terminal shows, include the smallest store that reproduces it; the fixtures
under `tests/fixtures/` show the layout each reader expects.
