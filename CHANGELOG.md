# Changelog

What changed for someone who uses agent-sessions. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Neither version has been tagged or published,
so the version headings link to nothing: 0.1.0 is the state of the repository at commit
`c3029a8`, 0.2.0 the state after the production pass described in `docs/SPEC.md`
("Phase 2").

Nothing in either version has been run against a real agent tool yet; the README lists what
is still owed ("What has not been tried on a real tool").

## [Unreleased]

The fixes for the acceptance audit of 0.2.0 (`docs/AUDIT.md`; finding numbers in brackets).
Not given a version number yet: nothing has been tagged or published, and which number the
next release gets is the owner's decision. The program still says 0.2.0.

### Added

- The browser takes the look of NERV's screens in Neon Genesis Evangelion: black ground with colour only
  as a signal (orange system, green nominal, red alert, cyan external), numbered rows and provider plates,
  framed panels, a LOCATION that reads SIGNAL LOST for a drive that is not mounted, a MAGI panel that
  votes on resume, import and drive for the selected session, key hints drawn as plates, and a hazard
  band over any question that cannot be taken back or sends a message to a model.
- The browser is laid out as Central Dogma's monitors are, not as a table with a detail pane. From 160
  columns the providers are SUBJECT plates in a column (the selected one framed, its number in
  seven-segment numerals, each with a trace of the last 18 days' sessions); from 135 a leader line runs
  from the selected row to a TARGET readout beside the log: a 内部 INTERNAL / 外部 EXTERNAL / 信号途絶
  SIGNAL LOST plate, the time since the session was last active as a seven-segment clock, the data, and
  the MAGI drawn as their three panels with the resolution (決議 可決 / 否決) between them. Every row
  carries a signal mark; a lost one is moving static. Narrower terminals keep the readout under the log.
- The space round the log carries information, fitted to the window: a panel that has no room is left
  out, so short, tall, wide and narrow terminals each show what fits. Where TITLE can spare the width,
  each row draws its lifespan (start to last activity) on a log scale of age (2MO … 1H, NOW) and a
  message meter in identical units with the count. Under the subjects: a cell a day for the selected
  subject over the last ten weeks, and a drive board (D-01 …: name, ONLINE / STANDBY / 信号途絶,
  filesystem, letter, sessions held). Under the MAGI: the other sessions of the same folder in any tool,
  a sync graph of the selected session's turns over its time, and its communication log (the last
  things said by the user and the agent). The transcript is read, bounded in turns and time, once the
  selection settles, and kept for the sessions read last.
- The screen redraws as a cathode-ray tube (`tui/crt.py`): a boot log prints the drives and subjects as
  the index is read; a provider change, a rescan and the end of the boot sweep a raster band over the
  whole screen, the old picture showing below it; a new target types itself in behind a block cursor
  while its clock's digits roll and the MAGI deliberate (審議中); help and the transcript come in
  interlaced; a danger band alternates its stripes with a dark 非常事態 EMERGENCY beat; a job under way
  blinks. `AGENT_SESSIONS_MOTION=0` stills it all.

### Security

- Starting the browser in a folder that holds a `json.py`, `textual.py` or another module
  of that name no longer runs it: the checkout's launcher keeps the current directory off
  Python's path [A-01].
- A sparse file of any size named like a transcript no longer keeps a scan reading for days:
  at most 2 GiB of a file is read, and a cancel is seen inside a file [A-02].
- In the C and POSIX locales, text from a store no longer reaches the terminal as raw C1
  control bytes, and `--json` is valid UTF-8 in every locale [A-03].
- A command on `PATH` is no longer taken for the real tool when it is a script that names an
  installer in a form that is not understood: such a tool is "unknown" and never started;
  `AGENT_SESSIONS_TRUST_TOOLS` vouches for one [A-09].
