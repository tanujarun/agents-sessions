# Plan

Status key: `[ ]` open, `[~]` delegated, `[x]` accepted. Spec: `docs/SPEC.md`.

## 1. Scaffold — Light — after: none — [x]

Create the layout in SPEC "Architecture": `src/agent_sessions/` package with
empty-but-importable modules and the `Provider` base class + registry,
`bin/agent-sessions` (adds `src` to `sys.path`, calls `cli.main`), an argparse
CLI with every subcommand from SPEC "CLI contract" stubbed (exit 1,
"not implemented"), `install.sh` (symlink `bin/agent-sessions` to
`~/.local/bin/agent-sessions`, idempotent), `tests/` with one smoke test,
`Makefile` targets `test` (`python3 -m unittest discover -s tests -v`) and
`lint` (`python3 -m compileall -q src tests`). Stdlib only.
Verify: `make test`, `make lint`, `bin/agent-sessions --help`.

## 2. Format investigation — Heavy — after: 1 — [x]

Read-only study of the five providers' stores on Linux and on the Windows
profile. Produce `docs/FORMATS.md`: per provider the store signature, the file
layout, where id/title/cwd/timestamps/message count come from (exact JSON keys
or SQL), the verified resume command and how the tool finds a session at resume
time (cwd-scoped or global), what an import needs (files, fields to rewrite) or
why it is unsafe, quirks (Windows paths, encodings, WAL files). Also: drive
letter evidence for the path map, deep-walk timing per mount, and sanitised
fixtures under `tests/fixtures/<provider>/`.
Verify: orchestrator spot-checks claims against the real files.

## 3. Core: mounts, discovery, path map, index, CLI — Careful — after: 2 — [~]

Implement `model`, `mounts`, `discover`, `pathmap`, `index`, and the CLI
commands `scan`, `list`, `status`, `verify` against the adapter
contract (adapters may still be stubs; use a fake provider in tests). All of
SPEC "Discovery", "Read-only rule", "Path map" and the determinism rule.
Verify: unit tests with temp-dir fake mounts (spaces in names, unreadable dir,
vanished mount, nested store, symlink loop); `scan --deep` on the real machine
finishes and reports timing.

## 4a. Adapters: Claude, Codex — Careful — after: 3 — [ ]

`providers/claude.py`, `providers/codex.py` per FORMATS.md: `is_store`,
`list_sessions`, `resume_argv`. Tests on the fixtures, including a truncated
file and a non-UTF-8 line.
Verify: `list --provider claude --json` and `codex` counts equal a `find`
count on the real stores; titles sane.

## 4b. Adapters: Grok, Antigravity, Gemini — Careful — after: 3 — [ ]

Same as 4a for `providers/grok.py`, `providers/antigravity.py`,
`providers/gemini.py`. SQLite opened read-only/immutable as SPEC requires.
Verify: counts against the real stores; no `-wal`/`-shm` created on any drive.

## 5. Launch and import — Careful — after: 4a, 4b — [ ]

`launch.py`, `importer.py`, `import_session` in each adapter, CLI `open`,
`import`, `show`. All SPEC "Launch" and "Import" rules; `NotSupported` with a
reason where FORMATS.md says import is unsafe. Remove the stubbed `menu` subcommand. No model calls and no
interactive tool launches during the task: verify by the scanner listing the
imported copy as native and launchable, and by dry-run printing of the launch
command (`open --dry-run`). Imports during testing go to a temp native store
(env override), never the real one.
Verify: tests for idempotence, no-overwrite, atomic write, provenance.

## 6. TUI front end — Careful — after: 5 — [ ]

`tui/` (Textual) per SPEC "TUI" and "Stack": layout, keys, foreign-row prompt,
built-in transcript pager, scans in workers, resize handling, all styling in
`app.tcss`. `install.sh` creates `.venv` and installs a pinned Textual.
`agent-sessions` with no subcommand on a tty opens it. `share/agent-sessions.desktop`
plus the install step. The TUI calls engine functions only. Keep the view
logic (row formatting, column fitting, filtering, selection-by-key) in pure
functions in `tui/view.py` that are unit-tested without Textual.
Verify: unit tests for the pure functions; headless `run_test()` pilot tests
at 120x40, 80x24 and 40x10 that press keys and assert on the screen,
including an empty index and a resize; no real launch (the launcher is
injected and recorded).

## 7. Ground-truth audit — Medium — after: 5 — [ ]

An independent check that does not import the package: a throwaway script
that brute-force walks every mount and counts session files per provider by
the signatures in FORMATS.md, then compares with `list --json`; run `scan`
three times and diff; simulate a missing mount and a corrupt file. Report
every mismatch with paths.
Verify: orchestrator reruns the script.
