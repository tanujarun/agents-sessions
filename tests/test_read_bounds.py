"""What one file can cost a scan (docs/AUDIT.md A-02 and A-16): a byte
budget per file, a cancel that is seen inside a file, and a bound on what a
line costs to parse, not only on its bytes.

The audit's reproductions (``b/t09_bounds.py``, ``b/t09b_sparse_hang.py``,
``b/t12_sqlite.py``): a sparse file named like a transcript kept a scan
reading for days; one line of 31 MiB of ``[],`` took 918 MB to parse; a
300 MB value in a database took 625 MB. The scans that could run for ever or
fill the machine's memory on the old code run in a child process under a
timeout; the child reports its own peak (``VmHWM``, which starts again with
every program a process becomes)."""

import errno
import io
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import AGY_WIN, World
from support import (
    CHILD_TIMEOUT,
    FIXTURES,
    claude_transcript,
    finish,
    launcher_child,
    python_child,
    write_jsonl,
)

from agent_sessions import api
from agent_sessions.model import Store
from agent_sessions.providers import base
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import collect_warnings, iter_jsonl
from agent_sessions.providers.claude import Claude

A = "aaaaaaaa-0000-4000-8000-0000000000aa"
MIB = 1024 * 1024
GIB = 1024 * MIB
PROC = os.path.exists("/proc/self/status")
# A scan in a child that prints what it found and what it cost.
DRIVER = """
import json, time
from agent_sessions import api
started = time.monotonic()
summary = api.scan(deep=True)
peak = [line for line in open("/proc/self/status") if line.startswith("VmHWM")][0].split()[1]
print(json.dumps({"peak_kb": int(peak), "sessions": summary["sessions"],
                  "seconds": time.monotonic() - started, "warnings": summary["warnings"]}))
"""
PEAK_MAX_KB = 300 * 1024  # what a scan may hold at its worst, whatever one file holds


def sparse(path, size: int) -> bool:
    """Give ``path`` a hole up to ``size``; False where the filesystem
    cannot hold a file that long."""
    try:
        os.truncate(path, size)
    except OSError as exc:
        if exc.errno in (errno.EFBIG, errno.EINVAL, errno.ENOSPC):
            return False
        raise
    return os.stat(path).st_blocks * 512 < size // 2


class ReadBudgetTest(unittest.TestCase):
    def lines(self, data: bytes, budget: int, piece: int = 8) -> tuple[list, bool]:
        got: list = []
        with mock.patch.object(base, "_PIECE", piece):
            try:
                for line in base.read_lines(io.BytesIO(data), 1000, budget):
                    got.append(line)
            except base.FileTooLong as exc:
                self.assertEqual(exc.budget, budget)
                self.assertIsInstance(exc, OSError)
                return got, True
        return got, False

    def test_a_file_within_the_budget_is_read_as_before(self):
        data = b"one\ntwo\nthree\n"
        for piece in (1, 3, 8, 4096):
            self.assertEqual(
                self.lines(data, len(data), piece), ([b"one\n", b"two\n", b"three\n"], False)
            )
            self.assertEqual(self.lines(data, 10**9, piece)[1], False)
        self.assertEqual(self.lines(b"", 0), ([], False))

    def test_reading_stops_at_the_budget(self):
        data = b"one\ntwo\nthree\nfour\n"
        for piece in (1, 3, 8, 4096):
            with self.subTest(piece=piece):
                self.assertEqual(self.lines(data, 8, piece), ([b"one\n", b"two\n"], True))
                self.assertEqual(self.lines(data, 9, piece), ([b"one\n", b"two\n"], True))
                self.assertEqual(self.lines(data, len(data) - 1, piece)[1], True)
                self.assertEqual(self.lines(data, 0, piece), ([], True))

    def test_no_more_than_the_budget_and_one_byte_is_asked_for(self):
        class Counting(io.BytesIO):
            taken = 0

            def readline(self, size=-1):
                data = super().readline(size)
                type(self).taken += len(data)
                return data

        with self.assertRaises(base.FileTooLong):
            list(base.read_lines(Counting(b"x" * 10_000), 100, 1000))
        self.assertEqual(Counting.taken, 1001)

    def test_iter_jsonl_lists_what_was_read_and_warns_once(self):
        with (
            self.subTest("a file longer than the budget"),
            mock.patch.object(base, "FILE_READ_MAX", 40),
        ):
            path = Path(self.enterContext(TempDir())) / "t.jsonl"
            path.write_text('{"n":1}\n{"n":2}\n{"n":3}\n' + '{"n":4}\n' * 20, encoding="utf-8")
            with collect_warnings() as seen:
                self.assertEqual([r["n"] for r in iter_jsonl(path)], [1, 2, 3, 4, 4])
            self.assertEqual(
                seen.items, [f"{path}: longer than 40 B; only the first 40 B were read"]
            )
            path.write_text('{"n":1}\n' * 5, encoding="utf-8")  # 40 bytes exactly
            with collect_warnings() as seen:
                self.assertEqual(len(list(iter_jsonl(path))), 5)
            self.assertEqual(seen.items, [])

    def test_the_budget_is_two_gibibytes(self):
        self.assertEqual(base.FILE_READ_MAX, 2 * GIB)


