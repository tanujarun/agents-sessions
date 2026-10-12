"""providers/claude.py against tests/fixtures and edge cases on temp copies.

Expected values: tests/fixtures/README.md. Rules: docs/FORMATS.md "Claude Code".
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
from agent_sessions.providers import claude as claude_mod  # noqa: E402
from agent_sessions.providers.base import Turn, collect_warnings, parse_ts  # noqa: E402
from agent_sessions.providers.claude import Claude  # noqa: E402

NATIVE = FIXTURES / "claude" / "native" / ".claude"
WINDOWS = FIXTURES / "claude" / "windows" / ".claude"

# id: (project dir, title, cwd, started, updated, messages, note)
LISTED = {
    NATIVE: {
        "3f47bd5d-9d03-4733-9887-2452632e5d1a": (
            "-home-user-My-Proj",
            "title-7",
            "/home/user/My Proj",
            "2026-10-10T11:40:07.198Z",
            "2026-10-10T11:41:13.487Z",
            2,
            None,
        ),
        "55bb3e6d-4d87-4540-a78d-b485572fa15a": (
            "-home-user-proj",
            "title-16",
            "/home/user/proj",
            "2026-10-08T14:29:37.111Z",
            "2026-10-08T14:29:37.291Z",
            1,
            None,
        ),
        "054512e4-865c-4884-85ac-4c598dd2d2ec": (
            "-home-user",
            "title-45",
            "/home/user",
            "2026-10-06T16:21:25.473Z",
            "2026-10-06T16:23:37.247Z",
            2,
            None,
        ),
        "09d1d9c9-87b3-4bb1-a661-a53e32a64e8c": (
            "-home-user",
            "/cmd args-20",
            "/home/user",
            "2026-09-25T15:19:46.521Z",
            "2026-09-25T15:20:35.347Z",
            1,
            None,
        ),
        "1f28998c-0737-4594-824d-94eab408a6a3": (
            "-home-user",
            "title-33",
            "/home/user",
            "2026-10-06T16:46:38.878Z",
            "2026-10-06T16:50:39.334Z",
            2,
            "continued in 93752775-69de-4168-be73-93ccde31fecc",
        ),
        "93752775-69de-4168-be73-93ccde31fecc": (
            "-home-user",
            "title-33",
            "/home/user",
            "2026-10-06T16:46:38.878Z",
            "2026-10-06T16:54:38.007Z",
            2,
            None,
        ),
    },
    WINDOWS: {
        "214de9ed-5aa4-4788-8a9f-cec8d18b3a3d": (
            "F--Proj-My-Site",
            "title-69",
            "F:\\Proj\\My Site",
            "2026-10-08T16:01:41.706Z",
            "2026-10-08T16:01:49.073Z",
            2,
            None,
        ),
        "69669cf3-d7ae-4ba4-a028-c6abfd2bd622": (
            "F--Proj-My-Site",
            "title-59",
            "F:\\Proj\\My Site",
            "2026-09-10T14:39:17.691Z",
            "2026-09-10T14:43:33.110Z",
            2,
            None,
        ),
    },
}
# id: (project dir, title, cwd, started, updated) of the empty sessions
EMPTY = {
    NATIVE: {
        "0786864e-bbc2-4772-a45c-396ce7dd01e1": (
            "-home-user",
            "/mcp",
            "/home/user",
            "2026-10-03T13:25:29.098Z",
            "2026-10-03T13:25:52.668Z",
        ),
    },
    WINDOWS: {
        "2a82a0e9-5e6d-48ad-9a66-ce6bf743f29b": (
            "C--Users-user",
            "2a82a0e9",
            "C:\\Users\\user",
            "2026-09-10T17:49:12.150Z",
            "2026-09-10T17:49:12.242Z",
        ),
        "e4797d48-dfc8-493b-88f2-0fc08153bc79": (
            "C--Users-user",
            "/cmd args-74",
            "C:\\Users\\user",
            "2026-10-09T19:54:56.737Z",
            "2026-10-09T19:54:59.645Z",
        ),
        "326c0d3d-188e-4eb7-a98f-6783f772d1d1": (
            "F--Proj-My-Site",
            "326c0d3d",
            "F:\\Proj\\My Site",
            "2026-09-15T15:44:35.309Z",
            "2026-09-15T15:44:35.945Z",
        ),
    },
}
BOOKKEEPING = "3f86453c-8a00-4bfe-badc-8b210c714024"
SUBAGENT = "-home-user/054512e4-865c-4884-85ac-4c598dd2d2ec/subagents/agent-4e6b69c678d421890.jsonl"
NEW_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def store_for(root) -> Store:
    native = Path(root) == NATIVE
    return Store(
        "claude",
        str(root),
        origin="native" if native else "foreign",
        mount="/" if native else "/mnt/win",
        mount_label="Linux" if native else "Win",
    )


def transcript_path(root, project, ident) -> str:
    return str(Path(root) / "projects" / project / f"{ident}.jsonl")


def user(content, ts="2026-10-01T10:00:00.000Z", cwd="/work/dir", **extra) -> dict:
    record = {
        "parentUuid": None,
        "isSidechain": False,
        "type": "user",
        "message": {"role": "user", "content": content},
        "timestamp": ts,
        "cwd": cwd,
        "sessionId": NEW_ID,
    }
    record.update(extra)
    return record


def assistant(blocks, ident="msg_1", ts="2026-10-01T10:00:05.000Z", cwd="/work/dir") -> dict:
    message = {"role": "assistant", "content": blocks}
    if ident is not None:
        message["id"] = ident
    return {
        "parentUuid": "p",
        "isSidechain": False,
        "type": "assistant",
        "message": message,
        "timestamp": ts,
        "cwd": cwd,
        "sessionId": NEW_ID,
    }


def text(value) -> dict:
    return {"type": "text", "text": value}


class ClaudeCase(unittest.TestCase):
    def setUp(self):
        self.provider = Claude()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-claude-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / ".claude"
        self.project = self.root / "projects" / "-work-dir"
        self.project.mkdir(parents=True)
        self.store = Store("claude", str(self.root), origin="native")

    def write(self, records, ident=NEW_ID, raw=b"") -> Path:
        """Write a transcript into the temp store; ``raw`` is appended as is."""
        path = self.project / f"{ident}.jsonl"
        data = b"".join(json.dumps(r).encode() + b"\n" for r in records)
        path.write_bytes(data + raw)
        return path

    def copy(self, root, project, ident) -> Path:
        path = self.project / f"{ident}.jsonl"
        shutil.copyfile(transcript_path(root, project, ident), path)
        return path

    def parse(self, path, store=None):
        """``(session, warnings)`` for one transcript."""
        path = str(path)
        candidate = Candidate(os.path.basename(path)[: -len(".jsonl")], [path])
        with collect_warnings() as collected:
            session = self.provider.parse(store or self.store, candidate)
        return session, collected.items

    def listed(self, records, **kwargs) -> Session:
        session, warnings = self.parse(self.write(records, **kwargs))
        self.assertEqual(warnings, [])
        self.assertIsNotNone(session)
        return session


class CandidatesTest(ClaudeCase):
    def test_fixture_candidates(self):
        for root in (NATIVE, WINDOWS):
            with self.subTest(store=root.parent.name):
                expect = {
                    ident: transcript_path(root, row[0], ident)
                    for table in (LISTED, EMPTY)
                    for ident, row in table[root].items()
                }
                if root == NATIVE:
                    expect[BOOKKEEPING] = transcript_path(root, "-home-user", BOOKKEEPING)
                found = self.provider.candidates(store_for(root))
                self.assertEqual([c.id_hint for c in found], sorted(expect))
                self.assertEqual(
                    {c.id_hint: c.paths for c in found},
                    {ident: [path] for ident, path in expect.items()},
                )

    def test_counts_match_the_readme(self):
        # README counts 9 candidates for the native store: it includes the
        # sub-agent transcript, which FORMATS §3 says is never a candidate.
        for root, candidates, listed in ((NATIVE, 8, 6), (WINDOWS, 5, 2)):
            store = store_for(root)
            self.assertEqual(len(self.provider.candidates(store)), candidates)
            sessions = self.provider.list_sessions(store)
            self.assertEqual([s.id for s in sessions], sorted(LISTED[root]))
            self.assertEqual(len(sessions), listed)

    def test_only_uuid_transcripts_at_project_depth(self):
        self.write([user("hello")])
        for name in (
            "notes.jsonl",
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE.jsonl",
            f"{NEW_ID}.json",
            f"{NEW_ID}.desktop-released.json",
            f"{NEW_ID[:-1]}.jsonl",
        ):
            (self.project / name).write_text("{}\n")
        deeper = self.project / NEW_ID / "subagents"
        deeper.mkdir(parents=True)
        (deeper / "agent-0123456789abcdef0.jsonl").write_text("{}\n")
        (deeper / "11111111-2222-4333-8444-555555555555.jsonl").write_text("{}\n")
        (self.project / "memory").mkdir()
        (self.root / "projects" / "22222222-2222-4333-8444-555555555555.jsonl").write_text("{}\n")
        found = self.provider.candidates(self.store)
        self.assertEqual(
            [(c.id_hint, c.paths) for c in found],
            [(NEW_ID, [str(self.project / f"{NEW_ID}.jsonl")])],
        )

    def test_candidates_read_no_file(self):
        boom = mock.Mock(side_effect=AssertionError("candidates must not read files"))
        with mock.patch.object(base, "open_ro", boom), mock.patch("builtins.open", boom):
            self.assertEqual(len(self.provider.candidates(store_for(NATIVE))), 8)
        boom.assert_not_called()

    def test_store_without_projects(self):
        self.assertEqual(self.provider.candidates(Store("claude", str(self.tmp / "nope"))), [])

    def test_same_id_in_two_project_dirs_is_one_candidate(self):
        first = self.write([user("first copy")])
        other = self.root / "projects" / "-work-other"
        other.mkdir()
        second = other / f"{NEW_ID}.jsonl"
        second.write_text(json.dumps(user("second copy")) + "\n")
        found = self.provider.candidates(self.store)
        self.assertEqual(
            [(c.id_hint, c.paths) for c in found], [(NEW_ID, [str(first), str(second)])]
        )
        with collect_warnings() as collected:
            session = self.provider.parse(self.store, found[0])
        self.assertEqual(session.title, "first copy")
        self.assertEqual(session.files, [str(first)])
        self.assertEqual(
            collected.items,
            [f"{second}: session id also exists at {first}; this copy is not listed"],
        )


class FixtureParseTest(ClaudeCase):
    def test_listed_sessions(self):
        for root, rows in LISTED.items():
            store = store_for(root)
            for ident, (project, title, cwd, started, updated, messages, note) in rows.items():
                with self.subTest(session=ident):
                    path = transcript_path(root, project, ident)
                    session, warnings = self.parse(path, store)
                    self.assertEqual(warnings, [])
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
                        (ident, title, cwd, parse_ts(started), parse_ts(updated), messages, note),
                    )
                    self.assertEqual(session.files[0], path)
                    self.assertEqual(
                        (session.provider, session.store, session.origin, session.mount_label),
                        ("claude", store.root, store.origin, store.mount_label),
                    )
                    self.assertEqual(session.key, Session.for_store(store, id=ident).key)

    def test_side_directory_belongs_to_the_session(self):
        files = {}
        for root, rows in LISTED.items():
            for session in self.provider.list_sessions(store_for(root)):
                files[session.id] = session.files
        for ident in (
            "054512e4-865c-4884-85ac-4c598dd2d2ec",
            "214de9ed-5aa4-4788-8a9f-cec8d18b3a3d",
        ):
            transcript = files.pop(ident)[0]
            self.assertTrue(os.path.isdir(transcript[: -len(".jsonl")]))
            self.assertEqual(
                self.provider.parse(store_for(NATIVE), Candidate(ident, [transcript])).files,
                [transcript, transcript[: -len(".jsonl")]],
            )
        for ident, paths in files.items():
            self.assertEqual(len(paths), 1, ident)

    def test_custom_title_json_of_the_desktop_app_is_not_a_title_source(self):
        ident = "214de9ed-5aa4-4788-8a9f-cec8d18b3a3d"
        side = WINDOWS / "projects" / "F--Proj-My-Site" / ident / "custom-title.json"
        self.assertEqual(json.loads(side.read_text())["customTitle"], "title-71")
        session, _ = self.parse(
            transcript_path(WINDOWS, "F--Proj-My-Site", ident), store_for(WINDOWS)
        )
        self.assertEqual(session.title, "title-69")

    def test_empty_sessions_are_excluded(self):
        for root, rows in EMPTY.items():
            for ident, (project, title, cwd, started, updated) in rows.items():
                with self.subTest(session=ident):
                    path = transcript_path(root, project, ident)
                    self.assertEqual(self.parse(path, store_for(root)), (None, []))
                    scan = claude_mod._scan(path)
                    self.assertTrue(scan.has_message)
                    self.assertEqual((scan.prompts, scan.assistants, scan.messages), (0, 0, 0))
                    self.assertEqual(claude_mod._title(scan, ident)[0], title)
                    self.assertEqual(
                        (scan.cwd, scan.started, scan.updated),
                        (cwd, parse_ts(started), parse_ts(updated)),
                    )

    def test_bookkeeping_only_file_is_excluded(self):
        path = transcript_path(NATIVE, "-home-user", BOOKKEEPING)
        self.assertEqual(self.parse(path, store_for(NATIVE)), (None, []))
        scan = claude_mod._scan(path)
        self.assertFalse(scan.has_message)
        self.assertEqual(claude_mod._title(scan, BOOKKEEPING), ("title-17", "agent-name"))
        self.assertEqual(
            (scan.cwd, scan.started, scan.updated, scan.messages), (None, None, None, 0)
        )

    def test_subagent_transcript_is_not_a_candidate(self):
        path = NATIVE / "projects" / SUBAGENT
        self.assertTrue(path.is_file())
        for candidate in self.provider.candidates(store_for(NATIVE)):
            self.assertNotIn("subagents", candidate.paths[0])

    def test_sidechain_at_top_level_is_excluded(self):
        path = self.project / f"{NEW_ID}.jsonl"
        shutil.copyfile(NATIVE / "projects" / SUBAGENT, path)
        self.assertEqual(self.parse(path), (None, []))
        scan = claude_mod._scan(path)
        self.assertTrue(scan.has_message and scan.sidechain)
        # The same records without the flag are an ordinary session.
        records = [json.loads(line) for line in path.read_text().splitlines()]
        for record in records:
            record["isSidechain"] = False
        self.assertEqual(self.listed(records).messages, 2)

    def test_superseded_parent_and_its_child_are_both_listed(self):
        sessions = {s.id: s for s in self.provider.list_sessions(store_for(NATIVE))}
        parent = sessions["1f28998c-0737-4594-824d-94eab408a6a3"]
        child = sessions["93752775-69de-4168-be73-93ccde31fecc"]
        self.assertEqual(parent.note, f"continued in {child.id}")
        self.assertIsNone(child.note)
        self.assertEqual((parent.title, parent.started), (child.title, child.started))

    def test_resume_argv(self):
        for root in (NATIVE, WINDOWS):
            for session in self.provider.list_sessions(store_for(root)):
                self.assertEqual(
                    self.provider.resume_argv(session), ["claude", "--resume", session.id]
                )

    def test_import_needs_the_session_files(self):
        """The import itself: tests/test_importer.py."""
        from agent_sessions.providers.base import NotSupported

        store = Store("claude", "/nowhere/store", "foreign", "/nowhere", "x")
        session = Session.for_store(store, id="00000000-0000-4000-8000-000000000000")
        with self.assertRaises(NotSupported) as caught:
            self.provider.import_plan(session, None)
        self.assertIn("no transcript file", caught.exception.reason)


class RulesTest(ClaudeCase):
    """FORMATS §3-4 on synthetic transcripts."""

    def test_title_precedence_and_last_record_wins(self):
        records = [
            {"type": "summary", "summary": "from summary", "leafUuid": "x"},
            {"type": "ai-title", "aiTitle": "ai one", "sessionId": NEW_ID},
            user("the first prompt"),
            {"type": "ai-title", "aiTitle": "ai two", "sessionId": NEW_ID},
        ]
        self.assertEqual(self.listed(records).title, "ai two")
        records += [
            {"type": "custom-title", "customTitle": "custom one"},
            {"type": "custom-title", "customTitle": "custom two"},
            {"type": "ai-title", "aiTitle": "ai three"},
        ]
        self.assertEqual(self.listed(records).title, "custom two")
        records += [
            {"type": "agent-name", "agentName": "agent one"},
            {"type": "agent-name", "agentName": "agent two"},
            {"type": "custom-title", "customTitle": "custom three"},
        ]
        self.assertEqual(self.listed(records).title, "agent two")
        # A blank or non-string value does not hide the next source.
        records += [
            {"type": "agent-name", "agentName": "  "},
            {"type": "custom-title", "customTitle": 7},
        ]
        self.assertEqual(self.listed(records).title, "custom three")

    def test_summary_is_the_last_title_record(self):
        records = [
            {"type": "summary", "summary": "old"},
            user("prompt"),
            {"type": "summary", "summary": "from summary"},
        ]
        self.assertEqual(self.listed(records).title, "from summary")
        scan = claude_mod._scan(self.project / f"{NEW_ID}.jsonl")
        self.assertEqual(claude_mod._title(scan, NEW_ID), ("from summary", "summary"))

    def test_title_falls_back_to_the_first_real_prompt(self):
        long = "word " * 40
        records = [
            user("<local-command-caveat>ignore</local-command-caveat>", isMeta=True),
            user("<command-name>/model</command-name>\n<command-args></command-args>"),
            user("<local-command-stdout>ok</local-command-stdout>"),
            user("<system-reminder>injected</system-reminder>"),
            user("[Request interrupted by user for tool use]"),
            user("summary of before", isCompactSummary=True),
            user(
                [
                    {"type": "tool_result", "tool_use_id": "t", "content": "out"},
                    text("not a prompt"),
                ]
            ),
            user("   \n  "),
            user(f"  first\nline\t and more {long}"),
            user("second prompt"),
        ]
        session = self.listed(records)
        self.assertEqual(session.title, ("first line and more " + long)[:80].rstrip())
        self.assertLessEqual(len(session.title), 80)
        self.assertEqual(session.messages, 2)

    def test_prompt_in_a_text_block_list(self):
        records = [user([{"type": "image", "source": {}}, text("look at this"), text("too")])]
        session = self.listed(records)
        self.assertEqual((session.title, session.messages), ("look at this", 1))

    def test_bash_input_is_a_prompt(self):
        records = [
            user("<bash-input>ls -la\n</bash-input>"),
            user("<bash-stdout>files</bash-stdout><bash-stderr></bash-stderr>"),
        ]
        session = self.listed(records)
        self.assertEqual((session.title, session.messages), ("! ls -la", 1))

    def test_command_fallback_then_id(self):
        command = user(
            "<command-message>mcp</command-message>\n"
            "<command-name>/mcp</command-name>\n<command-args></command-args>"
        )
        later = user("<command-name>/other</command-name><command-args>x y</command-args>")
        reply = assistant([text("done")])
        session = self.listed([command, later, reply])
        self.assertEqual((session.title, session.messages), ("/mcp", 1))
        session = self.listed([later, command, reply])
        self.assertEqual(session.title, "/other x y")
        session = self.listed([reply])
        self.assertEqual((session.title, session.messages), (NEW_ID[:8], 1))
        # Without an assistant record the command-only session is empty.
        self.assertEqual(self.parse(self.write([command])), (None, []))

    def test_message_count(self):
        records = [
            user("one"),
            assistant([{"type": "thinking", "thinking": ""}], "msg_a"),
            assistant([text("part")], "msg_a"),
            assistant([{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}], "msg_a"),
            user([{"type": "tool_result", "tool_use_id": "t", "content": "out"}]),
            assistant([text("answer")], "msg_b"),
            assistant([text("no id")], None),
            assistant([text("no id either")], None),
            user("two"),
            user("meta", isMeta=True),
        ]
        self.assertEqual(self.listed(records).messages, 2 + 2 + 2)

    def test_cwd_is_the_first_recorded_one(self):
        records = [
            {"type": "mode", "mode": "normal"},
            {"type": "attachment", "parentUuid": None, "cwd": 7},
            user("start", cwd="/start/here"),
            user("moved", cwd="/elsewhere"),
        ]
        self.assertEqual(self.listed(records).cwd, "/start/here")
        records += [
            {"type": "relocated", "relocatedCwd": "/first/move"},
            {"type": "relocated", "relocatedCwd": "/last/move"},
        ]
        self.assertEqual(self.listed(records).cwd, "/last/move")

    def test_started_and_updated_are_min_and_max(self):
        records = [
            user("later", ts="2026-10-02T10:00:00.000Z"),
            {"type": "attachment", "parentUuid": "x", "timestamp": "2026-10-01T09:00:00.000Z"},
            {"type": "queue-operation", "timestamp": "2026-10-03T08:00:00.123456789Z"},
            {
                "type": "file-history-snapshot",
                "snapshot": {"timestamp": "2027-01-01T00:00:00.000Z"},
            },
            assistant([text("x")], ts="2026-10-02T11:00:00.000Z"),
            {"type": "system", "parentUuid": "x", "timestamp": "not a time"},
        ]
        session = self.listed(records)
        self.assertEqual(
            (session.started, session.updated),
            ("2026-10-01T09:00:00.000000Z", "2026-10-03T08:00:00.123456Z"),
        )

    def test_timestamps_fall_back_to_the_file_mtime(self):
        record = user("no clock")
        del record["timestamp"]
        path = self.write([record])
        os.utime(path, (1790000000, 1790000000))
        session, warnings = self.parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual(
            (session.started, session.updated), (parse_ts(1790000000), parse_ts(1790000000))
        )

    def test_continued_in_note_needs_no_message_after_it(self):
        mark = {
            "type": "continued-in",
            "continuedInSessionId": "child-id",
            "timestamp": "2026-10-01T10:01:00.000Z",
        }
        tail = [{"type": "last-prompt", "lastPrompt": "x"}, {"type": "cost-state"}]
        self.assertEqual(self.listed([user("a"), mark] + tail).note, "continued in child-id")
        self.assertIsNone(self.listed([user("a"), mark, assistant([text("b")])]).note)
        self.assertIsNone(self.listed([user("a"), mark, user("b")] + tail).note)
        self.assertIsNone(self.listed([user("a")]).note)

    def test_title_only_and_summary_only_files_are_not_sessions(self):
        for records in (
            [{"type": "summary", "summary": "old style", "leafUuid": "x"}],
            [{"type": "ai-title", "aiTitle": "t"}, {"type": "agent-name", "agentName": "t"}],
            [{"type": "custom-title", "customTitle": "t"}],
        ):
            with self.subTest(records=records):
                self.assertEqual(self.parse(self.write(records)), (None, []))

    def test_startup_only_file_is_empty(self):
        records = [
            {"type": "mode", "mode": "normal"},
            {
                "type": "attachment",
                "parentUuid": None,
                "isSidechain": False,
                "timestamp": "2026-10-01T10:00:00.000Z",
                "cwd": "/work/dir",
            },
            {"type": "system", "parentUuid": "x", "subtype": "x"},
        ]
        self.assertEqual(self.parse(self.write(records)), (None, []))

    def test_prompt_without_a_reply_is_listed(self):
        session = self.listed([user("only a prompt")])
        self.assertEqual((session.title, session.messages), ("only a prompt", 1))

    def test_unknown_and_malformed_records_are_ignored(self):
        records = [
            {"type": "frame-link", "x": 1},
            {"type": "from-the-future", "parentUuid": None},
            {"no": "type"},
            {"type": 7, "timestamp": 12, "cwd": ["x"]},
            {"type": "user", "parentUuid": None, "message": "text", "timestamp": None},
            {"type": "user", "message": {"content": 5}},
            {"type": "user", "message": None},
            {"type": "user", "message": {"content": [7, None, "x", {"type": "text", "text": 5}]}},
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": {"id": 5}},
            {"type": "assistant", "message": {"content": "text", "id": ""}},
            {"type": "ai-title", "aiTitle": None},
            {"type": "ai-title"},
            {"type": "continued-in", "continuedInSessionId": 5},
            {"type": "relocated"},
            user("real"),
        ]
        raw = b'[1, 2]\n"string"\n7\nnull\n\n   \n'
        session = self.listed(records, raw=raw)
        self.assertEqual((session.title, session.messages, session.note), ("real", 1 + 3, None))
        self.assertEqual(
            list(self.provider.transcript(session)),
            [Turn("user", "real", "2026-10-01T10:00:00.000000Z")],
        )


class DamagedFileTest(ClaudeCase):
    FIXTURE = (WINDOWS, "F--Proj-My-Site", "69669cf3-d7ae-4ba4-a028-c6abfd2bd622")

    def expected(self):
        return LISTED[self.FIXTURE[0]][self.FIXTURE[2]]

    def fields(self, session):
        return (
            session.title,
            session.cwd,
            session.started,
            session.updated,
            session.messages,
            session.note,
        )

    def test_half_written_last_line(self):
        _project, title, cwd, started, updated, messages, note = self.expected()
        whole = (title, cwd, parse_ts(started), parse_ts(updated), messages, note)
        path = self.copy(*self.FIXTURE)
        with open(path, "ab") as fh:
            fh.write(
                b'{"parentUuid":"x","type":"assistant","timestamp":"2027-01-01T00:00:00.000Z",'
                b'"message":{"id":"msg_new","content":[{"type":"text","te'
            )
        session, warnings = self.parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual(self.fields(session), whole)

    def test_file_cut_in_the_middle_of_a_record(self):
        path = self.copy(*self.FIXTURE)
        lines = path.read_bytes().splitlines(keepends=True)
        # Cut inside the assistant record (line 11 of 16): the prompt before
        # it survives, the reply and everything after are gone.
        self.assertEqual(json.loads(lines[10])["type"], "assistant")
        path.write_bytes(b"".join(lines[:10]) + lines[10][: len(lines[10]) // 2])
        session, warnings = self.parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual(
            (session.title, session.messages, session.updated),
            ("title-59", 1, parse_ts("2026-09-10T14:39:33.272Z")),
        )

    def test_non_utf8_line(self):
        _project, title, cwd, started, _updated, messages, note = self.expected()
        path = self.copy(*self.FIXTURE)
        with open(path, "ab") as fh:
            fh.write(
                b'{"parentUuid":"x","type":"user","timestamp":"2026-09-10T15:00:00.000Z",'
                b'"message":{"role":"user","content":"caf\xe9 \xff\xfe ok"}}\n'
            )
            fh.write(b"\xff\xfe\x00\x80 not json at all\n")
            fh.write(b'{"type":"ai-title","aiTitle":"na\xefve title"}\n')
        session, warnings = self.parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual(
            self.fields(session),
            (
                "na\ufffdve title",
                cwd,
                parse_ts(started),
                parse_ts("2026-09-10T15:00:00.000Z"),
                messages + 1,
                note,
            ),
        )
        turns = list(self.provider.transcript(session))
        self.assertEqual(
            turns[-1], Turn("user", "caf\ufffd \ufffd\ufffd ok", "2026-09-10T15:00:00.000000Z")
        )

    def test_crlf_and_bom(self):
        lines = [
            json.dumps(r).encode() for r in (user("windows line ends"), assistant([text("ok")]))
        ]
        path = self.write([], raw=b"\xef\xbb\xbf" + b"\r\n".join(lines) + b"\r\n")
        session, warnings = self.parse(path)
        self.assertEqual(warnings, [])
        self.assertEqual((session.title, session.messages), ("windows line ends", 2))

    def test_empty_file(self):
        path = self.write([])
        self.assertEqual(path.stat().st_size, 0)
        self.assertEqual([c.id_hint for c in self.provider.candidates(self.store)], [NEW_ID])
        self.assertEqual(self.parse(path), (None, []))

    def test_unreadable_file_is_a_warning(self):
        missing = str(self.project / f"{NEW_ID}.jsonl")
        session, warnings = self.parse(missing)
        self.assertIsNone(session)
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith(f"{missing}: cannot read"), warnings)
        path = self.write([user("x")])
        with mock.patch.object(base, "open_ro", side_effect=PermissionError(13, "denied")):
            session, warnings = self.parse(path)
        self.assertIsNone(session)
        self.assertEqual(warnings, [f"{path}: cannot read: denied"])

    def test_directory_named_like_a_transcript(self):
        path = self.project / f"{NEW_ID}.jsonl"
        path.mkdir()
        session, warnings = self.parse(path)
        self.assertIsNone(session)
        self.assertEqual(len(warnings), 1)


class _Counting:
    """A binary file that counts how it is read."""

    def __init__(self, fh, log):
        self.fh, self.log = fh, log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.close()

    def readline(self, *args):
        line = self.fh.readline(*args)
        self.log["longest"] = max(self.log["longest"], len(line))
        self.log["lines"] += 1
        return line

    def __iter__(self):
        return iter(self.readline, b"")

    def read(self, size=-1):
        self.log["reads"].append(size)
        return self.fh.read(size)

    def readlines(self, *args):
        self.log["reads"].append("readlines")
        return self.fh.readlines(*args)


class StreamingTest(ClaudeCase):
    def run_counted(self, action):
        opened, log = [], {"reads": [], "lines": 0, "longest": 0}
        real = base.open_ro

        def counting(path):
            opened.append(str(path))
            return _Counting(real(path), log)

        with (
            mock.patch.object(base, "open_ro", counting),
            mock.patch("builtins.open", side_effect=AssertionError("plain open")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes")),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("read_text")),
        ):
            result = action()
        return result, opened, log

    def test_parse_reads_the_file_once_line_by_line(self):
        ident = "054512e4-865c-4884-85ac-4c598dd2d2ec"
        path = transcript_path(NATIVE, "-home-user", ident)
        with open(path, "rb") as fh:
            lines = fh.read().count(b"\n")
        candidate = Candidate(ident, [path])
        session, opened, log = self.run_counted(
            lambda: self.provider.parse(store_for(NATIVE), candidate)
        )
        self.assertEqual(session.title, "title-45")
        self.assertEqual(opened, [path])
        self.assertEqual(log["reads"], [])
        self.assertEqual(log["lines"], lines + 1)  # every line, then the end
        # The side directory's files (sub-agents, tool results) are not read.

    def test_transcript_streams_too(self):
        ident = "69669cf3-d7ae-4ba4-a028-c6abfd2bd622"
        path = transcript_path(WINDOWS, "F--Proj-My-Site", ident)
        session = self.provider.parse(store_for(WINDOWS), Candidate(ident, [path]))
        turns, opened, log = self.run_counted(lambda: list(self.provider.transcript(session)))
        self.assertEqual(len(turns), 2)
        self.assertEqual((opened, log["reads"]), ([path], []))

    def test_big_lines_are_not_accumulated(self):
        # 40 records of 256 kB each: the parser may hold one line at a time.
        blob = "x" * 256_000
        records = [user("the prompt")]
        records += [
            user([{"type": "tool_result", "tool_use_id": "t", "content": blob}]) for _ in range(40)
        ]
        path = self.write(records + [assistant([text("done")])])
        import tracemalloc

        tracemalloc.start()
        try:
            session, _ = self.parse(path)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(session.messages, 2)
        self.assertGreater(path.stat().st_size, 10_000_000)
        self.assertLess(peak, 3_000_000)


class TranscriptTest(ClaudeCase):
    def turns(self, root, project, ident):
        path = transcript_path(root, project, ident)
        session = self.provider.parse(store_for(root), Candidate(ident, [path]))
        return list(self.provider.transcript(session))

    def test_fixture_transcripts(self):
        self.assertEqual(
            self.turns(NATIVE, "-home-user", "054512e4-865c-4884-85ac-4c598dd2d2ec"),
            [
                Turn("tool", "Agent", "2026-10-06T16:21:34.607000Z"),
                Turn("user", "text-44", "2026-10-06T16:23:37.247000Z"),
            ],
        )
        self.assertEqual(
            self.turns(NATIVE, "-home-user", "09d1d9c9-87b3-4bb1-a661-a53e32a64e8c"), []
        )

    def test_windows_session(self):
        self.assertEqual(
            self.turns(WINDOWS, "F--Proj-My-Site", "69669cf3-d7ae-4ba4-a028-c6abfd2bd622"),
            [
                Turn("user", "text-58", "2026-09-10T14:39:33.272000Z"),
                Turn("assistant", "text-60", "2026-09-10T14:39:35.162000Z"),
            ],
        )
        # "[Request interrupted by user]" is not a prompt.
        self.assertEqual(
            self.turns(WINDOWS, "F--Proj-My-Site", "214de9ed-5aa4-4788-8a9f-cec8d18b3a3d"),
            [Turn("user", "text-65", "2026-10-08T16:01:42.676000Z")],
        )

    def test_content_and_order(self):
        tool = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "secret"}}
        records = [
            {"type": "ai-title", "aiTitle": "t"},
            user("<system-reminder>injected</system-reminder>", ts="2026-10-01T10:00:00.000Z"),
            user("first\nprompt  ", ts="2026-10-01T10:00:01.000Z"),
            assistant(
                [{"type": "thinking", "thinking": "hidden"}], "m1", ts="2026-10-01T10:00:02.000Z"
            ),
            assistant(
                [text("\n\nI will look.\n"), tool, text("   ")], "m1", ts="2026-10-01T10:00:03.000Z"
            ),
            user(
                [{"type": "tool_result", "tool_use_id": "t1", "content": "tool output"}],
                ts="2026-10-01T10:00:04.000Z",
            ),
            {
                "type": "attachment",
                "parentUuid": "x",
                "timestamp": "2026-10-01T09:00:00.000Z",
                "attachment": {"type": "hook_success", "stdout": "hook output"},
            },
            assistant(
                [
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "t3", "name": "mcp__x__y", "input": {}},
                    {"type": "tool_use", "id": "t4"},
                ],
                "m2",
                ts="2026-10-01T10:00:05.000Z",
            ),
            user("compacted history", isCompactSummary=True, ts="2026-10-01T10:00:06.000Z"),
            user("<command-name>/clear</command-name>", ts="2026-10-01T10:00:07.000Z"),
            user("<bash-input>git status</bash-input>", ts="2026-10-01T10:00:08.000Z"),
            user(
                [text("part one"), text("<system-reminder>no</system-reminder>"), text("part two")],
                ts="2026-10-01T10:00:09.000Z",
            ),
            assistant([text("Done.")], "m3", ts=None),
        ]
        session = self.listed(records)
        self.assertEqual(
            list(self.provider.transcript(session)),
            [
                Turn("user", "first\nprompt", "2026-10-01T10:00:01.000000Z"),
                Turn("assistant", "I will look.", "2026-10-01T10:00:03.000000Z"),
                Turn("tool", "Bash", "2026-10-01T10:00:03.000000Z"),
                Turn("tool", "Read", "2026-10-01T10:00:05.000000Z"),
                Turn("tool", "mcp__x__y", "2026-10-01T10:00:05.000000Z"),
                Turn("user", "! git status", "2026-10-01T10:00:08.000000Z"),
                Turn("user", "part one\npart two", "2026-10-01T10:00:09.000000Z"),
                Turn("assistant", "Done.", None),
            ],
        )
        text_seen = " ".join(turn.text for turn in self.provider.transcript(session))
        for hidden in ("hidden", "secret", "tool output", "hook output", "injected", "compacted"):
            self.assertNotIn(hidden, text_seen)

    def test_session_without_files_or_with_a_missing_file(self):
        session = self.listed([user("x")])
        session.files = []
        self.assertEqual(list(self.provider.transcript(session)), [])
        session.files = [str(self.project / "gone.jsonl")]
        with collect_warnings() as collected:
            self.assertEqual(list(self.provider.transcript(session)), [])
        self.assertEqual(len(collected.items), 1)


class IndexIntegrationTest(unittest.TestCase):
    def test_scan_lists_the_fixture_stores(self):
        box = Sandbox(self, {"claude": Claude})
        disk = box.mount("Win")
        shutil.copytree(NATIVE, box.home / ".claude")
        shutil.copytree(WINDOWS, disk / "Users" / "user" / ".claude")
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(
            (summary["stores"], summary["candidates"], summary["sessions"], summary["parsed"]),
            (2, 13, 8, 13),
        )
        sessions = api.list_sessions("claude")
        self.assertEqual({s.id for s in sessions}, set(LISTED[NATIVE]) | set(LISTED[WINDOWS]))
        self.assertEqual({s.origin for s in sessions if s.id in LISTED[WINDOWS]}, {"foreign"})
        again = api.scan(deep=True)
        self.assertEqual((again["parsed"], again["cached"], again["sessions"]), (0, 13, 8))
        self.assertTrue(api.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
