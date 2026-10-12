"""What stops a command half way: Ctrl-C, SIGTERM and SIGHUP, an exception
nobody planned for, and an import whose source goes away or whose disk fills
up in the middle of the copy (docs/REVIEW.md, test case 10).

The signals are sent to a real child process that is in the middle of
copying (``support.SLOW_IMPORT``); afterwards the native store and the state
directory must be as they were."""

import contextlib
import errno
import io
import os
import shutil
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_WIN, World, files_below
from support import LOG_NAME, SLOW_IMPORT, Sandbox, finish, python_child, state_names, wait_for

from agent_sessions import api, cli, importer, journal

POSIX = os.name == "posix"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except KeyboardInterrupt:
            # A failure of the test, not a Ctrl-C for the test runner.
            raise AssertionError("KeyboardInterrupt came out of cli.main") from None
    return code, out.getvalue(), err.getvalue()


def _default_sigint() -> None:
    """In the child, before it becomes Python: a test runner started in the
    background hands down an ignored SIGINT, and Python then leaves it
    ignored. A terminal's Ctrl-C is not ignored."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class CliCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.native = self.box.store(self.box.home)
        self.box.session(self.native, "a")
        self.log = Path(os.environ["AGENT_SESSIONS_STATE"]) / LOG_NAME


class InterruptTest(CliCase):
    def test_ctrl_c_is_one_line_and_exit_130(self):
        for command in (["scan"], ["list"], ["status"], ["verify"], ["show", "a"], ["open", "a"]):
            with self.subTest(command=command[0]):
                target = {
                    "scan": "scan",
                    "list": "load_index",
                    "status": "status",
                    "verify": "verify",
                    "show": "load_index",
                    "open": "open_session",
                }[command[0]]
                with mock.patch.object(api, target, side_effect=KeyboardInterrupt):
                    self.assertEqual(run(*command), (130, "", "agent-sessions: interrupted\n"))

    def test_ctrl_c_while_the_arguments_are_read(self):
        with mock.patch.object(cli, "build_parser", side_effect=KeyboardInterrupt):
            self.assertEqual(run("scan"), (130, "", "agent-sessions: interrupted\n"))

    def test_an_interrupt_is_not_an_internal_error_and_is_not_logged(self):
        with mock.patch.object(api, "scan", side_effect=KeyboardInterrupt):
            run("scan")
        self.assertFalse(self.log.exists())

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "needs SIGTERM and SIGHUP")
    def test_sigterm_and_sighup_become_an_exception_for_the_length_of_the_command(self):
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name)
            before = signal.getsignal(signum)
            seen = {}

            def scan(signum=signum, seen=seen, before=before, **kwargs):
                seen["handler"] = signal.getsignal(signum)
                if seen["handler"] == before:  # the signal would end the test run
                    raise AssertionError(f"no handler was installed for {signum!r}")
                try:
                    os.kill(os.getpid(), signum)
                    time.sleep(5)  # the handler raises in the middle of this
                    seen["slept"] = True
                finally:
                    seen["unwound"] = True
                    os.kill(os.getpid(), signum)  # a second one, while unwinding: ignored
                    time.sleep(0.05)
                    seen["cleanup finished"] = True

            with self.subTest(signal=name), mock.patch.object(api, "scan", scan):
                code, out, err = run("scan")
                self.assertEqual(
                    (code, out, err), (128 + signum, "", f"agent-sessions: terminated ({name})\n")
                )
                self.assertEqual(
                    {k: v for k, v in seen.items() if k != "handler"},
                    {"unwound": True, "cleanup finished": True},
                )
                self.assertNotEqual(seen["handler"], before)
                self.assertEqual(signal.getsignal(signum), before)  # put back afterwards

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "needs SIGTERM")
    def test_the_handlers_are_put_back_after_a_command_that_ended_normally(self):
        before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)
        self.assertEqual(run("scan")[0], 0)
        with mock.patch.object(api, "scan", side_effect=api.ApiError("no")):
            self.assertEqual(run("scan")[0], 1)
        self.assertEqual(
            (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)), before
        )

    def test_a_signal_is_not_swallowed_by_code_that_catches_errors(self):
        self.assertFalse(issubclass(cli._Signalled, Exception))


class InternalErrorTest(CliCase):
    def test_one_line_that_names_the_log_and_exit_1(self):
        failure = RuntimeError("the index is \x1b[2Jbroken\nreally")
        with mock.patch.object(api, "scan", side_effect=failure):
            code, out, err = run("scan")
        self.assertEqual(
            (code, out, err),
            (
                1,
                "",
                "agent-sessions: internal error: RuntimeError: the index is  [2Jbroken really "
                f"(details in {self.log})\n",
            ),
        )
        text = self.log.read_text(encoding="utf-8")
        self.assertIn(
            "ERROR unexpected error in `agent-sessions scan`: RuntimeError: the index", text
        )
        self.assertIn("  Traceback (most recent call last):", text)
        self.assertIn("  RuntimeError: the index is  [2Jbroken", text)

    def test_with_debug_the_traceback_is_printed_too(self):
        def scan(**kwargs):
            raise RuntimeError("broken")

        with mock.patch.object(api, "scan", scan):
            plain = run("scan")[2]
            code, out, err = run("--debug", "scan")
        self.assertNotIn("Traceback", plain)
        self.assertEqual((code, out), (1, ""))
        lines = err.splitlines()
        self.assertRegex(
            lines[0], r" ERROR unexpected error in `agent-sessions scan`: RuntimeError"
        )
        self.assertEqual(lines[1], "  Traceback (most recent call last):")
        self.assertIn("  RuntimeError: broken", lines)
        self.assertEqual(
            lines[-1],
            f"agent-sessions: internal error: RuntimeError: broken (details in {self.log})",
        )

    def test_an_exception_from_inside_the_facade_is_logged_once(self):
        with mock.patch("agent_sessions.index.Index.scan", side_effect=ZeroDivisionError("x")):
            code, _out, err = run("scan")
        self.assertEqual(code, 1)
        self.assertIn("internal error: ZeroDivisionError: x (details in ", err)
        text = self.log.read_text(encoding="utf-8")
        self.assertEqual(text.count("unexpected error"), 1)
        self.assertIn("unexpected error in scan: ZeroDivisionError", text)

    def test_without_a_log_the_line_names_none(self):
        os.makedirs(self.log)  # a directory where the log belongs: nothing can be written
        with mock.patch.object(api, "scan", side_effect=RuntimeError("broken")):
            self.assertEqual(
                run("scan"), (1, "", "agent-sessions: internal error: RuntimeError: broken\n")
            )

    def test_the_failures_with_a_reason_stay_what_they_were(self):
        self.assertEqual(run("open", "nope")[0], 1)
        self.assertEqual(run("list", "--provider", "fake")[0], 0)
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["no-such-command"])
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.log.exists() and "unexpected" in self.log.read_text(encoding="utf-8"))


class ImportCase(unittest.TestCase):
    def setUp(self):
        self.w = World(self, foreign=("claude",))
        self.home = self.w.home
        self.index = self.w.scan()
        self.source = self.w.session(CLAUDE_WIN, "foreign", self.index)
        self.native = self.home / ".claude"
        self.project = self.w.profile / ".claude" / "projects" / "F--Proj-My-Site"

    def nothing_left(self):
        """The native store and the state directory are as they were:
        neither had anything in it, and the log is not the import's."""
        self.assertFalse(self.native.exists())
        self.assertEqual(state_names(), [])
        self.assertEqual(journal.read(), [])


