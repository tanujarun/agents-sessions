# On-disk formats, resume commands, import recipes

Investigated 2026-10-12 on this machine, read-only. Sources of every claim:

- `[verified]` — seen in the store data, in `--help` output, in the tool's own shipped
  documentation, or in the tool's own source/strings as embedded in its binary or bundle
  (the source of a claim is named where it is not the data).
- `[inferred]` — deduced; not observed end to end. No agent CLI was started and no session
  was resumed, so **everything about what a tool does at resume time is unproven until a
  live test**, even where the lookup code was read. See "Open questions".

The one exception is in "Phase 2 findings": four model-free Grok commands were run in a
throwaway home with no network. What they showed is tagged `[verified, sandbox]`.

Tool versions: `claude` 2.1.296, `codex` 0.162.0, `grok` 1.0.46, `agy` 1.3.3. `gemini` is not
installed (see Gemini CLI). Stores were written by: Claude 2.1.281–2.1.296 (Linux),
2.1.229–2.1.295 (Windows); Codex 0.156.1 (Linux), 0.150.1 (Windows); Grok 1.0.46 (Linux),
1.0.34 (Windows); Gemini CLI 0.54.0 (Windows). The commands on PATH are wrappers, and the
Grok npm package is 1.0.50 while the binary it starts is 1.0.46: see "Phase 2 findings".

Throwaway scripts that produced the numbers are in `scratch/` (git-ignored): `walk.py`,
`claude_count.py`, `codex_count.py`, `grok_count.py`, `agy_count.py`, `drives.py`,
`build_fixtures.py`. Fixtures and their expected values: `tests/fixtures/README.md`.

## Rules common to all providers

- **Listed session** (what `list_sessions` returns) = a top-level conversation that has at
  least one real user prompt **or** at least one assistant/model message. Everything else
  is skipped with no warning (it is not malformed, just not a conversation).
- JSONL: read as bytes, split on `\n`, strip a trailing `\r`, skip empty lines, `json.loads`
  each line inside `try`; a line that fails (including a partially written last line of a
  live session) is skipped. Decode as UTF-8 with `errors="replace"`. No BOM, no CRLF and no
  unparseable line were found in any transcript, rollout or chat file of any store `[verified]`,
  and every JSONL file ended with `\n`; do not rely on it.
- Timestamps are UTC everywhere. Forms seen: `2026-10-03T14:25:33.100Z` (ms),
  `2026-09-27T23:35:21.831252584Z` (ns, Linux) / `2026-08-29T08:31:15.3316788Z` (100 ns,
  Windows), `2026-08-29 09:11:11.2289146+00:00` (SQLite text, 6–7 fraction digits), epoch
  seconds / milliseconds (ints), protobuf `{1: seconds, 2: nanos}`. Truncate fractions to 6
  digits and turn the space into `T` before `datetime.fromisoformat`.
- **SQLite**: every DB found is a WAL-mode file (header byte 18 = 2). Opening one with
  `mode=ro` only is not side-effect free `[verified]`: on a DB with no sidecars it creates
  `-shm` and `-wal` next to it (reproduced on copies of the Antigravity DBs), and on a DB that
  already has them it rewrites the `-shm` (the mtime of `~/.codex/state_5.sqlite-shm` and
  `thread_history_1.sqlite-shm` changed during this investigation; nothing else did). **Open
  every DB, native ones too, with `file:<quoted path>?mode=ro&immutable=1`**; quote the path
  with `urllib.parse.quote` (spaces, `%`, `#`, `?`). Immutable mode ignores a hot `-wal`, so
  rows not yet checkpointed are invisible; where that matters copy `db` + `db-wal` into
  `~/.cache/agent-sessions/` and open the copy.
- Mounts are `relatime`: reading a file can update its atime on the NTFS drive. Open with
  `os.O_NOATIME` where the platform has it (ignore `PermissionError`).
- **Size caps.** A file on another drive can hold anything, so every read is bounded; what is
  over a cap is skipped with a warning (a title, a folder and a note are cut or left out).

  | what | cap | constant |
  |---|---|---|
  | one line of a JSONL file (with its line break) | 32 MiB | `providers.base.JSONL_LINE_MAX` |
  | a Gemini chat file (`.jsonl`, and the older `.json`) | 64 MiB | `providers.gemini._CHAT_MAX`, `_LEGACY_MAX` |
  | the Codex `session_index.jsonl` | 64 MiB | `providers.codex._INDEX_MAX` |
  | the text a title is made from | 64 Ki characters | `providers.base.TITLE_SOURCE_MAX` |
  | a recorded working directory | 32,767 characters | `index.CWD_MAX` |
  | a session's note | 200 characters | `index.NOTE_MAX` |

  The longest real line is 2.74 MB; the caps are far above anything seen and exist for what
  was not.
