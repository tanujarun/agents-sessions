"""Files a store on another drive can hold that are not what their name
says: named pipes, devices and directories named like a transcript, lines and
files of any size, JSON nested without end. docs/REVIEW.md R-02 and R-05.

A scan that meets one must finish, warn, and list everything else. The scans
that could wait for ever on the code before the fix run in a child process
(our own launcher) under a timeout and a memory limit, so a regression fails
a test instead of hanging the suite or filling the machine's memory."""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import AGY_WIN, GEMINI_WIN, World
from support import FIXTURES, claude_transcript, write_jsonl

from agent_sessions import api, fsutil, importer, journal
from agent_sessions.model import Store
from agent_sessions.pathmap import PathMap
from agent_sessions.providers import antigravity as agy_mod
from agent_sessions.providers import base, claude, codex, gemini, grok
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import clean_title, collect_warnings, iter_jsonl, open_sqlite_ro
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok

try:
    import resource
except ImportError:  # not a POSIX system: the tests that need it are skipped
    HAVE_RESOURCE = False
else:
    HAVE_RESOURCE = True

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "bin" / "agent-sessions"
CHILD_TIMEOUT = 60  # seconds; a scan of these stores takes well under one
CHILD_MEMORY = 2 * 1024**3  # bytes of address space a child may use
NOT_REGULAR = "not a regular file"
POSIX = hasattr(os, "mkfifo") and HAVE_RESOURCE

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
MB = 1024 * 1024


def _limit_memory() -> None:
    """In the child, before it becomes the launcher: a reader that keeps a
    whole device in memory fails there instead of filling the machine."""
    resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY, CHILD_MEMORY))


