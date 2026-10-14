"""launch.py and api.open_session. No process is started and no desktop
notification is sent: the starter and the notifier are replaced (see
tests/fixture_world.py), and ``exec`` is patched where ``here`` is tested.
Rules: docs/SPEC.md "Launch", docs/FORMATS.md section 5 of each provider."""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_NO_CWD,
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CODEX_NATIVE,
    GEMINI_WIN,
    GROK_NATIVE,
    World,
)
from support import FIXTURES

from agent_sessions import api, launch
from agent_sessions.launch import LaunchPlan
from agent_sessions.providers.gemini import Gemini

TERMINAL = "omarchy-launch-tui"
NO_TERMINAL = (
    "no terminal was found to open a window with; set AGENT_SESSIONS_TERMINAL to the command "
    "that starts yours (the tool's command is added at its end)"
)


class LaunchCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("claude",)
    native: tuple[str, ...] = ("claude", "codex", "grok")

    def setUp(self):
        self.w = World(self, self.foreign, self.native)
        self.home = self.w.home
        # Antigravity's native fixture holds no conversation; its Windows one,
        # placed in the home, gives native conversations with Windows folders.
        shutil.copytree(
            FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli",
            self.home / ".gemini" / "antigravity-cli",
            dirs_exist_ok=True,
        )
        self.index = self.w.scan()

    def failure(self, ref, **kwargs) -> str:
        with self.assertRaises(api.LaunchFailed) as caught:
            api.open_session(ref, **kwargs)
        return caught.exception.reason


