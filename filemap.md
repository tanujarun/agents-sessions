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
- `src/agent_sessions/model.py` — Session and Store dataclasses, session key
- `src/agent_sessions/mounts.py` — list real mounted filesystems (platform layer)
- `src/agent_sessions/discover.py` — find stores on mounts; prune list
- `src/agent_sessions/pathmap.py` — foreign path to local path
- `src/agent_sessions/index.py` — cache of stores and parsed sessions
- `src/agent_sessions/providers/` — one adapter per provider, plus the registry
- `src/agent_sessions/providers/__init__.py` — registry: `register`, `all_providers`, `get`
- `src/agent_sessions/providers/base.py` — `Provider` base class, `NotSupported`
- `src/agent_sessions/importer.py` — copy a foreign session into the native store
- `src/agent_sessions/launch.py` — build and run the resume command (platform layer)
- `src/agent_sessions/cli.py` — argparse entry, all subcommands
- `src/agent_sessions/tui/` — Textual front end: `app.py`, `view.py` (pure view logic), `app.tcss` (all styling)
- `share/agent-sessions.desktop` — desktop launcher for the TUI
- `tests/` — unittest suites
- `tests/__init__.py` — puts `src` on `sys.path`
- `tests/test_smoke.py` — imports, CLI skeleton, `make_key`
- `tests/fixtures/README.md` — what each fixture is, expected parsed values and counts, leak check
- `tests/fixtures/<provider>/<native|windows>/` — sanitised real stores, laid out like a home directory
  (`claude/`, `codex/`, `grok/`, `antigravity/`, `gemini/`; SQLite fixtures are WAL-mode, open immutable)
- `tests/fixtures/claude/not-a-store/`, `tests/fixtures/gemini/native-no-chats/` — negative cases for `is_store`
- `scratch/` — throwaway investigation scripts (git-ignored): `walk.py`, `*_count.py`, `drives.py`, `build_fixtures.py`
