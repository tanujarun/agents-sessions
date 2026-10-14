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

## 3. Core: mounts, discovery, path map, index, CLI — Careful — after: 2 — [x]

Implement `model`, `mounts`, `discover`, `pathmap`, `index`, and the CLI
commands `scan`, `list`, `status`, `verify` against the adapter
contract (adapters may still be stubs; use a fake provider in tests). All of
SPEC "Discovery", "Read-only rule", "Path map" and the determinism rule.
Verify: unit tests with temp-dir fake mounts (spaces in names, unreadable dir,
vanished mount, nested store, symlink loop); `scan --deep` on the real machine
finishes and reports timing.

## 4a. Adapters: Claude, Codex — Careful — after: 3 — [x]

`providers/claude.py`, `providers/codex.py` per FORMATS.md: `is_store`,
`list_sessions`, `resume_argv`. Tests on the fixtures, including a truncated
file and a non-UTF-8 line.
Verify: `list --provider claude --json` and `codex` counts equal a `find`
count on the real stores; titles sane.

## 4b. Adapters: Grok, Antigravity, Gemini — Careful — after: 3 — [x]

Same as 4a for `providers/grok.py`, `providers/antigravity.py`,
`providers/gemini.py`. SQLite opened read-only/immutable as SPEC requires.
Verify: counts against the real stores; no `-wal`/`-shm` created on any drive.

## 5. Launch and import — Careful — after: 4a, 4b — [x]

`launch.py`, `importer.py`, `import_session` in each adapter, CLI `open`,
`import`, `show`. All SPEC "Launch" and "Import" rules; `NotSupported` with a
reason where FORMATS.md says import is unsafe. Remove the stubbed `menu` subcommand. No model calls and no
interactive tool launches during the task: verify by the scanner listing the
imported copy as native and launchable, and by dry-run printing of the launch
command (`open --dry-run`). Imports during testing go to a temp native store
(env override), never the real one.
Verify: tests for idempotence, no-overwrite, atomic write, provenance.

## 6. TUI front end — Careful — after: 4a, 4b — [x]

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

## 7. Ground-truth audit — Medium — after: 4a, 4b — [x]

An independent check that does not import the package: a throwaway script
that brute-force walks every mount and counts session files per provider by
the signatures in FORMATS.md, then compares with `list --json`; run `scan`
three times and diff; simulate a missing mount and a corrupt file. Report
every mismatch with paths.
Verify: orchestrator reruns the script.

## 8. Audit fixes — Medium — after: 5, 7 — [x]

Defects found by task 7: a wrong-typed record field drops a whole Claude or
Codex session; an unreadable directory inside a store drops its sessions
without a warning; of two copies of one session the path-first one is listed,
not the newest; a corrupt or missing cache makes `list` print nothing without
saying why; stores inside this project's own tree get listed by a real scan;
`C:` stays unresolved on a system volume with no path evidence; import staging
directories are never swept.
Verify: a regression test per defect; real scan still 135 sessions, `verify` passes.

# Phase 2 (SPEC "Phase 2: production pass")

## 9. Production readiness review — Heavy — after: 8 — [x]

Independent review of the phase 1 code: `docs/REVIEW.md` (R-01 to R-14).

## 10. Limits investigation — Heavy — after: 8 — [x]

FORMATS.md "Phase 2 findings": Grok group naming (sandbox), mounting, first-prompt
argv, Antigravity lookup, tools with no data, `D:`, re-pointing at import.

## 11. Tooling baseline — Medium — after: 8 — [x]

`pyproject.toml`, ruff (lint and format), mypy, coverage, Makefile targets, CI file;
suites pass on Python 3.11 and 3.14.

## 12. Security hardening — Careful — after: 9, 11 — [x]

REVIEW R-01, R-02, R-03, R-04, R-05, R-08, R-09, plus import destination containment.
SPEC "Threat model". A regression test per finding.