class PlanTest(LaunchCase):
    def test_plan_for_each_provider(self):
        cases = (
            (
                CLAUDE_NATIVE,
                ["claude", "--resume", CLAUDE_NATIVE],
                "org.omarchy.claude",
                self.home / "My Proj",
            ),
            (CODEX_NATIVE, ["codex", "resume", CODEX_NATIVE], "org.omarchy.codex", self.home),
            (
                GROK_NATIVE,
                ["grok", "--resume", GROK_NATIVE],
                "org.omarchy.grok",
                self.home / "proj",
            ),
            (AGY_WIN, ["agy", "--conversation", AGY_WIN], "org.omarchy.agy", self.w.site),
        )
        for session_id, argv, app_id, cwd in cases:
            with self.subTest(argv=argv[0]):
                session = self.w.session(session_id, "native", self.index)
                plan = api.open_session(session.key, dry_run=True)
                self.assertEqual(
                    plan,
                    LaunchPlan(
                        cwd=str(cwd),
                        argv=argv,
                        command=[TERMINAL, f"--app-id={app_id}", *argv],
                        note=None,
                    ),
                )
                self.assertEqual(api.open_session(session, dry_run=True, index=self.index), plan)
        self.assertEqual((self.w.started, self.w.notified), ([], []))

    def test_open_starts_the_terminal_in_the_session_directory(self):
        plan = api.open_session(CLAUDE_NATIVE)
        self.assertEqual(self.w.started, [(plan.command, str(self.home / "My Proj"))])
        self.assertEqual(
            plan.command,
            [TERMINAL, "--app-id=org.omarchy.claude", "claude", "--resume", CLAUDE_NATIVE],
        )
        self.assertEqual(self.w.notified, [])

    def test_folder_gone_starts_in_the_home_directory_with_a_note(self):
        (self.home / "My Proj" / "sub").mkdir()
        shutil.rmtree(self.home / "My Proj")
        for index in (self.index, self.w.scan()):  # a stale listing, then a fresh one
            session = self.w.session(CLAUDE_NATIVE, "native", index)
            plan = api.open_session(session, dry_run=True)
            self.assertEqual(
                (plan.cwd, plan.note),
                (str(self.home), "project folder is gone; starting in the home directory"),
            )
            self.assertEqual(plan.argv, ["claude", "--resume", CLAUDE_NATIVE])
        # Never the parent that still exists; never a folder that came back elsewhere.
        api.open_session(CLAUDE_NATIVE)
        self.assertEqual(self.w.started[-1][1], str(self.home))

    def test_folder_unknown_starts_in_the_home_directory_with_a_note(self):
        plan = api.open_session(AGY_NO_CWD, dry_run=True)
        self.assertEqual(
            (plan.cwd, plan.note, plan.argv),
            (
                str(self.home),
                "project folder is unknown; starting in the home directory",
                ["agy", "--conversation", AGY_NO_CWD],
            ),
        )

    def test_a_folder_that_came_back_is_used_even_with_a_stale_listing(self):
        shutil.rmtree(self.home / "proj")
        stale = self.w.session(GROK_NATIVE, "native", self.w.scan())
        self.assertIsNone(stale.cwd_local)
        self.w.override({"/home/user": str(self.home), "/home/user/proj": str(self.home / "p2")})
        (self.home / "p2").mkdir()
        fresh = self.w.session(GROK_NATIVE, "native", self.w.scan())
        self.assertEqual(api.open_session(fresh, dry_run=True).cwd, str(self.home / "p2"))

    def test_foreign_session_is_refused(self):
        foreign = self.w.session(CLAUDE_WIN, "foreign", self.index)
        for dry_run in (True, False):
            self.assertEqual(
                self.failure(foreign.key, dry_run=dry_run),
                "this session is on C:; import it first",
            )
        self.assertEqual(self.w.started, [])
        # Only the real attempt tells the desktop.
        self.assertEqual(self.w.notified, ["this session is on C:; import it first"])

    def test_imported_foreign_session_resumes_the_native_copy(self):
        foreign = self.w.session(CLAUDE_WIN, "foreign", self.index)
        native = api.import_session(foreign)
        for ref in (foreign.key, foreign, self.w.session(CLAUDE_WIN, "foreign")):
            plan = api.open_session(ref, dry_run=True)
            self.assertEqual(
                (plan.cwd, plan.argv), (str(self.w.site), ["claude", "--resume", CLAUDE_WIN])
            )
        self.assertEqual(api.open_session(native.key, dry_run=True), plan)
        api.open_session(foreign.key)
        self.assertEqual(self.w.started, [(plan.command, str(self.w.site))])

    def test_tool_not_installed(self):
        self.w.installed.discard("codex")
        self.assertEqual(
            self.failure(CODEX_NATIVE, dry_run=True), "codex is not installed (not found on PATH)"
        )
        self.assertEqual(self.failure(CODEX_NATIVE), "codex is not installed (not found on PATH)")
        self.assertEqual(self.w.notified, ["codex is not installed (not found on PATH)"])
        self.assertEqual(self.w.started, [])
        self.assertFalse(self.w.session(CODEX_NATIVE, "native", self.w.scan()).launchable)

    def test_no_terminal_at_all(self):
        self.w.installed.discard(TERMINAL)
        reason = self.failure(CLAUDE_NATIVE)
        self.assertEqual(reason, NO_TERMINAL)
        self.assertEqual((self.w.started, self.w.notified), ([], [reason]))
        self.assertEqual(self.failure(CLAUDE_NATIVE, dry_run=True), NO_TERMINAL)
        # Resuming in this terminal does not need one.
        plan = api.open_session(CLAUDE_NATIVE, here=True, dry_run=True)
        self.assertEqual(plan.command, plan.argv)

    def test_unknown_and_ambiguous_keys(self):
        with self.assertRaises(api.NotFound):
            api.open_session("claude:0000")
        with self.assertRaises(api.NotFound) as caught:
            api.open_session("claude:", dry_run=True)
        self.assertIn("matches", caught.exception.reason)
        self.assertEqual((self.w.started, self.w.notified), ([], []))


