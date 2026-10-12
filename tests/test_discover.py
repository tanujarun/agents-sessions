"""discover.py: fast pass, deep pass, prune list, the skeleton signatures."""

import os
import shutil
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES, FakeProvider, OtherProvider, Sandbox

from agent_sessions import discover, providers
from agent_sessions.discover import PRUNE, brute_pass, deep_pass, fast_pass
from agent_sessions.mounts import Mount, list_mounts
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.claude import Claude
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok

REAL = {
    "antigravity": Antigravity,
    "claude": Claude,
    "codex": Codex,
    "gemini": Gemini,
    "grok": Grok,
}


def roots(found) -> list[str]:
    return [s.root for s in found.stores]


class DeepPassTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.providers = providers.all_providers()

    def deep(self, **kwargs):
        return deep_pass(list_mounts(), self.providers, **kwargs)

    def test_mount_name_with_spaces(self):
        mount = self.box.mount("No Other Way")
        store = self.box.store(mount / "D Drive" / "My Stuff")
        found = self.deep()
        self.assertEqual(roots(found), [str(store)])
        s = found.stores[0]
        self.assertEqual(
            (s.provider, s.origin, s.mount, s.mount_label),
            ("fake", "foreign", str(mount), "No Other Way"),
        )

    def test_native_store_is_native(self):
        store = self.box.store(self.box.home)
        found = self.deep()
        self.assertEqual(
            [(s.root, s.origin, s.mount_label) for s in found.stores],
            [(str(store), "native", "Linux")],
        )

    @unittest.skipIf(os.geteuid() == 0, "root can read everything")
    def test_unreadable_dir_is_counted_not_fatal(self):
        mount = self.box.mount("Disk")
        locked = mount / "locked"
        self.box.store(locked)
        store = self.box.store(mount / "open")
        os.chmod(locked, 0)
        found = self.deep()
        self.assertEqual(roots(found), [str(store)])
        stats = found.stats[str(mount)]
        self.assertEqual((stats.errors, stats.error_paths), (1, [str(locked)]))

    def test_symlink_loop_and_symlinked_dirs_are_not_followed(self):
        mount = self.box.mount("Disk")
        store = self.box.store(mount / "a" / "b")
        os.symlink(mount / "a", mount / "a" / "b" / "loop")
        os.symlink(mount, mount / "a" / "top")
        elsewhere = self.box.root / "elsewhere"
        self.box.store(elsewhere)
        os.symlink(elsewhere, mount / "link")
        found = self.deep()
        self.assertEqual(roots(found), [str(store)])
        self.assertEqual(found.stats[str(mount)].dirs, 3)  # mount, a, b

    def test_nested_store_is_not_descended(self):
        mount = self.box.mount("Disk")
        outer = self.box.store(mount / "u")
        self.box.store(outer / "plugins" / "repo")
        self.box.store(outer / "sessions" / "deep")
        found = self.deep()
        self.assertEqual(roots(found), [str(outer)])
        self.assertEqual(found.stats[str(mount)].dirs, 2)  # mount, u

    def test_store_name_that_is_not_a_store_is_descended(self):
        mount = self.box.mount("Disk")
        (mount / "proj" / ".fake" / "settings").mkdir(parents=True)
        inner = self.box.store(mount / "proj" / ".fake" / "vendor")
        self.assertEqual(roots(self.deep()), [str(inner)])

    def test_prune_by_name_prefix_and_path_suffix(self):
        mount = self.box.mount("Win")
        for pruned in (
            "node_modules/pkg",
            "Windows/System32",
            "Program Files/App",
            "Program Files (x86)/App",
            "$RECYCLE.BIN/S-1",
            "$Recycle.Bin/S-1",
            "repo/.git/modules",
            "Users/u/AppData/Local/Temp/x",
            ".venv/lib",
            "py/site-packages/p",
            "Users/u/.npm/_cacache",
            "Users/u/.cache/x",
        ):
            self.box.store(mount / pruned)
        kept = [
            self.box.store(mount / keep)
            for keep in (
                "Projects/Temp",  # a user folder called Temp
                "Users/u/AppData/Local/Packages/x",  # AppData and Packages are never pruned
                "Users/u/AppData/Roaming/app",
                "Users/u/Local/Temp",  # not under AppData
                "usr/share",  # root-only names apply to "/" alone
                "var",
            )
        ]
        found = self.deep()
        self.assertEqual(roots(found), sorted(str(k) for k in kept))
        self.assertEqual(found.stats[str(mount)].pruned, 12)

    def test_prune_list_data(self):
        self.assertTrue(PRUNE.matches("Temp", "/m/Users/u/AppData/Local", False))
        self.assertFalse(PRUNE.matches("Temp", "/m/Users/u/Local", False))
        self.assertFalse(PRUNE.matches("Temp", "/m/AppData/NotLocal", False))
        self.assertFalse(PRUNE.matches("AppData", "/m/Users/u", False))
        self.assertFalse(PRUNE.matches("Packages", "/m/Users/u/AppData/Local", False))
        for name in ("proc", "sys", "dev", "run", "tmp", "var", "usr", "nix"):
            self.assertTrue(PRUNE.matches(name, "/", True), name)
            self.assertFalse(PRUNE.matches(name, "/mnt/x", False), name)
        self.assertTrue(PRUNE.matches("Program Files (x86)", "/m", False))
        self.assertFalse(PRUNE.matches("home", "/", True))

    def test_other_mountpoint_inside_a_mount_is_not_crossed(self):
        outer = self.box.mount("Outer")
        inner = outer / "nested" / "Inner"
        inner.mkdir(parents=True)
        self.box.set_mounts((outer, "Outer"), (inner, "Inner"))
        store = self.box.store(inner / "u")
        found = self.deep()
        self.assertEqual([(s.root, s.mount_label) for s in found.stores], [(str(store), "Inner")])
        self.assertEqual(found.stats[str(outer)].mount_skips, 1)
        self.assertEqual(found.stats[str(outer)].dirs, 2)  # Outer, nested
        self.assertEqual(found.stats[str(inner)].dirs, 2)  # Inner, u

    def test_store_on_an_unlisted_mountpoint_is_not_reached(self):
        outer = self.box.mount("Outer")
        self.box.store(outer / "pseudo" / "u")
        with unittest.mock.patch(
            "agent_sessions.mounts.mount_points", return_value={str(outer), str(outer / "pseudo")}
        ):
            found = self.deep()
        self.assertEqual(roots(found), [])

    def test_cancel_before_start(self):
        mount = self.box.mount("Disk")
        self.box.store(mount / "u")
        cancel = threading.Event()
        cancel.set()
        found = self.deep(cancel=cancel)
        self.assertTrue(found.cancelled)
        self.assertEqual(roots(found), [])

    def test_cancel_during_the_walk(self):
        mount = self.box.mount("Disk")
        for i in range(600):
            (mount / f"d{i:03}").mkdir()
        cancel = threading.Event()
        calls = []

        def progress(label, dirs):
            calls.append((label, dirs))
            cancel.set()

        old = discover.PROGRESS_EVERY
        discover.PROGRESS_EVERY = 0
        self.addCleanup(setattr, discover, "PROGRESS_EVERY", old)
        found = self.deep(progress=progress, cancel=cancel)
        self.assertTrue(found.cancelled)
        self.assertLess(found.stats[str(mount)].dirs, 601)
        self.assertEqual(calls[0][0], "Linux")
        self.assertNotIn(str(self.box.root / "nowhere"), found.stats)

    def test_progress_is_reported_per_mount(self):
        mount = self.box.mount("Disk")
        (mount / "a").mkdir()
        calls = []
        self.deep(progress=lambda label, dirs: calls.append((label, dirs)))
        self.assertEqual(calls, [("Linux", 2), ("Disk", 2)])

    def test_deterministic_and_sorted(self):
        a = self.box.mount("b-disk")
        b = self.box.mount("a disk")
        made = [self.box.store(p) for p in (a / "z", a / "m" / "n", b / "q", b / "Users" / "x")]
        first, second = self.deep(), self.deep()
        self.assertEqual(roots(first), sorted(str(m) for m in made))
        self.assertEqual(first.stores, second.stores)
        self.assertEqual(list(first.stats), sorted(first.stats))

    def test_two_providers_one_tree(self):
        self.box = Sandbox(self, {"fake": FakeProvider, "other": OtherProvider})
        mount = self.box.mount("Disk")
        fake = self.box.store(mount / "u")
        other = self.box.store(mount / "u", ".other")
        found = deep_pass(list_mounts(), providers.all_providers())
        self.assertEqual(
            [(s.provider, s.root) for s in found.stores],
            [("fake", str(fake)), ("other", str(other))],
        )


