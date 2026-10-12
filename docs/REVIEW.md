# Production-readiness review

Independent review of `agent-sessions` at `main` / `c3029a8`. Read-only; findings only, no
fixes. Experiments ran in throwaway temp dirs outside the repo with the env overrides
(`AGENT_SESSIONS_HOME`/`_CACHE`/`_STATE`/`_MOUNTS`, `AGENT_SESSIONS_NOTIFY=0`); no real store,
cache, state or desktop was touched, and no agent CLI was started beyond `--help`.

The code is careful and well-tested on the dimensions the authors anticipated (read-only
discipline, atomic writes, malformed records, unreadable dirs). The gaps are at the trust
boundary the SPEC itself names: a session **id**, **cwd** and **file type** coming from another
machine's files are treated as safe, and they are not. Ordered by severity.

---

### R-01 · critical · the resume command is built from an unvalidated session id → flag/code injection

`providers/*.resume_argv` and `launch.plan` / `launch._directory`.

Every adapter's `resume_argv` returns `[binary, "<flag>", s.id]` with **no check that `s.id`
is the id shape that tool uses** (`claude.py:307`, `codex.py:374`, `grok.py:347`,
`antigravity.py:417`, `gemini.py:333`). For Codex/Grok/Antigravity the id is taken from inside
the foreign store (`session_meta.payload.id`, `summary.json` `info.id`, the `.db` filename), so an
attacker chooses it. The id becomes its own element of the argv passed to
`omarchy-launch-tui … <tool> resume <id>`. Resume is the tool's whole purpose and the TUI offers
"import and resume" as one keystroke, so a user only has to open an attacker's session (a Windows
profile, a synced folder, an extracted archive on any mounted drive) and press Enter.

The launcher is spawned with a Popen **list** (`launch._detached`), so there is no shell and no
shell-metacharacter injection — good — but there is nothing stopping the id from being a **flag**.
For Codex `-c/--config` sets arbitrary config (`codex resume --help`: sandbox permissions, shell
environment policy, and MCP server commands are all config keys), i.e. arbitrary command execution
when the session is resumed. Grok exposes `--always-approve` / `--sandbox`.

Failure scenario `[reproduced]`: a foreign Codex thread with
`session_meta.payload.id = "--config=shell_environment_policy.inherit=all"` and an innocent title
("fix the bug"). After the files land in `~/.codex` (see R-04), `agent-sessions open --dry-run`
prints the command the TUI would run:

```
$ python3 e06_argv_e2e.py
listed foreign: codex id= '--config=shell_environment_policy.inherit=all' title= 'fix the bug' …
RESUME COMMAND the tool would run:
    cd …/work && omarchy-launch-tui --app-id=org.omarchy.codex codex resume --config=shell_environment_policy.inherit=all
```

(Same construction with a native store built directly: `grok --resume --yolo`,
`codex resume '--config=mcp_servers.x={command="sh",args=[…]}'` — both reproduced via
`open --dry-run`.)

Fix: validate each id against that provider's id grammar **before** it is accepted as a session
(reject a candidate whose id is not, e.g., a lowercase UUID for Claude/Codex/Grok/Antigravity) and,
belt-and-braces, pass `--` before the id in every `resume_argv` (and in the launcher wrapper) so
no id can be read as an option. Validation at parse time also fixes R-04.

### R-02 · high · scan hangs forever on a FIFO or device named like a transcript (no regular-file check)

`fsutil.open_ro`, reached from `providers/base.iter_jsonl` and `_sqlite_in_memory`; candidate
discovery in each adapter's `candidates`.

`candidates()` accepts any directory entry whose **name** matches the transcript pattern and
`parse()` opens it with `open_ro` → `os.open(path, O_RDONLY[,O_NOATIME])`. Opening a FIFO
read-only blocks until a writer appears; a character device (e.g. a copy of `/dev/zero` named
`<uuid>.jsonl`) would feed bytes forever. There is no `stat.S_ISREG` guard on the read path
(the import path has one in `importer.file_copy`/`tree`, but scanning does not). A foreign store
can be on any mount including one under `/home` (btrfs/ext4 support FIFOs and device nodes), which
the SPEC explicitly allows.

Failure scenario `[reproduced]`: a foreign Claude store with one real transcript plus a FIFO
`projects/C--Users-u/<uuid>.jsonl`. `agent-sessions scan --deep` never returns:

```
$ python3 e03_files.py
=== E3: a FIFO named <uuid>.jsonl in a foreign Claude store
subprocess.TimeoutExpired: Command '[… 'scan', '--deep']' timed out after 25 seconds
```