class TerminalTest(LaunchCase):
    """Which terminal opens the window (docs/SPEC.md "Tool detection"):
    ``launch.terminal_command`` on a fake PATH. Nothing is started."""

    foreign = ()
    native = ("claude",)
    ARGV = ("claude", "--resume", CLAUDE_NATIVE)
    APP = "--app-id=org.omarchy.claude"
    # The table, as it is specified: the program and its own way of being
    # told which command to run.
    FORMS = (
        ("alacritty", ["alacritty", "-e"]),
        ("kitty", ["kitty"]),
        ("foot", ["foot"]),
        ("ghostty", ["ghostty", "-e"]),
        ("wezterm", ["wezterm", "start", "--"]),
        ("gnome-terminal", ["gnome-terminal", "--"]),
        ("konsole", ["konsole", "-e"]),
        ("xterm", ["xterm", "-e"]),
    )

    def command(self, *on_path, **env) -> list[str]:
        """What a resume would run with these programs on PATH beside the
        tool, and these variables set."""
        self.w.installed.clear()
        self.w.installed.update(("claude", *on_path))
        with mock.patch.dict(os.environ, env):
            plan = api.open_session(CLAUDE_NATIVE, dry_run=True)
        self.assertEqual(plan.argv, list(self.ARGV))
        return plan.command

    def refused(self, *on_path, **env) -> str:
        with self.assertRaises(api.LaunchFailed) as caught:
            self.command(*on_path, **env)
        return caught.exception.reason

    def test_the_table_is_what_is_specified(self):
        self.assertEqual(
            [(program, [program, *form]) for program, form in launch.TERMINALS], list(self.FORMS)
        )

    def test_each_known_terminal_alone_on_path(self):
        for program, prefix in self.FORMS:
            with self.subTest(program):
                self.assertEqual(self.command(program), [*prefix, *self.ARGV])

    def test_the_first_of_the_table_that_is_installed(self):
        programs = [program for program, _prefix in self.FORMS]
        for position, (_program, prefix) in enumerate(self.FORMS):
            self.assertEqual(self.command(*programs[position:]), [*prefix, *self.ARGV])
            self.assertEqual(self.command(*reversed(programs[position:])), [*prefix, *self.ARGV])

    def test_the_order_of_everything_that_can_name_a_terminal(self):
        everything = (TERMINAL, "xdg-terminal-exec", "myterm", "xterm", "special")
        env = {"TERMINAL": "myterm", "AGENT_SESSIONS_TERMINAL": "special --flag"}
        self.assertEqual(self.command(*everything, **env), ["special", "--flag", *self.ARGV])
        env["AGENT_SESSIONS_TERMINAL"] = "  "  # blank is not set
        self.assertEqual(self.command(*everything, **env), [TERMINAL, self.APP, *self.ARGV])
        self.assertEqual(
            self.command(*everything[1:], **env),
            ["xdg-terminal-exec", self.APP, "--", *self.ARGV],
        )
        self.assertEqual(self.command(*everything[2:], **env), ["myterm", "-e", *self.ARGV])
        self.assertEqual(self.command(*everything[3:], **env), ["xterm", "-e", *self.ARGV])
        self.assertEqual(self.refused(*everything[4:], **env), NO_TERMINAL)

    def test_the_users_own_command_line(self):
        spaced = str(self.w.path.directory / "my term")  # a program by its path
        cases = (
            ("myterm", ["myterm"]),
            ("myterm -e", ["myterm", "-e"]),
            (
                "  myterm --title 'agent sessions' -e  ",
                ["myterm", "--title", "agent sessions", "-e"],
            ),
            (spaced.replace(" ", "\\ ") + ' --class="a b" --', [spaced, "--class=a b", "--"]),
            # Nothing in it is run by a shell.
            ("myterm -e $(reboot) ; rm", ["myterm", "-e", "$(reboot)", ";", "rm"]),
        )
        for line, words in cases:
            with self.subTest(line):
                on_path = (words[0], TERMINAL, "xterm")
                self.assertEqual(
                    self.command(*on_path, AGENT_SESSIONS_TERMINAL=line), [*words, *self.ARGV]
                )

    def test_a_command_line_of_the_users_that_cannot_be_used_is_the_failure(self):
        everything = (TERMINAL, "xdg-terminal-exec", "xterm")
        self.assertEqual(
            self.refused(*everything, AGENT_SESSIONS_TERMINAL="myterm 'unclosed"),
            "AGENT_SESSIONS_TERMINAL is not a command line (No closing quotation)",
        )
        not_found = "AGENT_SESSIONS_TERMINAL names a terminal that was not found: "
        self.assertEqual(
            self.refused(*everything, AGENT_SESSIONS_TERMINAL="nowhere -e"), not_found + "nowhere"
        )
        self.assertEqual(self.refused(*everything, AGENT_SESSIONS_TERMINAL="''"), not_found)
        # A real attempt fails the same way, and says so on the desktop.
        self.w.installed.update(everything)
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_TERMINAL": "nowhere"}):
            reason = self.failure(CLAUDE_NATIVE)
        self.assertEqual(reason, not_found + "nowhere")
        self.assertEqual((self.w.started, self.w.notified), ([], [reason]))

    def test_the_terminal_variable(self):
        # A terminal the table knows gets its own form, with what the variable adds.
        self.assertEqual(
            self.command("wezterm", "xterm", TERMINAL="wezterm"),
            ["wezterm", "start", "--", *self.ARGV],
        )
        kitty = str(self.w.path.directory / "kitty")  # named by its path, with a flag
        self.assertEqual(
            self.command(kitty, "xterm", TERMINAL=f"{kitty} --single-instance"),
            [kitty, "--single-instance", *self.ARGV],
        )
        # Any other one is given -e.
        self.assertEqual(
            self.command("st", "xterm", TERMINAL="st -f 'Mono 12'"),
            ["st", "-f", "Mono 12", "-e", *self.ARGV],
        )
        # One that is not there, or is no command line, is passed over.
        for value in ("st", "st 'unclosed", "''", " "):
            self.assertEqual(self.command("xterm", TERMINAL=value), ["xterm", "-e", *self.ARGV])
            self.assertEqual(self.refused(TERMINAL=value), NO_TERMINAL)

    def test_the_window_is_opened_with_that_command(self):
        self.w.installed.clear()
        self.w.installed.update(("claude", "foot"))
        plan = api.open_session(CLAUDE_NATIVE)
        self.assertEqual(plan.command, ["foot", *self.ARGV])
        self.assertEqual(self.w.started, [(plan.command, str(self.home / "My Proj"))])

    def test_nothing_is_looked_for_when_the_tool_is_started_here(self):
        self.w.installed.clear()
        self.w.installed.add("claude")
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_TERMINAL": "nowhere 'unclosed"}):
            plan = api.open_session(CLAUDE_NATIVE, here=True, dry_run=True)
        self.assertEqual(plan.command, list(self.ARGV))


