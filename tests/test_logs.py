"""logs.py and what the facade writes into the log (docs/SPEC.md "Logs")."""

import contextlib
import io
import json
import logging
import os
import re
import shlex
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_NATIVE, CLAUDE_WIN, GEMINI_WIN, World
from support import LOG_NAME, Sandbox, finish, launcher_child, python_child, state_names

from agent_sessions import api, cli, fsutil, logs
from agent_sessions.index import Index

POSIX = os.name == "posix"
LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (\d+) (DEBUG|INFO|WARNING|ERROR) (.*)$")

# Import everything a front end imports and set the log up, without writing.
QUIET = """
import os
from agent_sessions import api, cli, importer, journal, launch, logs
from agent_sessions.tui import engine, view
logs.logger()
logs.debug(False)
state, home = os.environ["AGENT_SESSIONS_STATE"], os.environ["AGENT_SESSIONS_HOME"]
print(os.path.exists(state), os.listdir(home))
"""


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class LogCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])
        self.path = self.state / LOG_NAME

    def lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def messages(self, level=None) -> list[str]:
        """The messages of the log's own lines (a traceback's are left out)."""
        found = []
        for line in self.lines():
            match = LINE.match(line)
            if match and level in (None, match.group(3)):
                found.append(match.group(4))
        return found


