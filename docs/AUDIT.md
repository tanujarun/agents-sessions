# Acceptance audit of phase 2 (production pass)

Independent audit of `agent-sessions` at `main` / `d5f496b`, everything since `c3029a8`
(96 files, +37,514 / -4,108 lines). Done on 2026-10-13 by an auditor who took no part in the
phase. Findings only: nothing in the repository was changed but this file.

One thing follows from the file itself: until `filemap.md` names `docs/AUDIT.md`,
`test_packaging.DocumentsTest.test_the_file_map_lists_every_module_and_document` fails
(`AssertionError: Lists differ: ['docs/AUDIT.md'] != []`). The audit was not allowed to edit
`filemap.md`; whoever takes the findings up adds that line first.

How it was done:

- Every gate, test run and mutation ran in copies made with `git archive HEAD | tar -x`, never
  in the main checkout. The copies used the main checkout's `.venv` through a link.
- Every feature experiment ran in a throwaway home built from `tests/fixtures`, with all four
  overrides (`AGENT_SESSIONS_HOME`, `_CACHE`, `_STATE`, `_MOUNTS`) and `HOME`/`XDG_*` pointing
  into it. `open` and `continue` only with `--dry-run`.
- The real machine was looked at read-only: a copy of the real index as `AGENT_SESSIONS_CACHE`
  and a temp `AGENT_SESSIONS_STATE` for every command.
- Parts C (features), E (documents), G (packaging) and the TUI were examined in parallel
  sessions of this audit. Their reproductions of everything rated medium or higher were run a
  second time by the main session; where a finding rests on the parallel session's run alone,
  the text says so.
- The audit's scripts are in the session scratch directory
  `/tmp/claude-1000/-home-tanuj/1312d18c-06ba-45a8-bdc0-1522af26442e/scratchpad/audit/`
  (`b/` security, `r/` review findings, `v/` re-runs, `flake/`, `mut/`, `alone/`, `d/` real
  machine, `feat/`, `docs/`, `pkg/`, `tui/`). That directory does not outlive the session, so
  each finding gives the steps and the output that matter.
- The machine was loaded by the parallel sessions for most of the run (load average mostly
  2.5 to 4 on 24 threads, higher while the mutation runs were going). Times are from that
  machine, not from an idle one.

**Result in one paragraph.** The hardening the review asked for is in place and holds up: all
of R-01 to R-13 are fixed as written, 77 of 81 small mutations of security-relevant lines are
caught by the suite, the cache survives 6,000 corrupted variants, and import and undo are
byte-exact on every fixture. The phase is nevertheless not done by its own definition: the
engine suite fails on the first run in a fresh checkout and is flaky after that (item 1), and
`verify` fails on the real machine as it was left (item 3). The audit also found two ways in
which a hostile drive still gets what the review's two worst findings were about: code runs
as the user when the browser is started in a directory of that drive (A-01), and a scan that
never ends and a browser that cannot be quit (A-02). In the TUI, the new folder input writes
the escape sequences of a directory name to the terminal (A-12), and a key that arrives twice
ends the program with a traceback (A-13).

Findings: **2 high, 13 medium, 12 low, 37 nits.**

## 1. Definition of done (phase 2): verdicts

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | `make check` clean on Python 3.11 and 3.14; engine coverage not lower | **not met** | 3.14: the first `make test` in a fresh checkout fails, 4 of 4 times (`HugeLineTest.test_scan_memory_stays_bounded`, A-07). With a bytecode cache the suite failed 4 of 43 runs (`test_two_processes_undoing_the_same_import`, A-08). `make lint`, `make typecheck`, `make test-tui`, `make coverage`: clean, once each. 3.11.17: `make dev` and `make check` in a venv of their own: exit 0, once. Coverage: no engine module is lower than in REVIEW's table (section 5). |
| 2 | Every review finding has a regression test that fails on the old code | **met, with one weak spot** | Taking each fix out again (section 5, mutations) makes a named test fail for R-01, R-02, R-03, R-04, R-06, R-08, R-09, R-10, R-11. R-05's line cap is held by one test only, the flaky one of item 1 (M37, M66). R-07, R-12, R-13 have tests (`test_json_contract.py`, `test_smoke.py`, `test_entry.py`). The old source was not run against the new tests: they import modules it does not have. |
| 3 | The real machine lists the same 135 sessions with `verify` passing; 29 offline | **partly** | 136 sessions listed (107 native, 29 offline); the one more is a session made since. The 29 offline sessions are listed, and `show`, `import --dry-run`, `continue --dry-run`, `export`, `open --dry-run` of one refuse with `the drive 22EEA841EEA80F5B is not mounted`, exit 1. `verify` on the index as it stands: **exit 1, 44 `DIFFERENT` lines** (A-20); after `scan --deep`: exit 0, `verify: the index matches the disks`. |
| 4 | Throwaway home: import and undo byte-identical; Grok confirmed by `grok export`; the five sessions without a folder import with `--to` | **met, except what needs the drive** | 7 fixture sessions imported plain (53 checks against an independent implementation of each recipe) and imported and undone with every `--to` variant (242 checks): the fake home is byte-identical after every undo (a Codex thread with a name leaves its appended line, as documented). A byte-identical copy of grok 1.0.46 under `unshare -r -n`, `env -i`: `export <id>` exits 0 with our turn counts and `sessions list` lists the import; `~/.grok` unchanged (2,384 entries, same digest). The five real sessions are on the unmounted drive: **cannot be verified here**; the mechanism was checked on fixtures (folder gone, folder not recorded). |
| 5 | No test and no worker starts an agent tool, sends anything to a model, mounts or writes a real store | **met for the tests; one trace of an earlier worker** | Each of the 44 test modules alone under an audit hook (every process start and every write logged): programs started are `python3` (100), `bash` (72), `python` (21), `desktop-file-validate` (1) and nothing else; no write outside `TMPDIR` but one temp directory in `/dev/shm` (`test_import_safety.py:38`, on purpose, removed). The real home is unchanged by all of the audit's runs (section 4). The user's real log holds one entry a test of an earlier worker wrote (`2026-10-12T21:24:30Z … ERROR unexpected error in agent-sessions tui: RuntimeError: boom`, from a worktree, through `unittest/mock.py`); commit d3ef628 closed that hole, with the limit in A-23. |
| 6 | Still owed by the user, listed in the README | **met** | README lines 384-399 list the five runs. |

## 2. REVIEW findings R-01 to R-14, run again

Own stores and own scripts (`r/r_all.py`, `r/r10_index_line.py`, `r/r13_tui.py`), against the
current code, in throwaway homes.

| Finding | Now | What was run, and what came out |
|---|---|---|
| R-01 id as a flag | **fixed** | A foreign Codex thread with `payload.id = --config=shell_environment_policy.inherit=all`; native stores built by hand with a Codex id `--config=mcp_servers.x={command="sh"}` and Grok ids `--yolo`, `--always-approve`. Listed: 0. Of 18 `open` / `import` / `continue --dry-run` calls by those ids, none gives a plan. 4 warnings, e.g. `session id '--config=mcp_servers.x={command="sh"}' is not in the form Codex ids have; not listed`. |
| R-02 scan hangs on a FIFO or device | **fixed as written; the symptom is back by another file, A-02** | FIFOs named as a Claude transcript, a Grok `summary.json`, an Antigravity `.db` and a Codex rollout, and links to `/dev/zero` and `/dev/urandom`: `scan --deep` exit 0 in 0.1 s, 7 sessions listed, 6 warnings. |
| R-03 escapes reach the terminal | **fixed in UTF-8 locales; open in the C locale, A-03** | cwd `C:\Users\u\proj␛]0;PWNED-TITLE␇␛[2J`, a prompt with `␛[31m` and U+009B, a reply with OSC 52 and U+202E, a Grok id with `␛c`. Control characters in the output of `list`, `list --keys`, `show`, `status`, `export`, `export --format json`, `list --json`, `import --dry-run`, `verify`: 0 each. |
| R-04 failed Codex import leaves files | **fixed** | `payload.id` differs from the uuid in the file name: `import` exit 1 (`a rollout is not named after its thread id: …`), no file in the fake home, 0 journal records. |
| R-05 unbounded line | **fixed; the bound is high, A-16** | One 200 MB line: peak resident set of the scanning process 58 MB (`VmHWM`; the review measured 823 MB), the session still listed, 1 warning. |
| R-06 concurrent imports lose records | **fixed** | 5 rounds of 4 imports started together, each undone again: 0 records lost, 0 imports failed, 40 journal lines. |
| R-07 `--json` undocumented | **fixed** | `docs/JSON.md` (312 lines), `tests/test_json_contract.py` (16 tests). An independent checker found 0 violations on fixture homes and on the real machine. In the C locale the output can be invalid UTF-8 (A-03). |
| R-08 SQLite not hardened | **fixed** | Truncated, scrambled, with a view under a table's name, with a trigger, with an FTS5 virtual table: none listed, 5 warnings (`conversation database not readable: database disk image is malformed`, …), scan exit 0, the good database still listed. |
| R-09 notification body | **fixed** | A native session whose folder is `-x/gone␛[2J<b>` + U+202E: `notify-send` would get `['--app-name=agent-sessions', 'Cannot resume session', 'the project folder -x/gone [2J<b>  is gone; Claude can only resume from it']`. No control character, no leading `-`. (`<b>` passes: documented as not defended.) |
| R-10 index file 0644 | **fixed** | A `session_index.jsonl` an import creates: 0600. One that exists keeps its mode. What is appended is another matter: A-11. |
| R-11 epoch units | **fixed** | Seconds, milliseconds, microseconds and nanoseconds of the same moment all give `2025-10-09T08:53:20.000000Z`; `5e9`, `10**30`, `-1` and year 0001 give no time; a unit can be passed. |
| R-12 test order | **fixed** | All of `tests/` in one process under the venv (`make coverage`): 1587 tests, OK (skipped=1). |
| R-13 `tui` "not implemented" | **fixed** | `cli.main(["tui"])` on a Python without Textual: exit 1, `the TUI needs Textual; run ./install.sh in <root>`. (New: `tui` with no terminal never returns, A-21.) |
| R-14 stale documents | **fixed for the file map; SPEC keeps a few** | Every tracked file is in `filemap.md`. Stale phase 1 statements left in SPEC: nits N-01 to N-04. |

## 3. New findings

Severity as in REVIEW. `[reproduced]` means run in this audit, with the command and what it
printed; `[by reading]` means not run. `<W>` is the root of a throwaway world, `<id>` a
session id.

