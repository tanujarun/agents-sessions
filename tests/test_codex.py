"""providers/codex.py against tests/fixtures and edge cases on temp copies.

Expected values: tests/fixtures/README.md. Rules: docs/FORMATS.md "Codex".
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES, Sandbox  # noqa: E402

from agent_sessions import api  # noqa: E402
from agent_sessions.model import Candidate, Session, Store  # noqa: E402
from agent_sessions.providers import base  # noqa: E402
from agent_sessions.providers.base import Turn, collect_warnings, parse_ts  # noqa: E402
from agent_sessions.providers.codex import Codex  # noqa: E402

NATIVE = FIXTURES / "codex" / "native" / ".codex"
WINDOWS = FIXTURES / "codex" / "windows" / ".codex"

SEGMENTED = "01a0e538-c7bb-77dd-885b-0c4d54663241"
SEGMENT = "01a0e57f-b05d-7b55-a44a-dbc2d903cdef"
PLAIN = "01a0e914-cf2b-7dcd-a58f-cb54c3f3e8c7"
WIN = "01a04ca1-6e27-7817-9f54-07105f962073"
EXEC = "01a04cae-468f-7291-95d7-556e159046ee"

BASE_FILE = f"sessions/2026/09/28/rollout-2026-09-28T05-04-58-{SEGMENTED}.jsonl"
SEGMENT_FILE = f"sessions/2026/09/28/rollout-2026-09-28T06-22-25-{SEGMENTED}_{SEGMENT}.jsonl"
PLAIN_FILE = f"sessions/2026/09/28/rollout-2026-09-28T23-04-10-{PLAIN}.jsonl"
WIN_FILE = f"sessions/2026/08/29/rollout-2026-08-29T13-57-23-{WIN}.jsonl"
EXEC_FILE = f"sessions/2026/08/29/rollout-2026-08-29T14-11-24-{EXEC}.jsonl"

# id: (store, files, title, cwd, started, updated, messages)
LISTED = {
    SEGMENTED: (
        NATIVE,
        [BASE_FILE, SEGMENT_FILE],
        "title-168",
        "/home/user",
        "2026-09-27T23:34:58.749Z",
        "2026-09-28T00:52:37.556Z",
        4,
    ),
    PLAIN: (
        NATIVE,
        [PLAIN_FILE],
        "title-169",
        "/home/user",
        "2026-09-28T17:34:10.219Z",
        "2026-09-28T17:49:40.106Z",
        2,
    ),
    WIN: (
        WINDOWS,
        [WIN_FILE],
        "text-193",
        "F:\\Proj\\My Site",
        "2026-08-29T08:27:23.048Z",
        "2026-08-29T08:38:45.669Z",
        2,
    ),
}
NEW_ID = "01a11111-2222-7333-8444-555555555555"
OTHER_ID = "01a1aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


def store_for(root) -> Store:
    native = Path(root) == NATIVE
    return Store(
        "codex",
        str(root),
        origin="native" if native else "foreign",
        mount="/" if native else "/mnt/win",
        mount_label="Linux" if native else "Win",
    )


def line(kind, ordinal, payload, ts="2026-10-01T10:00:00.000Z") -> dict:
    return {"timestamp": ts, "ordinal": ordinal, "type": kind, "payload": payload}


def meta(ordinal=0, ident=NEW_ID, ts="2026-10-01T10:00:00.000Z", **extra) -> dict:
    payload = {
        "session_id": ident,
        "id": ident,
        "timestamp": "2026-10-01T09:59:00.000Z",
        "cwd": "/work/dir",
        "originator": "codex-tui",
        "cli_version": "0.156.1",
        "source": "cli",
        "thread_source": "user",
        "history_mode": "paginated",
        "base_instructions": {"text": "instructions " * 50},
    }
    payload.update(extra)
    return line("session_meta", ordinal, payload, ts)


def item(ordinal, kind, text, ts="2026-10-01T10:00:00.000Z") -> dict:
    block = (
        {"type": "text", "text": text, "text_elements": []}
        if kind == "UserMessage"
        else {"type": "Text", "text": text}
    )
    return line(
        "event_msg",
        ordinal,
        {
            "type": "item_completed",
            "thread_id": NEW_ID,
            "item": {"type": kind, "id": "i", "content": [block]},
        },
        ts,
    )


def said(ordinal, text, ts="2026-10-01T10:00:01.000Z") -> dict:
    return item(ordinal, "UserMessage", text, ts)


def replied(ordinal, text, ts="2026-10-01T10:00:02.000Z") -> dict:
    return item(ordinal, "AgentMessage", text, ts)


def tool_call(ordinal, name, ts="2026-10-01T10:00:03.000Z", kind="custom_tool_call") -> dict:
    return line(
        "response_item",
        ordinal,
        {"type": kind, "name": name, "call_id": "c", "input": "secret input"},
        ts,
    )


class CodexCase(unittest.TestCase):
    def setUp(self):
        self.provider = Codex()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-codex-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / ".codex"
        self.day = self.root / "sessions" / "2026" / "10" / "01"
        self.day.mkdir(parents=True)
        self.store = Store("codex", str(self.root), origin="native")

    def write(
        self,
        records,
        ident=NEW_ID,
        segment=None,
        raw=b"",
        stamp="2026-10-01T10-00-00",
        suffix=".jsonl",
    ) -> Path:
        name = f"rollout-{stamp}-{ident}" + (f"_{segment}" if segment else "") + suffix
        path = self.day / name
        self.day.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records) + raw)
        return path

    def copy_store(self, root) -> Store:
        shutil.rmtree(self.root)
        shutil.copytree(root, self.root)
        return self.store

    def parse_all(self, store=None):
        """``({id: session}, excluded hints, warnings)`` for a whole store."""
        store = store or self.store
        listed, excluded = {}, []
        with collect_warnings() as collected:
            for candidate in self.provider.candidates(store):
                session = self.provider.parse(store, candidate)
                if session is None:
                    excluded.append(candidate.id_hint)
                else:
                    listed[session.id] = session
        return listed, excluded, collected.items

    def only(self) -> Session:
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((excluded, warnings), ([], []))
        self.assertEqual(len(listed), 1)
        return next(iter(listed.values()))

    def rewrite(self, rel, change) -> Path:
        """Apply ``change(records)`` to one rollout of the temp store."""
        path = self.root / rel
        records = [json.loads(row) for row in path.read_text().splitlines()]
        records = change(records) or records
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path


class CandidatesTest(CodexCase):
    def test_one_candidate_per_thread(self):
        found = self.provider.candidates(store_for(NATIVE))
        index = str(NATIVE / "session_index.jsonl")
        self.assertEqual(
            [(c.id_hint, c.paths) for c in found],
            [
                (SEGMENTED, [str(NATIVE / BASE_FILE), str(NATIVE / SEGMENT_FILE), index]),
                (PLAIN, [str(NATIVE / PLAIN_FILE), index]),
            ],
        )

    def test_index_is_listed_even_when_absent(self):
        index = WINDOWS / "session_index.jsonl"
        self.assertFalse(index.exists())
        found = self.provider.candidates(store_for(WINDOWS))
        self.assertEqual(
            [(c.id_hint, c.paths) for c in found],
            [
                (WIN, [str(WINDOWS / WIN_FILE), str(index)]),
                (EXEC, [str(WINDOWS / EXEC_FILE), str(index)]),
            ],
        )

    def test_counts_match_the_readme(self):
        for root, candidates, listed in ((NATIVE, 2, 2), (WINDOWS, 2, 1)):
            store = store_for(root)
            self.assertEqual(len(self.provider.candidates(store)), candidates)
            self.assertEqual(len(self.provider.list_sessions(store)), listed)

    def test_only_rollouts_under_the_date_directories(self):
        path = self.write([meta(), said(1, "hello")])
        for name in (
            "notes.jsonl",
            f"rollout-{NEW_ID}.jsonl",
            f"rollout-2026-10-01-{NEW_ID}.jsonl",
            f"rollout-2026-10-01T10-00-00-{NEW_ID}.json",
            f"rollout-2026-10-01T10-00-00-{NEW_ID.upper()}.jsonl",
        ):
            (self.day / name).write_text("{}\n")
        stray = f"rollout-2026-10-01T10-00-00-{OTHER_ID}.jsonl"
        (self.root / "sessions" / stray).write_text("{}\n")
        (self.root / "sessions" / "2026" / stray).write_text("{}\n")
        for other in ("thread-writer-locks", "shell_snapshots", "archived_sessions"):
            (self.root / other).mkdir()
            (self.root / other / stray).write_text("{}\n")
        found = self.provider.candidates(self.store)
        self.assertEqual(
            [(c.id_hint, c.paths) for c in found],
            [(NEW_ID, [str(path), str(self.root / "session_index.jsonl")])],
        )

    def test_candidates_read_no_file(self):
        boom = mock.Mock(side_effect=AssertionError("candidates must not read files"))
        with mock.patch.object(base, "open_ro", boom), mock.patch("builtins.open", boom):
            self.assertEqual(len(self.provider.candidates(store_for(NATIVE))), 2)
        boom.assert_not_called()

    def test_store_without_sessions(self):
        self.assertEqual(self.provider.candidates(Store("codex", str(self.tmp / "nope"))), [])


class FixtureParseTest(CodexCase):
    def test_listed_sessions(self):
        found = {}
        for root in (NATIVE, WINDOWS):
            listed, _excluded, warnings = self.parse_all(store_for(root))
            self.assertEqual(warnings, [])
            found.update(listed)
        self.assertEqual(sorted(found), sorted(LISTED))
        for ident, (root, files, title, cwd, started, updated, messages) in LISTED.items():
            with self.subTest(session=ident):
                session, store = found[ident], store_for(root)
                self.assertEqual(
                    (
                        session.id,
                        session.title,
                        session.cwd,
                        session.started,
                        session.updated,
                        session.messages,
                        session.note,
                    ),
                    (ident, title, cwd, parse_ts(started), parse_ts(updated), messages, None),
                )
                self.assertEqual(session.files, [str(root / rel) for rel in files])
                self.assertEqual(
                    (session.provider, session.store, session.origin),
                    ("codex", store.root, store.origin),
                )
                self.assertEqual(session.key, Session.for_store(store, id=ident).key)

    def test_exec_run_is_excluded(self):
        listed, excluded, warnings = self.parse_all(store_for(WINDOWS))
        self.assertEqual((sorted(listed), excluded, warnings), ([WIN], [EXEC], []))
        # It is excluded for its source alone: as a cli thread it would list
        # with the README's values.
        self.copy_store(WINDOWS)
        self.rewrite(EXEC_FILE, lambda rows: rows[0]["payload"].update(source="cli"))
        session = self.parse_all()[0][EXEC]
        self.assertEqual(
            (session.title, session.cwd, session.started, session.updated, session.messages),
            (
                "text-208",
                "C:\\Users\\user\\sub1\\sub2\\sub3\\sub4\\sub5\\sub6\\sub7\\sub8",
                parse_ts("2026-08-29T08:41:24.880Z"),
                parse_ts("2026-08-29T08:42:41.451Z"),
                2,
            ),
        )

    def test_title_is_the_last_index_line_for_the_thread(self):
        rows = (NATIVE / "session_index.jsonl").read_text().splitlines()
        names = [
            json.loads(row)["thread_name"] for row in rows if json.loads(row)["id"] == SEGMENTED
        ]
        self.assertEqual(names, ["title-old", "title-168"])
        self.copy_store(NATIVE)
        with open(self.root / "session_index.jsonl", "a") as fh:
            fh.write(json.dumps({"id": SEGMENTED, "thread_name": "  renamed\n again  "}) + "\n")
            fh.write(json.dumps({"id": OTHER_ID, "thread_name": "someone else"}) + "\n")
            fh.write(json.dumps({"id": PLAIN, "thread_name": None}) + "\n")
            fh.write("not json\n")
        listed = self.parse_all()[0]
        self.assertEqual(
            (listed[SEGMENTED].title, listed[PLAIN].title), ("renamed again", "title-169")
        )

    def test_resume_argv(self):
        for root in (NATIVE, WINDOWS):
            for session in self.provider.list_sessions(store_for(root)):
                self.assertEqual(
                    self.provider.resume_argv(session), ["codex", "resume", session.id]
                )

    def test_import_needs_the_session_files(self):
        """The import itself: tests/test_importer.py."""
        from agent_sessions.providers.base import NotSupported

        store = Store("codex", "/nowhere/store", "foreign", "/nowhere", "x")
        session = Session.for_store(store, id="00000000-0000-4000-8000-000000000000")
        with self.assertRaises(NotSupported) as caught:
            self.provider.import_plan(session, None)
        self.assertIn("no rollout file", caught.exception.reason)


class SegmentTest(CodexCase):
    """A thread with a continuation segment (FORMATS §3 "Two-id files")."""

    def setUp(self):
        super().setUp()
        self.copy_store(NATIVE)

    def thread(self) -> Session:
        listed, _excluded, warnings = self.parse_all()
        self.assertEqual(warnings, [])
        return listed[SEGMENTED]

    def test_fixture_shape(self):
        base_rows = [json.loads(r) for r in (NATIVE / BASE_FILE).read_text().splitlines()]
        seg_rows = [json.loads(r) for r in (NATIVE / SEGMENT_FILE).read_text().splitlines()]
        head = seg_rows[0]["payload"]
        self.assertEqual(
            (head["id"], head["history_base"]["end_ordinal_exclusive"]), (SEGMENTED, 366)
        )
        messages = [
            (r["ordinal"], r["payload"]["item"]["type"])
            for r in base_rows + seg_rows
            if r["type"] == "event_msg"
            and r["payload"]["type"] == "item_completed"
            and r["payload"]["item"]["type"] in ("UserMessage", "AgentMessage")
        ]
        # Five message items on disk; the base file's ordinal 369 is superseded.
        self.assertEqual(
            messages,
            [
                (9, "UserMessage"),
                (10, "AgentMessage"),
                (369, "UserMessage"),
                (373, "UserMessage"),
                (376, "AgentMessage"),
            ],
        )
        self.assertEqual(self.thread().messages, 4)

    def test_count_drops_base_records_from_the_segment_start_on(self):
        def add(rows):
            rows.append(replied(365, "kept: before the segment start"))
            rows.append(replied(366, "dropped: at the segment start"))
            rows.append(said(400, "dropped: after it"))

        self.rewrite(BASE_FILE, add)
        session = self.thread()
        self.assertEqual(session.messages, 5)
        texts = [t.text for t in self.provider.transcript(session)]
        self.assertIn("kept: before the segment start", texts)
        self.assertFalse([t for t in texts if t.startswith("dropped")])

    def test_updated_is_the_last_timestamp_of_the_last_segment(self):
        def later(rows):
            rows.append(line("event_msg", 500, {"type": "token_count"}, "2027-01-01T00:00:00.000Z"))

        self.rewrite(BASE_FILE, later)
        session = self.thread()
        self.assertEqual(session.updated, parse_ts("2026-09-28T00:52:37.556Z"))
        # started, cwd and the first user message come from the base file.
        self.assertEqual(session.started, parse_ts("2026-09-27T23:34:58.749Z"))
        self.rewrite(
            SEGMENT_FILE,
            lambda rows: rows[0]["payload"].update(
                cwd="/segment/dir", timestamp="2026-09-28T00:52:25.821Z"
            ),
        )
        self.assertEqual(
            (self.thread().cwd, self.thread().started),
            ("/home/user", parse_ts("2026-09-27T23:34:58.749Z")),
        )
        (self.root / "session_index.jsonl").unlink()
        self.assertEqual(self.thread().title, "text-25")

    def test_files_are_in_thread_order_whatever_the_names_say(self):
        # Give the segment a name that sorts before the base file.
        early = (
            self.root
            / "sessions/2026/09/28"
            / (f"rollout-2026-09-28T01-00-00-{SEGMENTED}_{SEGMENT}.jsonl")
        )
        (self.root / SEGMENT_FILE).rename(early)
        session = self.thread()
        self.assertEqual(session.files, [str(self.root / BASE_FILE), str(early)])
        self.assertEqual(
            (session.messages, session.updated), (4, parse_ts("2026-09-28T00:52:37.556Z"))
        )

    def test_two_segments(self):
        self.write(
            [
                meta(),
                said(1, "one"),
                replied(2, "two"),
                said(3, "three"),
                replied(4, "superseded by the first segment"),
                said(9, "superseded as well"),
            ],
            stamp="2026-10-01T10-00-00",
        )
        self.write(
            [
                meta(4, history_base={"thread_id": NEW_ID, "end_ordinal_exclusive": 4}),
                replied(5, "four"),
                said(6, "five"),
                replied(7, "superseded by the second segment"),
            ],
            segment=SEGMENT,
            stamp="2026-10-01T11-00-00",
        )
        self.write(
            [
                meta(
                    7,
                    history_base={"thread_id": NEW_ID, "end_ordinal_exclusive": 7},
                    ts="2026-10-01T12:00:00.000Z",
                ),
                replied(8, "six", "2026-10-01T12:00:05.000Z"),
            ],
            segment=OTHER_ID,
            stamp="2026-10-01T12-00-00",
        )
        session = self.parse_all()[0][NEW_ID]
        self.assertEqual(
            (session.messages, session.updated, len(session.files)),
            (6, "2026-10-01T12:00:05.000000Z", 3),
        )
        self.assertEqual(
            [t.text for t in self.provider.transcript(session)],
            ["one", "two", "three", "four", "five", "six"],
        )

    def test_segment_without_its_base_file(self):
        (self.root / BASE_FILE).unlink()
        session = self.thread()
        self.assertEqual(
            (session.id, session.messages, session.files),
            (SEGMENTED, 2, [str(self.root / SEGMENT_FILE)]),
        )
        self.assertEqual(session.started, parse_ts("2026-09-28T00:52:25.821Z"))


class RulesTest(CodexCase):
    def test_missing_index_falls_back_to_the_first_user_message(self):
        self.write(
            [
                meta(),
                replied(1, "agent first"),
                said(2, "  the first\n user message " + "x" * 100),
                said(3, "second"),
            ]
        )
        self.assertFalse((self.root / "session_index.jsonl").exists())
        session = self.only()
        self.assertEqual(session.title, ("the first user message " + "x" * 100)[:80])
        self.assertEqual(session.messages, 3)

    def test_user_text_is_the_item_content_joined_by_a_space(self):
        record = said(1, "part one")
        record["payload"]["item"]["content"].append({"type": "text", "text": "part two"})
        record["payload"]["item"]["content"].append({"type": "image", "url": "x"})
        # response_item user messages are injected context, never the title.
        injected = line(
            "response_item",
            0,
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<environment_context>x</environment_context>"}
                ],
            },
        )
        self.write([meta(), injected, record])
        session = self.only()
        self.assertEqual((session.title, session.messages), ("part one part two", 1))

    def test_title_falls_back_to_the_id(self):
        self.write([meta(), replied(1, "only the agent spoke")])
        self.assertEqual(self.only().title, NEW_ID[:8])

    def test_id_comes_from_session_meta(self):
        self.write([meta(ident=OTHER_ID), said(1, "hello")])
        session = self.only()
        self.assertEqual(session.id, OTHER_ID)
        self.assertEqual(self.provider.resume_argv(session), ["codex", "resume", OTHER_ID])

    def test_file_of_another_thread_is_left_out(self):
        base_path = self.write([meta(), said(1, "mine"), replied(2, "yours")])
        stray = self.write(
            [meta(3, ident=OTHER_ID), said(4, "from another thread")],
            segment=SEGMENT,
            stamp="2026-10-01T11-00-00",
        )
        listed, excluded, warnings = self.parse_all()
        session = listed[NEW_ID]
        self.assertEqual((session.messages, session.files, excluded), (2, [str(base_path)], []))
        self.assertEqual(
            warnings, [f"{stray}: belongs to thread {OTHER_ID}, not {NEW_ID}; skipped"]
        )

    def test_fallbacks_without_session_meta(self):
        path = self.write(
            [
                line(
                    "turn_context",
                    5,
                    {"cwd": "/from/turn", "workspace_roots": []},
                    "2026-10-01T08:00:00.000Z",
                ),
                line("turn_context", 6, {"cwd": "/later/turn"}),
                said(7, "hi", "2026-10-01T08:00:01.000Z"),
            ]
        )
        session = self.only()
        self.assertEqual(
            (session.id, session.cwd, session.started, session.updated),
            (NEW_ID, "/from/turn", "2026-10-01T08:00:00.000000Z", "2026-10-01T08:00:01.000000Z"),
        )
        # Without any timestamp, updated is the file's mtime.
        record = said(1, "no clock")
        del record["timestamp"]
        path.write_text(json.dumps(record) + "\n")
        os.utime(path, (1790000000, 1790000000))
        session = self.only()
        self.assertEqual(
            (session.started, session.updated, session.cwd), (None, parse_ts(1790000000), None)
        )

    def test_cwd_falls_back_to_turn_context(self):
        head = meta()
        del head["payload"]["cwd"]
        self.write([head, line("turn_context", 1, {"cwd": "C:\\Work Dir"}), said(2, "x")])
        self.assertEqual(self.only().cwd, "C:\\Work Dir")

    def test_started_falls_back_to_the_first_line_timestamp(self):
        head = meta(ts="2026-10-01T07:00:00.123456789Z")
        del head["payload"]["timestamp"]
        self.write([head, said(1, "x", "2026-10-01T07:00:09.000Z")])
        session = self.only()
        self.assertEqual(
            (session.started, session.updated),
            ("2026-10-01T07:00:00.123456Z", "2026-10-01T07:00:09.000000Z"),
        )

    def test_subagent_threads_are_excluded(self):
        for change in (
            {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
            {"source": None},
            {"thread_source": "agent"},
            {"thread_source": None},
            {"agent_nickname": "scout"},
            {"agent_role": "worker"},
            {"source": "exec", "originator": "codex_exec"},
        ):
            with self.subTest(change=change):
                self.write([meta(**change), said(1, "task"), replied(2, "done")])
                self.assertEqual(self.parse_all(), ({}, [NEW_ID], []))

    def test_interactive_sources_are_listed(self):
        for change in ({"source": "cli"}, {"source": "vscode"}):
            with self.subTest(change=change):
                self.write([meta(**change), said(1, "task")])
                self.assertEqual(self.only().messages, 1)
        # A rollout that predates the keys is not taken for a sub-agent.
        head = meta()
        del head["payload"]["source"], head["payload"]["thread_source"]
        self.write([head, said(1, "task")])
        self.assertEqual(self.only().messages, 1)

    def test_empty_thread_is_excluded(self):
        self.write(
            [
                meta(),
                line("event_msg", 1, {"type": "task_started"}),
                line(
                    "event_msg",
                    2,
                    {"type": "item_completed", "item": {"type": "Reasoning", "id": "r"}},
                ),
                line(
                    "response_item",
                    3,
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "injected"}],
                    },
                ),
                line("event_msg", 4, {"type": "item_started", "item": {"type": "UserMessage"}}),
            ]
        )
        self.assertEqual(self.parse_all(), ({}, [NEW_ID], []))

    def test_compressed_rollout_is_skipped_with_a_warning(self):
        packed = self.write([], raw=b"\x28\xb5\x2f\xfd binary", suffix=".jsonl.zst")
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((listed, excluded), ({}, [NEW_ID]))
        self.assertEqual(
            warnings, [f"{packed}: compressed rollout (.jsonl.zst) is not supported; skipped"]
        )
        # Beside a readable file of the same thread the rest still lists.
        plain = self.write([meta(), said(1, "readable")], stamp="2026-10-01T09-00-00")
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((listed[NEW_ID].files, excluded, len(warnings)), ([str(plain)], [], 1))

    def test_unknown_and_malformed_records_are_ignored(self):
        records = [
            meta(),
            {"type": "event_msg"},
            {"type": "event_msg", "payload": None},
            {"type": "event_msg", "payload": {"type": "item_completed"}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": "x"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "UserMessage", "content": 5},
                },
            },
            {
                "type": "event_msg",
                "ordinal": "7",
                "timestamp": 5.5,
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [
                            5,
                            None,
                            {"type": "Text"},
                            {"type": "Text", "text": 9},
                            {"type": "Text", "text": "ok"},
                        ],
                    },
                },
            },
            {"type": "from-the-future", "payload": [1]},
            {"no": "type"},
            {"type": "turn_context", "payload": {"cwd": 5}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": 5}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "name": "x"}},
            {"type": "response_item", "payload": {"type": 5, "name": "x"}},
        ]
        self.write(records, raw=b"[1]\n7\n\n")
        session = self.only()
        self.assertEqual((session.title, session.messages), (NEW_ID[:8], 2))
        self.assertEqual(
            [t[:2] for t in self.provider.transcript(session)], [("user", ""), ("assistant", "ok")]
        )


class DamagedFileTest(CodexCase):
    def setUp(self):
        super().setUp()
        self.copy_store(NATIVE)

    def fields(self, session):
        return (session.title, session.cwd, session.started, session.updated, session.messages)

    def expected(self, ident):
        _root, _files, title, cwd, started, updated, messages = LISTED[ident]
        return (title, cwd, parse_ts(started), parse_ts(updated), messages)

    def test_half_written_last_line(self):
        with open(self.root / PLAIN_FILE, "ab") as fh:
            fh.write(
                b'{"timestamp":"2027-01-01T00:00:00.000Z","ordinal":300,"type":"event_msg",'
                b'"payload":{"type":"item_completed","item":{"type":"AgentMessage","cont'
            )
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((excluded, warnings), ([], []))
        self.assertEqual(self.fields(listed[PLAIN]), self.expected(PLAIN))

    def test_file_cut_in_the_middle_of_a_record(self):
        path = self.root / PLAIN_FILE
        rows = path.read_bytes().splitlines(keepends=True)
        self.assertEqual(json.loads(rows[6])["payload"]["item"]["type"], "AgentMessage")
        path.write_bytes(b"".join(rows[:6]) + rows[6][: len(rows[6]) // 2])
        session = self.parse_all()[0][PLAIN]
        self.assertEqual(
            (session.title, session.messages, session.updated),
            ("title-169", 1, parse_ts("2026-09-28T17:34:30.804Z")),
        )

    def test_non_utf8_line(self):
        title, cwd, started, _updated, messages = self.expected(PLAIN)
        with open(self.root / PLAIN_FILE, "ab") as fh:
            fh.write(
                b'{"timestamp":"2026-09-28T18:00:00.000Z","ordinal":300,"type":"event_msg",'
                b'"payload":{"type":"item_completed","item":{"type":"UserMessage",'
                b'"content":[{"type":"text","text":"caf\xe9 \xff ok"}]}}}\n'
            )
            fh.write(b"\xff\xfe\x00\x80 not json at all\n")
        with open(self.root / "session_index.jsonl", "ab") as fh:
            fh.write(b"\xff\xfe broken index line\n")
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((excluded, warnings), ([], []))
        self.assertEqual(
            self.fields(listed[PLAIN]),
            (title, cwd, started, parse_ts("2026-09-28T18:00:00.000Z"), messages + 1),
        )
        self.assertEqual(
            list(self.provider.transcript(listed[PLAIN]))[-1],
            Turn("user", "caf\ufffd \ufffd ok", "2026-09-28T18:00:00.000000Z"),
        )
        self.assertEqual(self.fields(listed[SEGMENTED]), self.expected(SEGMENTED))

    def test_non_utf8_thread_name(self):
        with open(self.root / "session_index.jsonl", "ab") as fh:
            fh.write(b'{"id":"' + PLAIN.encode() + b'","thread_name":"na\xefve name"}\n')
        self.assertEqual(self.parse_all()[0][PLAIN].title, "na\ufffdve name")

    def test_empty_file(self):
        (self.root / PLAIN_FILE).write_bytes(b"")
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((sorted(listed), excluded, warnings), ([SEGMENTED], [PLAIN], []))
        # An empty segment does not hide its thread.
        (self.root / SEGMENT_FILE).write_bytes(b"")
        session = self.parse_all()[0][SEGMENTED]
        self.assertEqual(
            (session.messages, session.files, session.updated),
            (3, [str(self.root / BASE_FILE)], parse_ts("2026-09-28T00:52:22.002Z")),
        )

    def test_missing_session_index(self):
        (self.root / "session_index.jsonl").unlink()
        listed, excluded, warnings = self.parse_all()
        self.assertEqual((excluded, warnings), ([], []))
        self.assertEqual((listed[SEGMENTED].title, listed[PLAIN].title), ("text-25", "text-152"))
        self.assertEqual(self.fields(listed[PLAIN])[1:], self.expected(PLAIN)[1:])

    def test_empty_session_index(self):
        (self.root / "session_index.jsonl").write_bytes(b"")
        self.assertEqual(self.parse_all()[0][PLAIN].title, "text-152")

    def test_unreadable_rollout_is_a_warning(self):
        candidate = Candidate(
            NEW_ID,
            [
                str(self.day / f"rollout-2026-10-01T10-00-00-{NEW_ID}.jsonl"),
                str(self.root / "session_index.jsonl"),
            ],
        )
        with collect_warnings() as collected:
            self.assertIsNone(self.provider.parse(self.store, candidate))
        self.assertEqual(len(collected.items), 1)
        self.assertIn("cannot read", collected.items[0])


class _Counting:
    """A binary file that counts how it is read."""

    def __init__(self, fh, log):
        self.fh, self.log = fh, log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()

    def readline(self, *args):
        return self.fh.readline(*args)

    def read(self, size=-1):
        self.log.append(size)
        return self.fh.read(size)


class StreamingTest(CodexCase):
    def run_counted(self, action):
        opened, reads = [], []
        real = base.open_ro

        def counting(path):
            opened.append(str(path))
            return _Counting(real(path), reads)

        with (
            mock.patch.object(base, "open_ro", counting),
            mock.patch("builtins.open", side_effect=AssertionError("plain open")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes")),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("read_text")),
        ):
            result = action()
        return result, opened, reads

    def test_parse_reads_every_file_once(self):
        store = store_for(NATIVE)
        candidate = self.provider.candidates(store)[0]
        session, opened, reads = self.run_counted(lambda: self.provider.parse(store, candidate))
        self.assertEqual(session.messages, 4)
        self.assertEqual(sorted(opened), sorted(candidate.paths))
        self.assertEqual(len(opened), 3)  # base file, segment, thread-name index
        self.assertEqual(reads, [])

    def test_missing_index_is_not_opened(self):
        store = store_for(WINDOWS)
        candidate = self.provider.candidates(store)[0]
        session, opened, reads = self.run_counted(lambda: self.provider.parse(store, candidate))
        self.assertEqual(session.title, "text-193")
        self.assertEqual((opened, reads), ([str(WINDOWS / WIN_FILE)], []))

    def test_transcript_reads_every_rollout_once(self):
        store = store_for(NATIVE)
        session = self.provider.parse(store, self.provider.candidates(store)[0])
        turns, opened, reads = self.run_counted(lambda: list(self.provider.transcript(session)))
        self.assertEqual(len(turns), 5)
        self.assertEqual((opened, reads), (session.files, []))


class TranscriptTest(CodexCase):
    def test_fixture_transcripts(self):
        sessions = {
            s.id: s
            for root in (NATIVE, WINDOWS)
            for s in self.provider.list_sessions(store_for(root))
        }
        # "text-146" was typed twice: the first attempt (ordinal 369, turn
        # aborted) is superseded by the segment and appears once.
        self.assertEqual(
            list(self.provider.transcript(sessions[SEGMENTED])),
            [
                Turn("user", "text-25", "2026-09-27T23:35:18.636000Z"),
                Turn("assistant", "text-26", "2026-09-27T23:35:21.396000Z"),
                Turn("tool", "exec", "2026-09-27T23:35:23.117000Z"),
                Turn("user", "text-146", "2026-09-28T00:52:28.383000Z"),
                Turn("assistant", "text-151", "2026-09-28T00:52:37.530000Z"),
            ],
        )
        self.assertEqual(
            list(self.provider.transcript(sessions[PLAIN])),
            [
                Turn("user", "text-152", "2026-09-28T17:34:30.804000Z"),
                Turn("assistant", "text-153", "2026-09-28T17:34:34.033000Z"),
                Turn("tool", "exec", "2026-09-28T17:35:18.435000Z"),
            ],
        )

    def test_windows_session(self):
        session = self.provider.list_sessions(store_for(WINDOWS))[0]
        self.assertEqual((session.origin, session.cwd), ("foreign", "F:\\Proj\\My Site"))
        self.assertEqual(
            list(self.provider.transcript(session)),
            [
                Turn("user", "text-193", "2026-08-29T08:31:15.313000Z"),
                Turn("assistant", "text-195", "2026-08-29T08:31:21.701000Z"),
            ],
        )

    def test_content_and_order(self):
        developer = line(
            "response_item",
            2,
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {"type": "input_text", "text": "<permissions>hidden rules</permissions>"}
                ],
            },
        )
        injected = line(
            "response_item",
            3,
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>hidden env</environment_context>",
                    }
                ],
            },
        )
        reasoning = line(
            "event_msg",
            6,
            {
                "type": "item_completed",
                "item": {"type": "Reasoning", "id": "r", "summary_text": ["hidden thought"]},
            },
        )
        output = line(
            "response_item",
            8,
            {
                "type": "custom_tool_call_output",
                "call_id": "c",
                "output": [{"type": "input_text", "text": "hidden out"}],
            },
        )
        command = line(
            "event_msg",
            9,
            {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "id": "e", "stdout": "hidden stdout"},
            },
        )
        answer = replied(11, "line one", "2026-10-01T10:00:09.000Z")
        answer["payload"]["item"]["content"].append({"type": "Text", "text": "line two"})
        self.write(
            [
                meta(),
                developer,
                injected,
                said(4, "do the thing", "2026-10-01T10:00:01.000Z"),
                reasoning,
                tool_call(7, "exec", "2026-10-01T10:00:03.000Z"),
                output,
                command,
                tool_call(10, "shell", "2026-10-01T10:00:04.000Z", kind="function_call"),
                answer,
                line(
                    "event_msg", 12, {"type": "task_complete", "last_agent_message": "hidden dup"}
                ),
            ]
        )
        session = self.only()
        self.assertEqual(session.messages, 2)
        turns = list(self.provider.transcript(session))
        self.assertEqual(
            turns,
            [
                Turn("user", "do the thing", "2026-10-01T10:00:01.000000Z"),
                Turn("tool", "exec", "2026-10-01T10:00:03.000000Z"),
                Turn("tool", "shell", "2026-10-01T10:00:04.000000Z"),
                Turn("assistant", "line one\nline two", "2026-10-01T10:00:09.000000Z"),
            ],
        )
        self.assertNotIn("hidden", " ".join(turn.text for turn in turns))
        self.assertNotIn("secret", " ".join(turn.text for turn in turns))

    def test_superseded_tool_calls_are_dropped(self):
        self.write(
            [
                meta(),
                said(1, "go"),
                tool_call(2, "kept"),
                tool_call(5, "dropped"),
                replied(6, "dropped reply"),
            ]
        )
        self.write(
            [
                meta(4, history_base={"thread_id": NEW_ID, "end_ordinal_exclusive": 4}),
                tool_call(5, "again"),
                replied(6, "final"),
            ],
            segment=SEGMENT,
            stamp="2026-10-01T11-00-00",
        )
        session = self.parse_all()[0][NEW_ID]
        self.assertEqual(
            [t[:2] for t in self.provider.transcript(session)],
            [("user", "go"), ("tool", "kept"), ("tool", "again"), ("assistant", "final")],
        )

    def test_session_without_files_or_with_a_missing_file(self):
        self.write([meta(), said(1, "x")])
        session = self.only()
        session.files = []
        self.assertEqual(list(self.provider.transcript(session)), [])
        session.files = [str(self.day / f"rollout-2026-10-01T10-00-00-{OTHER_ID}.jsonl")]
        with collect_warnings() as collected:
            self.assertEqual(list(self.provider.transcript(session)), [])
        self.assertEqual(len(collected.items), 1)


class IndexIntegrationTest(unittest.TestCase):
    def test_scan_lists_the_fixture_stores(self):
        box = Sandbox(self, {"codex": Codex})
        disk = box.mount("Win")
        shutil.copytree(NATIVE, box.home / ".codex")
        shutil.copytree(WINDOWS, disk / "Users" / "user" / ".codex")
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(
            (summary["stores"], summary["candidates"], summary["sessions"], summary["parsed"]),
            (2, 4, 3, 4),
        )
        self.assertEqual({s.id for s in api.list_sessions("codex")}, set(LISTED))
        again = api.scan(deep=True)
        self.assertEqual((again["parsed"], again["cached"]), (0, 4))
        # A rename appends to the index: every thread of that store is re-read.
        with open(box.home / ".codex" / "session_index.jsonl", "a") as fh:
            fh.write(json.dumps({"id": PLAIN, "thread_name": "renamed"}) + "\n")
        third = api.scan(deep=True)
        self.assertEqual((third["parsed"], third["cached"]), (2, 2))
        titles = {s.id: s.title for s in api.list_sessions("codex")}
        self.assertEqual((titles[PLAIN], titles[SEGMENTED]), ("renamed", "title-168"))
        # The index appearing where there was none counts as a change too.
        index = disk / "Users" / "user" / ".codex" / "session_index.jsonl"
        index.write_text(json.dumps({"id": WIN, "thread_name": "named later"}) + "\n")
        fourth = api.scan(deep=True)
        self.assertEqual((fourth["parsed"], fourth["cached"]), (2, 2))
        titles = {s.id: s.title for s in api.list_sessions("codex")}
        self.assertEqual(titles[WIN], "named later")
        self.assertTrue(api.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