## 13. Robustness hardening — Careful — after: 12 — [x]

REVIEW R-06, R-07, R-10 to R-14; the import journal (`imports.jsonl`, lock, manifest);
no-overwrite placement; interrupts and signals; logs; tool detection (SPEC "Tool
detection", "Logs"); cache and state file modes; the latent `None` comparisons.

## 14. Offline drives and mounting — Careful — after: 10, 13 — [x]

SPEC "Offline drives" and "Drive letters": volume identity, cache version 2, offline
sessions, `mount`, `forget`, `map`, Claude path evidence.

## 15. Import retarget and undo — Careful — after: 14 — [x]

SPEC "Import": `--to`, Grok group naming with BLAKE3, Antigravity experimental import,
`unimport`, `import_options`.

## 16. Continue as new session — Careful — after: 15 — [x]

SPEC "Continue as a new session": `export`, `continue`, `new_session_argv`, handoff
files, terminal fallback.

## 17. TUI for new actions — Careful — after: 16 — [x]

SPEC "TUI additions".

## 18. Packaging and docs — Medium — after: 17 — [x]

SPEC "Packaging": entry point starts the TUI, `uninstall.sh`, README, CHANGELOG,
`docs/JSON.md`, `docs/SECURITY.md`, help texts, version 0.2.0.

## 19. Acceptance audit — Heavy — after: 18 — [x]

Fresh eyes on the whole change against SPEC "Definition of done (phase 2)", on the
real machine and in throwaway homes. `docs/AUDIT.md` (A-01 to A-27, N-01 to N-37), at
commit 371cf8a.

## 20. Audit fixes — Careful — after: 19 — [x]

Done as the four fix tasks below and the final pass (task 25). What became of each finding:
`docs/AUDIT.md`, section 6.

## 21. Audit fixes: import and undo integrity — Careful — after: 19 — [x]

A-04, A-05, A-06, A-08, A-11, A-18, A-24, N-23, N-27, N-28: the `begin` and `rollback`
records and `importer.recover`, `importer.fits`, incomplete undos, the plan under the lock,
the Codex name line, the lock file and the journal as regular files. Merged at f2a93a8.

## 22. Audit fixes: reading and output — Careful — after: 19 — [x]

A-02 (engine), A-03, A-09, A-10, A-16, A-17, A-18 (log), A-19, A-20, A-25, A-26, A-27,
N-16 to N-22, N-26: the read budget and bounds, `textsafe` and the CLI's own encoding,
`tool_state` failing closed, `export -o`, the cache loaded entry by entry, `verify`'s stale
stores, `fmt.py`. Merged at 1f2dba9.

## 23. Audit fixes: tests, scripts, build and CI — Medium — after: 19 — [x]

A-07, A-22, A-23, N-15, N-24, N-30: the memory test, `install.sh` and `uninstall.sh`,
test isolation, the file map from the tracked files, `MANIFEST.in`, `make dist`,
`make dist-check`, CI time limits, the coverage floor and the package job. Merged at
a7d3634.

## 24. Audit fixes: TUI and launcher — Careful — after: 19 — [x]

A-01, A-02 (TUI), A-12, A-13, A-14, A-15, A-21, A-26 (TUI), N-13, N-25, N-32 to N-37: the
`-P` bootstrap, quitting at once, `SafeInput`, keys that arrive twice, the pager's slices,
hostile text in every TUI value. Merged at 1b8b59e.

## 25. Final pass — Careful — after: 21, 22, 23, 24 — [x]

The loose ends the fix tasks left in code (the mount cancelled on quit, one copy of the
helpers, dead code, the TUI test isolation exception, the tests for M27 and M79,
`uninstall.sh --purge` for the new file names, the help's key examples and width), every
document brought in line with the code, the disposition of every finding in
`docs/AUDIT.md` section 6, and the verification on the real machine (read-only) and in a
throwaway home.