### High

#### A-01 · high · the checkout launcher runs Python code from the current directory

`bin/agent-sessions`, `run_tui` (line 28): `command = [str(python), "-m", "agent_sessions.tui", *argv]`.

`python -m` puts the current directory first on `sys.path`, ahead of `PYTHONPATH` (the
checkout's `src`) and of the standard library. The TUI is what `agent-sessions` with no
argument starts, and this launcher is what `install.sh` links into `~/.local/bin`. Whatever
`.py` file in the current directory has the name of a module the TUI imports is executed as
the user: `textual.py`, `rich.py`, `json.py`, `logging.py`, `typing.py`, `queue.py`, …

Failure scenario `[reproduced]` (`v/v_g01_cwd.py`, and the packaging session's
`pkg/scripts/05_launcher.sh`, `06_cwd_collision_and_fix.sh`): the user changes into a folder
of a mounted drive and starts the browser there.

```
$ cd <W>/media/WinC/Users/user/Documents/x      # holds json.py: writes a marker file, 2 lines
$ agent-sessions tui
cwd holds json.py; `agent-sessions tui`: planted code ran: True ['RAN-AS-1000'] (the TUI started …)
cwd holds textual.py; `agent-sessions tui`: planted code ran: True ['RAN-AS-1000'] rc=1
    'agent-sessions: the TUI needs Textual; run ./install.sh in …'
cwd holds json.py; `agent-sessions list (control)`: planted code ran: False  rc=0
```

With `json.py` the TUI comes up normally afterwards: nothing tells the user. An innocent
`logging.py` or `types.py` in a project directory instead crashes the TUI with a traceback
(packaging session, 06). The installed wheel's entry point and every non-TUI command are not
affected.

Fix: `[str(python), "-P", "-m", "agent_sessions.tui", *argv]` (`-P` exists from Python 3.11,
the floor of this project; tried on a copy: the planted file no longer runs, first paint
0.18 s, exit 0). Better, start the venv's Python with `-P -c` and a two-line bootstrap that
inserts `SRC` itself, which also ends A-21's `PYTHONPATH` leak. Add a test that runs the
launcher's TUI branch from a directory holding `textual.py` (no test runs that branch today,
A-23).

#### A-02 · high · a sparse file named like a transcript keeps a scan reading for days, and the TUI cannot be quit meanwhile

`providers/base.read_lines` and `iter_jsonl` (a line has a cap, a file has none, and nothing
looks at the cancel event while one file is read); `index._parse` (cancel is checked between
candidates only); the TUI's quit, which waits for the scan worker.

REVIEW R-02 was "scan hangs forever on a FIFO". FIFOs and devices are refused now. A regular
file does the same: after one real record, a hole of any size. `truncate -s 1P` costs 4 kB
on disk on NTFS, ext4, btrfs, XFS and tmpfs. The reader takes the hole as one over-long line
and drops it a megabyte at a time, to the end.

Failure scenario `[reproduced]` (`b/t09_bounds.py`, `t09b_sparse_hang.py`, `t09c_sparse_tui.py`,
`t09d_tui_control.py`):

```
file: apparent size 1 PiB, 4096 bytes on disk, regular file: True
--- scan: no answer after 25 s (TimeoutExpired); at the 4 GiB/s measured … the file takes 3.0 days
--- list: no answer after 25 s          (no cache yet: `list` scans by itself)
--- scan --deep: no answer after 25 s
--- Ctrl-C after 3 s of scanning: rc=130 after 0.01 s, 'agent-sessions: interrupted'
sparse transcript, apparent size 16 GiB, on disk 4 KiB: {'seconds': 4.0, 'hwm_kb': 59568, …}
```

In the TUI (private pty, a good cache first, then the file appears on the drive):

```
control, no sparse file, q:             first_paint 0.161 s, exit 0, 0.0 s after q, screen: scanning, scanned
48 GiB sparse file, q after 3 s:        exit 0, but 8.51 s after q
1 PiB sparse file, q after 3 s:         no exit in 30 s (killed by the driver); Esc, Esc, q: the same
```

This is R-02's symptom word for word ("the spinner stays forever and quit cannot stop it").
The command line can at least be interrupted. SECURITY.md says "Reads never block and never
allocate without a bound" and lists as not defended only the *number* of files.

The wait after `q` is general: the terminal is given back at once, but the process stays,
without a word, until the engine call of any worker returns (an import, a folder listing, a
transcript read: gone 5.9 to 6.5 s after `q` with a call slowed to 8 s, TUI session's
`tui/exp_pty_quit.py`; by reading, up to 120 s while a mount waits for the password).

Fix: a byte budget per candidate file (stop after, say, 4 GiB with a warning, and list the
session from what was read); skip holes with `os.lseek(fd, pos, os.SEEK_DATA)` where the
platform has it; pass the cancel event into `iter_jsonl` so that Esc works inside a file; and
let the TUI's quit restore the terminal and leave without waiting for a scan worker.

### Medium

#### A-03 · medium · in the C and POSIX locales, text from a store reaches the terminal as raw C1 control bytes