In the TUI the scan runs in a worker that checks its cancel event only between candidates, so the
blocked `os.open` is uncancellable: the spinner stays forever and quit cannot stop it. Affects
Claude/Codex/Grok/Gemini (JSONL) and Antigravity (`.db` read through `open_ro`).

Fix: on the read path, `os.lstat` the candidate and skip (with a warning) anything that is not a
regular file; and/or open with `O_NONBLOCK` for the probe. Pair with a cancel check that does not
rely on reaching the next candidate.

### R-03 · high · control characters / terminal escapes from a foreign store reach the terminal via the CLI

`cli._out` (no sanitising), `cli._project`, `cli._list`, `cli._show`, `cli._status`; and the
uncleaned id-fallback titles in `claude._title` (`:226`), `grok._fields` (`:320`),
`antigravity._fields` (`:311`), `gemini._read` (`:310`), `codex.parse` (`:363`).

`clean_title` strips C0/C1/DEL from titles, but (a) the **id-fallback** title path returns
`id[:8]` without `clean_title`, and (b) the CLI prints `cwd`, the PROJECT segment, the `id` and the
`key` **verbatim**. `_out` only guards `UnicodeEncodeError`; it never removes control bytes. So a
crafted foreign session makes `agent-sessions list`, `list --keys` and `show` emit raw escape
sequences to the user's terminal (title/status spoofing, `ESC]52` clipboard write, `ESC[2J` clear,
and worse on terminals with dangerous OSC handlers).

Failure scenario `[reproduced]`: a foreign Claude cwd `C:\Users\u\proj␛]0;PWNED-TITLE␇␛[2J` and a
Grok session whose id is `␛c␛[2Jabcdef-not-a-uuid`:

```
$ python3 e01_escape.py
--- list: raw ESC bytes in stdout: 4
    '11d  C:  claude  what is this  proj\x1b]0;PWNED-TITLE\x07\x1b[2J'
    '6w   C:  grok    \x1bc\x1b[2Jab      u'
--- show …: raw ESC bytes in stdout: 4
    'reply \x1b]52;c;ZWNobyBwd25lZA==\x07 \x1b[31mred'   (OSC 52 clipboard write + colour)
```

The TUI is safe here (it routes everything through `view.clean`/`view.clip`, verified). `--json`
output is also safe to pipe (JSON-escaped), but raw to a terminal it still carries the bytes.

Fix: sanitise every value the CLI prints to a tty through one helper (reuse `view.clean`'s
approach, or apply `clean_title`-style stripping to cwd/project/id/key, not only titles); run the
id-fallback title through `clean_title` in every adapter.

### R-04 · high · a Codex import that "fails" verification still commits files + provenance, leaving an orphan native session

`providers/base.Provider.import_session` vs `codex.Codex.candidates`/`parse`; `importer.execute`;
`api.import_report`.

`import_session` copies the files (`importer.execute` finishes and writes `imports.json`), then
re-reads the native store and looks for a candidate whose `id_hint == plan.session_id`. For Codex,
`candidates()` sets `id_hint = <uuid in the file name>` (`match.group(2)`) while `parse` sets the
session id from `session_meta.payload.id`. When those differ, the loop finds nothing and raises
`ImportFailed("the files were copied but the copy does not read as a session")` — **after** the
copy and provenance are already on disk. The next `scan` lists the orphan as a native session.
In normal data `payload.id == filename-uuid`, so this is masked today, but it is exactly what the
R-01 id makes diverge, and any future rename/cross-version rollout would trip it too.

