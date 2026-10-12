# Adding a provider

A provider is one agent tool whose saved sessions agent-sessions can read: Claude Code,
Codex, Grok, Antigravity and Gemini CLI today. Each is one module under
`src/agent_sessions/providers/` with one class, an **adapter**, that subclasses
`providers.base.Provider`. Everything else (discovery, the index, the path map, launching,
importing, the command line, the TUI) works from that class and needs no change for a new
tool, apart from the few places listed in step 6.

This page describes the contract as it is in `providers/base.py` now, the steps to add a
reader, and the state of the four tools that have none.

The first rule comes before any code: **formats come from evidence, never from memory or
from a guess.** A reader is written only when real session files exist to read, and every
statement about the format is recorded in [FORMATS.md](FORMATS.md) with where it was seen.
A reader built on guesses lists wrong sessions silently, which is worse than listing none.

## The adapter contract

### Attributes

| Attribute | Required | Meaning |
|---|---|---|
| `name` | yes | the provider's key, lower case: `"claude"`. Used in session keys, `--provider`, the journal, file names of exported transcripts. Must be unique; the registry refuses an empty one |
| `label` | yes | what a person reads: `"Claude"` |
| `binary` | yes | the command that resumes a session and starts a new one: `"claude"`. `launch.tool_state(binary)` decides whether the tool counts as installed |
| `app_id` | yes | the window class given to the terminal launcher (`--app-id`): `"org.omarchy.claude"` |
| `dir_names` | yes | a set of directory names that may be a store root (`{".claude"}`). Discovery looks for these names on every drive and confirms with `is_store` |
| `id_pattern` | no | a compiled pattern a session id must match **whole**. Default: a UUID (`model.UUID_PATTERN`), which fits all five tools. Override it only for a tool whose ids are not UUIDs, and then make it as narrow as the tool's ids are: it must never match text that starts with `-` or holds a path separator |
| `resume_needs_cwd` | no | `True` when the tool finds a session only from its project folder. Then a session whose folder is gone is refused instead of being started in the home directory. Default `False` |
| `resumable` | no | a property; `False` when the tool cannot resume a session on this machine at all. Default `True`. Gemini computes it from whether the real program is installed |

### Methods every reader needs

