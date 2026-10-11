# Test fixtures

Sanitised samples of the five providers' on-disk stores, built by `scratch/build_fixtures.py`
from the real stores on 2026-10-12 (the script is throwaway and git-ignored; the fixtures are the
deliverable). Structure, keys, value types, record order, timestamps and path shapes are real.
Every free-text value (prompts, replies, tool input/output, titles, file contents) is a placeholder
(`text-N`, `title-N`, `args-N`, `name-N`), ids are regenerated consistently (UUIDv7 ids keep their
48-bit time prefix), and paths are stand-ins: `/home/user`, `/home/user/proj`, `/home/user/My Proj`,
`C:\Users\user`, `F:\Proj\My Site`. Long sessions are trimmed to the first and last records plus
one record of every distinct type. Format rules: `docs/FORMATS.md`.

Each `<provider>/<origin>/` directory is laid out like a home directory: point a fake home
(`AGENT_SESSIONS_HOME`) or a fake mount at it. `native` came from the Linux stores, `windows` from
the Windows profile (Windows cwd forms).

## Things the trimming changed

- Message counts are those of the trimmed files (table below), not of the originals.
- Codex `history_base.end_byte_offset` still holds the original byte offset; `end_ordinal_exclusive`
  and the `ordinal` values are original and consistent.
- Grok `summary.json` `num_chat_messages` / `num_messages` were set to the trimmed line counts of
  `chat_history.jsonl` / `updates.jsonl` (the real files obey that equality).
- Antigravity conversation DBs keep the real schema, `trajectory_meta`, a re-encoded
  `trajectory_metadata_blob` and `steps` rows with `step_type`, `status` and a `metadata` blob that
  holds only the step timestamp; `step_payload` holds two varints, not the real payload.
  `raw_summary` is re-encoded with the same field numbers and sanitised strings.
- SQLite fixtures are WAL-mode files with no `-wal`/`-shm` next to them, like the real ones at rest.
  Open them with `mode=ro&immutable=1` or a test will create sidecar files.
- `gemini/windows/.gemini/tmp/my-site/chats/session-2026-08-30T10-00-5b0c3e6a.jsonl` is **synthetic**:
  no resumable Gemini CLI chat exists on any drive, so it was written from the record shapes in the
  Gemini CLI 0.54.0 bundle (header, message records, `$rewindTo`, `$set`). The other Gemini file is real.

## Extra files that matter to tests

- `claude/not-a-store/project/.claude/` — project-level settings folder; `is_store` must be false.
- `claude/windows/.claude/projects/d--Other-Dir/` — project dir with `memory/` only (no transcript).
- `claude/*/.claude.json` — sits beside `.claude`, `projects` keys are real cwd strings (both slash forms, lower-case drive letter).
- `claude/native/.claude/projects/-home-user/<id>/subagents/agent-*.jsonl` + `.meta.json`, `tool-results/` — session side files.
- `codex/native/.codex/session_index.jsonl` — two entries for the first thread; the last one wins.
- `codex/native/.codex/state_5.sqlite` — real `threads` schema (0.162.0), two rows; `rollout_path` points at the newest segment.
- `grok/*/.grok/sessions/session_search.sqlite` — derived FTS index (schema version 4); not a source of truth.
- `grok/*/…/*.lock`, `antigravity/*/…/presence/*.lock` — empty lock files, never sessions.
- `antigravity/windows/.gemini/antigravity-ide/` — empty IDE store (as found).
- `antigravity/*/…/cache/last_conversations.json` — cwd to last conversation id; cwd fallback.
- `gemini/native-no-chats/.gemini/` — a `.gemini` with Antigravity data and settings but no `tmp/`: not a Gemini CLI store.
- `gemini/windows/.gemini/tmp/empty-proj/` — project dir without chats.

## Expected parsed values

`kind` is `listed` for a top-level resumable session; anything else must not be returned by `list_sessions`.
Timestamps are as recorded (adapters normalise to ISO 8601 UTC). `messages` follows the per-provider rule in FORMATS.md.

### claude