class TempDir:
    """``tempfile.TemporaryDirectory`` under a shorter name, for ``enterContext``."""

    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *exc):
        self._tmp.cleanup()


class CancelContextTest(unittest.TestCase):
    def test_a_set_event_ends_a_read_within_a_few_megabytes(self):
        event = threading.Event()
        taken = []

        class Endless(io.RawIOBase):
            def readline(self, size=-1):
                taken.append(size)
                if len(taken) == 10:
                    event.set()
                return b"x" * size

        with (
            mock.patch.object(base, "_PIECE", 1024),
            mock.patch.object(base, "_CHECK_EVERY", 4096),
            base.cancellable(event),
            self.assertRaises(base.Cancelled),
        ):
            list(base.read_lines(Endless(), 100, 10**12))
        self.assertLessEqual(len(taken), 10 + 4)
        self.assertFalse(issubclass(base.Cancelled, Exception))  # no reader swallows it

    def test_outside_the_block_and_in_another_thread_nothing_is_cancelled(self):
        event = threading.Event()
        event.set()
        data = b"line\n" * 10_000
        with mock.patch.object(base, "_CHECK_EVERY", 16):
            self.assertEqual(len(list(base.read_lines(io.BytesIO(data)))), 10_000)
            with base.cancellable(event):
                with base.cancellable(None):  # an inner block of its own
                    self.assertEqual(len(list(base.read_lines(io.BytesIO(data)))), 10_000)
                with self.assertRaises(base.Cancelled):
                    list(base.read_lines(io.BytesIO(data)))
                seen = []
                other = threading.Thread(
                    target=lambda: seen.append(len(list(base.read_lines(io.BytesIO(data)))))
                )
                other.start()
                other.join()
                self.assertEqual(seen, [10_000])
            base.check_cancelled()  # the block is over
            self.assertEqual(len(list(base.read_lines(io.BytesIO(data)))), 10_000)

    def test_the_load_of_a_database_is_cancelled_too(self):
        source = FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli"
        path = source / "conversations" / f"{AGY_WIN}.db"
        event = threading.Event()
        event.set()
        with (
            mock.patch.object(base, "_PIECE", 512),
            mock.patch.object(base, "_CHECK_EVERY", 1024),
            base.cancellable(event),
            self.assertRaises(base.Cancelled),
        ):
            base.open_sqlite_ro(path)
        base.open_sqlite_ro(path).close()