@unittest.skipUnless(POSIX, "signals sent to a child process")
class SignalledImportTest(ImportCase):
    """A real process, signalled while it copies."""

    def interrupted(self, signum) -> tuple[int, str, str]:
        mark, gate = self.w.box.root / "copying", self.w.box.root / "never"
        child = python_child(
            SLOW_IMPORT, mark, gate, "import", self.source.key, preexec_fn=_default_sigint
        )
        try:
            wait_for(self, mark)
            # It is in the middle of the copy: a staging directory with the
            # first bytes, and the lock.
            staging = [n for n in os.listdir(self.native) if n.startswith(importer.STAGING_PREFIX)]
            self.assertEqual(len(staging), 1)
            self.assertTrue(files_below(self.native / staging[0]))
            self.assertIn("agent-sessions.lock", state_names())
            os.kill(child.pid, signum)
        finally:
            ((status, out, err),) = finish(self, [child])
        return status, out, err

    def test_sigterm_rolls_the_import_back(self):
        status, out, err = self.interrupted(signal.SIGTERM)
        # An exit status, not a death by signal: the handler ran and the command unwound.
        self.assertEqual((status, out), (128 + signal.SIGTERM, ""))
        self.assertEqual(err, "agent-sessions: terminated (SIGTERM)\n")
        self.nothing_left()

    def test_sighup_rolls_the_import_back(self):
        status, out, err = self.interrupted(signal.SIGHUP)
        self.assertEqual((status, out), (128 + signal.SIGHUP, ""))
        self.assertEqual(err, "agent-sessions: terminated (SIGHUP)\n")
        self.nothing_left()

    def test_ctrl_c_rolls_the_import_back(self):
        status, out, err = self.interrupted(signal.SIGINT)
        self.assertEqual((status, out, err), (130, "", "agent-sessions: interrupted\n"))
        self.nothing_left()

    def test_and_the_import_works_afterwards(self):
        self.interrupted(signal.SIGTERM)
        native = api.import_session(self.w.session(CLAUDE_WIN, "foreign"))
        self.assertEqual(native.id, CLAUDE_WIN)
        self.assertEqual([r["id"] for r in journal.read()], [CLAUDE_WIN])


