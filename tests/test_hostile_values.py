"""Values a store on another drive can hold that cost time or memory rather
than break anything: text that made a pattern start over at every character,
a "time" of a megabyte, a folder or a message id of any length.

The timings are far apart on purpose: what is tested took from ten seconds
to minutes before, and takes milliseconds now."""

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeProvider, Sandbox, write_jsonl

from agent_sessions import api
from agent_sessions.model import Session, Store
from agent_sessions.providers import antigravity, claude, codex, grok
from agent_sessions.providers.base import between, clean_title, parse_ts

LIMIT = 3.0  # seconds; the slow forms of these took 10 s and more
A = "aaaaaaaa-0000-4000-8000-000000000001"


def timed(action):
    started = time.perf_counter()
    result = action()
    return result, time.perf_counter() - started


class BetweenTest(unittest.TestCase):
    PAIRS = (("<command-name>", "</command-name>"), ("<user_query>", "</user_query>"))
    SAMPLES = (
        "",
        "plain",
        "<o>x</o>",
        "a<o></o>b",
        "<o>one</o><o>two</o>",
        "<o><o>inner</o></o>",
        "</o><o>after a stray end</o>",
        "<o>no end",
        "no start</o>",
        "<o>line\nbreak\r\n</o>",
        "<o>x</o",
        "x<o>" * 5,
        "<o>" + "</o" * 3 + "</o>",
    )

    def test_same_answer_as_the_lazy_pattern(self):
        for opening, closing in (*self.PAIRS, ("<o>", "</o>")):
            pattern = re.compile(re.escape(opening) + "(.*?)" + re.escape(closing), re.S)
            for sample in self.SAMPLES:
                text = sample.replace("<o>", opening).replace("</o>", closing)
                found = pattern.search(text)
                self.assertEqual(
                    between(text, opening, closing), found.group(1) if found else None, text
                )

    def test_one_pass_over_text_with_no_end_tag(self):
        text = "<command-name>" * 150_000  # 2 MB; the pattern needed minutes
        result, seconds = timed(lambda: between(text, "<command-name>", "</command-name>"))
        self.assertIsNone(result)
        self.assertLess(seconds, LIMIT)


class SlowTextTest(unittest.TestCase):
    def test_claude_prompt_of_unclosed_command_tags(self):
        for tag in ("<command-name>", "<command-args>", "<bash-input>"):
            record = {"type": "user", "message": {"content": "do this " + tag * 150_000}}
            (real, command), seconds = timed(lambda record=record: claude._prompts(record))
            self.assertLess(seconds, LIMIT, tag)
            self.assertEqual((len(real), command), (1, None), tag)

    def test_claude_prompt_rules_are_what_they_were(self):
        cases = {
            "plain words": (["plain words"], None),
            "<command-name>/mcp</command-name>": ([], "/mcp"),
            "<command-name>/model</command-name> <command-args> opus </command-args>": (
                [],
                "/model opus",
            ),
            "<command-args>x</command-args><command-name>/a</command-name>": ([], "/a x"),
            "<bash-input> ls -la\n</bash-input>": (["! ls -la"], None),
            "<system-reminder>x</system-reminder>": ([], None),
            "[Request interrupted by user for tool use]": ([], None),
            "<command-name>unclosed": ([], None),
            "  ": ([], None),
        }
        for text, expected in cases.items():
            record = {"type": "user", "message": {"content": text}}
            self.assertEqual(claude._prompts(record), expected, text)

    def test_grok_and_antigravity_wrappers(self):
        text = "<user_query>" * 150_000
        record = {"type": "user", "prompt_index": 0, "content": text}
        result, seconds = timed(lambda: grok._prompt_text(record))
        self.assertLess(seconds, LIMIT)
        self.assertEqual(result, text)
        wrapped = {
            "type": "user",
            "content": "<image_files>a</image_files><user_query>\n hi \n</user_query>",
        }
        self.assertEqual(grok._prompt_text(wrapped), "hi")

        text = "<USER_REQUEST>" * 150_000
        result, seconds = timed(lambda: antigravity._user_request(text))
        self.assertLess(seconds, LIMIT)
        self.assertEqual(result, text)
        self.assertEqual(
            antigravity._user_request("<USER_REQUEST>\nfix it\n</USER_REQUEST>\n<META>x</META>"),
            "fix it",
        )
        self.assertEqual(antigravity._user_request(" no wrapper "), "no wrapper")

    def test_antigravity_annotation_of_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pbtxt"
            path.write_bytes(b"\n" * 400_000)  # the pattern with ``^\\s*`` needed two minutes
            result, seconds = timed(lambda: antigravity._pbtxt_title(path))
            self.assertLess(seconds, LIMIT)
            self.assertEqual(result, "")
            for content, title in (
                (b'title:"plain"', "plain"),
                (b'\n\n  \ttitle : "after blank lines"\n', "after blank lines"),
                (b'other: 1\r\n title:"second \\"line\\""\nlast: 2', 'second "line"'),
                (b'subtitle:"no"\n', ""),
                (b'x title:"not first on its line"', ""),
                (b'title:"one"\ntitle:"two"', "one"),
            ):
                path.write_bytes(content)
                self.assertEqual(antigravity._pbtxt_title(path), title, content)

    def test_a_time_that_is_not_one(self):
        long_blank = "2026-01-01T00:00:00" + " " * 400_000 + "x"
        result, seconds = timed(lambda: parse_ts(long_blank))
        self.assertLess(seconds, LIMIT)
        self.assertIsNone(result)
        self.assertIsNone(parse_ts("9" * 5000))  # ``int`` raises on more than 4300 digits
        self.assertIsNone(parse_ts("1" + "0" * 400_000))
        self.assertEqual(
            parse_ts("  2026-10-10T11:40:07.198123456+05:30  "), "2026-10-10T06:10:07.198123Z"
        )
        self.assertEqual(parse_ts("1787992274"), "2026-08-29T08:31:14.000000Z")

    def test_a_whole_claude_scan_of_such_a_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / ".claude"
            records = [
                {
                    "type": "user",
                    "parentUuid": None,
                    "timestamp": "2026-10-01T10:00:00Z" + " " * 300_000 + "x",
                    "message": {"content": "<command-name>" * 100_000},
                },
                {"type": "user", "parentUuid": "p", "message": {"content": "real prompt"}},
            ]
            write_jsonl(store / "projects" / "p" / f"{A}.jsonl", records)
            found, seconds = timed(
                lambda: claude.Claude().list_sessions(Store("claude", str(store), "foreign"))
            )
            self.assertLess(seconds, LIMIT)
            # The first record starts with a tag: by the tool's rule it is no prompt.
            self.assertEqual([(s.id, s.messages, s.title) for s in found], [(A, 1, "real prompt")])