@unittest.skipUnless(os.name == "posix", "sparse files")
class SparseFileTest(unittest.TestCase):
    """AUDIT A-02: after one real record, a hole of any size."""

    def setUp(self):
        self.w = World(self, foreign=())
        self.path = claude_transcript(self.w.profile / ".claude", A, "C:\\x", prompt="hello")

    def hole(self, size: int) -> None:
        if not sparse(self.path, size):
            self.skipTest(f"this filesystem holds no sparse file of {size:,} bytes")
        self.assertEqual(os.path.getsize(self.path), size)

    def scan_in_a_child(self, *argv) -> tuple[float, dict]:
        started = time.monotonic()
        ((code, out, err),) = finish(self, [launcher_child(*argv)], timeout=CHILD_TIMEOUT)
        self.assertEqual(code, 0, err[-2000:])
        return time.monotonic() - started, json.loads(out)

    def test_a_scan_ends_within_seconds_and_says_so(self):
        for size in (16 * GIB, 1024**5):  # the audit's: 4 s, and three days
            with self.subTest(size=size):
                self.hole(size)
                took, summary = self.scan_in_a_child("scan", "--deep", "--json")
                self.assertLess(took, 20)
                self.assertEqual(summary["sessions"], 1)
                mine = [w for w in summary["warnings"] if str(self.path) in w]
                self.assertEqual(
                    mine, [f"{self.path}: longer than 2.1 GB; only the first 2.1 GB were read"]
                )
        # The session is listed from what was read, and a scan that finds
        # the file unchanged does not read it again.
        (session,) = api.load_index().sessions
        self.assertEqual((session.id, session.title, session.messages), (A, "hello", 2))
        took, summary = self.scan_in_a_child("scan", "--json")
        self.assertEqual((summary["parsed"], summary["cached"]), (0, 1))
        self.assertLess(took, 5)

    def test_list_and_show_end_too(self):
        self.hole(1024**5)
        started = time.monotonic()
        children = [launcher_child("list", "--json")]  # no cache yet: it scans by itself
        ((code, out, err),) = finish(self, children, timeout=CHILD_TIMEOUT)
        self.assertEqual((code, [row["id"] for row in json.loads(out)]), (0, [A]), err)
        ((code, out, err),) = finish(self, [launcher_child("show", A)], timeout=CHILD_TIMEOUT)
        self.assertEqual(code, 0, err)
        self.assertIn("hello", out)
        self.assertLess(time.monotonic() - started, 30)

    @unittest.skipUnless(PROC, "needs /proc")
    def test_it_costs_no_memory(self):
        self.hole(16 * GIB)
        ((code, out, err),) = finish(self, [python_child(DRIVER)], timeout=CHILD_TIMEOUT)
        self.assertEqual(code, 0, err[-2000:])
        self.assertLess(json.loads(out)["peak_kb"], 100 * 1024)

    def test_cancel_is_seen_inside_the_file(self):
        """Before, a cancel was looked at between two candidates only: the
        scan went on until the file was read. With a budget that the read
        cannot reach in this test's time, only the cancel can end it."""
        self.hole(1024**5)
        cancel = threading.Event()
        done: list = []

        def run():
            with mock.patch.object(base, "FILE_READ_MAX", 64 * GIB):
                done.append(api.scan(deep=False, cancel=cancel))
            done.append(time.monotonic())

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(0.5)
        self.assertEqual(done, [])  # it is reading the hole
        asked = time.monotonic()
        cancel.set()
        worker.join(timeout=CHILD_TIMEOUT)
        self.assertFalse(worker.is_alive(), "the scan did not end")
        summary, ended = done
        self.assertLess(ended - asked, 0.5)
        self.assertTrue(summary["cancelled"])
        self.assertEqual((summary["sessions"], summary["parsed"]), (0, 0))
        # Nothing was cached of the read that was cut short: the next scan
        # reads the file, to the budget this time.
        os.truncate(self.path, os.path.getsize(self.path) - 1024**5 + 8 * MIB)
        summary = api.scan(deep=False)
        self.assertEqual(
            (summary["cancelled"], summary["sessions"], summary["parsed"]), (False, 1, 1)
        )

    def test_a_cancel_keeps_what_the_cache_had(self):
        api.scan(deep=True)
        self.assertEqual(len(api.load_index().sessions), 1)
        self.hole(1024**5)  # the file changed: its cached parse no longer stands
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with mock.patch.object(base, "FILE_READ_MAX", 64 * GIB):
            summary = api.scan(deep=False, cancel=cancel)
        self.assertTrue(summary["cancelled"])
        (session,) = api.load_index().sessions  # as it was read before
        self.assertEqual((session.id, session.title), (A, "hello"))