| file (under tests/fixtures) | id | kind | title | cwd | started | updated | messages | note |
|---|---|---|---|---|---|---|---|---|
| `claude/native/.claude/projects/-home-user-My-Proj/3f47bd5d-9d03-4733-9887-2452632e5d1a.jsonl` | `3f47bd5d-9d03-4733-9887-2452632e5d1a` | listed | `title-7` | `/home/user/My Proj` | 2026-10-10T11:40:07.198Z | 2026-10-10T11:41:13.487Z | 2 |  |
| `claude/native/.claude/projects/-home-user-proj/55bb3e6d-4d87-4540-a78d-b485572fa15a.jsonl` | `55bb3e6d-4d87-4540-a78d-b485572fa15a` | listed | `title-16` | `/home/user/proj` | 2026-10-08T14:29:37.111Z | 2026-10-08T14:29:37.291Z | 1 |  |
| `claude/native/.claude/projects/-home-user/054512e4-865c-4884-85ac-4c598dd2d2ec.jsonl` | `054512e4-865c-4884-85ac-4c598dd2d2ec` | listed | `title-45` | `/home/user` | 2026-10-06T16:21:25.473Z | 2026-10-06T16:23:37.247Z | 2 |  |
| `claude/native/.claude/projects/-home-user/0786864e-bbc2-4772-a45c-396ce7dd01e1.jsonl` | `0786864e-bbc2-4772-a45c-396ce7dd01e1` | excluded: empty (no prompt, no assistant) | `/mcp` | `/home/user` | 2026-10-03T13:25:29.098Z | 2026-10-03T13:25:52.668Z | 0 |  |
| `claude/native/.claude/projects/-home-user/09d1d9c9-87b3-4bb1-a661-a53e32a64e8c.jsonl` | `09d1d9c9-87b3-4bb1-a661-a53e32a64e8c` | listed | `/cmd args-20` | `/home/user` | 2026-09-25T15:19:46.521Z | 2026-09-25T15:20:35.347Z | 1 |  |
| `claude/native/.claude/projects/-home-user/1f28998c-0737-4594-824d-94eab408a6a3.jsonl` | `1f28998c-0737-4594-824d-94eab408a6a3` | listed | `title-33` | `/home/user` | 2026-10-06T16:46:38.878Z | 2026-10-06T16:50:39.334Z | 2 | continued in 93752775-69de-4168-be73-93ccde31fecc |
| `claude/native/.claude/projects/-home-user/3f86453c-8a00-4bfe-badc-8b210c714024.jsonl` | `3f86453c-8a00-4bfe-badc-8b210c714024` | excluded: bookkeeping-only (no message record) | `title-17` | `` |  |  | 0 |  |
| `claude/native/.claude/projects/-home-user/93752775-69de-4168-be73-93ccde31fecc.jsonl` | `93752775-69de-4168-be73-93ccde31fecc` | listed | `title-33` | `/home/user` | 2026-10-06T16:46:38.878Z | 2026-10-06T16:54:38.007Z | 2 |  |
| `claude/native/.claude/projects/-home-user/054512e4-865c-4884-85ac-4c598dd2d2ec/subagents/agent-4e6b69c678d421890.jsonl` | `agent-4e6b69c678d421890` | excluded: sub-agent transcript (not at projects/<dir>/<uuid>.jsonl) | `` | `` |  |  |  |  |
| `claude/windows/.claude/projects/C--Users-user/2a82a0e9-5e6d-48ad-9a66-ce6bf743f29b.jsonl` | `2a82a0e9-5e6d-48ad-9a66-ce6bf743f29b` | excluded: empty (no prompt, no assistant) | `2a82a0e9` | `C:\Users\user` | 2026-09-10T17:49:12.150Z | 2026-09-10T17:49:12.242Z | 0 |  |
| `claude/windows/.claude/projects/C--Users-user/e4797d48-dfc8-493b-88f2-0fc08153bc79.jsonl` | `e4797d48-dfc8-493b-88f2-0fc08153bc79` | excluded: empty (no prompt, no assistant) | `/cmd args-74` | `C:\Users\user` | 2026-10-09T19:54:56.737Z | 2026-10-09T19:54:59.645Z | 0 |  |
| `claude/windows/.claude/projects/F--Proj-My-Site/214de9ed-5aa4-4788-8a9f-cec8d18b3a3d.jsonl` | `214de9ed-5aa4-4788-8a9f-cec8d18b3a3d` | listed | `title-69` | `F:\Proj\My Site` | 2026-10-08T16:01:41.706Z | 2026-10-08T16:01:49.073Z | 2 |  |
| `claude/windows/.claude/projects/F--Proj-My-Site/326c0d3d-188e-4eb7-a98f-6783f772d1d1.jsonl` | `326c0d3d-188e-4eb7-a98f-6783f772d1d1` | excluded: empty (no prompt, no assistant) | `326c0d3d` | `F:\Proj\My Site` | 2026-09-15T15:44:35.309Z | 2026-09-15T15:44:35.945Z | 0 |  |
| `claude/windows/.claude/projects/F--Proj-My-Site/69669cf3-d7ae-4ba4-a028-c6abfd2bd622.jsonl` | `69669cf3-d7ae-4ba4-a028-c6abfd2bd622` | listed | `title-59` | `F:\Proj\My Site` | 2026-09-10T14:39:17.691Z | 2026-09-10T14:43:33.110Z | 2 |  |

