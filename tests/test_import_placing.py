"""How an import puts its copy in place: one import at a time (the state
directory's lock around the whole of ``importer.execute``), a move that never
replaces what is there (``importer._place``), and directories flushed to
disk. The machine is the one of tests/fixture_world.py; the races are run
with real processes."""

import errno
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_WIN, CLAUDE_WIN_PLAIN, World, files_below
from support import (
    SLOW_IMPORT,
    drop_state,
    finish,
    launcher_child,
    python_child,
    state_names,
    wait_for,
)

from agent_sessions import api, fsutil, importer, journal
from agent_sessions.providers.claude import encode_cwd

POSIX = os.name == "posix"
HOLDER = """
from agent_sessions import fsutil
with fsutil.lock():
    print("held", flush=True)
    sys.stdin.read()
"""


class PlacingCase(unittest.TestCase):
    def setUp(self):
        self.w = World(self, foreign=("claude",))
        self.home = self.w.home
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])
        self.index = self.w.scan()
        self.native = self.home / ".claude"
        self.target = self.native / "projects" / encode_cwd(str(self.w.site))
        self.transcript = self.target / f"{CLAUDE_WIN}.jsonl"
        self.side = self.target / CLAUDE_WIN

    def source(self, session_id=CLAUDE_WIN):
        return self.w.session(session_id, "foreign", self.w.scan())

    def plan(self, session_id=CLAUDE_WIN) -> importer.ImportPlan:
        return api._plan(self.source(session_id), api.load_index())

    def nothing_left(self):
        """No staging directory, no journal, no lock file."""
        if self.native.exists():
            names = os.listdir(self.native)
            self.assertEqual([n for n in names if n.startswith(importer.STAGING_PREFIX)], [])
        self.assertFalse(journal.path().exists())
        self.assertFalse(fsutil.lock_file().exists())