class RealSignatureWalkTest(unittest.TestCase):
    """Walker rules that need the real providers' names."""

    def setUp(self):
        self.box = Sandbox(self, REAL)
        self.providers = providers.all_providers()

    def test_gemini_is_descended_store_or_not(self):
        mount = self.box.mount("Win")
        store_home = mount / "Users" / "a"
        (store_home / ".gemini" / "tmp").mkdir(parents=True)
        (store_home / ".gemini" / "antigravity-cli" / "conversations").mkdir(parents=True)
        (store_home / ".gemini" / "antigravity-ide" / "conversations").mkdir(parents=True)
        plain_home = mount / "Users" / "b"
        (plain_home / ".gemini" / "antigravity-cli" / "conversations").mkdir(parents=True)
        (
            plain_home
            / ".gemini"
            / "antigravity-cli"
            / "conversations"
            / "antigravity-cli"
            / "conversations"
        ).mkdir(parents=True)  # inside a store: not reached
        found = deep_pass(list_mounts(), self.providers)
        self.assertEqual(
            [(s.provider, s.root) for s in found.stores],
            [
                ("antigravity", str(store_home / ".gemini" / "antigravity-cli")),
                ("antigravity", str(store_home / ".gemini" / "antigravity-ide")),
                ("antigravity", str(plain_home / ".gemini" / "antigravity-cli")),
                ("gemini", str(store_home / ".gemini")),
            ],
        )

    def test_codex_and_grok_are_told_apart_by_name(self):
        mount = self.box.mount("Win")
        (mount / "u" / ".codex" / "sessions" / "2026").mkdir(parents=True)
        (mount / "u" / ".grok" / "sessions" / "%2Fhome").mkdir(parents=True)
        found = deep_pass(list_mounts(), self.providers)
        self.assertEqual(
            [(s.provider, os.path.basename(s.root)) for s in found.stores],
            [("codex", ".codex"), ("grok", ".grok")],
        )

    def test_claude_desktop_stores_are_skipped_and_counted(self):
        mount = self.box.mount("Win")
        real = mount / "Users" / "a" / ".claude"
        (real / "projects").mkdir(parents=True)
        desktop = (
            mount
            / "Users"
            / "a"
            / "AppData"
            / "Local"
            / "Packages"
            / "Claude_x"
            / "LocalCache"
            / "Roaming"
            / "Claude"
            / "local-agent-mode-sessions"
            / "u1"
            / "u2"
            / "local_1"
            / ".claude"
        )
        (desktop / "projects").mkdir(parents=True)
        found = deep_pass(list_mounts(), self.providers)
        self.assertEqual(roots(found), [str(real)])
        self.assertEqual(
            [(s.provider, s.root, s.reason, s.mount) for s in found.skipped],
            [("claude", str(desktop), "Claude Desktop history", str(mount))],
        )
        # the same through the fast pass, when remembered
        again = fast_pass(list_mounts(), self.providers, [("claude", str(desktop))])
        self.assertEqual([s.root for s in again.skipped], [str(desktop)])
        self.assertEqual(roots(again), [str(real)])

    def test_fast_pass_checks_homes_and_antigravity(self):
        mount = self.box.mount("Win")
        linux = self.box.mount("OldLinux")
        expect = []
        for base in (
            mount / "Users" / "a",
            linux / "home" / "b",
            self.box.home,
            self.box.home.parent / "other",
        ):
            for rel in (
                ".claude/projects",
                ".codex/sessions",
                ".grok/sessions",
                ".gemini/tmp",
                ".gemini/antigravity-cli/conversations",
                ".gemini/antigravity-ide/conversations",
            ):
                (base / rel).mkdir(parents=True)
                expect.append(str((base / rel).parent))
        (mount / "deep" / "er" / ".claude" / "projects").mkdir(parents=True)  # walk only
        os.symlink(mount / "Users" / "a", mount / "Users" / "All Users")  # not a home
        found = fast_pass(list_mounts(), self.providers)
        self.assertEqual(sorted(roots(found)), sorted(expect))
        native = {s.provider: s.root for s in found.stores if s.origin == "native"}
        self.assertEqual(
            native,
            {
                "antigravity": str(self.box.home / ".gemini" / "antigravity-cli"),
                "claude": str(self.box.home / ".claude"),
                "codex": str(self.box.home / ".codex"),
                "gemini": str(self.box.home / ".gemini"),
                "grok": str(self.box.home / ".grok"),
            },
        )
        # the native antigravity-ide next to it is a foreign store
        ide = [
            s for s in found.stores if s.root == str(self.box.home / ".gemini" / "antigravity-ide")
        ]
        self.assertEqual([s.origin for s in ide], ["foreign"])


class FastPassTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.providers = providers.all_providers()

    def test_remembered_stores(self):
        mount = self.box.mount("Disk")
        store = self.box.store(mount / "far" / "away")
        gone = mount / "gone" / ".fake"
        self.assertEqual(roots(fast_pass(list_mounts(), self.providers)), [])
        found = fast_pass(
            list_mounts(),
            self.providers,
            [("fake", str(store)), ("fake", str(gone)), ("unknown", str(store))],
        )
        self.assertEqual(roots(found), [str(store)])

    def test_same_store_by_two_paths_is_one_store(self):
        mount = self.box.mount("Disk")
        store = self.box.store(mount / "Users" / "a")
        link = self.box.root / "link"
        os.symlink(store, link)
        found = fast_pass(list_mounts(), self.providers, [("fake", str(link))])
        self.assertEqual(roots(found), [str(store)])

    def test_native_store_through_a_symlinked_home(self):
        real = self.box.root / "home" / "real"
        self.box.store(real)
        self.box.home.rmdir()
        os.symlink(real, self.box.home)
        found = fast_pass(list_mounts(), self.providers)
        self.assertEqual(
            [(s.root, s.origin) for s in found.stores], [(str(real / ".fake"), "native")]
        )

    def test_native_store_outside_every_mount(self):
        self.box.store(self.box.home)
        found = fast_pass([Mount(str(self.box.root / "media"), "m")], self.providers)
        self.assertEqual(
            [(s.origin, s.mount, s.mount_label) for s in found.stores], [("native", "/", "Linux")]
        )

    def test_no_mounts_no_stores(self):
        self.assertEqual(fast_pass([], self.providers).stores, [])


class BrutePassTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.providers = providers.all_providers()

    def test_ignores_the_prune_list_and_descends_stores(self):
        mount = self.box.mount("Disk")
        plain = self.box.store(mount / "u")
        hidden = self.box.store(mount / "node_modules" / "pkg")
        inner = self.box.store(plain / "plugins")
        os.symlink(mount, mount / "loop")
        found = brute_pass(list_mounts(), self.providers)
        self.assertEqual(roots(found), sorted(str(p) for p in (plain, hidden, inner)))
        self.assertEqual([s.root for s in discover.nested_stores(found.stores)], [str(inner)])
        self.assertEqual(roots(deep_pass(list_mounts(), self.providers)), [str(plain)])

    def test_stops_at_other_mountpoints_and_cancels(self):
        outer = self.box.mount("Outer")
        inner = outer / "Inner"
        inner.mkdir()
        self.box.set_mounts((outer, "Outer"), (inner, "Inner"))
        store = self.box.store(inner)
        found = brute_pass(list_mounts(), self.providers)
        self.assertEqual([(s.root, s.mount_label) for s in found.stores], [(str(store), "Inner")])
        self.assertEqual(found.stats[str(outer)].mount_skips, 1)
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(brute_pass(list_mounts(), self.providers, cancel=cancel).cancelled)


