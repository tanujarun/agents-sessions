# On-disk formats, resume commands, import recipes

Investigated 2026-10-12 on this machine, read-only. Sources of every claim:

- `[verified]` — seen in the store data, in `--help` output, in the tool's own shipped
  documentation, or in the tool's own source/strings as embedded in its binary or bundle
  (the source of a claim is named where it is not the data).
- `[inferred]` — deduced; not observed end to end. No agent CLI was started and no session
  was resumed, so **everything about what a tool does at resume time is unproven until a
  live test**, even where the lookup code was read. See "Open questions".

Tool versions: `claude` 2.1.296, `codex` 0.162.0, `grok` 1.0.46, `agy` 1.3.3. `gemini` is not
installed (see Gemini CLI). Stores were written by: Claude 2.1.281–2.1.296 (Linux),
2.1.229–2.1.295 (Windows); Codex 0.156.1 (Linux), 0.150.1 (Windows); Grok 1.0.46 (Linux),
1.0.34 (Windows); Gemini CLI 0.54.0 (Windows).

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
  `dir_names = {".grok"}`. `GROK_HOME` can relocate it `[docs]`.

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

`<enc-cwd>` `[verified on 3 names]`: percent-encoding of the UTF-8 cwd, upper-case hex:
`/home/tanuj/Work` → `%2Fhome%2Ftanuj%2FWork`; `F:\AI Projects\Website Works` →
`F%3A%5CAI%20Projects%5CWebsite%20Works`. Space is `%20` (not `+`). `urllib.parse.quote(cwd,
safe="")` reproduces all three, **but** whether Grok also escapes `-`, `.`, `_`, `~` is unknown:
no recorded cwd contains one, and the binary links the `percent-encoding` crate, whose usual
sets do escape them. Decode with `urllib.parse.unquote` (works either way) and prefer
`summary.json` `info.cwd`. "When the encoded name exceeds 255 bytes, it instead uses a slug plus
a hash and records the original path in a `.cwd` file inside the group" `[docs; not seen]`.

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
  `summary.json` (one stale row found). Do not read it.
- Cheap read: `summary.json` alone gives everything except the empty test and `messages`.
  `chat_history.jsonl` up to 24.3 MB (Windows), 9.2 MB native; longest line 1.7 MB.
  `updates.jsonl` up to 49.6 MB; a session directory up to 123.7 MB. Do not read `updates.jsonl`.

### 5. Resume

- argv: `["grok", "--resume", "<session-uuid>"]` `[verified, --help: "-r, --resume
  [<SESSION_ID_OR_TITLE>] Resume a session by ID or title … Non-ID values match session titles
  for the current directory …; UUID-shaped values always mean IDs"]`. `--cwd <CWD>` sets the
  working directory. `-c` = most recent session of the current directory. `-s/--session-id`
  creates, never resumes.
- Scoping: listing and title lookup are per current directory `[docs]`. For an id, the binary
  holds `Session found locally`, `Session found locally under different CWD`, `Session not found
  locally…`, `No session found with id` — so other groups are searched `[inferred from strings]`.
- Launcher: `cd <cwd> && grok --resume <id>`. When the cwd no longer exists: start from `$HOME`
  `[inferred]`.
- No-model checks `[verified, --help]`: `grok sessions list [-n N]` (current directory only),
  `grok sessions search <query>`, `grok export <id>` (Markdown transcript to stdout),
  `grok usage <id>` (JSON).

### 6. Import

Verdict: **supported only in the narrow case below, otherwise NotSupported** `[inferred, medium
to high risk]`.

1. Mapped cwd must consist of `[A-Za-z0-9/ ]` only, **or** an existing native group directory
   must already decode to it (then reuse that directory name). Otherwise raise
   `NotSupported("Grok's directory encoding of '-', '.', '_', '~' is unverified")`.
2. Refuse when `~/.grok/sessions/*/<id>/` exists.
3. Copy the session directory to `~/.grok/sessions/<quote(mapped cwd, safe="")>/<id>/`, leaving
   out `*.lock`.
4. Rewrite `summary.json` only: `info.cwd` → mapped cwd; `grok_home` → the native store path.
   Write it as compact JSON.
5. Leave `session_search.sqlite`, `active_sessions.json` and `prompt_history.jsonl` alone (index
   is rebuilt by the tool `[inferred]`).

To confirm live: that the id is found (`grok export <id>` is a model-free probe), that a
1.0.34 session loads in 1.0.46 (`chat_format_version` is 1 in both), and that Windows paths in
`prompt_context.json` / `system_prompt.txt` are regenerated rather than trusted. The one
Windows conversation is 123.7 MB on disk.

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
- There is no list subcommand `[verified, --help]`.
- IDE conversations: none exist to test. The summaries table has an `app_data_dir` column
  (`antigravity-cli` in every row), which suggests one schema for both apps and that `agy` reads
  only its own directory `[inferred]`. Treat an `antigravity-ide` store as **list-only**.

### 6. Import

Verdict: **NotSupported** — "Antigravity keeps conversations in SQLite files with protobuf
columns; registering one needs a row in `conversation_summaries.db` and the workspace path is
inside protobuf blobs".

Whether a bare copy of `conversations/<id>.db` + `brain/<id>/` is enough for `agy --conversation
<id>` is unknown (the summaries DB and `jetbox_summaries_proto.pb` both index conversations;
which one the tool trusts, and whether it rebuilds them, was not established). Even if it is,
the conversation would keep Windows workspace URIs.

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

## Open questions / needs live test

Each needs one supervised resume by the user; none can be answered from the files.

1. **Claude import** — does 2.1.296 resume a byte-copied Windows transcript placed under
   `projects/<enc(mapped cwd)>/`? Risk low–medium. Also: does `claude --resume <id>` started in
   `$HOME` really pick up a session from another project directory (lookup step 3 was read in the
   source, not run)?
2. **Codex import** — does 0.162.0 find a rollout with no `threads` row and repair its DB? Does it
   accept 0.150.1 rollouts? Does the directory prompt read `session_meta.payload.cwd` or the last
   `turn_context.payload.cwd`? Risk medium.
3. **Codex segments** — what creates a `<A>_<B>` segment, and is `end_byte_offset` validated
   against the base file (matters only when importing a thread that has one; none of the foreign
   threads does).
4. **Grok encoding** — are `-`, `.`, `_`, `~` percent-encoded in group directory names? One
   `grok` session started in such a directory answers it. Until then import is limited as in §6.
5. **Grok import and cross-cwd resume** — `grok export <id>` after a test import is a
   model-free probe. Does `grok --resume <id>` work from another directory?
6. **Antigravity** — does `agy --conversation <id>` work after copying only `conversations/<id>.db`
   and `brain/<id>/`? If yes the verdict could become "supported, copy-only, cwd not rewritten".
   Is resume by id independent of the cwd?
7. **Antigravity IDE** — conversation format unknown (no data).
8. **Gemini CLI** — nothing is testable until a real `gemini` is installed. Version skew to
   expect: the wrapper would install 0.63.0, the Windows data is 0.54.0.
9. **Sub-agent detection for Codex, Grok, Antigravity** rests on schema and docs; no instance
   exists on any drive.
10. **Cold walk time** — measure once after a reboot or remount.