Failure scenario `[reproduced]`: the R-01 Codex import reports `rc 1` ("… does not read as a
session"), yet `~/.codex/sessions/2026/08/29/rollout-…jsonl` and `state/imports.json` are present
afterwards (`e02_argv.py` output), and a later `scan` lists it as native and resumes it (R-01).

Fix: key Codex candidates by the thread id the session will actually carry (or verify the copy by
re-parsing, not by `id_hint`); and make `execute` roll back (or make the caller roll back) when
post-copy verification fails, so a "failed" import never leaves files or a provenance record.

### R-05 · medium · an unbounded single JSONL line is read whole and the title path copies it several times

`providers/base.iter_jsonl` (`fh.readline()` with no cap) and `claude._prompts` /
`clean_title`.

JSONL is read with `readline()`, which materialises a whole line regardless of length; the title
path then does `raw.replace("\n"," ").strip()` and regex-searches over the full string before
`clean_title` caps it at 80 chars. FORMATS records a 2.74 MB longest line in real data, but a
foreign file is unbounded. A multi-GB line OOMs the scanner (and the TUI scan worker).

Failure scenario `[reproduced]`: one 200 MB user-message line → the scanning child peaks at
~820 MB RSS (`e04_files.py`: `child peak RSS (kB): 842580`). Scales linearly to OOM.

Fix: cap the bytes read per line (skip and warn past a limit), and build titles/first-prompts from
a bounded prefix rather than the whole content string.

### R-06 · medium · concurrent imports lose provenance records (read-modify-write on imports.json)

`importer.execute` / `read_provenance` / `_record`.

`execute` calls `read_provenance()` (reads the whole list), appends its record, writes
`records + [record]` to a pid-named temp and `os.replace`s it in. Two imports that overlap both
read the same base list and the second rename wins, dropping the first's record. The copied files
survive (different paths), but the audit trail in `imports.json` silently loses an entry. The SPEC
calls out "two imports … scan during import"; the file copies are atomic and safe, only the
provenance is not.

Failure scenario `[by reading]`: imports A and B start together, both read `imports.json` = `[…n]`;
A writes `[…n, A]`, B writes `[…n, B]`; final file has B but not A.

Fix: append under an advisory lock (`fcntl.flock` on POSIX, best-effort elsewhere), or append a
single JSON line to a JSONL provenance file instead of rewriting a JSON array, or re-read inside the
lock just before writing.

### R-07 · medium · `list --json` output shape is undocumented and thus not a stable contract

`cli._list` → `Session.to_dict`; SPEC "CLI contract".

The SPEC promises `list --json` is "stable enough for scripts and a later Windows front end" and
byte-identical across scans, but the JSON schema (field names, types, nullability, that it is the
full `Session` dataclass via `asdict`) is written down nowhere. A consumer has only the dataclass
to reverse-engineer, and any field rename silently breaks them. Same for `scan --json`,
`status --json`, `import --dry-run`'s shape.

Failure scenario `[by reading]`: a Windows front end keys on `cwd_local`; a later refactor renames
it; nothing flags the break because no schema is pinned or tested as a contract.

Fix: document the `--json` object in SPEC/FORMATS and add a test that asserts the key set and types,
so a change to it is a deliberate, reviewed event.

### R-08 · low · untrusted SQLite opened without defensive/`trusted_schema` hardening

`providers/base._sqlite_in_memory` / `open_sqlite_ro`.

A ≤64 MB foreign `.db` is `deserialize`d into memory; `PRAGMA query_only = 1` is set (good, blocks
writes and SELECT cannot fire triggers), but `SQLITE_DBCONFIG_DEFENSIVE` and
`PRAGMA trusted_schema = OFF` are not, and the first statement
(`select count(*) from sqlite_master`) runs **before** `query_only`. A crafted/corrupt schema
(virtual-table module, malformed b-tree) could still provoke a crash in the SQLite layer that
`antigravity`'s `try/except sqlite3.Error` cannot catch (a C-level abort is not a Python
exception). Larger files fall back to `mode=ro&immutable=1`, same concern.

Failure scenario `[by reading]`: a corrupt page or a schema referencing a missing vtab module in a
foreign Antigravity `.db` aborts the process rather than being skipped with a warning.

Fix: set `trusted_schema=OFF`/defensive before any query where the API allows it, and treat a
worker crash as a skipped candidate (scan a `.db` out of process or wrap the read so one bad file
cannot take the whole scan down).

### R-09 · low · notify-send body is not sanitised

`launch._notify` / `launch._directory`.

The desktop-notification body is `LaunchFailed.reason`, which for a native session whose folder is
gone embeds `session.cwd` verbatim (`f"the project folder {session.cwd} …"`). `cwd` is foreign
(for an imported session) and can carry control bytes or notification-daemon markup. It is a single
argv element, so no option injection, but escapes/markup reach the notification UI.

Fix: run the reason through the same control-stripping helper before `notifier(reason)`.

### R-10 · low · appended `session_index.jsonl` is created world-readable (0644) while copies are 0600

`importer._append` (`0o644`) vs `FILE_MODE = 0o600`.

The module comment says "sessions are private; never wider than the tools write", and copied files
use 0600, but the Codex thread-name append creates `session_index.jsonl` at 0644 when it does not
exist. Minor (thread names only), but inconsistent with the stated privacy intent.

Fix: create the appended file with 0600, or document why index files are exempt.

### R-11 · low · `parse_ts` magnitude heuristic can misclassify epoch units

`providers/base._from_number`.