class KeptValuesTest(unittest.TestCase):
    def test_claude_counts_long_message_ids_without_keeping_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / ".claude"
            long_one, long_two = "a" * 300_000, "b" * 300_000
            records = [{"type": "user", "parentUuid": None, "message": {"content": "hello"}}]
            for ident in (long_one, long_one, long_two, "short", "short"):
                records.append({"type": "assistant", "message": {"id": ident, "content": []}})
            path = write_jsonl(store / "projects" / "p" / f"{A}.jsonl", records)
            scan = claude._scan(path)
            self.assertEqual(scan.messages, 1 + 3)
            self.assertLessEqual(max(len(ident) for ident in scan.assistant_ids), claude._ID_KEPT)
            self.assertIn("short", scan.assistant_ids)

    def test_a_version_that_is_not_one_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_jsonl(Path(tmp) / "t.jsonl", [{"version": "2.1.267"}])
            self.assertEqual(claude._recorded_version(path), "2.1.267")
            write_jsonl(path, [{"version": "9" * 500}])
            self.assertIsNone(claude._recorded_version(path))
        self.assertEqual(codex._VERSION_MAX, 64)


class IndexBoundsTest(unittest.TestCase):
    """Whatever an adapter returns, the index keeps a listed session's free
    text to a size that makes sense."""

    def setUp(self):
        self.box = Sandbox(self)
        self.native = self.box.store(self.box.home)

    def test_folder_note_and_title(self):
        self.box.session(self.native, "long", cwd="/x" * 40_000, note="n" * 5_000)
        self.box.session(self.native, "fine", cwd="/y" * 16_000, note="short")
        summary = api.scan()
        by_id = {s.id: s for s in api.list_sessions()}
        self.assertIsNone(by_id["long"].cwd)
        self.assertEqual(by_id["long"].note, "n" * 200)
        self.assertEqual((by_id["fine"].cwd, by_id["fine"].note), ("/y" * 16_000, "short"))
        self.assertEqual(
            summary["warnings"],
            [
                f"{self.native}: long: the recorded folder is 80,000 characters long, "
                "which no path is; left out"
            ],
        )

    def test_an_adapter_that_does_not_clean_its_title(self):
        raw = "  fix\x1b[2J the‮ bug " + "x" * 200

        def parse(provider, store, c):
            return Session.for_store(store, id=c.id_hint, title=raw, files=list(c.paths))

        self.box.session(self.native, "a")
        with mock.patch.object(FakeProvider, "parse", parse):
            api.scan()
        (session,) = api.list_sessions()
        self.assertEqual(session.title, clean_title(raw))
        self.assertEqual(len(session.title), 80)


if __name__ == "__main__":
    unittest.main()