class DenseLineTest(unittest.TestCase):
    """AUDIT A-16: 31 MiB of ``[],`` in one line is ten million lists."""

    def test_the_count_is_of_brackets_braces_and_commas(self):
        with mock.patch.object(base, "JSONL_STRUCTURE_MAX", 10):
            for document, dense in (
                (b"[" * 11, True),
                (b"{" * 11, True),
                (b"," * 11, True),
                (b"[{," * 4, True),
                (b"[{," * 3 + b"x" * 50, False),
                (b']}:" ' * 100, False),
                (b"[" * 10 + b"x", False),
                ("[" * 11, True),
                ("," * 5 + "{" * 5 + "é" * 20, False),
            ):
                self.assertEqual(base.too_dense(document), dense, document[:20])
        # A document no longer than the limit cannot pass it: not counted.
        self.assertFalse(base.too_dense(b"," * base.JSONL_STRUCTURE_MAX))
        self.assertTrue(base.too_dense(b"," * (base.JSONL_STRUCTURE_MAX + 1)))

    def test_iter_jsonl_skips_such_a_line_and_warns(self):
        tmp = Path(self.enterContext(TempDir()))
        path = tmp / "t.jsonl"
        dense = json.dumps({"n": 2, "pad": [[] for _ in range(40)]})
        prose = json.dumps({"n": 5, "text": "a sentence, with commas, " * 3})
        path.write_text(f'{{"n":1}}\n{dense}\n{{"n":3}}\n{dense}\n{prose}\n', encoding="utf-8")
        with mock.patch.object(base, "JSONL_STRUCTURE_MAX", 30), collect_warnings() as seen:
            self.assertEqual([r["n"] for r in iter_jsonl(path)], [1, 3, 5])
        self.assertEqual(
            sorted(set(seen.items)),
            [f"{path}: a line with more than 30 brackets and commas was skipped"],
        )
        with collect_warnings() as seen:
            self.assertEqual([r["n"] for r in iter_jsonl(path)], [1, 2, 3, 2, 5])
        self.assertEqual(seen.items, [])

    def test_a_whole_document_is_held_to_it_as_well(self):
        document = b'{"projects": {' + b'"k":[],' * 20 + b'"z": 1}}'
        self.assertEqual(base.load_json(document)["projects"]["z"], 1)
        self.assertEqual(base.load_json(b"\xef\xbb\xbf[1]"), [1])  # a byte order mark
        self.assertEqual(base.load_json("[1]"), [1])
        with mock.patch.object(base, "JSONL_STRUCTURE_MAX", 30):
            for form in (document, document.decode()):
                with self.assertRaises(base.TooDense) as caught:
                    base.load_json(form)
                self.assertIsInstance(caught.exception, ValueError)
            # A reader takes it as it takes any file that is not JSON.
            tmp = Path(self.enterContext(TempDir()))
            (tmp / ".claude" / "projects").mkdir(parents=True)
            (tmp / ".claude.json").write_bytes(document)
            store = Store("claude", str(tmp / ".claude"), "foreign")
            self.assertEqual(Claude().path_evidence(store), [])
        self.assertEqual(Claude().path_evidence(store), ["k", "z"])

    def test_no_reader_parses_a_whole_file_around_it(self):
        """Every ``json.loads`` of the readers is of one line of a file that
        ``read_lines`` cut to size, or of text this program made itself."""
        allowed = {
            "base.py": 2,  # iter_jsonl, and load_json itself
            "claude.py": 1,  # the first line of a transcript, of at most 64 kB
            "codex.py": 5,  # an index line of at most 64 kB, read and imported; three in the import rewrite
            "grok.py": 3,  # the import rewrite
        }
        root = Path(base.__file__).parent
        found = {
            path.name: path.read_text(encoding="utf-8").count("json.loads(")
            for path in sorted(root.glob("*.py"))
        }
        self.assertEqual({name: count for name, count in found.items() if count}, allowed)

    def test_the_real_shapes_are_far_below_the_limit(self):
        """A line as long as the longest real one (2.74 MB of a tool's
        output: text with commas in it) is read."""
        tmp = Path(self.enterContext(TempDir()))
        text = "a line of code, with (commas, brackets) and [lists], {dicts};\n" * 45_000
        self.assertGreater(len(text), 2_740_000)
        path = write_jsonl(tmp / "t.jsonl", [{"n": 1, "text": text}])
        with collect_warnings() as seen:
            self.assertEqual([r["n"] for r in iter_jsonl(path)], [1])
        self.assertEqual(seen.items, [])
        self.assertLess(text.count(",") + text.count("[") + text.count("{"), 300_000)


