# File map

- `CLAUDE.md` — rules and commands for every agent working here
- `docs/SPEC.md` — goals, architecture, CLI contract, TUI; phase 2 (production pass): threat model, tool
  detection, journal, logs, exit codes
- `docs/JSON.md` — every `--json` output of the CLI, field by field, and its ordering; pinned by
  `tests/test_json_contract.py`
- `docs/REVIEW.md` — the production-readiness review (findings R-01 to R-14)
- `docs/PLAN.md` — task list and status
- `docs/FORMATS.md` — per-provider on-disk formats, resume commands, import recipes, store discovery,
  drive letters, ground-truth counts, open questions
- `bin/agent-sessions` — launcher script; no logic
- `install.sh` — symlink the launcher into `~/.local/bin`
- `Makefile` — `test`, `test-tui`, `lint`, `format`, `typecheck`, `coverage` (every suite in one run), `dev`
  (creates `.venv` with Textual and the development tools), `check` (lint, typecheck and both suites)
- `pyproject.toml` — packaging (setuptools, `src` layout, extras `tui` and `dev`, the `agent-sessions`
  entry point) and the ruff, mypy and coverage configuration
- `.github/workflows/ci.yml` — CI: lint, typecheck and both suites on Python 3.11 to 3.14 (Linux); engine
  suite on Windows, allowed to fail until the port is done
- `src/agent_sessions/__init__.py` — package marker, `__version__`
- `src/agent_sessions/model.py` — `Session` (`for_store`, `from_dict`; `offline`: listed from the cache, its
  drive is not mounted), `Store`, `Candidate`, `make_key`, `UUID_PATTERN` (the default session id grammar)
- `src/agent_sessions/fsutil.py` — our own locations (`home`, `cache_file`, `cache_dir`, `state_dir`, `config_dir`,
  `log_file`, `lock_file`, env overrides) and trace-free reads (`open_ro`, `subdirs`, `listdir_ro`: `O_NOATIME` where the
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
  records of an old `imports.json` before them, a torn line skipped with a warning), `append` (lock, one
  write on an appending descriptor, fsync), `open_for_append`/`write` for a caller that holds the lock
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
  dedupe, order), `where`, scan policy; `fresh` (cache-free index for `verify`)
- `src/agent_sessions/providers/` — one adapter per provider, plus the registry
- `src/agent_sessions/providers/__init__.py` — registry: `register`, `all_providers`, `get`; imports the five adapters
- `src/agent_sessions/providers/base.py` — `Provider` contract (with `id_pattern`, `valid_id`, `read_back`, the
  `resumable` property, `evidence_files` and `path_evidence`: recorded folders beyond the sessions' own, for
  the path map), `NotSupported`, shared helpers (`iter_jsonl`, `read_lines` and `JSONL_LINE_MAX`,
  `read_small`, `open_sqlite_ro`, `parse_ts` (nothing outside 1990 to 2100; `unit` for epoch numbers),
  `clean_title`, `open_ro`), warnings (`warn`, `collect_warnings`)
- `src/agent_sessions/providers/{claude,codex,grok,antigravity,gemini}.py` — the adapters: store signature,
  `candidates`, `parse`, `transcript`, `resume_argv`, and `import_plan` (Claude, Codex and Grok; Antigravity
  and Gemini raise `NotSupported` with the reason); Claude also gives path evidence (the `projects` keys of
  the `.claude.json` beside the store)
- `src/agent_sessions/blake3.py` — BLAKE3 of an input of up to 1,024 bytes (one chunk; the plain hash and
  nothing else): `digest`, `hexdigest`. What the Grok adapter names a long group directory with
  (`providers.grok.group_name`)