The unit (s/ms/µs) is inferred from magnitude. Values ≥1e17 are treated as "ns→µs" via
`int(value)//1000`; a genuine epoch-microseconds value, or an out-of-range int, is bucketed by size
rather than by source. FORMATS only documents s/ms ints, so this is latent, but a foreign file with
an odd numeric timestamp yields a silently wrong `started`/`updated` (hence wrong ordering/age).

Fix: where the provider format fixes the unit, parse it per-provider rather than by magnitude; keep
the heuristic only as a last resort and clamp the plausible year range.

### R-12 · low · a test asserts global interpreter state and fails when run after the TUI suite

`tests/test_smoke.py::test_engine_does_not_import_textual`.

It asserts `"textual" not in sys.modules` after importing the engine. Run alone (engine suite) it
passes; run in one process **after** `test_tui_app` has imported Textual, it fails — the full
`unittest discover` over all of `tests/` reports one failure for this reason, not a product bug.

Failure scenario `[reproduced]`: `coverage run -m unittest discover -s tests` (TUI venv) →
`FAIL: test_engine_does_not_import_textual … 'textual' unexpectedly found`. Engine-only run: clean.

Fix: check in a fresh subprocess (`subprocess.run([python, -c, "import agent_sessions.api, sys; …"])`)
instead of the shared `sys.modules`.

### R-13 · nit · `tui` via `cli.main` reports "not implemented"

`cli._COMMANDS` has no `tui` entry, so `cli.main(["tui"])` → `_not_implemented` (exit 1). It works
only because `bin/agent-sessions` intercepts `tui` first. Anyone importing/calling the CLI directly
(or a future packaging that bypasses the launcher) gets a confusing error. Make `cli` handle `tui`
explicitly (print the "use the launcher / venv" hint) or document that the launcher owns it.

### R-14 · nit · stale docs / comments

`filemap.md` still says `candidates`/`parse`/`resume_argv`/`import_session` are "not yet"
implemented for the adapters (line ~29) and that `cli` has `open`/`import`/`show` "stubbed"
(~35) — all are implemented. `providers/base.Provider.import_session`'s contract names
`importer.Exists`, which it no longer raises directly (the plan path does). `docs/SPEC.md`'s TUI
mock-up and the "Facts" section are fine but should be cross-checked against FORMATS once more
before release. These mislead the next reader.

---

## Checked and found sound

- **No shell in launch.** `launch._detached` uses `subprocess.Popen` with a list and
  `start_new_session=True`; `cd` is a real `chdir`, not a shell string. No shell-metachar injection
  (the R-01 risk is argv-level flags only).
- **Read-only discipline.** Reads go through `fsutil.open_ro` (`O_NOATIME`, EPERM fallback);
  SQLite is `deserialize`d from bytes or opened `mode=ro&immutable=1`; `signature` uses `stat`
  only. The atime/WAL-sidecar tests (`test_base_helpers`, `fixture_world.age_access_times`) back
  this up.
