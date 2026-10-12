# File map

- `README.md` — for someone who has never seen the project: what it is, what it supports, install
  and uninstall, the TUI keys, the CLI by task, concepts, the files and environment variables, logs and
  troubleshooting, safety, what was never run on a real tool, development
- `CHANGELOG.md` — what changed for a user, per version (0.1.0: phase 1; 0.2.0: the production pass)
- `CLAUDE.md` — rules and commands for every agent working here
- `docs/SPEC.md` — goals, architecture, CLI contract, TUI; phase 2 (production pass): threat model, tool
  detection, journal, logs, exit codes
- `docs/JSON.md` — every `--json` output of the CLI, field by field, and its ordering, and the lines of
  `export --format json`; pinned by `tests/test_json_contract.py`
- `docs/REVIEW.md` — the production-readiness review (findings R-01 to R-14)
- `docs/PLAN.md` — task list and status
- `docs/SECURITY.md` — the threat model, each defence with the module that implements it and the test
  that proves it, and what is not defended
- `docs/ADDING_A_PROVIDER.md` — the adapter contract (`providers/base.py`) attribute by attribute, what
  the index enforces, the steps to add a reader, the state of the four tools without one
- `docs/FORMATS.md` — per-provider on-disk formats, resume commands, import recipes, store discovery,
  drive letters, ground-truth counts, open questions
- `bin/agent-sessions` — launcher script; no logic
- `install.sh` — set up from this checkout: `.venv` with the pinned Textual, the link in `~/.local/bin`,
  the desktop launcher; checks the Python version and `venv` first; `--dev` adds the development tools
- `uninstall.sh` — take the link and the desktop launcher back, each only when it is this checkout's;
  `--purge` also removes the cache, state and configuration directories (asks, or `--yes`)
- `Makefile` — `test`, `test-tui`, `lint`, `format`, `typecheck`, `coverage` (every suite in one run), `dev`
  (creates `.venv` with Textual and the development tools), `check` (lint, typecheck and both suites)
- `pyproject.toml` — packaging (setuptools, `src` layout, extras `tui` and `dev`, the `agent-sessions`
  entry point, readme, keywords and classifiers; no license field yet) and the ruff, mypy and coverage
  configuration
- `.github/workflows/ci.yml` — CI: lint, typecheck and both suites on Python 3.11 to 3.14 (Linux); engine
  suite on Windows, allowed to fail until the port is done
- `src/agent_sessions/__init__.py` — package marker, `__version__`
- `src/agent_sessions/model.py` — `Session` (`for_store`, `from_dict`; `offline`: listed from the cache, its
  drive is not mounted), `Store`, `Candidate`, `make_key`, `UUID_PATTERN` (the default session id grammar)
- `src/agent_sessions/fsutil.py` — our own locations (`home`, `cache_file`, `cache_dir`, `state_dir`, `config_dir`,
  `log_file`, `lock_file`, `handoff_dir`, env overrides), the check of a folder a user named
  (`existing_folder`: what `import --to` and `continue --in` take) and trace-free reads (`open_ro`, `subdirs`, `listdir_ro`: `O_NOATIME` where the
  platform has it). `open_ro` never waits and reads regular files only: `NotRegularFile` for a named pipe, a
  device, a directory. Our own files and directories (`open_private`: 0600, `make_private_dir`: 0700,
  `fsync_dir`) and the state directory's
  lock (`lock`, `LockTimeout`: `flock` on POSIX, a byte-range lock on Windows, re-entrant in one thread)
- `src/agent_sessions/logs.py` — the log `<state>/agent-sessions.log`: `logger()` (set up on first use; a
  rotating file, 1 MB and three older ones, created 0600 when the first message is written, silent when
  it cannot be written), `SafeFormatter` (every message through `textsafe.clean`, a traceback indented
  below it), `debug` (`--debug`: DEBUG level, a copy on stderr), `unexpected` (an exception with its
  traceback, once), `written`, `close`
- `src/agent_sessions/journal.py` — the import journal `<state>/imports.jsonl`: `read` (oldest first, the
  records of an old `imports.json` before them, a torn line skipped with a warning, or without one for a
  caller that reads it with every listing), `append` (lock, one
  write on an appending descriptor, fsync), `open_for_append`/`write` for a caller that holds the lock; the
  two kinds of record, `IMPORT` and `UNDO`; which imports still stand: `ident`, `not_undone` and
  `find_record` (the last import of a session that no undo took back)
