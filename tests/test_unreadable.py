"""A directory inside a store that cannot be listed: its sessions are not
listed, and the scan says so (SPEC "Discovery" 4) instead of dropping them
silently. Per adapter, and through the index."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES, Sandbox

from agent_sessions import api
from agent_sessions.model import Store
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import collect_warnings
from agent_sessions.providers.claude import Claude
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok

MESSAGE = "unreadable; its sessions are not listed"
GROK_GROUP = "sessions/F%3A%5CProj%5CMy%20Site"

# (adapter, fixture store, directories to lock one at a time; a "*" is
# replaced by the first name in that place).
CASES = (
    (Claude, "claude/windows/.claude", ("projects/F--Proj-My-Site", "projects")),
    (
        Codex,
        "codex/windows/.codex",
        ("sessions/2026/08/29", "sessions/2026/08", "sessions/2026", "sessions"),
    ),
    (Grok, "grok/windows/.grok", (GROK_GROUP, GROK_GROUP + "/*", "sessions")),
    (Gemini, "gemini/windows/.gemini", ("tmp/my-site/chats", "tmp/my-site", "tmp")),
    (Antigravity, "antigravity/windows/.gemini/antigravity-cli", ("conversations",)),
)


def lock(case, path) -> None:
    os.chmod(path, 0)
    case.addCleanup(os.chmod, path, 0o700)


@unittest.skipIf(os.geteuid() == 0, "root can read everything")
class AdapterTest(unittest.TestCase):
    def test_every_adapter_reports_a_directory_it_cannot_list(self):
        for adapter, fixture, directories in CASES:
            provider = adapter()
            tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-locked-"))
            self.addCleanup(shutil.rmtree, tmp, True)
            root = tmp / Path(fixture).name
            shutil.copytree(FIXTURES / fixture, root)
            store = Store(provider.name, str(root), "foreign", str(tmp), "tmp")
            with collect_warnings() as clean:
                before = sorted(c.id_hint for c in provider.candidates(store))
            self.assertEqual(clean.items, [])
            self.assertTrue(before)
            for relative in directories:
                locked = root / relative
                if locked.name == "*":
                    locked = sorted(p for p in locked.parent.iterdir() if p.is_dir())[0]
                with self.subTest(provider=provider.name, locked=relative):
                    # What the path that fails is: the directory itself, or
                    # the one below it that the adapter lists.
                    lost = [h for h in before if self._below(provider, store, h, locked)]
                    self.assertTrue(lost)
                    os.chmod(locked, 0)
                    try:
                        with collect_warnings() as collected:
                            got = sorted(c.id_hint for c in provider.candidates(store))
                    finally:
                        os.chmod(locked, 0o700)
                    self.assertEqual(got, [h for h in before if h not in lost])
                    self.assertEqual(len(collected.items), 1, collected.items)
                    path, _, message = collected.items[0].rpartition(": ")
                    self.assertEqual(message, MESSAGE)
                    self.assertTrue(
                        path == str(locked) or path.startswith(str(locked) + os.sep), path
                    )
                    with collect_warnings() as collected:
                        again = sorted(c.id_hint for c in provider.candidates(store))
                    self.assertEqual((again, collected.items), (before, []))

    @staticmethod
    def _below(provider, store, hint, directory) -> bool:
        """Whether the candidate's own file lies in ``directory``."""
        candidate = next(c for c in provider.candidates(store) if c.id_hint == hint)
        return candidate.paths[0].startswith(str(directory) + os.sep)

    def test_a_directory_that_is_not_there_is_not_reported(self):
        for adapter, _fixture, _directories in CASES:
            provider = adapter()
            tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-locked-"))
            self.addCleanup(shutil.rmtree, tmp, True)
            store = Store(provider.name, str(tmp / "nothing"), "foreign", str(tmp), "tmp")
            with collect_warnings() as collected:
                self.assertEqual(provider.candidates(store), [], provider.name)
            self.assertEqual(collected.items, [], provider.name)


@unittest.skipIf(os.geteuid() == 0, "root can read everything")
class IndexTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self, {"claude": Claude, "grok": Grok})
        self.disk = self.box.mount("WinC")
        self.profile = self.disk / "Users" / "user"
        self.profile.mkdir(parents=True)
        for name in ("claude", "grok"):
            shutil.copytree(FIXTURES / name / "windows", self.profile, dirs_exist_ok=True)
        which = mock.patch("shutil.which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def check(self, locked: Path, provider: str):
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [])
        before = [s.key for s in api.list_sessions()]
        mine = [s.key for s in api.list_sessions(provider)]
        self.assertTrue(mine)

        lock(self, locked)
        summary = api.scan()
        want = [f"{locked}: {MESSAGE}"]
        self.assertEqual(summary["warnings"], want)
        self.assertEqual(api.status()["warnings"], want)
        self.assertEqual(api.load_index().warnings, want)  # kept in the cache
        left = [s.key for s in api.list_sessions()]
        self.assertLess(len(left), len(before))
        self.assertTrue(set(left) < set(before))
        self.assertEqual(api.scan()["warnings"], want)  # and on every scan

        os.chmod(locked, 0o700)
        summary = api.scan()
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(api.status()["warnings"], [])
        self.assertEqual([s.key for s in api.list_sessions()], before)

    def test_a_locked_claude_project_directory(self):
        self.check(self.profile / ".claude" / "projects" / "F--Proj-My-Site", "claude")

    def test_a_locked_grok_group_directory(self):
        self.check(self.profile / ".grok" / GROK_GROUP, "grok")

    def test_a_locked_grok_session_directory(self):
        group = self.profile / ".grok" / GROK_GROUP
        api.scan(deep=True)
        listed = {Path(s.files[0]) for s in api.list_sessions("grok")}
        self.check(sorted(p for p in group.iterdir() if p in listed)[0], "grok")


if __name__ == "__main__":
    unittest.main()
