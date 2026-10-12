"""The state directory's lock (``fsutil.lock``) and the import journal
(``journal``): docs/REVIEW.md R-06, docs/SPEC.md "Import".

The lock and the journal are there for several processes at once, so the
tests that matter start real ones (a Python that imports the package, or our
own launcher) in the sandbox's environment."""

import errno
import json
import os
import stat
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import World
from support import Sandbox, finish, launcher_child, python_child, state_names

from agent_sessions import api, fsutil, journal

POSIX = os.name == "posix"

# Each child adds its lines one by one; the padding makes a line longer than
# what a pipe or a page would take in one piece.
APPENDER = """
from agent_sessions import journal
who, count = sys.argv[1], int(sys.argv[2])
for number in range(count):
    journal.append({"record": "test", "who": who, "n": number, "pad": "x" * 5000})
"""

# Read, add one, write back: without the lock, counts are lost.
COUNTER = """
from agent_sessions import fsutil
path, count = sys.argv[1], int(sys.argv[2])
for _ in range(count):
    with fsutil.lock():
        try:
            with open(path) as fh:
                value = int(fh.read())
        except FileNotFoundError:
            value = 0
        with open(path, "w") as fh:
            fh.write(str(value + 1))
"""

# Take the lock, say so, keep it until stdin closes.
HOLDER = """
from agent_sessions import fsutil
with fsutil.lock():
    print("held", flush=True)
    sys.stdin.read()
"""


class StateCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])

    def journal_lines(self) -> list[bytes]:
        return journal.path().read_bytes().split(b"\n")


class LockTest(StateCase):
    def setUp(self):
        super().setUp()
        self.state.mkdir()
        self.lock_path = self.state / "agent-sessions.lock"

    def test_the_lock_file_is_in_the_state_directory_and_goes_again(self):
        self.assertEqual(fsutil.lock_file(), self.lock_path)
        with fsutil.lock():
            self.assertEqual(self.lock_path.read_text(), f"{os.getpid()}\n")
            if POSIX:
                self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), 0o600)
        if POSIX:
            self.assertEqual(os.listdir(self.state), [])

    def test_the_holding_thread_may_take_it_again(self):
        with fsutil.lock(), fsutil.lock(timeout=0), fsutil.lock(timeout=0):
            self.assertTrue(self.lock_path.exists())
        with fsutil.lock(timeout=0):  # and it was released
            pass

    def test_another_thread_waits_and_then_gives_up(self):
        seen = {}

        def other():
            started = time.monotonic()
            try:
                with fsutil.lock(timeout=0.2):
                    seen["got"] = True
            except fsutil.LockTimeout as exc:
                seen["error"] = exc
            seen["waited"] = time.monotonic() - started

        with fsutil.lock():
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(10)
        self.assertNotIn("got", seen)
        self.assertGreaterEqual(seen["waited"], 0.2)
        self.assertEqual(seen["error"].pid, os.getpid())
        self.assertIsInstance(seen["error"], OSError)
        # Released: the same thread body gets it now.
        other()
        self.assertTrue(seen["got"])

    def test_another_thread_gets_it_when_it_is_released(self):
        order = []

        def other():
            with fsutil.lock(timeout=10):
                order.append("other")

        with fsutil.lock():
            thread = threading.Thread(target=other)
            thread.start()
            time.sleep(0.1)
            order.append("first")
        thread.join(10)
        self.assertEqual(order, ["first", "other"])

    def test_a_lock_held_by_another_process_is_named_in_the_error(self):
        holder = python_child(HOLDER, stdin=-1)  # -1: a pipe, closed to let it go
        try:
            self.assertEqual(holder.stdout.readline(), "held\n")
            started = time.monotonic()
            with self.assertRaises(fsutil.LockTimeout) as caught, fsutil.lock(timeout=0.3):
                self.fail("the lock was given twice")
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            self.assertEqual(caught.exception.pid, holder.pid)
            self.assertEqual(
                str(caught.exception),
                f"{self.lock_path} is locked by process {holder.pid}; gave up after 0.3 s",
            )
        finally:
            holder.stdin.close()
            ((status, _out, err),) = finish(self, [holder])
        self.assertEqual((status, err), (0, ""))
        with fsutil.lock(timeout=5):  # the holder is gone: it is free
            pass

    def test_a_lock_file_left_by_a_dead_process_does_not_block(self):
        """The file is nothing; the lock is on the open descriptor, and that
        went with the process."""
        self.lock_path.write_text("999999999\n")
        with fsutil.lock(timeout=0):
            self.assertEqual(self.lock_path.read_text(), f"{os.getpid()}\n")

    def test_without_the_directory_there_is_no_lock(self):
        os.rmdir(self.state)
        with self.assertRaises(FileNotFoundError), fsutil.lock(timeout=0):
            pass

    def test_processes_exclude_each_other(self):
        counter = self.box.root / "counter"
        children = [python_child(COUNTER, counter, 150) for _ in range(6)]
        for status, _out, err in finish(self, children):
            self.assertEqual((status, err), (0, ""))
        self.assertEqual(int(counter.read_text()), 6 * 150)
        if POSIX:
            self.assertEqual(os.listdir(self.state), [])


