"""All five adapters against records and values of the wrong type.

A record whose fields have the wrong type is skipped, never the session
(SPEC "Provider adapter contract"). Every fixture store is copied to a temp
directory and each file a reader looks at gets junk: records appended and,
separately, inserted in the middle of a JSONL file; values replaced in a JSON
file; column values replaced in a SQLite file. The listing and the
transcripts must be what the clean copy gives, or, where a record of the
right shape legitimately counts (an assistant record with no usable content
is still an assistant record), parsing must at least not raise.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES  # noqa: E402

from agent_sessions.model import Store  # noqa: E402
from agent_sessions.providers.antigravity import Antigravity  # noqa: E402
from agent_sessions.providers.base import collect_warnings  # noqa: E402
from agent_sessions.providers.claude import Claude  # noqa: E402
from agent_sessions.providers.codex import Codex  # noqa: E402
from agent_sessions.providers.gemini import Gemini  # noqa: E402
from agent_sessions.providers.grok import Grok  # noqa: E402

# Values of a type no reader expects for a field that holds text or a
# number, plus the nested ones. BAD_TIME stands in for the numbers where a
# number would be a valid time.
BAD_TIME = "not a time"
JUNK = (["list"], {"a": 1}, 7, 2.5, None, True, False, [[{"a": [1]}]], {"a": {"b": [None]}})
TIMES = ("timestamp", "created_at", "startTime", "lastUpdated")
FLAGS = ("isSidechain", "isMeta", "isCompactSummary")


def junk_for(key):
    """The junk values that are of a wrong type for ``key``."""
    if key in TIMES:
        return [v for v in JUNK if not isinstance(v, (int, float))] + [BAD_TIME]
    if key in FLAGS:
        return [v for v in JUNK if not isinstance(v, bool)]
    return list(JUNK)


def spread(keys) -> list[dict]:
    """Junk records over ``keys``: for each junk value one record that has it
    under every key it is wrong for, and one record per key alone."""
    records = []
    for value in JUNK + (BAD_TIME,):
        record = {key: value for key in keys if any(value is v for v in junk_for(key))}
        if record:
            records.append(record)
    for key in keys:
        records += [{key: value} for value in junk_for(key)]
    return records


def each(template, key="J") -> list[dict]:
    """``template`` once per junk value, the value put wherever the string
    ``"J"`` stands."""

    def fill(node, value):
        if node == key:
            return value
        if isinstance(node, dict):
            return {k: fill(v, value) for k, v in node.items()}
        if isinstance(node, list):
            return [fill(v, value) for v in node]
        return node

    return [fill(template, value) for value in JUNK]


def lines(records) -> bytes:
    return b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in records)


class Edit:
    """Change one file of a temp copy and put it back, modification time
    included: a reader may fall back on it."""

    def __init__(self, path):
        self.path = str(path)
        self.raw = Path(path).read_bytes()
        st = os.stat(path)
        self.times = (st.st_atime_ns, st.st_mtime_ns)

    def write(self, data: bytes) -> None:
        Path(self.path).write_bytes(data)
        os.utime(self.path, ns=self.times)

    def restore(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)
        self.write(self.raw)

    def with_lines(self, junk: bytes, middle: bool, first: int = 0) -> None:
        """Junk lines at the end, or after half of the lines (never before
        line ``first``)."""
        rows = self.raw.splitlines(keepends=True)
        if rows and not rows[-1].endswith(b"\n"):
            rows[-1] += b"\n"
        at = max(first, len(rows) // 2) if middle else len(rows)
        self.write(b"".join(rows[:at]) + junk + b"".join(rows[at:]))


class WrongTypesCase(unittest.TestCase):
    provider = None
    stores: tuple = ()  # fixture store roots
    jsonl = ""  # glob, below the store, of the JSONL files read
    first_line = 0  # lines of such a file that must stay first
    json_names: tuple = ()  # names of the JSON files read
    invisible: list = []  # junk records that must change nothing
    append_only: list = []  # ... when they come after every real record
    tolerated: list = []  # junk records that must not raise

    def copy(self, source: Path) -> Store:
        tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-junk-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / source.name
        shutil.copytree(source, root)
        return Store(self.provider.name, str(root), "foreign", str(tmp), "tmp")

    def snapshot(self, store) -> dict:
        """hint -> (session fields, turns) of every candidate; None when not
        listed. Raises what the adapter raises."""
        out = {}
        for candidate in sorted(self.provider.candidates(store), key=lambda c: c.id_hint):
            session = self.provider.parse(store, candidate)
            out[candidate.id_hint] = (
                None
                if session is None
                else (session.to_dict(), list(self.provider.transcript(session)))
            )
        return out

    def each_store(self):
        self.assertTrue(self.stores)
        for source in self.stores:
            store = self.copy(source)
            with collect_warnings():
                clean = self.snapshot(store)
            yield source, store, clean

    def test_junk_records_change_nothing(self):
        if not self.jsonl:
            return  # this reader has no JSONL file
        seen = listed = 0
        for source, store, clean in self.each_store():
            listed += sum(1 for value in clean.values() if value is not None)
            for path in sorted(Path(store.root).glob(self.jsonl)):
                seen += 1
                edit = Edit(path)
                for middle in (False, True):
                    records = self.invisible + ([] if middle else self.append_only)
                    with self.subTest(file=os.path.relpath(path, store.root), middle=middle):
                        edit.with_lines(lines(records), middle, self.first_line)
                        try:
                            with collect_warnings():
                                self.assertEqual(self.snapshot(store), clean)
                        finally:
                            edit.restore()
        self.assertGreater(seen, 0)
        self.assertGreater(listed, 0)

    def test_records_of_the_right_shape_with_junk_inside_do_not_raise(self):
        if not self.jsonl or not self.tolerated:
            return  # nothing of that kind for this reader
        for source, store, _clean in self.each_store():
            for path in sorted(Path(store.root).glob(self.jsonl)):
                edit = Edit(path)
                for middle in (False, True):
                    with self.subTest(file=os.path.relpath(path, store.root), middle=middle):
                        edit.with_lines(lines(self.tolerated), middle, self.first_line)
                        try:
                            with collect_warnings():
                                self.snapshot(store)
                        finally:
                            edit.restore()

    def test_junk_values_in_json_files_do_not_raise(self):
        if not self.json_names:
            return  # this reader has no JSON file
        seen = 0
        for source, store, _clean in self.each_store():
            for path in sorted(
                p for p in Path(store.root).rglob("*.json") if p.name in self.json_names
            ):
                seen += 1
                edit = Edit(path)
                data = json.loads(edit.raw.decode("utf-8-sig"))
                variants = list(JUNK)
                if isinstance(data, dict):
                    for key, inner in data.items():
                        variants += [dict(data, **{key: value}) for value in JUNK]
                        if isinstance(inner, dict):
                            variants += [
                                dict(data, **{key: dict(inner, **{k: value})})
                                for k in inner
                                for value in JUNK
                            ]
                try:
                    for variant in variants:
                        edit.write(json.dumps(variant).encode("utf-8"))
                        with collect_warnings():
                            self.snapshot(store)
                finally:
                    edit.restore()
        self.assertGreater(seen, 0)


# --- Claude -----------------------------------------------------------------

_CLAUDE_KEYS = (
    "type",
    "timestamp",
    "cwd",
    "isSidechain",
    "isMeta",
    "isCompactSummary",
    "message",
    "agentName",
    "customTitle",
    "aiTitle",
    "summary",
    "continuedInSessionId",
    "relocatedCwd",
    "version",
)


class ClaudeWrongTypesTest(WrongTypesCase):
    provider = Claude()
    stores = (
        FIXTURES / "claude" / "native" / ".claude",
        FIXTURES / "claude" / "windows" / ".claude",
    )
    jsonl = "projects/*/*.jsonl"
    invisible = (
        [{"type": ["list"], "timestamp": {"a": 1}, "cwd": 7}]  # the audit's record
        + spread(_CLAUDE_KEYS)
        + each({"type": "ai-title", "aiTitle": "J"})
        + each({"type": "custom-title", "customTitle": "J"})
        + each({"type": "agent-name", "agentName": "J"})
        + each({"type": "summary", "summary": "J"})
        + each({"type": "relocated", "relocatedCwd": "J", "cwd": "J"})
        + each({"type": "continued-in", "continuedInSessionId": "J"})
    )
    # A record that has the key at all is a message record (the tool's own
    # test), so junk under it is only invisible once a real one came first.
    append_only = [{"parentUuid": value} for value in JUNK]
    tolerated = (
        each({"type": "assistant", "message": "J", "timestamp": "J"})
        + each({"type": "assistant", "message": {"id": "J", "content": "J"}})
        + each(
            {
                "type": "assistant",
                "message": {
                    "id": "J",
                    "content": [
                        "J",
                        {"type": "J"},
                        {"type": "text", "text": "J"},
                        {"type": "tool_use", "name": "J"},
                    ],
                },
            }
        )
        + each({"type": "user", "message": {"content": [{"type": "tool_result", "content": "J"}]}})
        # A user record ends a "continued in" note whatever is in it.
        + each({"type": "user", "parentUuid": "J", "message": "J", "isMeta": "J"})
        + each({"type": "user", "message": {"role": "J", "content": "J"}})
        + each(
            {
                "type": "user",
                "message": {
                    "content": ["J", {"type": "J", "text": "J"}, {"type": "text", "text": "J"}]
                },
            }
        )
    )

    def test_the_audits_record(self):
        """The exact line that made ``parse`` raise TypeError."""
        junk = b'{"type":["list"],"timestamp":{"a":1},"cwd":7}\n'
        for source, store, clean in self.each_store():
            for path in sorted(Path(store.root).glob(self.jsonl)):
                edit = Edit(path)
                edit.with_lines(junk, False)
                self.assertEqual(self.snapshot(store), clean)
                edit.restore()


# --- Codex ------------------------------------------------------------------

_CODEX_KEYS = ("type", "timestamp", "ordinal", "payload", "id", "thread_name")


class CodexWrongTypesTest(WrongTypesCase):
    provider = Codex()
    stores = (FIXTURES / "codex" / "native" / ".codex", FIXTURES / "codex" / "windows" / ".codex")
    jsonl = "sessions/*/*/*/rollout-*.jsonl"
    first_line = 1  # the session_meta line stays the first one
    invisible = (
        [
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": {"a": 1}}}}
        ]  # the audit's record
        + spread(_CODEX_KEYS)
        + each({"type": "event_msg", "payload": "J", "ordinal": "J"})
        + each({"type": "event_msg", "payload": {"type": "J", "item": "J"}})
        + each({"type": "event_msg", "payload": {"type": "item_completed", "item": "J"}})
        + each(
            {
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": {"type": "J", "content": "J"}},
            }
        )
        + each({"type": "turn_context", "payload": "J"})
        + each({"type": "response_item", "payload": "J"})
        + each({"type": "response_item", "payload": {"type": "J", "name": "J"}})
        + each({"type": "response_item", "payload": {"type": "function_call", "name": "J"}})
        + each(
            {
                "type": "session_meta",
                "payload": {
                    "id": "J",
                    "cwd": "J",
                    "source": "J",
                    "history_base": "J",
                    "timestamp": "J",
                },
            }
        )
    )
    tolerated = each(
        {
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": {"type": "UserMessage", "content": "J"}},
        }
    ) + each(
        {
            "type": "event_msg",
            "ordinal": "J",
            "payload": {
                "type": "item_completed",
                "item": {"type": "AgentMessage", "content": ["J", {"text": "J"}]},
            },
        }
    )

    def test_the_audits_record(self):
        junk = b'{"type":"event_msg","payload":{"type":"item_completed","item":{"type":{"a":1}}}}\n'
        for source, store, clean in self.each_store():
            for path in sorted(Path(store.root).glob(self.jsonl)):
                edit = Edit(path)
                edit.with_lines(junk, False)
                self.assertEqual(self.snapshot(store), clean)
                edit.restore()

    def test_turn_context_junk_changes_nothing(self):
        junk = lines(each({"type": "turn_context", "payload": {"cwd": "J"}}))
        for source, store, clean in self.each_store():
            for path in sorted(Path(store.root).glob(self.jsonl)):
                edit = Edit(path)
                for middle in (False, True):
                    edit.with_lines(junk, middle, 1)
                    self.assertEqual(self.snapshot(store), clean)
                edit.restore()

    def test_junk_in_the_first_line_does_not_raise(self):
        """Every ``session_meta`` field of the wrong type, one at a time."""
        for source, store, _clean in self.each_store():
            for path in sorted(Path(store.root).glob(self.jsonl)):
                edit = Edit(path)
                head, _, rest = edit.raw.partition(b"\n")
                first = json.loads(head)
                payload = first.get("payload")
                variants = [
                    dict(first, **{key: value})
                    for key in ("type", "timestamp", "ordinal", "payload")
                    for value in junk_for(key)
                ]
                if isinstance(payload, dict):
                    keys = [k for k in payload if k != "base_instructions"]
                    variants += [
                        dict(first, payload=dict(payload, **{key: value}))
                        for key in keys
                        for value in JUNK
                    ]
                try:
                    for variant in variants:
                        edit.write(json.dumps(variant).encode("utf-8") + b"\n" + rest)
                        with collect_warnings():
                            self.snapshot(store)
                finally:
                    edit.restore()

    def test_junk_in_the_thread_name_index_changes_nothing(self):
        source = FIXTURES / "codex" / "native" / ".codex"
        store = self.copy(source)
        clean = self.snapshot(store)
        threads = [fields["id"] for fields, _turns in filter(None, clean.values())]
        self.assertTrue(threads)
        junk = spread(("id", "thread_name"))
        for thread in threads:
            junk += each({"id": thread, "thread_name": "J"})
        edit = Edit(Path(store.root) / "session_index.jsonl")
        for middle in (False, True):
            edit.with_lines(lines(junk), middle)
            self.assertEqual(self.snapshot(store), clean)
        edit.restore()


# --- Grok -------------------------------------------------------------------

_GROK_KEYS = ("type", "synthetic_reason", "content", "tool_calls", "kind", "prompt_index")


class GrokWrongTypesTest(WrongTypesCase):
    provider = Grok()
    stores = (FIXTURES / "grok" / "native" / ".grok", FIXTURES / "grok" / "windows" / ".grok")
    jsonl = "sessions/*/*/chat_history.jsonl"
    json_names = ("summary.json", "meta.json")
    invisible = (
        spread(_GROK_KEYS)
        + each({"type": "user", "content": "J"})
        + each(
            {
                "type": "user",
                "content": ["J", {"type": "J", "text": "J"}, {"type": "text", "text": "J"}],
            }
        )
        + each({"type": "backend_tool_call", "kind": "J"})
        + each({"type": "backend_tool_call", "kind": {"tool_type": "J"}})
    )
    tolerated = (
        each({"type": "assistant", "content": "J", "tool_calls": "J"})
        + each(
            {
                "type": "assistant",
                "content": ["J", {"type": "text", "text": "J"}],
                "tool_calls": ["J", {"name": "J"}],
            }
        )
        + each({"type": "user", "prompt_index": "J", "synthetic_reason": "J", "content": "J"})
    )

    def test_junk_in_a_sub_agents_meta_file_does_not_raise(self):
        """A ``subagents/<child>/meta.json`` of any shape, read by every
        other session's ``parse``."""
        store = self.copy(self.stores[0])
        clean = self.snapshot(store)
        owner = sorted(Path(store.root).glob("sessions/*/*/summary.json"))[0].parent
        meta = owner / "subagents" / "child-1" / "meta.json"
        meta.parent.mkdir(parents=True)
        for value in JUNK + ({"session_id": ["list"], "parent": {"a": 1}, "ids": [7, None]},):
            meta.write_text(json.dumps(value), encoding="utf-8")
            with collect_warnings():
                self.assertEqual(self.snapshot(store), clean)