class GeminiTest(LaunchCase):
    foreign = ()
    native = ()

    def setUp(self):
        super().setUp()
        shutil.copytree(
            FIXTURES / "gemini" / "windows" / ".gemini", self.home / ".gemini", dirs_exist_ok=True
        )
        self.index = self.w.scan()

    def test_gemini_is_not_resumable_until_the_real_cli_is_installed(self):
        session = self.w.session(GEMINI_WIN, "native", self.index)
        self.assertEqual((session.launchable, session.note), (False, "Gemini CLI is not installed"))
        self.assertFalse(Gemini().resumable)
        self.w.path.wrapper("gemini")  # the script that would install it changes nothing
        self.assertFalse(Gemini().resumable)
        session = self.w.session(GEMINI_WIN, "native", self.w.scan())
        self.assertEqual((session.launchable, session.note), (False, "Gemini CLI is not installed"))
        expected = "Gemini sessions cannot be resumed here: the Gemini CLI is not installed"
        self.assertEqual(self.failure(session, dry_run=True), expected)
        self.assertEqual(self.failure(session.key), expected)
        self.assertEqual((self.w.started, self.w.notified), ([], [expected]))
        # What an install that did not finish leaves (FORMATS "What is on PATH").
        version = Path(os.environ["MISE_DATA_DIR"]) / "installs" / "gemini" / "0.63.0"
        version.mkdir(parents=True)
        (version / "package.json").write_text("{}", encoding="utf-8")
        self.assertFalse(self.w.session(GEMINI_WIN, "native", self.w.scan()).launchable)
        # The real program behind the wrapper: resumable, and launched through it.
        (version / "bin").mkdir()
        (version / "bin" / "gemini").write_text("#!/bin/sh\n", encoding="utf-8")
        (version / "bin" / "gemini").chmod(0o755)
        session = self.w.session(GEMINI_WIN, "native", self.w.scan())
        self.assertEqual((session.launchable, session.note), (True, None))
        self.assertTrue(Gemini().resumable)
        plan = api.open_session(session, dry_run=True)
        self.assertEqual(plan.argv, ["gemini", "--resume", GEMINI_WIN])

    def test_a_tool_that_needs_its_folder_fails_without_it(self):
        """Gemini once a real binary exists: resume is project-scoped."""
        session = self.w.session(GEMINI_WIN, "native", self.index)
        self.w.installed.add("gemini")
        with mock.patch.object(Gemini, "resumable", True):
            self.assertTrue(Gemini.resume_needs_cwd)
            (self.w.site / "x").mkdir()
            plan = api.open_session(session, dry_run=True)
            self.assertEqual(
                (plan.cwd, plan.argv, plan.note),
                (str(self.w.site), ["gemini", "--resume", GEMINI_WIN], None),
            )
            shutil.rmtree(self.w.site)
            session = self.w.session(GEMINI_WIN, "native", self.w.scan())
            self.assertEqual(
                self.failure(session),
                "the project folder f:\\proj\\my site is gone; Gemini can only resume from it",
            )
            session.cwd = None
            self.assertEqual(
                self.failure(session, dry_run=True),
                "the project folder is unknown; Gemini can only resume from it",
            )
        self.assertEqual(self.w.started, [])