class PrivateDirTest(StateCase):
    def test_the_directory_is_private_and_its_parents_are_not_ours(self):
        target = self.box.root / "a" / "b" / "ours"
        made = fsutil.make_private_dir(target)
        self.assertEqual(made, [str(target.parent.parent), str(target.parent), str(target)])
        if POSIX:
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o777 & ~umask)
        self.assertEqual(fsutil.make_private_dir(target), [])
        # One that is there is left as it is: the parent made a moment ago is not private.
        before = target.parent.stat().st_mode
        self.assertEqual(fsutil.make_private_dir(target.parent), [])
        self.assertEqual(target.parent.stat().st_mode, before)

    def test_what_was_made_is_known_even_when_it_fails_half_way(self):
        blocker = self.box.root / "file"
        blocker.write_text("x")
        made: list[str] = []
        with self.assertRaises(OSError):
            fsutil.make_private_dir(blocker / "below" / "deeper", made)
        self.assertEqual(made, [])
        real = os.mkdir

        def failing(path, mode=0o777):
            if os.path.basename(path) == "c":
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real(path, mode)

        with mock.patch.object(os, "mkdir", failing), self.assertRaises(PermissionError):
            fsutil.make_private_dir(self.box.root / "a" / "b" / "c", made)
        self.assertEqual(made, [str(self.box.root / "a"), str(self.box.root / "a" / "b")])

    def test_fsync_dir_never_raises(self):
        self.assertIn(fsutil.fsync_dir(self.box.root), (True, False))
        self.assertFalse(fsutil.fsync_dir(self.box.root / "missing"))


