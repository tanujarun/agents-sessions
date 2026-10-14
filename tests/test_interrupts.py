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

from agent_sessions import api, cli, importer, journal, unimport
from agent_sessions.providers.claude import Claude, encode_cwd

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

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "needs SIGHUP")
    def test_a_signal_that_is_ignored_when_the_command_starts_stays_ignored(self):
        """``nohup agent-sessions import …``: the hangup must not end it."""
        seen = []

        def scan(**kwargs):
            seen.append((signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGINT)))
            raise api.ApiError("no")

        for ignored in (signal.SIGHUP, signal.SIGINT):
            before = signal.signal(ignored, signal.SIG_IGN)
            try:
                with mock.patch.object(api, "scan", scan):
                    self.assertEqual(run("scan")[0], 1)
            finally:
                signal.signal(ignored, before)
        self.assertEqual(seen[0][0], signal.SIG_IGN)
        self.assertEqual(seen[1][1], signal.SIG_IGN)

    def test_after_the_first_signal_every_other_one_is_held_off(self):
        events = []

        def scan(**kwargs):
            try:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(5)  # the handler raises in the middle of this
            finally:
                for name in ("SIGINT", "SIGTERM", "SIGHUP"):
                    if hasattr(signal, name):
                        os.kill(os.getpid(), getattr(signal, name))
                        time.sleep(0.05)  # a handler would raise here
                events.append("unwound")

        with mock.patch.object(api, "scan", scan):
            self.assertEqual(run("scan"), (130, "", "agent-sessions: interrupted\n"))
        self.assertEqual(events, ["unwound"])
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

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
        self.assertEqual(
            [(r["record"], r["id"]) for r in journal.read()],
            [("begin", CLAUDE_WIN), ("import", CLAUDE_WIN)],
        )


# A child that runs the command line and stops at one point of an import,
# for the tests that have to catch it there: ``python_child(STOPPING_IMPORT,
# mark, gate, point, slow, "import", key)``. It creates the file ``mark``
# when it has reached the point and waits there for the file ``gate``. With
# ``slow`` above zero the removal of the staging directory, the first step
# of a roll back, creates ``mark + ".rollback"`` and then takes that many
# seconds, as it does for a large staged copy. The hook is this script: the
# package has no switch for it.
STOPPING_IMPORT = """
import os, shutil, time
from agent_sessions import cli, importer

mark, gate, point, slow = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])


def stop_before(real, number):
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        if len(calls) == number:
            open(mark, "a").close()
            while not os.path.exists(gate):
                time.sleep(0.01)
        return real(*args, **kwargs)

    return wrapper


if point == "copy":  # the staging directory is there; the first file is about to be staged
    importer._copy = stop_before(importer._copy, 1)
elif point == "first-unit":  # everything is staged; nothing is in place
    importer._place = stop_before(importer._place, 1)
elif point == "between-units":  # the first unit is in place, the second still staged
    importer._place = stop_before(importer._place, 2)
elif point == "placed":  # every unit is in place; nothing says that the import is done
    importer._manifest = stop_before(importer._manifest, 1)

real_rmtree = shutil.rmtree


def rmtree(path, *args, **kwargs):
    if slow and importer.STAGING_PREFIX in os.path.basename(os.fspath(path)):
        open(mark + ".rollback", "a").close()
        time.sleep(slow)
    return real_rmtree(path, *args, **kwargs)


shutil.rmtree = rmtree
sys.exit(cli.main(sys.argv[5:]))
"""


class StoppedImportCase(ImportCase):
    """A real import, in a process of its own, stopped at one point."""

    def stopped(self, point, slow=0.0):
        """Start the import and wait until it has reached ``point``;
        returns the child and the file its roll back creates."""
        marks = self.w.box.root / "marks"
        marks.mkdir(exist_ok=True)
        mark = marks / f"{point}-{len(os.listdir(marks))}"
        child = python_child(
            STOPPING_IMPORT,
            mark,
            self.w.box.root / "never",
            point,
            slow,
            "import",
            self.source.key,
            preexec_fn=_default_sigint,
        )
        try:
            wait_for(self, mark)
        except BaseException:
            child.kill()
            finish(self, [child])
            raise
        return child, Path(f"{mark}.rollback")

    def kinds(self) -> list[str]:
        return [record["record"] for record in journal.read()]

    def copy_is_whole(self):
        """The native copy is the source's files, byte for byte, and
        nothing else is in the native store."""
        target = self.native / "projects" / encode_cwd(str(self.w.site))
        expected = [rel for rel in files_below(self.project) if rel.startswith(CLAUDE_WIN)]
        self.assertEqual(
            files_below(self.native), [f"projects/{target.name}/{rel}" for rel in expected]
        )
        for rel in expected:
            self.assertEqual((target / rel).read_bytes(), (self.project / rel).read_bytes(), rel)


