# File map

- `CLAUDE.md` — rules and commands for every agent working here
- `docs/SPEC.md` — goals, architecture, CLI contract, TUI
- `docs/PLAN.md` — task list and status
- `docs/FORMATS.md` — per-provider on-disk formats, resume commands, import recipes, store discovery,
  drive letters, ground-truth counts, open questions
- `bin/agent-sessions` — launcher script; no logic
- `install.sh` — symlink the launcher into `~/.local/bin`
- `Makefile` — `test`, `test-tui`, `lint`, `format`, `typecheck`, `coverage`, `dev` (creates `.venv` with
  Textual and the development tools), `check` (lint, typecheck and both suites)
- `pyproject.toml` — packaging (setuptools, `src` layout, extras `tui` and `dev`, the `agent-sessions`
  entry point) and the ruff, mypy and coverage configuration
- `.github/workflows/ci.yml` — CI: lint, typecheck and both suites on Python 3.11 to 3.14 (Linux); engine
  suite on Windows, allowed to fail until the port is done
- `src/agent_sessions/__init__.py` — package marker, `__version__`
- `src/agent_sessions/model.py` — `Session` (`for_store`, `from_dict`), `Store`, `Candidate`, `make_key`,
  `UUID_PATTERN` (the default session id grammar)
- `src/agent_sessions/fsutil.py` — our own locations (`home`, `cache_file`, `cache_dir`, `state_dir`, `config_dir`,
  `log_file`, `lock_file`, env overrides) and trace-free reads (`open_ro`, `subdirs`, `listdir_ro`: `O_NOATIME` where the
  platform has it). `open_ro` never waits and reads regular files only: `NotRegularFile` for a named pipe, a
  device, a directory. Our own directories (`make_private_dir`: 0700, `fsync_dir`) and the state directory's
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
- `src/agent_sessions/mounts.py` — platform layer: `Mount`, `list_mounts`, `mount_points`, `find_mount`,
  `parse_mountinfo`; the real-filesystem list `REAL_FSTYPES`; `AGENT_SESSIONS_MOUNTS` override
- `src/agent_sessions/discover.py` — `fast_pass`, `deep_pass`, `brute_pass` (the walk `verify` uses), the prune
  list `PRUNE`, `ALWAYS_DESCEND`, `SKIP_RULES` (stores found but not listed), `PROJECT_ROOT` (stores inside this
  checkout are skipped too)
- `src/agent_sessions/pathmap.py` — `normalise`, `PathMap` (drive letters by evidence per source mount,
  `REDIRECTS`, override file `~/.config/agent-sessions/pathmap.json`)
- `src/agent_sessions/index.py` — `Index`: cache file, scan (parse cache by signature), `finalise` (derived
  session fields, dedupe, order), scan policy; `fresh` (cache-free index for `verify`)
- `src/agent_sessions/providers/` — one adapter per provider, plus the registry
- `src/agent_sessions/providers/__init__.py` — registry: `register`, `all_providers`, `get`; imports the five adapters
- `src/agent_sessions/providers/base.py` — `Provider` contract (with `id_pattern`), `NotSupported`, shared
  helpers (`iter_jsonl`, `read_lines` and `JSONL_LINE_MAX`, `read_small`, `open_sqlite_ro`, `parse_ts`,
  `clean_title`, `open_ro`), warnings (`warn`, `collect_warnings`)
- `src/agent_sessions/providers/{claude,codex,grok,antigravity,gemini}.py` — the adapters; attributes and store
  signature done, `candidates`/`parse`/`resume_argv`/`import_session` not yet (tasks 4a, 4b, 5)
