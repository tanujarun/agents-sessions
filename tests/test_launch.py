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

from fixture_world import (  # noqa: E402
    AGY_NO_CWD,
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CODEX_NATIVE,
    GEMINI_WIN,
    GROK_NATIVE,
    World,
)
from support import FIXTURES  # noqa: E402

from agent_sessions import api, launch  # noqa: E402
from agent_sessions.launch import LaunchPlan  # noqa: E402
from agent_sessions.providers.gemini import Gemini  # noqa: E402

TERMINAL = "omarchy-launch-tui"


class LaunchCase(unittest.TestCase):
    foreign = ("claude",)
    native = ("claude", "codex", "grok")

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
                "this session is on WinC; import it first",
            )
        self.assertEqual(self.w.started, [])
        # Only the real attempt tells the desktop.
        self.assertEqual(self.w.notified, ["this session is on WinC; import it first"])

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

    def test_launcher_missing(self):
        self.w.installed.discard(TERMINAL)
        reason = self.failure(CLAUDE_NATIVE)
        self.assertEqual(reason, "omarchy-launch-tui was not found; cannot open a terminal window")
        self.assertEqual((self.w.started, self.w.notified), ([], [reason]))
        # Resuming in this terminal does not need it.
        plan = api.open_session(CLAUDE_NATIVE, here=True, dry_run=True)
        self.assertEqual(plan.command, plan.argv)

    def test_unknown_and_ambiguous_keys(self):
        with self.assertRaises(api.NotFound):
            api.open_session("claude:0000")
        with self.assertRaises(api.NotFound) as caught:
            api.open_session("claude:", dry_run=True)
        self.assertIn("matches", caught.exception.reason)
        self.assertEqual((self.w.started, self.w.notified), ([], []))


class GeminiTest(LaunchCase):
    foreign = ()
    native = ()

    def setUp(self):
        super().setUp()
        shutil.copytree(
            FIXTURES / "gemini" / "windows" / ".gemini", self.home / ".gemini", dirs_exist_ok=True
        )
        self.index = self.w.scan()

    def test_gemini_is_not_resumable(self):
        session = self.w.session(GEMINI_WIN, "native", self.index)
        self.assertEqual((session.launchable, session.note), (False, "Gemini CLI is not installed"))
        self.w.installed.add("gemini")  # the wrapper script on PATH changes nothing
        expected = "Gemini sessions cannot be resumed here: the Gemini CLI is not installed"
        self.assertEqual(self.failure(session, dry_run=True), expected)
        self.assertEqual(self.failure(session.key), expected)
        self.assertEqual((self.w.started, self.w.notified), ([], [expected]))

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


class StarterTest(LaunchCase):
    def test_default_starter_is_detached(self):
        proc = mock.Mock()
        with (
            mock.patch.object(launch, "starter", launch._spawn),
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
                ("/usr/bin/notify-send", ["Cannot resume session", "the reason"]),
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