- A time before 1990 or after 2100 is not a time (`parse_ts` gives none): a damaged or made-up
  value, or a file written while the clock was unset. An epoch number is read in the unit its
  source fixes (a file's modification time: seconds); only a number in a field documented as
  text has its unit taken from its magnitude, and then only a result inside that range counts.

---

## Claude Code

### 1. Store signature

- A directory named `.claude` that contains a **directory** `projects` `[verified]`:
  `(p / "projects").is_dir()`. `dir_names = {".claude"}`.
- Project-level `.claude` folders hold only `settings.json`, `settings.local.json`, `agents/`,
  `commands/`, `hooks/`, `skills/`, `launch.json`, `scheduled_tasks.lock`,
  `package-manager.json`, `dev-matcha.cjs`; none has `projects/` (13 such folders on the four
  mounts, including the copied profile `Zephyro/…/Alt Device Files/.claude`, which holds
  `agents/`, `launch.json`, `settings.local.json`, `skills/` and **no sessions**) `[verified]`.
- `<home>/.claude.json` sits **beside** `.claude`, not inside it. Its `projects` object is keyed
  by real cwd strings (path-map evidence, see Drive letters) `[verified]`.
- Claude Desktop's "local agent mode" keeps one private config dir per desktop session that also
  passes the signature: `…/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/local-agent-mode-sessions/<uuid>/<uuid>/local_<uuid>/.claude`
  (2 found, one transcript each, `entrypoint: "local-agent"`) `[verified]`. They are desktop-app
  history (a SPEC non-goal); recommended: skip any store whose path contains a segment
  `local-agent-mode-sessions`, and report it in `status`.

### 2. Layout

```
<store>/projects/<enc-cwd>/<session-uuid>.jsonl                 the transcript (one session)
<store>/projects/<enc-cwd>/<session-uuid>/subagents/agent-<17 hex>.jsonl      sub-agent transcript
<store>/projects/<enc-cwd>/<session-uuid>/subagents/agent-<17 hex>.meta.json  {agentType, description, toolUseId, spawnDepth, …}
<store>/projects/<enc-cwd>/<session-uuid>/tool-results/<toolu_…|id>.txt      large tool outputs moved out of the transcript
<store>/projects/<enc-cwd>/<session-uuid>/custom-title.json    {"customTitle": …} (desktop sessions only)
<store>/projects/<enc-cwd>/<uuid>.desktop-released.json        desktop marker, no transcript
<store>/projects/<enc-cwd>/memory/*.md                          project memory, not a session
<store>/file-history/<session-uuid>/                            checkpoints for /rewind
<store>/session-env/<session-uuid>/                             per-session env snapshots
<store>/history.jsonl                                           prompt history {display, pastedContents, timestamp(ms), project, sessionId}
<store>/sessions/<pid>.json                                     live-process registry {pid, sessionId, cwd, startedAt, kind, …}; stale on a foreign store
```

`Session.files` = the transcript, plus the `<session-uuid>/` directory beside it when present.
`file-history/<id>/` and `session-env/<id>/` belong to the session but are not needed to resume
`[inferred]`.

`<enc-cwd>` `[verified, source in binary: function OO]`: `re.sub(r"[^a-zA-Z0-9]", "-", cwd)`;
when the result is longer than 200 chars it is `result[:200] + "-" + base36(abs(h))` where `h` is
the Java-style string hash `h = (h*31 + ord(c)) | 0` over the **original** cwd (UTF-16 code
units, 32-bit signed). The cwd is the directory the session was **started** in.
Verified against all 17 project dirs:

| cwd | directory |
|---|---|
| `/home/tanuj/Work` | `-home-tanuj-Work` |
| `/run/media/tanuj/Zephyro/AI Projects/Prompt-Maxxer AI STT` | `-run-media-tanuj-Zephyro-AI-Projects-Prompt-Maxxer-AI-STT` |
| `F:\AI Projects\Website Works` | `F--AI-Projects-Website-Works` |
| `C:\Users\arunt\AppData\Roaming\Claude\scratch-workspaces\<u>\<u>\scratch-2026-10-08-e01e1a` | `C--Users-arunt-AppData-Roaming-Claude-scratch-workspaces-<u>-<u>-scratch-2026-10-08-e01e1a` |
| `d:/Fable 5/Crime YT` (from `.claude.json`) | `d--Fable-5-Crime-YT` |

Space, `:`, `\`, `/`, `.`, `_` and every non-ASCII character each become one `-`; case is kept
(`d--` and `D--` are different directories). The encoding is lossy: never decode a directory
name into a path; read `cwd` from the transcript.

### 3. One session, and what is excluded

A candidate is a file `projects/<dir>/<name>.jsonl` at exactly that depth whose stem is a
lower-case UUID. One file = one session; `sessionId` in every record equals the stem (all 133
files) `[verified]`.

| kind | how to recognise | action |
|---|---|---|
| sub-agent transcript | under `<uuid>/subagents/`, name `agent-<17 hex>.jsonl`; records have `isSidechain: true`, `agentId`, and the **parent's** `sessionId` | never a candidate (depth rule). 27 native, 21 Windows |
| bookkeeping-only file | no record has a `parentUuid` key (the tool's own test: `"parentUuid":` in the first or last 64 KiB) `[verified, source: FYt]`. Seen: 2-line files holding only `ai-title` + `agent-name`, left when a hand-off target never started | exclude. 2 native |
| empty session | has message records but **no real prompt and no `assistant` record**: startup-only (hook `attachment` records, `mode`, `permission-mode`…) or local slash command only (`/model`, `/mcp`, `/login`) | exclude. 4 native, 14 Windows |
| superseded parent | last records include `{"type":"continued-in","continuedInSessionId":<child>}` with no `user`/`assistant` after it. The child file starts with `ai-title` + `agent-name` and repeats the parent's history from the same first timestamp | **list both**; set `note = "continued in <child id>"` on the parent. Hiding the parent when the child is listed is a UI choice. 5 native parents (3 with a real child, 2 whose child is a stub or missing), 3 Windows |
| sidechain top-level | first message record has `isSidechain: true` | exclude (none found) |
| summary-only file | only `{"type":"summary",…}` records (older versions) `[inferred]` | covered by the bookkeeping rule (none found) |

**Real prompt** (the tool's own rule `[verified, source: r8e]`): a record with `type == "user"`,
not `isMeta`, not `isCompactSummary`, whose `message.content` is a string or a list with no
`tool_result` block; take the string / each `text` block, replace `\n` by space, strip; skip
empty; if it contains `<command-name>X</command-name>` remember `X` as the command fallback and
skip; if it contains `<bash-input>C</bash-input>` the prompt is `"! " + C`; skip if it matches
`^(?:\s*<[a-z][\w-]*[\s>]|\[Request interrupted by user[^\]]*\])`; otherwise it is a real prompt.

Record types seen (top-level files): `user`, `assistant`, `attachment`, `system`, `last-prompt`,
`mode`, `permission-mode`, `atis-latch`, `ai-title`, `custom-title`, `agent-name`, `agent-color`,
`agent-setting`, `queue-operation`, `file-history-snapshot`, `file-history-delta`, `cost-state`,
`bridge-session`, `dev-mods`, `continued-in`, `frame-link`. In the source but not in the data:
`summary`, `tag`, `relocated`, `worktree-state`, `memory-mode`, `isolation-latch`. Unknown types
must be ignored.

### 4. Fields

| field | source | fallback |
|---|---|---|
| `id` | file stem | — |
| `title` | last record of each kind wins; order `agent-name.agentName` → `custom-title.customTitle` → `ai-title.aiTitle` → `summary.summary` `[verified, source: s6]` | first real prompt → command fallback (`<command-name>` value, plus the `<command-args>` text when not empty) → `id[:8]` |
| `cwd` | last `{"type":"relocated"}.relocatedCwd` if any `[source only]`, else the **first** record that has a string `cwd` | none: file is bookkeeping-only |
| `started` | **minimum** `timestamp` over records that have one | file mtime |
| `updated` | **maximum** `timestamp` | file mtime |
| `messages` | (number of real prompts) + (number of distinct `message.id` over `assistant` records; records without one count singly) | — |
| `note` | `continued in <id>` when the superseded rule matches | — |

- `agentName` equals `customTitle` or `aiTitle` in all 12 files that have one `[verified]`.
- `cwd` changes inside a file when the session `cd`s (45 of 96 native files have 2–15 distinct
  values); only the first one names the project directory (all 133 files) `[verified]`.
- Records are **not** in timestamp order (hook `attachment` records can carry earlier times);
  hence min/max.
- A fork child's `started` is the original conversation's start (history is copied).
- `ai-title`, `last-prompt`, `mode`, `cost-state`… carry no `timestamp`.
- Title records are re-appended near the end of the file as it grows, so the tool itself reads
  only the first and last 64 KiB (`rw = 65536`) `[verified, source: YUe]`. A cheap reader can do
  the same for `id`/`title`/`cwd`, but `started`/`updated`/`messages` and the empty test need the
  whole file. Whole-file parse is affordable: 96 native files, 535 MB, 0.93 s; 37 Windows files,
  378 MB, 0.61 s (warm cache, `json.loads` per line). Biggest file 123.9 MB (Windows), 115.9 MB
  native; longest line 2.74 MB. Read line by line; never `read()` + `splitlines()` in the adapter.
- `entrypoint` values: `cli`, `sdk-cli`, `claude-desktop` (desktop sessions live in the normal
  store and are ordinary transcripts), `local-agent`.

### 5. Resume

- argv: `["claude", "--resume", "<session-uuid>"]` `[verified, --help: "-r, --resume [value]
  Resume a conversation by session ID, or open interactive picker with optional search term"]`.
  `-c/--continue` is "the most recent conversation in the current directory"; `--fork-session`
  resumes under a new id.
- Lookup in 2.1.296 `[verified, source: Ike → cTe/fVr → Rho → Cho → cRr]`, in this order:
  1. `<projects>/<enc(process cwd)>/<id>.jsonl` (plus, for cwds whose encoding exceeds 200 chars,
     sibling dirs with the same 200-char prefix whose transcripts record that cwd);
  2. the same under every other git worktree of the current repository;
  3. a scan of **every** directory under `projects/` for `<id>.jsonl` that has message records;
     used only when exactly one directory has it.
  If all fail: `No conversation found with session ID: <id>`, exit 1.
  So resume is cwd-first with a global fallback. What 2.1.296 does after step 3 (it appears to
  continue in the process cwd and keep appending to the found file) is `[inferred]`. Older
  versions may lack steps 2–3.
- Launcher: `cd <cwd> && claude --resume <id>`. When the recorded cwd no longer exists: start
  from `$HOME` and rely on step 3 `[inferred]`; say so in `note`. Never `cd` to a parent that
  "looks close".
- No list/sessions subcommand exists `[verified, --help]`. `claude agents` lists background
  sessions only. The source also accepts `--resume <path ending in .jsonl>` (undocumented;
  do not use).
- `CLAUDE_CONFIG_DIR` moves the whole store, `projects/` included (Phase 2, Q2). Starting a
  new session with a first prompt: Phase 2, Q3.

### 6. Import

Verdict: **supported, copy-only** `[inferred from the verified lookup]`.

1. `dst_dir = ~/.claude/projects/<enc(mapped cwd)>/`; create it if missing.
2. Refuse when `~/.claude/projects/*/<id>.jsonl` exists anywhere (already imported, or a
   collision; step 3 of the lookup needs the id to be unique).
3. Copy `<id>.jsonl` byte for byte (temp name, then rename). Copy the `<id>/` side directory
   (`subagents/`, `tool-results/`) when present.
4. Rewrite nothing. The tool finds the file by path, not by recorded fields; `sessionId`
   already equals the stem. Re-serialising a 120 MB transcript only to change `cwd` strings is
   the larger risk.
5. Do not copy `file-history/`, `session-env/`, `history.jsonl` rows, `sessions/<pid>.json`.

Verified: directory encoding, lookup order, title/has-message logic. To confirm live: that
2.1.296 resumes a transcript whose records carry Windows cwds, Windows tool paths and an older
`version` without complaint (risk: low to medium); that `/rewind` degrades gracefully without
`file-history` (low). If the live test shows the transcript's head `cwd` matters, the tool's own
mechanism is a trailing `{"type":"relocated","relocatedCwd":<cwd>,"sessionId":<id>}` record
`[source only, never seen in data]` — do not write it without a test.

### 7. Quirks

- Windows `cwd` forms inside one store: `C:\Users\arunt` (transcripts, `history.jsonl`),
  `C:/Users/arunt` and `d:/Fable 5/Crime YT` (`.claude.json` keys) — both slash forms and a
  lower-case drive letter.
- Desktop-app cwds `C:\Users\arunt\AppData\Roaming\Claude\…` do not exist at that path: the
  MSIX package redirects them to `…\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\…`
  `[verified for scratch-workspaces]`.
- Two project dirs have `memory/` only and no transcript; three have one transcript that is empty.
- The native store is being written while the scanner runs (the running session and its
  sub-agents); counts move, and the last line can be incomplete.

---

## Codex

### 1. Store signature

- A directory named `.codex` that contains a directory `sessions` `[verified]`.
  `dir_names = {".codex"}`. Other files usually present: `config.toml`, `auth.json`,
  `state_5.sqlite`, `version.json`; `session_index.jsonl`, `history.jsonl` and
  `thread_history_1.sqlite` can be absent (the Windows store has none of the three).
- `sessions/` + `config.toml` + `auth.json` is **not** enough: `.grok` has the same three.
  Codex's `sessions/` holds `YYYY/` directories; Grok's holds percent-encoded names.
- **Mirror stores** `[verified]`: the Orca app keeps its own `CODEX_HOME` at
  `~/.config/orca/codex-runtime-home/home` (Linux) and
  `…/Users/arunt/AppData/Roaming/orca/codex-runtime-home/home` (Windows). The directory is named
  `home`, so a name-based walk does not find it. Its rollout files are **hard links** to the ones
  in `.codex/sessions` (same inode, `st_nlink == 2`). Do not add `home` to `dir_names`. If these
  paths are ever probed, drop sessions whose `(st_dev, st_ino)` or thread id is already listed.
  The one thing only the Windows mirror has is a `session_index.jsonl` (thread names).

### 2. Layout

```
<store>/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDTHH-MM-SS>-<thread-uuid>.jsonl
<store>/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDTHH-MM-SS>-<thread-uuid>_<segment-uuid>.jsonl
<store>/archived_sessions/…            archived threads (name in the binary; directory not present here)
<store>/session_index.jsonl            {"id", "thread_name", "updated_at"} per line, append-only
<store>/history.jsonl                  {"session_id", "ts" (epoch s), "text"}
<store>/state_5.sqlite                 table threads (index of rollouts; derived)
<store>/thread_history_1.sqlite        thread_turns / thread_items / thread_history_projection_state (derived)
<store>/shell_snapshots/<thread-uuid>.<ns>.sh, thread-writer-locks/<thread-uuid>.lock   not sessions
```

The date path and the time in the file name are **local time** of the creating machine; the
ids are UUIDv7. Regex:
`^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(<uuid>)(?:_(<uuid>))?\.jsonl$`.
The binary also knows `.jsonl.zst` (compressed rollouts); none exist. Python < 3.14 has no zstd:
skip such a file with a warning.

### 3. One session, and what is excluded

One session = one **thread** = all rollout files whose first-line `session_meta.payload.id` is
the same. `Session.files` = all of them, sorted by first `ordinal`.

**Two-id files** `[verified]`: `rollout-<ts>-<A>_<B>.jsonl` is a continuation segment of thread
`A`, not a session of its own. Its `session_meta.payload` has `id == session_id == A` and
`history_base: {"thread_id": A, "end_ordinal_exclusive": N, "end_byte_offset": M}`; its records
continue the thread's `ordinal` numbering from `N`. The base file still contains its own
records with `ordinal >= N` (here a turn ended by `turn_aborted`); they are superseded. `B` appears
only in the file name and as a `thread_id` in `thread_history_1.sqlite`. `state_5.sqlite`
`threads.rollout_path` for `A` points at the **segment** file. Why the tool cut a segment here
(resume after an aborted turn) is `[inferred]`.

| kind | how to recognise | action |
|---|---|---|
| continuation segment | second uuid in the name / `history_base` in `session_meta` | merge into its thread |
| non-interactive run | `session_meta.payload.source == "exec"` (`originator: "codex_exec"`) | exclude by default, as `codex resume` does unless `--include-non-interactive` `[verified, --help]`. 1 Windows |
| sub-agent thread | `session_meta.payload.source` not a plain string, or `thread_source != "user"`, or an `agent_nickname`/`agent_role` key `[inferred from the threads schema and thread_spawn_edges; none exist]` | exclude |
| empty thread | no `UserMessage` and no `AgentMessage` item | exclude (none found) |
| lock / snapshot files | `thread-writer-locks/*.lock`, `shell_snapshots/*.sh` | never candidates |

### 4. Fields

Every line is `{"timestamp", "ordinal", "type", "payload"}`. `type` values: `session_meta`,
`turn_context`, `response_item`, `event_msg`, `world_state`, `token_usage_record`.

| field | source | fallback |
|---|---|---|
| `id` | `session_meta.payload.id` of the first line | first uuid in the file name |
| `title` | `session_index.jsonl`: `thread_name` of the **last** line whose `id` matches (renames append) | first user message text → `id[:8]` |
| `cwd` | `session_meta.payload.cwd` of the base file | `turn_context.payload.cwd` |
| `started` | `session_meta.payload.timestamp` of the base file | first line `timestamp` |
| `updated` | last `timestamp` of the last segment | file mtime |
| `messages` | number of `event_msg` records with `payload.type == "item_completed"` and `payload.item.type` in `UserMessage`, `AgentMessage`, after dropping, for each segment, earlier records with `ordinal >= history_base.end_ordinal_exclusive` | — |

- First user message: first such `UserMessage`; text = the `text` values of
  `payload.item.content[]` joined by a space. Do **not** use `response_item` messages with
  `role == "user"`: the first of those is an injected `<environment_context>` block, and
  `role == "developer"` messages are instructions.
- `session_meta.payload` other keys: `originator` (`codex-tui`, `codex_exec`), `cli_version`,
  `source` (`cli`, `exec`), `thread_source` (`user`), `model_provider`, `history_mode`
  (`paginated`), `context_window.window_id`, `base_instructions.text` (18–21 kB),
  `runtime_workspace_roots` (0.156.1 only).
- SQL equivalents (not needed; schema differs by version — the 0.150.1 DB lacks `originator`,
  `daybreak_enabled`, `creator_user_id`, `creator_account_id`):
  `SELECT id, rollout_path, cwd, title, name, first_user_message, source, archived, created_at_ms, updated_at_ms FROM threads`.
  `name` = the `thread_name`; `title` = `first_user_message` = `preview`. On Windows `cwd` is
  stored as `\\?\F:\AI Projects\Website Works` (verbatim prefix) and `rollout_path` is an absolute
  Windows path.
- Read whole files: biggest rollout 2.09 MB (Windows), 0.81 MB native; longest line 41 kB.

### 5. Resume

- argv: `["codex", "resume", "<thread-uuid>"]` `[verified, codex resume --help: "[SESSION_ID]
  Session id (UUID) or session name. UUIDs take precedence if it parses"]`. `-C, --cd <DIR>`
  sets the working root. `codex fork <id>` forks.
- By id the lookup is **global**: only the picker is cwd-filtered (`--all` "disables cwd
  filtering") `[verified, --help]`. Strings in the binary show the order: `threads` row in
  `state_5.sqlite`, then a file-name search under `sessions/` (`find_thread_path_by_id_str_in_subdir`
  with outcomes `missing_row`, `stale_db_path`, `mismatched_db_path`, `falling_back`) followed by
  `read_repair_rollout_path: upsert_needed` `[verified strings; behaviour inferred]`.
- When the session's cwd differs from the process cwd the TUI asks: `Use session directory (…)`
  / `Use current directory (…)` (and `Always use …`) `[verified strings]`. Launcher: `cd <cwd> &&
  codex resume <id>`. When the cwd no longer exists: start from `$HOME`; the user answers the
  prompt `[inferred]`.
- Possible no-model check `[verified, --help; not run]`: `codex migrate-rollouts --thread <id>
  --json` without `--apply` "only reports eligible sessions" (it is about legacy rollouts and may
  report nothing for `paginated` ones). `codex agents` needs the app-server daemon.
- `CODEX_HOME` moves the store, `CODEX_SQLITE_HOME` the SQLite files (Phase 2, Q2).

### 6. Import

Verdict: **supported: copy, rewrite `cwd` in two record types** `[inferred]`.

1. Refuse when any `~/.codex/sessions/**/rollout-*-<id>*.jsonl` exists.
2. Copy every rollout file of the thread to `~/.codex/sessions/<same YYYY/MM/DD>/<same name>`.
3. In the copies, parse and re-serialise **only** these lines (all are small), mapping paths
   through the path map and leaving every other line byte-identical:
   `session_meta`: `payload.cwd` (and `payload.runtime_workspace_roots[]` when present);
   `turn_context`: `payload.cwd`, `payload.workspace_roots[]`.
   Without this the tool should still resume but will offer a "session directory" that is a
   Windows path `[inferred]`. `event_msg/thread_settings_applied.payload.thread_settings.cwd`
   and tool call payloads are history; leave them.
4. If the source has a name (`session_index.jsonl`, or the Orca mirror's), append
   `{"id","thread_name","updated_at"}` to `~/.codex/session_index.jsonl`. The file may be absent
   and the tool tolerates that (the Windows store has none) `[verified]`.
5. Never write `state_5.sqlite` or `thread_history_1.sqlite`; both are derived
   (`backfill_state`, `thread_history_projection_state` hold watermarks and byte offsets), and the
   file-name fallback plus read-repair is expected to register the thread `[inferred, medium risk]`.

To confirm live: that 0.162.0 finds a rollout that has no `threads` row; that it resumes a
0.150.1 rollout (cross-version; `history_mode` is `paginated` in both); that a rewritten
first line is accepted although `history_base.end_byte_offset` of a later segment refers to the
base file's bytes (only the base's first line and `turn_context` lines change length — **if a
thread has a segment, the base file's byte offsets shift; for such threads keep the base file
byte-identical and rewrite only the last segment**, risk medium).

### 7. Quirks

- Local time in paths and names, UTC inside.
- `session_index.jsonl` `updated_at` has 9 fraction digits on Linux, 7 on Windows.
- Hot WALs on the Windows store: `state_5.sqlite-wal` 57 kB, `logs_2.sqlite-wal` 74 kB (mirror:
  165 kB). Immutable reads of `threads` gave the same two rows as a WAL-applied copy.
- A foreign exec session's cwd was a Claude scratchpad under `AppData\Local\Temp` (deleted).

---

## Grok

The tool ships its own documentation in the store (`<store>/docs/user-guide/17-sessions.md`,
`<store>/README.md`); statements marked "docs" come from the 1.0.46 copy.

### 1. Store signature

- A directory named `.grok` that contains a directory `sessions` `[verified]`.
  `dir_names = {".grok"}`. `GROK_HOME` can relocate it `[docs]`; without it the store is
  `$HOME/.grok` `[verified, sandbox]`.

### 2. Layout

```
<store>/sessions/<enc-cwd>/<session-uuid>/summary.json          index entry (plain JSON)
<store>/sessions/<enc-cwd>/<session-uuid>/chat_history.jsonl    messages sent to the model
<store>/sessions/<enc-cwd>/<session-uuid>/updates.jsonl         ACP update stream; what /resume replays [docs]
<store>/sessions/<enc-cwd>/<session-uuid>/events.jsonl, system_prompt.txt, prompt_context.json,
        tool_definitions.json, signals.json, usage.json, rewind_points.jsonl, plan.json, plan_mode.json,
        announcement_state.json, resources_state.json, title_refresh_idx, last_recap_main_turn,
        terminal/, images/, assets/, web_fetch/, recap_requests/, compaction/, compaction_checkpoints/,
        subagents/ (meta.json per child) [docs]
<store>/sessions/<enc-cwd>/<session-uuid>/*.lock               summary.json.lock, chat_history.jsonl.lock, updates.jsonl.lock, rewind_points.jsonl.lock
<store>/sessions/<enc-cwd>/prompt_history.jsonl                {timestamp, session_id, prompt, is_bash}
<store>/sessions/<enc-cwd>/.cwd                                original path when the name was shortened [docs]
<store>/sessions/session_search.sqlite                         FTS index: meta, session_docs(session_id, cwd, updated_at, title, content, content_hash), session_docs_fts
<store>/active_sessions.json                                   [{session_id, pid, cwd, opened_at}]; stale on a foreign store
```

`Session.files` = the whole `<session-uuid>/` directory.

`<enc-cwd>` `[verified, sandbox: Phase 2, Q1]`: `urllib.parse.quote(cwd, safe="")`, that is,
`A-Z a-z 0-9 - . _ ~` stay as they are and every other byte of the UTF-8 cwd becomes `%XX` in
upper-case hex: `/home/tanuj/Work` → `%2Fhome%2Ftanuj%2FWork`; `F:\AI Projects\Website Works` →
`F%3A%5CAI%20Projects%5CWebsite%20Works`. Space is `%20` (not `+`). When that name is longer
than 255 bytes the group is `<slug>-<16 hex>` instead (slug of the last path segment, BLAKE3
of the cwd; exact rule in Phase 2, Q1); the documentation adds that the tool "records the
original path in a `.cwd` file inside the group" `[docs; no instance seen]`. The cwd is the
physical path (symbolic links resolved). To read a store, decode with `urllib.parse.unquote`
and prefer `summary.json` `info.cwd`; a `<slug>-<hex>` name cannot be decoded.

### 3. One session, and what is excluded

A candidate is a directory `sessions/<group>/<name>/` that contains `summary.json`.

| kind | how to recognise | action |
|---|---|---|
| empty session | `chat_history.jsonl` has no `assistant` record and no user record with `prompt_index` (only `system`, `<user_info>`, `<system-reminder>`); `session_summary == ""` | exclude. 6 native, 1 Windows |
| prompt, no reply | one user prompt, no `assistant`, `session_summary == ""` | listed (title = the prompt). 1 native |
| sub-agent session | "the child sessions live in the normal sessions tree" `[docs]`; the parent's `subagents/<child>/meta.json` names them; `summary.json` may carry `parent_session_id` ("the source session for a fork or restore") and `session_kind` `[docs / strings; no instance exists]` | exclude ids referenced by any `subagents/*/meta.json`; a session with `parent_session_id` alone (fork/restore) stays listed `[inferred]` |
| lock files, `prompt_history.jsonl`, `.cwd`, `session_search.sqlite` | not directories | never candidates |

### 4. Fields

`summary.json` keys seen: `info: {id, cwd}`, `session_summary`, `generated_title`, `created_at`,
`updated_at`, `last_active_at`, `num_messages`, `num_chat_messages`, `current_model_id`,
`chat_format_version` (1), `grok_home`, `agent_name`, `agent_id`, `attempt_id`, `request_id`,
`sandbox_profile`, `reasoning_effort`, `next_trace_turn`, `last_turn_summary`,
`last_turn_summary_prompt_id`, `last_recap`. Documented but not seen: `title_is_manual`,
`parent_session_id`.

| field | source | fallback |
|---|---|---|
| `id` | `summary.json` `info.id` (== directory name in all 22) | directory name |
| `title` | `session_summary` | `generated_title` (equal whenever both are set) → first user prompt → `id[:8]` |
| `cwd` | `info.cwd` (== `unquote(group dir)` in all 22) | `.cwd` file → `unquote(group dir)` |
| `started` | `created_at` | directory mtime |
| `updated` | `updated_at` | `last_active_at` → `summary.json` mtime |
| `messages` | from `chat_history.jsonl`: (user records that have `prompt_index` and no `synthetic_reason`) + (`assistant` records) | `num_chat_messages` |

- `chat_history.jsonl` line: `{"type": "system"|"user"|"reasoning"|"assistant"|"tool_result"|"backend_tool_call", "content": …}`.
  A real user prompt has `prompt_index` (int) and its text is wrapped:
  `content: [{"type":"text","text":"<user_query>\n…\n</user_query>"}]`; take the inside.
  Injected user records have `synthetic_reason` or start with `<user_info>` / `<system-reminder>`.
- `num_chat_messages` == number of lines of `chat_history.jsonl`; `num_messages` == number of
  lines of `updates.jsonl` (all 22) `[verified]`. Neither is a count of conversation messages.
- `session_search.sqlite` is a derived index (`meta.last_bootstrap_at`); its `title` lags
  `summary.json` (one stale row found). Do not read it. The tool builds it by itself on the
  first `grok sessions search` and adds sessions that appeared since `[verified, sandbox]`.
- Cheap read: `summary.json` alone gives everything except the empty test and `messages`.
  `chat_history.jsonl` up to 24.3 MB (Windows), 9.2 MB native; longest line 1.7 MB.
  `updates.jsonl` up to 49.6 MB; a session directory up to 123.7 MB. Do not read `updates.jsonl`.

### 5. Resume

- argv: `["grok", "--resume", "<session-uuid>"]` `[verified, --help: "-r, --resume
  [<SESSION_ID_OR_TITLE>] Resume a session by ID or title … Non-ID values match session titles
  for the current directory …; UUID-shaped values always mean IDs"]`. `--cwd <CWD>` sets the
  working directory. `-c` = most recent session of the current directory. `-s/--session-id`
  creates, never resumes.
- Scoping: listing and title lookup are per current directory `[docs]`; `grok sessions list`
  shows exactly the sessions in the group directory named after the current directory, whatever
  their `info.cwd` says `[verified, sandbox]`. For an id, the binary holds `Session found
  locally`, `Session found locally under different CWD`, `Session not found locally…`, `No
  session found with id` — so other groups are searched `[inferred from strings]`; `grok export
  <id>` does find a session in any group from any directory `[verified, sandbox]`, `grok
  --resume <id>` itself was not run.
- Launcher: `cd <cwd> && grok --resume <id>`. When the cwd no longer exists: start from `$HOME`
  `[inferred]`.
- No-model checks `[verified, --help; run in the sandbox]`: `grok sessions list [-n N]` (current
  directory only), `grok sessions search <query>` (current directory only, by `info.cwd`),
  `grok export <id>` (Markdown transcript to stdout; any directory; exit 1 and `Error: Session
  '<id>' not found.` for an unknown id), `grok usage <id>` (JSON; exit 1 with `No usage recorded`
  for a session without `usage.json`, so not an existence probe). None needs a login. `list` and
  `search` also try a remote search and wait about 5 s for it when offline. Details: Phase 2, Q1.
- `grok sessions delete <id>` exists `[verified, --help]`: never call it.

### 6. Import

Verdict: **supported for any folder: copy, rewrite two values of `summary.json`** `[verified,
sandbox: `sessions list` and `export`; the resume itself not run]`.

1. The group directory is the one the tool itself uses for the folder `[verified, sandbox]`
   (`providers.grok.group_name`): `quote(cwd, safe="")` when that is at most 255 bytes, else
   `<slug>-<first 16 hex of BLAKE3(cwd as UTF-8)>` (rule and slug: Phase 2, Q1). BLAKE3 is not
   in the standard library; `agent_sessions/blake3.py` is a one-chunk implementation (inputs up
   to 1,024 bytes), checked against the BLAKE3 team's test vectors. Refused, because never
   seen: a folder whose path is over 1,024 bytes, and one whose name is not valid UTF-8. When
   the native store has no directory of that name but has one that decodes
   (`urllib.parse.unquote`) to the same folder, that one is used.
2. Refuse when `~/.grok/sessions/*/<id>/` exists.
3. Copy the session directory to `~/.grok/sessions/<group>/<id>/`, leaving out `*.lock`.
4. Rewrite `summary.json` only: `info.cwd` → mapped cwd; `grok_home` → the native store path
   (layout: see "Import and launch notes"). Both the group name and `info.cwd` matter: the
   first decides `sessions list`, the second `sessions search` `[verified, sandbox]`.
5. Leave `session_search.sqlite`, `active_sessions.json` and `prompt_history.jsonl` alone (the
   index is rebuilt by the tool `[verified, sandbox]`).

Confirmed in the sandbox (Phase 2, Q1): the imported 1.0.34 session is found by id and exported
in full by 1.0.46 (`chat_format_version` is 1 in both). Still to confirm live: `grok --resume
<id>` itself, and that Windows paths in `prompt_context.json` / `system_prompt.txt` are
regenerated rather than trusted. The one Windows conversation is 123.7 MB on disk.

### 7. Quirks

- Timestamps have 9 fraction digits on Linux, 7 on Windows.
- The Windows `active_sessions.json` still lists a session with a pid: stale, ignore.
- `last_active_at` is missing in 2 summaries; `agent_id` / `attempt_id` in 1.
- `summary.json` can be 0 bytes mid-write (the binary has an error string for it): treat as
  malformed, skip with a warning.

---

## Antigravity

### 1. Store signature

- A directory named `antigravity-cli` or `antigravity-ide` that contains a directory
  `conversations` `[verified]`. `dir_names = {"antigravity-cli", "antigravity-ide"}`. Both live
  inside `<home>/.gemini/`, so **a `.gemini` directory must always be descended**, whether or not
  it is itself a Gemini CLI store.
- Not stores: `~/.cache/antigravity`, `AppData/Local/antigravity` (only `staging/`),
  `AppData/Roaming/Antigravity IDE` (only `Local State`), `~/.antigravity-ide`, and a project
  folder that happens to be called `Antigravity`.

### 2. Layout (`antigravity-cli`)

```
conversations/<conversation-uuid>.db        SQLite, one conversation
conversation_summaries.db                   SQLite index, table conversation_summaries
jetbox_summaries_proto.pb                   protobuf: repeated {1: conversation id, 2: summary message}
brain/<conversation-uuid>/                  artifacts; .system_generated/logs/transcript.jsonl, transcript_full.jsonl,
                                            .system_generated/logs/chunks/…, .system_generated/steps/<n>/output.txt
annotations/<conversation-uuid>.pbtxt       text proto: title:"…"
presence/<conversation-uuid>.lock           empty lock file
cache/last_conversations.json               {"<cwd>": "<conversation-uuid>"}   (what --continue uses [inferred])
cache/conversation_metadata.json, history.jsonl {display, timestamp(ms), workspace, type}, implicit/<uuid>.pb (opaque)
log/cli-<YYYYMMDD_HHMMSS>.log, cli.log      the tool's own log, one file per start (version, data directory, summary reconcile, resume)
```

`Session.files` = `conversations/<id>.db`, `brain/<id>/`, `annotations/<id>.pbtxt`.

`antigravity-ide` has the same directory names (`conversations/`, `brain/`, `implicit/`,
`knowledge/`, plus `html_artifacts/`, `plugins/`) but `conversations/` and `brain/` are **empty**
on the only copy that exists; the IDE conversation file format is therefore unknown.

### 3. One session, and what is excluded

A candidate is `conversations/<uuid>.db`.

| kind | how to recognise | action |
|---|---|---|
| empty conversation | `SELECT count(*) FROM steps` = 0 (`conversation_summaries.step_count = 0`, `title = ''`, `last_modified_time` = `0001-01-01 00:00:00+00:00`) | exclude. The only native conversation |
| sub-agent conversation | `conversation_summaries.parent_conversation_id != ''` or `nesting_depth > 0`; `parent_references` rows in the conversation DB `[inferred from the schema; none exist]` | exclude |
| lock files | `presence/*.lock` | never candidates |

### 4. Fields

`conversation_summaries.db` (user_version 3):

```sql
SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris,
       status, project_id, parent_conversation_id, nesting_depth, last_user_input_time,
       app_data_dir, raw_summary
FROM conversation_summaries;
```

`conversations/<id>.db` (user_version 1): `trajectory_meta(trajectory_id, cascade_id,
trajectory_type, source)` (one row; `cascade_id` == conversation id), `trajectory_metadata_blob(id,
data)` (one row, `id = 'main'`), `steps(idx, step_type, status, has_subtrajectory, metadata,
error_details, permissions, task_details, render_info, step_payload, step_format)`,
`gen_metadata`, `executor_metadata`, `parent_references`, `battle_mode_infos`.

Protobuf blobs, decoded without a schema (field numbers, all `[verified]` by matching values to
the SQL columns):

- `trajectory_metadata_blob.data`: `1.1` workspace URI, `2` created `{1: seconds, 2: nanos}`,
  `6` conversation id, `7` cwd URI, `18` project id.
- `conversation_summaries.raw_summary`: `1` preview, `2` step count, `3` last-modified timestamp,
  `7` created timestamp, `9.1` workspace URI, `10` last-user-input timestamp, `15` title (a string,
  or a message whose field `1` is the string), `17` = the same message as the metadata blob.
- `steps.metadata`: `1` step timestamp `{seconds, nanos}`.
- `steps.step_type`: 14 = user input, 15 = planner (model) response, 132 = generic/tool step
  (matched against `transcript.jsonl` `type` values `USER_INPUT`, `PLANNER_RESPONSE`, `GENERIC`).
  `step_payload` holds the text (field 19 for user input) but is not needed.

| field | source | fallback |
|---|---|---|
| `id` | file stem (== `trajectory_meta.cascade_id` == `conversation_id`) | — |
| `title` | `conversation_summaries.title` (== `annotations/<id>.pbtxt` title in all 4) | `preview` → first `USER_INPUT` `content` in `brain/<id>/.system_generated/logs/transcript.jsonl` → `id[:8]` |
| `cwd` | first element of `json.loads(workspace_uris)`, a `file://` URI | metadata blob field `7` → the key of `cache/last_conversations.json` whose value is the id → `null` |
| `started` | metadata blob field `2` | first step `metadata` field `1` → file mtime |
| `updated` | `last_modified_time` unless it starts with `0001-` | last step `metadata` field `1` → file mtime |
| `messages` | `SELECT count(*) FROM steps WHERE step_type IN (14, 15)` | `step_count` |

- URI → path: `urlparse`, `unquote` the path; if it matches `^/[A-Za-z]:` drop the leading `/`
  (`file:///F:/AI%20Projects/Website%20Works` → `F:/AI Projects/Website Works`).
- `workspace_uris` is `''` (not `[]`) for 3 of the 4 Windows conversations (headless runs);
  two get a cwd from `last_conversations.json`, one has **no recoverable cwd**.
- `transcript.jsonl` line: `{step_index, source: "USER_EXPLICIT"|"MODEL", type, status,
  created_at, content | thinking | tool_calls}` — plain text, usable for `show`.
- Sizes: conversation DBs 0.05–1.25 MB. All reads are single small queries.

### 5. Resume

- argv: `["agy", "--conversation", "<conversation-uuid>"]` `[verified, --help: "--conversation
  Resume a previous conversation by ID"]`. `-c/--continue` = most recent conversation.
- Scoping: files are flat under `conversations/`, so by id it is presumably global `[inferred]`.
  Launcher: `cd <cwd> && agy --conversation <id>`; when the cwd is unknown or gone, `$HOME`.
- An id the tool cannot find does **not** fail: the flag is dropped with `warning: conversation
  "<id>" not found` and a new conversation starts `[verified, code: entrypoints.resolveProject;
  not run]`. The id is looked up as the file `conversations/<id>.db`, not in either summary
  index (Phase 2, Q5).
- There is no list subcommand `[verified, --help]`.
- IDE conversations: none exist to test. The summaries table has an `app_data_dir` column
  (`antigravity-cli` in every row), which suggests one schema for both apps and that `agy` reads
  only its own directory `[inferred]`. Treat an `antigravity-ide` store as **list-only**.

### 6. Import

Verdict: **experimental: copy-only, done only when asked for** (`import --experimental`,
`experimental=True`) `[inferred from the verified lookup; no resume of a copy has been done]`.
Without the flag the adapter refuses and says what the flag is for.

The reason given at first ("registering one needs a row in `conversation_summaries.db`") does
not hold: the tool finds a conversation by its file `conversations/<id>.db` `[verified, code]`,
and it rebuilds both summary indexes from the conversation files at start-up `[verified, the
Windows store's own log: "reconcile checked 4 conversations, restored 4"]`. What stays true: the
workspace path is inside protobuf blobs, so an imported conversation keeps its Windows
workspace URIs, and nothing about an actual resume has been observed.

The recipe is the table of Phase 2, Q5, implemented as it stands:

1. Refuse (before anything else is looked at) unless the import was asked for as experimental.
2. Refuse a conversation of an `antigravity-ide` store, one whose file is not `<id>.db`, one
   with a `<id>.db-wal` that is not an empty regular file, one whose database cannot be read
   (a schema with a view or a trigger included), a sub-agent conversation (§3: the summaries
   row, or rows in `parent_references`) and one with no steps in the database file.
3. Refuse when any of `conversations/<id>.db`, `conversations/<id>.db-wal`,
   `conversations/<id>.pb`, `brain/<id>`, `annotations/<id>.pbtxt`, `presence/<id>.lock` exists
   in the native store.
4. Copy, byte for byte and in this order: `brain/<id>/` (when it is a directory; symbolic links
   and special files in it are left out), `annotations/<id>.pbtxt` (when it is a regular file),
   `conversations/<id>.db` last. Never `-wal`, `-shm`, either summary index, `presence/`,
   `cache/`, `history.jsonl`, `implicit/`.
5. Rewrite nothing, write no index. No folder is needed: a conversation without a recorded or
   an existing folder imports all the same, and a chosen folder is only noted in the record.
   A conversation whose folder was known from `cache/last_conversations.json` alone therefore
   has no folder in its native copy.
6. The copy is read back like any conversation of the native store (through the in-memory
   read, which creates no sidecar).

Undo (`unimport`): the three copied pieces; with `--force` also what the tool adds once it has
opened the conversation (`conversations/<id>.db-wal`, `-shm`, `presence/<id>.lock`). The rows
in the two summary indexes are left to the tool. An undo must not run while `agy` is running;
the files cannot say whether it is (Phase 2, Q5).

### 7. Quirks

- All DBs are WAL-mode with no sidecars at rest; `mode=ro` without `immutable=1` creates them.
- `last_modified_time` / `last_user_input_time` are Go zero time when unset.
- Three Windows conversations were headless runs in temp directories that no longer exist.

---

## Gemini CLI

### 0. State on this machine

- `gemini` on PATH is `~/.local/bin/gemini`, a 4-line shell wrapper: `mise use -g --quiet gemini
  || exit 1; exec mise x gemini -- gemini "$@"`. It installs the package on first use. The install
  is **incomplete** (`~/.local/share/mise/installs/gemini/0.63.0/` holds only `package.json`;
  `~/.local/state/mise/incomplete-installs/gemini` exists; `gemini` is not in
  `~/.config/mise/config.toml`). Running `gemini --help` would download and install software and
  edit the global mise config, so it was **not run**.
- The facts below come from the Windows data and from the real Gemini CLI 0.54.0 bundle found at
  `…/Users/arunt/AppData/Roaming/npm/node_modules/@google/gemini-cli/bundle/` (source read, not
  executed).
- Consequence: Gemini is **list-only** on Linux. `resume_argv` must not be offered until a real
  `gemini` binary exists (`launchable = false`, `note = "Gemini CLI is not installed"`).

### 1. Store signature

- A directory named `.gemini` that contains a directory `tmp` `[verified]`:
  `(p / "tmp").is_dir()`. `dir_names = {".gemini"}`. The native `~/.gemini` holds only
  `antigravity-cli/`, `config/`, `settings.json` (written by Antigravity and hook installers) and
  is **not** a Gemini CLI store. The native store, once the CLI is used, would be `~/.gemini`
  `[verified, source: getGlobalGeminiDir = homedir()/.gemini]`.

### 2. Layout

```
<store>/tmp/<slug>/chats/session-<YYYY-MM-DDTHH-MM>-<first 8 of session id>.jsonl   (.json in older versions)
<store>/tmp/<slug>/.project_root        the project path (lower-cased on Windows), no newline
<store>/tmp/<slug>/logs.json, logs/
<store>/history/<slug>/.project_root
<store>/projects.json                   {"projects": {"<normalised path>": "<slug>"}}
```

`<slug>` `[verified, source: ProjectRegistry]`: a slugified basename of the project path
(`^[a-z0-9-]+$`), made unique with `-1`, `-2`…; the mapping lives in `projects.json` and is
confirmed by the `.project_root` markers. `normalizePath` = `path.resolve`, plus lower-casing on
Windows only. Example: `"f:\\ai projects\\website works": "website-works"`.

`Session.files` = the one chat file.

### 3. One session, and what is excluded

A candidate is `tmp/*/chats/session-*.jsonl` (or `.json`). The tool's own listing rule
`[verified, source: getAllSessionFiles]` drops a file when it has no `sessionId`, when
`kind == "subagent"`, or when it has **no resumable content**: no user message other than one
that is empty or starts with `/`, `?`, `<session_context>` or `<hook_context>`, and no `gemini`
message with text, `toolCalls` or `thoughts`. Sessions with the same `sessionId` in several
files: the one with the newest `lastUpdated` wins.

The only chat file on any drive holds a single injected `<session_context>` user message, so
it is **excluded**. There is no resumable Gemini CLI session anywhere.

### 4. Fields

File format `[verified, source: loadConversationRecord]`, one JSON object per line, applied in
order:

- metadata record: has string `sessionId` and `projectHash`; also `startTime`, `lastUpdated`,
  `kind` (`main` | `subagent`), `summary`;
- message record: has string `id`; also `timestamp`, `type` (`user`, `gemini`, `info`, `error`,
  `warning`), `content` (string or list of parts with `text`), `toolCalls`, `thoughts`;
- `{"$set": {…}}`: merges metadata keys; a `messages` list inside replaces all messages;
- `{"$rewindTo": "<message id>"}`: removes that message and every later one (all of them if the
  id is unknown).

Reader rule (ours, not the tool's): when metadata keys are merged, a `sessionId`, `projectHash`,
`kind` or `summary` that is not a string and a `startTime` or `lastUpdated` that is not a time
are left out, so a damaged record cannot replace a good value.

| field | source | fallback |
|---|---|---|
| `id` | metadata `sessionId` | — (skip the file) |
| `title` | metadata `summary` | first user message that is not ignored content → `id[:8]` |
| `cwd` | `tmp/<slug>/.project_root` (lower-cased on Windows: map case-insensitively) | the `projects.json` key whose value is the slug |
| `started` | `startTime` | `lastUpdated` → file mtime |
| `updated` | `lastUpdated` | `startTime` → file mtime |
| `messages` | number of messages left after applying `$set` / `$rewindTo` | — |

`projectHash` = `sha256(project root)` hex, over the path in its **original** case `[verified:
reproduced for the one session]`.

### 5. Resume

- argv, once a real binary exists: `["gemini", "--resume", "<session-uuid>"]` `[verified, source:
  option "resume", alias "r": 'Resume a previous session. Use "latest" for most recent or index
  number (e.g. --resume 5)'; the error text adds "--resume {uuid}"]`.
- Strictly project-scoped `[verified, source: findSession]`: only
  `tmp/<slug of process cwd>/chats/` is searched. The launcher must `cd <cwd>`; when the cwd is
  gone the session cannot be resumed.
- No-model check: `gemini --list-sessions` ("List available sessions for the current project and
  exit") `[verified, source]`.
- `GEMINI_CLI_HOME` replaces the home directory the store is looked up in (the store is then
  `$GEMINI_CLI_HOME/.gemini`) `[verified, source: homedir()]`. First prompt of a new session:
  Phase 2, Q3.

### 6. Import

Verdict: **NotSupported** — "Gemini CLI is not installed on this machine, and no resumable
Gemini session exists to import". For later: an import would copy the chat file into
`~/.gemini/tmp/<slug>/chats/`, where `<slug>` must be the one the Linux CLI assigns to the mapped
cwd in `~/.gemini/projects.json` (creating the entry and both `.project_root` markers when
absent), and would probably need `projectHash` recomputed for the Linux path `[inferred;
whether the loader checks the hash was not established]`.

### 7. Quirks

- `.project_root` and `projects.json` keys are fully lower-cased on Windows
  (`f:\ai projects\website works`); `trustedFolders.json` uses `f:/ai projects/website works`.
- `tmp/<slug>/` can exist with an empty `chats/`.

---

## Store discovery

### Signatures in one place

| provider | `dir_names` | `is_store(p)` |
|---|---|---|
| claude | `.claude` | `(p/"projects").is_dir()` |
| codex | `.codex` | `(p/"sessions").is_dir()` |
| grok | `.grok` | `(p/"sessions").is_dir()` |
| antigravity | `antigravity-cli`, `antigravity-ide` | `(p/"conversations").is_dir()` |
| gemini | `.gemini` | `(p/"tmp").is_dir()` |

Compare names case-sensitively (all stores found use these exact spellings); NTFS itself is
case-insensitive, so a differently cased copy would be missed only if someone renamed it.

Walker rules that the data forces:

1. Do not descend into a confirmed store, **except** `.gemini`, which contains the Antigravity
   stores (and is descended whether or not it is a store).
2. Skip a Claude store whose path has a segment `local-agent-mode-sessions` (desktop app).
3. The fast pass must check `<home>/.gemini/antigravity-cli` and `…/antigravity-ide`, not only
   `<home>/.gemini`.

### Every store found (2026-10-12)

| provider | store | origin | listed / candidates |
|---|---|---|---|
| claude | `/home/tanuj/.claude` | native | 90 / 96 |
| claude | `/run/media/tanuj/22EEA841EEA80F5B/Users/arunt/.claude` | foreign | 23 / 37 |
| claude | `…/Users/arunt/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/local-agent-mode-sessions/8a21c259-…/52e70e85-…/local_459f241e-…/.claude` | foreign, desktop app | 1 / 1 (skip) |
| claude | same, `local_87338507-…/.claude` | foreign, desktop app | 1 / 1 (skip) |
| codex | `/home/tanuj/.codex` | native | 2 / 2 threads (3 files) |
| codex | `…/Users/arunt/.codex` | foreign | 1 / 2 threads (2 files) |
| codex | `/home/tanuj/.config/orca/codex-runtime-home/home` | mirror (hard links) | same 2 threads |
| codex | `…/Users/arunt/AppData/Roaming/orca/codex-runtime-home/home` | mirror (hard links) | same 2 threads |
| grok | `/home/tanuj/.grok` | native | 14 / 20 |
| grok | `…/Users/arunt/.grok` | foreign | 1 / 2 |
| antigravity | `/home/tanuj/.gemini/antigravity-cli` | native | 0 / 1 |
| antigravity | `…/Users/arunt/.gemini/antigravity-cli` | foreign | 4 / 4 |
| antigravity | `…/Users/arunt/.gemini/antigravity-ide` | foreign | 0 / 0 |
| gemini | `…/Users/arunt/.gemini` | foreign | 0 / 1 |

No store on `No Other Way`, `Folded` or `Zephyro`. Directories with a store name that are not
stores: 4 `.claude` on the Windows volume (plugin and marketplace caches inside `.claude` and
`.grok`, an MCP server repo), 6 on
`No Other Way`, 3 on `Zephyro`; `/home/tanuj/.gemini` (for Gemini).

A structural search (any directory whose children look like a store, any directory holding a
`rollout-*.jsonl`, any `summary.json` + `chat_history.jsonl` pair), run over all five roots,
found nothing beyond this table.

### Walk cost

`scratch/walk.py`: `os.scandir`, no symlink following, mountpoints from `/proc/self/mountinfo`
skipped, prune list from the SPEC, descending everything else (including stores).

| root | dirs | files | pruned dirs | errors | 1st run | 2nd run |
|---|---|---|---|---|---|---|
| `22EEA841EEA80F5B` (C:) | 75,094 | 291,233 | 130 | 0 | 2.79 s | 0.53 s |
| `No Other Way` | 6,421 | 62,044 | 13 | 0 | 0.63 s | 0.06 s |
| `Folded` | 2 | 0 | 3 | 0 | 0.00 s | 0.00 s |
| `Zephyro` | 21,078 | 178,349 | 127 | 0 | 1.84 s | 0.17 s |
| `/home/tanuj` | 24,171 | 164,615 | 88 | 0 | 0.27 s | 0.24 s |
| total | 126,766 | 696,241 | | | 5.5 s | 1.0 s |

The first run is **not a true cold run**: the page cache could not be dropped without root and
the drives had been read earlier in the session. A cold walk after a reboot or remount was not
measured; ntfs3 directory reads on NVMe make tens of seconds plausible, not minutes `[inferred]`.
On these numbers a full walk is fast enough to run on demand; keep the 24 h background refresh
for the cold case.

Where the directories are (candidates for the prune list; none has ever held a store):

| subtree | dirs | share of its root |
|---|---|---|
| `Users/arunt/AppData/Local/Temp` | 45,581 | 61 % of C: |
| `Zephyro/pinokio` (miniforge, uv cache) | 15,686 | 74 % of Zephyro |
| `.npm/_cacache`, `AppData/Local/npm-cache` | 6,375 / 2,568 | |
| `.cargo`, `.rustup` | 4,972 + 3,676 / 1,499 | |
| `~/.codex/.tmp`, `…/.codex/.tmp` (inside a store) | 2,950 / 2,819 | gone once stores are not descended |

Safe additions by name: `_cacache`, `npm-cache`, `.cargo`, `.rustup`, `.pnpm-store`, `.npm`,
`__pycache__`, `.venv`, `miniforge`. `Temp` only as the path `AppData/Local/Temp` (a bare name
`Temp` could be a user folder). Do **not** prune `AppData` or `Packages`: the Claude Desktop
stores and the Orca mirror are under them.

---

## Drive letters

| letter | mount | evidence |
|---|---|---|
| `C:` | `/run/media/tanuj/22EEA841EEA80F5B` | holds `Users/` and `Windows/`; 4 recorded cwds resolve there and nowhere else |
| `D:` | `/run/media/tanuj/No Other Way` | 3 recorded cwds resolve there and nowhere else |
| `F:` | `/run/media/tanuj/Zephyro` | 9 recorded cwds resolve there and nowhere else |
| `E:` and others | unresolved | no store records a path on them; `Folded` has no evidence at all |

Case: `d:` (one `.claude.json` key) and a fully lower-cased `f:\ai projects\website works`
(Gemini) occur; compare the letter and every segment case-insensitively and return the on-disk
spelling.

Every distinct cwd recorded in the foreign stores (sources: Claude transcripts, `.claude.json`,
`history.jsonl`; Codex `session_meta`; Grok `summary.json`; Antigravity `workspace_uris`,
`last_conversations.json`; Gemini `projects.json`, `.project_root`). Slash variants of the same
path are merged.

| recorded cwd | mapped local path | exists |
|---|---|---|
| `C:\Users\arunt` | `/run/media/tanuj/22EEA841EEA80F5B/Users/arunt` | yes |
| `C:\Users\arunt\Downloads\pdf2png` | `…/Users/arunt/Downloads/pdf2png` | yes |
| `C:\Users\arunt\AppData\Roaming\Claude\scratch-workspaces\8a21c259-…\52e70e85-…\scratch-2026-10-08-e01e1a` (and `-10-09-2ba813`, `-10-09-9d1e1d`) | not at that path; MSIX redirect: `…/Users/arunt/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/scratch-workspaces/…` | only via the redirect |
| `C:\Users\arunt\AppData\Local\Temp\claude\F--AI-Projects-Website-Works\402d1819-…\scratchpad\codex-img` (and `…\codex-img\agy`, `…\codex-img\gemini` in lower case) | — | no (temp, deleted) |
| `C:\Users\arunt\AppData\Local\Temp\sig-img-MwTxj4` | — | no |
| `D:\D Drive\The Pencl Projects\Final Fantasy Lore` | `/run/media/tanuj/No Other Way/D Drive/The Pencl Projects/Final Fantasy Lore` | yes |
| `D:\Hypermedia\Cine_Ads` | `/run/media/tanuj/No Other Way/Hypermedia/Cine_Ads` | yes |
| `D:\Hypermedia\Ep2` | — (`Hypermedia/Ep.2` exists: renamed) | no |
| `d:/Fable 5/Crime YT` | — (`No Other Way/Fable 5` exists, `Crime YT` does not) | no |
| `F:\AI Projects\Website Works` (also `f:\ai projects\website works`, `file:///F:/AI%20Projects/Website%20Works`) | `/run/media/tanuj/Zephyro/AI Projects/Website Works` | yes |
| `F:\AI Projects\Lead Generation` | `/run/media/tanuj/Zephyro/AI Projects/Lead Generation` | yes |
| `F:\AI Projects\Prompt-Maxxer AI STT\Prompt-Maxxer\source` | `/run/media/tanuj/Zephyro/AI Projects/Prompt-Maxxer AI STT/Prompt-Maxxer/source` | yes |
| `F:\AI Projects\Website Works\Portfolio Sites\Pill Content Site\Pill Content` | `…/Zephyro/AI Projects/Website Works/Portfolio Sites/Pill Content Site/Pill Content` | yes |
| `F:\AI Projects\Website Works\UWW Sites` | — | no |
| `F:\AI Projects\Website Works\UWW Sites\Landing Page` | — | no |
| `F:\High Status Game` | — (a folder of that name now sits under `No Other Way/Fable 5/`) | no |

Path-map normalisation needed before matching: strip a `\\?\` prefix (Codex DB), accept `/`
and `\`, percent-decode `file://` URIs and drop the slash before the drive letter, apply the
MSIX redirect above for `AppData\Roaming\Claude`. A letter resolves when at least one recorded
path maps under exactly one mount; a path whose letter resolves but whose folder is gone gets
`cwd_local = <mount>/<rest>` only if it exists, else `null` with `cwd_exists = false`.

---

## Ground-truth counts

Taken 2026-10-11T21:30Z. The native Claude store is live; its numbers grow.

| provider | store | candidates | listed | excluded, by kind |
|---|---|---|---|---|
| claude | native | 96 files | **90** | 2 bookkeeping-only, 4 empty |
| claude | Windows | 37 files | **23** | 14 empty |
| codex | native | 3 files = 2 threads | **2** | — |
| codex | Windows | 2 files = 2 threads | **1** | 1 non-interactive (`exec`) |
| grok | native | 20 dirs | **14** | 6 empty |
| grok | Windows | 2 dirs | **1** | 1 empty |
| antigravity | native cli | 1 db | **0** | 1 empty |
| antigravity | Windows cli | 4 db | **4** | — |
| antigravity | Windows ide | 0 | **0** | — |
| gemini | Windows | 1 file | **0** | 1 without resumable content |

Candidates, one line each (`S` = store root):

```sh
find "$S/projects" -mindepth 2 -maxdepth 2 -type f -name '*.jsonl' | grep -cE '/[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\.jsonl$'   # claude
find "$S/sessions" -type f -name 'rollout-*.jsonl' | wc -l                                                                           # codex files
find "$S/sessions" -type f -name 'rollout-*.jsonl' | sed -E 's/.*rollout-[0-9T-]{19}-([0-9a-f-]{36}).*/\1/' | sort -u | wc -l       # codex threads
find "$S/sessions" -mindepth 3 -maxdepth 3 -name summary.json | wc -l                                                                # grok
find "$S/conversations" -maxdepth 1 -name '*.db' | wc -l                                                                             # antigravity
find "$S/tmp" -path '*/chats/session-*.json*' | wc -l                                                                                # gemini
```

Listed:

```sh
# claude: files with a real prompt or an assistant record (rule of "Claude Code" §3)
python3 scratch/claude_count.py "$S"            # line "prompt>0 or asst>0"
# codex: threads whose first line has payload.source != "exec"
find "$S/sessions" -name 'rollout-*.jsonl' -print0 | while IFS= read -r -d '' f; do head -1 "$f" | python3 -c 'import json,sys;p=json.load(sys.stdin)["payload"];print(p["id"],p["source"])'; done | sort -u | grep -vc ' exec$'
# grok: sessions with an assistant record or a user record carrying prompt_index
for d in "$S"/sessions/*/*/; do grep -qE '"type":"assistant"|"prompt_index"' "$d/chat_history.jsonl" 2>/dev/null && echo x; done | wc -l
# antigravity: conversations with at least one step
for f in "$S"/conversations/*.db; do [ -e "$f" ] && python3 -c 'import sqlite3,sys,urllib.parse as u;print(sqlite3.connect("file:"+u.quote(sys.argv[1])+"?mode=ro&immutable=1",uri=True).execute("select count(*)>0 from steps").fetchone()[0])' "$f"; done | grep -c 1
# gemini: scratch/build_fixtures.py gemini_parse(), or by eye (one file)
```

(The Grok one-liner relies on `"prompt_index"` never appearing inside message text; it matches
the parsed count on both stores.)

---

## Reader notes (found while implementing, 2026-10-12)

- **Claude**: of the 3 Windows "superseded parent" files one is an empty session, so 2 listed
  sessions carry the `continued in` note. `tests/fixtures/README.md` counts the sub-agent
  transcript among the `claude/native` candidates; by §3 it is not one (8 candidates, 6 listed).
- **Codex tool turns** (for `transcript`): `response_item` records whose `payload.type` ends in
  `_call` and that carry a string `name` (only `custom_tool_call` / `exec` occurs in the data).
- **Grok**: `<user_query>` is not always at the start of the text (it can follow an
  `<image_files>` block or an interrupt notice). One compaction survivor has the wrapper but no
  `prompt_index`: not counted as a prompt, shown in the transcript. `chat_history.jsonl` has no
  timestamps. Sub-agent children are recognised by a tolerant match on the parent's
  `subagents/*/meta.json` (no instance exists; unverified).
- **Antigravity**: `USER_INPUT` content is `<USER_REQUEST>…</USER_REQUEST>` followed by metadata
  blocks. `steps.step_payload`: `19.2` user text, `20.1` reply, `20.7.2` tool name (matched
  `transcript.jsonl` on all 13 user and 19 planner steps). Title chain used: summaries `title` →
  `annotations/<id>.pbtxt` → `preview` → first user input → `id[:8]`. The fixture README shows one
  cwd with back-slashes; the reader returns the URI form `F:/Proj/My Site`.
- **Gemini** (0.54.0 bundle): a message with a known id replaces it in place; `messages` inside a
  metadata record adds; sub-agent chats live in `chats/<parent id>/`; a legacy `.json` chat is one
  JSON object with `sessionId` and `messages`, and a resumed `.json` gets a `.jsonl` twin
  (duplicate rule: newest `lastUpdated` wins).
- **Path map**: `D:` stays unresolved from listed sessions (the only listed session on it points
  at a folder that is gone); no listed session is affected. Which session, and the override:
  Phase 2, Q7.

### Import and launch notes (task 5, 2026-10-12)

- **Claude `<enc-cwd>`** (correction to §2): the tool's regex has no `u` flag, so the
  replacement is per UTF-16 code unit. A character outside the BMP (an emoji) becomes **two**
  `-`; "each become one `-`" holds for BMP characters only. `encode_cwd` was checked against the
  tool's own functions (`k`, `Mse`, `Le`, `OO`, copied from the 2.1.296 binary and run under
  node) on nine paths, four of them over 200 characters. The hash has no Bun-specific branch.
  Three real Windows sessions map to a folder whose encoding is over 200 characters (the MSIX
  scratch workspaces), so the hash rule is used by real imports.
- **Codex rewrite**: `json.dumps(record, ensure_ascii=False, separators=(",", ":"))` reproduces
  the original bytes of every `session_meta` and `turn_context` line of the real Windows rollout
  and of the fixtures, so a rewritten line differs from the source in the mapped values only. A
  path that does not map to an existing folder is left as recorded. The thread name is taken from
  the store's `session_index.jsonl`, else from the Orca mirror's (`<profile>/AppData/Roaming/orca/…/home`
  or `<profile>/.config/orca/…/home`); the source line is appended as it is, unless the native
  index already names the thread. The Windows thread has two name lines in the mirror only.
- **Grok `summary.json`** (correction to §6.4): all 15 real summaries (14 native, 1 Windows) are
  pretty-printed with a two-space indent and no final newline, and `json.dumps(…, indent=2,
  ensure_ascii=False)` reproduces each byte for byte. The import keeps that layout when the
  source has it (then only the two values differ) and writes compact JSON otherwise. Keys that
  are absent are not added.
- **Grok group directory**: besides the `[A-Za-z0-9/ ]` rule, an encoded name over 255 bytes is
  refused (the slug-and-hash naming is documented but was never seen). Both limits are gone
  since Phase 2, Q1 gave the tool's own rule (Grok §6, step 1). Sub-agent child sessions
  of an imported session are not copied (none exists).
- **Every import**: files are staged in `<native store>/.agent-sessions-import-<pid>-<n>/` and
  moved into place (a file is hard-linked under its name and the staged name removed, so an
  existing name is never replaced; a directory is renamed); a hard kill can leave that directory
  behind (it is inert). Copies keep the source's modification time, are created `0600`
  (directories `0700`, and so is a `session_index.jsonl` the import has to create), and symbolic
  links and special files in a session directory are left out.
- **SQLite**: a database up to 64 MB is now read through `open_ro` and loaded into an in-memory
  connection with `Connection.deserialize` (header bytes 18 and 19 set from 2 to 1 in the copy,
  `PRAGMA query_only`), so SQLite never opens the file and its access time cannot move. Larger
  files, and any file that does not load that way, are opened with `mode=ro&immutable=1` as
  before. Both ways give the same rows on every fixture database and on the real ones.
- **Launch**: `resume_needs_cwd` is true for Gemini only (§5 of each provider). The other four
  start in the home directory with a note when the recorded folder is gone or unknown.

---

## Phase 2 findings (2026-10-12)

Read-only on the real stores and drives. Agent programs run: `--help` / `--version` of the real
`claude`, `codex` and `agy` binaries, and four model-free Grok commands (`sessions list`,
`sessions search`, `export`, `usage`) in a throwaway home. System tools were used to read only
(`udisksctl info`, `pkaction`, `journalctl`, `objdump`). Nothing was mounted, unmounted,
resumed or sent to a model. Two more tags are used below:

- `[verified, sandbox]` — seen when one of the four Grok commands ran in the throwaway home;
- `[verified, code]` — read in the tool's own code (bundled JavaScript, or the disassembled
  function that is named), not run. What the tool does with it at run time stays unproven.

The sandbox: a byte-identical copy of the 1.0.46 binary (same SHA-256) in
`<sandbox>/home/.grok/bin/`, started as `unshare -r -n -- env -i HOME=<sandbox>/home
XDG_{CONFIG,DATA,CACHE,STATE}_HOME=<below it> PATH=/usr/bin:/bin <copy> …` with `stdin
</dev/null` and `timeout 30`; `<sandbox>` was `/tmp/ags…`, a path of letters, digits and `/`
only, so that each test path adds one kind of character. A listing of `~/.grok` (2,385 entries:
type, size, modification time) taken before the first run and after the last is identical.

**What is on PATH.** `~/.local/bin/{claude,codex,grok,gemini,opencode,cursor-agent,copilot,crush}`
are one and the same 4-line wrapper: `mise use -g --quiet <tool> || exit 1; exec mise x <tool>
-- <tool> "$@"` (it installs the tool when it is missing). Only `~/.local/bin/agy` is a program.
Behind the wrappers `[verified]`:

| tool | real program | state |
|---|---|---|
| claude | `~/.local/share/mise/installs/claude/2.1.296/claude` | installed |
| codex | `~/.local/share/mise/installs/codex/0.162.0/bin/codex` | installed |
| grok | npm package 1.0.50 under `…/installs/npm-xai-official-grok/`; its launcher (`grok-bootstrap.js`) starts `$GROK_HOME/bin/grok` when that exists, here `~/.grok/bin/grok -> grok-1.0.46` | 1.0.46 is what runs |
| opencode | `~/.local/share/mise/installs/opencode/1.18.35/opencode` | installed, never used (Q6) |
| gemini | `…/installs/gemini/0.63.0/` holds `package.json` only | install incomplete |
| cursor-agent, copilot, crush | no directory under `…/installs/`, not in `~/.config/mise/config.toml` | not installed; the first call would install |

The wrapper, byte for byte (here for claude, 123 bytes; the other seven are 120 to 153 bytes and
differ in the two names only) `[verified]`:

```
#!/bin/bash
export MISE_MINIMUM_RELEASE_AGE=0
mise use -g --quiet "claude" || exit 1
exec mise x "claude" -- "claude" "$@"
```

For Grok the spec is `npm:@xai-official/grok` and the tool `grok`. The directory under
`~/.local/share/mise/installs/` is named after the spec `[verified]` for the five that are
installed: `claude`, `codex`, `opencode`, `npm:playwright` → `npm-playwright`,
`npm:@xai-official/grok` → `npm-xai-official-grok`. The rule `launch._install_name` uses (lower
case, every run of characters that are not letters or digits becomes one `-`) fits those five
and was not checked against mise's code `[inferred]`. Each directory holds one sub-directory per
version, links to versions (`latest`, `1`, `1.0`) and `.mise.backend.toml`. The program is at
`claude/<version>/claude`, `codex/<version>/bin/codex`, `opencode/<version>/opencode` and
`npm-xai-official-grok/<version>/node_modules/.bin/grok`: at most three directories below the
version, which is how far `launch.tool_state` looks. `gemini/0.63.0/` has no program at any
depth.

### Q1. Grok: group directory, lookup by id, search index, 1.0.34 sessions

Method: copies of the native session `01a106cb-…` (118 kB) with the last 12 hex digits of the
id changed and a title naming the candidate, each placed under one candidate group name;
`summary.json` `info.cwd` set to the test directory; `grok sessions list` run with that (real)
directory as the current directory. A copy that is listed has the name the tool looks for.
Controls: from another directory the listing is `No sessions found.`

**Rule** `[verified, sandbox]`. With `enc = urllib.parse.quote(cwd, safe="")`:

```python
if len(enc.encode()) <= 255:
    group = enc                                  # A-Z a-z 0-9 - . _ ~ kept; all else %XX, upper-case hex, per UTF-8 byte
else:
    slug = re.sub(r"[^a-z0-9]+", "-", os.path.basename(cwd).lower()).strip("-")[:40] or "workspace"
    group = slug + "-" + blake3(cwd.encode("utf-8")).hexdigest()[:16]
```

`cwd` is the physical working directory: entered through a symbolic link, the session under
the name of the link is not listed and the one under the resolved path is, with or without
`PWD` in the environment.

| tested | candidates | listed |
|---|---|---|
| `-` `.` `_` `~`, an upper-case letter, a digit | the character itself; `%XX` | the character itself |
| space | `%20`; `+`; a literal space | `%20` |
| `Ü` `ï` `漢` `😀` | UTF-8 `%XX` in upper-case hex; the same in lower-case hex; the character itself | upper-case hex |
| `u` + U+0308 (decomposed ü) | `u%CC%88`; the composed form `%C3%BC`; the characters themselves | `u%CC%88` (no Unicode normalisation) |
| each of ``! ' ( ) * + , = @ & $ # % [ ] { } ; : \ ^ ` \| " < > ?`` | the character itself; `%XX` upper; `%XX` lower where it differs | `%XX` upper |
| `<sandbox>/work/my-proj_v1.2~x y/Ünï` | `quote(safe="")`; `quote_plus`; every non-alphanumeric escaped | `…%2Fwork%2Fmy-proj_v1.2~x%20y%2F%C3%9Cn%C3%AF` |
| encoded length 254, 255 | the full encoded name | listed |
| encoded length 256, 257, 299 | `<slug>-<hex>`; arbitrary name with a `.cwd` file | `<slug>-<16 hex>` only |

`/` is `%2F` and `:` `\` are `%3A` `%5C` in every native and Windows group name `[verified,
data]`. The long form was found by trying 2,144 slug and hash combinations for one path, then
worked out on 18 more paths, the last 10 of them named by the rule before the run: `MyProj` →
`myproj`; `my proj.v1_2~x` → `my-proj-v1-2-x`;
`Ünï Köln` → `n-k-ln`; `a  b__c` → `a-b-c`; `.hidden` → `hidden`; `name.` → `name`; `--x--` →
`x`; a 59-character name → its first 40 slug characters; 39 × `x` + `Ütail` → 39 × `x` + `-`
(the cut comes after trimming, and is not trimmed again); `a` + 45 spaces + 30 × `b` → `a-` +
30 × `b` (runs collapse before the cut); `漢字` → `workspace`. BLAKE3 is not in Python's
standard library; a 40-line single-chunk implementation (input up to 1,024 bytes), checked
against the published digests of the empty input and of one zero byte, gave every name above.
The same hash, first 8 hex digits, names Grok's memory folders, which gives three data points
outside the sandbox `[verified, data]`: `memory-v2/workspaces/tanuj-db4bfc44` for `/home/tanuj`,
`work-6b15ba01` for `/home/tanuj/Work`, and on Windows `memory/website-works-32f235e1` for
`F:\AI Projects\Website Works` (the hash is over the path exactly as recorded).

**What decides what** `[verified, sandbox]`:

| question | answer |
|---|---|
| `sessions list` in directory X | the sessions in the group directory named after X. `info.cwd` is not consulted (a session in X's group that records another cwd is listed in X and nowhere else). A `.cwd` file is not consulted either: an arbitrary group name with `.cwd` = X is not listed; the right name is listed with `.cwd` absent, correct, correct plus a newline, or wrong |
| `export <id>`, `usage <id>` | found from any directory, in any group directory (one was named `zzz-not-an-encoding`), whatever `info.cwd` says. Unknown id: `Error: Session '<id>' not found.`, exit 1 |
| `usage <id>` on the 1.0.34 session | `Error: No usage recorded for session '<id>'.`, exit 1 (it has no `usage.json`): not an existence probe |
| `sessions search <query>` in directory X | rows of `session_search.sqlite` whose `cwd` equals X, and that column is `summary.json` `info.cwd`. A session in X's group with another `info.cwd` is listed but not found by search; one in a wrongly named group whose `info.cwd` is X is found by search but not listed. An id as the query is found from the session's own directory only |
| who builds `session_search.sqlite` | the tool: the file is absent after `list`, `export` and `usage`, and created by the first `sessions search` (`meta.session_search_schema_version` = 4) with one row per session directory of every group; a session added afterwards is in the next search |
| login, network | none of the four commands needs a login. `list` and `search` also ask a remote service; with no network `search` prints `warning: remote session search timed out` (`list` says so only in `--debug-file`) after about 5 s and exits 0. With a network and a login they would contact xAI `[inferred]` |
| what the commands write in the store | `docs/user-guide/*.md` (27 files, unpacked on first run), `logs/unified.jsonl`, an empty `logs/hooks.log`, and `sessions/session_search.sqlite` (search only). No session directory, no `.cwd`, no `active_sessions.json` |

So an import must set **both** the group directory name and `info.cwd`, and needs nothing else
to be visible to `list`, `search` and `export`.

`session_docs.content` (prompt and reply text) stays empty for a session whose group directory
does not match its `info.cwd` (2 of 2) and for the imported 1.0.34 session; its title is
indexed. A native 36 MB session (`updates.jsonl` 18.8 MB) copied into the sandbox got 17,940
characters. Cause for the 1.0.34 session not established (format or size: its `updates.jsonl`
is 49.6 MB).

**The project's own import, end to end** `[verified, sandbox]`: with `AGENT_SESSIONS_HOME=<sandbox>/home`,
`bin/agent-sessions import grok:f75c715e:01a0a4c9-5206-7b00-8471-d1ab6503810c` copied 62 files,
123.7 MB, in 0.3 s into `sessions/%2Frun%2Fmedia%2Ftanuj%2FZephyro%2FAI%20Projects%2FWebsite%20Works/`
and rewrote `info.cwd` and `grok_home`. `grok export 01a0a4c9-…` from an unrelated directory
then exits 0 in 0.2 s with 66,112 bytes: 10 `## User`, 33 `## Assistant` and 26 `## Tools`
sections, which are the 10 `user_message_chunk` and 33 `agent_message_chunk` records of
`updates.jsonl` and the 10 user and 33 assistant turns `agent-sessions show` prints. So a
session written by 1.0.34 on Windows loads in 1.0.46 for export. `grok sessions list` was not
run inside the NTFS folder (the tool would read project files there without `O_NOATIME`); that
the group name is the right one follows from the rule above, and from a native-version session
placed in the same group getting indexed content, which a mismatched group does not `[inferred]`.

Not established: `grok --resume <id>` itself (interactive, model); what the tool writes into
`.cwd`; whether Windows paths in `prompt_context.json` (`working_directory`, `memory_*_path`,
`agents_md_files[].file_path`) are regenerated on resume.

### Q2. Model-free commands and data-directory variables

Does a non-interactive command that makes no model call list saved sessions, or load one by id?

| tool | answer | closest commands `[verified, --help; none run]` |
|---|---|---|
| claude | **no** | `claude agents --json`: "Print active sessions (interactive and background) as a JSON array and exit (for scripting; does not require a TTY)"; `--all`: "With --json: also include completed background sessions"; `--cwd <path>`: "Show only background sessions started under <path>". These are live and background sessions, not the transcript store. `claude purge --dry-run [path]`: "List what would be deleted without deleting anything" (the preview of a command that deletes a project's transcripts). `claude logs <id\|name>`: a background session's terminal output. `claude import [source]` imports **config** from `codex`, `gemini`, `cursor`, not sessions |
| codex | **no listing; one read-only inspector** | `codex migrate-rollouts [--thread <THREAD_ID>] [--json] [--verbose]`: without `--apply` "the command only reports eligible sessions"; `--verbose`: "Print one line for every inspected rollout"; `--json`: "Emit the complete per-thread report as JSON". Whether it reports `paginated` threads is unknown. `codex archive\|unarchive\|delete <SESSION>` resolve "Session id (UUID) or session name" without a model but change the store. `codex queue --thread <THREAD> --message <TEXT>` ("Queue a message for an existing session") ends in a model turn. `codex agents` is a TUI on the app-server daemon. `codex debug prompt-input [PROMPT]` takes no session |
| agy | **no** | subcommands are `agent(s)`, `changelog`, `install`, `mcp`, `mic-serve`, `models`, `plugin(s)`, `remote-control`, `update`; none reads conversations. `agy plugin import [source]` imports plugins |
| grok | yes | `sessions list`, `sessions search`, `export`, `usage` (Q1) |
| gemini | yes, per project | `--list-sessions` `[verified, source; not installed]` |

Variable that moves the data directory:

| tool | variable | evidence |
|---|---|---|
| claude | `CLAUDE_CONFIG_DIR` | `[verified, code]`: `ve = (process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")).normalize("NFC")`, `Df = join(ve(), "projects")`; the `.claude.json` file then sits inside that directory (`join(process.env.CLAUDE_CONFIG_DIR \|\| homedir(), ".claude.json")`). Message in the binary: "the configuration home (CLAUDE_CONFIG_DIR) is not an absolute path" |
| codex | `CODEX_HOME`; `CODEX_SQLITE_HOME` for the SQLite files | `[verified, --help]`: `-p, --profile`: "Layer $CODEX_HOME/<name>.config.toml on top of the base user config"; `codex exec resume --ignore-user-config`: "Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`". Strings: "CODEX_HOME points to …, but that path does not exist", "Environment value for `$CODEX_SQLITE_HOME` is overridden by the required `sqlite_home` value" |
| grok | `GROK_HOME`, else `$HOME/.grok` | `[docs]` "Override config directory (default: `~/.grok`)"; `$HOME` `[verified, sandbox]` |
| agy | no variable found; hidden flags `--gemini_dir` and `--app_data_dir` | Q5 |
| gemini | `GEMINI_CLI_HOME` (replaces the home directory) | `[verified, source]`: `homedir()` returns `process.env["GEMINI_CLI_HOME"]` when set |

### Q3. Starting a new session with a first prompt

Help output and shipped documentation only; none was started. "Sent at once" means the prompt
is submitted as the first turn without a key press.

| tool | argv | help line `[verified]` | sent at once | fill without sending | working directory |
|---|---|---|---|---|---|
| claude | `claude "<prompt>"` | "Usage: claude [options] [command] [prompt]"; "prompt  Your prompt"; "starts an interactive session by default, use -p/--print for non-interactive output" | `[inferred]`: the help does not say | hidden option `--prefill <text>`: "Pre-fill the prompt input with text without submitting it" `[verified, code: defined with hideHelp(), applied whenever set; absent from --help; not run]` | no flag (`--add-dir` only grants access); `cd` first |
| codex | `codex "<prompt>"` | "Usage: codex [OPTIONS] [PROMPT]"; "[PROMPT]  Optional user prompt to start the session" | `[inferred]` from "to start the session" | none in the help | `-C, --cd <DIR>`: "Tell the agent to use the specified directory as its working root" |
| grok | `grok "<prompt>"` | "[PROMPT]  Initial prompt for the interactive session, e.g. `grok "fix the bug"`" | yes `[docs, 01-getting-started.md]`: "Launch the interactive TUI and submit an initial prompt as the first turn" | none in the help | `--cwd <CWD>`: "Working directory" |
| agy | `agy --prompt-interactive "<prompt>"` (`-i`) | "--prompt-interactive  Run an initial prompt interactively and continue the session"; "-i  Short alias for --prompt-interactive" | yes, by that wording | none in the help | no flag (`--add-dir`: "Add a directory to the workspace (repeatable)"); `cd` first |
| gemini 0.54.0 | `gemini "<prompt>"` or `gemini -i "<prompt>"` | `[verified, source]` positional `query`: "Initial prompt. Runs in interactive mode by default; use -p/--prompt for non-interactive."; `-i, --prompt-interactive`: "Execute the provided prompt and continue in interactive mode" | yes, by that wording | none seen | none seen; not installed |

- Never use `claude -p`, `codex exec`, `grok -p`, `agy -p/--print/--prompt`, `gemini -p`: those
  are the non-interactive forms.
- Length `[verified on this machine]`: one argument can be at most 131,071 bytes (`/bin/true`
  with 131,072 fails with "Argument list too long"); `getconf ARG_MAX` is 2,097,152 for the
  whole command line and environment. No tool states a limit of its own in its help. A first
  prompt that names an exported transcript file stays far below this; do not pass the
  transcript itself.
- Claude's two other hidden options, `--prefill-b64` and `--deep-link-cwd-b64`, act only
  together with `--deep-link-origin` `[verified, code]`; do not use them.

### Q4. Mounting a drive that is attached but not mounted

All four NTFS volumes are internal NVMe partitions. Nothing was mounted or unmounted, so what
`udisksctl mount` prints and how it asks for a password are `[inferred]`.

**(a) Identity at scan time, from files only** `[verified]`. A `/proc/self/mountinfo` line is
`<id> <parent> <major:minor> <root> <mount point> <options> [optional fields] - <fstype>
<source> <super options>`; spaces in the mount point and the source are `\040`. For the NTFS
volumes: `224 29 259:3 / /run/media/tanuj/22EEA841EEA80F5B rw,nosuid,nodev,relatime shared:412 -
ntfs3 /dev/nvme2n1p3 rw,uid=1000,…`.

| want | read |
|---|---|
| device of a mount | the source field; or `major:minor`, which equals `os.stat(<device node>).st_rdev` for these volumes. Not for btrfs: `/` shows `0:29` while its device `dm-0` is `253:0`, so match btrfs by the resolved source path |
| filesystem UUID | the name in `/dev/disk/by-uuid/` whose link resolves to the device (`22EEA841EEA80F5B -> ../../nvme2n1p3`) |
| label | the name in `/dev/disk/by-label/`, with udev's escapes decoded: `\xNN` is one byte (`No\x20Other\x20Way`). The Windows system volume has **no label** and no entry there |
| type, label and UUID of a device that is not mounted | `/run/udev/data/b<major>:<minor>` (world-readable text): `E:ID_FS_TYPE=ntfs`, `E:ID_FS_UUID=583E82883E825EBE`, `E:ID_FS_LABEL_ENC=No\x20Other\x20Way`. Use `_ENC`: `E:ID_FS_LABEL` is `No_Other_Way` |

The UUID is the persistent key (`22EEA841EEA80F5B`, `524ADB244ADB041F` Zephyro, `583E82883E825EBE`
No Other Way, `9E94CE9494CE6E79` Folded); device names such as `nvme0n1p2` can change between boots.

**(b) Attached but not mounted** `[verified]`: `/dev/disk/by-uuid/<UUID>` exists and resolves to
a block device, and no mountinfo line has that device as its source. An example exists now:
`72E47873E4783B83` (`nvme2n1p4`, NTFS, no label) is attached and not mounted. When the UUID is
absent from `/dev/disk/by-uuid/` the drive is not attached.

**(c) Mounting.** `udisksctl mount --help` `[verified]`:

```
  -p, --object-path         Object to mount
  -b, --block-device        Block device to mount
  -t, --filesystem-type     Filesystem type to use
  -o, --options             Mount options
  --no-user-interaction     Do not authenticate the user if needed
```

- Invocation: `udisksctl mount --block-device /dev/disk/by-uuid/<UUID>`. With
  `--no-user-interaction` the call fails instead of asking when authentication is needed
  `[inferred from the help line]`.
- Where: `/run/media/<user>/<label>`, or `/run/media/<user>/<UUID>` for a volume without a
  label `[verified by the present mounts]`; `/run/mount/utab` marks all four `uhelper=udisks2`.
- Authentication **is** needed here `[verified up to the polkit defaults; the prompt itself is
  inferred]`: `udisksctl info -b`
  shows `HintSystem: true`, `HintAuto: false`, `IdType: ntfs` for all four; a system device
  falls under the polkit action `org.freedesktop.udisks2.filesystem-mount-system`, whose
  defaults are `implicit active: auth_admin_keep` (the action for removable drives,
  `…filesystem-mount`, is `implicit active: yes`); administrators are `unix-group:wheel`
  (`/usr/share/polkit-1/rules.d/50-default.rules`), and the user is in `wheel`. No rule under
  `/usr/share/polkit-1/rules.d` mentions udisks; `/etc/polkit-1/rules.d` cannot be listed
  (mode `drwxr-x--- root:polkitd`) but its size is 0, which on this btrfs means no entries
  `[inferred: three readable directories beside it have size = 2 × the summed name lengths]`.
  `/etc/fstab` has no line for these volumes; `/etc/udisks2/` holds the default `udisks2.conf`
  and no `mount_options.conf`. So a mount asks for the user's password through the session's
  polkit agent (the Omarchy shell registers one), once; `auth_admin_keep` then covers further
  mounts for a few minutes `[inferred]`.
- This boot `[verified, journal and directory times]`: booted at about 19:38; `udiskie
  --automount --no-notify --no-tray` runs in the session and left these volumes alone (they
  have `HintAuto: false`); Dolphin was opened at 20:27:23, a `polkit-agent-helper` ran
  20:27:25–29 (the only one this boot), the `ntfs3` module loaded at 20:27:29, `/run/media` was
  created at 20:27:29 and the last of the four mount points at 20:27:31. So the four were
  mounted from Dolphin after one password entry `[inferred from those times; udisksd logged
  nothing]`.

**(d) Mount options in effect** `[verified, /proc/mounts]`, the same for all four: driver
`ntfs3` (in-kernel; `ntfs-3g` is installed and not used), `rw,nosuid,nodev,relatime,uid=1000,
gid=1000,acl,iocharset=utf8,prealloc`.

Not verified, because nothing may be mounted: the text `udisksctl mount` prints on success and
its exit codes, the error under `--no-user-interaction`, how the password is asked when
`udisksctl` is a child of a TUI with no terminal of its own, the mount point chosen when the
label directory already exists, and unmounting.

### Q5. Antigravity: lookup by id, data directory, copy-only import

**Model-free command**: none (Q2).

**Data directory** `[verified, code: main.init.1 registers the flags, main.hideFlagsForExternal
hides them from --help; not run]`: `--gemini_dir` (default `.gemini`: "Path where Gemini files
are stored. If absolute path, will set directly. If relative path, will be resolved relative to
HomeDir.") and `--app_data_dir` (default `antigravity-cli`: "Path where application data is
stored, relative to GeminiDir"). Every start logs the result: `CLI app data directory:
/home/tanuj/.gemini/antigravity-cli` `[verified, native log]`. No environment variable that
moves the store was found: `JETSKI_APP_DATA_DIR`, `ANTIGRAVITY_APP_DATA_DIR` and
`ANTIGRAVITY_EXECUTABLE_DATA_DIR` occur only where the tool builds the environment of child
processes. A hand test can therefore run against a throwaway directory:
`agy --gemini_dir=<absolute dir> --conversation <id>` `[inferred]`. Give an absolute path: every
log has `Failed to resolve GeminiDir ".gemini": .gemini must be an absolute path … falling
back to default` for the relative default.

**Which index `--conversation <id>` consults: the bare file `conversations/<id>.db`.**

- `[verified, code]` `entrypoints.launchCLI` calls `resolveProject`, which asks the backend for
  the conversation (an indirect call; the log in the next item shows it is
  `GetConversationDetail`). `backend.(*ServerBackend).GetConversationDetail` looks in the live
  trajectories in memory, then calls `trajectorystore.(*Manager).LoadUnsafe`: a registry filled
  at start-up by listing the directory (`SqliteStore.List` → `os.ReadDir`), else
  `SqliteStore.Exists` → `os.Stat(<conversations>/<id>.db)`, then `SqliteStore.Load` →
  `dbtrajectory.newOrLoad`. Failing that, `fallbackGetConversationDetail` ("conversation %s not
  found locally, searching fallback import dirs") tries `conversations/<id>.db`, then the older
  `.pb` form, in another data directory named `antigravity` (what it is joined to was not
  traced). Neither `conversation_summaries.db` nor `jetbox_summaries_proto.pb` is on that path.
- `[verified, the Windows store's log of a real resume, 2026-09-08, version 1.1.22]` the same
  order at run time: `Creating trajectory store manager with proto store and SQLite store` →
  `GetConversationDetail: found conversation d2562a97-… (active=false)` → `project: resuming
  conversation belonging to project ID: default-cli-project` → `Resuming conversation d2562a97-…`.
- `[verified, the Windows store's log, 2026-10-08, first start of 1.3.1 after 1.1.27]` both
  summary indexes are rebuilt from the conversation files: `summary store: reconcile func
  returned 0 records, skipping`, then `[Summaries] reconcile checked 4 conversations, restored 4,
  cached 0, skipped 0, dropped 0`, then `summary store: reconciliation complete, synced 4
  records`. Every later start, Linux 1.3.x included, logs `restored 0, cached N`.
- When the id is not found the flag is ignored: `Conversation %s not found, ignoring
  --conversation flag` and `warning: conversation %q not found` `[verified, code:
  resolveProject]`, and a new conversation starts `[inferred]`.

**Copy-only import, to implement as an experimental action.** Source `S` = the foreign
`antigravity-cli` directory, destination `N` = `~/.gemini/antigravity-cli`.

| step | rule |
|---|---|
| copy, required | `S/conversations/<id>.db` → `N/conversations/<id>.db`, byte for byte |
| copy when present | `S/brain/<id>/` (whole tree: `.system_generated/`, `.user_uploaded/`, `.tempmediaStorage/`, `scratch/`, images) → `N/brain/<id>/`; `S/annotations/<id>.pbtxt` (the title) → `N/annotations/<id>.pbtxt` |
| copy only if not empty | `S/conversations/<id>.db-wal` beside the database, never `-shm` `[inferred: SQLite replays a WAL on open; all DBs found have no sidecars]`. Simpler and as safe: refuse a conversation that has a non-empty `-wal` |
| never copy or write | `conversation_summaries.db`, `jetbox_summaries_proto.pb` (rebuilt by the tool), `presence/<id>.lock`, `cache/last_conversations.json` (maps a cwd to the conversation `--continue` picks), `cache/conversation_metadata.json`, `history.jsonl`, `implicit/*.pb` (not named after conversations), `config/projects/*.json`, anything under an `antigravity-ide` store |
| order | `brain/<id>/` and the annotation first, the `.db` last, each staged and renamed as every import is: the `.db` is what makes the conversation exist for the tool |
| collision | refuse when any of `N/conversations/<id>.db`, `N/conversations/<id>.db-wal`, `N/conversations/<id>.pb`, `N/brain/<id>`, `N/annotations/<id>.pbtxt`, `N/presence/<id>.lock` exists (the last one means the id has been opened in this store before) |
| precondition | refuse a sub-agent conversation (§3) and one with no steps. `N/conversations/` exists once the tool has been started; otherwise the importer creates it as it creates any directory |
| undo | remove the paths the import record lists (`N/conversations/<id>.db`, `N/brain/<id>/`, `N/annotations/<id>.pbtxt`) and, when present, what the tool adds once it has opened the conversation: `N/conversations/<id>.db-wal`, `N/conversations/<id>.db-shm`, `N/presence/<id>.lock`. The row in `conversation_summaries.db` and the entry in `jetbox_summaries_proto.pb` are left to the tool (its reconcile has a `dropped` counter; what it does for a missing file is `[inferred]`). The lock file stays after the tool exits (both stores hold one per conversation at rest `[verified]`), so it does not tell whether the conversation is open: an undo must not run while `agy` is running, and the files cannot say whether it is |
| launch | `cd <mapped folder or $HOME> && agy --conversation <id>` |

Risks, all untested:

1. **A wrong or unreadable copy does not fail, it starts a new conversation** (flag ignored).
   The hand test must check that the old turns are on screen.
2. **Pruning** `[verified, code]`: at start-up `trajectorystore.(*Manager).Init(limit)` registers
   every file and, when `limit > 0`, `PruneAll` sorts the conversations by time and deletes the
   oldest beyond `limit`. Where `limit` comes from was not traced (the binary has a
   `max_conversations` field), nor which time is compared (the file's or a stored one), and no
   log here shows a prune. An imported conversation is old by either time until the tool
   rewrites it, so it would be among the first to go, and adding conversations can push native
   ones over a limit.
3. **Workspace**: the conversation keeps Windows workspace URIs in its blobs (three of the four
   have none at all); what resume does with a URI that does not exist is unknown. `agy` has no
   working-directory flag.
4. **Project**: a resume reads `~/.gemini/config/projects/<project id>.json` (`project: failed
   to read project config %s: %v` when it cannot). All four Windows conversations belong to
   `default-cli-project`, which also exists natively with the same content.
5. **Versions, and writes on first start**: the conversations date from 2026-08-29 (the oldest
   log left, of 2026-09-08, is from 1.1.22). The first start of 1.3.1 on 2026-10-08 rewrote
   three of the four conversation files
   (modification times 14:55:26.32–26.41, inside the 161 ms of the reconcile that logged
   `restored 4`) `[verified]`; native is 1.3.3. Expect the tool to write to a copied database
   the first time it starts, before any resume.
6. **Not logged in**: every native `cli-*.log` so far says `You are not logged into
   Antigravity`; the hand test needs a login first.

### Q6. Tools with no reader: gemini, opencode, cursor-agent, copilot, crush

**No session data exists for any of them, on Linux or on any mounted drive** `[verified]`.

| tool | installed command | data found |
|---|---|---|
| gemini | wrapper; install incomplete (above) | Windows: `…/Users/arunt/.gemini/tmp/` with one chat file that has no resumable content (see "Gemini CLI"). Linux: `~/.gemini` has no `tmp/`, `history/` or `projects.json` |
| opencode | wrapper; real 1.18.35 behind it | `~/.local/share/opencode/` holds two empty directories, `log/` and `repos/`, made 2026-10-10 17:57; no `opencode.db`. `~/.local/state/opencode/` empty; `~/.cache/opencode/bin/` empty; `~/.config/opencode/` holds `opencode.json`, `tui.jsonc`, `herdr-tui-session.js` and `plugins/` (herdr and orca status plugins). Windows: `…/Users/arunt/.config/opencode/` (`tui.json` and the orca plugins), no data directory |
| cursor-agent | wrapper; not installed | `~/.cursor/` holds `hooks.json`, `hooks.json.bak`, `herdr-agent-state.sh` only. Windows: a project folder `mcp-servers/davinci-resolve-mcp/.cursor/rules/` (rule files) |
| copilot | wrapper; not installed | `~/.copilot/` holds `settings.json` and `hooks/` only. Windows: `AppData/Local/Microsoft/Copilot` and the `Microsoft.Copilot_8wekyb3d8bbwe` package (the Windows Copilot app), and VS Code's bundled `extensions/copilot`: none is the Copilot CLI |
| crush | wrapper; not installed | nothing |

Where it was looked for:

- by name, on `/`, `/home` and the four NTFS volumes (48,269 directories listed, the SPEC's
  prune list, case-insensitive): directories `opencode`, `.opencode`, `.cursor`, `cursor`,
  `cursor-agent`, `.cursor-agent`, `.copilot`, `copilot`, `gh-copilot`, `github-copilot`,
  `.crush`, `crush`, `.gemini`, `gemini-cli`, `session-state`, `history-session-state`; files
  `crush.db`, `opencode.db`, `store.db`, `crush.json`, `opencode.json(c)`, `cli-config.json`.
  Every hit is in the table, bar these: the fixtures of this project, mise's own folders for
  opencode, the `/etc/skel` copy of the opencode config, an editor colour theme, a web asset
  folder, and the Windows Store's own `store.db`;
- by path, under `/home/tanuj` and `…/Users/arunt` (the only profile besides `Default` and
  `Public`): `.local/share/opencode`, `.local/state/opencode`, `.cache/opencode`, `.opencode`,
  `AppData/Local/opencode`, `AppData/Roaming/opencode`; `.cursor/chats`,
  `.cursor/cli-config.json`, `.config/cursor`, `.config/Cursor`, `.local/share/cursor-agent`,
  `AppData/Roaming/Cursor`, `AppData/Local/cursor-agent`, `AppData/Local/Programs/cursor`;
  `.copilot/session-state`, `.copilot/history-session-state`, `.config/github-copilot`,
  `.config/gh-copilot`, `.config/copilot`, `.local/share/copilot`, `.local/state/copilot`,
  `AppData/Local/copilot`, `AppData/Local/github-copilot`, `AppData/Roaming/GitHub Copilot`;
  `.crush`, `.config/crush`, `.local/share/crush`, `.cache/crush`, `AppData/Local/crush`,
  `AppData/Roaming/crush`. The default locations of cursor-agent, copilot and crush are from
  memory of their documentation, not checked against the tools `[inferred]`.

Known from the opencode binary, for when data appears `[verified, code; not run]`: sessions are
in one SQLite file, `<data>/opencode.db` (`opencode-<channel>.db` for other release channels),
with `<data>` = `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`; it has a `session`
command group (`session delete <sessionID>` among it). No format section is written: there is
no file to describe.

Consequence for `status`: it listed `copilot`, `crush` and `cursor-agent` as "installed tools
with no reader" because a wrapper of that name is on PATH; they are not installed. Since the
production pass `status` asks `launch.tool_state`, which reads the wrapper and looks into mise's
installs (below), and lists only what is there.

### Q7. Drive letter `D:`

- The one recorded path `[verified]`: `D:\Hypermedia\Ep2`, the working directory of the Claude
  session `900c4cc6-3c32-4c76-8b5d-d31aabfb0ff9` ("Crime doc asset organization and timeline",
  98 messages, last used 2026-08-25; key `claude:01d855c4:900c4cc6-…`; transcript
  `…/Users/arunt/.claude/projects/D--Hypermedia-Ep2/900c4cc6-….jsonl`).
- It exists on no mounted volume, compared case-insensitively `[verified]`. Only `No Other Way`
  has a `Hypermedia` folder, and it holds `Cine_Ads`, `Creative Content`, `Dr. Insanity`,
  `Ep.2`, `UGC Ads`; `Ep.2` is presumably the renamed folder `[inferred]`.
- The override is `~/.config/agent-sessions/pathmap.json` (the file does not exist yet):

  ```json
  {"D:": "/run/media/tanuj/No Other Way"}
  ```

  Checked with `PathMap.build(…, overrides=…)`, no file written `[verified]`: `D:` then shows as
  resolved by override, `D:\Hypermedia\Cine_Ads` and `D:\D Drive\The Pencl Projects\Final
  Fantasy Lore` map to existing folders, and **`D:\Hypermedia\Ep2` still maps to nothing**,
  because the folder is gone. The override file takes drive letters and absolute POSIX
  prefixes only; a per-folder entry is rejected (`_load_overrides`).
- Without an override `[verified]`: the path map uses the working directories of listed
  sessions as evidence, and this is the only one on `D:`. Adding the keys of the Windows
  `.claude.json` `projects` object (`D:\D Drive\The Pencl Projects\Final Fantasy Lore`,
  `D:\Hypermedia\Cine_Ads`, `D:\Hypermedia\Ep2`, `d:/Fable 5/Crime YT`) resolves `D:` to
  `No Other Way` by evidence, 2 paths.

### Q8. Sessions whose folder is gone, and re-pointing at import

Of the 29 listed foreign sessions, 5 have no existing local folder `[verified]`:

| provider | title | recorded cwd | reason |
|---|---|---|---|
| claude | Crime doc asset organization and timeline (`900c4cc6`) | `D:\Hypermedia\Ep2` | `D:` unresolved (Q7), and the folder is gone: `No Other Way/Hypermedia/` has `Ep.2`, no `Ep2` |
| claude | Matcha on Yu site localhost (`c732c3a3`) | `F:\AI Projects\Website Works\UWW Sites` | exists up to `Zephyro/AI Projects/Website Works`; no `UWW Sites` there (there is a `UWW Website`) |
| antigravity | Japanese Regulatory Document Design (`1a614fc4`) | `C:\Users\arunt\AppData\Local\Temp\claude\F--AI-Projects-Website-Works\402d1819-…\scratchpad\codex-img\agy` | temporary folder, deleted (exists up to `…\Temp\claude\F--AI-Projects-Website-Works`) |
| antigravity | Unbleached Kinari Paper Texture (`b8d26378`) | `C:\Users\arunt\AppData\Local\Temp\sig-img-MwTxj4` | temporary folder, deleted |
| antigravity | Vintage Japanese Regulatory Document (`a630b5c4`) | none recorded | no cwd can be recovered (Antigravity §4: empty `workspace_uris`, not in `last_conversations.json`) |

The one foreign Codex session and the one foreign Grok session resolve; so do the other 21
Claude sessions and 1 Antigravity conversation.

What must change to import a session into a folder `T` the user chooses (`T` exists, absolute;
for Grok take `os.path.realpath(T)`), compared with the recipes of each provider's §6:

- **Claude**: only the destination directory, `~/.claude/projects/<enc(T)>/<id>.jsonl` and the
  `<id>/` side directory, with `enc` as in §2 (the 200-character hash rule included). Nothing
  inside the transcript: the tool finds the file by path (§5, step 1, when started in `T`).
  Compared with a mapped import no other file differs. What differs is meaning: the paths in
  the transcript (each record's `cwd`, the tool calls) name a folder that is not `T` under any
  mapping. The tool's own marker for a moved session, a trailing
  `{"type":"relocated","relocatedCwd":T,"sessionId":<id>}`, is read by its code (`[verified,
  code]`: the last one in the file's tail wins over the head `cwd`) and was never seen in
  data: still not to be written without a test. Today `importer.start_plan` refuses a session
  whose folder is not found; that check is what a chosen folder replaces.
- **Codex**: the destination path does not depend on the cwd, so it is unchanged. In the
  copies the same four fields as in §6 step 3 get `T` instead of the mapped path:
  `session_meta.payload.cwd`, `session_meta.payload.runtime_workspace_roots[]`,
  `turn_context.payload.cwd`, `turn_context.payload.workspace_roots[]`. The difference: the path
  map cannot produce the value, so the rule becomes "a value equal to the recorded cwd `O`
  (case-insensitive, either slash, with or without `\\?\`) becomes `T`; a value below `O`
  becomes the same path below `T`; any other value goes through the path map as now". In the
  one foreign rollout all 1 + 3 + 3 values equal `O`. `thread_settings_applied` stays as
  history. The segment caveat of §6 is unchanged.
- **Grok**: two places, the same two as for a mapped import: the group directory, named by the
  rule of Q1 for `T`, and `summary.json` `info.cwd` = `T` (plus `grok_home`). The difference:
  the name can no longer be taken from an existing group or limited to `[A-Za-z0-9/ ]`; it has
  to be computed for any `T`, which Q1 now allows. Left as recorded, as before: the cwd inside
  `chat_history.jsonl`, `updates.jsonl`, `recap_requests/*.json` and `prompt_context.json`
  (`working_directory`).
- **Every provider**: the import record should say that the folder was chosen, and keep both
  the recorded cwd and `T`. `imported_as` is found by id and is not affected.

---

## Open questions / needs live test

Each open item needs one supervised run by the user; none can be answered from the files.
Items answered since are struck through, with where the answer is.

1. **Claude import** — does 2.1.296 resume a byte-copied Windows transcript placed under
   `projects/<enc(mapped cwd)>/`? Risk low–medium. Also: does `claude --resume <id>` started in
   `$HOME` really pick up a session from another project directory (lookup step 3 was read in the
   source, not run)?
2. **Codex import** — does 0.162.0 find a rollout with no `threads` row and repair its DB? Does it
   accept 0.150.1 rollouts? Does the directory prompt read `session_meta.payload.cwd` or the last
   `turn_context.payload.cwd`? Risk medium. `codex migrate-rollouts --thread <id> --json` (no
   `--apply`) may serve as a model-free check; it was not run (Phase 2, Q2).
3. **Codex segments** — what creates a `<A>_<B>` segment, and is `end_byte_offset` validated
   against the base file (matters only when importing a thread that has one; none of the foreign
   threads does).
4. ~~**Grok encoding** — are `-`, `.`, `_`, `~` percent-encoded in group directory names?~~
   Answered in the sandbox (Phase 2, Q1): they are kept; the full rule, the form for names over
   255 bytes included, is there, and the adapter now names the directory by it (Grok §6, step 1).
5. **Grok import and cross-cwd resume** — ~~`grok export <id>` after a test import is a
   model-free probe.~~ Done (Phase 2, Q1): the imported 1.0.34 session exports in full from any
   directory. Still open: `grok --resume <id>` itself, from the mapped folder and from another
   one; what the tool writes into a `.cwd` file (no group over 255 bytes was ever created by
   it); why the search index holds no text for the imported session.
6. **Antigravity** — ~~which index the tool trusts, and whether it rebuilds them~~: it looks for
   the file `conversations/<id>.db` and rebuilds both indexes (Phase 2, Q5; code and the store's
   own logs, not a run of ours). Still open, for the one hand test of the experimental import:
   does `agy --conversation <id>` show the old turns after a copy of `conversations/<id>.db`,
   `brain/<id>/` and `annotations/<id>.pbtxt`; what it does with Windows workspace URIs; is
   resume by id independent of the cwd; the value of the start-up prune limit.
7. **Antigravity IDE** — conversation format unknown (no data).
8. **Gemini CLI** — nothing is testable until a real `gemini` is installed. Version skew to
   expect: the wrapper would install 0.63.0, the Windows data is 0.54.0.
9. **Sub-agent detection for Codex, Grok, Antigravity** rests on schema and docs; no instance
   exists on any drive.
10. **Cold walk time** — measure once after a reboot or remount.
11. **Mounting** (Phase 2, Q4) — `udisksctl mount` was never run: what it prints, its exit
    codes, the error under `--no-user-interaction`, and where the password is asked when it is
    started from a TUI.
12. **First prompt** (Phase 2, Q3) — are `claude "<prompt>"` and `codex "<prompt>"` sent
    without a key press; does Claude's hidden `--prefill <text>` behave as its description
    says.
13. **Re-pointing** (Phase 2, Q8) — does a Claude session imported under a folder its
    transcript never names resume cleanly there, and is a `relocated` record needed.
14. **`grok sessions list` as a probe** — it also asks a remote service; what it sends when
    logged in and online was not observed (the sandbox had neither).
