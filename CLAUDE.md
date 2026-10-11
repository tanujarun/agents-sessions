# agent-sessions

Python 3.11+. Engine: standard library only. TUI (`src/agent_sessions/tui/`):
Textual, from the project `.venv` (`./install.sh` creates it).

## Commands

- Test: `make test` (`python3 -m unittest discover -s tests -v`); TUI tests
  need `.venv/bin/python` and skip themselves when Textual is absent
- Lint: `make lint` (`python3 -m compileall -q src tests`)
- Run: `bin/agent-sessions` (TUI) or `bin/agent-sessions <scan|list|open|import|show|status|verify>`
- Install: `./install.sh` (symlinks into `~/.local/bin`)

## Rules

- Read-only outside the native stores, `~/.cache/agent-sessions/` and
  `~/.local/state/agent-sessions/`. Never write to a store on another drive,
  including the Windows profile under `/run/media/tanuj/`.
- Open every SQLite file, native ones too, with
  `file:<quoted path>?mode=ro&immutable=1`. A scan must not create or touch
  `-wal`/`-shm` files.
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
(per-provider on-disk formats), `filemap.md` (where things are).