class SkeletonSignatureTest(unittest.TestCase):
    """The five adapters' store signatures against tests/fixtures."""

    STORES = {
        "claude": ["claude/native/.claude", "claude/windows/.claude"],
        "codex": ["codex/native/.codex", "codex/windows/.codex"],
        "grok": ["grok/native/.grok", "grok/windows/.grok"],
        "antigravity": [
            "antigravity/native/.gemini/antigravity-cli",
            "antigravity/windows/.gemini/antigravity-cli",
            "antigravity/windows/.gemini/antigravity-ide",
            "gemini/native-no-chats/.gemini/antigravity-cli",
        ],
        "gemini": ["gemini/windows/.gemini"],
    }
    NOT_STORES = {
        "claude": ["claude/not-a-store/project/.claude", "claude/native"],
        "gemini": [
            "gemini/native-no-chats/.gemini",
            "antigravity/native/.gemini",
            "antigravity/windows/.gemini",
        ],
        "codex": ["codex/native", "claude/native/.claude"],
        "grok": ["grok/native", "claude/native/.claude"],
        "antigravity": ["antigravity/native/.gemini", "gemini/windows/.gemini"],
    }

    def test_signatures(self):
        for name, cls in REAL.items():
            provider = cls()
            for rel in self.STORES[name]:
                with self.subTest(provider=name, store=rel):
                    self.assertIn(os.path.basename(rel), provider.dir_names)
                    self.assertTrue(provider.is_store(FIXTURES / rel))
            for rel in self.NOT_STORES[name]:
                with self.subTest(provider=name, not_store=rel):
                    self.assertFalse(provider.is_store(FIXTURES / rel))

    def test_attributes(self):
        expect = {
            "claude": ("Claude", "claude", "org.omarchy.claude", True, {".claude"}),
            "codex": ("Codex", "codex", "org.omarchy.codex", True, {".codex"}),
            "grok": ("Grok", "grok", "org.omarchy.grok", True, {".grok"}),
            "antigravity": (
                "Antigravity",
                "agy",
                "org.omarchy.agy",
                True,
                {"antigravity-cli", "antigravity-ide"},
            ),
            "gemini": ("Gemini", "gemini", "org.omarchy.gemini", False, {".gemini"}),
        }
        self.assertEqual([p.name for p in providers.all_providers()], sorted(expect))
        for p in providers.all_providers():
            with self.subTest(provider=p.name):
                self.assertEqual(
                    (p.label, p.binary, p.app_id, p.resumable, p.dir_names), expect[p.name]
                )
                # Readers: tests/test_<provider>.py. Import: tests/test_importer.py.
                self.assertEqual(p.resume_needs_cwd, p.name == "gemini")

    def test_native_stores_follow_the_home_override(self):
        box = Sandbox(self, REAL)
        got = {p.name: str(p.native_store()) for p in providers.all_providers()}
        self.assertEqual(
            got,
            {
                "antigravity": str(box.home / ".gemini" / "antigravity-cli"),
                "claude": str(box.home / ".claude"),
                "codex": str(box.home / ".codex"),
                "gemini": str(box.home / ".gemini"),
                "grok": str(box.home / ".grok"),
            },
        )

    def test_fixture_tree_through_the_walker(self):
        """A deep pass over the fixtures meets each fixture store once and
        none of the negative cases; being under tests/fixtures they are
        skipped, so that a project's sample stores never reach the listing."""
        box = Sandbox(self, REAL)
        box.set_mounts((FIXTURES, "fixtures"))
        found = deep_pass(list_mounts(), providers.all_providers())
        got = sorted((s.provider, os.path.relpath(s.root, FIXTURES)) for s in found.skipped)
        want = sorted((name, rel) for name, rels in self.STORES.items() for rel in rels)
        self.assertEqual(got, want)
        self.assertEqual({s.reason for s in found.skipped}, {"test fixtures"})
        self.assertEqual(found.stores, [])

    def test_stores_inside_this_project_are_skipped(self):
        """A fake home left under the project's own tree (scratch/…) is
        found, counted and not listed, by all three passes."""
        box = Sandbox(self)
        mount = box.mount("Disk")
        project = mount / "Projects" / "agent-sessions"
        inside = box.store(project / "scratch" / "fake-home")
        direct = box.store(project)
        fixture = box.store(project / "tests" / "fixtures" / "sample")
        sibling = box.store(mount / "Projects" / "agent-sessions-old")
        outside = box.store(mount / "elsewhere")
        remembered = [("fake", str(p)) for p in (inside, direct, fixture, sibling, outside)]
        passes = {
            "fast": lambda: fast_pass(list_mounts(), providers.all_providers(), remembered),
            "deep": lambda: deep_pass(list_mounts(), providers.all_providers()),
            "brute": lambda: brute_pass(list_mounts(), providers.all_providers()),
        }
        with unittest.mock.patch.object(discover, "PROJECT_ROOT", str(project)):
            for name, run in passes.items():
                with self.subTest(run=name):
                    found = run()
                    self.assertEqual(roots(found), sorted([str(sibling), str(outside)]))
                    self.assertEqual(
                        sorted((s.root, s.reason, s.mount) for s in found.skipped),
                        sorted(
                            [
                                (str(inside), discover.IN_PROJECT, str(mount)),
                                (str(direct), discover.IN_PROJECT, str(mount)),
                                (str(fixture), "test fixtures", str(mount)),
                            ]
                        ),
                    )
        # An installed copy has no project around it: nothing is skipped by this rule.
        with unittest.mock.patch.object(discover, "PROJECT_ROOT", None):
            for name, run in passes.items():
                with self.subTest(run=name, installed=True):
                    found = run()
                    self.assertEqual(
                        roots(found), sorted(str(p) for p in (inside, direct, sibling, outside))
                    )
                    self.assertEqual([s.reason for s in found.skipped], ["test fixtures"])

    def test_the_project_root_is_this_checkout(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertEqual(discover.PROJECT_ROOT, str(repo))
        self.assertEqual(discover.IN_PROJECT, "inside the agent-sessions project")
        # Not a source tree (no tests/fixtures beside src/, or no src/ at all).
        tmp = Sandbox(self).root
        for layout in ("copy/src/agent_sessions", "site-packages/agent_sessions"):
            package = tmp / layout
            package.mkdir(parents=True)
            with unittest.mock.patch.object(discover, "__file__", str(package / "discover.py")):
                self.assertIsNone(discover._project_root(), layout)
        (tmp / "copy" / "tests" / "fixtures").mkdir(parents=True)
        with unittest.mock.patch.object(
            discover, "__file__", str(tmp / "copy/src/agent_sessions/discover.py")
        ):
            self.assertEqual(discover._project_root(), str(tmp / "copy"))

    def test_scan_and_verify_agree_on_stores_inside_the_project(self):
        from agent_sessions import api

        box = Sandbox(self)
        mount = box.mount("Disk")
        project = mount / "Projects" / "agent-sessions"
        inside = box.store(project / "scratch" / "fake-home")
        box.session(inside, "leftover")
        outside = box.store(mount / "elsewhere")
        box.session(outside, "real")
        with unittest.mock.patch("agent_sessions.index.shutil.which", return_value=None):
            api.scan(deep=True)  # before the rule: both are listed
            with unittest.mock.patch.object(discover, "PROJECT_ROOT", None):
                api.scan(deep=True)
                self.assertEqual(sorted(s.id for s in api.list_sessions()), ["leftover", "real"])
            with unittest.mock.patch.object(discover, "PROJECT_ROOT", str(project)):
                summary = api.scan()  # a quick scan drops the remembered store
                self.assertEqual((summary["stores"], summary["skipped_stores"]), (1, 1))
                self.assertEqual([s.id for s in api.list_sessions()], ["real"])
                self.assertEqual(
                    [(s["root"], s["reason"]) for s in api.status()["skipped_stores"]],
                    [(str(inside), discover.IN_PROJECT)],
                )
                result = api.verify()
                self.assertEqual((result["ok"], result["differences"]), (True, []))

    def test_same_tree_elsewhere_is_listed(self):
        box = Sandbox(self, REAL)
        mount = box.mount("Copy")
        shutil.copytree(FIXTURES, mount / "samples")
        shutil.copytree(FIXTURES / "claude", mount / "tests" / "fixtures-old" / "claude")
        found = deep_pass(list_mounts(), providers.all_providers())
        got = sorted(
            (s.provider, os.path.relpath(s.root, mount / "samples"))
            for s in found.stores
            if "/samples/" in s.root
        )
        want = sorted((name, rel) for name, rels in self.STORES.items() for rel in rels)
        self.assertEqual(got, want)
        self.assertEqual(len(found.stores), len(want) + 2)  # fixtures-old is not "fixtures"
        self.assertEqual(found.skipped, [])


if __name__ == "__main__":
    unittest.main()
