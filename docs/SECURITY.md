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
- Proven by: `tests/test_session_ids.py` (`LaunchRefusesTest`), `tests/test_launch.py`,
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
  else from outside the adapter", a hostile session).
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
made of exactly the planned files; otherwise everything is taken out again.

- Implemented: `importer.py` (`contained`, `below`, `check`, `_place`, `execute`),
  `providers/base.py` (`read_back`).
- Proven by: `tests/test_import_safety.py` (containment, adapter names, the read-back and
  its roll back), `tests/test_import_placing.py` (never renamed over; names that appear at
  the last moment), `tests/test_importer.py` (collisions, idempotence).
- Closes REVIEW R-04.

### 5. An undo removes only what its record lists

`unimport` works from the journal record of the import, which lists every file and
directory the import created with size and modification time. Every path must again pass
`importer.below` for this native store, whatever the journal says; a file that differs
from the record (the copy was used since) stops the undo unless it is forced; a directory
goes only when it is empty; a line appended to a file the tool owns is left and reported.

- Implemented: `unimport.py` (`plan`, `undo`), `journal.py` (`find_record`).
- Proven by: `tests/test_unimport.py` (byte-identical store after import and undo, a
  journal that names paths outside the store or is damaged, what is left and why).

### 6. Nothing from a store reaches the terminal unfiltered

Everything printed for a person passes `textsafe.clean`: the C0 and C1 control characters,
DEL, the bidirectional controls, U+2028, U+2029 and U+FEFF each become a space. `--json`
output stays exact data and writes the same characters as `\uXXXX` escapes. In `cli.py`
only the output helpers write to a stream (`_out`, `_err`, `_json`, `_json_line`,
`_progress`, and `_ask` for a question) and there is no `print`; the TUI cleans through
`view.clean`;
the log's formatter cleans every message and traceback; the desktop notification is
cleaned and can never begin with `-`.

- Implemented: `textsafe.py`; `cli.py` (output helpers); `tui/view.py` (`clean`);
  `logs.py` (`SafeFormatter`); `launch.py` (`_notice`); `providers/base.py` (`clean_title`).
- Proven by: `tests/test_textsafe.py` (the character set, the JSON round trip, every CLI
  command on stores full of escape sequences, the rule that only the helpers write, the
  notification), `tests/test_logs.py`, `tests/test_tui_view.py`.
- Closes REVIEW R-03 and R-09.

### 7. Reads are bounded and never wait

`fsutil.open_ro` reads regular files only. The file type is checked before the open (a
device is not even opened) and again on the open descriptor (the name can be given to
something else in between), and the open itself does not block: a named pipe, a device, a
socket or a directory named like a session file raises `NotRegularFile`. One JSONL line is
read up to 32 MiB and a longer one is dropped piece by piece without being held; files
read whole have size caps; titles, folders and notes are cut; JSON nesting that exhausts
the recursion limit is caught (`RecursionError` with `ValueError`); a time outside 1990 to
2100 is not a time. The caps are listed in [FORMATS.md](FORMATS.md) ("Size caps"). A file
that fails any of this is skipped with a warning and never stops a scan. Records of the
wrong JSON type are treated the same way.

- Implemented: `fsutil.py` (`open_ro`, `listdir_ro`, `subdirs`), `providers/base.py`
  (`read_lines`, `iter_jsonl`, `JSONL_LINE_MAX`, `read_small`, `parse_ts`), `index.py`
  (`CWD_MAX`, `NOTE_MAX`), each adapter.
- Proven by: `tests/test_hostile_files.py` (pipes, devices and directories; whole scans in
  a child process under a timeout; a 200 MB line under a memory ceiling; size caps; deep
  JSON), `tests/test_hostile_values.py` (text that used to cost minutes of regular-expression
  time), `tests/test_wrong_types.py`, `tests/test_unreadable.py`.
- Closes REVIEW R-02 and R-05.

### 8. SQLite files are read without side effects, hardened, and refused when odd

Every database, native ones included, goes through `providers.base.open_sqlite_ro`: a
file of up to 64 MB is read through `open_ro` into an in-memory connection, a larger one is
opened in place with `mode=ro&immutable=1`, and nothing that is not a regular file is
opened at all. There is no plain `sqlite3.connect` on a store's file, so no `-wal` or
`-shm` file is created or touched beside it. Before any statement reads the data the connection is set to
`query_only`, `trusted_schema` off and `cell_size_check` on, with defensive mode where the
Python build can set it. A database whose schema holds a view, a trigger or a virtual table
is refused; a truncated or scrambled one is one skipped candidate with a warning.