- `src/agent_sessions/importer.py` — copy a foreign session into the native store: plans, the checks
  (`check`, `contained`), `execute` (stage, place, verify, append, journal record with a manifest; all
  under the state directory's lock, rolled back on any failure), `sweep_staging`
- `src/agent_sessions/launch.py` — build and run the resume command (platform layer): `plan`, `launch`, the
  `starter`/`notifier` hooks; and `tool_state` (`ToolState`: is the tool behind a command installed, or is
  the command a wrapper that would install it; read from the wrapper's text and mise's installs, no
  program run, remembered per run, `forget_tools`)
- `src/agent_sessions/api.py` — facade used by the CLI and the TUI: `load_index`, `scan`, `list_sessions`,
  `status`, `verify`, `find_session`, `transcript`, `open_session`, `can_import`, `import_plan`,
  `import_report`, `import_session`; the drives: `volumes`, `find_volume`, `mount_drive`, `forget_drive`,
  `set_letter`, `remove_letter`; what needs a session's files refuses an offline one (`Offline`); what goes
  into the log is written here (scan summary and warnings, imports, launches, mounts, forgotten drives,
  unexpected exceptions)
- `src/agent_sessions/cli.py` — argparse entry: `scan`, `list`, `open`, `import`, `show`, `status`, `verify`,
  `mount`, `forget`, `map` (`tui` is started by the launcher, not here), the global flags `--version` and
  `--debug`, the output helpers that make text safe (`_ask` for the one question), and what ends a command:
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
- `src/agent_sessions/errors.py` — `ApiError`, `NotFound`, `LaunchFailed`, `ImportFailed`, `Offline` (the
  drive of a session is not mounted), `MountFailed` (re-exported by `api`)
- `tests/fixture_world.py` — `World`: fake home plus two fake drives built from the fixtures, launches and
  notifications recorded; `tree_state`, `age_access_times`, `files_below`
- `tests/test_launch.py` — launch plans per provider, folder gone, foreign refused, `--here`, hooks
- `tests/test_json_contract.py` — every `--json` output against the tables of `docs/JSON.md`: key sets,
  types, nullability, the documented orderings, escapes, byte-identical repeats
- `tests/test_tools.py` — `launch.tool_state` on a fake PATH and a fake mise tree: the wrappers' text and
  what is not one, where the executable may lie, the install directory's name, `MISE_DATA_DIR` and
  `XDG_DATA_HOME`, the per-run cache, and the answer in the index, `status` and `launch.plan`
- `tests/test_blake3.py` — `blake3` against the BLAKE3 team's test vectors (every input length they list up
  to 1,024 bytes), the three folder names Grok made on this machine, the refusal of a longer input
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
- `src/agent_sessions/tui/__init__.py` — package marker; imports no Textual; `run()`, the "run ./install.sh" hint
- `src/agent_sessions/tui/__main__.py` — `python -m agent_sessions.tui`: run the app, exit 1 with the hint when
  Textual is missing
- `src/agent_sessions/tui/engine.py` — `Engine`: the object the app calls the engine through (forwards to
  `agent_sessions.api`, looked up at call time), `EngineError`; tests pass a fake in its place
- `src/agent_sessions/tui/view.py` — pure view logic, no Textual: rows, ages, display width and clipping,
  column fitting, filter, tabs, selection by key, small-terminal layout, status and prompt texts, key table
  (`KEYMAPS`), help, transcript wrapping
- `src/agent_sessions/tui/app.py` — `SessionsApp`, `MainScreen` (workers for load, scan, import, open,
  `can_import`), `SessionList` and `TranscriptView` (line-drawn, virtual), `TabLabel`, `HelpScreen`,
  `TranscriptScreen` (the pager)
- `src/agent_sessions/tui/app.tcss` — every colour, border and spacing value; the widget tree it styles is
  listed at its top
- `requirements-tui.txt` — the pinned Textual version `install.sh` installs into `.venv`
- `tests/tui_support.py` — `FakeEngine` (records calls, never launches or imports; refuses an offline
  session the way the engine does), fake sessions, `screen_text`, `settle`
- `tests/test_tui_view.py` — every function of `tui/view.py`, `tui/engine.py`, the launcher and import rules;
  runs on the system Python
- `tests/test_tui_app.py` — the app under Textual's pilot with the fake engine (`make test-tui`; skips itself
  without Textual)