`textsafe.UNSAFE` / `clean` (the surrogates U+D800 to U+DFFF are not in the set);
`cli._emit` (relies on the stream's own error handler); `cli._json`; `logs.PrivateRotatingFile`
(strict UTF-8: a message that cannot be encoded is dropped).

Python stands a raw byte `0xNN` in for by the lone surrogate `U+DCNN`, and where the locale
is C or POSIX (also `C.UTF-8`) `sys.stdout` writes such a surrogate back as the raw byte. JSON
can spell a lone surrogate (`"\udc9b"`), `textsafe.clean` lets it through, and so a title, a
folder or a turn in a hostile store puts the 8-bit CSI (0x9b), OSC (0x9d) and ST (0x9c) on
stdout. A terminal that is not in UTF-8 mode, which is what such a locale gives xterm, rxvt
and the Linux console, acts on them: colours, window title, clear screen, OSC 52.

Failure scenario `[reproduced]` (`b/t08c_surrogate_c1.py`): one foreign Claude transcript whose
first prompt is `title \udc9b31mRED \udc9d0;PWNED\udc9c end` (written with JSON escapes) and
whose cwd ends in `pro\udc9b2Jj`.

```
=== LANG=en_US.UTF-8
--- list: raw C1 bytes in stdout: none; e.g. b'… title \\udc9b31mRED \\udc9d0;PWNED\\udc9c end  pro\\udc9b2Jj'
=== LANG=C            (LANG=C.UTF-8: the same)
--- list:   raw C1 bytes in stdout: ['0x9b', '0x9c', '0x9d']; e.g. b'… title \x9b31mRED \x9d0;PWNED\x9c end  pro\x9b2Jj'
--- show:   raw C1 bytes in stdout: ['0x9b', '0x9c', '0x9d']
--- export: raw C1 bytes in stdout: ['0x9b', '0x9c', '0x9d']        (--format json too)
```

Two more consequences of the same gap, with a store below a directory whose name is not
UTF-8 (`caf\xe9`, `b/t08b_nonutf8.py`, `t17_log.py`): in those locales `list --json` and
`status --json` are not valid UTF-8 (`json.loads of the bytes fails: UnicodeDecodeError`;
JSON.md lines 14 and 26 promise UTF-8 with `\udcXX`); and in every locale the log loses the
line `import <key> (store <path>)` (1 of the 2 lines an import writes is there). In the TUI a
lone surrogate in a title is drawn as the six characters `\ud800` in a place measured as one
cell, so the row is too long (TUI session, `tui/exp_surrogate_width.py`). No terminal
was opened for this audit: what is shown is the bytes on stdout.

Fix: add U+D800 to U+DFFF to `UNSAFE` for readable text; in `_emit`, encode with
`backslashreplace` and write to `stream.buffer`, whatever the stream's handler is; in
`json_escape`, write a surrogate as its `\udcXX` escape; open the log with
`errors="backslashreplace"`. Add the three locales to `test_textsafe.CliBoundaryTest`.

#### A-04 · medium · an import that dies after its first unit is in place leaves a copy that can be neither imported nor undone

`importer._carry_out` (units are placed before any journal record exists, and the roll back
is a `finally` block that the next signal ends); `cli._signals` (a second SIGTERM or SIGHUP
is ignored while the first unwinds, a second Ctrl-C is not, and a SIGHUP or SIGTERM after a
Ctrl-C is raised).

Failure scenarios `[reproduced]`:

- SIGKILL between two units (`feat/t4_kill.py`, run twice): the Claude side directory is in
  place, the transcript still staged. Afterwards `import` → `already in the native store:
  …/214de9ed-…; nothing was overwritten`, `unimport` and `unimport --force` → `no import of
  this session is recorded; nothing was removed`. The same for Antigravity (`brain/<id>/`).
- SIGKILL after the last unit, before the record: the copy is listed as a native session,
  `import` → `already imported; nothing was copied`, `unimport` → `no import … recorded`.
  The window covers the directory flushes, the manifest, the read-back of the whole copy and
  the appends.
- Ctrl-C twice (`b/t05_double_signal.py`; the roll back is slowed by a 1.5 s sleep in front
  of `shutil.rmtree` of the staging directory to hit the window: simulated, it stands for a
  large staged copy):

```
--- stopped in 'copy':   SIGINT then SIGINT during the roll back: rc=130 'agent-sessions: interrupted'
    left: .claude/.agent-sessions-import-708349-…/0/custom-title.json
--- stopped in 'copy':   SIGINT then SIGHUP: rc=129, the same leftovers
--- stopped in 'copy':   SIGTERM then SIGTERM: rc=143, left: nothing        (the guarded pair)
--- stopped in 'placed': SIGINT then SIGINT: rc=130; left: the staging directory, <id>.jsonl, <id>/custom-title.json; journal: ''
    unimport: 'no import of this session is recorded'; import again: 'already imported; nothing was copied'
```

The command says `interrupted` and exits as after a clean roll back. SPEC ("an import that
was under way is rolled back first") and SECURITY 12 say more than is true.

Fix: write a `begin` record (destinations and the directories made) under the lock before
the first `_place`, and let the next import or undo of that store take back a `begin` that
has no `import` after it. Hold all three signals off for the length of the roll back.

#### A-05 · medium · on Python 3.11 a deeply nested side directory makes the roll back itself fail and leaves the tree in the native store

`importer._carry_out` (`finally`: `_remove` → `shutil.rmtree`), `importer._manifest`.

A foreign Claude session's `<id>/` directory is copied whole. Nested about 1,900 deep it is
still within PATH_MAX at the source and in staging, but not at its destination, so the
manifest fails (`File name too long`) after the units are in place. The roll back then calls
`shutil.rmtree`, which is recursive in 3.11 and passes the recursion limit; the
`RecursionError` leaves the `finally` block and the rest of the roll back is skipped.

Failure scenario `[reproduced]` (`b/t06_deep_dir.py`): side directory `<id>/a/a/…`, 1,930
levels, longest source path 4,051 bytes.

```
=== Python 3.14.7 and 3.13.16
--- import: rc=1  'agent-sessions: import failed: File name too long (…)'      ~/.claude after: None
=== Python 3.11.17
--- import: rc=1  'agent-sessions: internal error: RecursionError: maximum recursion depth exceeded (details in …log)'
    ~/.claude after: ['projects']   …/projects/<encoded folder>/214de9ed-5…/a/a/a/…   journal: None
--- unimport: rc=1 'agent-sessions: no import of this session is recorded; nothing was removed'
```

A hostile store alone does this, on a Python the project supports and CI tests. 3.12 is not
installed here and was not tried.

Fix: refuse at plan time a unit that is deeper than a few dozen levels or whose longest
destination path passes the platform's limit; make every step of the roll back independent
(catch `BaseException` per step, raise the first at the end).

#### A-06 · medium · an undo that could not remove everything still counts as the undo, and nothing can finish it

`unimport.undo` / `_carry_out` / `undo_record`, `journal.not_undone`, `cli._unimport` (exit 0).

Failure scenarios `[reproduced]`:

- A directory of the copy is read-only for a moment (`b/t04_unimport.py`):

```
--- unimport while the project directory is read-only: rc=0
    left …/<id>/custom-title.json (could not be removed: Permission denied)
    agent-sessions: removed the imported copy claude:2e4ea8f3:<id>: 1 file or directory; 3 left in place
    journal now: ['import', 'undo'] | files still in the store: 2
--- unimport again, directory writable: rc=1  no session matches 'claude:2e4ea8f3:<id>'; …
--- unimport --force: rc=1  the same
--- import again: rc=1  already in the native store: …/<id>; nothing was overwritten
```

- Antigravity (`feat/t3_unimport_awkward.py`, case k, run twice): the tool's
  `presence/<id>.lock` lies beside an untouched copy. A plain undo removes the database,
  leaves the lock and names `--force`. Then `unimport --force` → `no import of this session
  is recorded`, and `import --experimental` → `already in the native store:
  …/presence/<id>.lock; nothing was overwritten`.

Fix: an undo that leaves anything it set out to remove must not settle the import. Refuse a
plain undo up front while the tool's extras exist; when a removal fails, record the undo as
incomplete so that `not_undone` keeps the import standing for a forced retry; exit 1.

#### A-07 · medium · the memory test measures the test runner, not the scan: `make test` fails on the first run in a fresh checkout

`tests/test_hostile_files.py`, `HugeLineTest.DRIVER` / `measure` / `test_scan_memory_stays_bounded`.

The child process reports `resource.getrusage(RUSAGE_SELF).ru_maxrss`. On Linux that number
survives `fork` and `exec`: a child started with `preexec_fn` begins with the high-water
mark of the forked copy of its parent. What the test compares with its 100 MB ceiling is
therefore the size of the unittest process at that moment, unless the scan needs more.

Proof `[reproduced]` (`flake/hugeline_alone.sh`, `flake/peaks.py`):

```
== alone ==                                   Ran 1 test in 0.277s  OK
== alone, 150 MB ballast held by the test-runner process (never touched by the scan) ==
child reports peak_kb = 174436 warnings: 0    <- the baseline scan of one small session
AssertionError: 175456 not less than 102400
PEAK child_ru_maxrss_kb=96424 runner_rss_kb=107708 ceiling_kb=102400     (9 of 12 full runs: 96.3 to 97.0 MB)
PEAK child_ru_maxrss_kb=52448 runner_rss_kb=63752                        (3 of 12)
```

The scan's own peak for the 200 MB line is 58 MB (`VmHWM`, section 2). In the normal order
the runner sits 6% under the ceiling, and it is larger when the sources are compiled in the
same process, which is every first run:

```
checkout 1, first make test:  exit 2 | FAILED … test_scan_memory_stays_bounded … 103292 not less than 102400
checkout 1, second make test: that test passes      (bytecode cache present)
checkout 2, first make test:  … 104128 not less than 102400
checkout 3, first make test:  … 104108 not less than 102400
```

(`flake/fresh_checkout.sh`: three `git archive` copies. The audit's very first `make test`
failed the same way, 103156. The other failure those logs show, in `test_packaging`, is the
audit's own log file lying in the checkout: A-23.) With the test classes in another order
(seed 33) it failed too, 105212. A CI job is always a first run. The earlier worker's "failed
once in a full run" is this.

It matters twice, because this test is also the only one that holds the 32 MiB line cap:
mutations M37 (an over-long line is kept before it is dropped) and M66 (no cap at all) are
caught by it and by nothing else.

Fix: in the driver, report `VmHWM` from `/proc/self/status` (it belongs to the address space
and starts again at `exec`), or `tracemalloc`'s peak; assert on the difference to the
baseline run the test already makes and does not use.

#### A-08 · medium · two undos of one import: the loser is told the copy "was used since" and to force it; the test for it is flaky

`cli._unimport` (the dry-run plan and `api._undo_target` run before the lock is taken),
`unimport.undo`.

The first process removes the files and then writes its undo record. A second process that
plans in between finds the import still standing and its files gone.

Failure scenario `[reproduced]` (`v/v_undo_race.py`; the first undo is held for 2 s between
removal and record by a sleep, simulated, to make the window certain):

```
second undo, started while the first is between removal and record: rc 1 |
  … 69669cf3-….jsonl is gone. Nothing was removed; a forced undo removes it all the same (--force)
the same with --force, as the message advises: rc 1 | no import of this session is recorded; nothing was removed
third undo, after the first has finished (by key): rc 1 | no session matches 'claude:9120fc2a:…'; `list --keys` shows the keys
```

Nothing is lost, but the advice is wrong and three different messages come out of one
situation. Without any sleep, `test_unimport.LockAndRecordTest.
test_two_processes_undoing_the_same_import` (which expects `NO_RECORD`) failed in 3 of 43
full runs of the suite here (one more failure in those 43 was not captured by name), and the
packaging session saw it fail 5 of 8 times under CPU load.

Fix: plan under the lock. In `undo`, when the unlocked look finds differences that are all
"is gone", take the lock and look again before refusing; in `cli._unimport`, treat
`NotFound` after a standing record as `NO_RECORD`.

#### A-09 · medium · `tool_state` fails open: every wrapper that is not the one known form counts as "installed"

`launch._wrapper`, `launch._inspect` (`if found is None: return ToolState(path, True, False, INSTALLED)`).

SECURITY 9 says "a command that would install something is never started, so browsing
sessions cannot install software". The code recognises one text (a script of at most 8
commands and 4,096 bytes whose last command is exactly `exec mise x <spec> -- <tool> "$@"`)
and takes everything else for a program.

Failure scenario `[reproduced]`, on the real machine, files read only (`b/t01_toolstate_real.py`):

```
hermes   installed=True  wrapper=False detail='installed' path=/home/tanuj/.local/bin/hermes
```

`~/.local/bin/hermes` is Omarchy's own installing wrapper in another form (`if ! … mise
where … ; then mise use -g … ; fi`, last line `exec env -u UV_PYTHON mise x '…' -- hermes
"$@"`), and mise's `installs` holds no hermes. No reader uses `hermes` today, so nothing is
started; but it shows that the wrappers on this very machine come in more than one form, and
the next Omarchy update can give `claude` or `codex` the other one. Variants of the known
wrapper, tool not installed (`b/t02_wrappers.py`), 9 of 11 misjudged:

```
exact form (control)               wrapper=True  installed=False  refused (correct)
"${@}" instead of "$@"             wrapper=False installed=True   INSTALLED -> would be started
no exec                            wrapper=False installed=True   INSTALLED -> would be started
exec env -u VAR mise x (hermes)    wrapper=False installed=True   INSTALLED -> would be started
one more option (-q)               wrapper=False installed=True   INSTALLED -> would be started
a line after exec                  wrapper=False installed=True   INSTALLED -> would be started
nine commands                      wrapper=False installed=True   INSTALLED -> would be started
comment header over 4 kB           wrapper=False installed=True   INSTALLED -> would be started
Latin-1 byte in a comment          wrapper=False installed=True   INSTALLED -> would be started
npx auto-install wrapper           wrapper=False installed=True   INSTALLED -> would be started
```

Fix: fail closed for scripts. A `#!` text file on PATH that names `mise`, `npx`, `npm`,
`pipx` or `uv` and is not recognised is `unknown: a script that may install the tool`, and
is not started unless the user says so (a setting, or a question in the TUI). Find the
`mise x`/`mise exec` call anywhere in the script, not as its exact last line. Say in
SECURITY 9 what is recognised.

#### A-10 · medium · `export -o` writes into a session store the index does not know, a store on another drive included

`api._in_a_store` (compares with `index.stores` and `index.skipped` only), `api.export_file`.

CLAUDE.md's first rule: "Never write to a store on another drive". The check is as good as
the last scan.

Failure scenario `[reproduced]` (`feat/t11_misc.py`, run twice; `b/t07_export.py`):

```
-o <drive mounted since the last scan>/Users/user/.claude/projects/…/note.md:
   exit 0: agent-sessions: wrote <W>/media/WinC/Users/user/.claude/projects/F--Proj-My-Site/note.md (277 B)
after a scan: exit 1: … is inside the session store <W>/media/WinC/Users/user/.claude; nothing was written
-o ~/.grok/x.md (no sessions/ in it yet):          exit 0, written
-o ~/.gemini/antigravity-cli/x.md:                 exit 0, written
```

Fix: besides the index, refuse a path below any provider's `native_store()` and a path that
has an ancestor directory named in some provider's `dir_names` for which `is_store` is true.

#### A-11 · medium · a Codex import appends the source's index line to the native `session_index.jsonl` byte for byte

`providers/codex.py`, `_index_line` (decodes with `errors="replace"` only to test the line,
returns the raw bytes) and `import_plan` (`importer.Append(index, line + b"\n", …)`).

The line is the one write of an import that an undo never takes back. It is taken from the
foreign store as it is: any extra keys, a second `"id"` key, up to 64 kB, bytes that are not
UTF-8.

Failure scenario `[reproduced]` (`r/r10_index_line.py`; `feat/t11_misc.py`):

```
--- a line with two "id" keys (first: one of the user's own native threads) and extra keys: import rc=0
    appended bytes: b'{"id":"01a0e914-…(the user's thread)","thread_name":"RENAMED BY THE DRIVE","updated_at":"2099-01-01T00:00:00Z","anything":{"else":[1,2,3]},"id":"01a04ca1-…"}\n'
--- a name that is not UTF-8: import rc=0
    appended bytes: b'{"id":"01a04ca1-…","thread_name":"caf\xe9 \xff\xfe name"}\n'
    native index is valid UTF-8 afterwards: NO (invalid continuation byte)
    after unimport the line is still there: True
```

What Codex does with such a file was not run (no agent tool is started in this audit).
Python takes the last of two keys; a parser that takes the first renames the user's own
thread.

Fix: write a new line from the validated values and nothing else
(`{"id": <the session's id>, "thread_name": <clean text, bounded>, "updated_at": <parse_ts>}`),
and skip a source line that is not strict UTF-8.

#### A-12 · medium · the TUI's folder input writes escape sequences from a directory name on a foreign drive to the terminal

`tui/app.py`, `MainScreen._open_folder` (`box.value = text`), `_continue_options_thread`
(`options.folder` is the one field of that answer that is not cleaned), `_completed_folder`;
`tui/view.py`, `complete_folder`.

Textual's `Input` draws its value as it is. It is given the session's folder on this machine
(`c`, and Enter on a foreign row) and whatever name Tab completes. Everything else on the
screen goes through `view.clean`; this does not. REVIEW found the TUI safe; the folder input
is new in this phase.

Failure scenario `[reproduced]` (TUI session's `tui/exp_pty_hostile.py`, run again here; real
engine, private pty, throwaway home, a PATH of stubs): a drive holds a Claude session whose
recorded folder is `C:\Work\ev␛]0;PWNED-DIR-7f3a␇␛[5mil` + U+202E + `dir`, and that
directory. Keys `0`, `c`, Enter, Esc.

```
  blink SGR from the folder name            occurrences in terminal output: 5
  RLO U+202E from the folder name           occurrences in terminal output: 5
  title (OSC) from the transcript           occurrences in terminal output: 0     <- cleaned, as everywhere else
  mount label (OSC)                         occurrences in terminal output: 0
      context: b'…/media/EVIL/Work/ev\x1b]0;PWNED-DIR-7f3a\x1b[5mil\xe2\x80\xaedir \x1b[0m…'
argv.log (stub programs started): <none: nothing was started>
```

The same bytes come out 4 times when `t` is pressed, `<drive>/Work/e` typed and Tab pressed
(`tui/exp_pty_complete.py`, TUI session's run). The user has to ask for a continue or an
import of that session; nothing has to be confirmed.

Fix: never put a string that holds a `textsafe.UNSAFE` character or a surrogate into the
`Input`: `_open_folder` starts at `~/` instead and says why, `complete_folder` leaves such
names out and says how many. Subclass `Input` so that it draws `view.clean(value)`, which
also covers a paste. Test with a pilot that looks at the compositor's segments.

#### A-13 · medium · a closing or opening key that arrives twice ends the TUI with a traceback

`tui/app.py`: `TranscriptScreen.on_key`, `HelpScreen.on_key` and `on_click`,
`MainScreen.on_key`, `_view` (a second `TranscriptScreen(id="pager")`).

The first `q` pops the screen; a second one that is already in the queue pops again.

Failure scenario `[reproduced]` (TUI session's `tui/exp_pty_double_close.py` and
`exp_pty_double_open.py`, run again here; fake engine, private pty):

```
- pager: v, then 'qq' in one write                       exit status 1; ScreenStackError:
- pager: v, then q, 20 ms, q                             exit status 0; no error printed
- pager: v, then 'qqqqqqqq' (a key that repeats)         exit status 1; ScreenStackError:
- help: ?, then 'qq' in one write                        exit status 1; ScreenStackError:
- help: ?, then '??' in one write                        exit status 1; ScreenStackError:
- list: 'vv' in one write                                exit status 1; LookupError
```

`ScreenStackError: Can't pop screen; there must be at least one screen on the stack`; for
`vv` the pilot shows `DuplicateIds: Tried to insert a widget with ID 'pager'`. A held key
does it, and so does the user of A-14, who presses `q` again because the first was not
answered. SPEC: "the TUI never exits on an error".

Fix (tried by the TUI session in a scratch copy, `tui/out/suggested-fix.diff`; the suite
passes, 275 OK): a guard flag in each `on_key` and `on_click`, and `if self.app.screen is
not self: return` at the top of `MainScreen.on_key`. Do not call the flag `_closing`:
Textual's message pump owns that name.

#### A-14 · medium · the pager wraps the whole transcript before it answers a key; text without spaces holds the TUI for minutes

`tui/app.py`, `TranscriptView._wrap_some` and `_rewrap`; `tui/view.py`, `wrap_line`,
`_split_wide`, `text_width`.

Two causes. The slices of the wrap are chained with `call_later`, so the backlog is worked
off in one go before the next key (2,000 ordinary turns of 4,000 words: the loop is held
8.7 s; `tui/exp_pager_long.py`). And `wrap_line` measures the rest of a long word again for
every line it cuts, so one turn of 20,000 characters without a space costs 0.2 ms as ASCII,
332 ms as accented Latin and 625 ms as CJK at 80 columns (2,670 ms at 20).

Failure scenario `[reproduced]` (TUI session's `tui/exp_pty_pager_freeze.py`, run again
here; fake engine, private pty; `v`, then `q` after 2.0 s and again after 2.3 s):

```
- 30 turns, ascii,  100x30: the program left the screen 0.2 s after the last q;  exit status 0
- 30 turns, cjk,    100x30: … 12.9 s after the last q;  exit status 1
- 30 turns, accent, 100x30: …  6.5 s after the last q;  exit status 1
- 60 turns, cjk,     40x20: … 75.8 s after the last q;  exit status 1      (status 1 is A-13)
```

That is 0.4 s per turn at 100 columns; the pager takes up to 2,000 turns. During the freeze
Ctrl-C, SIGTERM and SIGHUP wait for its end too (31.6 to 32.8 s in the TUI session's
`exp_pty_freeze_signals.py`). A transcript is text from a foreign store, and `v` is the key
one presses to look at a session before trusting it. A resize wraps everything again
`[by reading]`.

Fix: `self.set_timer(0.001, self._wrap_some)` in place of `call_later` (tried in the scratch
copy: the longest stall at 2,000 turns is 35 ms instead of 8,684 ms); make `wrap_line` one
pass with a running width; stop wrapping when the screen has gone.

#### A-15 · medium · the TUI half of SECURITY 6 is not held by the tests it names

`tests/test_tui_view.py`, `tests/test_tui_app.py`.

SECURITY 6 names `tests/test_tui_view.py` as the proof that the TUI cleans what it shows.
`[reproduced]` by the TUI session (`tui/mutate.py`, table in `tui/out/mutations.tsv`): 35
mutations of `tui/`, one at a time, the whole TUI suite each. Every behaviour guard is caught
(offline rows, one job at a time, the confirmations, the stale-answer check). Of the 18 that
take one `clean` or one `markup=False` away, **10 pass all 275 tests**:

| What is no longer cleaned | |
|---|---|
| the detail pane's `cwd`, `local`, `store` and `id` | 4 |
| the PROJECT column (`project_name`) | 1 |
| the session title in the prompts (`_about`), the tool label and folder of the start prompt | 2 |
| a tool's name in the pager | 1 |
| the drive label in the scan's progress line | 1 |
| `markup=False` on the detail values becomes `True` | 1 |

The first two rows are the only thing between a recorded folder and the screen: the engine
hands `cwd` on as it is. Two of the ten were run again here (the detail pane's `cwd`, the
PROJECT column): see section 5. No test in `test_tui_app.py` draws a hostile value except
one for markup in the pager. This is why A-12 was not seen.

Fix: one pilot test that puts hostile text into every `Session` field and every engine
answer and asserts on the compositor's segments, and one view test per field.

### Low

#### A-16 · low · two of the bounds are far above what the documents suggest, and one is missing

- `providers/base.JSONL_LINE_MAX` (32 MiB) caps the bytes of a line, not what parsing it
  costs. `[reproduced]` (`b/t09_bounds.py`): one line of 31 MiB of `[],` → peak resident set
  918 MB (baseline 25 MB), no warning. A per-line cap near the largest real line (2.74 MB),
  or a refusal by token count, would hold it.
- `providers/base.open_sqlite_ro`: a database of more than 64 MB is opened in place and a
  value in it can be as large as SQLite allows. `[reproduced]` (`b/t12_sqlite.py`): a 300 MB
  `metadata` value in the first step of an Antigravity conversation → the scan peaks at
  625 MB. Fix: `con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 MiB)` and a warning for what
  it refuses.
- The same in-place path lets SQLite open the file itself, without `O_NOATIME`.
  `[reproduced]` (same script, tmpfs with `relatime`): `access time moved by the scan: True
  (before 2026-10-08 03:55:14, after 2026-10-13 03:55:14)`. CLAUDE.md: "A scan must not …
  move access times on another drive"; SECURITY 8: "read without side effects". Fix: read
  a large database through `open_ro` into a private temp file and open that, or say so.

#### A-17 · low · an offline copy of a session hides an identical mounted one

`index.Index.finalise` (of several copies the newest wins, then the smaller store key;
whether the volume is there is not asked). `[reproduced]` by the features session and again
here (`feat/t6_offline.py`, b2 and b): the same profile on two volumes, the first-sorted one
unplugged: all 8 sessions `offline`, `show <id>` → `the drive Backup is not mounted`, while
the other copy is mounted. Also a volume with no known UUID that comes back at `…/WinC1`:
`shown offline 8 …  WinC absent 6 stores 8 sessions, WinC1 mounted 6 stores 0 sessions`.
Fix: on equal `updated`, prefer the copy whose store is present.

#### A-18 · low · the lock file is opened through a link and truncated; a FIFO under one of our own names hangs every command

`fsutil._acquire` / `_note_holder` (`os.open(path, O_RDWR | O_CREAT)` then `ftruncate` and
the pid), `journal.read` and `logs.PrivateRotatingFile._open` (plain opens).

`[reproduced]` (`b/t03_lock.py`): `<state>/agent-sessions.lock` as a link to `~/notes.txt`:
after one import `victim now: '693956\n'` (28 bytes of notes gone). `imports.jsonl` as a
FIFO: `list` hangs (15 s timeout). `agent-sessions.log` as a FIFO: `scan` hangs. The state
directory is the user's own and trusted, so this needs the user or another program of
theirs; but "trusted, possibly malformed" should not cost a file. A stale lock file, a lock
that is a FIFO, a lock that is a directory and a killed holder are all handled well.
Fix: `O_NOFOLLOW` and a regular-file check for the lock; `O_NONBLOCK` and a regular-file
check (`fsutil.open_ro`) for the journal and, before opening, the log.

#### A-19 · low · the cache is replaced without a trace of the old one; its temp files are never swept; a scan can undo an import's view

`index.Index.load` / `save`, `cli._list`, `api._import`.

- A cache that does not load (one wrong value anywhere, or a version this program does not
  know) is treated as absent, and the next `list` scans and overwrites it. The sessions of
  drives that are not mounted exist nowhere else: on this machine 29 of them. The fuzz of
  section 4 shows how easily that happens: 3,628 of 6,000 single-value mutations give an
  unusable cache. `[by reading]`; `list` after such a cache prints `no usable cache;
  scanning…` (`b/t07_export.py`). Fix: move an unusable cache aside (`index.json.unusable`)
  before the first save, and take the `volumes` and parsed entries that do validate.
- `index.json.<pid>.tmp` left by a kill during a save (every title and path, 0600) stays for
  good. `[reproduced]` by the features session (`feat/t4_kill.py`, "cache-write"). Fix: on
  save, remove such files whose process is gone.
- A scan in another process that loaded the cache before an import saved it writes its own
  view back. `[reproduced]` by the features session (`feat/t5_concurrent.py`, 4 of 20
  rounds): `list` shows the session as not imported until the next scan. Journal and store
  are right. Fix: hold the state lock from load to save in `Index.scan`.

#### A-20 · low · `verify` calls a store that appeared since the last deep scan a fault, so it fails on the real machine as it stands

`api.verify` (`skipped store on disk but not in the index` goes to `differences`, where a
new candidate goes to `stale`).

`[reproduced]` on the real machine with a copy of its index (`d/real.py`):

```
verify (before any scan)        rc=1   2.18 s  44 DIFFERENT, 1 stale; last line: verify: 44 differences
scan --deep                     rc=0   0.15 s  stores: 4 (6 more on drives not mounted, 101 skipped)
verify (after the deep scan)    rc=0   2.17 s  verify: the index matches the disks
```

The 44 are fixture stores in agent worktrees below the project's `.claude/worktrees/`,
made after the last deep scan (18:43:48Z). Fix: report stores and skipped stores newer than
`last_deep` as stale with the advice the README already gives (`scan --deep` first), and
say so in `verify --help`.

#### A-21 · low · the launcher: `--debug` cannot reach the TUI, the tools it starts inherit `PYTHONPATH`, and `tui` without a terminal never returns

`bin/agent-sessions` (`wants_tui`, `run_tui`), `cli.main` (no terminal check for an explicit
`tui`), `launch._detached` and `os.execvp` (no `env=`).

- `agent-sessions --debug tui` and `agent-sessions --debug` on a terminal run `cli.main`
  on the system Python: `the TUI needs Textual; run ./install.sh in <repo>`, exit 1, in an
  installed checkout. `[reproduced]` by the documents and packaging sessions.
- The TUI runs with `PYTHONPATH=<repo>/src` in its environment, and every tool it starts
  inherits it: any Python the agent runs in the user's project sees it. `[reproduced]` for
  the TUI process (packaging session), `[by reading]` for the children.
- `agent-sessions tui </dev/null >file`: `still running after 20 s (killed)`, with screen
  codes written to stderr (`r/r13_tui.py`). `[reproduced]`. Fix: exit 2 with a line when
  stdin or stdout is not a terminal.
- `agent-sessions <&-` gives `AttributeError: 'NoneType' object has no attribute 'isatty'`;
  a `.venv/bin/python` that cannot be executed gives a traceback. `[reproduced]` by the
  packaging session.

Fix: one bootstrap for all of it (see A-01): decide "TUI or not" after the global options,
start the venv's Python with `-P -c`, put `SRC` on `sys.path` there and leave `PYTHONPATH`
alone.

#### A-22 · low · `install.sh` replaces what `uninstall.sh` is careful to leave

`install.sh` lines 82 (`ln -sfn`) and 99 (`install -m 644`). `[reproduced]` by the packaging
session (`pkg/scripts/04_script_edges.sh`): a link at `~/.local/bin/agent-sessions` that
points to some other program is repointed without a word; a launcher the user edited
(`Exec=kitty agent-sessions`, the edit the README itself suggests off Omarchy) is
overwritten on every run. Also: a relative `XDG_DATA_HOME` or `HOME` is taken by one script
and refused by the other; `update-desktop-database` rewrites `mimeinfo.cache`, and pip
writes `~/.cache/pip`, although the header says "nothing outside those three places is
written". Fix: say what was replaced, keep a launcher that differs and report it, check the
paths the same way in both scripts.

#### A-23 · low · gaps in the test suite

- `tests/support.py` line 38 sets the temp state directory only `if "AGENT_SESSIONS_STATE"
  not in os.environ`. The README gives that variable to users; with it exported, the tests
  write there. `[reproduced]`: `AGENT_SESSIONS_STATE=<dir> python3 -m unittest discover -s
  tests -p test_entry.py` leaves `agent-sessions.log` (972 bytes, `ERROR unexpected error in
  agent-sessions tui: RuntimeError: boom`) in `<dir>`. Fix: set it unconditionally.
- Three modules do not follow CLAUDE.md's rule ("imports `support` … before
  `agent_sessions`"): `test_smoke.py`, and `test_tui_app.py` / `test_tui_view.py`, whose
  `tui_support` sets no state directory. No write was seen from them.
- No test runs the launcher's TUI branch or `tui.__main__.main`: mutants "the venv Python is
  never used", "the user's PYTHONPATH is dropped", "any ImportError is reported as needs
  Textual", "the app's exit status is dropped" and "the TUI runs inside the signal wrapper"
  all survive (packaging session). That is why A-01 and A-21 went unseen.
  `tui/__main__.py` has 0% coverage.
- `test_install_scripts.py`: the stand-in Python decides `version >= (3, 11)` itself, so
  `install.sh` with a floor of 3.10, or with none, passes (packaging session).
- Surviving mutations of this audit (section 5): M16 (`importer.check` does not look for an
  existing destination: `_place` still refuses, later, after staging), M27 (redundant
  condition in `plan_new`), M79 (`launch._directory` takes a relative recorded folder: the
  "absolute path" half of SECURITY 2 has no test).
- `test_packaging.DocumentsTest.test_the_file_map_lists_every_module_and_document` walks the
  working tree: any stray file in the checkout's root (a log, a note) fails it.
  `[reproduced]`: `AssertionError: Lists differ: ['first.log'] != []`. Use `git ls-files`.
- Five test modules call `os.geteuid()` in a decorator without a guard
  (`test_discover.py:64`, `test_import_antigravity.py:434`, `test_importer.py:1388`,
  `test_unreadable.py:49`, `test_unimport.py:651`): on Windows the module does not import.
- The sdist ships 43 `tests/test_*.py` without `support.py`, `tui_support.py`,
  `fixture_world.py` or the fixtures: `Ran 84 tests … FAILED (errors=75)` (packaging
  session). Add a `MANIFEST.in` or prune the tests.
- CI has no build-and-install step, no coverage floor and no timeout.

#### A-24 · low · three smaller faults in import and undo

All `[reproduced]` by the features session; the first two were run again here.

- `unimport.plan` looks only at the directories of the record it undoes: import A, import B
  into the same new project directory, undo A, undo B leaves `.claude`, `.claude/projects`
  and `.claude/projects/<dir>` empty for good; newest first leaves nothing
  (`feat/t3_unimport_awkward.py`, b).
- `providers/grok.py`, `import_plan`: when the native store has a group directory that only
  *decodes* to the folder (`…%2fmedia%2f…`, lower-case hex), the import goes there, and
  FORMATS Q1 says the tool does not list from such a name (`feat/t11_misc.py`, 3). Always
  use `group_name(cwd)`.
- A Grok `summary.json` that starts with a BOM is listed and cannot be imported:
  `summary.json cannot be rewritten: Unexpected UTF-8 BOM (decode using utf-8-sig)`.

#### A-25 · low · what the documents and the help say and the program does not do

All `[reproduced]` by the documents session unless marked.

- Every documented key example is ambiguous by construction: `claude:1a2b3c4d` is provider
  plus store hash, which all sessions of a store share. On the real machine `open
  claude:da5bf20c --dry-run` → `'claude:da5bf20c' matches 91 sessions…`, exit 1. Use an
  example that reaches into the id and say that a start must.
- "`--debug`: log more" (README 321, `cli.py` 140): there is no DEBUG-level call in `src`
  (0 matches). It copies the log to stderr and shows the traceback of an internal error.
- With a read-only cache directory `scan` exits 0 and prints ``warnings: 1 (see
  `agent-sessions status`)``; `status` says `warnings: 0`. The warning ("cache not saved")
  is never kept. Print it in `scan`.
- README 283 says the cache directory is 0700; the owner's is 0755 (made by 0.1.0), and
  `make_private_dir` leaves an existing one alone. Tighten our own directories on save.
- `open` of a foreign Gemini session says `import it first`; `import` of it always refuses.
  Say "continue it as a new session" where `import_options` says not possible.
- SECURITY "not defended" 5 says an import copies the bytes a link in a foreign store points
  to. `[reproduced]` (`b/t16_symlink.py`): the session is listed and `show` reads it, but
  `import` refuses (`… is not a regular file`). The document claims less than the code does.

#### A-26 · low · a time in the cache that the loader accepts stops `list`, or the TUI, every time

`index._is_conversation` (checks that `started` and `updated` are text, not that they are
times); `cli._age`; `tui/view.py`, `local_time` and `detail_fields`.

The cache is the user's own file, "trusted but possibly malformed". The loader refuses a
wrong type but takes any text for a time. `[reproduced]` (`v/v_cache_times.py`, zone +05:30):

```
updated = '2026-10-05T10:00:00':          list: rc=1 "internal error: TypeError: can't subtract offset-naive and offset-aware datetimes …"
                                          list --json: rc=0   status: rc=0   the TUI: exit 0
updated = '9999-12-31T23:59:59.000000Z':  list: rc=0          the TUI: exit 1, OverflowError: date value out of range
```

A foreign store cannot do this (`parse_ts` keeps 1990 to 2100), and a scan does not mend it
while the session's file is unchanged. `cli._age` and `view.age_text` are two copies of one
function that have drifted. Fix: on load keep a cached time only when `parse_ts(value) ==
value`; one age function for both front ends, which gives `-` for what it cannot place.

#### A-27 · low · `textsafe.clean` leaves invisible characters in, and they travel into the file `continue` gives to a model

`textsafe._RANGES`. The set is the controls, the bidirectional controls, U+2028, U+2029 and
U+FEFF. The zero-width characters (U+200B, U+200C, U+200D, U+2060), the soft hyphen (U+00AD)
and the tag block (U+E0001 to U+E007F) pass. `[reproduced]` by the TUI session
(`tui/exp_inject.py`, `exp_surrogate_width.py`): they reach the screen, where nothing is
seen, and the Markdown export, from which the hidden ASCII of a tag sequence can be read
back. `show` and the pager therefore do not show the user everything the handoff file of
`continue` carries. SECURITY "not defended" 1 says a transcript can steer a model; it does
not say that part of it can be invisible. Fix: replace the format characters (category Cf)
and the tag block in readable output, or mark them (`<U+200B>`), and say in the export's
header when any were found.

### Nits

Documents:

- N-01 SPEC 228-229 "A mount that vanished keeps its sessions out of the listing"
  contradicts 570-572 "are **listed**".
- N-02 SPEC 237-239 "**Every** SQLite file … is opened with `…?mode=ro&immutable=1`":
  files up to 64 MB are an in-memory copy (SECURITY 160-163, FORMATS, the code).
- N-03 SPEC 821-822 "A failure the user sees names the log": only the `internal error`
  line does.
- N-04 SPEC 812-813 "laid out for 78 columns": the top-level help has a 79-column line.
- N-05 SPEC 827 and 943 say 135 sessions; the machine has 136.
- N-06 JSON.md 150 `last_deep` "when the last deep scan finished": it is stamped after the
  walk and before the parsing.
- N-07 README 38-39: the screen shown cannot be rebuilt from the repository (the nine
  sessions are not in `tui_support.py`; no test pins it). Rebuilt by hand it matches.
- N-08 README 411 / pyproject 121 "mypy (strict for the package)": `mypy --strict` gives
  104 errors; four flags are on.
- N-09 README 287, SECURITY 297: handoff files "older than 30 days are swept": only by a
  later real `continue`. README 294 "swept by a later import": only one into the same
  provider's native store.
- N-10 SECURITY 145 names FORMATS "Size caps" as the list of caps; the table lacks the
  16 MiB whole-file caps, Codex's 64 KiB index line, `EVIDENCE_MAX` and `WRAPPER_MAX`.
- N-11 README 99 and `install.sh`'s header: "three things and nothing else" (see A-22).
- N-12 README 328 lists three drive states; `status` has a fourth wording (`not mounted`).
  The README names only the Claude Desktop skip rule; `discover.SKIP_RULES` also skips every
  store under `…/tests/fixtures/…` and everything inside the checkout.
- N-13 `agent-sessions tui --help` prints one usage line through the launcher and the full
  description through the entry point.
- N-14 SECURITY "not defended" does not say that a resumed or continued tool starts in a
  folder of the foreign drive and loads that folder's own agent configuration (`CLAUDE.md`,
  `.claude/settings.json` hooks, `.mcp.json`, `AGENTS.md`). `[by reading]`
- N-15 The classifier `Environment :: Console :: Curses` is wrong (Textual is not curses);
  there is no `agent_sessions/__main__.py` (`python -m agent_sessions` fails); the desktop
  file has no `TryExec=omarchy-launch-tui`.

Code:

- N-16 `cli.py` imports `mounts.MountsError` and `providers.all_providers` at run time; SPEC
  192-194 says neither front end reaches behind `api`. Re-export both from `api`.
- N-17 The same helper five times: `_mtime` in every adapter (identical bodies). Twice:
  `_now` (`importer`, `unimport`; `index.utc_now` is a third), a byte-size formatter
  (`cli._size`, `importer.human_size`), `_join` (`discover`, `pathmap`), the drive-letter
  pattern `_LETTER` (`api`, `pathmap`), a remembering `mkdir -p` (`fsutil.make_private_dir`,
  `importer._make_dirs`), "link, else rename" (`importer._place`, `export._link`).
- N-18 Dead: `fsutil.cache_dir` (no caller in `src`), `cli._not_implemented` (every parser
  command is in `_COMMANDS`), `launch._spawn` (a second name for `_detached`).
- N-19 Long or deep functions a reviewer has to hold whole: `index.finalise` (110 lines of
  code, 36 branch points), `index.scan` (101, 42), `api.verify` (99, 39, nesting 6),
  `importer._carry_out` (101, 22), `index._parse` (95, 29), `cli._status` (123, 30),
  `claude._scan` and `discover.brute_pass` (nesting 7). 14 of 815 functions pass 60 lines
  of code. `cli.build_parser` (300) is a table and reads as one.
- N-20 `cli._map` does not expand `~`; `--to`, `--in` and `-o` do.
- N-21 `export -o ''` → `<cwd> is a directory`. `export -o <the cache or the journal>
  --force` replaces our own file: `export` guards the stores, not the program's files.
- N-22 `open --dry-run` prints a `cd` line in which the control characters of a folder name
  are spaces: for such a name the line is not what would run.
- N-23 Claude `import --to <symlink>` names the project directory after the link's path;
  Grok takes the real path for the reason FORMATS Q8 gives. `[by reading]`
- N-24 `uninstall.sh --purge` knows the default directories only: with `AGENT_SESSIONS_STATE`
  or `_CACHE` set it says "nothing to purge".
- N-25 `bin/agent-sessions` has `#!/usr/bin/env python3` and no version check: under a
  Python below 3.11 there is no "needs 3.11" line, and with mise shims the interpreter
  changes with the directory.
- N-26 The log is opened without tightening an existing wider mode, and two processes that
  log at once (the TUI and a command) can each rotate it under the other. `[by reading]`
- N-27 After a kill, a stale `agent-sessions.lock` and, for a first import, an empty
  `~/.claude` stay. Harmless.
- N-28 Messages for one situation differ by path: an undone import asked for again by key
  says `no session matches …`, by id `no import of this session is recorded`.
- N-29 The mise install-name rule in `launch._install_name` "was not checked against
  mise's code" (its own docstring): a wrong guess means "not installed", the safe side.
- N-30 `make lint` reports 44 I001 errors when the checkout's path holds `[` or `]` (ruff's
  handling of glob characters).
- N-31 `noqa` and `type: ignore`: 8 in all (`index.py:82`, `launch.py:304, 348, 660`,
  `mounts.py:228, 383`, `providers/__init__.py:32`, `bin/agent-sessions:29`), each with its
  reason beside it, and each reason holds. None in `tui/`.

The TUI (all from the TUI session; `[reproduced]` there unless marked):

- N-32 The folder input opens showing the beginning of a long suggested folder: with a
  212-character folder at 80 columns the end is not visible until a key is typed
  (`_open_folder` sets value and cursor while the bar is not displayed).
- N-33 A typed folder with `..` after a link is checked as one path and used as another:
  `<root>/a/link/../y` with `link -> <root>/b/c` is checked at `<root>/b/y` and imported
  with target `<root>/a/y` (`_folder_thread`: `isdir(path)`, then `normpath(path)`).
  Normalise first.
- N-34 `~` in the folder input is `os.path.expanduser("~")`, not the engine's home
  (`AGENT_SESSIONS_HOME`). `[by reading]`
- N-35 `test_forwards_to_the_api` checks the engine wrapper against a bare `mock.Mock()`;
  `mock.create_autospec(api)` passes today and would notice a renamed keyword.
- N-36 `MainScreen` is 1,294 lines, 137 methods and about 20 state flags (no function over
  50 lines). The worker scaffolding is written out 5 times, the progress throttle 3 times.
  `cli._age`, `_size`, `_project` and `view.age_text`, `bytes_text`, `project_name` are the
  same three functions twice (both print `1000.0 kB` for 999,950 bytes).
  `Engine.find_session` is used by a test only; `Engine.providers`, `list_folder` and
  `where` do not go through `api.py`. `view.text_width` and Rich's `cell_len` disagree for
  ZWJ emoji, VS16, Hangul jamo and the soft hyphen, so such rows are out of line.
- N-37 SPEC's TUI section says the start runs "a quick scan"; the code calls
  `scan(deep=None)`, which can be a full walk. `m` on a row whose drive is absent shows
  `mounting C: answer the password prompt…` before `no drive with the volume id … is
  attached`. A typed NUL and Tab show `open: embedded null character in path`.

## 4. What was examined

### Found sound

- **Session ids.** One grammar for all five readers, enforced at parse, again for every
  cached entry, in `launch.plan`, `importer._sound`, `launch.handoff_file` and
  `api.unimport`. Taking any one of those checks out fails a test (M09, M25, M28, M33, M34,
  M68).
- **Command lines.** No shell anywhere; `subprocess` only in `launch.py` and `mounts.py`;
  `_from_session` finds what came from the session without the adapter's help; a `-`, a NUL
  and a wrapper that is not installed are refused for a resume and for a continue (M23,
  M24, M26, M78). On this desktop `omarchy-launch-tui` passes its arguments on unsplit
  (`"$1" "${@:2}"`) to kitty.
- **Import containment and placing.** `..`, absolute parts, a destination outside the
  native root, a name taken at the last moment: refused (M07, M08, M10). `--to` with spaces,
  unicode, a trailing slash, a link, a 360-byte path, a name with a line break or an escape:
  correct; relative, missing, a file, empty, a dangling link: refused with nothing written.
- **Import recipes.** Seven fixture sessions against an independent implementation of each
  recipe in FORMATS: byte for byte, files 0600, directories 0700, source untouched (bytes,
  mtime, atime). Codex rewrite cases (equal, below, `..`, prefix, non-string values, CRLF,
  no final newline, a segmented thread, the Orca mirror's name line) all match.
- **Undo.** A journal that names a file outside the store, a path with `..`, another native
  store, a damaged manifest: refused. A file replaced by a link: refused, and when forced
  only the link goes. A home reached through a link: refused (`this import was made into
  another native store`). Torn and duplicated records, the cache deleted, the drive away,
  old `imports.json` records: as SPEC says. (M17 to M22, M82.)
- **Journal and lock.** 5 rounds of 4 imports at once here, 80 rounds of four scenarios in
  the features session: journal intact every time. A stale lock, a killed holder, a lock
  that is a FIFO or a directory: handled. A held lock: `another import is still running
  (process 699116)` after 30 s.
- **Signals.** SIGTERM, SIGHUP and one Ctrl-C at 5 points of an import: 143, 129, 130, store
  clean, import then works.
- **Mounting.** Fake `/dev/disk` trees with hostile names (`..`, `x y`, `--help`, 65
  characters, udev-escaped `../../etc`, a label of escape sequences): a volume id is taken
  only in its form, `udisksctl` gets `mount --block-device <by-uuid>/<id>` and nothing else,
  and nothing is started for 9 of 11 bad ids (the other two are well-formed ids of attached
  devices). A label with `ESC ] 0 ; … BEL`, U+202E and a line break is clean in `list`,
  `status`, `map`, `mount` and `forget`, and exact in `--json`.
- **Cache.** 6,000 single-value mutations of a version 2 and a version 1 cache (types
  swapped, keys removed and added): 2,372 load, 3,628 are refused as unusable, **0 crash**
  `load_index`, `status`, `volumes` or `where`. (What "unusable" then costs: A-19. The
  fuzz did not print the listing; two times that load and then stop `list` or the TUI: A-26.)
- **`path_evidence`.** A `.claude.json` of 17 MB, nested 200,000 deep, with `projects` as a
  list or a string, with 400,000 keys, with one key of 4 million segments: at most 139 MB
  and 0.4 s, the right warning, never a failure.
- **SQLite** up to 64 MB: no `-wal`, no `-shm`, damaged and odd schemas refused (M35, M36,
  M38, M39).
- **Terminal output in UTF-8 locales**, and the log: a folder with a line break and a
  made-up log entry in it becomes one line; no raw escape; log 0600 in a 0700 directory.
- **`export -o`** for stores the index knows: an existing file, a link, a dangling link, a
  FIFO, a directory, a path through a linked directory or `..` into a store: all refused; a
  0644 file replaced with `--force` comes out 0600.
- **Offline, `forget`, `map`, `continue --dry-run`** (45 pairs of source and tool), and
  **determinism** (`list --json` byte-identical over 6 fresh deep scans with varied
  `PYTHONHASHSEED`, and over 3 scans of the real machine).
- **The real machine**, section 5: every read-only command, and the TUI in a private pty.
- **The TUI** (TUI session; pilot and private pty, fake engine or a throwaway home with a
  PATH of stubs). Every `Static` has `markup=False`: action links (`[@click=app.quit]`),
  `[link=…]` and markup in a title, folder, note, reason, warning and transcript are shown
  as text, and 107 clicks on them do nothing. Nine hostile payloads across about 25 screen
  states: no control or bidirectional character from a title, WHERE, cwd, store, note,
  reason, warning, tool label, scan label, error text or transcript reaches a segment
  (the exceptions are A-12, A-03 and A-27). The folder input refuses relative paths,
  `~user`, an empty text, a file, a FIFO, a link loop, an unreadable directory and a NUL,
  each with a message; a directory of 100,000 entries is listed in 351 ms in a worker with a
  longest stall of the screen of 9 ms; a listing that hangs (simulated) stalls it 2 ms and
  its late answer is dropped. One job at a time holds, stale answers are dropped. Sizes
  from 1 by 1 to 300 by 100 with every prompt open, and resizes: no crash. With no sessions
  and no providers, every key: no crash and no engine action. Through the TUI's own keys on
  a fixture world: plain import, `t` into `~/target folder`, the experimental import after
  its warning, undo, continue (the stub launcher got `omarchy-launch-tui
  --app-id=org.omarchy.claude claude Continue an earlier Claude session. …`; handoff
  directory 0700, file 0600), resume; with the volume away: the right refusals and nothing
  started. 5,001 sessions on the real engine: first paint 0.32 s, `j` 2 to 37 ms, 79 MB.
- **Packaging.** The wheel (30 `.py` files and `tui/app.tcss`, nothing stray), installed
  with `[tui]` in a fresh venv: `--version`, `list --json` (`[]`), the TUI with no argument,
  with `tui` and with `--debug`, exit 0. Without `[tui]`: one line, exit 1. `install.sh`,
  `install.sh --dev` and `uninstall.sh` (with `--purge`) from a checkout at `chk out [x]*'s`
  with a home called `home dir`: correct, and the `printf %q` hint is right.
  `PYTHON='python3; touch …'` is not executed. shellcheck 0.11.0: clean.
- **Documents.** Every README command run; every environment variable in its table is one
  the code reads; 0 dead links or anchors in 15 Markdown files; `ADDING_A_PROVIDER.md`
  matches `base.py`; versions agree at 0.2.0.
- **Module rules of CLAUDE.md.** `textual` is imported in `tui/app.py` only. No engine
  module imports `tui` but `cli._tui`. Platform-specific calls outside `mounts.py` and
  `launch.py`, all guarded: `fsutil` (`fcntl`/`msvcrt` by platform and `ImportError`,
  `O_NOATIME`, `O_NONBLOCK`, `O_NOCTTY`, `O_DIRECTORY` by `getattr`, `os.fchmod` by
  `hasattr`, `scandir(fd)` by `os.supports_fd`, unlinking the open lock file not on
  Windows), `importer` (`os.kill(pid, 0)` only on POSIX, `os.link` with a rename fall-back),
  `export` (`os.link` fall-back, `os.fchmod` by `hasattr`), `cli` (SIGHUP by `getattr`).
  Nothing else was found (`journal`, `index`, `pathmap`, `unimport`, `logs` use portable
  calls). None of it was run on Windows.
- **Tests that cannot fail.** Of 1,577 test methods one has no assertion of its own
  (`test_wrong_types.py:641`, a "does not raise" test, which is one).

### The real home is unchanged

A snapshot (`audit/snap.py`) was taken before the first run and compared at the end:

- `~/.cache/agent-sessions` (`index.json`, sha256 48fd2f36…, 108,369 bytes, mtime
  2026-10-13 00:13), `~/.local/state/agent-sessions` (the log, 1,148 bytes) and
  `~/.config/agent-sessions` (does not exist): **identical**, contents, modes and times.
- `~/.local/bin/agent-sessions` → `/home/tanuj/Projects/agent-sessions/bin/agent-sessions`
  and `~/.local/share/applications/agent-sessions.desktop` (sha256 17be0e46…): **identical**.
- The top-level listings of `~/.codex`, `~/.grok`, `~/.gemini`, `~/.gemini/antigravity-cli`,
  `~/.grok/sessions`, `~/.codex/sessions`: see the last lines of this section for what
  moved and why.
- Each of the 44 test modules alone with the real home, under the audit hook: no write
  below `/home/tanuj`.
- `git status --short` in the repository: `docs/AUDIT.md` only.

What did move in the snapshot, none of it by this audit:

- `~/.claude.json`, `~/.claude/.credentials.json` and `~/.claude/backups`: the Claude Code
  sessions this audit runs in.
- `~/.codex/models_cache.json`: same size (201,451 bytes), modification time 03:24:56 at
  the start, then 03:54:56, 04:24:57, 04:39:56 and 04:54:57 at later looks: it moves in
  steps of 15 minutes on its own. No command of the audit runs `codex`,
  the repository does not contain that file's name, and no process called codex or orca was
  running; what refreshes it was not found.

Everything else in the listings of `~/.claude`, `~/.claude/projects`, `~/.codex`,
`~/.codex/sessions`, `~/.grok`, `~/.grok/sessions`, `~/.gemini` and
`~/.gemini/antigravity-cli` is identical, `/run/media/tanuj` is empty as before, and the
listing of `~/.local/bin` is identical.

### Not examined, and why

- Everything that needs the Windows volumes mounted: the files of the 29 offline sessions,
  the five sessions without a folder, the evidence for `D:` and `F:`. Mounting needs the
  user's password and was not allowed.
- Any real resume, continue, import, undo, mount or forget on the real machine, and any
  agent tool. Only `grok export` and `grok sessions list`, on a copy of the binary in a
  sandbox without network. What Claude, Codex, Grok and Antigravity do with an imported
  copy, and what Codex does with the line of A-11, is unknown.
- A real terminal. The TUI ran in pseudo-terminals only; whether a given terminal acts on
  the bytes of A-03 was not tried.
- Windows, the GitHub Actions run, Python 3.12 (not installed), Python below 3.11.
- Filesystems without hard links, a native store on another filesystem, a full disk, power
  loss as opposed to SIGKILL, sessions of hundreds of megabytes.
- The old code against the new tests (item 2 of section 1).

## 5. Numbers

### Suites

| Suite | Python | Tests | Time | Result |
|---|---|---|---|---|
| `make lint` | venv 3.14.7 | 79 files | 0.1 s | clean |
| `make typecheck` | venv 3.14.7 | 78 files | 2.2 s | clean |
| `make test` (engine) | 3.14.7 | 1587 (165 skipped: 164 need Textual, 1 needs shellcheck) | 23.5 to 25.0 s | see flakiness |
| engine suite | 3.11.17 | 1587 (165 skipped) | 22.7 s | OK, once |
| `make test-tui` | venv 3.14.7, Textual 8.2.8 | 275 | 200.4 s | OK; the TUI session ran it five more times from a clean copy (198.6 to 206.6 s; plain, under an audit hook, with an empty `HOME`, classes shuffled with two seeds): all OK |
| `make coverage` (all of `tests/`, one process) | venv 3.14.7 | 1587 (1 skipped) | 225.6 s | OK |
| `make dev` + `make check` | 3.11.17, a venv of its own | all four gates | 6.2 s + 224.9 s | exit 0, once |
| each of 44 modules alone | 3.14.7 | 1587 | 31.1 s in all | all OK |
| `test_tui_app` alone | venv 3.14.7 | 164 | 197.4 and 197.8 s; 226.6 s under 22 busy loops | OK three times. Once, while four engine suites, the audit hook and the parallel sessions were running, it reported `FAILED (failures=5)`; the names were not captured and it could not be made to fail again |
| `test_tui_view` alone | venv 3.14.7 | 111 | 0.5 s | OK |

### Flakiness of the engine suite (3.14.7)

| Runs | How | Failures |
|---|---|---|
| 4 | the first `make test` in a fresh `git archive` copy | **4**: `HugeLineTest.test_scan_memory_stays_bounded` (103,156 to 104,128 kB against 102,400) |
| 6 | second and third `make test` in three of those copies | 0 for that test |
| 43 | `python3 -m unittest discover -s tests`, bytecode cache present | **4**: `test_two_processes_undoing_the_same_import` 3 times, one not captured by name |
| 2 | test modules in shuffled order (seeds 11, 22) | 0 |
| 2 | test classes in shuffled order (seeds 33, 44) | 1: `HugeLineTest` (105,212 kB), seed 33 |
| 1 | every test in shuffled order (seed 55) | 0 |
| 2 | a copy with no bytecode cache and `-B` (the mutation harness, unmutated) | 2: `HugeLineTest` (103,284 and 103,312 kB) |

No other order dependence was seen.

### Coverage (`make coverage`: branch coverage, all suites in one process)

| Module | Stmts | Miss | Branch | BrPart | Cover | REVIEW (statements only) |
|---|---|---|---|---|---|---|
| `api.py` | 632 | 11 | 196 | 7 | 98% | 94% |
| `blake3.py` | 52 | 0 | 10 | 0 | 100% | new |
| `cli.py` | 614 | 29 | 204 | 18 | 94% | 87% |
| `discover.py` | 256 | 18 | 114 | 6 | 92% | 92% |
| `errors.py` | 24 | 0 | 0 | 0 | 100% | 100% |
| `export.py` | 97 | 3 | 30 | 3 | 95% | new |
| `fsutil.py` | 247 | 34 | 56 | 12 | 85% | not listed |
| `importer.py` | 527 | 30 | 164 | 10 | 94% | 92% |
| `index.py` | 545 | 9 | 200 | 9 | 98% | 96% |
| `journal.py` | 135 | 5 | 42 | 1 | 97% | new |
| `launch.py` | 312 | 5 | 106 | 2 | 98% | 96% |
| `logs.py` | 94 | 3 | 16 | 0 | 97% | new |
| `model.py` | 52 | 0 | 0 | 0 | 100% | 100% |
| `mounts.py` | 242 | 3 | 74 | 2 | 98% | 96% |
| `pathmap.py` | 290 | 0 | 128 | 1 | 99% | 99% |
| `providers/__init__.py` | 15 | 1 | 2 | 1 | 88% | not listed |
| `providers/antigravity.py` | 391 | 7 | 158 | 7 | 97% | 97% |
| `providers/base.py` | 334 | 9 | 106 | 3 | 97% | 96% |
| `providers/claude.py` | 307 | 7 | 144 | 2 | 98% | 97% |
| `providers/codex.py` | 368 | 11 | 166 | 4 | 97% | 96% |
| `providers/gemini.py` | 270 | 6 | 120 | 4 | 97% | 97% |
| `providers/grok.py` | 325 | 10 | 138 | 3 | 97% | 97% |
| `textsafe.py` | 18 | 0 | 4 | 0 | 100% | new |
| `unimport.py` | 235 | 0 | 90 | 1 | 99% | new |
| `tui/__init__.py` | 21 | 2 | 4 | 0 | 92% | not listed |
| `tui/__main__.py` | 23 | 23 | 8 | 0 | 0% | 0% |
| `tui/app.py` | 1302 | 37 | 354 | 39 | 95% | 95% |
| `tui/engine.py` | 61 | 4 | 0 | 0 | 93% | 95% |
| `tui/view.py` | 627 | 3 | 236 | 3 | 99% | 99% |
| **total** | 8419 | 270 | 2870 | 138 | 96% | |

### Mutations

81 changes of one safety-relevant line or check each, applied one at a time to a private
copy, the engine suite run on 3.14.7, the copy restored (`mut/mutations.py`,
`mut/mutations2.py`). A mutation counts as caught only by a test other than the two flaky
ones of section 1, unless the flaky memory test is the test for it.

- **Caught by an ordinary test: 75.** In `fsutil` (the two looks at a file's kind in
  `open_ro`, `O_NONBLOCK`, the private mode, tightening a wider mode, a relative folder, the
  process lock), `importer` (`..`, the native root, the id grammar, `_place`'s look, file
  mode, the roll back of placed units, free space, the sweep of a live import, appending to
  what is no file, the lock, the read-back, links in `file_copy` and `tree`), `unimport`
  (files and directories below the store, another store, used-since, the modification time,
  the tool's extras, the lock, old records), `launch` (options, NUL, id grammar, a wrapper
  that is not installed, the handoff id, the notification's `-`, a foreign session, the
  check in `plan_new`, the terminal override, the cleaned reason), `textsafe`, `index`
  (both id checks, the title, the cache's mode), `providers/base` (`trusted_schema`,
  `query_only`, the schema check, what is not a regular file, the time range), `mounts`
  (the id form, the session of its own, the tool's last line), `api` (export into a store,
  the sweep of what is no file, forgetting a mounted drive, the id for an undo, offline),
  `journal` (mode, a torn line, what an undo takes back), `logs` (cleaning, mode), `export`
  (`O_EXCL`, cleaning), `cli` (`_out`, `_err`, the progress line, the second signal),
  `pathmap` (a relative directory, a file as a folder), and the three adapters' name checks
  and `.claude.json`'s cap.
- **Caught only by the flaky memory test: 2.** M37 (an over-long line is kept before it is
  dropped: the child reports 231,296 kB) and M66 (no line cap: the test's warning assertion
  fails). See A-07.
- **Survived: 3.** M16, M27, M79 (A-23).
- **Hangs the suite: 1.** M83 (`fsutil._real_dir` follows links, so directory walks do):
  the suite did not finish in 900 s. No test fails for it and no test has a time limit, so
  in CI this would be a job that runs until the service ends it.

The TUI was mutated apart (TUI session, `tui/mutate.py`): 35 changes in `tui/app.py` and
`tui/view.py`, the whole TUI suite (275 tests, about 200 s) for each. **25 caught, 10
survived**, all ten of them among the 18 that take a `clean` or a `markup=False` away
(A-15). Two of the ten were run again by the main session in copies of their own
(`mut/tui_two.sh`): with `"cwd": session.cwd or EMPTY` in `detail_fields`, and with
`return parts[-1] if parts else EMPTY` in `project_name`, `Ran 275 tests … OK` both times
(198.1 s and 197.7 s). The packaging session tried 36 more in the scripts, `pyproject.toml`,
the launcher and `tui/__init__.py`: 25 caught, 11 survived (A-23).

### The real machine (read-only, a copy of the index, a temp state directory)

136 sessions: Claude 91 native and 23 offline, Codex 2 and 1, Grok 14 and 1, Antigravity 0
and 4. Two mounts (`/`, `/home`); the Windows volume is `attached, not mounted`; 4 stores
here, 6 on drives not mounted, 101 skipped; 0 warnings. `claude`, `codex`, `grok`, `agy`:
installed; `gemini`: `not installed (the command on PATH would install it)`.

| Command | Exit | Time |
|---|---|---|
| `list --json` on the index as it stands | 0 | 0.07 s |
| `verify` before any scan | **1** (44 differences, A-20) | 2.18 s |
| `scan` (first on the copy: 120 parsed again, the readers are newer than the cache) | 0 | 1.47 s |
| `scan` again | 0 | 0.07 s |
| `scan --deep` (walk 0.08 s) | 0 | 0.15 s |
| three scans, three `list --json` | 0 | byte-identical, 136 sessions |
| `list` (136 rows), `list --mounted` (107 rows), `status` (168 lines), `map` | 0 | 0.07 s each |
| `verify` after the deep scan | 0 | 2.17 s |
| `show`, `export`, `export --format json` of two native sessions (149 and 40 messages) | 0 | 0.08 to 0.09 s; every JSON line parses |
| `open --dry-run`, `continue --dry-run` of a native Claude, Codex and Grok session | 0 | 0.07 s each; `cd /home/tanuj && omarchy-launch-tui --app-id=org.omarchy.<tool> …` |
| `show`, `import --dry-run`, `continue --dry-run`, `export`, `open --dry-run` of an offline session | 1, `the drive 22EEA841EEA80F5B is not mounted` | 0.07 s each |

On the real machine `import` and `continue` of the offline session were run with
`--dry-run` only (the audit's rules allow no real import there); without it they refuse the
same way in the throwaway homes. No native Antigravity session exists to dry-run.

The TUI through the real launcher in a private pty of 120 by 40, three starts: first paint
after **0.16, 0.17 and 0.17 s**, exit status **0** each time, no traceback, no ERROR line in
the (temp) log. Keys sent: `q` alone the first time; then `j j k 1 2 3 0 ? Esc / c l a Esc v
q q`. Never Enter, `o`, `i`, `t`, `c` outside the filter, `u` or `m`.

### Other measurements

| What | Result |
|---|---|
| Wheel | 206,010 bytes, 36 entries, built in 1.2 s; install with `[tui]` 1.7 s |
| `install.sh` | 3.0 s first run, 0.37 s again; `--dev` 2.6 s |
| TUI first paint, empty home | 0.16 s (wheel), 0.16 to 0.17 s (checkout) |
| Scan of one 200 MB line | 58 MB peak (baseline 25 MB) |
| Scan of one 31 MiB line of `[],` | 918 MB peak, 0.9 s |
| Scan of a sparse file | 4.0 s per 16 GiB |
| Scan with a 300 MB value in a 300 MB database | 625 MB peak |
| `.claude.json` with 400,000 keys | 139 MB peak, 0.4 s |
| A held lock, until the second import gives up | 30 s |

## Leftovers

Nothing under `/home` and nothing in the repository but this file (`git status --short`:
`?? docs/AUDIT.md`). No process of the audit is left running, and no
`/tmp/agent-sessions-test-*` directory.

In the session scratch directory (tmpfs, removed with the session): the `audit/` tree named
at the top, 503 MB: 280 MB the packaging session's venvs, wheel and sdist builds (`pkg/`),
158 MB the Python 3.11 venv of the gates (`gates311/`), the rest copies of the repository
(`gates/`, `mut/`, `feat/repo`, `tui/copy`, `tui/fixcopy*`), scripts, logs and the three
snapshots of the real home (`before.json`, `after1.json`, `after2.json`: names, sizes, modes,
times and digests, no contents). `feat/t10_grok_sandbox.out` holds the title and id of one
real Grok session that was copied read-only into the sandbox. No throwaway home is left:
all were `tempfile.TemporaryDirectory`.

One command of the audit was held by the machine's permission hook for ten minutes and
never ran: its test data, a made-up log line, held the text of a destructive shell command.
It was replaced by one without that text (`b/t17_log.py`).