# --- Gemini -----------------------------------------------------------------

_GEMINI_KEYS = (
    "sessionId",
    "projectHash",
    "startTime",
    "lastUpdated",
    "kind",
    "summary",
    "messages",
    "$rewindTo",
    "id",
    "type",
    "content",
    "timestamp",
    "toolCalls",
    "thoughts",
)
_GEMINI_META = ("sessionId", "projectHash", "kind", "summary", "startTime", "lastUpdated")


class GeminiWrongTypesTest(WrongTypesCase):
    provider = Gemini()
    stores = (FIXTURES / "gemini" / "windows" / ".gemini",)
    jsonl = "tmp/*/chats/session-*.jsonl"
    json_names = ("projects.json",)
    invisible = (
        spread(_GEMINI_KEYS)
        + [{"$set": value} for value in JUNK if not isinstance(value, dict)]
        + [
            {"$set": {key: value}}
            for key in _GEMINI_META
            for value in junk_for(key)
            if not isinstance(value, (int, float)) or key not in TIMES
        ]
        + [{"$set": {"messages": value}} for value in JUNK if not isinstance(value, list)]
        + [
            {"sessionId": "junk-session", "projectHash": value, "kind": "subagent"}
            for value in JUNK
        ]
        + [{"sessionId": value, "projectHash": "junk-hash", "kind": "subagent"} for value in JUNK]
    )
    tolerated = (
        each(
            {
                "id": "junk-1",
                "type": "J",
                "content": "J",
                "toolCalls": "J",
                "thoughts": "J",
                "timestamp": "J",
            }
        )
        + each(
            {
                "id": "junk-2",
                "type": "gemini",
                "content": ["J", {"text": "J"}],
                "toolCalls": [
                    "J",
                    {"name": "J", "timestamp": "J"},
                    {"name": "tool", "timestamp": "J"},
                ],
                "thoughts": "J",
            }
        )
        + each({"id": "junk-3", "type": "user", "content": {"text": "J"}, "timestamp": "J"})
        + each({"$set": {"messages": ["J", {"id": "J"}, {"id": "junk-4", "type": "J"}]}})
    )

    def test_a_metadata_record_with_junk_keeps_the_good_values(self):
        """A later metadata record of the same session whose other fields
        have the wrong type replaces none of them."""
        for source, store, clean in self.each_store():
            for path in sorted(Path(store.root).glob(self.jsonl)):
                edit = Edit(path)
                meta = next(
                    (
                        r
                        for r in map(json.loads, edit.raw.splitlines())
                        if isinstance(r, dict)
                        and isinstance(r.get("sessionId"), str)
                        and isinstance(r.get("projectHash"), str)
                    ),
                    None,
                )
                if meta is None:
                    continue
                ids = {"sessionId": meta["sessionId"], "projectHash": meta["projectHash"]}
                junk = [
                    dict(ids, **{key: value})
                    for key in _GEMINI_META[2:]
                    for value in junk_for(key)
                    if not isinstance(value, (int, float)) or key not in TIMES
                ]
                for middle in (False, True):
                    edit.with_lines(lines(junk), middle)
                    with collect_warnings():
                        self.assertEqual(self.snapshot(store), clean)
                edit.restore()

    def test_a_legacy_json_chat_with_junk_does_not_raise(self):
        """The one-object form older versions wrote, every value replaced."""
        store = self.copy(self.stores[0])
        chats = sorted(Path(store.root).glob("tmp/*/chats"))[0]
        legacy = {
            "sessionId": "legacy-1",
            "projectHash": "h",
            "startTime": "2026-08-30T10:00:00Z",
            "lastUpdated": "2026-08-30T10:00:10Z",
            "summary": "s",
            "kind": "main",
            "messages": [
                {"id": "m1", "type": "user", "content": "hello"},
                {"id": "m2", "type": "gemini", "content": "hi", "toolCalls": [{"name": "t"}]},
            ],
        }
        path = chats / "session-2026-08-30T10-00-legacy01.json"
        variants = [legacy] + list(JUNK)
        variants += [dict(legacy, **{key: value}) for key in legacy for value in JUNK]
        variants += each(
            dict(
                legacy,
                messages=[
                    "J",
                    {"id": "J", "type": "J", "content": "J"},
                    {"type": "gemini", "content": ["J"], "toolCalls": ["J", {"name": "J"}]},
                ],
            )
        )
        listed = 0
        for variant in variants:
            path.write_text(json.dumps(variant), encoding="utf-8")
            with collect_warnings():
                snap = self.snapshot(store)
            listed += snap[f"tmp/{chats.parent.name}/chats/{path.name}"] is not None
        self.assertGreater(listed, 0)  # the clean one, at least, is read