- `src/agent_sessions/textsafe.py` — the one sanitiser for text from a store: `clean` (control and
  bidirectional characters become spaces; `multiline` keeps line feed and tab), `json_escape` (the same
  characters as `\uXXXX` in `--json` output), the set `UNSAFE`. Used by the CLI's output helpers, `clean_title`,
  `tui/view.clean` and the desktop notification
- `src/agent_sessions/mounts.py` — platform layer: `Mount` (with the filesystem's `uuid` and `fs_label`, read
  from `DEV_BY_UUID` and `DEV_BY_LABEL`), `list_mounts`, `mount_points`, `find_mount`, `parse_mountinfo`; the
  real-filesystem list `REAL_FSTYPES`; `AGENT_SESSIONS_MOUNTS` override; a volume by its UUID: `UUID_PATTERN`,
  `volume_state` (`mounted`, `attached`, `absent`; files only), `identity_from_mount_point` (a udisks mount
  point's name back to UUID and label), `mount_volume` (`udisksctl mount`, through the hook `starter`; the one
  `subprocess` use outside `launch.py`)
- `src/agent_sessions/discover.py` — `fast_pass`, `deep_pass`, `brute_pass` (the walk `verify` uses), the prune
  list `PRUNE`, `ALWAYS_DESCEND`, `SKIP_RULES` (stores found but not listed), `PROJECT_ROOT` (stores inside this
  checkout are skipped too)
- `src/agent_sessions/pathmap.py` — `normalise`, `PathMap` (drive letters by evidence per source mount,
  `REDIRECTS`, override file `~/.config/agent-sessions/pathmap.json`); editing that file: `override_file`,
  `set_override`, `remove_override` (validated, written atomically and 0600, an unreadable file refused)
- `src/agent_sessions/index.py` — `Index`: cache file (version 2; a version 1 file is migrated on load), scan
  (parse cache by signature, the path evidence of each store cached the same way), the `volumes` table (identity,
  drive letters and last-seen time of every volume that holds a remembered store; `forget`, `letter_for_mount`,
  a volume that came back at another path), `finalise` (derived session fields, offline sessions from the cache,
  dedupe, order; a native copy's local folder is the folder of its import, from the standing record of the
  journal), `where`, scan policy; `fresh` (cache-free index for `verify`)
- `src/agent_sessions/providers/` — one adapter per provider, plus the registry
- `src/agent_sessions/providers/__init__.py` — registry: `register`, `all_providers`, `get`; imports the five adapters
- `src/agent_sessions/providers/base.py` — `Provider` contract (with `id_pattern`, `valid_id`, `read_back`, the
  `resumable` property, `new_session_argv`: the command for a new session with a first prompt, `evidence_files` and `path_evidence`: recorded folders beyond the sessions' own, for
  the path map; `import_plan` and `import_session` with a chosen folder (`target`) and `experimental`;
  `undo_extras`: what the tool adds beside an imported session), `NotSupported`, `ExperimentalOnly` (an
  import that is done only when asked for; carries the warning to show first), shared helpers (`iter_jsonl`, `read_lines` and `JSONL_LINE_MAX`,
  `read_small`, `open_sqlite_ro`, `parse_ts` (nothing outside 1990 to 2100; `unit` for epoch numbers),
  `clean_title`, `open_ro`), warnings (`warn`, `collect_warnings`)
- `src/agent_sessions/providers/{claude,codex,grok,antigravity,gemini}.py` — the adapters: store signature,
  `candidates`, `parse`, `transcript`, `resume_argv`, `new_session_argv`, and `import_plan` (Claude, Codex and Grok; Antigravity
  as an experimental import only, a plain copy; Gemini raises `NotSupported` with the reason); Claude also
  gives path evidence (the `projects` keys of the `.claude.json` beside the store); Grok names its group
  directories (`group_name`, `group_slug`); Antigravity says what its tool adds beside a conversation
  (`undo_extras`)
- `src/agent_sessions/blake3.py` — BLAKE3 of an input of up to 1,024 bytes (one chunk; the plain hash and
  nothing else): `digest`, `hexdigest`. What the Grok adapter names a long group directory with
  (`providers.grok.group_name`)
