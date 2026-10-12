"""providers/gemini.py against tests/fixtures and edge cases on temp copies.

Expected values: tests/fixtures/README.md. Rules: docs/FORMATS.md "Gemini CLI".
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
from agent_sessions.providers import gemini as gemini_mod
from agent_sessions.providers.base import Turn, collect_warnings, parse_ts
from agent_sessions.providers.gemini import Gemini, apply_records

WINDOWS = FIXTURES / "gemini" / "windows" / ".gemini"
NOT_A_STORE = FIXTURES / "gemini" / "native-no-chats" / ".gemini"

CONTEXT_ONLY = "session-2026-08-29T09-07-a92c6b68.jsonl"  # excluded: no resumable content
SYNTHETIC = "session-2026-08-30T10-00-5b0c3e6a.jsonl"
SYNTHETIC_ID = "5b0c3e6a-1f2d-4c7b-9a8e-0d1c2b3a4f5e"
SID = "0c7e5f1a-1111-4222-8333-444455556666"
OTHER_SID = "9b8a7c6d-1111-4222-8333-444455556666"


def header(session_id=SID, **extra):
    record = {
        "sessionId": session_id,
        "projectHash": "ab" * 32,
        "startTime": "2026-09-01T10:00:00.000Z",
        "lastUpdated": "2026-09-01T10:00:00.000Z",
        "kind": "main",
    }
    record.update(extra)
    return {k: v for k, v in record.items() if v is not None}


def user(ident, text, second=1):
    return {
        "id": ident,
        "timestamp": f"2026-09-01T10:00:{second:02d}.000Z",
        "type": "user",
        "content": [{"text": text}],
    }


def model(ident, text, second=2, **extra):
    return {
        "id": ident,
        "timestamp": f"2026-09-01T10:00:{second:02d}.000Z",
        "type": "gemini",
        "content": text,
        **extra,
    }


def chat_name(session_id=SID, stamp="2026-09-01T10-00", ext="jsonl") -> str:
    return f"session-{stamp}-{session_id[:8]}.{ext}"


class GeminiCase(unittest.TestCase):
    def setUp(self):
        self.provider = Gemini()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-gemini-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / ".gemini"
        self.chats = self.root / "tmp" / "my-proj" / "chats"
        self.chats.mkdir(parents=True)
        (self.root / "tmp" / "my-proj" / ".project_root").write_bytes(b"/home/user/my proj")
        (self.root / "projects.json").write_text(
            json.dumps({"projects": {"/home/user/my proj": "my-proj"}})
        )
        self.store = Store("gemini", str(self.root), origin="native")

    def write(self, records, name=None, raw=b"") -> Path:
        """Write a chat file into the temp store; ``raw`` is appended as is."""
        path = self.chats / (name or chat_name())
        path.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records) + raw)
        return path

    def candidate(self, path, store=None) -> Candidate:
        store = store or self.store
        hint = os.path.relpath(path, store.root).replace(os.sep, "/")
        found = [c for c in self.provider.candidates(store) if c.id_hint == hint]
        self.assertEqual(len(found), 1, hint)
        return found[0]

    def parse(self, path, store=None):
        """``(session, warnings)`` for one chat file."""
        candidate = self.candidate(path, store)
        with collect_warnings() as collected:
            session = self.provider.parse(store or self.store, candidate)
        return session, collected.items

    def listed(self, records, **kwargs) -> Session:
        session, warnings = self.parse(self.write(records, **kwargs))
        self.assertEqual(warnings, [])
        self.assertIsNotNone(session)
        return session

    def excluded(self, records, **kwargs):
        self.assertEqual(self.parse(self.write(records, **kwargs)), (None, []))

    def texts(self, session):
        return [(t.role, t.text) for t in self.provider.transcript(session)]


FIXTURE_STORE = Store("gemini", str(WINDOWS), origin="foreign")


class CandidatesTest(GeminiCase):
    def test_fixture_candidates(self):
        found = self.provider.candidates(FIXTURE_STORE)
        self.assertEqual(
            [c.id_hint for c in found],
            [f"tmp/my-site/chats/{CONTEXT_ONLY}", f"tmp/my-site/chats/{SYNTHETIC}"],
        )
        for c, name in zip(found, (CONTEXT_ONLY, SYNTHETIC), strict=False):
            self.assertEqual(
                c.paths,
                [
                    str(WINDOWS / "tmp" / "my-site" / "chats" / name),
                    str(WINDOWS / "tmp" / "my-site" / ".project_root"),
                    str(WINDOWS / "projects.json"),
                ],
            )

    def test_a_chat_files_name_is_matched_whole(self):
        """A name with a line break in it or after it is not a chat file's
        name (with ``match`` and ``$`` the second passed)."""
        good = self.write([], name="session-2026-08-30T10-00-abcd1234.jsonl")
        for name in (
            "session-2026-08-30T10-00-abcd1234.jsonl\n",
            "session-2026-08-30T10-00-abcd1234.json\n",
            "session-2026\n-08-30T10-00-abcd1234.jsonl",
            "xsession-2026-08-30T10-00-abcd1234.jsonl",
            "session-2026-08-30T10-00-abcd1234.jsonlx",
        ):
            self.write([], name=name)
        hints = [c.id_hint for c in self.provider.candidates(self.store)]
        self.assertEqual(hints, [f"tmp/my-proj/chats/{good.name}"])
        self.assertEqual(gemini_mod._id_group(good.name), "abcd1234")
        self.assertEqual(gemini_mod._id_group(good.name + "\n"), good.name + "\n")

    def test_counts_match_the_readme(self):
        self.assertEqual(len(self.provider.candidates(FIXTURE_STORE)), 2)
        self.assertEqual(len(self.provider.list_sessions(FIXTURE_STORE)), 1)

    def test_candidates_read_no_file(self):
        refuse = mock.Mock(side_effect=AssertionError("file opened"))
        with (
            mock.patch.object(gemini_mod, "open_ro", refuse),
            mock.patch.object(base, "open_ro", refuse),
            mock.patch("builtins.open", refuse),
        ):
            self.assertEqual(len(self.provider.candidates(FIXTURE_STORE)), 2)

    def test_only_session_files_directly_in_chats(self):
        self.write([header(), user("m1", "hi")])
        self.write([header()], name="session-old.json")
        (self.chats / "notes.jsonl").write_text("{}\n")
        (self.chats / "session-x.txt").write_text("x")
        sub = self.chats / SID  # sub-agent chats of that session
        sub.mkdir()
        (sub / f"{OTHER_SID}.jsonl").write_text(json.dumps(header(OTHER_SID, kind="subagent")))
        (self.root / "tmp" / "no-chats").mkdir()
        (self.root / "tmp" / "stray.jsonl").write_text("{}")
        self.assertEqual(
            [c.id_hint for c in self.provider.candidates(self.store)],
            [f"tmp/my-proj/chats/{chat_name()}", "tmp/my-proj/chats/session-old.json"],
        )

    def test_paths_are_listed_even_when_absent(self):
        path = self.write([header(), user("m1", "hi")])
        os.unlink(self.root / "projects.json")
        os.unlink(self.root / "tmp" / "my-proj" / ".project_root")
        self.assertEqual(
            self.candidate(path).paths,
            [
                str(path),
                str(self.root / "tmp" / "my-proj" / ".project_root"),
                str(self.root / "projects.json"),
            ],
        )

    def test_files_of_one_session_are_in_each_others_paths(self):
        a = self.write([header()], name=chat_name(stamp="2026-09-01T10-00"))
        b = self.write([header()], name=chat_name(stamp="2026-09-02T08-30"))
        c = self.write([header(OTHER_SID)], name=chat_name(OTHER_SID))
        self.assertEqual(self.candidate(a).paths[3:], [str(b)])
        self.assertEqual(self.candidate(b).paths[3:], [str(a)])
        self.assertEqual(self.candidate(c).paths[3:], [])


class FixtureParseTest(GeminiCase):
    def test_listed_session(self):
        (s,) = self.provider.list_sessions(FIXTURE_STORE)
        self.assertEqual(
            (s.id, s.title, s.cwd, s.messages),
            (SYNTHETIC_ID, "title-synthetic", "f:\\proj\\my site", 3),
        )
        self.assertEqual(s.started, parse_ts("2026-08-30T10:00:00.000Z"))
        self.assertEqual(s.updated, parse_ts("2026-08-30T10:00:10.000Z"))
        self.assertEqual(s.files, [str(WINDOWS / "tmp" / "my-site" / "chats" / SYNTHETIC)])
        self.assertIsNone(s.note)
        self.assertEqual((s.provider, s.store, s.origin), ("gemini", str(WINDOWS), "foreign"))

    def test_injected_context_only_is_excluded_without_a_warning(self):
        path = WINDOWS / "tmp" / "my-site" / "chats" / CONTEXT_ONLY
        self.assertEqual(self.parse(path, FIXTURE_STORE), (None, []))

    def test_fixture_transcript(self):
        (s,) = self.provider.list_sessions(FIXTURE_STORE)
        self.assertEqual(
            list(self.provider.transcript(s)),
            [
                Turn("user", "text-prompt-1", "2026-08-30T10:00:02.000000Z"),
                Turn("assistant", "text-reply-1", "2026-08-30T10:00:05.000000Z"),
            ],
        )

    def test_resume_argv_and_not_resumable(self):
        (s,) = self.provider.list_sessions(FIXTURE_STORE)
        self.assertEqual(self.provider.resume_argv(s), ["gemini", "--resume", SYNTHETIC_ID])
        # "The real CLI is installed" (tests/test_tools.py has the cases):
        # with no gemini command at all it is not.
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(self.provider.resumable)

    def test_a_gemini_dir_without_tmp_is_not_a_store(self):
        self.assertFalse(self.provider.is_store(NOT_A_STORE))

    def test_import_is_refused_with_the_documented_reason(self):
        """FORMATS section 6. More in tests/test_importer.py."""
        from agent_sessions.providers import gemini as module
        from agent_sessions.providers.base import NotSupported

        for call in (self.provider.import_plan, self.provider.import_session):
            with self.assertRaises(NotSupported) as caught:
                call(None, None)
            self.assertEqual(caught.exception.reason, module.IMPORT_UNSAFE)


class RecordModelTest(GeminiCase):
    def test_apply_records(self):
        meta, messages = apply_records(
            [
                header(),
                user("m1", "one"),
                model("m2", "two"),
                user("m3", "three"),
                {"$rewindTo": "m3"},
                user("m4", "four"),
                {"$set": {"summary": "S", "lastUpdated": "2026-09-01T11:00:00.000Z"}},
                "not a record",
                {"unknown": 1},
                {"id": 5},
            ]
        )
        self.assertEqual([m["id"] for m in messages], ["m1", "m2", "m4"])
        self.assertEqual(
            (meta["sessionId"], meta["summary"], meta["lastUpdated"], meta["kind"]),
            (SID, "S", "2026-09-01T11:00:00.000Z", "main"),
        )

    def test_set_with_messages_replaces_all_messages(self):
        s = self.listed(
            [
                header(),
                user("m1", "dropped"),
                model("m2", "dropped too"),
                {
                    "$set": {
                        "messages": [
                            user("n1", "kept question"),
                            model("n2", "kept answer"),
                            {"no": "id"},
                            "junk",
                        ],
                        "lastUpdated": "2026-09-01T12:00:00.000Z",
                    }
                },
                user("n3", "after the set", 9),
            ]
        )
        self.assertEqual((s.messages, s.title), (3, "kept question"))
        self.assertEqual(s.updated, parse_ts("2026-09-01T12:00:00.000Z"))
        self.assertEqual(
            self.texts(s),
            [("user", "kept question"), ("assistant", "kept answer"), ("user", "after the set")],
        )

    def test_set_merges_metadata(self):
        s = self.listed(
            [
                header(),
                user("m1", "question"),
                {"$set": {"summary": "The summary"}},
                {"$set": {"startTime": "2026-08-31T00:00:00.000Z"}},
                {"$set": {"summary": "Renamed", "memoryScratchpad": {"a": 1}}},
                {"$set": "not an object"},
            ]
        )
        self.assertEqual((s.title, s.messages), ("Renamed", 1))
        self.assertEqual(s.started, parse_ts("2026-08-31T00:00:00.000Z"))
        self.assertEqual(s.updated, parse_ts("2026-09-01T10:00:00.000Z"))

    def test_rewind_to_a_known_id_drops_it_and_everything_after(self):
        s = self.listed(
            [
                header(),
                user("m1", "first"),
                model("m2", "reply"),
                user("m3", "regretted"),
                model("m4", "also gone"),
                {"$rewindTo": "m3"},
            ]
        )
        self.assertEqual(s.messages, 2)
        self.assertEqual(self.texts(s), [("user", "first"), ("assistant", "reply")])

    def test_rewind_to_an_unknown_id_drops_everything(self):
        records = [header(), user("m1", "first"), model("m2", "reply"), {"$rewindTo": "nope"}]
        self.excluded(records)
        s = self.listed(records + [user("m5", "fresh start", 30)])
        self.assertEqual((s.messages, s.title), (1, "fresh start"))
        self.assertEqual(self.texts(s), [("user", "fresh start")])

    def test_a_message_with_a_known_id_replaces_it_in_place(self):
        s = self.listed(
            [
                header(),
                user("m1", "question"),
                model("m2", "draft"),
                user("m3", "next"),
                model("m2", "final answer"),
            ]
        )
        self.assertEqual(s.messages, 3)
        self.assertEqual(
            self.texts(s), [("user", "question"), ("assistant", "final answer"), ("user", "next")]
        )

    def test_messages_inside_a_metadata_record_are_added(self):
        s = self.listed(
            [user("m0", "before the header"), header(messages=[user("m1", "inside"), {"id": 7}])]
        )
        self.assertEqual(s.messages, 2)
        self.assertEqual(self.texts(s), [("user", "before the header"), ("user", "inside")])


class ListingRuleTest(GeminiCase):
    def test_no_resumable_content(self):
        for label, messages in (
            ("no messages", []),
            ("injected context", [user("m1", "<session_context>cwd</session_context>")]),
            ("hook context", [user("m1", "  <hook_context>x</hook_context>")]),
            ("slash command", [user("m1", "/help")]),
            ("question command", [user("m1", "?")]),
            ("blank", [user("m1", "   \n")]),
            ("model said nothing", [model("m1", "  ")]),
            (
                "info and errors",
                [
                    {"id": "m1", "type": "info", "content": "Welcome"},
                    {"id": "m2", "type": "error", "content": "Boom"},
                    {"id": "m3", "type": "warning", "content": "Hm"},
                ],
            ),
            ("unknown type", [{"id": "m1", "type": "system", "content": "text"}]),
        ):
            with self.subTest(case=label):
                self.excluded([header(), *messages])

    def test_resumable_content(self):
        for label, messages, count in (
            ("a prompt", [user("m1", "real question")], 1),
            ("prompt after commands", [user("m1", "/model"), user("m2", "hello")], 2),
            ("string content", [{"id": "m1", "type": "user", "content": "plain"}], 1),
            ("model text", [model("m1", "an answer")], 1),
            ("model tool calls only", [model("m1", "", toolCalls=[{"name": "read_file"}])], 1),
            ("model thoughts only", [model("m1", "", thoughts=[{"subject": "s"}])], 1),
            (
                "image part only",
                [
                    {
                        "id": "m1",
                        "type": "user",
                        "content": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}],
                    }
                ],
                1,
            ),
        ):
            with self.subTest(case=label):
                self.assertEqual(self.listed([header(), *messages]).messages, count)

    def test_sub_agent_kind_is_excluded(self):
        self.excluded([header(kind="subagent"), user("m1", "delegated task"), model("m2", "done")])

    def test_kind_can_be_set_later(self):
        self.excluded([header(), user("m1", "task"), {"$set": {"kind": "subagent"}}])

    def test_title_fallbacks(self):
        s = self.listed(
            [
                header(),
                user("m1", "/clear"),
                user("m2", "<session_context>x"),
                user("m3", "the  real\nprompt " + "x" * 100),
                model("m4", "ok"),
            ]
        )
        self.assertTrue(s.title.startswith("the real prompt xxx"))
        self.assertEqual(len(s.title), 80)
        self.assertEqual(self.listed([header(), model("m1", "only the model")]).title, SID[:8])
        self.assertEqual(self.listed([header(summary=""), user("m1", "ask")]).title, "ask")
        self.assertEqual(self.listed([header(summary="Named"), user("m1", "ask")]).title, "Named")

    def test_timestamp_fallbacks(self):
        s = self.listed([header(startTime=None), user("m1", "q")])
        self.assertEqual((s.started, s.updated), (parse_ts("2026-09-01T10:00:00.000Z"),) * 2)
        s = self.listed(
            [header(lastUpdated=None, startTime="2026-09-01T09:00:00.000Z"), user("m1", "q")]
        )
        self.assertEqual((s.started, s.updated), (parse_ts("2026-09-01T09:00:00.000Z"),) * 2)
        path = self.write([header(startTime=None, lastUpdated=None), user("m1", "q")])
        os.utime(path, (1_790_000_000, 1_790_000_000))
        session, _ = self.parse(path)
        self.assertEqual((session.started, session.updated), (parse_ts(1_790_000_000),) * 2)

    def test_cwd_sources(self):
        path = self.write([header(), user("m1", "q")])
        self.assertEqual(self.parse(path)[0].cwd, "/home/user/my proj")
        marker = self.root / "tmp" / "my-proj" / ".project_root"
        marker.write_bytes(b"f:\\ai projects\\website works")  # lower-cased on Windows
        self.assertEqual(self.parse(path)[0].cwd, "f:\\ai projects\\website works")
        marker.unlink()
        (self.root / "projects.json").write_text(
            json.dumps(
                {"projects": {"/z/later": "my-proj", "/a/first": "my-proj", "/other": "elsewhere"}}
            )
        )
        self.assertEqual(self.parse(path)[0].cwd, "/a/first")
        (self.root / "projects.json").write_text(json.dumps({"projects": {"/other": "elsewhere"}}))
        session, warnings = self.parse(path)
        self.assertEqual((session.cwd, warnings), (None, []))
        (self.root / "projects.json").write_text("{broken")
        session, warnings = self.parse(path)
        self.assertIsNone(session.cwd)
        self.assertEqual(len(warnings), 1)
        self.assertIn("projects.json", warnings[0])
        (self.root / "projects.json").unlink()
        self.assertEqual(self.parse(path)[1], [])
        self.assertIsNone(self.parse(path)[0].cwd)


class DuplicateSessionTest(GeminiCase):
    def pair(self, older_updated, newer_updated):
        first = self.write(
            [header(lastUpdated=older_updated), user("m1", "from the first file")],
            name=chat_name(stamp="2026-09-01T10-00"),
        )
        second = self.write(
            [
                header(lastUpdated=newer_updated),
                user("m1", "from the second file"),
                model("m2", "more"),
            ],
            name=chat_name(stamp="2026-09-03T08-00"),
        )
        return first, second

    def ids(self, reverse=False):
        candidates = sorted(
            self.provider.candidates(self.store), key=lambda c: c.id_hint, reverse=reverse
        )
        found = [self.provider.parse(self.store, c) for c in candidates]
        return sorted((s.id, s.title) for s in found if s is not None)

    def test_newest_last_updated_wins(self):
        first, second = self.pair("2026-09-01T10:00:00.000Z", "2026-09-03T08:00:00.000Z")
        self.assertIsNone(self.parse(first)[0])
        s = self.parse(second)[0]
        self.assertEqual(
            (s.id, s.title, s.messages, s.files), (SID, "from the second file", 2, [str(second)])
        )
        self.assertEqual(self.ids(), [(SID, "from the second file")])
        self.assertEqual(self.ids(), self.ids(reverse=True))

    def test_the_file_name_does_not_decide(self):
        first, second = self.pair("2026-09-05T00:00:00.000Z", "2026-09-03T08:00:00.000Z")
        self.assertEqual(self.parse(first)[0].title, "from the first file")
        self.assertIsNone(self.parse(second)[0])
        self.assertEqual(self.ids(), self.ids(reverse=True))

    def test_a_tie_goes_to_the_first_file_name(self):
        self.pair("2026-09-03T08:00:00.000Z", "2026-09-03T08:00:00Z")
        self.assertEqual(self.ids(), [(SID, "from the first file")])
        self.assertEqual(self.ids(), self.ids(reverse=True))

    def test_a_file_that_is_not_listed_shadows_nothing(self):
        first = self.write(
            [header(), user("m1", "the conversation")], name=chat_name(stamp="2026-09-01T10-00")
        )
        self.write(
            [header(lastUpdated="2026-09-09T00:00:00.000Z"), user("m1", "/quit")],
            name=chat_name(stamp="2026-09-09T00-00"),
        )
        lookalike = SID[:8] + "-9999-4999-8999-999999999999"  # same first 8, another session
        self.write(
            [
                header(lookalike, lastUpdated="2026-09-09T00:00:00.000Z"),
                user("m1", "another session"),
            ],
            name=chat_name(stamp="2026-09-08T00-00"),
        )
        self.assertEqual(self.parse(first)[0].title, "the conversation")
        self.assertEqual(len(self.ids()), 2)

    def test_legacy_json_next_to_its_jsonl_conversion(self):
        legacy = self.chats / chat_name(ext="json")
        legacy.write_text(
            json.dumps({**header(), "messages": [user("m1", "old format")]}, indent=2)
        )
        converted = self.write(
            [
                header(lastUpdated="2026-09-02T00:00:00.000Z"),
                user("m1", "old format"),
                model("m2", "continued"),
            ]
        )
        self.assertIsNone(self.parse(legacy)[0])
        self.assertEqual(self.parse(converted)[0].messages, 2)

    def test_through_the_index(self):
        box = Sandbox(self, {"gemini": Gemini})
        chats = box.home / ".gemini" / "tmp" / "p" / "chats"
        chats.mkdir(parents=True)
        self.chats = chats
        self.pair("2026-09-01T10:00:00.000Z", "2026-09-03T08:00:00.000Z")
        summary = api.scan(deep=False)
        self.assertEqual(
            (summary["candidates"], summary["sessions"], summary["warnings"]), (2, 1, [])
        )
        (s,) = api.list_sessions("gemini")
        self.assertEqual((s.title, s.cwd, s.launchable), ("from the second file", None, False))
        # the older file grows past the newer one: both are re-read, the winner flips
        with open(chats / chat_name(stamp="2026-09-01T10-00"), "ab") as fh:
            fh.write(
                json.dumps({"$set": {"lastUpdated": "2026-09-04T00:00:00.000Z"}}).encode() + b"\n"
            )
        again = api.scan(deep=False)
        self.assertEqual((again["parsed"], again["sessions"]), (2, 1))
        self.assertEqual(api.list_sessions("gemini")[0].title, "from the first file")


class LegacyAndDamagedTest(GeminiCase):
    def test_legacy_json_file_is_read(self):
        path = self.chats / chat_name(ext="json")
        path.write_text(
            json.dumps(
                {
                    "sessionId": SID,
                    "projectHash": "ab" * 32,
                    "startTime": "2026-07-01T10:00:00.000Z",
                    "lastUpdated": "2026-07-01T10:05:00.000Z",
                    "summary": "Legacy chat",
                    "messages": [
                        user("m1", "old question"),
                        model("m2", "old answer", toolCalls=[{"id": "t", "name": "shell"}]),
                        "junk",
                    ],
                },
                indent=2,
            )
        )
        session, warnings = self.parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual((session.id, session.title, session.messages), (SID, "Legacy chat", 2))
        self.assertEqual(session.started, parse_ts("2026-07-01T10:00:00.000Z"))
        self.assertEqual(
            self.texts(session),
            [("user", "old question"), ("assistant", "old answer"), ("tool", "shell")],
        )

    def test_legacy_json_on_one_line(self):
        path = self.chats / chat_name(ext="json")
        path.write_text(json.dumps({**header(), "messages": [user("m1", "one line")]}))
        self.assertEqual(self.parse(path)[0].title, "one line")

    def test_legacy_json_without_content_or_id(self):
        path = self.chats / chat_name(ext="json")
        path.write_text(json.dumps({"sessionId": SID, "messages": []}, indent=1))
        self.assertEqual(self.parse(path), (None, []))
        path.write_text(json.dumps({"sessionId": 12, "messages": [user("m1", "x")]}, indent=1))
        session, warnings = self.parse(path)
        self.assertIsNone(session)
        self.assertEqual(len(warnings), 1)

    def test_file_without_metadata_is_a_warning(self):
        for data in (
            b'{"id":"m1","type":"user","content":"orphan"}\n',
            b"not json at all\n",
            b'{"sessionId":"' + SID.encode() + b'"}\n{"id":"m1","type":"user"}\n',
            b"[1, 2, 3]",
        ):
            with self.subTest(data=data):
                path = self.chats / chat_name()
                path.write_bytes(data)
                session, warnings = self.parse(path)
                self.assertIsNone(session)
                self.assertEqual(len(warnings), 1)
                self.assertIn(str(path), warnings[0])

    def test_empty_file_is_skipped_quietly(self):
        for data in (b"", b"\n\n  \n"):
            path = self.chats / chat_name()
            path.write_bytes(data)
            self.assertEqual(self.parse(path), (None, []))

    def test_half_written_last_line_and_junk_lines(self):
        s = self.listed(
            [header(), user("m1", "question")],
            raw=b'\xff\xfe\n[1]\n"text"\n{"id":"m2","type":"gemini","content":"half',
        )
        self.assertEqual(s.messages, 1)
        self.assertEqual(self.texts(s), [("user", "question")])

    def test_file_that_vanished(self):
        path = self.write([header(), user("m1", "q")])
        candidate = self.candidate(path)
        path.unlink()
        with collect_warnings() as collected:
            self.assertIsNone(self.provider.parse(self.store, candidate))
        self.assertEqual(len(collected.items), 1)
        self.assertIsNone(self.provider.parse(self.store, Candidate("x", [])))


class TranscriptTest(GeminiCase):
    def test_content_and_order(self):
        s = self.listed(
            [
                header(),
                user("m1", "<session_context>\nToday is…\n</session_context>", 1),
                user("m2", "/model", 2),
                {
                    "id": "m3",
                    "timestamp": "2026-09-01T10:00:03.000Z",
                    "type": "info",
                    "content": "Model set",
                },
                {
                    "id": "m4",
                    "timestamp": "2026-09-01T10:00:04.000Z",
                    "type": "user",
                    "content": [
                        {"text": "look at "},
                        {"text": "this"},
                        {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
                    ],
                },
                model(
                    "m5",
                    "",
                    5,
                    thoughts=[{"subject": "Plan", "description": "secret reasoning"}],
                    toolCalls=[
                        {
                            "id": "c1",
                            "name": "read_file",
                            "args": {"path": "x"},
                            "result": [{"functionResponse": {"response": {"output": "secret"}}}],
                            "status": "success",
                            "timestamp": "2026-09-01T10:00:06.000Z",
                        },
                        {"id": "c2", "name": "run_shell_command"},
                        {"id": "c3"},
                    ],
                ),
                model(
                    "m6",
                    [
                        {"text": "Here is "},
                        {"text": "the answer."},
                        {"thought": True, "text": "hidden"},
                    ],
                    7,
                ),
                {
                    "id": "m7",
                    "timestamp": "2026-09-01T10:00:08.000Z",
                    "type": "error",
                    "content": "API error",
                },
                user("m8", "thanks", 9),
            ]
        )
        self.assertEqual(s.messages, 8)
        self.assertEqual(
            list(self.provider.transcript(s)),
            [
                Turn("user", "look at this", "2026-09-01T10:00:04.000000Z"),
                Turn("tool", "read_file", "2026-09-01T10:00:06.000000Z"),
                Turn("tool", "run_shell_command", "2026-09-01T10:00:05.000000Z"),
                Turn("assistant", "Here is the answer.", "2026-09-01T10:00:07.000000Z"),
                Turn("user", "thanks", "2026-09-01T10:00:09.000000Z"),
            ],
        )

    def test_session_without_files_or_with_a_missing_file(self):
        s = self.listed([header(), user("m1", "q")])
        s.files = []
        self.assertEqual(list(self.provider.transcript(s)), [])
        s.files = [str(self.tmp / "gone.jsonl")]
        self.assertEqual(list(self.provider.transcript(s)), [])


class IndexIntegrationTest(unittest.TestCase):
    def test_scan_lists_the_fixture_store(self):
        box = Sandbox(self, {"gemini": Gemini})
        disk = box.mount("Win")
        shutil.copytree(WINDOWS, disk / "Users" / "user" / ".gemini")
        shutil.copytree(NOT_A_STORE, box.home / ".gemini")
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(
            (summary["stores"], summary["candidates"], summary["sessions"], summary["parsed"]),
            (1, 2, 1, 2),
        )
        (s,) = api.list_sessions("gemini")
        self.assertEqual(
            (s.id, s.origin, s.launchable, s.cwd),
            (SYNTHETIC_ID, "foreign", False, "f:\\proj\\my site"),
        )
        again = api.scan(deep=True)
        self.assertEqual((again["parsed"], again["cached"], again["sessions"]), (0, 2, 1))
        self.assertTrue(api.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