- Implemented: `providers/base.py` (`open_sqlite_ro`).
- Proven by: `tests/test_sqlite_hardening.py`, `tests/test_base_helpers.py` (no sidecar
  files, access times unchanged).
- Closes REVIEW R-08.

### 9. A tool is started only when it is installed

On the machine this was built on, most agent commands on `PATH` are wrappers that download
and install the tool on first use. `launch.tool_state` recognises such a wrapper by its
text and looks for the real program in mise's install directory, reading files only; a
command that would install something is never started, so browsing sessions cannot install
software.

- Implemented: `launch.py` (`tool_state`, `_check`).
- Proven by: `tests/test_tools.py`.

### 10. No traces on other drives, no writes outside our own places

Files on other drives are opened with `O_NOATIME` where the platform has it, directories
are listed the same way, and nothing is ever written to a foreign store. The program
writes only: the native stores (by `import`, undone by `unimport`), its cache, state and
configuration directories, and the one file `export -o` names, which is refused when it
lies inside any session store (compared by real path) and is created with `O_EXCL` under a
new temp name, then linked into place, so nothing that already lies in the target
directory is written through.

- Implemented: `fsutil.py` (`open_ro`, `listdir_ro`), `export.py` (`write`), `api.py`
  (`export_file`, `_in_a_store`).
- Proven by: `tests/test_base_helpers.py`, `tests/test_importer.py` (source times),
  `tests/test_export.py` (no write into a store, nothing replaced unless asked),
  `tests/test_letters.py` (`pathmap.json`: validated, atomic, private).

### 11. What we keep is private

The index, the journal, the log, the lock file and the exported transcripts hold titles,
prompts and paths. Files are created 0600 and the directories made for them 0700
(`fsutil.open_private`, `make_private_dir`); a cache file with a wider mode is tightened
when it is rewritten; imported files and directories are 0600 and 0700, and a file that an
import has to create for a line it appends is created 0600 (one that exists keeps its mode).

- Implemented: `fsutil.py`, `index.py`, `journal.py`, `logs.py`, `export.py`, `importer.py`.
- Proven by: `tests/test_journal.py`, `tests/test_logs.py`, `tests/test_index.py`,
  `tests/test_export.py`, `tests/test_continue.py`, `tests/test_importer.py`.
- Closes REVIEW R-10.

### 12. Imports and undos are serialised and roll back

An import and an undo each run under one lock on the state directory (`flock`; re-entrant
in one thread, with a timeout that names the holder). The journal is append-only, one
`write` per record on an appending descriptor, then `fsync`; a torn last line is skipped
when it is read. On any failure before the record is written (a failed read-back, a full
disk, a source that disappears, Ctrl-C, SIGTERM, SIGHUP) everything the import placed is
removed again and the staging directory goes; a staging directory left by a killed process
is swept by a later import.

- Implemented: `fsutil.py` (`lock`), `journal.py`, `importer.py` (`execute`,
  `sweep_staging`), `cli.py` (`_signals`).
- Proven by: `tests/test_journal.py` (threads and real processes), `tests/test_import_placing.py`
  (two real processes importing the same and different sessions), `tests/test_interrupts.py`
  (a child signalled in the middle of a copy, the source disappearing, the disk filling up),
  `tests/test_unimport.py` (the lock, with real processes).
- Closes REVIEW R-06.

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
   `continue`) removes control characters only. A turn's text can contain headings, links,
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
   listed if what it points to parses as a session, and an import copies those bytes into
   the native store. Directory walks do not follow links and do not cross mounts.
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
   0700 keep other users out. The exported transcripts in the state directory stay until
   they are 30 days old; the cache keeps titles and folder paths of sessions on drives that
   are no longer attached until `forget` or `uninstall.sh --purge`.
10. **The tools' own data.** A session copied in by an import is from then on an ordinary
    session of that tool. What the tool writes next to it, indexes from it or sends
    elsewhere is the tool's business.

## Reporting a problem

No security contact has been set up yet; report to the owner of the repository. For
anything that lets a file on a drive run a command, write outside the paths named above or
forge what the terminal shows, include the smallest store that reproduces it; the fixtures
under `tests/fixtures/` show the layout each reader expects.