- `src/agent_sessions/importer.py` — copy a foreign session into the native store: plans (`start_plan`; a
  folder the user chose, `chosen_folder`; `NoFolder` when the session's own is not found), the checks
  (`check`, `contained`, and `below` for one path), `execute` (stage, place, verify, append, journal record
  with a manifest; all under the state directory's lock, rolled back on any failure), `sweep_staging`
- `src/agent_sessions/unimport.py` — take an import back: the record is `journal.find_record`'s (the last import of a
  session that no undo took back), `plan` (what would be removed and left, read from the record's manifest
  and the disk; refuses a copy that was used since, a damaged record, a path outside the native store),
  `undo` (plan and removal under the state directory's lock, files then empty directories, an `undo` record
  in the journal)
- `src/agent_sessions/export.py` — a transcript as a document, made piece by piece: `markdown` (header, a
  section per turn, a line per tool call; cleaned text), `json_lines` (one object per turn, exact data),
  and `write` (a document into a file: temp file beside it, then moved into place, 0600, nothing replaced
  unless asked)
- `src/agent_sessions/launch.py` — build and run the resume command (platform layer): `plan`, `launch`, the
  `starter`/`notifier` hooks; which terminal opens the window (`terminal_command`: `AGENT_SESSIONS_TERMINAL`,
  `omarchy-launch-tui`, `xdg-terminal-exec`, `$TERMINAL`, the table `TERMINALS`); a new session that
  continues an old one (`FIRST_PROMPT`, `handoff_file`, `plan_new`: the prompt is made here and is the only
  element of the command that is not the adapter's own word, `launch_new`, `ContinuePlan`); and `tool_state` (`ToolState`: is the tool behind a command installed, or is
  the command a wrapper that would install it; read from the wrapper's text and mise's installs, no
  program run, remembered per run, `forget_tools`)
- `src/agent_sessions/api.py` — facade used by the CLI and the TUI: `load_index`, `scan`, `list_sessions`,
  `status`, `verify`, `find_session`, `transcript`, `export_transcript` and `export_file` (the transcript as
  Markdown or JSON lines; never into a session store), `continue_options` (`ContinueOptions`) and
  `continue_session` (the transcript to `<state>/handoff/`, old ones swept, then a tool started as a new
  session), `open_session`, `import_options` (`ImportOptions`: what an
  import of a session can be), `can_import`, `import_plan`, `import_report`, `import_session` (each with a
  chosen folder and the experimental flag), `unimport` (`UnimportReport`); the drives: `volumes`, `find_volume`, `mount_drive`, `forget_drive`,
  `set_letter`, `remove_letter`; what needs a session's files refuses an offline one (`Offline`); what goes
  into the log is written here (scan summary and warnings, imports, undos, launches, continues, mounts,
  forgotten drives, unexpected exceptions)
- `src/agent_sessions/cli.py` — argparse entry: `tui` (`_tui`: the one import of `agent_sessions.tui`
  outside it, made when the TUI is asked for; also what no arguments on a terminal starts), `scan`, `list`,
  `open`, `import` (`--to`, `--experimental`),
  `unimport`, `show`, `export` (`-o`, `--format`, `--force`), `continue` (`--with`, `--in`, `--dry-run`,
  `--here`), `status`, `verify`, `mount`, `forget`, `map`, the global flags `--version` and `--debug`;
  the help (`GROUPS`: the commands in four groups with their summaries, descriptions, examples,
  `_HelpFormatter`: 78 columns; `subcommands`); the flag hints the command line adds to an engine
  refusal (`_REMEDIES`, `_reason`, `_TO_HINT`); the output helpers that make text safe (`_ask` for a
  question), and what ends a command:
  exit codes, Ctrl-C and SIGTERM/SIGHUP (`_signals`), the `internal error` line
- `src/agent_sessions/tui/` — Textual front end: `app.py`, `view.py` (pure view logic), `app.tcss` (all styling)
- `share/agent-sessions.desktop` — desktop launcher for the TUI
- `tests/` — unittest suites
- `tests/__init__.py` — puts `src` on `sys.path`
- `tests/support.py` — `Sandbox` (fake home, cache, mounts with or without an identity, registry) and
  `FakeProvider` for the engine tests; `FakeDisks` (a fake `/dev/disk` tree, mount table and `udisksctl`,
  the mount program's start recorded) and `FakeProcess`; `mounts.starter` replaced for every suite, so no
  test can start a real mount;
  `write_jsonl`, `claude_transcript` (a minimal transcript written from scratch); child processes for the
  tests that need real ones (`python_child`, `launcher_child`, `finish`, `wait_for`, the slow-import driver
  `SLOW_IMPORT`); `FakePath` (what PATH holds for a test: programs and installing wrappers in a temp
  directory); `state_names`/`drop_state` (the state directory without its log); a temp
  `AGENT_SESSIONS_STATE` for anything that runs outside a `Sandbox`
- `tests/test_smoke.py` — imports, CLI skeleton, `make_key`; no engine module imports Textual or `tui`
  (checked in a fresh interpreter)
- `tests/test_entry.py` — how the program is entered: `cli.main` starts the TUI for `tui` and for no
  arguments on a terminal, the help and exit 2 without one; the hint when Textual is missing, in a
  checkout and in an installed package (`tui.checkout`); every other command loads neither `tui` nor
  Textual (a fresh interpreter, a temp home)
- `tests/test_packaging.py` — what keeps code, help and documents in step: no message outside `cli.py`
  names a command-line flag, the hint the CLI adds for every `remedy`; the help (groups, descriptions,
  examples that parse, 80 columns); every command in the README and in SPEC's CLI sections, every
  environment variable in the README, every module in this file, one version everywhere; the package
  metadata
- `tests/test_install_scripts.py` — `install.sh` and `uninstall.sh` run on a copy of the checkout with a
  throwaway `HOME` and stand-ins for `python3` and the desktop tools: a first and a second run, `--dev`,
  an old Python, no `venv`, the `PATH` warning, the desktop file's validation, what is not ours and is
  left, `--purge` with its question (a pty), `--yes`; a guard that the real home's installation is
  unchanged; `shellcheck` when it is installed
- `tests/test_base_helpers.py` — `iter_jsonl`, `open_sqlite_ro`, `parse_ts`, `clean_title`, warnings, model
- `tests/test_mounts.py` — mountinfo parsing and filtering, env override, `find_mount`
- `tests/test_volumes.py` — `mounts` on a fake `/dev/disk` tree and mount table: UUID and label of each
  mount, udev escapes, `identity_from_mount_point`, `volume_state`, `mount_volume` (the exact command,
  refusals before anything is run, the tool's failures, the timeout and the child's end), how the program
  is started (tried on a Python child)
- `tests/test_offline.py` — drives that are not mounted: offline sessions in the listing, WHERE, what is
  refused (`Offline`), `verify`, the `volumes` table and cache version 2, the migration of a version 1
  cache (wrong types included), a volume at another mount path, `api.volumes`, `find_volume`,
  `mount_drive`, `forget_drive`, and the commands `list --mounted`, `scan`, `status`, `mount`, `forget`
- `tests/test_letters.py` — path evidence: Claude's `.claude.json` (every wrong type, the size cap, what is
  not a regular file), how the index caches it by signature and feeds the path map; the `map` command and
  the override file (validation, atomic and private write, an unreadable file refused)
- `tests/test_discover.py` — fast, deep and brute pass, prune list, the five store signatures on the fixtures
- `tests/test_pathmap.py` — normalisation, letters by evidence, overrides, MSIX redirect
- `tests/test_index.py` — parse cache, scan policy, vanished mounts, derived fields, order, `verify`
- `tests/test_claude.py`, `tests/test_codex.py`, `tests/test_grok.py`, `tests/test_antigravity.py`, `tests/test_gemini.py` — one suite per adapter, on the fixtures
- `tests/test_cli.py` — every subcommand but `tui` through `cli.main`
- `tests/test_session_ids.py` — the session id grammar (`Provider.id_pattern`) in its three places: the
  index (fresh parses and cached entries), `launch.plan` (the id, and every argument that came from session
  data), `importer.check`
- `tests/test_textsafe.py` — `textsafe`, the JSON round trip, every CLI command on stores full of escape
  sequences, the rule that only the CLI's output helpers write to a stream, id-fallback titles, the
  notification text
- `tests/test_wrong_types.py` — all five adapters against records, JSON values and SQLite column values of
  the wrong type, on temp copies of every fixture store
- `tests/test_unreadable.py` — a directory inside a store at mode 000: the warning per adapter and through
  the index
- `tests/fixtures/README.md` — what each fixture is, expected parsed values and counts, leak check
- `tests/fixtures/<provider>/<native|windows>/` — sanitised real stores, laid out like a home directory
  (`claude/`, `codex/`, `grok/`, `antigravity/`, `gemini/`; SQLite fixtures are WAL-mode, open immutable)
- `tests/fixtures/claude/not-a-store/`, `tests/fixtures/gemini/native-no-chats/` — negative cases for `is_store`
- `scratch/` — throwaway investigation scripts (git-ignored): `walk.py`, `*_count.py`, `drives.py`, `build_fixtures.py`
- `src/agent_sessions/errors.py` — `ApiError` (`reason`: plain words that name no command-line flag;
  `remedy`: one of the `REMEDY_` words when asking in another way gets past the refusal), `NotFound`, `LaunchFailed`, `ImportFailed`, `UndoFailed` (an
  import could not be taken back; `no_record` and `differences` mark the two refusals a front end answers
  in its own way), `ExportFailed` (a transcript could not be written to a file), `Offline`
  (the drive of a session is not mounted), `MountFailed` (re-exported by `api`)
- `tests/fixture_world.py` — `World`: fake home plus two fake drives built from the fixtures, launches and
  notifications recorded; `tree_state`, `age_access_times`, `files_below`
- `tests/test_launch.py` — launch plans per provider, folder gone, foreign refused, `--here`, hooks; which
  terminal is used, table-driven on a fake PATH (every known terminal, the order, `$TERMINAL`,
  `AGENT_SESSIONS_TERMINAL`, none at all)
- `tests/test_json_contract.py` — every `--json` output and the lines of `export --format json` against the
  tables of `docs/JSON.md`: key sets, types, nullability, the documented orderings, escapes, byte-identical
  repeats
- `tests/test_tools.py` — `launch.tool_state` on a fake PATH and a fake mise tree: the wrappers' text and
  what is not one, where the executable may lie, the install directory's name, `MISE_DATA_DIR` and
  `XDG_DATA_HOME`, the per-run cache, and the answer in the index, `status` and `launch.plan`
- `tests/test_blake3.py` — `blake3` against the BLAKE3 team's test vectors (every input length they list up
  to 1,024 bytes), the three folder names Grok made on this machine, the refusal of a longer input