class MidCopyFailureTest(ImportCase):
    """docs/REVIEW.md, test case 10: the source disappears, or the disk is
    full, while the copy is under way."""

    def refused(self) -> str:
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(self.source)
        return caught.exception.reason

    def test_the_source_disappears_in_the_middle_of_the_copy(self):
        """The drive is pulled: the file being read ends in an error, and
        the files after it are no longer there."""
        real_open = importer.open_ro
        opened = []

        class Unplugged:
            def __init__(self, reader, project):
                self.reader, self.project, self.reads = reader, project, 0

            def read(self, size=-1):
                self.reads += 1
                if self.reads == 2:
                    shutil.rmtree(self.project)
                    raise OSError(errno.EIO, "Input/output error")
                return self.reader.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.reader.close()

            def __getattr__(self, name):
                return getattr(self.reader, name)

        def open_ro(path):
            opened.append(path)
            return Unplugged(real_open(path), self.project)

        with (
            mock.patch.object(importer, "CHUNK", 16),
            mock.patch.object(importer, "open_ro", open_ro),
        ):
            reason = self.refused()
        self.assertEqual(len(opened), 1)
        self.assertIn("Input/output error", reason)
        self.assertIn("nothing was left in the native store", reason)
        self.assertFalse(self.project.exists())
        self.nothing_left()

    def test_a_source_file_that_is_gone_when_its_turn_comes(self):
        """The first file is copied whole; the rest of the session has
        vanished by then."""
        real_copy = importer._copy
        copied = []

        def copy(item, target, tick):
            real_copy(item, target, tick)
            copied.append(item.src)
            shutil.rmtree(self.project, ignore_errors=True)

        with mock.patch.object(importer, "_copy", copy):
            reason = self.refused()
        self.assertEqual(len(copied), 1)
        self.assertIn("No such file or directory", reason)
        self.assertIn("nothing was left in the native store", reason)
        self.nothing_left()

    def test_the_disk_fills_up_in_the_middle_of_the_copy(self):
        """ENOSPC from the writer, after some of a file has been written."""
        real_fdopen = os.fdopen
        written = []

        class Full:
            def __init__(self, writer):
                self.writer = writer

            def write(self, data):
                if sum(written) >= 64:
                    raise OSError(errno.ENOSPC, "No space left on device")
                written.append(len(data))
                return self.writer.write(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.writer.close()

            def __getattr__(self, name):
                return getattr(self.writer, name)

        def fdopen(fd, mode="r", *args, **kwargs):
            opened = real_fdopen(fd, mode, *args, **kwargs)
            return Full(opened) if mode == "wb" else opened

        with (
            mock.patch.object(importer, "CHUNK", 16),
            mock.patch.object(importer.os, "fdopen", fdopen),
        ):
            reason = self.refused()
        self.assertIn(sum(written), range(64, 64 + 16))  # it got that far
        self.assertEqual(
            reason,
            "import failed: No space left on device; nothing was left in the native store",
        )
        self.nothing_left()
        # The source is as it was, and with room again the import goes through.
        self.assertEqual(api.import_session(self.w.session(CLAUDE_WIN, "foreign")).id, CLAUDE_WIN)

    def test_the_disk_fills_up_when_the_copy_is_flushed(self):
        real_fsync, calls = os.fsync, []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 1:  # the first staged file
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_fsync(fd)

        with mock.patch.object(importer.os, "fsync", fsync):
            reason = self.refused()
        self.assertIn("No space left on device", reason)
        self.nothing_left()

    def test_the_disk_fills_up_when_the_record_is_written(self):
        """The last step. The copy is in place and verified; it is taken out
        again, and the journal is not left with half a line."""
        real_write = os.write
        seen = {}

        def write(fd, data):
            if os.path.samestat(os.fstat(fd), os.stat(journal.path())):
                seen["placed"] = sorted(os.listdir(self.native / "projects"))
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(journal.os, "write", write):
            reason = self.refused()
        self.assertEqual(len(seen["placed"]), 1)  # the copy was in the store at that moment
        self.assertIn("No space left on device", reason)
        self.assertIn("nothing was left in the native store", reason)
        self.nothing_left()


if __name__ == "__main__":
    unittest.main()
