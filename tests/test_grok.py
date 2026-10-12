"""providers/grok.py against tests/fixtures and edge cases on temp copies.

Expected values: tests/fixtures/README.md. Rules: docs/FORMATS.md "Grok".
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

from support import FIXTURES, Sandbox

from agent_sessions import api
from agent_sessions.model import Candidate, Session, Store
from agent_sessions.providers import base
from agent_sessions.providers import grok as grok_mod
from agent_sessions.providers.base import Turn, collect_warnings, parse_ts
from agent_sessions.providers.grok import Grok

NATIVE = FIXTURES / "grok" / "native" / ".grok"
WINDOWS = FIXTURES / "grok" / "windows" / ".grok"

HOME_GROUP = "%2Fhome%2Fuser"
PROJ_GROUP = "%2Fhome%2Fuser%2Fproj"
WIN_GROUP = "F%3A%5CProj%5CMy%20Site"

TITLED = "01a106cb-e8dc-7e6e-a7d7-4559e30a19e1"  # prompt, reply, tool call, reply
PROMPT_ONLY = "01a10227-8547-7120-95ff-aa7696261403"
IMAGE = "01a0ec08-671b-72e4-b2fb-47b426cf36e6"
EMPTY = "01a0e500-fff7-7be6-bc91-b1036c11243e"
WIN_LISTED = "01a0a4c9-5206-7636-8fb7-5bef47aa8954"
WIN_EMPTY = "01a08073-6af7-7c7c-9dba-e3859e9a2ac8"

# id: (group dir, title, cwd, started, updated, messages)
LISTED = {
    NATIVE: {
        IMAGE: (
            PROJ_GROUP,
            "title-46",
            "/home/user/proj",
            "2026-09-29T07:19:28.817687314Z",
            "2026-09-29T07:39:14.310818030Z",
            3,
        ),
        PROMPT_ONLY: (
            HOME_GROUP,
            "text-42",
            "/home/user",
            "2026-10-03T14:25:06.901219758Z",
            "2026-10-03T14:25:09.153227016Z",
            1,
        ),
        TITLED: (
            HOME_GROUP,
            "title-1",
            "/home/user",
            "2026-10-04T12:03:09.161527561Z",
            "2026-10-04T12:03:39.305069470Z",
            3,
        ),
    },
    WINDOWS: {
        WIN_LISTED: (
            WIN_GROUP,
            "title-69",
            "F:\\Proj\\My Site",
            "2026-09-15T11:17:32.296109300Z",
            "2026-09-18T11:05:32.847759800Z",
            2,
        ),
    },
}
# Excluded: empty (no prompt, no assistant).
EXCLUDED = {NATIVE: {EMPTY: HOME_GROUP}, WINDOWS: {WIN_EMPTY: WIN_GROUP}}

SYSTEM = {"type": "system", "content": "sys"}
USER_INFO = {"type": "user", "content": [{"type": "text", "text": "<user_info>os</user_info>"}]}
REMINDER = {
    "type": "user",
    "synthetic_reason": "system_reminder",
    "content": [{"type": "text", "text": "<system-reminder>r</system-reminder>"}],
}


def prompt(text, index=0, **extra):
    return {
        "type": "user",
        "prompt_index": index,
        "content": [{"type": "text", "text": f"<user_query>\n{text}\n</user_query>"}],
        **extra,
    }


def reply(text, *tools):
    record = {"type": "assistant", "content": text, "model_id": "m"}
    if tools:
        record["tool_calls"] = [
            {"id": f"call-{i}", "name": name, "arguments": "{}"} for i, name in enumerate(tools)
        ]
    return record


class GrokCase(unittest.TestCase):
    def setUp(self):
        self.provider = Grok()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-grok-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def copy(self, source=NATIVE) -> Store:
        """A writable copy of a fixture store."""
        root = self.tmp / source.parent.name / ".grok"
        shutil.copytree(source, root)
        return Store("grok", str(root), origin="native")

    def new_store(self) -> Store:
        root = self.tmp / "new" / ".grok"
        (root / "sessions").mkdir(parents=True)
        return Store("grok", str(root), origin="native")

    def add(self, store, ident, records=None, group=HOME_GROUP, summary=None, **fields) -> Path:
        """Write one session directory; ``records=None`` writes no chat file."""
        directory = Path(store.root) / "sessions" / group / ident
        directory.mkdir(parents=True)
        if summary is None:
            summary = {
                "info": {"id": ident, "cwd": "/home/user"},
                "session_summary": "",
                "created_at": "2026-10-01T10:00:00.000000000Z",
                "updated_at": "2026-10-01T11:00:00.000000000Z",
                "num_messages": 7,
                "num_chat_messages": 9,
            }
            summary.update(fields)
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        if records is not None:
            (directory / "chat_history.jsonl").write_bytes(
                b"".join(json.dumps(r).encode() + b"\n" for r in records)
            )
        return directory

    def candidate(self, store, ident) -> Candidate:
        found = [c for c in self.provider.candidates(store) if c.id_hint == ident]
        self.assertEqual(len(found), 1, ident)
        return found[0]

    def parse(self, store, ident):
        """``(session, warnings)`` for one session id."""
        candidate = self.candidate(store, ident)
        with collect_warnings() as collected:
            session = self.provider.parse(store, candidate)
        return session, collected.items

    def listed(self, store, ident) -> Session:
        session, warnings = self.parse(store, ident)
        self.assertEqual(warnings, [])
        self.assertIsNotNone(session)
        return session

    def one(self, records, **fields) -> Session:
        store = self.new_store()
        self.add(store, TITLED, records, **fields)
        return self.listed(store, TITLED)


def fixture_store(root) -> Store:
    return Store("grok", str(root), origin="native" if root == NATIVE else "foreign")


class CandidatesTest(GrokCase):
    def test_fixture_candidates(self):
        for root, count in ((NATIVE, 4), (WINDOWS, 2)):
            with self.subTest(store=root.parent.name):
                store = fixture_store(root)
                found = self.provider.candidates(store)
                self.assertEqual(
                    sorted(c.id_hint for c in found), sorted([*LISTED[root], *EXCLUDED[root]])
                )
                self.assertEqual(len(found), count)
                groups = {**{i: v[0] for i, v in LISTED[root].items()}, **EXCLUDED[root]}
                for c in found:
                    directory = root / "sessions" / groups[c.id_hint] / c.id_hint
                    self.assertEqual(
                        c.paths,
                        [
                            str(directory / "summary.json"),
                            str(directory / "chat_history.jsonl"),
                            str(directory.parent / ".cwd"),
                        ],
                    )

    def test_counts_match_the_readme(self):
        for root, listed, count in ((NATIVE, 3, 4), (WINDOWS, 1, 2)):
            store = fixture_store(root)
            self.assertEqual(len(self.provider.candidates(store)), count)
            self.assertEqual(len(self.provider.list_sessions(store)), listed)

    def test_candidates_read_no_file(self):
        refuse = mock.Mock(side_effect=AssertionError("file opened"))
        with (
            mock.patch.object(grok_mod, "open_ro", refuse),
            mock.patch.object(base, "open_ro", refuse),
            mock.patch("builtins.open", refuse),
        ):
            self.assertEqual(len(self.provider.candidates(fixture_store(NATIVE))), 4)

    def test_only_directories_with_a_summary(self):
        store = self.copy()
        sessions = Path(store.root) / "sessions"
        (sessions / HOME_GROUP / "no-summary").mkdir()
        (sessions / HOME_GROUP / "no-summary" / "chat_history.jsonl").write_text("{}\n")
        (sessions / "stray-file").write_text("x")
        hints = [c.id_hint for c in self.provider.candidates(store)]
        self.assertEqual(len(hints), 4)
        # lock files, prompt_history.jsonl and session_search.sqlite are not directories
        self.assertNotIn("prompt_history.jsonl", hints)
        self.assertNotIn("no-summary", hints)

    def test_paths_are_listed_even_when_absent(self):
        store = self.new_store()
        directory = self.add(store, TITLED, None)
        (paths,) = [c.paths for c in self.provider.candidates(store)]
        self.assertIn(str(directory / "chat_history.jsonl"), paths)
        self.assertFalse((directory / "chat_history.jsonl").exists())

    def test_same_id_under_two_groups_keeps_hints_unique(self):
        store = self.new_store()
        self.add(store, TITLED, [prompt("a")], group=HOME_GROUP)
        self.add(store, TITLED, [prompt("a")], group=PROJ_GROUP)
        self.add(store, PROMPT_ONLY, [prompt("b")], group=HOME_GROUP)
        hints = sorted(c.id_hint for c in self.provider.candidates(store))
        self.assertEqual(
            hints, sorted([PROMPT_ONLY, f"{HOME_GROUP}/{TITLED}", f"{PROJ_GROUP}/{TITLED}"])
        )

    def test_store_without_groups(self):
        self.assertEqual(self.provider.candidates(self.new_store()), [])


class FixtureParseTest(GrokCase):
    def test_listed_sessions(self):
        for root, expected in LISTED.items():
            store = fixture_store(root)
            sessions = {s.id: s for s in self.provider.list_sessions(store)}
            self.assertEqual(sorted(sessions), sorted(expected))
            for ident, (group, title, cwd, started, updated, messages) in expected.items():
                with self.subTest(id=ident):
                    s = sessions[ident]
                    self.assertEqual((s.title, s.cwd, s.messages), (title, cwd, messages))
                    self.assertEqual(s.started, parse_ts(started))
                    self.assertEqual(s.updated, parse_ts(updated))
                    self.assertEqual(s.files, [str(root / "sessions" / group / ident)])
                    self.assertIsNone(s.note)
                    self.assertEqual(
                        (s.provider, s.store, s.origin), ("grok", str(root), store.origin)
                    )

    def test_empty_sessions_are_excluded_without_a_warning(self):
        for root, excluded in EXCLUDED.items():
            for ident in excluded:
                with self.subTest(id=ident):
                    session, warnings = self.parse(fixture_store(root), ident)
                    self.assertIsNone(session)
                    self.assertEqual(warnings, [])

    def test_prompt_without_a_reply_is_listed_and_titled_by_the_prompt(self):
        s = self.listed(fixture_store(NATIVE), PROMPT_ONLY)
        self.assertEqual((s.title, s.messages), ("text-42", 1))

    def test_windows_session(self):
        s = self.listed(fixture_store(WINDOWS), WIN_LISTED)
        self.assertEqual(s.cwd, "F:\\Proj\\My Site")
        self.assertEqual(s.origin, "foreign")
        self.assertEqual(s.started, "2026-09-15T11:17:32.296109Z")  # 7 fraction digits cut to 6

    def test_resume_argv(self):
        s = self.listed(fixture_store(NATIVE), TITLED)
        self.assertEqual(self.provider.resume_argv(s), ["grok", "--resume", TITLED])
        self.assertTrue(self.provider.resumable)

    def test_import_needs_the_session_files(self):
        """The import itself: tests/test_importer.py."""
        from agent_sessions.providers.base import NotSupported

        store = Store("grok", "/nowhere/store", "foreign", "/nowhere", "x")
        session = Session.for_store(store, id="00000000-0000-4000-8000-000000000000")
        with self.assertRaises(NotSupported) as caught:
            self.provider.import_plan(session, None)
        self.assertIn("no directory", caught.exception.reason)


class RulesTest(GrokCase):
    def test_messages_are_prompts_plus_assistant_records(self):
        s = self.one(
            [
                SYSTEM,
                USER_INFO,
                REMINDER,
                prompt("one"),
                {"type": "reasoning", "summary": []},
                reply("", "tool"),
                {"type": "tool_result", "tool_call_id": "call-0", "content": "out"},
                {
                    "type": "user",
                    "prompt_index": 1,
                    "synthetic_reason": "task_completed",
                    "content": [
                        {"type": "text", "text": "<system-reminder>done</system-reminder>"}
                    ],
                },
                reply("answer"),
                prompt("two", 2),
                reply("again"),
            ],
            num_chat_messages=99,
        )
        self.assertEqual(s.messages, 5)  # 2 prompts + 3 assistant records

    def test_only_injected_records_is_an_empty_session(self):
        store = self.new_store()
        self.add(
            store,
            TITLED,
            [
                SYSTEM,
                USER_INFO,
                REMINDER,
                {
                    "type": "user",
                    "prompt_index": 0,
                    "synthetic_reason": "task_completed",
                    "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}],
                },
            ],
        )
        self.assertEqual(self.parse(store, TITLED), (None, []))

    def test_assistant_without_a_prompt_is_listed(self):
        s = self.one([SYSTEM, reply("hello")])
        self.assertEqual((s.messages, s.title), (1, TITLED[:8]))

    def test_title_order(self):
        records = [prompt("the first prompt"), reply("ok")]
        self.assertEqual(
            self.one(records, session_summary="Summary", generated_title="Generated").title,
            "Summary",
        )
        self.setUp()
        self.assertEqual(self.one(records, generated_title="Generated").title, "Generated")
        self.setUp()
        self.assertEqual(
            self.one(records, session_summary="  ", generated_title=None).title, "the first prompt"
        )
        self.setUp()
        self.assertEqual(self.one([SYSTEM, reply("ok")]).title, TITLED[:8])

    def test_title_is_one_line_of_at_most_80_characters(self):
        s = self.one([prompt("line one\n  line two " + "x" * 100)])
        self.assertTrue(s.title.startswith("line one line two xxx"))
        self.assertEqual(len(s.title), 80)

    def test_user_query_is_unwrapped_wherever_it_sits(self):
        after_images = {
            "type": "user",
            "prompt_index": 0,
            "content": [
                {
                    "type": "text",
                    "text": "<image_files>\n/tmp/a.png\n</image_files>\n\n"
                    "<user_query>\nlook at this\n</user_query>",
                },
                {"type": "image", "url": "data:..."},
            ],
        }
        self.assertEqual(self.one([after_images]).title, "look at this")
        self.setUp()
        interrupted = {
            "type": "user",
            "prompt_index": 1,
            "prior_turn_interrupt": "mid_turn_abort",
            "content": [
                {
                    "type": "text",
                    "text": "The previous turn was interrupted.\n"
                    "<user_query>\ncarry on\n</user_query>\nnote",
                }
            ],
        }
        self.assertEqual(self.one([interrupted]).title, "carry on")
        self.setUp()
        bare = {"type": "user", "prompt_index": 0, "content": "no wrapper here"}
        self.assertEqual(self.one([bare]).title, "no wrapper here")

    def test_injected_records_never_give_the_title(self):
        s = self.one([SYSTEM, USER_INFO, REMINDER, prompt("real one"), reply("ok")])
        self.assertEqual(s.title, "real one")

    def test_id_and_cwd_fallbacks(self):
        store = self.new_store()
        group = "%2Fsrv%2Fmy%20proj"
        directory = self.add(
            store, TITLED, [prompt("a")], group=group, summary={"session_summary": "t"}
        )
        s = self.listed(store, TITLED)
        self.assertEqual((s.id, s.cwd), (TITLED, "/srv/my proj"))  # unquote(group dir)
        (directory.parent / ".cwd").write_text("/a/very/long/original/path\n")
        self.assertEqual(self.listed(store, TITLED).cwd, "/a/very/long/original/path")
        (directory / "summary.json").write_text(
            json.dumps({"info": {"id": "other-id", "cwd": "/recorded"}, "session_summary": "t"})
        )
        s = self.listed(store, TITLED)
        self.assertEqual((s.id, s.cwd), ("other-id", "/recorded"))

    def test_timestamp_fallbacks(self):
        store = self.new_store()
        directory = self.add(
            store,
            TITLED,
            [prompt("a")],
            summary={
                "info": {"id": TITLED, "cwd": "/x"},
                "last_active_at": "2026-10-02T08:00:00.1234567Z",
            },
        )
        os.utime(directory, (1_790_000_000, 1_790_000_000))
        s = self.listed(store, TITLED)
        self.assertEqual(s.started, parse_ts(1_790_000_000))  # directory mtime
        self.assertEqual(s.updated, "2026-10-02T08:00:00.123456Z")  # last_active_at
        (directory / "summary.json").write_text(json.dumps({"info": {"id": TITLED}}))
        os.utime(directory / "summary.json", (1_790_000_500, 1_790_000_500))
        self.assertEqual(self.listed(store, TITLED).updated, parse_ts(1_790_000_500))

    def test_parent_session_id_alone_stays_listed(self):
        s = self.one([prompt("a"), reply("b")], parent_session_id=PROMPT_ONLY, session_kind="fork")
        self.assertEqual(s.messages, 2)

    def test_unknown_and_malformed_records_are_ignored(self):
        s = self.one(
            [
                SYSTEM,
                {"type": "brand-new"},
                {"no": "type"},
                {"type": "user"},
                {"type": "user", "prompt_index": 0, "content": 5},
                {"type": "assistant", "content": None, "tool_calls": "nope"},
            ]
        )
        self.assertEqual(s.messages, 2)


class SubAgentTest(GrokCase):
    PARENT, CHILD, OTHER = TITLED, PROMPT_ONLY, IMAGE

    def store_with_child(self, meta, child_dir="agent-1") -> tuple[Store, Path]:
        store = self.new_store()
        parent = self.add(
            store, self.PARENT, [prompt("parent"), reply("spawned", "spawn_subagent")]
        )
        self.add(store, self.CHILD, [prompt("child task"), reply("done")])
        self.add(store, self.OTHER, [prompt("other"), reply("ok")], group=PROJ_GROUP)
        path = parent / "subagents" / child_dir / "meta.json"
        path.parent.mkdir(parents=True)
        if meta is not None:
            path.write_text(meta if isinstance(meta, str) else json.dumps(meta))
        return store, path

    def ids(self, store, order=None):
        candidates = sorted(
            self.provider.candidates(store), key=lambda c: c.id_hint, reverse=bool(order)
        )
        with collect_warnings() as collected:
            found = [self.provider.parse(store, c) for c in candidates]
        return sorted(s.id for s in found if s is not None), collected.items

    def test_child_named_inside_meta_json_is_excluded(self):
        store, _ = self.store_with_child(
            {
                "description": "research",
                "status": "completed",
                "child_session_id": self.CHILD,
                "parent_session_id": self.PARENT,
            }
        )
        self.assertEqual(self.ids(store), (sorted([self.PARENT, self.OTHER]), []))

    def test_child_named_by_the_subagents_directory_is_excluded(self):
        store, _ = self.store_with_child({"description": "research"}, child_dir=self.CHILD)
        self.assertEqual(self.ids(store), (sorted([self.PARENT, self.OTHER]), []))
        (
            Path(store.root)
            / "sessions"
            / HOME_GROUP
            / self.PARENT
            / "subagents"
            / self.CHILD
            / "meta.json"
        ).unlink()  # the directory alone names it
        self.assertEqual(self.ids(store), (sorted([self.PARENT, self.OTHER]), []))

    def test_exclusion_does_not_depend_on_candidate_order(self):
        store, _ = self.store_with_child({"session": {"id": self.CHILD}})
        self.assertEqual(self.ids(store), self.ids(store, order="reversed"))
        self.assertEqual(self.ids(store)[0], sorted([self.PARENT, self.OTHER]))
        self.assertEqual(len(self.provider.list_sessions(store)), 2)

    def test_meta_json_is_in_the_paths_of_every_other_candidate(self):
        store, path = self.store_with_child(None)  # directory there, file not written yet
        by_hint = {c.id_hint: c.paths for c in self.provider.candidates(store)}
        self.assertIn(str(path), by_hint[self.CHILD])
        self.assertIn(str(path), by_hint[self.OTHER])
        self.assertNotIn(str(path), by_hint[self.PARENT])
        self.assertEqual(self.ids(store), (sorted([self.PARENT, self.CHILD, self.OTHER]), []))
        path.write_text(json.dumps({"id": self.CHILD}))
        self.assertEqual(self.ids(store)[0], sorted([self.PARENT, self.OTHER]))

    def test_a_parent_is_never_excluded_by_a_parent_key(self):
        # The child's own subagents/ names its parent only as the parent.
        store, _ = self.store_with_child({"child_session_id": self.CHILD})
        child = Path(store.root) / "sessions" / HOME_GROUP / self.CHILD
        nested = child / "subagents" / "agent-2" / "meta.json"
        nested.parent.mkdir(parents=True)
        nested.write_text(
            json.dumps({"parent_session_id": self.PARENT, "parents": [self.PARENT], "note": "x"})
        )
        self.assertEqual(self.ids(store)[0], sorted([self.PARENT, self.OTHER]))

    def test_unreadable_meta_json_is_a_warning_not_an_exclusion(self):
        for bad in ("", "{not json"):
            with self.subTest(meta=bad):
                self.setUp()
                store, path = self.store_with_child(bad)
                ids, warnings = self.ids(store)
                self.assertEqual(ids, sorted([self.PARENT, self.CHILD, self.OTHER]))
                self.assertTrue(warnings)
                self.assertTrue(all(str(path) in w for w in warnings), warnings)


class DamagedStoreTest(GrokCase):
    def test_zero_byte_summary_is_a_warning(self):
        store = self.copy()
        path = Path(store.root) / "sessions" / HOME_GROUP / TITLED / "summary.json"
        path.write_bytes(b"")
        session, warnings = self.parse(store, TITLED)
        self.assertIsNone(session)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(path), warnings[0])
        self.assertIn("empty", warnings[0])
        self.assertEqual(len(self.provider.list_sessions(store)), 2)  # the others still list

    def test_unparseable_summary_is_a_warning(self):
        for data in (b'{"info": {"id": "x"', b"[1, 2]", b"\xff\xfe\x00garbage"):
            with self.subTest(data=data):
                self.setUp()
                store = self.copy()
                path = Path(store.root) / "sessions" / HOME_GROUP / TITLED / "summary.json"
                path.write_bytes(data)
                session, warnings = self.parse(store, TITLED)
                self.assertIsNone(session)
                self.assertEqual(len(warnings), 1)
                self.assertIn(str(path), warnings[0])

    def test_summary_that_vanished_is_a_warning(self):
        store = self.copy()
        candidate = self.candidate(store, TITLED)
        os.unlink(candidate.paths[0])
        with collect_warnings() as collected:
            self.assertIsNone(self.provider.parse(store, candidate))
        self.assertEqual(len(collected.items), 1)

    def test_missing_chat_history_of_a_titled_session(self):
        store = self.copy()
        directory = Path(store.root) / "sessions" / HOME_GROUP / TITLED
        (directory / "chat_history.jsonl").unlink()
        session, warnings = self.parse(store, TITLED)
        self.assertEqual((session.title, session.messages), ("title-1", 9))  # num_chat_messages
        self.assertEqual(len(warnings), 1)
        self.assertIn("chat_history.jsonl", warnings[0])

    def test_missing_chat_history_of_an_untitled_session(self):
        store = self.copy()
        directory = Path(store.root) / "sessions" / HOME_GROUP / PROMPT_ONLY
        (directory / "chat_history.jsonl").unlink()
        self.assertEqual(self.parse(store, PROMPT_ONLY), (None, []))

    def test_truncated_last_line(self):
        store = self.copy()
        chat = Path(store.root) / "sessions" / HOME_GROUP / TITLED / "chat_history.jsonl"
        with open(chat, "ab") as fh:
            fh.write(b'{"type":"assistant","content":"half writ')
        s = self.listed(store, TITLED)
        self.assertEqual((s.messages, s.title), (3, "title-1"))
        self.assertEqual(len(list(self.provider.transcript(s))), 4)

    def test_file_cut_in_the_middle_and_non_utf8(self):
        store = self.new_store()
        directory = self.add(store, TITLED, [prompt("a"), reply("b")])
        with open(directory / "chat_history.jsonl", "ab") as fh:
            fh.write(
                b'\xff\xfe{"type":"assistant"}\n\n{"type":"assistant","content":"caf\xe9"}\n[1]\n'
            )
        s = self.listed(store, TITLED)
        self.assertEqual(s.messages, 3)

    def test_empty_chat_history(self):
        store = self.new_store()
        self.add(store, TITLED, [], session_summary="titled")
        self.assertEqual(self.parse(store, TITLED), (None, []))

    def test_candidate_without_paths(self):
        store = self.new_store()
        self.assertIsNone(self.provider.parse(store, Candidate("x", [])))


class ReadsTest(GrokCase):
    def opened(self, action):
        seen, reads = [], []
        real = base.open_ro

        class Counting:
            def __init__(self, path):
                self.path, self.fh = str(path), real(path)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fh.close()

            def readline(self, *args):
                return self.fh.readline(*args)

            def read(self, size=-1):
                reads.append((os.path.basename(self.path), size))
                return self.fh.read(size)

        def counting(path):
            seen.append(os.path.basename(str(path)))
            return Counting(path)

        with (
            mock.patch.object(base, "open_ro", counting),
            mock.patch.object(grok_mod, "open_ro", counting),
            mock.patch("builtins.open", side_effect=AssertionError("plain open")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes")),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("read_text")),
        ):
            result = action()
        return result, seen, reads

    def test_parse_streams_the_chat_and_leaves_the_rest_alone(self):
        store = fixture_store(NATIVE)
        candidate = self.candidate(store, TITLED)
        session, seen, reads = self.opened(lambda: self.provider.parse(store, candidate))
        self.assertEqual(session.messages, 3)
        self.assertEqual(seen, ["summary.json", "chat_history.jsonl"])
        self.assertEqual([name for name, _ in reads], ["summary.json"])  # chat: readline only

    def test_listing_never_opens_updates_or_the_search_index(self):
        store = fixture_store(NATIVE)
        sessions, seen, _ = self.opened(lambda: self.provider.list_sessions(store))
        self.assertEqual(len(sessions), 3)
        self.assertEqual(set(seen), {"summary.json", "chat_history.jsonl"})

    def test_transcript_streams_too(self):
        s = self.listed(fixture_store(NATIVE), TITLED)
        turns, seen, reads = self.opened(lambda: list(self.provider.transcript(s)))
        self.assertEqual(len(turns), 4)
        self.assertEqual((seen, reads), (["chat_history.jsonl"], []))


class TranscriptTest(GrokCase):
    def test_fixture_transcripts(self):
        store = fixture_store(NATIVE)
        self.assertEqual(
            list(self.provider.transcript(self.listed(store, TITLED))),
            [
                Turn("user", "text-7", None),
                Turn("assistant", "text-10", None),
                Turn("tool", "name-11", None),
                Turn("assistant", "text-16", None),
            ],
        )
        self.assertEqual(
            list(self.provider.transcript(self.listed(store, PROMPT_ONLY))),
            [Turn("user", "text-42", None)],
        )
        # The sanitised image prompt lost its <user_query> wrapper: whole text.
        self.assertEqual(
            list(self.provider.transcript(self.listed(store, IMAGE))),
            [
                Turn("user", "<image_files>text-51</image_files>", None),
                Turn("assistant", "text-55", None),
                Turn("tool", "name-56", None),
                Turn("assistant", "text-62", None),
            ],
        )

    def test_windows_session(self):
        s = self.listed(fixture_store(WINDOWS), WIN_LISTED)
        self.assertEqual(
            list(self.provider.transcript(s)),
            [
                Turn("user", "text-75", None),
                Turn("assistant", "text-78", None),
                Turn("tool", "name-56", None),
                Turn("tool", "name-56", None),
                Turn("tool", "list_dir", None),
                Turn("tool", "name-82", None),
            ],
        )

    def test_content_and_order(self):
        s = self.one(
            [
                SYSTEM,
                USER_INFO,
                REMINDER,
                prompt("first question"),
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                    "encrypted_content": "zzz",
                },
                reply("", "read_file", "grep"),
                {"type": "tool_result", "tool_call_id": "call-0", "content": "secret output"},
                {
                    "type": "backend_tool_call",
                    "kind": {
                        "id": "b1",
                        "tool_type": "web_search",
                        "status": "completed",
                        "action": {"type": "search", "query": "q"},
                    },
                },
                {
                    "type": "user",
                    "prompt_index": 1,
                    "synthetic_reason": "task_completed",
                    "content": [
                        {"type": "text", "text": "<system-reminder>done</system-reminder>"}
                    ],
                },
                reply("the answer"),
                {
                    "type": "user",
                    "content": [{"type": "text", "text": "<system-reminder>r</system-reminder>"}],
                },
                prompt("second question", 2),
                reply("  \n"),
            ]
        )
        self.assertEqual(
            list(self.provider.transcript(s)),
            [
                Turn("user", "first question", None),
                Turn("tool", "read_file", None),
                Turn("tool", "grep", None),
                Turn("tool", "web_search", None),
                Turn("assistant", "the answer", None),
                Turn("user", "second question", None),
            ],
        )

    def test_prompt_kept_by_compaction_is_shown(self):
        kept = {
            "type": "user",
            "content": [{"type": "text", "text": "<user_query>\nthe original ask\n</user_query>"}],
        }
        meta = {
            "type": "user",
            "synthetic_reason": "compaction_meta",
            "content": [{"type": "text", "text": "<user_query>\nnot shown\n</user_query>"}],
        }
        s = self.one([SYSTEM, meta, kept, reply("after compaction")])
        self.assertEqual(s.messages, 1)  # only the assistant record counts
        self.assertEqual(
            list(self.provider.transcript(s)),
            [Turn("user", "the original ask", None), Turn("assistant", "after compaction", None)],
        )

    def test_session_without_files_or_with_a_missing_chat(self):
        s = self.listed(fixture_store(NATIVE), TITLED)
        s.files = []
        self.assertEqual(list(self.provider.transcript(s)), [])
        s.files = [str(self.tmp / "gone")]
        self.assertEqual(list(self.provider.transcript(s)), [])


class IndexIntegrationTest(unittest.TestCase):
    def test_scan_lists_the_fixture_stores(self):
        box = Sandbox(self, {"grok": Grok})
        disk = box.mount("Win")
        shutil.copytree(NATIVE, box.home / ".grok")
        shutil.copytree(WINDOWS, disk / "Users" / "user" / ".grok")
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(
            (summary["stores"], summary["candidates"], summary["sessions"], summary["parsed"]),
            (2, 6, 4, 6),
        )
        sessions = api.list_sessions("grok")
        self.assertEqual({s.id for s in sessions}, set(LISTED[NATIVE]) | set(LISTED[WINDOWS]))
        self.assertEqual({s.origin for s in sessions if s.id in LISTED[WINDOWS]}, {"foreign"})
        again = api.scan(deep=True)
        self.assertEqual((again["parsed"], again["cached"], again["sessions"]), (0, 6, 4))
        self.assertTrue(api.verify()["ok"])

    def test_a_grown_chat_or_a_new_meta_json_invalidates_the_cache(self):
        box = Sandbox(self, {"grok": Grok})
        shutil.copytree(NATIVE, box.home / ".grok")
        self.assertEqual(api.scan(deep=False)["sessions"], 3)
        directory = box.home / ".grok" / "sessions" / HOME_GROUP
        with open(directory / PROMPT_ONLY / "chat_history.jsonl", "ab") as fh:
            fh.write(json.dumps(reply("late answer")).encode() + b"\n")
        again = api.scan(deep=False)
        self.assertEqual((again["parsed"], again["cached"]), (1, 3))
        by_id = {s.id: s for s in api.list_sessions("grok")}
        self.assertEqual(by_id[PROMPT_ONLY].messages, 2)
        meta = directory / TITLED / "subagents" / "a1" / "meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"child_session_id": PROMPT_ONLY}))
        third = api.scan(deep=False)
        self.assertEqual(third["sessions"], 2)
        self.assertNotIn(PROMPT_ONLY, {s.id for s in api.list_sessions("grok")})


if __name__ == "__main__":
    unittest.main()