### codex

| file (under tests/fixtures) | id | kind | title | cwd | started | updated | messages | note |
|---|---|---|---|---|---|---|---|---|
| `rollout-2026-09-28T05-04-58-01a0e538-c7bb-77dd-885b-0c4d54663241.jsonl, rollout-2026-09-28T06-22-25-01a0e538-c7bb-77dd-885b-0c4d54663241_01a0e57f-b05d-7b55-a44a-dbc2d903cdef.jsonl` | `01a0e538-c7bb-77dd-885b-0c4d54663241` | listed | `title-168` | `/home/user` | 2026-09-27T23:34:58.749Z | 2026-09-28T00:52:37.556Z | 4 | 2 rollout file(s) |
| `rollout-2026-09-28T23-04-10-01a0e914-cf2b-7dcd-a58f-cb54c3f3e8c7.jsonl` | `01a0e914-cf2b-7dcd-a58f-cb54c3f3e8c7` | listed | `title-169` | `/home/user` | 2026-09-28T17:34:10.219Z | 2026-09-28T17:49:40.106Z | 2 | 1 rollout file(s) |
| `rollout-2026-08-29T13-57-23-01a04ca1-6e27-7817-9f54-07105f962073.jsonl` | `01a04ca1-6e27-7817-9f54-07105f962073` | listed | `text-193` | `F:\Proj\My Site` | 2026-08-29T08:27:23.048Z | 2026-08-29T08:38:45.669Z | 2 | 1 rollout file(s) |
| `rollout-2026-08-29T14-11-24-01a04cae-468f-7291-95d7-556e159046ee.jsonl` | `01a04cae-468f-7291-95d7-556e159046ee` | excluded by default: non-interactive (source=exec) | `text-208` | `C:\Users\user\sub1\sub2\sub3\sub4\sub5\sub6\sub7\sub8` | 2026-08-29T08:41:24.880Z | 2026-08-29T08:42:41.451Z | 2 | 1 rollout file(s) |

### grok

| file (under tests/fixtures) | id | kind | title | cwd | started | updated | messages | note |
|---|---|---|---|---|---|---|---|---|
| `grok/native/.grok/sessions/%2Fhome%2Fuser%2Fproj/01a0ec08-671b-72e4-b2fb-47b426cf36e6/` | `01a0ec08-671b-72e4-b2fb-47b426cf36e6` | listed | `title-46` | `/home/user/proj` | 2026-09-29T07:19:28.817687314Z | 2026-09-29T07:39:14.310818030Z | 3 | group dir %2Fhome%2Fuser%2Fproj |
| `grok/native/.grok/sessions/%2Fhome%2Fuser/01a0e500-fff7-7be6-bc91-b1036c11243e/` | `01a0e500-fff7-7be6-bc91-b1036c11243e` | excluded: empty (no prompt, no assistant) | `01a0e500` | `/home/user` | 2026-09-27T22:34:03.141160747Z | 2026-09-27T22:34:03.308925335Z | 0 | group dir %2Fhome%2Fuser |
| `grok/native/.grok/sessions/%2Fhome%2Fuser/01a10227-8547-7120-95ff-aa7696261403/` | `01a10227-8547-7120-95ff-aa7696261403` | listed | `text-42` | `/home/user` | 2026-10-03T14:25:06.901219758Z | 2026-10-03T14:25:09.153227016Z | 1 | group dir %2Fhome%2Fuser |
| `grok/native/.grok/sessions/%2Fhome%2Fuser/01a106cb-e8dc-7e6e-a7d7-4559e30a19e1/` | `01a106cb-e8dc-7e6e-a7d7-4559e30a19e1` | listed | `title-1` | `/home/user` | 2026-10-04T12:03:09.161527561Z | 2026-10-04T12:03:39.305069470Z | 3 | group dir %2Fhome%2Fuser |
| `grok/windows/.grok/sessions/F%3A%5CProj%5CMy%20Site/01a08073-6af7-7c7c-9dba-e3859e9a2ac8/` | `01a08073-6af7-7c7c-9dba-e3859e9a2ac8` | excluded: empty (no prompt, no assistant) | `01a08073` | `F:\Proj\My Site` | 2026-09-08T09:57:22.808447400Z | 2026-09-08T09:57:25.434797900Z | 0 | group dir F%3A%5CProj%5CMy%20Site |
| `grok/windows/.grok/sessions/F%3A%5CProj%5CMy%20Site/01a0a4c9-5206-7636-8fb7-5bef47aa8954/` | `01a0a4c9-5206-7636-8fb7-5bef47aa8954` | listed | `title-69` | `F:\Proj\My Site` | 2026-09-15T11:17:32.296109300Z | 2026-09-18T11:05:32.847759800Z | 2 | group dir F%3A%5CProj%5CMy%20Site |