# --- Antigravity ------------------------------------------------------------

_AGY_KEYS = ("type", "content", "created_at", "tool_calls")
_SQL_JUNK = (b"\xff\x00junk", "junk", "[7]", '{"a":1}', 7, -1, 2.5, None)


class AntigravityWrongTypesTest(WrongTypesCase):
    provider = Antigravity()
    stores = (
        FIXTURES / "antigravity" / "native" / ".gemini" / "antigravity-cli",
        FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli",
        FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-ide",
    )
    jsonl = "brain/*/.system_generated/logs/transcript.jsonl"
    json_names = ("last_conversations.json",)
    invisible = (
        spread(_AGY_KEYS)
        + each({"type": "USER_INPUT", "content": "J", "created_at": "J"})
        + each({"type": "PLANNER_RESPONSE", "content": "J", "tool_calls": "J"})
        + each({"type": "PLANNER_RESPONSE", "tool_calls": ["J", {"name": "J"}]})
    )

    def test_junk_records_change_nothing(self):
        # Not every fixture store has a brain log; the ones that have are enough.
        seen = 0
        for source in self.stores:
            if sorted(source.glob(self.jsonl)):
                seen += 1
        self.assertGreater(seen, 0)
        self.stores = tuple(s for s in self.stores if sorted(s.glob(self.jsonl)))
        super().test_junk_records_change_nothing()

    def test_junk_values_in_json_files_do_not_raise(self):
        store = self.copy(self.stores[1])
        cache = Path(store.root) / "cache" / "last_conversations.json"
        cache.parent.mkdir(exist_ok=True)
        ids = [c.id_hint for c in self.provider.candidates(store)]
        variants = list(JUNK) + [{"/work": value, "F:\\Proj": ids[0]} for value in JUNK]
        for variant in variants:
            cache.write_text(json.dumps(variant), encoding="utf-8")
            with collect_warnings():
                self.snapshot(store)

    @staticmethod
    def _columns(path) -> list[tuple[str, str]]:
        """``(table, column)`` of every column that is not a primary key."""
        con = sqlite3.connect(str(path))
        try:
            tables = [
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return [
                (table, row[1])
                for table in tables
                for row in con.execute(f'PRAGMA table_info("{table}")')
                if not row[5]
            ]
        finally:
            con.close()

    def test_junk_column_values_do_not_raise(self):
        """Every column of every table of every database the reader opens,
        set to a value of each SQLite type in turn. With the brain log out
        of the way too, so that the transcript comes from the steps table."""
        tried = 0
        for source, store, _clean in self.each_store():
            root = Path(store.root)
            databases = sorted(root.glob("conversations/*.db"))
            databases += [p for p in (root / "conversation_summaries.db",) if p.exists()]
            for with_logs in (True, False):
                if not with_logs:
                    shutil.rmtree(root / "brain", ignore_errors=True)
                for path in databases:
                    edit = Edit(path)
                    try:
                        for table, column in self._columns(path):
                            for value in _SQL_JUNK:
                                con = sqlite3.connect(str(path))
                                try:
                                    con.execute(f'UPDATE "{table}" SET "{column}" = ?', (value,))
                                    con.commit()
                                except sqlite3.Error:
                                    continue  # the schema refuses this value
                                finally:
                                    con.close()
                                tried += 1
                                with collect_warnings():
                                    self.snapshot(store)
                                edit.restore()
                    finally:
                        edit.restore()
        self.assertGreater(tried, 100)

    def test_a_junk_row_for_another_conversation_changes_nothing(self):
        for source, store, clean in self.each_store():
            path = Path(store.root) / "conversation_summaries.db"
            if not path.exists():
                continue
            con = sqlite3.connect(str(path))
            try:
                columns = [
                    row[1] for row in con.execute('PRAGMA table_info("conversation_summaries")')
                ]
                for number, value in enumerate(_SQL_JUNK):
                    row = [f"junk-{number}" if c == "conversation_id" else value for c in columns]
                    try:
                        con.execute(
                            "INSERT INTO conversation_summaries VALUES (%s)"
                            % ", ".join("?" * len(columns)),
                            row,
                        )
                    except sqlite3.Error:
                        pass
                con.commit()
            finally:
                con.close()
            for suffix in ("-wal", "-shm"):
                self.assertFalse(os.path.exists(str(path) + suffix))
            with collect_warnings():
                self.assertEqual(self.snapshot(store), clean)


del WrongTypesCase  # the base class has nothing to run on its own


if __name__ == "__main__":
    unittest.main()