class NoReplaceTest(PlacingCase):
    def test_a_file_that_appears_at_the_last_moment_is_not_overwritten(self):
        """Another program creates the transcript's name after every check
        has passed, just before the copy would take it."""
        real = os.link

        def late(src, dst, **kwargs):
            if dst == str(self.transcript) and not os.path.lexists(dst):
                with open(dst, "w", encoding="utf-8") as fh:
                    fh.write("theirs")
            return real(src, dst, **kwargs)

        with (
            mock.patch.object(importer.os, "link", late),
            self.assertRaises(importer.Exists) as caught,
        ):
            importer.execute(self.plan())
        self.assertEqual(caught.exception.paths, [str(self.transcript)])
        self.assertEqual(self.transcript.read_text(encoding="utf-8"), "theirs")
        # Everything of ours is gone again, the side directory included.
        self.assertEqual(files_below(self.native), [os.path.relpath(self.transcript, self.native)])
        self.nothing_left()

    def test_a_file_is_linked_into_place_not_renamed_over(self):
        plan = self.plan()
        with (
            mock.patch.object(importer.os, "link", wraps=os.link) as link,
            mock.patch.object(importer.os, "replace", wraps=os.replace) as replace,
        ):
            importer.execute(plan)
        self.assertEqual([call.args[1] for call in link.call_args_list], [str(self.transcript)])
        replace.assert_not_called()
        self.assertEqual(os.stat(self.transcript).st_nlink, 1)  # the staged name is gone

    def test_where_links_are_not_supported_the_name_is_checked_and_the_file_renamed(self):
        for code in ("EPERM", "ENOTSUP", "ENOSYS", "EMLINK"):
            with self.subTest(errno=code):
                target = self.w.box.root / f"staged-{code}"
                target.write_text("ours", encoding="utf-8")
                dst = self.w.box.root / f"placed-{code}"
                failure = OSError(getattr(errno, code), os.strerror(getattr(errno, code)))
                with mock.patch.object(importer.os, "link", side_effect=failure):
                    importer._place(str(target), str(dst), False)
                    self.assertEqual(dst.read_text(encoding="utf-8"), "ours")
                    self.assertFalse(target.exists())
                    # And the name that is taken is still refused there.
                    target.write_text("again", encoding="utf-8")
                    with self.assertRaises(importer.Exists):
                        importer._place(str(target), str(dst), False)
                self.assertEqual(dst.read_text(encoding="utf-8"), "ours")
        with mock.patch.object(importer.os, "link", side_effect=NotImplementedError):
            target = self.w.box.root / "staged"
            target.write_text("ours", encoding="utf-8")
            importer._place(str(target), str(self.w.box.root / "placed"), False)
        self.assertEqual((self.w.box.root / "placed").read_text(encoding="utf-8"), "ours")

    @unittest.skipUnless(POSIX, "error numbers of a POSIX link")
    def test_another_failure_of_the_link_fails_the_import(self):
        failure = OSError(errno.EIO, "Input/output error")
        plan = self.plan()
        with (
            mock.patch.object(importer.os, "link", side_effect=failure),
            mock.patch.object(importer.os, "replace") as replace,
            self.assertRaises(importer.ImportFailed) as caught,
        ):
            importer.execute(plan)
        replace.assert_not_called()
        self.assertIn("Input/output error", caught.exception.reason)
        self.assertFalse(self.native.exists())
        self.nothing_left()

    def test_a_name_that_is_taken_by_anything_is_refused(self):
        base = self.w.box.root
        for kind in ("file", "dir", "empty dir", "dangling link", "link to a dir"):
            for is_dir in (False, True):
                with self.subTest(taken_by=kind, unit_is_dir=is_dir):
                    dst = base / f"dst-{kind}-{is_dir}"
                    if kind == "file":
                        dst.write_text("theirs", encoding="utf-8")
                    elif kind == "dir":
                        (dst / "sub").mkdir(parents=True)
                    elif kind == "empty dir":
                        dst.mkdir()
                    elif kind == "dangling link":
                        os.symlink(base / "nowhere", dst)
                    else:
                        os.symlink(base, dst)
                    target = base / f"staged-{kind}-{is_dir}"
                    if is_dir:
                        target.mkdir()
                    else:
                        target.write_text("ours", encoding="utf-8")
                    before = os.lstat(dst)
                    with self.assertRaises(importer.Exists):
                        importer._place(str(target), str(dst), is_dir)
                    self.assertTrue(os.path.samestat(os.lstat(dst), before))
                    self.assertTrue(os.path.lexists(target))

    @unittest.skipUnless(POSIX, "what rename of a directory does on POSIX")
    def test_a_directory_is_renamed_and_never_replaces_one_that_holds_anything(self):
        """The side directory's name appears, with a file in it, between the
        check and the rename."""
        real = os.rename

        def late(src, dst):
            if dst == str(self.side):
                os.mkdir(dst)
                with open(os.path.join(dst, "theirs.txt"), "w", encoding="utf-8") as fh:
                    fh.write("theirs")
            return real(src, dst)

        with (
            mock.patch.object(importer.os, "rename", late),
            self.assertRaises(importer.ImportFailed) as caught,
        ):
            importer.execute(self.plan())
        self.assertIn("nothing was left in the native store", caught.exception.reason)
        self.assertEqual(
            files_below(self.native), [os.path.relpath(self.side / "theirs.txt", self.native)]
        )
        self.assertEqual((self.side / "theirs.txt").read_text(encoding="utf-8"), "theirs")
        self.nothing_left()

    def test_an_interrupt_between_the_move_and_its_note_is_rolled_back(self):
        """The unit is in place and the import does not know it yet."""
        real = importer._place
        for stop_at, is_dir in ((str(self.side), True), (str(self.transcript), False)):
            with self.subTest(unit=os.path.basename(stop_at)):

                def place(target, dst, unit_is_dir, stop_at=stop_at):
                    real(target, dst, unit_is_dir)
                    if dst == stop_at:
                        raise KeyboardInterrupt

                with (
                    mock.patch.object(importer, "_place", place),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    importer.execute(self.plan())
                self.assertFalse(self.native.exists())
                self.nothing_left()
        # The same when the link is made and the staged name not yet removed.
        with (
            mock.patch.object(importer.os, "unlink", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            importer.execute(self.plan())
        self.assertFalse(self.native.exists())
        self.nothing_left()

    def test_arrived(self):
        base = self.w.box.root
        staged, dst = base / "staged", base / "dst"
        self.assertFalse(importer._arrived(str(staged), str(dst)))  # neither is there
        staged.write_text("ours", encoding="utf-8")
        self.assertFalse(importer._arrived(str(staged), str(dst)))  # not moved yet
        dst.write_text("theirs", encoding="utf-8")
        self.assertFalse(importer._arrived(str(staged), str(dst)))  # someone else's
        os.unlink(dst)
        os.link(staged, dst)
        self.assertTrue(importer._arrived(str(staged), str(dst)))  # linked, not yet unlinked
        os.unlink(staged)
        self.assertTrue(importer._arrived(str(staged), str(dst)))  # moved


class FlushTest(PlacingCase):
    def test_the_directories_an_import_changed_are_flushed_before_the_record(self):
        events: list[tuple[str, str]] = []
        real_sync, real_write = fsutil.fsync_dir, journal.write

        def sync(path):
            events.append(("dir", os.fspath(path)))
            return real_sync(path)

        def write(fd, record):
            events.append(("record", record["id"]))
            return real_write(fd, record)

        with (
            mock.patch.object(fsutil, "fsync_dir", sync),
            mock.patch.object(journal, "write", write),
        ):
            record = importer.execute(self.plan())
        staging = [path for kind, path in events if importer.STAGING_PREFIX in path]
        flushed = [path for kind, path in events if kind == "dir" and path not in staging]
        position = events.index(("record", CLAUDE_WIN))
        before_record = {path for kind, path in events[:position] if kind == "dir"}
        # The staged side directory, every directory in it, deepest first.
        inside = [d for d in record["manifest"]["dirs"] if d.startswith(str(self.side))]
        self.assertEqual(len(staging), len(inside))
        self.assertEqual(staging, sorted(staging, reverse=True))
        # Where a name was created: the parents of the units and of every
        # directory made for them, and those directories.
        made = [str(self.native), str(self.native / "projects"), str(self.target)]
        expected = {str(self.home), *made}
        self.assertLessEqual(expected, before_record)
        self.assertEqual(sorted(set(flushed) - {str(self.state)}), sorted(expected))
        # The state directory, once the journal it now holds is written.
        self.assertEqual(events[-1], ("dir", str(self.state)))
        if POSIX:
            self.assertTrue(real_sync(self.target))

    def test_a_platform_that_cannot_flush_a_directory_still_imports(self):
        with mock.patch.object(fsutil, "fsync_dir", return_value=False):
            importer.execute(self.plan())
        self.assertTrue(self.transcript.is_file())


class OneAtATimeTest(PlacingCase):
    def test_execute_holds_the_lock_from_the_checks_to_the_record(self):
        seen = {}
        real_check, real_write = importer.check, journal.write

        def check(plan):
            seen.setdefault("check", fsutil.lock_file().exists())
            return real_check(plan)

        def write(fd, record):
            seen["record"] = fsutil.lock_file().exists()
            return real_write(fd, record)

        def verify():
            seen["verify"] = fsutil.lock_file().exists()

        with (
            mock.patch.object(importer, "check", check),
            mock.patch.object(journal, "write", write),
        ):
            importer.execute(self.plan(), verify=verify)
        self.assertEqual(seen, {"check": True, "verify": True, "record": True})
        if POSIX:
            self.assertFalse(fsutil.lock_file().exists())

    def test_the_roll_back_happens_before_the_lock_is_given_up(self):
        seen = {}
        real = importer._remove

        def remove(path):
            seen.setdefault("locked", fsutil.lock_file().exists())
            return real(path)

        plan = self.plan()
        drop_state()
        with (
            mock.patch.object(importer, "_remove", remove),
            self.assertRaises(importer.ImportFailed),
        ):
            importer.execute(plan, verify=mock.Mock(side_effect=ValueError("no")))
        self.assertEqual(seen, {"locked": True})
        self.assertFalse(self.native.exists())
        self.nothing_left()
        self.assertFalse(self.state.exists())  # made for the lock, removed with it

    def test_an_import_that_cannot_get_the_lock_is_refused(self):
        self.state.mkdir(exist_ok=True)
        holder = python_child(HOLDER, stdin=-1)  # -1: a pipe, closed to let it go
        try:
            self.assertEqual(holder.stdout.readline(), "held\n")
            with (
                mock.patch.object(fsutil, "LOCK_TIMEOUT", 0.3),
                self.assertRaises(importer.ImportFailed) as caught,
            ):
                importer.execute(self.plan())
        finally:
            holder.stdin.close()
            finish(self, [holder])
        self.assertEqual(
            caught.exception.reason,
            f"another import is still running (process {holder.pid}); nothing was imported",
        )
        self.assertFalse(self.native.exists())
        self.assertEqual(state_names(), [])
        importer.execute(self.plan())  # and afterwards it goes through

    def test_two_processes_importing_the_same_session(self):
        """The second starts while the first is in the middle of its copy.
        It has to wait, and then finds the session imported."""
        key = self.source().key
        mark, gate = self.w.box.root / "copying", self.w.box.root / "go"
        first = python_child(SLOW_IMPORT, mark, gate, "import", key)
        second = None
        try:
            wait_for(self, mark)
            second = launcher_child("import", key)
            time.sleep(1.0)
            # The first is still copying; the second has changed nothing and
            # has not finished: it waits for the lock.
            self.assertIsNone(first.poll())
            self.assertIsNone(second.poll(), "the second import did not wait for the first")
            staging = [n for n in os.listdir(self.native) if n.startswith(importer.STAGING_PREFIX)]
            self.assertEqual(staging, [f"{importer.STAGING_PREFIX}{first.pid}-{staging[0][-16:]}"])
            self.assertFalse(self.transcript.exists())
            self.assertEqual(fsutil._holder(str(fsutil.lock_file())), first.pid)
        finally:
            gate.touch()
            results = finish(self, [first] + ([second] if second is not None else []))
        (status_1, out_1, err_1), (status_2, out_2, err_2) = results
        self.assertEqual((status_1, status_2), (0, 0), err_1 + err_2)
        self.assertIn("imported 2 items", err_1)
        self.assertEqual(err_2, "agent-sessions: already imported; nothing was copied\n")
        self.assertEqual(out_1, out_2)  # the same native key
        self.assertEqual([r["id"] for r in journal.read()], [CLAUDE_WIN])
        self.assertEqual(
            files_below(self.target),
            sorted(
                rel
                for rel in files_below(self.w.profile / ".claude" / "projects" / "F--Proj-My-Site")
                if rel.startswith(CLAUDE_WIN)
            ),
        )
        self.assertEqual(os.listdir(self.native), ["projects"])
        self.assertEqual(state_names(), ["imports.jsonl"])

    def test_two_processes_importing_different_sessions_take_turns(self):
        first_key, second_key = self.source().key, self.source(CLAUDE_WIN_PLAIN).key
        mark, gate = self.w.box.root / "copying", self.w.box.root / "go"
        first = python_child(SLOW_IMPORT, mark, gate, "import", first_key)
        second = None
        try:
            wait_for(self, mark)
            second = launcher_child("import", second_key)
            time.sleep(0.7)
            self.assertIsNone(second.poll(), "the second import did not wait for the first")
            self.assertFalse((self.target / f"{CLAUDE_WIN_PLAIN}.jsonl").exists())
        finally:
            gate.touch()
            results = finish(self, [first] + ([second] if second is not None else []))
        self.assertEqual([status for status, _out, _err in results], [0, 0], results)
        self.assertEqual([r["id"] for r in journal.read()], [CLAUDE_WIN, CLAUDE_WIN_PLAIN])


if __name__ == "__main__":
    unittest.main()
