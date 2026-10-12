# JSON output

What `agent-sessions <command> --json` prints. This file is the contract:
`tests/test_json_contract.py` reads the tables below and checks every real
output against them (the exact set of keys, the type of every value, the
orderings), so a field cannot be added, renamed, dropped or change its type
without this file changing with it.

Three commands have a JSON form: `list`, `scan` and `status`.

## Rules for every output

- One JSON document on stdout, UTF-8, followed by a line break. Nothing else
  is written to stdout; messages go to stderr.
- It is printed with a two-space indent and **the keys of every object in
  alphabetical order**. The same index gives the same bytes: two `list
  --json` calls with no scan between them are byte-identical, and so are two
  after scans that found nothing changed.
- The data is exact. Text from a store (a folder, a path, a warning) is
  not cleaned as it is in the readable output; a character a terminal would
  act on (the C0 and C1 controls, DEL, the bidirectional controls, U+2028,
  U+2029, U+FEFF) is written as a `\uXXXX` escape, which a JSON parser turns
  back into that character. (A `title` never holds one: it is made one clean
  line when the session is read.)
- A path whose name holds bytes that are not UTF-8 has each such byte as
  `\udcXX` (Python's `surrogateescape` form of the byte `XX`).
- Times are UTC text. A session's `started` and `updated` are
  `YYYY-MM-DDTHH:MM:SS.ffffffZ`, always that width, so they sort as text.
  `last_quick` and `last_deep` are `YYYY-MM-DDTHH:MM:SSZ`.
- Exit status 0 when the document was printed. On a failure nothing is
  printed to stdout, one line goes to stderr, and the status is 1.

In the tables, a type is `string`, `integer`, `number` (an integer or a
fraction), `boolean`, the name of another table, `array of …`, `map of string
to …` (an object whose keys are data), and any of them `or null`. A field
without `or null` is never null, and no field is ever left out.

## `list --json`

Output: `array of Session`

The listed sessions: every provider, or one with `--provider`. Newest
`updated` first; sessions with the same `updated` in ascending order of
`key`; sessions with no `updated` last, in ascending order of `key`. `key` is
unique in the array. When there is no usable cache yet, `list` scans first
and says so on stderr; stdout is the array all the same.

### Session

| field | type | meaning |
|---|---|---|
| `provider` | string | the adapter's name: `antigravity`, `claude`, `codex`, `gemini`, `grok` |
| `id` | string | the tool's own id of the session; always in the form the provider's ids have (a UUID for all five) |
| `key` | string | `<provider>:<8 hex of the store's path>:<id>`; the same across scans, and what `open`, `import` and `show` take |
| `title` | string | the tool's own title when it has one, else the first real prompt; one line, at most 80 characters; may be empty |
| `cwd` | string or null | the working directory as the tool recorded it (`F:\Proj\My Site`, `/home/u/x`); null when none is recorded |
| `cwd_local` | string or null | that folder on this machine; null when it is not recorded, not on a mounted drive, or gone |
| `cwd_exists` | boolean | true exactly when `cwd_local` is a directory that exists |
| `started` | string or null | time of the first record |
| `updated` | string or null | time of the last record; what the array is ordered by |
| `messages` | integer or null | number of user and assistant messages; null when the format does not give one |
| `store` | string | root of the store the session is in |
| `origin` | string | `native` (the store the installed tool itself uses) or `foreign` (any other) |
| `mount` | string | mount point of the filesystem the store is on |
| `mount_label` | string | its label (`Linux` for the machine's own) |
| `files` | array of string | the files and directories that make up the session, absolute paths, in the order the adapter lists them (the same on every scan) |
| `launchable` | boolean | true when `open` would resume it: a native session whose tool is installed |
| `imported_as` | string or null | for a foreign session, the `key` of the native session with the same provider and id; null otherwise, and for every native session |
| `note` | string or null | why it cannot be resumed, or what the adapter has to say about it; at most 200 characters |

## `scan --json`

Output: `ScanSummary`

What the scan that just ran did and found.

### ScanSummary

| field | type | meaning |
|---|---|---|
| `mode` | string | `deep` (every mount was walked) or `quick` (known store locations only) |
| `cancelled` | boolean | true when the scan was stopped before it finished (never from the CLI) |
| `seconds` | number | how long it took |
| `mounts` | array of Mount | the filesystems that were mounted, in ascending order of `path` |
| `walk` | map of string to WalkStats or null | per mount path, what the walk of a deep scan did; null after a quick scan |
| `stores` | integer | stores on mounted filesystems |
| `stores_not_mounted` | integer | stores remembered on filesystems that are not mounted now |
| `skipped_stores` | integer | stores found and not listed on purpose (see `status`) |
| `candidates` | integer | things in those stores that might be a session |
| `sessions` | integer | listed sessions |
| `parsed` | integer | candidates that were read in this scan |
| `cached` | integer | candidates taken from the cache because their files had not changed |
| `by_provider` | map of string to ProviderCount | per provider name, every registered provider present |
| `warnings` | array of string | what could not be read and why, in ascending order, no duplicates |

### Mount

| field | type | meaning |
|---|---|---|
| `path` | string | the mount point |
| `label` | string | its label |
| `fstype` | string | the filesystem type (`btrfs`, `ntfs3`, …); may be empty |
| `device` | string | the device it is on; may be empty |

### WalkStats

| field | type | meaning |
|---|---|---|
| `dirs` | integer | directories listed |
| `pruned` | integer | directories skipped by the prune list |
| `errors` | integer | directories that could not be listed |
| `mount_skips` | integer | other mount points that were not crossed |
| `seconds` | number | how long the walk of this mount took |
| `error_paths` | array of string | some of the directories that could not be listed (a sample), in ascending order |

### ProviderCount

| field | type | meaning |
|---|---|---|
| `stores` | integer | stores of this provider on mounted filesystems |
| `sessions` | integer | its listed sessions |

## `status --json`

Output: `Status`

What the index knows, without scanning: reads the cache and the list of
mounts, no store.

### Status

| field | type | meaning |
|---|---|---|
| `cache` | string | path of the index file |
| `log` | string | path of the log file (it exists once something was logged) |
| `last_quick` | string or null | when the last scan of any kind finished; null before the first |
| `last_deep` | string or null | when the last deep scan finished; null before the first |
| `mounts` | array of StatusMount | the mounted filesystems in ascending order of `path`, then the unmounted ones that hold a remembered store, in ascending order of `path` |
| `providers` | array of ProviderStatus | one per provider, in ascending order of `name` |
| `stores` | array of StoreStatus | every remembered store, in ascending order of `provider`, then `root` |
| `skipped_stores` | array of SkippedStore | stores found and not listed on purpose, in ascending order of `provider`, then `root` |
| `letters` | array of DriveLetter | drive letters that are resolved, in ascending order of `source_mount` (null first), then `letter` |
| `unresolved_letters` | array of UnresolvedLetter | drive letters recorded in sessions that are not resolved, in ascending order of `source_mount`, then `letter` |
| `tools` | array of Tool | every agent tool this program knows of, with or without a reader, in ascending order of `tool` |
| `no_reader` | array of NoReaderTool | the tools of `tools` that are installed and have no reader, in ascending order of `tool` |
| `warnings` | array of string | the warnings of the last scan and of the path map, in ascending order, no duplicates |

### StatusMount

| field | type | meaning |
|---|---|---|
| `path` | string | the mount point |
| `label` | string | its label |
| `fstype` | string | the filesystem type; empty when not mounted |
| `device` | string | the device; empty when not mounted |
| `mounted` | boolean | false for a filesystem that holds a remembered store and is not mounted now |
| `letter` | string or null | the Windows drive letter this mount stands for (`C:`), when one is resolved |
| `walk` | WalkStats or null | what the last deep scan did here; null when it was never walked or is not mounted |

### ProviderStatus

| field | type | meaning |
|---|---|---|
| `name` | string | the adapter's name |
| `label` | string | the name for people (`Claude`) |
| `binary` | string | the command that resumes a session |
| `installed` | string or null | path of that command on PATH when the tool is installed; null when it is not found, and when the command is only a wrapper that would install the tool |
| `tool` | string | which of the three: `installed`, `not installed (the command on PATH would install it)`, `not found` |
| `resumable` | boolean | false when this provider's sessions cannot be resumed on this machine at all |
| `reader` | boolean | false when a store of this provider could not be read by its adapter |
| `stores` | integer | its stores on mounted filesystems |
| `sessions` | integer | its listed sessions |

### StoreStatus

| field | type | meaning |
|---|---|---|
| `provider` | string | the adapter's name |
| `root` | string | the store's directory |
| `origin` | string | `native` or `foreign` |
| `mount` | string | mount point of its filesystem |
| `mount_label` | string | that filesystem's label |
| `present` | boolean | false when its filesystem is not mounted (its sessions are then not listed) |
| `reader` | boolean | false when its adapter could not read it |
| `candidates` | integer | things in it that might be a session |
| `listed` | integer | sessions of it that are listed |
| `skipped` | integer | candidates that are not sessions (sub-agent transcripts, empty files, refused ids) |
| `duplicates` | integer | sessions left out because another store of the same origin holds the same one |

### SkippedStore

| field | type | meaning |
|---|---|---|
| `provider` | string | the adapter's name |
| `root` | string | the store's directory |
| `reason` | string | why it is not listed (a desktop app's history, a store inside this checkout, …) |
| `mounted` | boolean | whether its filesystem is mounted now |

### DriveLetter

| field | type | meaning |
|---|---|---|
| `letter` | string | the drive letter with its colon (`F:`), or another root that was mapped |
| `source_mount` | string or null | mount point of the store whose sessions recorded it; null for a manual override that no session uses |
| `mount` | string | the directory on this machine it stands for |
| `how` | string | `system volume`, `evidence` or `override` |
| `paths` | integer | recorded paths that use it |

### UnresolvedLetter

| field | type | meaning |
|---|---|---|
| `letter` | string | the drive letter with its colon |
| `source_mount` | string | mount point of the store whose sessions recorded it |
| `reason` | string | `no evidence` or `ambiguous` |
| `candidates` | array of string | mount points it could be, in ascending order; empty for `no evidence` |
| `paths` | integer | recorded paths that use it |

### Tool

| field | type | meaning |
|---|---|---|
| `tool` | string | the command's name |
| `path` | string or null | what PATH gives for it; null when nothing |
| `installed` | boolean | true when the tool itself is there to be started |
| `wrapper` | boolean | true when the command on PATH is a script that installs the tool when it is missing |
| `detail` | string | `installed`, `not installed (the command on PATH would install it)` or `not found` |
| `reader` | boolean | true when it is the command of one of `providers` |

### NoReaderTool

| field | type | meaning |
|---|---|---|
| `tool` | string | the command's name |
| `path` | string | the command on PATH |

## Commands without a JSON form

`verify`, `open --dry-run`, `import --dry-run` and `show` print text for a
person and have no `--json`. Their data is available to a program through
the engine (`agent_sessions.api`: `verify()`, `open_session(…, dry_run=True)`,
`import_plan()`, `transcript()`); those return values are not part of this
contract.
