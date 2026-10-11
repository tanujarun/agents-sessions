# File map

- `CLAUDE.md` — rules and commands for every agent working here
- `docs/SPEC.md` — goals, architecture, CLI contract, TUI
- `docs/PLAN.md` — task list and status
- `docs/FORMATS.md` — per-provider on-disk formats, resume commands, import recipes, store discovery,
  drive letters, ground-truth counts, open questions
- `bin/agent-sessions` — launcher script; no logic
- `install.sh` — symlink the launcher into `~/.local/bin`
- `Makefile` — `test`, `lint`
- `src/agent_sessions/__init__.py` — package marker, `__version__`
- `src/agent_sessions/model.py` — `Session` (`for_store`, `from_dict`), `Store`, `Candidate`, `make_key`
- `src/agent_sessions/fsutil.py` — our own locations (`home`, `cache_file`, `cache_dir`, `state_dir`, `config_dir`, env overrides)
  and trace-free reads (`open_ro`, `subdirs`, `listdir_ro`: `O_NOATIME` where the platform has it)
- `src/agent_sessions/mounts.py` — platform layer: `Mount`, `list_mounts`, `mount_points`, `find_mount`,
  `parse_mountinfo`; the real-filesystem list `REAL_FSTYPES`; `AGENT_SESSIONS_MOUNTS` override
- `src/agent_sessions/discover.py` — `fast_pass`, `deep_pass`, `brute_pass` (the walk `verify` uses), the prune
  list `PRUNE`, `ALWAYS_DESCEND`, `SKIP_RULES` (stores found but not listed)
- `src/agent_sessions/pathmap.py` — `normalise`, `PathMap` (drive letters by evidence per source mount,
  `REDIRECTS`, override file `~/.config/agent-sessions/pathmap.json`)
- `src/agent_sessions/index.py` — `Index`: cache file, scan (parse cache by signature), `finalise` (derived
  session fields, dedupe, order), scan policy; `fresh` (cache-free index for `verify`)
- `src/agent_sessions/providers/` — one adapter per provider, plus the registry
- `src/agent_sessions/providers/__init__.py` — registry: `register`, `all_providers`, `get`; imports the five adapters
- `src/agent_sessions/providers/base.py` — `Provider` contract, `NotSupported`, shared helpers (`iter_jsonl`,
  `open_sqlite_ro`, `parse_ts`, `clean_title`, `open_ro`), warnings (`warn`, `collect_warnings`)
- `src/agent_sessions/providers/{claude,codex,grok,antigravity,gemini}.py` — the adapters; attributes and store
  signature done, `candidates`/`parse`/`resume_argv`/`import_session` not yet (tasks 4a, 4b, 5)
- `src/agent_sessions/importer.py` — copy a foreign session into the native store
- `src/agent_sessions/launch.py` — build and run the resume command (platform layer)
- `src/agent_sessions/api.py` — facade used by the CLI and the TUI: `load_index`, `scan`, `list_sessions`,
  `status`, `verify`
- `src/agent_sessions/cli.py` — argparse entry; `scan`, `list`, `status`, `verify` done, `tui`, `open`, `import`,
  `show` stubbed
- `src/agent_sessions/tui/` — Textual front end: `app.py`, `view.py` (pure view logic), `app.tcss` (all styling)
- `share/agent-sessions.desktop` — desktop launcher for the TUI
- `tests/` — unittest suites
- `tests/__init__.py` — puts `src` on `sys.path`
- `tests/support.py` — `Sandbox` (fake home, cache, mounts, registry) and `FakeProvider` for the engine tests
- `tests/test_smoke.py` — imports, CLI skeleton, `make_key`
- `tests/test_base_helpers.py` — `iter_jsonl`, `open_sqlite_ro`, `parse_ts`, `clean_title`, warnings, model
- `tests/test_mounts.py` — mountinfo parsing and filtering, env override, `find_mount`
- `tests/test_discover.py` — fast, deep and brute pass, prune list, the five store signatures on the fixtures
- `tests/test_pathmap.py` — normalisation, letters by evidence, overrides, MSIX redirect
- `tests/test_index.py` — parse cache, scan policy, vanished mounts, derived fields, order, `verify`
- `tests/test_claude.py`, `tests/test_codex.py`, `tests/test_grok.py`, `tests/test_antigravity.py`, `tests/test_gemini.py` — one suite per adapter, on the fixtures
- `tests/test_cli.py` — `scan`, `list`, `status`, `verify` through `cli.main`
- `tests/fixtures/README.md` — what each fixture is, expected parsed values and counts, leak check
- `tests/fixtures/<provider>/<native|windows>/` — sanitised real stores, laid out like a home directory
  (`claude/`, `codex/`, `grok/`, `antigravity/`, `gemini/`; SQLite fixtures are WAL-mode, open immutable)
- `tests/fixtures/claude/not-a-store/`, `tests/fixtures/gemini/native-no-chats/` — negative cases for `is_store`
- `scratch/` — throwaway investigation scripts (git-ignored): `walk.py`, `*_count.py`, `drives.py`, `build_fixtures.py`
