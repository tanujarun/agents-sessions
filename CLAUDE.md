# agent-sessions

Python 3.11+. Engine: standard library only. TUI (`src/agent_sessions/tui/`):
Textual, from the project `.venv` (`./install.sh` creates it).

## Commands

- Test: `make test` (`python3 -m unittest discover -s tests -v`); TUI tests
  need `.venv/bin/python` and skip themselves when Textual is absent
- Lint: `make lint` (`ruff check`, `ruff format --check`, `compileall`); `make format` applies the
  formatter; `make typecheck` runs `mypy`; `make coverage`; `make check` runs lint, typecheck and
  both suites. These need the tools in `.venv`: `make dev` (installs `.[tui,dev]` editable)
- Run: `bin/agent-sessions` (TUI) or `bin/agent-sessions <scan|list|open|import|unimport|show|status|verify|mount|forget|map>`
- TUI tests: `make test-tui` (needs `.venv`; about 80 s)
- Install: `./install.sh` (creates `.venv` with Textual, symlinks into
  `~/.local/bin`, installs the desktop launcher)

## Rules

- Read-only outside the native stores, `~/.cache/agent-sessions/`,
  `~/.local/state/agent-sessions/` and `~/.config/agent-sessions/pathmap.json` (`map` only). Never write to a store on another drive,
  including the Windows profile under `/run/media/tanuj/`.
- Open SQLite files only through `providers.base.open_sqlite_ro` (in-memory
  copy; never a plain `sqlite3.connect`). A scan must not create or touch
  `-wal`/`-shm` files or move access times on another drive.
- `subprocess` only in `launch.py`, and in `mounts.py` for `udisksctl mount`. Tests never mount or
  unmount anything (replace `mounts.starter`). Read files on other drives through
  `fsutil.open_ro`, never `shutil.copy` or a plain `open`.
- A store that is not the native one is hostile: a session id must `fullmatch` `Provider.id_pattern`,
  and an import writes only below the native root (`importer.contained`).
- An undo removes only what its journal record lists; every file must pass `importer.below`,
  and a directory goes only when empty.
- Text from a store is shown only through `textsafe` (`cli._out`/`_err`/`_json`, `view.clean`); no `print` in `cli.py`.
  The log is written through `logs.logger()` only (its formatter cleans every message).
- Foreign bytes are read bounded (`open_ro`, `iter_jsonl`, `read_small`, `open_sqlite_ro`); where JSON is
  parsed, catch `RecursionError` with `ValueError`.
- Do not leave store-shaped directories (fake homes, copied fixtures)
  readable under `scratch/`: a real scan of `/home` would list them.
- Tests never touch the real stores for writing. Use temp dirs and the env
  overrides `AGENT_SESSIONS_HOME` (fake home), `AGENT_SESSIONS_CACHE`,
  `AGENT_SESSIONS_STATE`, `AGENT_SESSIONS_MOUNTS` (fake mount list).
- Do not start an agent CLI interactively or make a model call (no
  `claude -p`, `codex exec`, …). `--help` and `--version` are fine.
- Do not send keyboard or mouse input to the desktop session.
- Platform-specific code only in `mounts.py` and `launch.py`; everything else
  must also run on Windows later (pathlib, no shelling out). `textual` is
  imported only under `tui/`, and no engine module imports `tui`.
- Formats come from `docs/FORMATS.md`. If the files disagree with it, fix the
  doc in the same change; never guess a field.
- Output order is deterministic: sort everything you emit.
- Update `filemap.md` when you add or move a file.

## Docs

`docs/SPEC.md` (what and why), `docs/PLAN.md` (tasks), `docs/FORMATS.md`
(per-provider on-disk formats), `docs/JSON.md` (the `--json` outputs; a test
holds the code to it, so change both together), `filemap.md` (where things are).