- **Atomic, rolled-back imports.** Staging dir + `os.replace` in plan order, free-space check with
  headroom, ENOSPC/OSError rollback of placed units and created dirs, `sweep_staging` of dead-pid
  leftovers, byte-for-byte rewrite verification (`importer.only_changed`), symlinks/special files
  excluded (`importer.tree`, `file_copy` `S_ISREG`). (The one gap is R-04's post-copy path.)
- **Malformed input during parse.** `iter_jsonl` skips blank/non-object/unparseable lines and BOM,
  decodes surrogate-safe; the protobuf decoder bounds varints (≤10 bytes) and length-delimited
  reads and stops at truncation (`test_antigravity.test_truncated_protobuf_blobs` passes);
  wrong-typed records are dropped not fatal (`test_wrong_types`, 15 cases, all pass).
- **Cache robustness.** A missing/corrupt/other-version cache yields an empty index with
  `load_error`, never a crash (`Index.load`); `reader_id` invalidates parses when adapter code
  changes.
- **Discovery.** mountinfo parsing handles `\040`/octal escapes, spaces and per-device dedup;
  prune list is data in one place; `.gemini` always descended; desktop/test/fixture stores skipped
  and counted.
- **Path map.** Letters by evidence per source mount, never substitutes a parent, case-insensitive
  segment match returning the on-disk spelling, MSIX redirect, override file validated.
- **CLI edges.** `BrokenPipeError` handled (`list | head` → exit 0, reproduced); non-UTF-8 stdout
  via `backslashreplace`; exit codes 0/1/2 per SPEC; `verify` returns 1 on mismatch.
- **Portability.** `src` compiles and the engine suite (675 tests) passes under **Python 3.11.17**
  as well as 3.14; POSIX-only calls are behind `getattr`/`os.name` guards or in `mounts`/`launch`;
  `sqlite3.Connection.deserialize` exists in 3.11 and is `hasattr`-guarded anyway.
- **TUI safety.** Control chars neutralised via `view.clean`/`view.clip`; worker results marshalled
  to the UI thread; SIGTERM/SIGHUP handled; small-terminal layout and resize covered.

## Test coverage (engine suite, branch coverage, `coverage==7.16.2`, Python 3.14)

Run: `coverage run --branch --source=src -m unittest discover -s tests` from an outside venv
(engine-only; TUI suite needs Textual). 675 tests, 71 skipped, pass.

| module | stmt cover | notable uncovered / untested branches |
|---|---|---|
| model.py | 100% | — |
| errors.py | 100% | — |
| pathmap.py | 99% | override-collision list row (`:167`) |
| index.py | 96% | some `_adopt` validation raises (`:171,175,185`), cancel-mid-parse paths |
| launch.py | 96% | home-missing raise (`:87`), exec rollback (`:128-129`) |
| mounts.py | 96% | non-linux `_read_mountinfo`, override error path |
| providers/antigravity.py | 97% | pbtxt escape edges, a couple of `_log_turns` branches |
| providers/claude.py | 97% | `_recorded_version` error paths |
| providers/codex.py | 96% | `_index_line` OSError, segment-rewrite byte-offset path |
| providers/gemini.py | 97% | legacy-`.json` size cap, `$set` edges |
| providers/grok.py | 97% | `_load_json` error branches, `_group_cwd` OSError |
| base.py | 96% | `_sqlite_in_memory` failure fallbacks (`:196-198`) |
| discover.py | 92% | `_Found.check` OSError (`:144-145`), brute-pass error/progress branches |
| importer.py | 92% | rewrite `preview` error, append-newline branch, rollback edges |
| api.py | 94% | `verify` stale branches (`:183-184,198`), `_native_copy` edges |
| cli.py | 87% | tty-only progress callbacks, `_show`/`_import` branches |
| tui/view.py | 99% | — |
| tui/engine.py | 95% | `EngineError` on missing attr |
| tui/app.py | **0% engine-only / 95% with Textual** | measured under the TUI venv (`make test-tui`): 95% stmt, 94% total |
| tui/__main__.py | 0% engine-only | exercised only indirectly; add a direct test |

Important uncovered behaviour: the FIFO/special-file read path (R-02), oversized-line handling
(R-05), concurrent-import provenance (R-06), and corrupt-SQLite crash handling (R-08) are not
tested at all — these are the hostile-input paths that matter most.

## Ten test cases I would add first

1. A foreign session whose id is a flag (`--config=…`, `--yolo`) is **rejected at parse** (or the
   resume argv is proven safe); assert `resume_argv`/`open --dry-run` never yields a token starting
   with `-` from session data. (R-01)
2. A FIFO, and a char-device copy, named `<uuid>.jsonl` / `summary.json` / `<id>.db` in a foreign
   store: `scan` finishes within a timeout and warns, does not hang. (R-02)
3. `list`, `list --keys`, `show`, `status` with a foreign cwd/title/id full of `\x1b`, `\x07`,
   C1 bytes: assert no control byte reaches stdout when it is a tty. (R-03)
4. Codex import where `session_meta.payload.id` != filename uuid: import either succeeds cleanly or
   leaves **nothing** in the native store and no provenance record. (R-04)
5. A single JSONL line of N hundred MB: scan stays under a memory bound and still lists the session
   (title taken from a bounded prefix). (R-05)
6. Two `import_report` calls racing on different sessions: both provenance records survive. (R-06)
7. A corrupt / truncated-mid-page SQLite `.db` and one with a view/vtab in the schema: parse skips
   it with a warning, process does not abort. (R-08)
8. A `--json` contract test: assert the exact key set and types of one `list --json` row, so any
   field change is deliberate. (R-07/R-11)
9. `cli.main(["tui"])` behaviour is defined (hint, not "not implemented"); and the engine-imports-
   no-Textual check runs in a subprocess so it is order-independent. (R-12/R-13)
10. An import whose source directory is removed mid-copy (drive unplugged) and one that hits ENOSPC:
    assert full rollback (no partial native session, staging swept).

## Leftover temp paths

None. All experiment directories were created with `tempfile.TemporaryDirectory`/a cleaning
context manager and removed; `/tmp/as-review-*` is empty after the run. The review venvs live only
under the session scratchpad.