class HereTest(LaunchCase):
    def test_here_changes_directory_and_execs_the_tool(self):
        calls = []
        with (
            mock.patch.object(launch.os, "chdir", lambda path: calls.append(("chdir", path))),
            mock.patch.object(
                launch.os, "execvp", lambda file, argv: calls.append(("execvp", file, argv))
            ),
        ):
            plan = api.open_session(GROK_NATIVE, here=True)
        argv = ["grok", "--resume", GROK_NATIVE]
        self.assertEqual(calls, [("chdir", str(self.home / "proj")), ("execvp", "grok", argv)])
        self.assertEqual(plan, LaunchPlan(str(self.home / "proj"), argv, argv, None))
        self.assertEqual(self.w.started, [])  # no new window

    def test_here_dry_run_does_nothing(self):
        with (
            mock.patch.object(launch.os, "chdir") as chdir,
            mock.patch.object(launch.os, "execvp") as execvp,
        ):
            plan = api.open_session(GROK_NATIVE, here=True, dry_run=True)
        self.assertEqual(plan.command, ["grok", "--resume", GROK_NATIVE])
        chdir.assert_not_called()
        execvp.assert_not_called()

    def test_exec_failure_is_reported_and_the_directory_restored(self):
        before = os.getcwd()
        with mock.patch.object(
            launch.os, "execvp", side_effect=FileNotFoundError(2, "No such file", "grok")
        ):
            reason = self.failure(GROK_NATIVE, here=True)
        self.assertEqual(os.getcwd(), before)
        self.assertEqual(reason, "could not start grok: No such file")
        self.assertEqual(self.w.notified, [reason])