class JournalTest(StateCase):
    def test_no_journal_no_records(self):
        self.assertEqual(journal.read(), [])
        self.assertFalse(self.state.exists())  # reading creates nothing

    def test_append_and_read(self):
        first = {"record": "import", "version": 2, "id": "a", "title": "naïve ☃"}
        second = {"record": "import", "version": 2, "id": "b", "nested": {"k": [1, None]}}
        journal.append(first)
        journal.append(second)
        self.assertEqual(journal.read(), [first, second])
        raw = journal.path().read_bytes()
        self.assertEqual(raw.count(b"\n"), 2)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertIn("naïve ☃".encode(), raw)  # text as it is, not escaped
        self.assertEqual(sorted(os.listdir(self.state)), ["imports.jsonl"])
        if POSIX:
            self.assertEqual(stat.S_IMODE(journal.path().stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_each_record_is_one_write_on_an_appending_descriptor(self):
        calls = []
        real_open, real_write = os.open, os.write

        def spy_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if os.fspath(path) == str(journal.path()):
                calls.append(("open", bool(flags & os.O_APPEND), fd))
            return fd

        def spy_write(fd, data):
            if any(kind == "open" and fd == known for kind, _appending, known in calls):
                calls.append(("write", bytes(data), fd))
            return real_write(fd, data)

        journal.append({"id": "first"})
        with (
            mock.patch.object(os, "open", spy_open),
            mock.patch.object(os, "write", spy_write),
            mock.patch.object(os, "fsync", wraps=os.fsync) as synced,
        ):
            journal.append({"id": "x", "text": "line\nbreak"})
        opens = [call for call in calls if call[0] == "open"]
        writes = [call for call in calls if call[0] == "write"]
        self.assertEqual([appending for _kind, appending, _fd in opens], [True])
        self.assertEqual(
            [data for _kind, data, _fd in writes], [b'{"id":"x","text":"line\\nbreak"}\n']
        )
        self.assertEqual([call.args[0] for call in synced.call_args_list], [opens[0][2]])

    def test_a_torn_last_line_is_skipped_with_a_warning(self):
        journal.append({"id": "whole"})
        with open(journal.path(), "ab") as fh:
            fh.write(b'{"id":"torn","files":["/a/b')  # the crash came here
        with self.assertLogs("agent_sessions", "WARNING") as logged:
            self.assertEqual(journal.read(), [{"id": "whole"}])
        self.assertEqual(len(logged.output), 1)
        self.assertIn("the last line is incomplete", logged.output[0])
        self.assertIn(str(journal.path()), logged.output[0])
        # The next record starts on a line of its own and is read.
        journal.append({"id": "next"})
        with self.assertLogs("agent_sessions", "WARNING") as logged:
            self.assertEqual(journal.read(), [{"id": "whole"}, {"id": "next"}])
        self.assertEqual(len(logged.output), 1)
        self.assertIn("line 2 is not a record", logged.output[0])
        self.assertEqual(self.journal_lines()[1], b'{"id":"torn","files":["/a/b')

    def test_lines_that_are_not_records_are_skipped(self):
        self.state.mkdir()
        journal.path().write_bytes(
            b'{"id":"a"}\n\n[1,2]\n"text"\nnot json\n\xff\xfe\n   \n{"id":"b"}\n'
        )
        with self.assertLogs("agent_sessions", "WARNING") as logged:
            self.assertEqual(journal.read(), [{"id": "a"}, {"id": "b"}])
        self.assertEqual(
            [line.rsplit(": ", 1)[1] for line in logged.output],
            [f"line {n} is not a record; skipped" for n in (3, 4, 5, 6)],
        )

    def test_a_whole_last_line_without_its_line_break_is_a_record(self):
        self.state.mkdir()
        journal.path().write_bytes(b'{"id":"a"}\n{"id":"b"}')
        with self.assertNoLogs("agent_sessions", "WARNING"):
            self.assertEqual(journal.read(), [{"id": "a"}, {"id": "b"}])
        journal.append({"id": "c"})
        self.assertEqual(journal.read(), [{"id": "a"}, {"id": "b"}, {"id": "c"}])

    def test_a_path_that_is_not_utf8_is_kept_exactly(self):
        name = os.fsdecode(b"/store/caf\xe9.jsonl")  # a lone surrogate in the text
        journal.append({"id": "a", "files": [name], "title": "é"})
        self.assertEqual(journal.read(), [{"id": "a", "files": [name], "title": "é"}])
        self.assertTrue(journal.path().read_bytes().isascii())

    def test_a_journal_that_cannot_be_read_raises(self):
        journal.path().mkdir(parents=True)
        with self.assertRaises(OSError):
            journal.read()
        with self.assertRaises(OSError):
            journal.append({"id": "a"})

    def test_a_write_that_fails_leaves_the_journal_as_it_was(self):
        journal.append({"id": "kept"})
        before = journal.path().read_bytes()
        real = os.write

        def partial(fd, data):
            return real(fd, data[: len(data) // 2])  # the disk took half of it

        with mock.patch.object(os, "write", partial), self.assertRaises(OSError) as caught:
            journal.append({"id": "lost", "pad": "x" * 100})
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(journal.path().read_bytes(), before)

        def failing(fd, data):
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(os, "write", failing), self.assertRaises(OSError):
            journal.append({"id": "lost"})
        with (
            mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "Input/output error")),
            self.assertRaises(OSError),
        ):
            journal.append({"id": "lost"})
        with (
            mock.patch.object(os, "fsync", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            journal.append({"id": "lost"})
        self.assertEqual(journal.path().read_bytes(), before)
        if POSIX:
            self.assertEqual(os.listdir(self.state), ["imports.jsonl"])  # the lock was let go
        journal.append({"id": "after"})
        self.assertEqual(journal.read(), [{"id": "kept"}, {"id": "after"}])

    def test_processes_appending_at_once_all_leave_their_records(self):
        children = [python_child(APPENDER, name, 40) for name in "abcdef"]
        for status, _out, err in finish(self, children):
            self.assertEqual((status, err), (0, ""))
        with self.assertNoLogs("agent_sessions", "WARNING"):
            records = journal.read()
        self.assertEqual(len(records), 6 * 40)
        for name in "abcdef":  # all of each child's, in the order it wrote them
            self.assertEqual([r["n"] for r in records if r["who"] == name], list(range(40)))
        self.assertTrue(journal.path().read_bytes().endswith(b"\n"))


class LegacyRecordTest(StateCase):
    """``imports.json``, the record from before the journal."""

    def setUp(self):
        super().setUp()
        self.state.mkdir()
        self.old = self.state / "imports.json"

    def test_its_records_come_first_and_the_file_is_never_touched(self):
        records = [{"id": "one", "provider": "claude"}, {"id": "two", "bytes": 3}]
        self.old.write_text(json.dumps({"version": 1, "imports": records}, indent=1))
        os.utime(self.old, ns=(1_000_000_000, 2_000_000_000))
        before = (self.old.read_bytes(), self.old.stat().st_mtime_ns)
        self.assertEqual(
            journal.read(),
            [{"record": "import", "version": 1, **record} for record in records],
        )
        journal.append({"record": "import", "version": 2, "id": "three"})
        self.assertEqual([r["id"] for r in journal.read()], ["one", "two", "three"])
        self.assertEqual([r["version"] for r in journal.read()], [1, 1, 2])
        self.assertEqual((self.old.read_bytes(), self.old.stat().st_mtime_ns), before)
        self.assertEqual(sorted(os.listdir(self.state)), ["imports.json", "imports.jsonl"])

    def test_one_that_cannot_be_used_is_left_alone_with_a_warning(self):
        journal.append({"id": "kept"})
        for content in ("{ not json", '{"imports": 3}', "[]", '{"version": 1}'):
            self.old.write_text(content)
            with self.assertLogs("agent_sessions", "WARNING") as logged:
                self.assertEqual(journal.read(), [{"id": "kept"}])
            self.assertEqual(len(logged.output), 1)
            self.assertIn(str(self.old), logged.output[0])
            self.assertIn("left as it is", logged.output[0])
            self.assertEqual(self.old.read_text(), content)
        self.old.write_text('{"imports": [{"id": "ok"}, 7, "x"]}')  # entries that are not records
        self.assertEqual([r["id"] for r in journal.read()], ["ok", "kept"])


class ConcurrentImportTest(unittest.TestCase):
    """R-06: imports of different sessions, started at the same moment in
    processes of their own (our launcher), all leave their record."""

    def test_every_import_leaves_its_record(self):
        w = World(self, foreign=("claude", "codex"))
        index = w.scan()
        sources = [
            s for s in index.sessions if s.origin != "native" and api.can_import(s, index)[0]
        ]
        self.assertGreaterEqual(len(sources), 3)
        self.assertEqual({s.provider for s in sources}, {"claude", "codex"})
        children = [launcher_child("import", s.key) for s in sources]
        for (status, out, err), source in zip(finish(self, children), sources, strict=True):
            self.assertEqual(status, 0, err)
            self.assertIn("imported", err)
            self.assertTrue(out.startswith(f"{source.provider}:"), out)  # the native key
        with self.assertNoLogs("agent_sessions", "WARNING"):
            records = journal.read()
        self.assertEqual(sorted(r["id"] for r in records), sorted(s.id for s in sources))
        self.assertEqual({r["source_key"] for r in records}, {s.key for s in sources})
        for record in records:
            self.assertEqual((record["record"], record["version"]), ("import", 2))
            self.assertTrue(record["manifest"]["files"])
            for item in record["manifest"]["files"]:
                st = os.stat(item["path"])
                self.assertEqual((st.st_size, st.st_mtime_ns), (item["size"], item["mtime_ns"]))
        index = api.load_index()
        for source in sources:
            self.assertEqual(w.session(source.id, "native", index).provider, source.provider)
        self.assertEqual(state_names(), ["imports.jsonl"])


if __name__ == "__main__":
    unittest.main()