@unittest.skipUnless(POSIX, "signals sent to a child process")
class SecondSignalTest(StoppedImportCase):
    """docs/AUDIT.md A-04: the roll back of an interrupted import was a
    ``finally`` block, and the next signal ended it: a second Ctrl-C, or the
    hangup that follows when the terminal is closed. The command said
    ``interrupted`` and left the staging directory, or the whole copy with
    no record of it."""

    def twice(self, point, first, second):
        child, rolling_back = self.stopped(point, slow=0.8)
        try:
            os.kill(child.pid, first)
            wait_for(self, rolling_back)
            time.sleep(0.2)  # well inside the roll back
            os.kill(child.pid, second)
        finally:
            ((status, out, err),) = finish(self, [child])
        # It ends as the first signal ends it, once the roll back is done.
        if first == signal.SIGINT:
            expected = (130, "agent-sessions: interrupted\n")
        else:
            name = signal.Signals(first).name
            expected = (128 + first, f"agent-sessions: terminated ({name})\n")
        self.assertEqual((status, err, out), (*expected, ""))
        self.assertFalse(self.native.exists(), files_below(self.native))
        if point == "copy":  # nothing had been put in place: the journal was never told
            self.assertEqual(state_names(), [])
        else:
            self.assertEqual(state_names(), ["imports.jsonl"])
            self.assertEqual(self.kinds(), ["begin", "rollback"])
            self.assertFalse(journal.read()[1]["recovered"])  # its own roll back, to the end

    def test_a_second_signal_does_not_cut_the_roll_back_short(self):
        for point in ("copy", "placed"):
            for first, second in (
                (signal.SIGINT, signal.SIGINT),  # Ctrl-C, twice
                (signal.SIGINT, signal.SIGHUP),  # Ctrl-C, then the terminal is closed
                (signal.SIGTERM, signal.SIGINT),
                (signal.SIGHUP, signal.SIGTERM),
            ):
                with self.subTest(point=point, first=first.name, second=second.name):
                    self.twice(point, first, second)
                    journal.path().unlink(missing_ok=True)

    def test_and_afterwards_the_session_is_imported_and_undone_like_any_other(self):
        self.twice("placed", signal.SIGINT, signal.SIGINT)
        code, _out, err = run("import", self.source.key)
        self.assertEqual(code, 0, err)
        self.assertIn("imported 2 items", err)
        self.copy_is_whole()
        self.assertEqual(self.kinds(), ["begin", "rollback", "begin", "import"])
        self.assertEqual(run("unimport", CLAUDE_WIN, "--yes")[0], 0)
        self.assertFalse(self.native.exists())