class ForeignRefusalTest(LaunchCase):
    foreign = ("claude", "gemini")

    def test_a_foreign_session_that_cannot_be_imported_is_to_be_continued(self):
        """AUDIT A-25: ``open`` of a foreign Gemini session said ``import it
        first``, and ``import`` of it always refuses."""
        gemini = self.w.session(GEMINI_WIN, "foreign")
        with self.assertRaises(api.LaunchFailed) as caught:
            api.open_session(gemini, dry_run=True)
        self.assertEqual(
            caught.exception.reason,
            "this session is on C: and cannot be imported; continue it as a new session",
        )
        with self.assertRaises(api.LaunchFailed) as caught:
            api.open_session(CLAUDE_WIN)  # importable: as before
        self.assertEqual(caught.exception.reason, "this session is on C:; import it first")
        self.assertEqual(self.w.started, [])


class StarterTest(LaunchCase):
    def test_default_starter_is_detached(self):
        proc = mock.Mock()
        with (
            mock.patch.object(launch, "starter", launch._detached),
            mock.patch.object(launch.subprocess, "Popen", return_value=proc) as popen,
        ):
            plan = api.open_session(CLAUDE_NATIVE)
        popen.assert_called_once_with(
            plan.command,
            cwd=str(self.home / "My Proj"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.assertEqual(self.w.started, [])

    def test_a_starter_that_fails_becomes_a_launch_failure(self):
        def broken(command, cwd):
            raise PermissionError(13, "Permission denied", command[0])

        with mock.patch.object(launch, "starter", broken):
            reason = self.failure(CLAUDE_NATIVE)
        self.assertEqual(reason, "could not start omarchy-launch-tui: Permission denied")
        self.assertEqual(self.w.notified, [reason])

    def test_notification_is_best_effort(self):
        def broken(reason):
            raise RuntimeError("no notification daemon")

        self.w.installed.discard("claude")
        with mock.patch.object(launch, "notifier", broken):
            self.assertEqual(
                self.failure(CLAUDE_NATIVE), "claude is not installed (not found on PATH)"
            )

    def test_default_notifier(self):
        self.w.installed.add("notify-send")
        with mock.patch.object(launch.subprocess, "Popen") as popen:
            with mock.patch.dict(os.environ, {"AGENT_SESSIONS_NOTIFY": "0"}):
                launch._notify("reason")  # switched off (as in every test)
            popen.assert_not_called()
            with mock.patch.dict(os.environ, {"AGENT_SESSIONS_NOTIFY": "1"}):
                launch._notify("the reason")
            popen.assert_called_once()
            command = popen.call_args[0][0]
            self.assertEqual(
                (command[0], command[-2:]),
                (
                    str(self.w.path.directory / "notify-send"),
                    ["Cannot resume session", "the reason"],
                ),
            )
            self.assertTrue(popen.call_args[1]["start_new_session"])
            # notify-send is not installed: nothing is started.
            popen.reset_mock()
            self.w.installed.discard("notify-send")
            with mock.patch.dict(os.environ, {"AGENT_SESSIONS_NOTIFY": "1"}):
                launch._notify("reason")
            popen.assert_not_called()
        # It exists but cannot be started: the launch failure is what is raised.
        self.w.installed.add("notify-send")
        self.w.installed.discard("claude")
        with (
            mock.patch.dict(os.environ, {"AGENT_SESSIONS_NOTIFY": "1"}),
            mock.patch.object(launch, "notifier", launch._notify),
            mock.patch.object(
                launch.subprocess, "Popen", side_effect=OSError(8, "Exec format error")
            ) as popen,
        ):
            self.assertEqual(
                self.failure(CLAUDE_NATIVE), "claude is not installed (not found on PATH)"
            )
            popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