- `src/agent_sessions/importer.py` — copy a foreign session into the native store: plans, the checks
  (`check`, `contained`), `execute` (stage, place, verify, append, journal record with a manifest; all
  under the state directory's lock, rolled back on any failure), `sweep_staging`
- `src/agent_sessions/launch.py` — build and run the resume command (platform layer)
- `src/agent_sessions/api.py` — facade used by the CLI and the TUI: `load_index`, `scan`, `list_sessions`,
  `status`, `verify`, `find_session`, `transcript`, `open_session`, `can_import`, `import_plan`,
  `import_report`, `import_session`; what goes into the log is written here (scan summary and warnings,
  imports, launches, unexpected exceptions)
- `src/agent_sessions/cli.py` — argparse entry; `scan`, `list`, `status`, `verify` done, `tui`, `open`, `import`,
  `show` stubbed
- `src/agent_sessions/tui/` — Textual front end: `app.py`, `view.py` (pure view logic), `app.tcss` (all styling)
- `share/agent-sessions.desktop` — desktop launcher for the TUI
- `tests/` — unittest suites
- `tests/__init__.py` — puts `src` on `sys.path`
- `tests/support.py` — `Sandbox` (fake home, cache, mounts, registry) and `FakeProvider` for the engine tests;
  `write_jsonl`, `claude_transcript` (a minimal transcript written from scratch); child processes for the
  tests that need real ones (`python_child`, `launcher_child`, `finish`, `wait_for`, the slow-import driver
  `SLOW_IMPORT`); `state_names`/`drop_state` (the state directory without its log); a temp
  `AGENT_SESSIONS_STATE` for anything that runs outside a `Sandbox`
- `tests/test_smoke.py` — imports, CLI skeleton, `make_key`
- `tests/test_base_helpers.py` — `iter_jsonl`, `open_sqlite_ro`, `parse_ts`, `clean_title`, warnings, model
- `tests/test_mounts.py` — mountinfo parsing and filtering, env override, `find_mount`
- `tests/test_discover.py` — fast, deep and brute pass, prune list, the five store signatures on the fixtures
- `tests/test_pathmap.py` — normalisation, letters by evidence, overrides, MSIX redirect
- `tests/test_index.py` — parse cache, scan policy, vanished mounts, derived fields, order, `verify`
- `tests/test_claude.py`, `tests/test_codex.py`, `tests/test_grok.py`, `tests/test_antigravity.py`, `tests/test_gemini.py` — one suite per adapter, on the fixtures
- `tests/test_cli.py` — `scan`, `list`, `status`, `verify` through `cli.main`
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
- `src/agent_sessions/errors.py` — `ApiError`, `NotFound`, `LaunchFailed`, `ImportFailed` (re-exported by `api`)
- `tests/fixture_world.py` — `World`: fake home plus two fake drives built from the fixtures, launches and
  notifications recorded; `tree_state`, `age_access_times`, `files_below`
- `tests/test_launch.py` — launch plans per provider, folder gone, foreign refused, `--here`, hooks
- `tests/test_importer.py` — imports per provider (bytes, rewrites, idempotence, collisions, roll back, free
  space, provenance, source times), `encode_cwd`, refusals
- `tests/test_hostile_files.py` — named pipes, devices and directories named like session files (`open_ro`,
  whole scans in a child process under a timeout), over-long lines and the 200 MB line's memory ceiling, size
  caps of files read whole, deeply nested JSON
- `tests/test_sqlite_hardening.py` — `open_sqlite_ro` on files that are not trusted: the settings made before
  anything is read, schemas with a view, a trigger or a virtual table refused, truncated and scrambled
  databases as one skipped candidate each
- `tests/test_hostile_values.py` — text that used to cost minutes (unclosed tags, blank lines, an endless
  "time"), `between`, long message ids, the index's bounds on a session's folder, note and title
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
- `tests/tui_support.py` — `FakeEngine` (records calls, never launches or imports), fake sessions,
  `screen_text`, `settle`
- `tests/test_tui_view.py` — every function of `tui/view.py`, `tui/engine.py`, the launcher and import rules;
  runs on the system Python
- `tests/test_tui_app.py` — the app under Textual's pilot with the fake engine (`make test-tui`; skips itself
  without Textual)