@unittest.skipUnless(PROC and os.name == "posix", "measures the scanning process (VmHWM)")
class MemoryTest(unittest.TestCase):
    """The peak of a real scan, in a process of its own, for the dearest
    things one file can hold."""

    def setUp(self):
        self.w = World(self, foreign=())
        self.path = claude_transcript(self.w.profile / ".claude", A, "C:\\x", prompt="hello")

    def measure(self) -> dict:
        ((code, out, err),) = finish(self, [python_child(DRIVER)], timeout=CHILD_TIMEOUT)
        self.assertEqual(code, 0, err[-2000:])
        result = json.loads(out)
        self.assertEqual(result["sessions"], 1)
        return result

    def append(self, body: bytes) -> None:
        with open(self.path, "ab") as fh:
            fh.write(b'{"type":"assistant","message":{"id":"m","content":' + body + b"}}\n")

    def warned(self, result: dict, text: str) -> bool:
        return any(str(self.path) in w and text in w for w in result["warnings"])

    def test_a_line_of_empty_lists_is_skipped_not_parsed(self):
        """The audit's line, as long as a line may be now: 918 MB then."""
        baseline = self.measure()["peak_kb"]
        self.append(b"[" + b"[]," * (base.JSONL_LINE_MAX // 3 - 100) + b"[]]")
        self.assertLessEqual(os.path.getsize(self.path), base.JSONL_LINE_MAX + 1000)
        result = self.measure()
        self.assertTrue(self.warned(result, "brackets and commas was skipped"), result["warnings"])
        self.assertLess(result["peak_kb"], baseline + 64 * 1024)
        self.assertLess(result["peak_kb"], PEAK_MAX_KB)

    def test_the_dearest_line_that_is_still_parsed(self):
        """A million one-key objects: the most a line may hold, in the form
        that costs the most per bracket and comma (about 100 bytes)."""
        count = base.JSONL_STRUCTURE_MAX // 2 - 100  # each has a brace and a comma
        self.append(b"[" + b'{"a":0},' * count + b"0]")
        result = self.measure()
        self.assertEqual(result["warnings"], [])
        self.assertLess(result["peak_kb"], PEAK_MAX_KB)

    def test_the_longest_line_with_text_beyond_the_first_plane(self):
        """Text with one such character takes four bytes a character once it
        is decoded, and the string that is parsed out of it as much again."""
        pad = base.JSONL_LINE_MAX - 200_000
        self.append(b'"\xf0\x9f\x98\x80' + b"x" * pad + b'"')
        result = self.measure()
        self.assertEqual(result["warnings"], [])
        self.assertLess(result["peak_kb"], PEAK_MAX_KB)

    def test_a_line_over_the_length_cap_is_dropped_as_before(self):
        self.append(b'"' + b"x" * (base.JSONL_LINE_MAX + 1) + b'"')
        result = self.measure()
        self.assertTrue(self.warned(result, "a line longer than 16.8 MB was skipped"))
        self.assertLess(result["peak_kb"], 100 * 1024)


@unittest.skipUnless(os.name == "posix", "sparse files, access times")
class LargeDatabaseTest(unittest.TestCase):
    """AUDIT A-16 (``b/t12_sqlite.py``): a conversation database of more
    than 64 MB was opened in place, and a value in it could be of any size."""

    def setUp(self):
        self.w = World(self, foreign=("antigravity",))
        self.store = self.w.profile / ".gemini" / "antigravity-cli"
        self.db = self.store / "conversations" / f"{AGY_WIN}.db"
        self.assertIn(AGY_WIN, [s.id for s in self.w.scan().sessions])
        shutil.rmtree(self.w.box.cache)

    def grow(self, megabytes: int, where: str) -> None:
        con = sqlite3.connect(self.db)
        if where == "pad":  # a table of its own: the tool's tables stay as they are
            con.execute("create table pad (x blob)")
            con.execute("insert into pad values (zeroblob(?))", (megabytes * MIB,))
        else:
            first = con.execute("select min(idx) from steps").fetchone()[0]
            con.execute(
                "update steps set metadata = zeroblob(?) where idx = ?", (megabytes * MIB, first)
            )
        con.commit()
        con.execute("pragma wal_checkpoint(truncate)")
        con.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(f"{self.db}{suffix}"):
                os.unlink(f"{self.db}{suffix}")

    def scan(self) -> dict:
        if PROC:
            ((code, out, err),) = finish(self, [python_child(DRIVER)], timeout=CHILD_TIMEOUT)
            self.assertEqual(code, 0, err[-2000:])
            return json.loads(out)
        return dict(api.scan(deep=True), peak_kb=0)

    def mine(self, result: dict) -> list[str]:
        return [w for w in result["warnings"] if str(self.db) in w]

    def test_a_value_larger_than_any_real_one_is_not_built_in_memory(self):
        baseline = self.scan()["peak_kb"]
        shutil.rmtree(self.w.box.cache)
        self.grow(40, "metadata")  # the audit's was 300 MB; the limit is 16 MiB
        before = sorted(p.name for p in self.db.parent.iterdir())
        result = self.scan()
        self.assertEqual(len(self.mine(result)), 1, result["warnings"])
        self.assertIn("conversation database not readable", self.mine(result)[0])
        self.assertIn("too big", self.mine(result)[0])
        self.assertNotIn(AGY_WIN, [s.id for s in api.load_index().sessions])
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before)
        # The file is loaded (40 MB of it, twice for a moment); the value is never made.
        self.assertLess(result["peak_kb"], baseline + 3 * 40 * 1024)
        # The adapter itself: one skipped candidate, a warning, no exception.
        with collect_warnings() as seen:
            found = Antigravity().list_sessions(Store("antigravity", str(self.store)))
        self.assertNotIn(AGY_WIN, [s.id for s in found])
        self.assertTrue(any("too big" in item for item in seen.items), seen.items)

    def test_a_database_that_is_merely_large_is_read_and_its_access_time_stays(self):
        """70 MB, as in the audit: above the old limit for a copy in memory,
        so it was opened in place and its access time moved."""
        self.grow(70, "pad")
        old = time.time() - 5 * 86400
        os.utime(self.db, (old, old + 2 * 86400))
        before = os.stat(self.db)
        names = sorted(p.name for p in self.db.parent.iterdir())
        result = self.scan()
        after = os.stat(self.db)
        self.assertEqual(self.mine(result), [])
        self.assertIn(AGY_WIN, [s.id for s in api.load_index().sessions])
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), names)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        if getattr(os, "O_NOATIME", 0):
            self.assertEqual(after.st_atime_ns, before.st_atime_ns)

    def test_a_database_larger_than_any_conversation_is_not_opened(self):
        if not sparse(self.db, base.SQLITE_MEMORY_MAX + 1):
            self.skipTest("this filesystem holds no sparse file of that size")
        old = time.time() - 5 * 86400
        os.utime(self.db, (old, old + 2 * 86400))
        before = os.stat(self.db)
        started = time.monotonic()
        result = self.scan()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(self.mine(result)), 1, result["warnings"])
        self.assertIn(
            "larger than 268.4 MB, which no conversation database is; not opened",
            self.mine(result)[0],
        )
        self.assertNotIn(AGY_WIN, [s.id for s in api.load_index().sessions])
        if PROC:
            self.assertLess(result["peak_kb"], 100 * 1024)  # nothing of it was read
        if getattr(os, "O_NOATIME", 0):
            self.assertEqual(os.stat(self.db).st_atime_ns, before.st_atime_ns)


if __name__ == "__main__":
    unittest.main()