def child(case, *argv, timeout=CHILD_TIMEOUT) -> subprocess.CompletedProcess:
    """Run our launcher with the environment of the test's sandbox."""
    try:
        done = subprocess.run(
            [sys.executable, str(LAUNCHER), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=_limit_memory,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"`agent-sessions {' '.join(argv)}` did not finish in {timeout} s"
        ) from None
    return done


def child_scan(case) -> dict:
    done = child(case, "scan", "--deep", "--json")
    case.assertEqual(done.returncode, 0, done.stderr[-2000:])
    return json.loads(done.stdout)


def finishes(case, action, timeout=20):
    """Run ``action`` on a thread; fail when it does not come back."""
    result: list = []

    def run():
        try:
            result.append(("value", action()))
        except BaseException as exc:
            result.append(("raised", exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        case.fail("did not finish: waits on a file")
    kind, value = result[0]
    if kind == "raised":
        raise value
    return value


def fifo(path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    return path


def warned(summary_or_list, path, text=NOT_REGULAR) -> bool:
    warnings = summary_or_list["warnings"] if isinstance(summary_or_list, dict) else summary_or_list
    return any(str(path) in warning and text in warning for warning in warnings)


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


# --- open_ro ------------------------------------------------------------------


class OpenRoTest(TempCase):
    def test_a_regular_file_and_a_link_to_one(self):
        path = self.tmp / "a.jsonl"
        path.write_bytes(b"data")
        os.symlink(path, self.tmp / "link.jsonl")
        for name in ("a.jsonl", "link.jsonl"):
            with fsutil.open_ro(self.tmp / name) as fh:
                self.assertEqual(fh.read(), b"data")

    def test_a_missing_file_is_still_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            fsutil.open_ro(self.tmp / "nothing")

    def test_a_directory_is_refused(self):
        (self.tmp / "d.jsonl").mkdir()
        with self.assertRaises(fsutil.NotRegularFile) as caught:
            fsutil.open_ro(self.tmp / "d.jsonl")
        self.assertIsInstance(caught.exception, OSError)
        self.assertEqual(caught.exception.strerror, NOT_REGULAR)
        self.assertEqual(caught.exception.filename, str(self.tmp / "d.jsonl"))

    @unittest.skipUnless(POSIX, "needs named pipes")
    def test_a_named_pipe_is_refused_without_waiting(self):
        path = fifo(self.tmp / "p.jsonl")
        os.symlink(path, self.tmp / "link.jsonl")
        for name in ("p.jsonl", "link.jsonl"):
            with self.assertRaises(fsutil.NotRegularFile):
                finishes(self, lambda name=name: fsutil.open_ro(self.tmp / name))

    @unittest.skipUnless(POSIX, "needs named pipes")
    def test_the_open_itself_does_not_wait(self):
        """The name can be given to a pipe after the first look: the open
        that follows must not wait, and the descriptor is looked at again."""
        self.assertTrue(fsutil._FILE_FLAGS & os.O_NONBLOCK)
        self.assertTrue(fsutil._FILE_FLAGS & os.O_NOCTTY)
        path = fifo(self.tmp / "p.jsonl")
        regular = os.stat(__file__)
        closed = []
        real_close = os.close
        with (
            mock.patch.object(fsutil.os, "stat", return_value=regular),
            mock.patch.object(
                fsutil.os, "close", side_effect=lambda fd: (closed.append(fd), real_close(fd))
            ),
            self.assertRaises(fsutil.NotRegularFile),
        ):
            finishes(self, lambda: fsutil.open_ro(path))
        self.assertEqual(len(closed), 1)  # the descriptor is not left open

    def test_a_device_is_refused_before_it_is_opened(self):
        """No device node can be made without root: the kind is reported by a
        patched ``stat``."""
        path = self.tmp / "zero.jsonl"
        path.write_bytes(b"data")
        real = os.stat(path)
        for kind in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK, stat.S_IFIFO):
            fake = os.stat_result((kind | 0o644, *tuple(real)[1:10]))
            with (
                mock.patch.object(fsutil.os, "stat", return_value=fake),
                mock.patch.object(fsutil.os, "open", wraps=os.open) as opened,
                self.assertRaises(fsutil.NotRegularFile),
            ):
                fsutil.open_ro(path)
            opened.assert_not_called()

    @unittest.skipUnless(os.path.exists("/dev/zero"), "needs /dev/zero")
    def test_a_link_to_a_real_device_is_refused(self):
        os.symlink("/dev/zero", self.tmp / "zero.jsonl")
        with self.assertRaises(fsutil.NotRegularFile):
            fsutil.open_ro(self.tmp / "zero.jsonl")
        with collect_warnings() as seen:
            self.assertEqual(list(iter_jsonl(self.tmp / "zero.jsonl")), [])
        self.assertEqual(seen.items, [f"{self.tmp / 'zero.jsonl'}: cannot read: {NOT_REGULAR}"])

    @unittest.skipUnless(POSIX, "needs named pipes")
    def test_listing_a_pipe_as_a_directory_does_not_wait(self):
        path = fifo(self.tmp / "projects")
        for lister in (fsutil.listdir_ro, fsutil.subdirs):
            with self.assertRaises(NotADirectoryError):
                finishes(self, lambda lister=lister: lister(path))

    @unittest.skipUnless(POSIX, "needs named pipes")
    def test_sqlite_is_never_pointed_at_a_pipe_or_a_device(self):
        import sqlite3

        targets = [fifo(self.tmp / "p.db"), self.tmp / "d.db"]
        targets[1].mkdir()
        if os.path.exists("/dev/zero"):
            os.symlink("/dev/zero", self.tmp / "zero.db")
            targets.append(self.tmp / "zero.db")
        for path in targets:
            with self.assertRaises(sqlite3.Error) as caught:
                finishes(self, lambda path=path: open_sqlite_ro(path))
            self.assertIn(NOT_REGULAR, str(caught.exception))


# --- whole scans, in a child --------------------------------------------------


@unittest.skipUnless(POSIX, "needs named pipes and resource limits")
class SpecialFileScanTest(unittest.TestCase):
    def setUp(self):
        self.w = World(self, foreign=())
        self.profile = self.w.profile

    def test_claude_transcripts(self):
        """The review's reproduction: a pipe named ``<uuid>.jsonl``."""
        store = self.profile / ".claude"
        claude_transcript(store, A, "C:\\Users\\user")
        pipe = fifo(store / "projects" / "proj" / f"{B}.jsonl")
        folder = store / "projects" / "proj" / f"{C}.jsonl"
        folder.mkdir()
        summary = child_scan(self)
        self.assertEqual(summary["by_provider"]["claude"]["sessions"], 1)
        self.assertEqual(summary["candidates"], 3)
        self.assertTrue(warned(summary, pipe), summary["warnings"])
        self.assertTrue(warned(summary, folder), summary["warnings"])
        listed = json.loads(child(self, "list", "--json").stdout)
        self.assertEqual([row["id"] for row in listed], [A])
        self.assertEqual(child(self, "verify").returncode, 0)

    @unittest.skipUnless(os.path.exists("/dev/zero"), "needs /dev/zero")
    def test_a_link_to_a_device_named_like_a_transcript(self):
        """A character device that never ends, reached without being root."""
        store = self.profile / ".claude"
        claude_transcript(store, A, "C:\\Users\\user")
        link = store / "projects" / "proj" / f"{B}.jsonl"
        os.symlink("/dev/zero", link)
        summary = child_scan(self)
        self.assertEqual(summary["sessions"], 1)
        self.assertTrue(warned(summary, link), summary["warnings"])

    def test_codex_rollout_and_index(self):
        store = self.profile / ".codex"
        shutil.copytree(FIXTURES / "codex" / "windows" / ".codex", store)
        day = store / "sessions" / "2026" / "08" / "29"
        pipe = fifo(day / f"rollout-2026-08-29T15-00-00-{A}.jsonl")
        index = fifo(store / "session_index.jsonl")
        summary = child_scan(self)
        self.assertEqual(summary["by_provider"]["codex"]["sessions"], 1)
        self.assertTrue(warned(summary, pipe), summary["warnings"])
        self.assertTrue(warned(summary, index), summary["warnings"])

    def test_grok_session_files(self):
        store = self.profile / ".grok"
        shutil.copytree(FIXTURES / "grok" / "windows" / ".grok", store)
        group = store / "sessions" / "F%3A%5CProj%5CMy%20Site"
        good = group / "01a08073-6af7-7c7c-9dba-e3859e9a2ac8"
        for name in (A, B):
            shutil.copytree(good, group / name)
        summary_pipe = fifo(group / A / "summary.json")
        chat_pipe = fifo(group / B / "chat_history.jsonl")
        cwd_pipe = fifo(group / ".cwd")
        meta_pipe = fifo(group / B / "subagents" / "child" / "meta.json")
        summary = child_scan(self)
        self.assertEqual(summary["by_provider"]["grok"]["sessions"], 1)
        for pipe in (summary_pipe, chat_pipe, meta_pipe):
            self.assertTrue(warned(summary, pipe), (pipe, summary["warnings"]))
        self.assertFalse(warned(summary, cwd_pipe))  # info.cwd is there: .cwd was not needed

    def test_antigravity_databases_and_side_files(self):
        store = self.profile / ".gemini" / "antigravity-cli"
        shutil.copytree(FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli", store)
        before = child_scan(self)["by_provider"]["antigravity"]["sessions"]
        self.assertGreater(before, 0)
        db = fifo(store / "conversations" / f"{A}.db")
        summary = child_scan(self)
        self.assertEqual(summary["by_provider"]["antigravity"]["sessions"], before)
        self.assertTrue(warned(summary, db), summary["warnings"])
        # Now every side file an adapter reads is a pipe as well.
        pipes = [
            fifo(store / "conversation_summaries.db"),
            fifo(store / "annotations" / f"{AGY_WIN}.pbtxt"),
            fifo(store / "cache" / "last_conversations.json"),
            fifo(store / "brain" / AGY_WIN / ".system_generated" / "logs" / "transcript.jsonl"),
        ]
        summary = child_scan(self)
        self.assertGreater(summary["by_provider"]["antigravity"]["sessions"], 0)
        for pipe in pipes:
            self.assertTrue(warned(summary, pipe), (pipe, summary["warnings"]))

    def test_gemini_chat_and_project_files(self):
        store = self.profile / ".gemini"
        shutil.copytree(FIXTURES / "gemini" / "windows" / ".gemini", store)
        chats = store / "tmp" / "my-site" / "chats"
        pipe = fifo(chats / "session-2026-08-31T10-00-aaaaaaaa.jsonl")
        root_pipe = fifo(store / "tmp" / "my-site" / ".project_root")
        summary = child_scan(self)
        self.assertEqual(summary["by_provider"]["gemini"]["sessions"], 1)
        self.assertTrue(warned(summary, pipe), summary["warnings"])
        self.assertTrue(warned(summary, root_pipe), summary["warnings"])
        listed = json.loads(child(self, "list", "--json").stdout)
        self.assertEqual(
            [(row["id"], row["cwd"]) for row in listed], [(GEMINI_WIN, "f:\\proj\\my site")]
        )
        fifo(store / "projects.json")  # the fallback for the folder, a pipe too
        summary = child_scan(self)
        self.assertTrue(warned(summary, store / "projects.json"), summary["warnings"])

    def test_a_file_that_became_a_pipe_after_the_scan(self):
        """``show`` and ``import`` read the files again."""
        store = self.profile / ".claude"
        path = claude_transcript(store, A, "F:\\Proj\\My Site")
        self.assertEqual(child_scan(self)["sessions"], 1)
        fifo(path)
        shown = child(self, "show", A)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("Traceback", shown.stderr)
        for argv in (("import", A, "--dry-run"), ("import", A)):
            done = child(self, *argv)
            self.assertEqual(done.returncode, 1, argv)
            self.assertIn("is not a regular file", done.stderr)
            self.assertNotIn("Traceback", done.stderr)
        self.assertFalse((self.w.home / ".claude").exists())


# --- lines and files of any size ----------------------------------------------


class ReadLinesTest(TempCase):
    def lines(self, data: bytes, limit: int, piece: int = 8) -> list:
        with mock.patch.object(base, "_PIECE", piece):
            return list(base.read_lines(io.BytesIO(data), limit))

    def test_same_lines_as_a_plain_read(self):
        data = b"a\n\nbb\r\n" + b"c" * 50 + b"\n" + b"last without a line break"
        expected = io.BytesIO(data).readlines()
        for piece in (1, 2, 7, 8, 64, 1024):
            self.assertEqual(self.lines(data, 1000, piece), expected, piece)
        self.assertEqual(self.lines(b"", 10), [])
        self.assertEqual(self.lines(b"\n", 10), [b"\n"])
        self.assertEqual(self.lines(b"x", 10), [b"x"])

    def test_a_line_over_the_limit_is_none_and_the_next_is_read(self):
        long = b"x" * 100
        data = b"one\n" + long + b"\n" + b"two\n" + long
        for piece in (1, 8, 99, 100, 101, 4096):
            self.assertEqual(self.lines(data, 20, piece), [b"one\n", None, b"two\n", None], piece)

    def test_the_limit_counts_the_line_break(self):
        self.assertEqual(self.lines(b"12345678\n", 9), [b"12345678\n"])
        self.assertEqual(self.lines(b"123456789\n", 9), [None])
        self.assertEqual(self.lines(b"123456789", 9), [b"123456789"])
        self.assertEqual(self.lines(b"1234567890", 9), [None])

    def test_a_long_line_is_dropped_piece_by_piece(self):
        """Nothing of an over-long line is kept once it is over the limit."""

        class Counting(io.BytesIO):
            asked: list = []

            def readline(self, size=-1):
                type(self).asked.append(size)
                return super().readline(size)

        with mock.patch.object(base, "_PIECE", 16):
            got = list(base.read_lines(Counting(b"y" * 1000 + b"\nz\n"), 64))
        self.assertEqual(got, [None, b"z\n"])
        self.assertEqual(set(Counting.asked), {16})

    def test_iter_jsonl_skips_and_warns_once(self):
        path = self.tmp / "t.jsonl"
        big = json.dumps({"n": 2, "pad": "x" * 500})
        path.write_text(f'{{"n":1}}\n{big}\n{{"n":3}}\n{big}\n{{"n":4}}', encoding="utf-8")
        with mock.patch.object(base, "JSONL_LINE_MAX", 100), collect_warnings() as seen:
            self.assertEqual([r["n"] for r in iter_jsonl(path)], [1, 3, 4])
        self.assertEqual(sorted(set(seen.items)), [f"{path}: a line longer than 100 B was skipped"])
        with collect_warnings() as seen:
            self.assertEqual([r["n"] for r in iter_jsonl(path)], [1, 2, 3, 2, 4])
        self.assertEqual(seen.items, [])

    def test_the_limit_is_far_above_any_real_line(self):
        self.assertGreaterEqual(base.JSONL_LINE_MAX, 6 * 2_740_000)  # FORMATS: 2.74 MB
        # The densest real line holds 29,405 brackets and commas (in 131 kB).
        self.assertGreaterEqual(base.JSONL_STRUCTURE_MAX, 30 * 29_405)


@unittest.skipUnless(POSIX, "needs resource limits")
class HugeLineTest(unittest.TestCase):
    """The review's reproduction: one line of 200 MB."""

    LINE = 200 * MB
    CEILING_KB = 100 * 1024  # peak resident memory of the scanning process

    DRIVER = (
        "import json, resource, sys\n"
        f"sys.path.insert(0, {str(REPO / 'src')!r})\n"
        "from agent_sessions import api\n"
        "summary = api.scan(deep=True)\n"
        "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "print(json.dumps({'peak_kb': peak, 'sessions': summary['sessions'],\n"
        "                  'warnings': summary['warnings']}))\n"
    )

    def setUp(self):
        self.w = World(self, foreign=())

    def measure(self) -> dict:
        try:
            done = subprocess.run(
                [sys.executable, "-c", self.DRIVER],
                capture_output=True,
                text=True,
                timeout=CHILD_TIMEOUT,
                preexec_fn=_limit_memory,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("the scan did not finish")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout)

    def test_scan_memory_stays_bounded(self):
        path = claude_transcript(self.w.profile / ".claude", A, "C:\\Users\\user", prompt="hello")
        baseline = self.measure()
        self.assertEqual((baseline["sessions"], baseline["warnings"]), (1, []))
        head, tail = b'{"type":"user","message":{"role":"user","content":"', b'"}}\n'
        with open(path, "ab") as fh:
            fh.write(head)
            for _ in range(self.LINE // MB):
                fh.write(b"A" * MB)
            fh.write(tail)
            fh.write(b'{"type":"assistant","message":{"id":"m2","content":[]}}\n')
        self.assertGreater(os.path.getsize(path), self.LINE)
        result = self.measure()
        self.assertEqual(result["sessions"], 1)
        self.assertTrue(warned(result, path, "a line longer than"), result["warnings"])
        self.assertLess(result["peak_kb"], self.CEILING_KB, result)
        self.assertLess(result["peak_kb"] * 1024, self.LINE // 2)
        # The session is still the one its other lines describe.
        index = api.load_index()
        (session,) = index.sessions
        self.assertEqual((session.title, session.messages), ("hello", 3))


class BoundedTextTest(TempCase):
    def test_a_title_is_made_from_the_start_of_its_text(self):
        self.assertEqual(clean_title("word " * 5_000_000), ("word " * 16).strip())
        self.assertEqual(clean_title("x" * 10_000_000), "x" * 80)
        late = " " * base.TITLE_SOURCE_MAX + "late"
        self.assertEqual(clean_title(late), "")
        self.assertEqual(clean_title(late[1:]), "l")

    def test_claude_keeps_only_the_start_of_a_prompt(self):
        store = self.tmp / ".claude"
        long = "fix the bug " + "x" * (2 * MB)
        path = claude_transcript(store, A, "/home/u", prompt=long)
        write_jsonl(self.tmp / "extra.jsonl", [{"type": "ai-title", "aiTitle": "t" * MB}])
        with open(path, "ab") as fh:
            fh.write((self.tmp / "extra.jsonl").read_bytes())
        scan = claude._scan(path)
        self.assertEqual(len(scan.first_prompt), base.TITLE_SOURCE_MAX)
        self.assertEqual(len(scan.titles["ai-title"]), base.TITLE_SOURCE_MAX)
        (session,) = claude.Claude().list_sessions(Store("claude", str(store), "foreign"))
        self.assertEqual(session.title, "t" * 80)
        # The transcript itself is not cut.
        (turn, _reply) = claude.Claude().transcript(session)
        self.assertEqual(turn.text, long)

    def test_codex_listing_does_not_keep_the_text_of_every_turn(self):
        store = self.tmp / ".codex"
        thread = "01a04ca1-6e27-7817-9f54-07105f962073"
        name = f"rollout-2026-08-29T13-57-23-{thread}.jsonl"
        records = [
            {"timestamp": "2026-08-29T08:31:14Z", "type": "session_meta", "payload": {"id": thread}}
        ]
        for number in range(20):
            kind = "UserMessage" if number % 2 == 0 else "AgentMessage"
            item = {
                "type": kind,
                "content": [{"type": "text", "text": f"turn {number} " + "y" * MB}],
            }
            records.append(
                {
                    "type": "event_msg",
                    "ordinal": number + 1,
                    "payload": {"type": "item_completed", "item": item},
                }
            )
        path = write_jsonl(store / "sessions" / "2026" / "08" / "29" / name, records)
        listing = codex._read(str(path), titles=True)
        self.assertEqual(len(listing.turns), 20)
        self.assertTrue(all(len(turn.text) <= base.TITLE_MAX for _ordinal, turn in listing.turns))
        (session,) = Codex().list_sessions(Store("codex", str(store), "foreign"))
        self.assertEqual((session.title, session.messages), (("turn 0 " + "y" * 80)[:80], 20))
        turns = list(Codex().transcript(session))
        self.assertEqual(len(turns[0].text), len("turn 0 ") + MB)


class SizeCapTest(TempCase):
    """Files that are read whole have a size they are not read beyond; the
    limits are lowered here instead of writing files of many megabytes."""

    def sessions(self, provider, root):
        with collect_warnings() as seen:
            found = provider.list_sessions(Store(provider.name, str(root), "foreign"))
        return found, seen.items

    def test_grok_summary(self):
        root = self.tmp / ".grok"
        shutil.copytree(FIXTURES / "grok" / "windows" / ".grok", root)
        with mock.patch.object(grok, "_SMALL_FILE", 100):
            found, warnings = self.sessions(Grok(), root)
        self.assertEqual(found, [])
        self.assertEqual(len(warnings), 2)  # both sessions of the fixture
        for warning in warnings:
            self.assertTrue(warning.endswith("summary.json: summary.json skipped: file too large"))

    def test_grok_cwd_file(self):
        root = self.tmp / ".grok"
        shutil.copytree(FIXTURES / "grok" / "windows" / ".grok", root)
        group = root / "sessions" / "F%3A%5CProj%5CMy%20Site"
        (group / ".cwd").write_text("F:\\Proj\\My Site" + " " * 200, encoding="utf-8")
        self.assertEqual(Grok._group_cwd(group), ("F:\\Proj\\My Site", ".cwd"))
        with mock.patch.object(grok, "_SMALL_FILE", 100), collect_warnings() as seen:
            self.assertEqual(Grok._group_cwd(group), ("F:\\Proj\\My Site", "group directory"))
        self.assertEqual(seen.items, [f"{group / '.cwd'}: larger than 100 B; not read"])

    def test_gemini_chat_and_project_root(self):
        root = self.tmp / ".gemini"
        shutil.copytree(FIXTURES / "gemini" / "windows" / ".gemini", root)
        found, warnings = self.sessions(Gemini(), root)
        self.assertEqual(([s.id for s in found], warnings), ([GEMINI_WIN], []))
        with mock.patch.object(gemini, "_CHAT_MAX", 200):
            found, warnings = self.sessions(Gemini(), root)
        self.assertEqual(found, [])
        self.assertEqual(len(warnings), 2)  # both chat files of the fixture
        self.assertTrue(
            all(w.endswith(": larger than 200 B; not read") for w in warnings), warnings
        )
        with mock.patch.object(gemini, "_SMALL_FILE", 4):
            found, warnings = self.sessions(Gemini(), root)
        self.assertEqual([(s.id, s.cwd) for s in found], [(GEMINI_WIN, None)])
        self.assertEqual(len([w for w in warnings if w.endswith("larger than 4 B; not read")]), 2)

    def test_codex_thread_name_index(self):
        root = self.tmp / ".codex"
        shutil.copytree(FIXTURES / "codex" / "native" / ".codex", root)
        found, warnings = self.sessions(Codex(), root)
        named = {s.id: s.title for s in found}
        self.assertEqual(warnings, [])
        with mock.patch.object(codex, "_INDEX_MAX", 10):
            found, warnings = self.sessions(Codex(), root)
            self.assertIsNone(codex._index_line(str(root / "session_index.jsonl"), found[0].id))
        self.assertEqual(sorted(s.id for s in found), sorted(named))
        self.assertNotEqual({s.id: s.title for s in found}, named)  # the index gave the names
        self.assertEqual(
            sorted(set(warnings)),
            [f"{root / 'session_index.jsonl'}: larger than 10 B; thread names not read"],
        )
        # A line of the index that is too long is not taken as a name line.
        thread = found[0].id
        line = json.dumps({"id": thread, "thread_name": "n" * 500})
        (root / "session_index.jsonl").write_text(line + "\n", encoding="utf-8")
        self.assertEqual(
            codex._index_line(str(root / "session_index.jsonl"), thread), line.encode()
        )
        with mock.patch.object(codex, "_INDEX_LINE_MAX", 100):
            self.assertIsNone(codex._index_line(str(root / "session_index.jsonl"), thread))

    def test_antigravity_annotation_and_cache(self):
        root = self.tmp / "antigravity-cli"
        shutil.copytree(FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli", root)
        annotation = root / "annotations" / f"{AGY_WIN}.pbtxt"
        cache = root / "cache" / "last_conversations.json"
        self.assertTrue(agy_mod._pbtxt_title(annotation))
        with mock.patch.object(agy_mod, "_SMALL_FILE", 4), collect_warnings() as seen:
            self.assertEqual(agy_mod._pbtxt_title(annotation), "")
            self.assertIsNone(Antigravity._last_cwd(cache, "x"))
        self.assertEqual(
            seen.items,
            [f"{annotation}: larger than 4 B; not read", f"{cache}: larger than 4 B; not read"],
        )

    def test_codex_rewrite_copies_an_over_long_line_as_it_is(self):
        meta = json.dumps({"type": "session_meta", "payload": {"cwd": "F:\\x", "pad": "p" * 300}})
        short = json.dumps({"type": "session_meta", "payload": {"cwd": "F:\\x"}})
        data = f"{short}\n{meta}\n{short}\n{meta}".encode()
        table = mock.Mock()
        table.to_local.side_effect = lambda value, mount: (
            ("/mnt/x", True) if value == "F:\\x" else (None, False)
        )
        rewrite = codex._CwdRewrite(table, "/m")
        with mock.patch.object(codex, "JSONL_LINE_MAX", 100):
            out = b"".join(rewrite.apply(io.BytesIO(data)))
        lines = out.split(b"\n")
        self.assertEqual(json.loads(lines[0])["payload"]["cwd"], "/mnt/x")
        self.assertEqual(lines[1], meta.encode())  # too long to be read as a record
        self.assertEqual(json.loads(lines[2])["payload"]["cwd"], "/mnt/x")
        self.assertEqual(lines[3], meta.encode())
        self.assertEqual(len(lines), 4)


# --- JSON nested without end --------------------------------------------------


class DeepJsonTest(TempCase):
    """Two depths: one no parser accepts, and one a parser may accept while
    code that walks the result would run out of stack."""

    DEPTHS = (3_000, 200_000)

    @staticmethod
    def nested(depth: int, inner: str = "1") -> str:
        return "[" * depth + inner + "]" * depth

    def test_a_jsonl_line(self):
        for depth in self.DEPTHS:
            path = self.tmp / f"{depth}.jsonl"
            path.write_text(
                f'{{"n":1}}\n{{"n":2,"deep":{self.nested(depth)}}}\n{{"n":3}}\n', encoding="utf-8"
            )
            numbers = [record["n"] for record in iter_jsonl(path)]
            self.assertIn(numbers, ([1, 3], [1, 2, 3]))

    def test_gemini_message_content(self):
        for depth in self.DEPTHS:
            root = self.tmp / str(depth) / ".gemini"
            path = root / "tmp" / "p" / "chats" / "session-2026-08-30T10-00-aaaaaaaa.jsonl"
            path.parent.mkdir(parents=True)
            deep = self.nested(depth, '{"text":"hidden"}')
            path.write_text(
                f'{{"sessionId":"{A}","projectHash":"h"}}\n'
                f'{{"id":"m1","type":"user","content":{deep}}}\n'
                '{"id":"m2","type":"user","content":"plain prompt"}\n'
                f'{{"id":"m3","type":"gemini","content":{deep}}}\n',
                encoding="utf-8",
            )
            store = Store("gemini", str(root), "foreign")
            (session,) = Gemini().list_sessions(store)
            self.assertEqual(session.title, "plain prompt")
            turns = list(Gemini().transcript(session))
            self.assertEqual([turn.text for turn in turns], ["plain prompt"])
        self.assertEqual(gemini._content_string(json.loads(self.nested(10, '{"text":"t"}'))), "t")
        self.assertEqual(gemini._plain_text(json.loads(self.nested(10, '{"text":"t"}'))), "t")
        self.assertEqual(gemini._content_string(json.loads(self.nested(50, '{"text":"t"}'))), "")

    def test_grok_sub_agent_meta(self):
        value: object = A
        for _ in range(5_000):
            value = {"child": [value]}
        self.assertFalse(grok._names_session(value, A))
        self.assertTrue(grok._names_session({"a": [{"session": A}]}, A))
        self.assertFalse(grok._names_session({"parent_session": A}, A))

    def test_rewrites_fail_as_an_import_failure(self):
        for depth in self.DEPTHS:
            summary = self.tmp / f"summary-{depth}.json"
            summary.write_text(
                f'{{"info":{{"id":"{A}","cwd":"F:\\\\x","deep":{self.nested(depth)}}},'
                f'"grok_home":"C:\\\\g","more":{self.nested(depth)}}}',
                encoding="utf-8",
            )
            rollout = self.tmp / f"rollout-{depth}.jsonl"
            rollout.write_text(
                f'{{"type":"session_meta","payload":{{"cwd":"F:\\\\x","deep":{self.nested(depth)}}}}}\n',
                encoding="utf-8",
            )
            table = mock.Mock()
            table.to_local.return_value = ("/mnt/x", True)
            for rewrite, path in (
                (grok._SummaryRewrite("/mnt/x", "/home/u/.grok"), summary),
                (codex._CwdRewrite(table, "/m"), rollout),
            ):
                try:
                    changes = rewrite.preview(path)
                except importer.ImportFailed as exc:
                    self.assertIn("cannot be rewritten", exc.reason)
                else:
                    self.assertIsInstance(changes, list)  # read, or left as it is
                target = self.tmp / f"out-{depth}-{path.name}"
                item = importer.FileCopy(str(path), "", path.stat().st_size, rewrite)
                try:
                    importer._copy(item, str(target), lambda count: None)
                except importer.ImportFailed as exc:
                    self.assertIn("cannot be rewritten", exc.reason)

    def test_our_own_files(self):
        """The cache, the path-map override, the import journal and the
        import record from before it are ours, but they may be damaged."""
        deep = self.nested(200_000)
        env = {
            "AGENT_SESSIONS_HOME": str(self.tmp / "home"),
            "AGENT_SESSIONS_STATE": str(self.tmp / "state"),
        }
        config = self.tmp / "home" / ".config" / "agent-sessions"
        config.mkdir(parents=True)
        (config / "pathmap.json").write_text(deep, encoding="utf-8")
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "imports.json").write_text(deep, encoding="utf-8")
        (self.tmp / "state" / "imports.jsonl").write_text(
            deep + '\n{"record":"import","id":"kept"}\n', encoding="utf-8"
        )
        with mock.patch.dict(os.environ, env):
            pathmap = PathMap([], None)
            self.assertEqual(len(pathmap.warnings), 1)
            self.assertIn("pathmap.json: ignored", pathmap.warnings[0])
            with self.assertLogs("agent_sessions", "WARNING") as logged:
                self.assertEqual(importer.read_journal(), [{"record": "import", "id": "kept"}])
            self.assertEqual(len(logged.output), 2)
            self.assertIn("imports.json cannot be read", logged.output[0])
            self.assertIn("line 1 is not a record", logged.output[1])
            self.assertEqual(journal.read(), importer.read_journal())


class TimingTest(unittest.TestCase):
    def test_reading_many_short_lines_is_not_slower_than_before(self):
        """The bounded reader is on the path of every line of every scan."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_bytes(b'{"n":1,"text":"some words here"}\n' * 200_000)
            started = time.perf_counter()
            self.assertEqual(sum(1 for _ in iter_jsonl(path)), 200_000)
            self.assertLess(time.perf_counter() - started, 5)


if __name__ == "__main__":
    unittest.main()
