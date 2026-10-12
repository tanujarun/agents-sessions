# Changelog

What changed for someone who uses agent-sessions. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). Neither version has been tagged or published,
so the version headings link to nothing: 0.1.0 is the state of the repository at commit
`c3029a8`, 0.2.0 the state after the production pass described in `docs/SPEC.md`
("Phase 2").

Nothing in either version has been run against a real agent tool yet; the README lists what
is still owed ("What has not been tried on a real tool").

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