- `tests/test_unimport.py` — `unimport`: import then unimport leaves the fake home byte for byte what it was
  (every provider that can import, with and without a native store; Codex's one index line), which record
  is used, a copy that was used since, `--force`, what is left and why, the provider's extras, a journal
  that names paths outside the native store or is damaged, a record without a manifest, the lock (real
  processes), the undo record, the command and its question
- `tests/test_resume_folder.py` — an imported copy is resumed in the folder of its import: the chosen folder
  (Claude, Antigravity), the mapped one while the source drive is away, an import that was undone, records
  and journals that are malformed or unreadable (no failure, no warning), the journal read once per listing
- `tests/test_continue.py` — `continue`: every adapter's new-session command against FORMATS Q3, the plan
  (`launch.plan_new`: the prompt and nothing else from outside the adapter, the handoff file's name, the
  folder, a tool that is not installed), `continue_options`, `continue_session` (the transcript written
  private and replaced in one step, another tool, a foreign session, a hostile session, `--here`), every
  refusal, the sweep of old handoff files, the log, the command
- `tests/test_export.py` — `export`: the Markdown and the JSON lines (structure, what is not known, hostile
  values, laziness), `export.write` (private, whole or not at all, nothing replaced unless asked, paths
  that will not do), the facade on every provider's fixture, offline sessions, no write into a session
  store, the command
- `tests/test_importer.py` — imports per provider (bytes, rewrites, idempotence, collisions, roll back, free
  space, provenance, source times), `encode_cwd`, refusals
- `tests/test_import_folder.py` — imports into a folder the user chose (`target`, `import --to`): what is
  accepted as one (`importer.chosen_folder`), and what it changes per provider: Claude's directory name,
  Codex's four fields (`_CwdRewrite` with a recorded and a chosen folder), Grok's group and `info.cwd` for
  the real path; the record's `chosen_folder`; the command
- `tests/test_import_antigravity.py` — the experimental import of an Antigravity conversation against the
  table of FORMATS Q5: refused unless asked for, the three pieces and their order, every collision, every
  precondition (a `-wal` that is not empty, a sub-agent, no steps, an unreadable database, the IDE store),
  no folder needed, nothing written to an index, the read-back, the command
- `tests/test_import_options.py` — `api.import_options` for every kind of session (native, offline, imported,
  importable, folder gone or not recorded, Antigravity, Gemini, a bad id, no free space, a bad folder), that
  asking writes nothing, `can_import` held to the same answer, the warning `import --experimental` prints
- `tests/test_hostile_files.py` — named pipes, devices and directories named like session files (`open_ro`,
  whole scans in a child process under a timeout), over-long lines and the 200 MB line's memory ceiling, size
  caps of files read whole, deeply nested JSON
- `tests/test_sqlite_hardening.py` — `open_sqlite_ro` on files that are not trusted: the settings made before
  anything is read, schemas with a view, a trigger or a virtual table refused, truncated and scrambled
  databases as one skipped candidate each
- `tests/test_hostile_values.py` — text that used to cost minutes (unclosed tags, blank lines, an endless
  "time"), `between`, long message ids, the index's bounds on a session's folder, note and title
- `tests/test_interrupts.py` — Ctrl-C (exit 130, one line), SIGTERM and SIGHUP raised as an exception for
  the length of a command, the `internal error` line and `--debug`; a real child process signalled in the
  middle of an import's copy; the source disappearing and the disk filling up mid-copy (complete roll back)
- `tests/test_logs.py` — `logs` (nothing created until a message is written, modes, rotation, a log that
  cannot be written, cleaned messages and tracebacks, `--debug`) and what the facade logs (scan, warnings
  once per scan, import, launch, unexpected exceptions)
- `tests/test_journal.py` — `fsutil.lock` (threads, real processes, the lock file that goes again),
  `make_private_dir`, the journal (append and read, torn and foreign lines, the old `imports.json`, failed
  writes), and imports of different sessions started at once in processes of their own
- `tests/test_import_placing.py` — how a copy is put in place: `importer._place` (a file is linked, never
  renamed over; a directory renamed; names that appear at the last moment), an interrupt between a move and
  its note, directories flushed before the record, the lock around the whole of `execute`, and two real
  processes importing the same session and different ones
- `tests/test_import_safety.py` — what keeps an import inside the native store: `importer.contained`
  (destinations, paths below a unit, appended files; symbolic links inside the native store are followed),
  the names each adapter takes from the
  source; the read-back of a placed copy (`Provider.read_back`, the `verify` step of `importer.execute`) and
  the roll back when it fails
- `src/agent_sessions/tui/__init__.py` — package marker; imports no Textual; `run()`; what is said when
  Textual is missing (`install_hint`: `run ./install.sh in <checkout>` or `pip install 'agent-sessions[tui]'`)
  and how a checkout is told from an installed package (`checkout`, `CHECKOUT_FILES`)
- `src/agent_sessions/tui/__main__.py` — `python -m agent_sessions.tui`: run the app, exit 1 with the hint when
  Textual is missing
- `src/agent_sessions/tui/engine.py` — `Engine`: the object the app calls the engine through (forwards to
  `agent_sessions.api`, looked up at call time; `list_folder`: a directory's entries for the folder input,
  through `fsutil.listdir_ro`), `EngineError`; tests pass a fake in its place
- `src/agent_sessions/tui/view.py` — pure view logic, no Textual: rows, ages, display width and clipping,
  column fitting, filter, tabs, selection by key, small-terminal layout and key hints, status texts, the
  import verdict (`Verdict`, from `import_options`), every prompt's wording at every width (foreign row,
  mount, warning, tool, start, undo), the folder a user types (`expand_folder`, `complete_folder`), which
  refusal of an undo it is (`undo_refusal`), one key table per mode (`KEYMAPS`), help, transcript wrapping
- `src/agent_sessions/tui/app.py` — `SessionsApp`, `MainScreen` (workers for load, scan, import, open,
  `import_options`, mount, continue, undo, and the folder input's listing and check; `Question`: what the
  prompt on screen is about), `SessionList` and `TranscriptView` (line-drawn, virtual), `TabLabel`,
  `HelpScreen`, `TranscriptScreen` (the pager)
- `src/agent_sessions/tui/app.tcss` — every colour, border and spacing value; the widget tree it styles is
  listed at its top
- `requirements-tui.txt` — the pinned Textual version `install.sh` installs into `.venv`
- `tests/tui_support.py` — `FakeEngine` (records calls, never mounts, launches, imports or removes; refuses
  an offline session and an undo the way the engine does), fake sessions, `screen_text`, `settle`
- `tests/test_tui_view.py` — every function of `tui/view.py`, `tui/engine.py`, the launcher and import rules
  (one import of `tui` outside it, in `cli._tui`); runs on the system Python
- `tests/test_tui_app.py` — the app under Textual's pilot with the fake engine (`make test-tui`; skips itself
  without Textual)