- `export -o` refuses every session store (also one on a drive mounted since the last scan,
  and a tool's store on this system that does not exist yet) and the program's own files
  [A-10, N-21].
- A Codex import no longer copies the source's line into the native `session_index.jsonl`:
  it writes a new line from checked values, or none [A-11].
- The TUI's folder input no longer draws the escape sequences of a folder name from another
  drive [A-12].
- A line can no longer cost hundreds of megabytes to parse, a SQLite value is limited to
  16 MiB, and no database is opened in place, so no access time moves [A-16].
- The lock file is never opened through a symbolic link, and a named pipe under the name of
  the journal or the log no longer hangs a command [A-18].
- Invisible characters (zero-width, soft hyphen, the tag characters) are replaced in what is
  shown, and the Markdown export says how many there were [A-27].

### Fixed

- An import killed half way is settled by the next import or undo of that store, and a
  second Ctrl-C or signal no longer cuts a roll back short [A-04].
- A copy nested too deep, or with a path too long for the filesystem, is refused before
  anything is written; on Python 3.11 its roll back used to fail and leave the copy [A-05].
- An undo that could not remove everything keeps the import standing and exits 1, and the
  next undo finishes it; a plain undo is refused while the tool's own files lie beside the
  copy [A-06].
- The memory test of the suite measures the scan, not the test runner, so `make test` no
  longer fails on the first run in a fresh checkout [A-07].
- Two undos of one import no longer tell the second one to force it; an undone import gets
  one answer by key and by id [A-08, N-28].
- A key that arrives twice in the TUI no longer ends it with a traceback [A-13].
- The tests now hold every value the TUI draws to being cleaned, at several screen sizes;
  ten ways of leaving one value uncleaned used to pass them [A-15].
- The TUI's pager answers keys while it wraps a long transcript, and quitting no longer waits
  for it [A-14].
- An offline copy of a session no longer hides a mounted copy of the same session [A-17].
- An index that cannot be used is kept aside as `index.json.unusable`, damaged entries are
  left out one by one, the temp files of a killed save are removed, and a scan no longer
  writes back a view older than an import's [A-19].
- `verify` reports a store that appeared since the last deep scan as stale, not as a
  difference [A-20].
- `--debug` reaches the TUI, the tools the TUI starts no longer inherit `PYTHONPATH`, and
  `tui` without a terminal is one line and exit 2 instead of hanging [A-21].
- Undoing imports that share a directory leaves no empty directories behind, in either
  order; a Grok import always uses the tool's own group name; a Grok `summary.json` with a
  byte order mark imports [A-24].
- A cached time that is not a time no longer stops `list` or the TUI [A-26].
- Quitting the TUI is immediate during a scan, a folder listing or a transcript read; a
  mount under way is cancelled and its `udisksctl` does not outlive the program [A-02].
- `scan` prints the warnings about the cache itself; `open` of a foreign session that cannot
  be imported says to continue it as a new session [A-25].
- `map D: ~/dir` expands `~` [N-20]; `open --dry-run` says when a folder's name holds
  characters printed as spaces [N-22]; a Claude import with `--to` a link names the project
  directory after the real path [N-23].
- `uninstall.sh --purge` also takes the program's files out of the directories
  `AGENT_SESSIONS_STATE` and `AGENT_SESSIONS_CACHE` name, the index's `.unusable` copy and
  lock included [N-24].
- The launcher says so under a Python older than 3.11 [N-25]; `tui --help` is the full help
  [N-13].
- A log with a wider mode is tightened to 0600 [N-26]; a message that names a path that is
  not UTF-8 is no longer dropped from the log [A-03].
- In the TUI: a long suggested folder shows its end [N-32]; a typed folder with `..` is
  checked and used as the same path [N-33]; `~` is the program's home [N-34]; `m` on a drive
  that is not attached says so at once, and a typed NUL is refused in plain words [N-37].

### Changed

- `install.sh` no longer replaces a link or a desktop launcher that is not its own (it says
  so; `--force` replaces them), treats a relative `XDG_DATA_HOME` as unset, and says what pip
  and `update-desktop-database` write [A-22, N-11].
- The cache, state and configuration directories are tightened to 0700 when they exist
  wider [A-25].
- The help's examples use key starts that reach into the id, and the help says that a start
  has to; every help line fits 78 columns [A-25, N-04].
- The TUI formats ages, sizes and times with the same code as the command line: a time
  without a zone is UTC in both, and 999,950 bytes are `1.0 MB` [A-26, N-36].
- `make dist` and `make dist-check` build and try the sdist and the wheel; the sdist carries
  no tests; CI has time limits, a coverage floor of 94 % and a package job; `make lint` works
  in a checkout whose path holds `[` or `]` [A-23, N-15, N-30].
- The test suite can no longer write the user's own state, cache or home, whatever the
  environment holds, and has tests for the three mutations the audit found unguarded [A-23].
- The desktop launcher has `TryExec=omarchy-launch-tui`, and the wrong `Curses` classifier
  is gone [N-15].
- The documents say what the code does: SPEC, SECURITY, FORMATS, JSON.md and the README
  [N-01 to N-10, N-12, N-14].

## [0.2.0] - 2026-10-13

### Security

- A session whose id is not in the form its tool uses (a UUID) is no longer listed,
  resumed or imported; before, an id such as `--config=…` from a foreign drive could reach
  the tool's command line as an option.
- Text from a session (titles, folders, transcripts, warnings) is cleaned of terminal
  control and text-direction characters before it is printed, logged or put into a
  notification; `--json` output escapes them instead.
- A named pipe, a device or a directory named like a session file no longer hangs a scan:
  only regular files are read, and opening never waits.
- Every read from another drive is bounded: one line up to 32 MiB, whole files up to a
  cap, deeply nested JSON caught; what is over a limit is skipped with a warning.
- SQLite databases from other drives are opened with hardened settings, and one whose
  schema holds a view, a trigger or a virtual table is refused.
- An import can only write below this system's own store, whatever names the source
  holds, and never replaces a file that exists.
- An imported copy is read back before the import counts as done; when it is not the
  planned session, everything is removed again.
- The index, the import journal, the log and exported transcripts are readable by their
  owner only (0600 in 0700 directories).
- A command on `PATH` that would download and install a tool when run is never started;
  such a tool counts as not installed.

### Added

- Sessions of a drive that is not mounted stay in the list, marked `offline`.
- `mount <drive>` mounts a remembered drive with `udisksctl` and scans it; `forget <drive>`
  drops a drive that is not mounted from the index.
- `map` prints the drive letters and sets or removes one by hand; Claude's `.claude.json`
  is used as further evidence for which drive a letter stands for.
- `import --to DIR` imports a session into a folder you choose, also when its own folder
  is gone or was never recorded.
- Grok sessions can be imported for any folder name (the tool's own directory naming is
  reproduced, including its hashed form for very long paths).
- Antigravity conversations can be imported with `import --experimental`.
- `unimport` takes an import back: it removes exactly what the import created and refuses
  when the copy was used since, unless `--force` is given.
- `export` writes a transcript as Markdown or JSON lines, to stdout or to a private file.
- `continue` starts a tool as a new session that carries an old one on, in the session's
  own tool or another (`--with`), from an exported transcript.
- The TUI can do all of it: mount (`m`), import into a typed folder (`t`), continue (`c`),
  undo an import (`u`), and it shows whether and how each foreign session can be imported.
- A log at `~/.local/state/agent-sessions/agent-sessions.log`, and `--debug`.
- `list --mounted` leaves the offline sessions out.
- `pip install '.[tui]'` gives a working `agent-sessions` command that starts the TUI.
- `uninstall.sh`, with `--purge` for the program's own cache, state and configuration.
- `README.md`, `docs/JSON.md` (every `--json` output), `docs/SECURITY.md`,
  `docs/ADDING_A_PROVIDER.md` and this file.
- Examples in the `--help` of the commands that need them.

### Changed

- The import journal is now `imports.jsonl`, appended under a lock, and records every file
  an import created. An old `imports.json` is still read.
- An imported session is resumed in the folder it was imported for.
- A terminal is found on desktops without `omarchy-launch-tui`: `xdg-terminal-exec`,
  `$TERMINAL`, or the first installed of a list of known terminals;
  `AGENT_SESSIONS_TERMINAL` overrides all of them.
- Whether a tool is installed is decided from the program's files, not from a command of
  that name being on `PATH`; `status` reports each tool's state.
- `status` lists every remembered drive with its state, letter and volume id.
- Ctrl-C, SIGTERM and SIGHUP end a command cleanly with one line (exit 130, 143, 129), and
  an import that was under way is rolled back first.
- An unexpected error prints one line that names the log instead of a traceback.
- The top-level `--help` lists the commands in groups, and every help text fits 80 columns.
- `agent-sessions` with no arguments on a terminal starts the TUI from an installed
  package too, not only through the checkout's launcher script.
- Messages that the TUI shows no longer name command-line options; the command line adds
  its own hint.
- The TUI says `starting <tool>…` while a continued session is being started.
- `install.sh` checks the Python version and the `venv` module first, installs development
  tools only with `--dev`, and warns when `~/.local/bin` is not on `PATH`.
- The index file has a new version (2). An index written by 0.1.0 is converted when it is
  first read; nothing it remembers is lost.

### Fixed

- Two imports running at the same time no longer lose one another's journal record.
- A Codex import that failed its check no longer leaves files and a journal record behind.
- A file an import creates for a line it appends is private like the rest of the copy.
- Times outside 1990 to 2100 are no longer shown as dates, and a number is no longer
  guessed to be seconds or milliseconds where its source fixes the unit.
- Text patterns that could take minutes on crafted input no longer do.
- `status` no longer lists tools as installed that are only a wrapper on `PATH`.
- `agent-sessions tui`, run through the installed command, no longer says "not implemented".

## [0.1.0] - 2026-10-12

The first working version (phase 1).

### Added

- Discovery of session stores on every mounted drive: known locations first, then a
  complete walk of each drive, with an index that re-reads only sessions that changed.
- Readers for Claude Code, Codex, Grok, Antigravity and Gemini CLI.
- `scan`, `list`, `status`, `verify` (the index against an independent walk of the disks).
- `show`: a session's transcript as plain text.
- `open`: resume a session of this system's store in a new terminal window, in its folder.
- `import`: copy a Claude or Codex session, or a Grok session whose folder name has only
  letters, digits, slashes and spaces, from another drive into this system's store.
- Windows drive letters resolved from evidence, with a hand-written override file.
- The TUI: provider tabs, list, detail pane, filter, transcript pager, resume, import,
  rescan; and a desktop launcher.
- `install.sh`: a private `.venv` with Textual, a link in `~/.local/bin`, the launcher.
- Reads that leave no trace on other drives: no access-time updates, no SQLite sidecar
  files.