class LogFileTest(LogCase):
    def test_where_it_is(self):
        self.assertEqual(fsutil.log_file(), self.path)
        self.assertIsNone(logs.written())
        logs.logger().info("hello")
        self.assertEqual(logs.written(), self.path)

    def test_nothing_is_created_until_something_is_logged(self):
        child = python_child(QUIET)
        ((status, out, err),) = finish(self, [child])
        self.assertEqual((status, out, err), (0, "False []\n", ""))
        log = logs.logger()
        log.debug("below the level: not written")
        self.assertFalse(self.state.exists())
        log.info("first")
        self.assertEqual(os.listdir(self.state), [LOG_NAME])

    def test_private_file_in_a_private_directory(self):
        logs.logger().info("naïve ☃ %s", "text")
        if POSIX:
            self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertIn("naïve ☃ text", self.path.read_bytes().decode("utf-8"))

    def test_the_form_of_a_line(self):
        logs.logger().warning("scan: %s has %d", "x", 3)
        (line,) = self.lines()
        match = LINE.match(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.groups()[1:], (str(os.getpid()), "WARNING", "scan: x has 3"))

    def test_module_loggers_of_the_package_end_up_in_the_file(self):
        logs.logger()
        logging.getLogger("agent_sessions.journal").warning("from a module")
        self.assertEqual(self.messages("WARNING"), ["from a module"])

    def test_it_rotates_at_the_limit_and_keeps_three_older_files(self):
        self.assertEqual((logs.MAX_BYTES, logs.BACKUPS), (1024 * 1024, 3))
        with mock.patch.object(logs, "MAX_BYTES", 2000):
            logs.close()  # set up again, with the small limit
            log = logs.logger()
            for number in range(200):
                log.info("message %03d %s", number, "x" * 60)
        names = sorted(os.listdir(self.state))
        self.assertEqual(names, [LOG_NAME] + [f"{LOG_NAME}.{n}" for n in (1, 2, 3)])
        for name in names:
            self.assertLessEqual(os.path.getsize(self.state / name), 2000)
            if POSIX:
                self.assertEqual(stat.S_IMODE(os.stat(self.state / name).st_mode), 0o600)
        # The newest messages are in the file itself, older ones behind it.
        self.assertIn("message 199", self.lines()[-1])
        older = (self.state / f"{LOG_NAME}.1").read_text(encoding="utf-8").splitlines()
        self.assertLess(int(older[-1].split("message ")[1][:3]), int(self.lines()[0].split()[4]))
        self.assertEqual(state_names(), [])  # all of them are the log

    def test_a_new_state_directory_gets_its_own_file(self):
        logs.logger().info("one")
        other = self.box.root / "other-state"
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_STATE": str(other)}):
            logs.logger().info("two")
            self.assertEqual(logs.written(), other / LOG_NAME)
        logs.logger().info("three")
        self.assertEqual(self.messages(), ["one", "three"])
        self.assertIn("two", (other / LOG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(logging.getLogger(logs.NAME).handlers), 2)  # the null one and the file

    def test_it_does_not_reach_the_root_logger(self):
        seen = []
        handler = logging.Handler()
        handler.emit = seen.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        logs.logger().error("ours")
        self.assertEqual(seen, [])
        self.assertEqual(self.messages(), ["ours"])


class NeverBreaksTest(LogCase):
    """A log that cannot be written is dropped without a word."""

    def quiet(self, action):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            action()
        self.assertEqual(err.getvalue(), "")

    def test_the_state_directory_cannot_be_made(self):
        self.state.write_text("a file where the directory belongs", encoding="utf-8")
        self.quiet(lambda: logs.logger().error("lost"))
        self.quiet(lambda: logs.unexpected(ValueError("lost too")))
        self.assertTrue(self.state.is_file())
        self.assertIsNone(logs.written())

    @unittest.skipIf(not POSIX or os.geteuid() == 0, "needs a directory that refuses writes")
    def test_a_read_only_state_directory(self):
        self.state.mkdir()
        os.chmod(self.state, 0o500)
        self.addCleanup(os.chmod, self.state, 0o700)
        self.quiet(lambda: logs.logger().error("lost"))
        self.assertEqual(os.listdir(self.state), [])
        # A whole command runs, logs nothing, and says nothing about it.
        code, out, err = run("scan")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("scan: ", out)
        self.assertEqual(os.listdir(self.state), [])
        # Writable again: the next message is written.
        os.chmod(self.state, 0o700)
        logs.logger().info("back")
        self.assertEqual(self.messages(), ["back"])

    def test_a_log_that_is_a_directory(self):
        self.path.mkdir(parents=True)
        self.quiet(lambda: logs.logger().error("lost"))
        self.assertEqual(os.listdir(self.path), [])

    def test_a_write_that_fails(self):
        log = logs.logger()
        log.info("kept")
        with mock.patch.object(logs.PrivateRotatingFile, "flush", side_effect=OSError(28, "full")):
            self.quiet(lambda: log.info("lost"))
        log.info("after")
        self.assertEqual(self.messages()[0], "kept")
        self.assertEqual(self.messages()[-1], "after")


class SafeTextTest(LogCase):
    """A log is read in a terminal, and its values come from stores."""

    def test_a_message_is_one_line_with_nothing_a_terminal_acts_on(self):
        title = "evil\x1b]0;title\x07\x1b[2J \u202egnp.exe\n2026-01-01T00:00:00Z 1 INFO forged"
        logs.logger().info("import %s", title)
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(raw.count("\n"), 1)
        for bad in ("\x1b", "\x07", "\u202e", "\r"):
            self.assertNotIn(bad, raw)
        (message,) = self.messages()
        self.assertTrue(message.startswith("import evil ]0;title  [2J  gnp.exe 2026-01-01T"))

    def test_a_traceback_is_below_its_message_and_cannot_pass_for_log_lines(self):
        try:
            raise ValueError("bad\n2026-01-01T00:00:00Z 1 INFO forged\x1b[31m")
        except ValueError as exc:
            logs.unexpected(exc, "scan")
        lines = self.lines()
        self.assertEqual(len(self.messages()), 1)  # one line in the form of the log
        self.assertTrue(
            self.messages("ERROR")[0].startswith("unexpected error in scan: ValueError: bad 2026-")
        )
        self.assertEqual(lines[1], "  Traceback (most recent call last):")
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        self.assertIn("  ValueError: bad", lines)
        self.assertIn("  2026-01-01T00:00:00Z 1 INFO forged [31m", lines)
        self.assertNotIn("\x1b", "\n".join(lines))

    def test_an_exception_is_logged_once_however_often_it_is_reported(self):
        failure = RuntimeError("boom")
        logs.unexpected(failure, "inner")
        logs.unexpected(failure, "outer")
        self.assertEqual(self.messages("ERROR"), ["unexpected error in inner: RuntimeError: boom"])
        logs.unexpected(RuntimeError("boom"))  # another exception, the same words
        self.assertEqual(len(self.messages("ERROR")), 2)


class DebugTest(LogCase):
    def test_debug_lowers_the_level_and_copies_to_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            logs.logger().debug("unseen")
            logs.debug()
            logs.logger().debug("seen %s", "a\x1bb")
            logs.logger().info("and this")
            logs.debug(False)
            logs.logger().debug("unseen again")
            logs.logger().info("file only")
        self.assertEqual(self.messages(), ["seen a b", "and this", "file only"])
        self.assertEqual(
            [LINE.match(line).group(4) for line in err.getvalue().splitlines()],
            ["seen a b", "and this"],
        )

    def test_the_cli_flag(self):
        self.box.session(self.box.store(self.box.home), "a")
        code, _out, err = run("scan")
        self.assertEqual((code, err), (0, ""))
        code, out, err = run("--debug", "scan")
        self.assertEqual(code, 0)
        self.assertIn("scan: quick", out)
        logged = [LINE.match(line).group(4) for line in err.splitlines()]
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0].startswith("scan quick: "))
        self.assertEqual(len(self.messages()), 2)  # both scans are in the file
        self.assertIn("--debug", run("--help")[1])

    def test_debug_with_a_log_that_cannot_be_written_still_shows_on_stderr(self):
        self.state.write_text("in the way", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            logs.debug()
            logs.logger().info("shown")
        self.assertIn(" INFO shown", err.getvalue())


class FacadeTest(LogCase):
    """What ``api`` logs, with the fake provider."""

    def setUp(self):
        super().setUp()
        self.native = self.box.store(self.box.home)
        self.box.session(self.native, "a", title="one")
        self.box.session(self.native, "b", title="two")

    def test_a_scan_logs_its_summary(self):
        summary = api.scan(deep=True)
        (line,) = self.messages("INFO")
        self.assertRegex(
            line,
            r"^scan deep: \d+\.\d\d s, 1 mounts, 1 stores, 2 candidates "
            r"\(2 parsed, 0 from cache\), 2 sessions, 0 warnings$",
        )
        self.assertEqual(summary["sessions"], 2)
        api.scan()
        self.assertRegex(self.messages("INFO")[1], r"^scan quick: .* \(0 parsed, 2 from cache\)")

    def test_warnings_are_logged_once_per_scan_not_per_listing(self):
        (self.native / "sessions" / "broken.json").write_text("{ not json", encoding="utf-8")
        summary = api.scan()
        self.assertEqual(len(summary["warnings"]), 1)
        self.assertEqual(self.messages("WARNING"), [f"scan: {summary['warnings'][0]}"])
        for _ in range(3):  # what a front end does for every render
            self.assertEqual(api.load_index().warnings, summary["warnings"])
            api.list_sessions()
            api.status()
        self.assertEqual(len(self.messages("WARNING")), 1)
        api.scan()
        self.assertEqual(len(self.messages("WARNING")), 2)

    def test_a_warning_with_text_from_a_store_is_cleaned(self):
        self.box.session(self.native, "c", id="bad\x1b[2Jid\n")
        api.scan()
        (warning,) = self.messages("WARNING")
        self.assertIn("session id 'bad [2Jid '", warning)
        self.assertNotIn("\x1b", self.path.read_text(encoding="utf-8"))

    def test_an_unexpected_exception_is_logged_with_its_traceback_and_raised(self):
        with (
            mock.patch.object(Index, "scan", side_effect=RuntimeError("the disk went away")),
            self.assertRaises(RuntimeError),
        ):
            api.scan()
        self.assertEqual(
            self.messages("ERROR"),
            ["unexpected error in scan: RuntimeError: the disk went away"],
        )
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("  Traceback (most recent call last):", text)
        self.assertIn("  RuntimeError: the disk went away", text)

    def test_a_failure_the_caller_is_told_is_not_an_unexpected_one(self):
        with self.assertRaises(api.NotFound):
            api.find_session("no-such-session")
        with self.assertRaises(ValueError):
            api.list_sessions("no-such-provider")
        self.assertEqual(self.messages("ERROR"), [])

    def test_the_cli_shows_where_the_log_is(self):
        run("scan")
        code, out, _err = run("status")
        self.assertEqual(code, 0)
        self.assertIn(f"\nlog: {self.path}\n", out)
        self.assertEqual(json.loads(run("status", "--json")[1])["log"], str(self.path))


class ImportAndLaunchTest(unittest.TestCase):
    """Imports and launches, on the machine of tests/fixture_world.py."""

    def setUp(self):
        self.w = World(self, foreign=("claude", "gemini"), native=("claude",))
        self.path = Path(os.environ["AGENT_SESSIONS_STATE"]) / LOG_NAME
        self.index = self.w.scan()
        self.path.unlink()  # the scan's own lines
        logs.close()

    def messages(self, level=None) -> list[str]:
        found = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            match = LINE.match(line)
            if match and level in (None, match.group(3)):
                found.append(match.group(4))
        return found

    def test_an_import_and_its_result(self):
        source = self.w.session(CLAUDE_WIN, "foreign")
        report = api.import_report(source)
        lines = [m for m in self.messages() if m.startswith("import ")]
        self.assertEqual(lines[0], f"import {source.key} (store {source.store})")
        self.assertEqual(
            lines[1],
            f"import {source.key}: imported as {report.session.key}, 2 items, "
            f"{report.bytes} bytes: {', '.join(report.files)}",
        )
        api.import_report(self.w.session(CLAUDE_WIN, "foreign"))
        self.assertEqual(
            self.messages()[-1], f"import {source.key}: already imported as {report.session.key}"
        )
        # A dry run and a question are not imports.
        before = len(self.messages())
        api.can_import(source)
        api.import_plan(source)
        self.assertEqual(len(self.messages()), before)

    @unittest.skipUnless(POSIX, "file modes")
    def test_everything_we_keep_is_private(self):
        """After a scan, an import and a launch: the cache, the journal, the
        log, and the directories made for them."""
        api.import_session(self.w.session(CLAUDE_WIN, "foreign"))
        api.open_session(self.w.session(CLAUDE_NATIVE, "native"))
        state = Path(os.environ["AGENT_SESSIONS_STATE"])
        cache = Path(os.environ["AGENT_SESSIONS_CACHE"])
        self.assertEqual(sorted(os.listdir(state)), [LOG_NAME, "imports.jsonl"])
        self.assertEqual(os.listdir(cache), ["index.json"])
        for directory in (state, cache):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
            for name in os.listdir(directory):
                self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600, name)

    def test_a_refused_import(self):
        source = self.w.session(GEMINI_WIN, "foreign")
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(source)
        self.assertEqual(
            self.messages("WARNING"), [f"import {source.key} refused: {caught.exception.reason}"]
        )
        self.assertEqual(self.messages("ERROR"), [])

    def test_a_launch_logs_the_exact_command_and_directory(self):
        native = self.w.session(CLAUDE_NATIVE, "native")
        plan = api.open_session(native)
        self.assertEqual(self.w.started, [(plan.command, plan.cwd)])
        self.assertEqual(
            self.messages("INFO"),
            [f"launch {native.key}: cd {shlex.quote(plan.cwd)} && {shlex.join(plan.command)}"],
        )
        self.assertIn(f"--resume {CLAUDE_NATIVE}", self.messages()[0])
        api.open_session(native, here=True, dry_run=True)
        api.open_session(native, dry_run=True)
        self.assertEqual(len(self.messages()), 1)  # a dry run starts nothing and logs nothing

    def test_a_launch_in_this_terminal_is_logged_before_it_replaces_the_process(self):
        native = self.w.session(CLAUDE_NATIVE, "native")
        seen = {}

        def execvp(program, argv):
            seen["logged"] = self.messages("INFO")

        with (
            mock.patch("os.execvp", execvp),
            mock.patch("os.chdir"),
        ):
            plan = api.open_session(native, here=True)
        self.assertEqual(
            seen["logged"],
            [
                f"launch {native.key} (in this terminal): cd {shlex.quote(plan.cwd)} && "
                f"{shlex.join(plan.argv)}"
            ],
        )

    def test_a_refused_launch(self):
        foreign = self.w.session(CLAUDE_WIN, "foreign")
        with self.assertRaises(api.LaunchFailed) as caught:
            api.open_session(foreign)
        self.assertEqual(
            self.messages(), [f"launch {foreign.key} refused: {caught.exception.reason}"]
        )
        self.assertEqual(self.w.notified, [caught.exception.reason])  # told once, as before

    def test_a_launch_that_cannot_start(self):
        native = self.w.session(CLAUDE_NATIVE, "native")
        failing = mock.Mock(side_effect=OSError(8, "Exec format error", "omarchy-launch-tui"))
        with (
            mock.patch("agent_sessions.launch.starter", failing),
            self.assertRaises(api.LaunchFailed) as caught,
        ):
            api.open_session(native)
        self.assertEqual(len(self.messages("INFO")), 1)
        self.assertEqual(
            self.messages("WARNING"), [f"launch {native.key} refused: {caught.exception.reason}"]
        )


class RealProcessTest(unittest.TestCase):
    def test_a_scan_through_the_launcher_writes_the_log(self):
        box = Sandbox(self)
        box.session(box.store(box.home), "a")
        # The launcher's own registry has no fake provider: an empty machine.
        ((status, out, err),) = finish(self, [launcher_child("scan")])
        self.assertEqual((status, err), (0, ""), err)
        state = Path(os.environ["AGENT_SESSIONS_STATE"])
        (line,) = (state / LOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertRegex(line, r"^\S+Z \d+ INFO scan deep: ")
        self.assertIn("scan: deep", out)


if __name__ == "__main__":
    unittest.main()