### antigravity

| file (under tests/fixtures) | id | kind | title | cwd | started | updated | messages | note |
|---|---|---|---|---|---|---|---|---|
| `antigravity/native/.gemini/antigravity-cli/conversations/d3936295-e998-4e42-bb0d-b8ecbb30fed3.db` | `d3936295-e998-4e42-bb0d-b8ecbb30fed3` | excluded: empty (0 steps) | `d3936295` | `/home/user` | 2026-09-27T22:36:20.053832Z |  | 0 | step_count=0 |
| `antigravity/windows/.gemini/antigravity-cli/conversations/1410b3e9-0fcb-4606-81b1-301f7a833e1a.db` | `1410b3e9-0fcb-4606-81b1-301f7a833e1a` | listed | `title-15` | `C:\Users\user\sub1\sub2\sub3\sub4` | 2026-08-29T09:14:43.040448Z | 2026-08-29T09:15:05.079779Z | 3 | step_count=4 |
| `antigravity/windows/.gemini/antigravity-cli/conversations/1ca54b8b-127f-4f86-9cf4-98d04217f5ce.db` | `1ca54b8b-127f-4f86-9cf4-98d04217f5ce` | listed | `title-1` | `F:\Proj\My Site` | 2026-08-29T08:11:33.927210Z | 2026-08-29T08:57:26.263722Z | 7 | step_count=8 |
| `antigravity/windows/.gemini/antigravity-cli/conversations/45184a9e-8a4e-4f3f-8c4c-f0e85aeecaaf.db` | `45184a9e-8a4e-4f3f-8c4c-f0e85aeecaaf` | listed | `title-26` | `` | 2026-08-29T09:10:34.699091Z | 2026-08-29T09:11:11.228914Z | 3 | step_count=5 |

### gemini

| file (under tests/fixtures) | id | kind | title | cwd | started | updated | messages | note |
|---|---|---|---|---|---|---|---|---|
| `gemini/windows/.gemini/tmp/my-site/chats/session-2026-08-29T09-07-a92c6b68.jsonl` | `a92c6b68-5d07-4d82-adbf-05f532385ccf` | excluded: no resumable content (only injected context) | `a92c6b68` | `f:\proj\my site` | 2026-08-29T09:07:07.397Z | 2026-08-29T09:07:07.398Z | 1 | project slug my-site |
| `gemini/windows/.gemini/tmp/my-site/chats/session-2026-08-30T10-00-5b0c3e6a.jsonl` | `5b0c3e6a-1f2d-4c7b-9a8e-0d1c2b3a4f5e` | listed | `title-synthetic` | `f:\proj\my site` | 2026-08-30T10:00:00.000Z | 2026-08-30T10:00:10.000Z | 3 | project slug my-site |

## Expected counts per fixture store

| provider | store (under tests/fixtures) | listed | candidates |
|---|---|---|---|
| antigravity | `antigravity/native/.gemini/antigravity-cli` | 0 | 1 |
| antigravity | `antigravity/windows/.gemini/antigravity-cli` | 3 | 3 |
| claude | `claude/native/.claude` | 6 | 9 |
| claude | `claude/windows/.claude` | 2 | 5 |
| codex | `codex/native/.codex` | 2 | 2 |
| codex | `codex/windows/.codex` | 1 | 2 |
| gemini | `gemini/windows/.gemini` | 1 | 2 |
| grok | `grok/native/.grok` | 3 | 4 |
| grok | `grok/windows/.grok` | 1 | 2 |

`antigravity/windows/.gemini/antigravity-ide` yields 0 sessions (empty store).

## Leak check

Run from the repo root; all three must print nothing:

```
grep -rail -E 't[a]nuj|a[r]unt' tests/fixtures
grep -raoh -E '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[a-z]{2,}' tests/fixtures
grep -raoh -E '(\bsk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,})' tests/fixtures
```