| Method | What it must do |
|---|---|
| `native_store() -> Path \| None` | the store the installed tool itself uses, built from `fsutil.home()` (never `Path.home()`: the tests and `AGENT_SESSIONS_HOME` replace the home). It need not exist |
| `is_store(path) -> bool` | a cheap signature check: does this directory look like a store of this tool (for example, does it hold a `projects` directory). Called on many directories during a walk; one or two `stat` calls, no reads |
| `candidates(store) -> list[Candidate]` | everything that may be a session, from names and `stat` only, no file reads. A `Candidate` is `(id_hint, paths)`: `paths` are **all** the files whose path, size and modification time decide whether a cached parse still stands (a directory's own `stat` does not change when a file in it grows). Report a directory that cannot be listed with `base.unreadable` |
| `parse(store, candidate) -> Session \| None` | read one candidate and return `Session.for_store(store, id=…, title=…, cwd=…, started=…, updated=…, messages=…, files=…, note=…)`, or `None` when it is not a listed session (empty, a sub-agent run, a bookkeeping file). Never raise on a malformed file: call `base.warn(message, path)` and return `None` |
| `transcript(session) -> Iterator[Turn]` | the conversation as `Turn(role, text, ts)`, oldest first, lazily: `"user"` and `"assistant"` turns with their text, one `Turn("tool", "<tool name>", ts)` per tool call. Tool output, reasoning and injected context are left out. Must work for a foreign session |
| `resume_argv(session) -> list[str]` | the command that resumes a native session: the tool's own words and the id, for example `[self.binary, "--resume", s.id]` |

### Methods with a default

| Method | Default | Override when |
|---|---|---|
| `new_session_argv(prompt)` | raises `NotImplementedError`; `continue` then says the tool has no such command | the tool can be started as a new **interactive** session with a first message. Return the tool's own words and the prompt as one whole element; never a non-interactive form (`-p`, `exec`) |
| `import_plan(session, pathmap, target=None, experimental=False)` | raises `NotSupported("… sessions cannot be imported")` | a foreign session can be copied into the native store so that the tool resumes it. Build the plan with `importer.start_plan`, `importer.file_copy` and `importer.tree`; read the source only and write nothing. Raise `NotSupported(reason)` when the format cannot be imported safely, `ExperimentalOnly(reason, warning)` when no real resume has confirmed the recipe and `experimental` is false |
| `import_session(...)` | plans, runs `importer.execute`, reads the copy back | almost never: the default is the whole import |
| `read_back(plan)` | parses the native store's candidates that are made of the placed files and returns the one with the plan's id | the tool's layout makes that test wrong for it |
| `undo_extras(session_id, native_root)` | `[]` | the tool creates files of its own beside an imported session once it has opened it (Antigravity: `-wal`, `-shm`, a presence lock). An undo removes them only when it is forced |
| `evidence_files(store)`, `path_evidence(store)` | `[]` | the store records project folders beyond the sessions' own working directories (Claude: the `projects` keys of `.claude.json`). They help to work out which mount a drive letter stands for. Read bounded; never raise |
| `valid_id(session_id)` | text that matches `id_pattern` whole | never |
| `list_sessions(store)` | parse every candidate, uncached | never; it is a convenience for tests |

### Helpers to use, and what not to use

From `providers.base`: `iter_jsonl` (bounded lines, damaged lines skipped), `read_small` (a
whole file up to a cap), `open_sqlite_ro` (the only way a SQLite file may be opened),
`open_ro` (the only way a store's file may be opened), `parse_ts`, `clean_title`, `warn`,
`unreadable`. Do not use a plain `open`, `sqlite3.connect`, `shutil.copy` or `subprocess`
in an adapter, and catch `RecursionError` together with `ValueError` wherever JSON is
parsed. `CLAUDE.md` states these rules, and several tests search the source for them.

### What the index enforces, whatever the adapter returns

An adapter cannot weaken these; they are applied to every parsed session and again to
every cached one (`index.py`):

- **The id.** A session whose `id` does not pass `valid_id` is not listed, and a warning
  says so. Launch and import check the id again.
- **Sizes.** The title goes through `clean_title` (one line, 80 characters, no control
  characters); a recorded folder longer than 32,767 characters is dropped; a note is cut to
  200 characters.
- **Errors.** An exception out of `candidates`, `parse` or `path_evidence` is caught,
  reported as a warning and not cached, so the candidate is tried again on the next scan.
  It never ends a scan. (It is still a bug in the adapter: `parse` should warn and return
  `None`.)
- **Fields the index owns.** `key`, `store`, `origin`, `mount`, `mount_label`,
  `cwd_local`, `cwd_exists`, `launchable`, `imported_as` and `offline` are filled by the
  index; `parse` fills only the conversation fields.
- **Caching.** `parse` runs only for a candidate whose signature (the `(path, mtime, size)`
  of its `paths`) changed. Anything `parse` reads must therefore be in `paths`.
- **Duplicates and order.** Of two sessions with the same provider and id from stores of
  the same origin, the newer is listed. Output is sorted; `list --json` of an unchanged
  machine is byte-identical between runs.

And for an import (`importer.py`): every destination must lie below the native store;
nothing existing is overwritten; the copy is read back and must be the planned session;
any failure takes out what was placed. See [SECURITY.md](SECURITY.md).

## Steps

1. **Investigate, read-only, and write it down.** Find real stores of the tool (on Linux
   and, when there is one, on a Windows drive). Add a section to
   [FORMATS.md](FORMATS.md) in the shape of the existing ones: store signature, layout,
   what one session is and what is excluded, where each field comes from (exact JSON keys
   or SQL), the resume command, whether resume depends on the working directory, what an
   import would need, quirks. Tag every statement: `[verified]` (seen in the data, in
   `--help`, in the tool's shipped documentation or code), `[verified, code]` (read in the
   tool's code, not run), `[verified, sandbox]` (seen when a model-free command ran in a
   throwaway home without network) or `[inferred]`. `--help` and `--version` of a tool may
   be run; the tool itself may not be started interactively, and no model call may be made
   (`CLAUDE.md`). Put what only a live run can answer under "Open questions".
2. **Make fixtures.** Add sanitised copies of real stores under
   `tests/fixtures/<provider>/native/` and `…/windows/`, laid out like a home directory.
   Keep structure, keys, types, record order, timestamps and path shapes real; replace
   every free text with a placeholder and regenerate ids consistently. Record what each
   file is and its expected parsed values in `tests/fixtures/README.md`, and run the leak
   check described there. A SQLite fixture must be a WAL-mode file with no sidecar files.
3. **Write the adapter** in `providers/<name>.py`, decorated with `@register`, and import
   it at the bottom of `providers/__init__.py`. Start with `is_store`, `candidates`,
   `parse`, `transcript` and `resume_argv`; leave import as the default refusal until its
   recipe is in FORMATS.md.
4. **Test the reader** in `tests/test_<name>.py` against the fixtures: the store
   signature (and a directory that is not a store), every expected value of the fixture
   table, what is excluded, a truncated file, a line that is not UTF-8, the transcript,
   the resume command.
5. **Test it against hostile input.** Add the provider to the suites that feed every
   adapter bad data, and make them pass: `tests/test_wrong_types.py` (records, JSON values
   and SQLite columns of the wrong type), `tests/test_hostile_files.py` (a named pipe, a
   device and a directory under a session file's name; over-long lines; size caps; deeply
   nested JSON), `tests/test_unreadable.py` (a directory at mode 000),
   `tests/test_session_ids.py` (an id that is not in the tool's form, in the index, in
   `launch.plan` and in `importer.check`), `tests/test_textsafe.py` (escape sequences in
   every text field), and for a SQLite store `tests/test_sqlite_hardening.py`. If the
   adapter imports: `tests/test_import_safety.py` (every name it takes from the source)
   and `tests/test_unimport.py` (import then undo leaves the home byte for byte as it was).
6. **Register it in the places that list providers by name.**
   - `tests/fixture_world.py` (`REAL`, `TOOLS`) and `tests/test_discover.py` (`REAL`, the
     table of labels, binaries and app ids), `tests/test_smoke.py` (the registry's names).
   - `tui/view.py` (`PROVIDER_ORDER`): the tab order. A provider that is not in it still
     gets a tab, after the known ones.
   - `api.NO_READER_TOOLS`: remove the tool's command from it.
   - `tests/test_continue.py`: the new-session command, against FORMATS.md.
   - The command line needs nothing: `--provider` and `continue --with` take their choices
     from the registry.
7. **Keep the JSON contract.** A new provider adds values, not fields: `provider` in
   `list --json` and a row in `status --json`. If a change does alter a shape, change
   [JSON.md](JSON.md) and `tests/test_json_contract.py` together.
8. **Update the documents**: the capability table in the README and in
   [SPEC.md](SPEC.md), `filemap.md` (a test fails when a module is missing from it), and
   `CHANGELOG.md`.
9. **Run the gates**: `make check`, and the engine suite on Python 3.11. Then, on the real
   machine, `agent-sessions scan --deep` and `agent-sessions verify`: `verify` counts the
   sessions on disk without the index and must agree. An independent count by a throwaway
   script that does not import the package (as was done for the first five) is the check
   that the reader lists what is there.

A resume, an import or a continue of the new tool then joins the list of things that were
never run for real (README, "What has not been tried on a real tool") until the owner has
done each once.

## The four tools without a reader

From [FORMATS.md](FORMATS.md), "Q6" (2026-10-12). **No session data of any of them exists
on Linux or on any mounted drive**, which is why there is no reader: there is no file to
describe.

| Tool | State on the machine this was built on | What is known about its data |
|---|---|---|
| opencode | installed (1.18.35, behind a wrapper), never used | from its binary, not run: sessions are in one SQLite file, `<data>/opencode.db` (`opencode-<channel>.db` for other release channels), with `<data>` being `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`; it has a `session` command group. The data directory held two empty directories and no database |
| cursor-agent | not installed (the wrapper on `PATH` would install it) | nothing: `~/.cursor/` held hook files only. Its default data locations are remembered from its documentation, not checked |
| copilot | not installed (wrapper) | nothing: `~/.copilot/` held `settings.json` and `hooks/` only. What was found on Windows is the Windows Copilot app and a VS Code extension, neither of which is the Copilot CLI |
| crush | not installed (wrapper) | nothing |

`status` lists those of the four that are really installed under "installed tools with no
reader" (`api.NO_READER_TOOLS`), so the gap is visible to the user.

Gemini CLI is a different case: it has a reader, and its chats are listed, but the real
program was not installed on the machine (the wrapper's install was incomplete) and the
only chat with content was written by hand from the record shapes in the tool's bundle.
Its resume is therefore untested, and its import is refused.

To write the first of these readers, start at step 1 with real data: install the tool, use
it once so that it writes a session, and describe what it wrote.