@unittest.skipUnless(POSIX, "a child process that is killed")
class KilledImportTest(StoppedImportCase):
    """docs/AUDIT.md A-04: an import that dies with a unit in place (it is
    killed, the machine goes down) cannot roll anything back. What it left
    could be neither imported again, since its names were taken, nor undone,
    since nothing said it had been imported. Now the journal has the line of
    its beginning, and the next import or undo of that store settles it: the
    store ends as it began, or with a whole copy and its record."""

    def killed(self, point):
        child, _rolling_back = self.stopped(point)
        child.kill()
        ((status, _out, _err),) = finish(self, [child])
        self.assertEqual(status, -signal.SIGKILL)
        self.assertEqual(self.kinds(), ["begin"])
        self.assertIn("agent-sessions.lock", state_names())  # N-27: left by the kill
        return journal.read()[0]

    def everything_below(self) -> list[str]:
        """What lies in the native store, the staging directory included."""
        found = []
        for base, dirs, files in os.walk(self.native):
            found += [os.path.relpath(os.path.join(base, n), self.native) for n in dirs + files]
        return sorted(found)

    # --- between two units: the side directory is in place, the transcript is not

    def between_units(self):
        begin = self.killed("between-units")
        side, transcript = begin["destinations"]
        self.assertTrue(os.path.isdir(side))
        self.assertFalse(os.path.exists(transcript))
        self.assertTrue(os.path.isfile(os.path.join(begin["staging"], "1")))  # still staged
        return begin

    def test_killed_between_two_units_then_imported_again(self):
        begin = self.between_units()
        code, out, err = run("import", self.source.key)
        self.assertEqual((code, err.count("imported 2 items")), (0, 1), err)
        self.assertEqual(out, self.w.session(CLAUDE_WIN, "native").key + "\n")
        self.copy_is_whole()  # and no staging directory beside it
        self.assertEqual(self.kinds(), ["begin", "rollback", "begin", "import"])
        rolled_back = journal.read()[1]
        self.assertEqual((rolled_back["recovered"], rolled_back["id"]), (True, CLAUDE_WIN))
        self.assertEqual(
            rolled_back["removed"],
            sorted([*begin["dirs"], begin["destinations"][0], begin["staging"]]),
        )
        self.assertEqual(state_names(), ["imports.jsonl"])  # the dead holder's lock file went
        # And it is undone like any other: the home is what it was at the start.
        self.assertEqual(run("unimport", CLAUDE_WIN, "--yes")[0], 0)
        self.assertFalse(self.native.exists())

    def test_killed_between_two_units_then_undone(self):
        self.between_units()
        dry = run("unimport", CLAUDE_WIN, "--dry-run")
        self.assertEqual(dry, (1, "", f"agent-sessions: {unimport.UNFINISHED}\n"))
        self.assertEqual(self.kinds(), ["begin"])  # a dry run settles nothing
        with self.assertRaises(api.UndoFailed) as caught:
            api.unimport(CLAUDE_WIN)
        self.assertEqual(caught.exception.reason, unimport.TAKEN_OUT)
        self.assertTrue(caught.exception.no_record)
        self.assertFalse(self.native.exists())  # as it was before the import began
        self.assertEqual(self.kinds(), ["begin", "rollback"])
        self.assertEqual(state_names(), ["imports.jsonl"])
        # Settled: asked again, there is nothing to undo, and the import works.
        again = run("unimport", CLAUDE_WIN, "--yes", "--force")
        self.assertEqual(again, (1, "", f"agent-sessions: {unimport.NO_RECORD}\n"))
        self.assertEqual(run("import", self.source.key)[0], 0)
        self.copy_is_whole()

    def test_killed_between_two_units_then_undone_on_the_command_line(self):
        self.between_units()
        code, out, err = run("unimport", CLAUDE_WIN, "--yes")
        self.assertEqual((code, out, err), (1, "", f"agent-sessions: {unimport.TAKEN_OUT}\n"))
        self.assertFalse(self.native.exists())
        self.assertEqual(self.kinds(), ["begin", "rollback"])

    def test_killed_before_the_record_then_undone_on_the_command_line(self):
        self.killed("placed")
        code, _out, err = run("unimport", CLAUDE_WIN, "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("removed the imported copy", err)
        self.assertFalse(self.native.exists())
        self.assertEqual(self.kinds(), ["begin", "import", "undo"])

    def test_killed_with_everything_staged_and_nothing_in_place(self):
        begin = self.killed("first-unit")
        staging = os.path.relpath(begin["staging"], self.native)
        project = os.path.join("projects", encode_cwd(str(self.w.site)))
        self.assertEqual(
            self.everything_below(),
            [
                staging,
                *(os.path.join(staging, rel) for rel in ("0", "0/custom-title.json", "1")),
                "projects",
                project,  # made for the first unit, just before it was to be placed
            ],
        )
        self.assertEqual(run("import", self.source.key)[0], 0)
        self.copy_is_whole()
        self.assertEqual(self.kinds(), ["begin", "rollback", "begin", "import"])

    # --- after the last unit, before the record: a whole copy that nothing records

    def test_killed_before_the_record_then_imported_again(self):
        begin = self.killed("placed")
        self.copy_is_whole()
        self.w.scan()
        self.assertEqual(self.w.session(CLAUDE_WIN, "native").id, CLAUDE_WIN)  # listed
        code, out, err = run("import", self.source.key)
        self.assertEqual((code, err), (0, "agent-sessions: already imported; nothing was copied\n"))
        self.copy_is_whole()  # a copy that reads back whole is never removed
        self.assertEqual(self.kinds(), ["begin", "import"])
        record = journal.read()[1]
        # What the import would have recorded, and a manifest of the store as it is.
        self.assertEqual({key: record[key] for key in begin["import"]}, begin["import"])
        self.assertTrue(record["recovered"])
        self.assertRegex(record["recovered_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(
            record["manifest"]["dirs"], sorted([*begin["dirs"], begin["destinations"][0]])
        )
        for item in record["manifest"]["files"]:
            st = os.stat(item["path"])
            self.assertEqual((st.st_size, st.st_mtime_ns), (item["size"], item["mtime_ns"]))
        listed = sorted(
            os.path.relpath(i["path"], self.native) for i in record["manifest"]["files"]
        )
        self.assertEqual(listed, files_below(self.native))
        self.assertEqual(self.w.session(CLAUDE_WIN, "foreign").imported_as, out.strip())
        code, out, err = run("unimport", CLAUDE_WIN, "--yes")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.native.exists(), out)
        self.assertEqual(self.kinds(), ["begin", "import", "undo"])

    def test_killed_before_the_record_then_undone(self):
        self.killed("placed")
        report = api.unimport(CLAUDE_WIN)
        self.assertEqual((report.left, report.differences), ([], []))
        self.assertFalse(self.native.exists())
        self.assertEqual(self.kinds(), ["begin", "import", "undo"])
        self.assertEqual(state_names(), ["imports.jsonl"])

    def test_a_whole_copy_that_was_used_since_is_kept_and_recorded_as_it_is_now(self):
        begin = self.killed("placed")
        transcript = begin["destinations"][1]
        with open(transcript, "ab") as fh:  # the tool resumed it meanwhile
            fh.write(b'{"type":"user","message":{"role":"user","content":"more"}}\n')
        size = os.path.getsize(transcript)
        self.assertEqual(run("import", self.source.key)[0], 0)
        self.assertEqual(os.path.getsize(transcript), size)
        self.assertEqual(self.kinds(), ["begin", "import"])
        listed = {item["path"]: item["size"] for item in journal.read()[1]["manifest"]["files"]}
        self.assertEqual(listed[transcript], size)

    def test_a_copy_that_does_not_read_back_is_taken_out(self):
        begin = self.killed("placed")
        with open(begin["destinations"][1], "wb") as fh:
            fh.write(b"not a transcript\n")
        self.assertEqual(run("import", self.source.key)[0], 0)
        self.copy_is_whole()  # the new one
        self.assertEqual(self.kinds(), ["begin", "rollback", "begin", "import"])


class RecoverTest(ImportCase):
    """``importer.recover`` on a journal written by hand: what a ``begin``
    line may say, and what is done about it."""

    def setUp(self):
        super().setUp()
        self.plan = api._plan(self.source, self.index)
        self.root = self.plan.native_root
        self.side, self.transcript = self.plan.destinations

    def begin(self, **fields) -> dict:
        record = {
            "record": "begin",
            "version": 2,
            "begun_at": "2026-10-13T10:00:00Z",
            "provider": "claude",
            "id": CLAUDE_WIN,
            "destination_store": self.root,
            "destinations": [self.side, self.transcript],
            "dirs": [self.root, os.path.join(self.root, "projects"), os.path.dirname(self.side)],
            "staging": None,
            "import": {"source_key": "claude:x:y"},
        }
        record.update(fields)
        journal.append(record)
        return record

    def test_nothing_open_nothing_done_and_no_lock_taken(self):
        with mock.patch.object(importer.fsutil, "lock", side_effect=AssertionError("locked")):
            self.assertEqual(importer.recover(self.root), [])
        self.begin(destination_store="/another/store")  # another store's is not this one's
        with mock.patch.object(importer.fsutil, "lock", side_effect=AssertionError("locked")):
            self.assertEqual(importer.recover(self.root), [])

    def test_a_line_that_names_what_is_not_below_the_store_removes_nothing(self):
        victim = self.w.box.root / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        inside = Path(self.root) / "projects" / "mine.jsonl"
        inside.parent.mkdir(parents=True)
        inside.write_text("mine", encoding="utf-8")
        for fields in (
            {"destinations": [str(victim)]},
            {"destinations": [f"{self.root}/projects/../../../victim.txt"]},
            {"destinations": []},
            {"destinations": "not a list"},
            {"dirs": [str(self.w.box.root / "elsewhere")]},
            {"staging": str(inside)},  # not a name a staging directory has
            {"staging": f"{self.root}/projects/.agent-sessions-import-1-1"},  # not at the root
        ):
            with self.subTest(fields=fields):
                self.begin(**fields)
                with self.assertLogs("agent_sessions", "WARNING"):
                    (settled,) = importer.recover(self.root, Claude().read_back)
                self.assertEqual((settled["record"], settled["removed"]), ("rollback", []))
                self.assertTrue(settled["damaged"])
                self.assertEqual(journal.unsettled(journal.read()), [])
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(inside.read_text(), "mine")

    def test_whether_a_whole_copy_is_whole_is_the_providers_to_say(self):
        os.makedirs(self.side)
        Path(self.transcript).write_text("{}\n", encoding="utf-8")
        self.begin()
        # Without the provider's reading, nothing is done.
        with self.assertLogs("agent_sessions", "WARNING") as logged:
            self.assertEqual(importer.recover(self.root), [])
        self.assertIn("cannot be told here", logged.output[0])
        self.assertTrue(os.path.exists(self.transcript))
        # With it: not the session, so taken out, the directories with it.
        (settled,) = importer.recover(self.root, Claude().read_back)
        self.assertEqual(settled["record"], "rollback")
        self.assertTrue(settled["recovered"])
        self.assertFalse(os.path.exists(self.root))

    def test_a_copy_that_will_not_go_stays_open_and_is_tried_again(self):
        os.makedirs(self.side)
        self.begin()
        with (
            mock.patch.object(importer, "_remove"),
            self.assertLogs("agent_sessions", "WARNING") as logged,
        ):
            self.assertEqual(importer.recover(self.root, Claude().read_back), [])
        self.assertIn("will be tried again", logged.output[0])
        self.assertEqual(len(journal.unsettled(journal.read())), 1)
        (settled,) = importer.recover(self.root, Claude().read_back)
        self.assertIn(self.side, settled["removed"])
        self.assertFalse(os.path.exists(self.root))

    def test_a_journal_that_cannot_take_the_line_leaves_it_open(self):
        self.begin()
        with (
            mock.patch.object(journal, "append", side_effect=OSError(28, "No space")),
            self.assertLogs("agent_sessions", "WARNING"),
        ):
            self.assertEqual(importer.recover(self.root, Claude().read_back), [])
        self.assertEqual(len(journal.unsettled(journal.read())), 1)

    def test_a_plan_made_before_the_recovery_is_brought_up_to_date(self):
        """``Provider.import_session`` plans, then executes: what the plan
        found in the store may be what the recovery took out."""
        os.makedirs(self.side)
        self.begin()
        self.plan.existing = [self.side]  # as an adapter that looks for its names would find
        importer.execute(self.plan, verify=lambda: Claude().read_back(self.plan))
        self.assertEqual(
            [r["record"] for r in journal.read()], ["begin", "rollback", "begin", "import"]
        )


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
        again, and the journal is not left with half a line. It keeps the
        line of the import's beginning, which nothing closes while the disk
        is full; the next import closes it."""
        real_write = os.write
        seen = {"lines": 0}

        def write(fd, data):
            if os.path.samestat(os.fstat(fd), os.stat(journal.path())):
                seen["lines"] += 1
                if seen["lines"] > 1:  # the first is the ``begin`` record
                    seen.setdefault("placed", sorted(os.listdir(self.native / "projects")))
                    raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with mock.patch.object(journal.os, "write", write):
            reason = self.refused()
        self.assertEqual(len(seen["placed"]), 1)  # the copy was in the store at that moment
        self.assertEqual(seen["lines"], 3)  # begin; the import record; the rollback record
        self.assertIn("No space left on device", reason)
        self.assertIn("nothing was left in the native store", reason)
        self.assertFalse(self.native.exists())
        (begin,) = journal.read()
        self.assertEqual(begin["record"], "begin")
        self.assertTrue(journal.path().read_bytes().endswith(b"}\n"))  # whole lines only
        self.assertEqual(journal.unsettled(journal.read()), [begin])
        self.assertEqual(journal.not_undone(journal.read()), [])
        # With room again: the open line is closed, and the import goes through.
        self.assertEqual(api.import_session(self.w.session(CLAUDE_WIN, "foreign")).id, CLAUDE_WIN)
        self.assertEqual(
            [r["record"] for r in journal.read()], ["begin", "rollback", "begin", "import"]
        )
        self.assertTrue(journal.read()[1]["recovered"])

    def test_the_disk_is_full_before_anything_is_put_in_place(self):
        """The journal cannot take the line of the import's beginning:
        nothing appears in the native store, and no journal is left."""
        real_write = os.write

        def write(fd, data):
            if os.path.samestat(os.fstat(fd), os.stat(journal.path())):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with (
            mock.patch.object(journal.os, "write", write),
            mock.patch.object(importer, "_place", side_effect=AssertionError("placed")),
        ):
            reason = self.refused()
        self.assertIn("No space left on device", reason)
        self.nothing_left()


if __name__ == "__main__":
    unittest.main()
