"""What keeps an import inside the native store.

Names taken from the source store (ids, date directories, file names) become
names in the native store. ``importer.contained`` checks every destination
of a plan whatever the adapter did, and each adapter checks the names it
takes over before it uses them. The machine is the one of
tests/fixture_world.py."""

import copy
import dataclasses
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_WIN, CODEX_WIN, GROK_WIN, World
from support import LOG_NAME, drop_state, state_names, write_jsonl

from agent_sessions import api, importer
from agent_sessions.providers.claude import encode_cwd
from agent_sessions.providers.codex import Codex

OTHER = "0f0f0f0f-1111-4222-8333-444444444444"
CODEX_ROLLOUT = f"rollout-2026-08-29T13-57-23-{CODEX_WIN}.jsonl"


def other_filesystem(case, here) -> Path | None:
    """A fresh temp directory on another filesystem than ``here``, removed
    with the test; None when this machine offers none."""
    device = os.stat(here).st_dev
    for base in ("/dev/shm", "/var/tmp"):
        try:
            if os.stat(base).st_dev == device or not os.access(base, os.W_OK):
                continue
        except OSError:
            continue
        holder = tempfile.TemporaryDirectory(prefix="agent-sessions-test-", dir=base)
        case.addCleanup(holder.cleanup)
        return Path(holder.name)
    return None


def snapshot(*roots) -> dict:
    """Every file and directory below the roots: a regular file with its
    bytes, anything else (a directory, a link, a named pipe) with its kind.
    The log is left out: every scan and every import through the facade adds
    to it, and it is not what an import leaves or takes back."""
    found: dict[str, bytes | int] = {}
    for root in roots:
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
                if name == LOG_NAME:
                    continue
                path = os.path.join(base, name)
                mode = os.lstat(path).st_mode
                if stat.S_ISREG(mode):
                    with open(path, "rb") as fh:
                        found[path] = fh.read()
                else:
                    found[path] = stat.S_IFMT(mode)
    return found


class SafetyCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("claude", "codex", "grok")

    def setUp(self):
        self.w = World(self, self.foreign)
        self.home = self.w.home
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])
        self.index = self.w.scan()

    def source(self, session_id):
        return self.w.session(session_id, "foreign", self.w.scan())

    def plan(self, session_id=CLAUDE_WIN) -> importer.ImportPlan:
        return api._plan(self.source(session_id), api.load_index())

    def refused(self, plan) -> str:
        """Both ``check`` and ``execute`` refuse; nothing is written."""
        before = snapshot(self.w.box.root)
        reasons = []
        for action in (importer.contained, importer.check, importer.execute):
            with self.assertRaises(importer.ImportFailed) as caught:
                action(plan)
            reasons.append(caught.exception.reason)
        self.assertEqual(len(set(reasons)), 1, reasons)
        self.assertEqual(snapshot(self.w.box.root), before)
        return reasons[0]


class ContainedTest(SafetyCase):
    foreign = ("claude",)

    def setUp(self):
        super().setUp()
        self.root = str(self.home / ".claude")
        self.good = self.plan()
        self.assertEqual([unit.is_dir for unit in self.good.units], [True, False])
        importer.contained(self.good)
        importer.check(self.good)

    def with_dst(self, dst, unit=1):
        plan = copy.deepcopy(self.good)
        plan.units[unit].dst = dst
        return plan

    def test_a_destination_must_be_below_the_native_root(self):
        outside = self.w.box.root / "elsewhere"
        outside.mkdir()
        for dst in (
            str(outside / "x.jsonl"),
            f"{self.root}/../.bashrc",
            f"{self.root}/projects/../../.bashrc",
            f"{self.root}/projects/./x.jsonl",
            f"{self.root}/projects//x.jsonl",
            f"{self.root}//etc/passwd",
            f"{self.root}/",
            self.root,
            f"{self.root}.evil/x.jsonl",
            f"{self.root}/projects/x\x00.jsonl",
            "projects/x.jsonl",
            "",
            "/",
        ):
            for unit in (0, 1):
                with self.subTest(dst=dst, unit=unit):
                    reason = self.refused(self.with_dst(dst, unit))
                    self.assertIn("nothing was imported", reason)

    def test_a_path_below_a_unit_must_stay_below_it(self):
        for rel in (
            "../x",
            "..",
            ".",
            "",
            "a/../../x",
            "a//b",
            "a/./b",
            "/etc/passwd",
            "a/",
            "a\x00b",
        ):
            for kind in ("files", "dirs"):
                with self.subTest(rel=rel, kind=kind):
                    plan = copy.deepcopy(self.good)
                    if kind == "files":
                        plan.units[0].files[0].rel = rel
                    else:
                        plan.units[0].dirs.append(rel)
                    self.assertIn("is not a path below", self.refused(plan))

    def test_a_single_file_unit_is_one_file(self):
        plan = copy.deepcopy(self.good)
        plan.units[1].files[0].rel = "x"
        self.assertIn("is planned as one file", self.refused(plan))
        plan = copy.deepcopy(self.good)
        plan.units[1].files = []
        self.assertIn("is planned as one file", self.refused(plan))

    def test_an_appended_file_must_be_below_the_native_root(self):
        for path in (str(self.home / ".bashrc"), f"{self.root}/../.profile", "/etc/passwd"):
            plan = copy.deepcopy(self.good)
            plan.appends.append(importer.Append(path, b"x\n", "a line"))
            self.assertIn("nothing was imported", self.refused(plan))
        self.assertFalse((self.home / ".bashrc").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_line_is_only_appended_to_a_plain_file(self):
        os.makedirs(self.root)
        index = os.path.join(self.root, "session_index.jsonl")
        plan = copy.deepcopy(self.good)
        plan.appends.append(importer.Append(index, b"x\n", "a line"))
        importer.check(plan)  # not there yet: it would be created
        os.mkfifo(index)  # opening it for reading would wait for a writer
        self.assertIn("is not a regular file", self.refused(plan))
        os.unlink(index)
        os.mkdir(index)
        self.assertIn("is not a regular file", self.refused(plan))

    def test_a_native_root_that_is_not_absolute(self):
        for root in ("", "relative/.claude", "\x00"):
            plan = dataclasses.replace(copy.deepcopy(self.good), native_root=root)
            self.assertIn("is not an absolute path", self.refused(plan))

    def test_an_import_goes_through_a_linked_sub_directory(self):
        """``~/.claude/projects`` is a link to a directory elsewhere, the way
        a user keeps the transcripts on another disk. The native store is
        the user's own: the link is followed, as the tool follows it."""
        elsewhere = self.w.box.root / "other-disk" / "claude-projects"
        elsewhere.mkdir(parents=True)
        os.makedirs(self.root)
        os.symlink(elsewhere, os.path.join(self.root, "projects"))
        importer.contained(self.good)
        source = self.source(CLAUDE_WIN)
        self.assertEqual(api.can_import(source), (True, ""))
        native = api.import_session(source)
        self.assertEqual(
            (native.id, native.origin, native.launchable), (CLAUDE_WIN, "native", True)
        )
        folder = encode_cwd(str(self.w.site))
        copied = elsewhere / folder / f"{CLAUDE_WIN}.jsonl"
        self.assertEqual(
            copied.read_bytes(),
            (
                self.w.profile / ".claude/projects/F--Proj-My-Site" / f"{CLAUDE_WIN}.jsonl"
            ).read_bytes(),
        )
        self.assertTrue((elsewhere / folder / CLAUDE_WIN).is_dir())
        # The link is still a link, and the record names the paths as the store has them.
        self.assertTrue(os.path.islink(os.path.join(self.root, "projects")))
        (record,) = importer.read_journal()
        self.assertEqual(
            record["destination_files"],
            [
                os.path.join(self.root, "projects", folder, CLAUDE_WIN),
                os.path.join(self.root, "projects", folder, f"{CLAUDE_WIN}.jsonl"),
            ],
        )
        self.assertEqual(sorted(os.listdir(self.root)), ["projects"])  # no staging left
        self.assertEqual(api.import_report(self.source(CLAUDE_WIN)).created, False)

    def test_a_link_the_reader_does_not_follow_ends_in_a_clean_refusal(self):
        """The project's own directory is the link. ``contained`` lets it
        pass, the copy is placed through it, and the read-back does not find
        it: the Claude reader lists real project directories only
        (``fsutil.subdirs``), in the native store as anywhere. The import is
        taken out again and says why."""
        elsewhere = self.w.box.root / "other-disk" / "site"
        elsewhere.mkdir(parents=True)
        project = os.path.dirname(self.good.units[1].dst)
        os.makedirs(os.path.dirname(project))
        os.symlink(elsewhere, project)
        importer.contained(self.good)
        before = snapshot(self.w.box.root)
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(self.source(CLAUDE_WIN))
        self.assertEqual(
            caught.exception.reason,
            f"the copy does not read as session {CLAUDE_WIN}; it was removed again and "
            "nothing was left in the native store",
        )
        self.assertEqual(os.listdir(elsewhere), [])
        after = snapshot(self.w.box.root)
        cache = str(self.w.box.cache)  # the scans of this test rewrote the index
        self.assertEqual(
            {k: v for k, v in after.items() if not k.startswith(cache)},
            {k: v for k, v in before.items() if not k.startswith(cache)},
        )

    def test_a_failed_import_through_a_link_leaves_the_linked_directory_as_it_was(self):
        elsewhere = self.w.box.root / "other-disk" / "claude-projects"
        (elsewhere / "kept").mkdir(parents=True)
        os.makedirs(self.root)
        os.symlink(elsewhere, os.path.join(self.root, "projects"))
        before = snapshot(self.w.box.root)
        with self.assertRaises(importer.ImportFailed):
            importer.execute(self.good, verify=mock.Mock(side_effect=ValueError("no")))
        self.assertEqual(snapshot(self.w.box.root), before)

    def test_a_link_to_another_filesystem_is_refused_in_plain_words(self):
        """The copy is staged at the root of the native store; a move from
        there to another filesystem cannot be one step. (Simulated: the
        error every move across filesystems gives.)"""
        elsewhere = self.w.box.root / "other-disk" / "claude-projects"
        elsewhere.mkdir(parents=True)
        os.makedirs(self.root)
        os.symlink(elsewhere, os.path.join(self.root, "projects"))
        before = snapshot(self.w.box.root)
        crossing = OSError(18, "Invalid cross-device link")
        with (
            mock.patch.object(importer.os, "link", side_effect=crossing),
            mock.patch.object(importer.os, "replace", side_effect=crossing),
            mock.patch.object(importer.os, "rename", side_effect=crossing),
            self.assertRaises(importer.ImportFailed) as caught,
        ):
            importer.execute(self.good)
        self.assertIn("is on another filesystem than the native store", caught.exception.reason)
        self.assertIn(os.path.dirname(self.good.units[0].dst), caught.exception.reason)
        self.assertEqual(snapshot(self.w.box.root), before)

    def test_a_link_to_a_real_other_filesystem(self):
        """The same with the kernel's own answer, where the machine has a
        second filesystem to try it on."""
        elsewhere = other_filesystem(self, self.w.box.root)
        if elsewhere is None:
            self.skipTest("no second writable filesystem for temp files")
        os.makedirs(self.root)
        os.symlink(elsewhere, os.path.join(self.root, "projects"))
        before = snapshot(self.w.box.root)
        with self.assertRaises(api.ImportFailed) as caught:
            importer.execute(self.good)
        self.assertIn("is on another filesystem than the native store", caught.exception.reason)
        self.assertEqual(snapshot(self.w.box.root), before)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_link_that_stays_inside_the_native_root_is_fine(self):
        os.makedirs(os.path.join(self.root, "real-projects"))
        os.symlink("real-projects", os.path.join(self.root, "projects"))
        importer.check(self.good)
        native = api.import_session(self.source(CLAUDE_WIN))
        self.assertTrue(
            os.path.isfile(
                os.path.join(
                    self.root, "real-projects", encode_cwd(str(self.w.site)), f"{CLAUDE_WIN}.jsonl"
                )
            )
        )
        self.assertEqual(native.id, CLAUDE_WIN)

    def test_a_native_root_that_is_itself_a_link_keeps_working(self):
        """``~/.claude`` kept in a dotfiles repository."""
        dotfiles = self.w.box.root / "home" / "dotfiles" / "claude"
        dotfiles.mkdir(parents=True)
        os.symlink(dotfiles, self.root)
        source = self.source(CLAUDE_WIN)
        self.assertEqual(api.can_import(source), (True, ""))
        native = api.import_session(source)
        self.assertEqual((native.id, native.origin), (CLAUDE_WIN, "native"))
        copied = dotfiles / "projects" / encode_cwd(str(self.w.site)) / f"{CLAUDE_WIN}.jsonl"
        self.assertTrue(copied.is_file())
        self.assertTrue(os.path.islink(self.root))
        # The staging directory was made, and removed, where the link leads.
        self.assertEqual(os.listdir(dotfiles), ["projects"])
        # And a link below it is followed too.
        plain = "69669cf3-d7ae-4ba4-a028-c6abfd2bd622"
        other = self.w.box.root / "other-disk"
        shutil.move(dotfiles / "projects", other)
        os.symlink(other, dotfiles / "projects")
        self.assertEqual(api.import_session(self.source(plain)).id, plain)
        self.assertTrue((other / encode_cwd(str(self.w.site)) / f"{plain}.jsonl").is_file())

    def test_the_names_are_still_checked_when_links_are_about(self):
        """Following the user's links does not loosen the rule for the names
        a plan carries: ``..`` is refused before any link is looked at."""
        os.makedirs(os.path.join(self.root, "projects"))
        os.symlink(self.w.box.root, os.path.join(self.root, "up"))
        for dst in (f"{self.root}/up/../x.jsonl", f"{self.root}/projects/../../x.jsonl"):
            self.assertIn("nothing was imported", self.refused(self.with_dst(dst)))


class DepthAndLengthTest(SafetyCase):
    """docs/AUDIT.md A-05: a side directory is copied whole, and how deep it
    nests and how long its paths are is the source's to decide. What the
    native store cannot hold, or a roll back could not be trusted to remove,
    is refused before anything is written."""

    foreign = ("claude",)

    def setUp(self):
        super().setUp()
        self.side = self.w.profile / ".claude" / "projects" / "F--Proj-My-Site" / CLAUDE_WIN
        self.copy = self.home / ".claude" / "projects" / encode_cwd(str(self.w.site)) / CLAUDE_WIN

    def nest(self, names) -> Path:
        """Directories below the side directory, one in the other, and a
        file in the last; returns the file."""
        deepest = self.side
        for name in names:
            deepest = deepest / name
            os.mkdir(deepest)
        (deepest / "f").write_text("x", encoding="utf-8")
        return deepest / "f"

    def reason(self) -> str:
        """The dry run, ``import_options`` and the import refuse alike, and
        nothing is written."""
        source = self.source(CLAUDE_WIN)
        before = snapshot(self.home, self.state)
        reasons = []
        for action in (api.import_plan, api.import_session):
            with self.assertRaises(api.ImportFailed) as caught:
                action(source)
            reasons.append(caught.exception.reason)
        self.assertEqual(len(set(reasons)), 1, reasons)
        self.assertEqual(api.import_options(source), api.ImportOptions(False, reasons[0]))
        self.assertEqual(snapshot(self.home, self.state), before)
        self.assertFalse((self.home / ".claude").exists())
        return reasons[0]

    def test_a_directory_nested_deeper_than_64_levels_is_refused(self):
        self.assertEqual(importer.DEPTH_MAX, 64)
        self.nest(["a"] * 65)
        self.assertEqual(
            self.reason(),
            f"{self.side} holds directories nested more than 64 levels deep, and a session "
            "like that is not imported; nothing was imported",
        )

    def test_64_levels_are_imported(self):
        deepest = self.nest(["a"] * 64)
        api.import_session(self.source(CLAUDE_WIN))
        self.assertEqual((self.copy / deepest.relative_to(self.side)).read_text(), "x")
        api.unimport(CLAUDE_WIN)
        self.assertFalse((self.home / ".claude").exists())

    def test_a_plan_made_any_other_way_is_measured_too(self):
        """``execute`` does not rely on the adapter having walked the tree
        with ``importer.tree``."""
        deep = os.sep.join(["d"] * 65)
        for change in (
            lambda unit: unit.dirs.append(deep),
            lambda unit: unit.files.append(importer.FileCopy("/s", os.path.join(deep, "f"))),
        ):
            plan = self.plan()
            change(plan.units[0])
            for action in (importer.fits, importer.check, importer.execute):
                with self.assertRaises(importer.ImportFailed) as caught:
                    action(plan)
                self.assertIn("nested more than 64 levels deep", caught.exception.reason)
        # A file in the sixty-fourth directory is as deep as it goes.
        plan = self.plan()
        plan.units[0].dirs.append(os.sep.join(["d"] * 64))
        plan.units[0].files.append(importer.FileCopy("/s", os.sep.join(["d"] * 64 + ["f"])))
        importer.fits(plan)
        self.assertFalse((self.home / ".claude").exists())

    @unittest.skipUnless(hasattr(os, "pathconf"), "asks the filesystem for its limits")
    def test_a_path_that_fits_on_the_other_drive_and_not_here_is_refused(self):
        """The auditor's case in small: the longest path of the source is
        as long as a path may be, and its place in the native store lies
        below a longer directory."""
        limit = os.pathconf(self.home, "PC_PATH_MAX")
        self.assertGreater(len(str(self.copy)), len(str(self.side)))
        room = limit - 1 - len(str(self.side)) - len("/f")  # for the directories, "/" included
        names = []
        while room > 1:
            length = min(200, room - 1)
            if room - (length + 1) == 1:  # a rest of one byte holds no "/x"
                length -= 1
            names.append("n" * length)
            room -= length + 1
        self.assertEqual(room, 0)
        longest = self.nest(names)
        self.assertEqual(len(str(longest)), limit - 1)
        self.assertEqual(longest.read_text(), "x")  # the source can be read: it fits there
        needed = len(str(self.copy)) + len(str(longest)) - len(str(self.side))
        self.assertEqual(
            self.reason(),
            f"the copy of {self.side} would need a path of {needed:,} bytes below "
            f"{self.home / '.claude'}, and at most {limit - 1:,} fit there; nothing was imported",
        )

    def test_the_limits_are_the_filesystems_and_the_staged_path_counts(self):
        self.nest(["a", "b" * 20])
        longest = len(str(self.copy / "a" / ("b" * 20) / "f"))
        measured = []

        def limit(where, name, default):
            measured.append((where, name, default))
            return {"PC_PATH_MAX": longest + 1, "PC_NAME_MAX": 255}[name]

        with mock.patch.object(importer, "_limit", limit):
            plan = self.plan()
            importer.fits(plan)  # to the byte
            plan.units[0].files.append(importer.FileCopy("/s", os.path.join("a", "b" * 20, "f2")))
            with self.assertRaises(importer.ImportFailed) as caught:
                importer.fits(plan)
        self.assertIn("would need a path of", caught.exception.reason)
        # Asked of the directory that exists nearest the native store.
        self.assertEqual(
            measured[:2],
            [(str(self.home), "PC_PATH_MAX", 4096), (str(self.home), "PC_NAME_MAX", 255)],
        )
        # A short destination does not help when the staged path is too long.
        plan = self.plan()
        short = os.path.join(plan.native_root, "x")
        rel = os.path.join("a", "b" * 20, "f")
        staged = len(os.path.join(plan.native_root, importer._staging_name(), "0", rel))
        self.assertGreater(staged, len(os.path.join(short, rel)))
        plan.units = [dataclasses.replace(plan.units[0], dst=short)]
        with (
            mock.patch.object(importer, "_limit", lambda where, name, default: staged),
            self.assertRaises(importer.ImportFailed),
        ):
            importer.fits(plan)
        with mock.patch.object(importer, "_limit", lambda where, name, default: staged + 1):
            importer.fits(plan)

    def test_a_name_longer_than_the_filesystem_takes_is_refused(self):
        plan = self.plan()
        plan.units = [dataclasses.replace(plan.units[0], dst=os.path.join(plan.native_root, "x"))]
        unit = plan.units[0]
        with mock.patch.object(
            importer,
            "_limit",
            lambda where, name, default: 20 if name == "PC_NAME_MAX" else default,
        ):
            unit.dirs.append("n" * 20)
            importer.fits(plan)
            unit.dirs[-1] = "n" * 21
            with self.assertRaises(importer.ImportFailed) as caught:
                importer.fits(plan)
            self.assertEqual(
                caught.exception.reason,
                f"the copy of {self.side} would need a file or directory name of 21 bytes below "
                f"{self.home / '.claude'}, and at most 20 fit there; nothing was imported",
            )
            unit.dirs[-1] = "é" * 11  # 22 bytes: bytes are what is counted
            with self.assertRaises(importer.ImportFailed):
                importer.fits(plan)
            unit.dirs[-1] = os.path.join("ok", "n" * 21)  # any name on the way
            with self.assertRaises(importer.ImportFailed):
                importer.fits(plan)
            unit.dirs.pop()
            unit.dst = os.path.join(plan.native_root, "sub", "y" * 21)  # the unit's own too
            with self.assertRaises(importer.ImportFailed):
                importer.fits(plan)

    def test_where_the_platform_gives_no_limit_the_usual_ones_hold(self):
        self.assertEqual((importer.PATH_MAX, importer.NAME_MAX), (4096, 255))
        for failure in (AttributeError("no pathconf"), OSError(22, "Invalid"), ValueError("name")):
            with mock.patch.object(importer.os, "pathconf", side_effect=failure, create=True):
                self.assertEqual(importer._limit("/", "PC_PATH_MAX", 4096), 4096)
        with mock.patch.object(importer.os, "pathconf", return_value=-1, create=True):
            self.assertEqual(importer._limit("/", "PC_NAME_MAX", 255), 255)  # "no limit"
        with mock.patch.object(importer.os, "pathconf", return_value=1024, create=True):
            self.assertEqual(importer._limit("/", "PC_PATH_MAX", 4096), 1024)


class AdapterNamesTest(SafetyCase):
    """Each adapter checks the names it takes from the source before it
    builds a destination from them."""

    def reason(self, session) -> str:
        before = snapshot(self.home, self.state)
        allowed, why = api.can_import(session)
        self.assertFalse(allowed)
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(session)
        self.assertEqual(caught.exception.reason, why)
        self.assertEqual(snapshot(self.home, self.state), before)
        self.assertFalse((self.state / "imports.jsonl").exists())
        return why

    # --- Codex ----------------------------------------------------------

    def codex_session(self, *parts):
        """The fixture's thread, its rollout moved to ``sessions/<parts>``."""
        sessions = self.w.profile / ".codex" / "sessions"
        source = sessions / "2026" / "08" / "29" / CODEX_ROLLOUT
        target = sessions.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return self.w.session(CODEX_WIN, "foreign", self.w.scan())

    def test_codex_date_directories_must_be_digits(self):
        for parts in (
            ("aaaa", "08", "29", CODEX_ROLLOUT),
            ("2026", "8", "29", CODEX_ROLLOUT),
            ("2026", "08", "29x", CODEX_ROLLOUT),
            ("\uff12\uff10\uff12\uff16", "08", "29", CODEX_ROLLOUT),  # full-width digits
        ):
            with self.subTest(parts=parts):
                session = self.codex_session(*parts)
                self.assertIn("is not under sessions/YYYY/MM/DD", self.reason(session))
                os.replace(
                    self.w.profile.joinpath(".codex", "sessions", *parts),
                    self.w.profile / ".codex" / "sessions" / "2026" / "08" / "29" / CODEX_ROLLOUT,
                )

    def test_codex_rollout_name_is_matched_whole(self):
        """A name with a line break after it is not a rollout's name: the
        file is no candidate and no session, and a session that names such a
        file all the same is not imported."""
        listed = self.w.session(CODEX_WIN, "foreign", self.w.scan())
        day = self.w.profile / ".codex" / "sessions" / "2026" / "08" / "29"
        os.replace(day / CODEX_ROLLOUT, day / (CODEX_ROLLOUT + "\n"))
        index = self.w.scan()
        self.assertEqual([s.id for s in index.sessions if s.id == CODEX_WIN], [])
        self.assertEqual(index.warnings, [])
        odd = dataclasses.replace(listed, files=[listed.files[0] + "\n"])
        self.assertEqual(self.reason(odd), "this thread has no rollout file to copy")

    def test_codex_rollout_must_carry_this_threads_id(self):
        """A file named for one thread whose first line names another: the
        copy would sit in the native store under a name that belongs to a
        different thread."""
        name = f"rollout-2026-08-29T13-57-23-{OTHER}.jsonl"
        session = self.codex_session("2026", "08", "29", name)
        self.assertEqual(session.id, CODEX_WIN)  # from session_meta.payload.id
        self.assertIn("is not named after its thread id", self.reason(session))

    # --- Claude ---------------------------------------------------------

    def test_claude_transcript_name_is_matched_whole(self):
        project = self.w.profile / ".claude" / "projects" / "F--Proj-My-Site"
        plain = "69669cf3-d7ae-4ba4-a028-c6abfd2bd622"
        listed = self.w.session(plain, "foreign", self.w.scan())
        os.replace(project / f"{plain}.jsonl", project / f"{plain}.jsonl\n")
        index = self.w.scan()
        self.assertEqual([s.id for s in index.sessions if s.id == plain], [])  # no candidate
        self.assertEqual(index.warnings, [])
        # A session that names such a file all the same is not imported.
        odd = dataclasses.replace(listed, files=[listed.files[0] + "\n"])
        self.assertEqual(self.reason(odd), "this session has no transcript file to copy")

    def test_claude_transcript_must_carry_this_sessions_id(self):
        session = dataclasses.replace(self.source(CLAUDE_WIN), id=OTHER)
        self.assertIn("is not named after its session id", self.reason(session))

    # --- Grok -----------------------------------------------------------

    def grok_session(self, dir_name, info_id):
        group = self.w.profile / ".grok" / "sessions" / "F%3A%5CProj%5CMy%20Site"
        os.replace(group / GROK_WIN, group / dir_name)
        summary = group / dir_name / "summary.json"
        data = json.loads(summary.read_text(encoding="utf-8"))
        data["info"]["id"] = info_id
        summary.write_text(json.dumps(data), encoding="utf-8")
        return self.w.session(info_id, "foreign", self.w.scan())

    def test_grok_directory_must_be_named_after_the_session_id(self):
        session = self.grok_session("some-folder", GROK_WIN)
        self.assertIn("is not named after its session id", self.reason(session))

    def test_grok_directory_named_after_another_id(self):
        session = self.grok_session(OTHER, GROK_WIN)
        self.assertEqual((session.id, Path(session.files[0]).name), (GROK_WIN, OTHER))
        self.assertIn("is not named after its session id", self.reason(session))


class TreeNamesTest(unittest.TestCase):
    def test_tree_gives_only_names_below_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "side"
            write_jsonl(root / "a" / "b.jsonl", [{}])
            write_jsonl(root / "..x" / "...", [{}])
            os.symlink("/etc", root / "etc")
            dirs, files = importer.tree(root)
            self.assertEqual(dirs, ["..x", "a"])
            self.assertEqual([item.rel for item in files], ["..x/...", "a/b.jsonl"])
            for rel in dirs + [item.rel for item in files]:
                self.assertTrue(importer._relative_ok(rel), rel)


def codex_thread(root, file_id, payload_id, name_line=None) -> Path:
    """A foreign Codex thread: a rollout named for ``file_id`` whose first
    line says the thread is ``payload_id``."""
    turn = {"type": "UserMessage", "content": [{"type": "text", "text": "fix the bug"}]}
    path = write_jsonl(
        Path(root)
        / "sessions"
        / "2026"
        / "08"
        / "29"
        / f"rollout-2026-08-29T13-57-23-{file_id}.jsonl",
        [
            {
                "timestamp": "2026-08-29T08:31:14.474Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"id": payload_id, "cwd": "F:\\Proj\\My Site", "source": "cli"},
            },
            {
                "timestamp": "2026-08-29T08:31:15.000Z",
                "ordinal": 1,
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": turn},
            },
        ],
    )
    if name_line is not None:
        write_jsonl(Path(root) / "session_index.jsonl", [name_line])
    return path


class VerifyAndRollBackTest(unittest.TestCase):
    """docs/REVIEW.md R-04: an import whose copy does not read back as the
    planned session leaves nothing behind. The native store already holds
    sessions and the state directory an import record (in the form from
    before the journal), so "unchanged" means something."""

    FILE_ID = "01a04ca1-6e27-7817-9f54-07105f962073"

    def setUp(self):
        self.w = World(self, foreign=(), native=("codex",))
        self.home = self.w.home
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])
        self.state.mkdir(exist_ok=True)
        record = {"version": 1, "imports": [{"id": "earlier", "provider": "codex"}]}
        (self.state / "imports.json").write_text(json.dumps(record), encoding="utf-8")
        self.foreign = self.w.profile / ".codex"
        self.native = self.home / ".codex"
        self.native_index = (self.native / "session_index.jsonl").read_bytes()

    def unchanged(self, before):
        self.assertEqual(snapshot(self.home, self.state), before)
        self.assertEqual(state_names(), ["imports.json"])
        self.assertEqual((self.native / "session_index.jsonl").read_bytes(), self.native_index)
        self.assertEqual(
            [n for n in os.listdir(self.native) if n.startswith(importer.STAGING_PREFIX)], []
        )

    def good_plan(self) -> importer.ImportPlan:
        name = {"id": self.FILE_ID, "thread_name": "a name", "updated_at": "2026-08-29T08:31:16Z"}
        codex_thread(self.foreign, self.FILE_ID, self.FILE_ID, name)
        index = self.w.scan()
        plan = api._plan(self.w.session(self.FILE_ID, "foreign", index), index)
        self.assertEqual(len(plan.appends), 1)
        return plan

    def test_the_reviews_scenario_leaves_the_native_store_and_the_state_untouched(self):
        """The rollout is named for one thread and its first line names
        another."""
        codex_thread(self.foreign, self.FILE_ID, OTHER)
        index = self.w.scan()
        session = self.w.session(OTHER, "foreign", index)  # listed by the id inside
        before = snapshot(self.home, self.state)
        with self.assertRaises(api.ImportFailed):
            api.import_session(session)
        self.unchanged(before)
        code_listing = [s.id for s in self.w.scan().sessions if s.origin == "native"]
        self.assertNotIn(OTHER, code_listing)
        self.assertNotIn(self.FILE_ID, code_listing)

    def test_a_copy_that_reads_back_as_another_session_is_taken_out_again(self):
        """The same, past the adapter's own check of the file name: the
        plan says one id, the copy parses as another. The candidate the copy
        becomes is *named* after the planned id, which is what the old check
        looked at."""
        plan = self.good_plan()
        codex_thread(self.foreign, self.FILE_ID, OTHER)  # same file name, another id inside
        before = snapshot(self.home, self.state)
        with (
            mock.patch.object(Codex, "import_plan", return_value=plan),
            self.assertRaises(importer.ImportFailed) as caught,
        ):
            Codex().import_session(self.w.session(self.FILE_ID, "foreign"), None)
        self.assertEqual(
            caught.exception.reason,
            f"the copy does not read as session {self.FILE_ID}; it was removed again and "
            "nothing was left in the native store",
        )
        self.unchanged(before)

    def test_the_order_is_place_verify_append_record(self):
        plan = self.good_plan()
        seen = {}

        def verify():
            seen["placed"] = [os.path.isfile(dst) for dst in plan.destinations]
            seen["index"] = (self.native / "session_index.jsonl").read_bytes()
            seen["records"] = len(importer.read_journal())

        old = (self.state / "imports.json").read_bytes()
        importer.execute(plan, verify=verify)
        self.assertEqual(seen, {"placed": [True], "index": self.native_index, "records": 1})
        records = importer.read_journal()
        self.assertEqual([r["id"] for r in records], ["earlier", self.FILE_ID])
        self.assertEqual([r["version"] for r in records], [1, 2])
        # The record from before the journal is read and never written.
        self.assertEqual((self.state / "imports.json").read_bytes(), old)
        self.assertEqual(state_names(), ["imports.json", "imports.jsonl"])
        added = (self.native / "session_index.jsonl").read_bytes()[len(self.native_index) :]
        self.assertEqual(json.loads(added)["thread_name"], "a name")

    def test_a_failed_verification_rolls_everything_back(self):
        plan = self.good_plan()
        before = snapshot(self.home, self.state)
        for failure, reason in (
            (importer.ImportFailed("not the session"), "not the session"),
            (
                ValueError("boom"),
                "the copy could not be read back (ValueError: boom); it was removed again "
                "and nothing was left in the native store",
            ),
            (
                OSError(5, "Input/output error"),
                "the copy could not be read back (OSError: [Errno 5] Input/output error); "
                "it was removed again and nothing was left in the native store",
            ),
        ):
            with self.assertRaises(importer.ImportFailed) as caught:
                importer.execute(plan, verify=mock.Mock(side_effect=failure))
            self.assertEqual(caught.exception.reason, reason)
            self.unchanged(before)

    def test_directories_made_for_the_import_are_removed_too(self):
        """Nothing native and no state directory yet: none is left."""
        shutil.rmtree(self.native)
        plan = self.good_plan()
        drop_state()
        self.native_index = b""
        with self.assertRaises(importer.ImportFailed):
            importer.execute(plan, verify=mock.Mock(side_effect=importer.ImportFailed("no")))
        self.assertFalse(self.native.exists())
        self.assertFalse(self.state.exists())
        # And the import that verifies makes them and leaves them.
        native = Codex().import_session(
            self.w.session(self.FILE_ID, "foreign"), api.load_index().pathmap
        )
        self.assertEqual((native.id, native.title), (self.FILE_ID, "a name"))
        self.assertTrue((self.state / "imports.jsonl").is_file())

    def test_read_back(self):
        plan = self.good_plan()
        with self.assertRaises(importer.ImportFailed):
            Codex().read_back(plan)  # nothing is in place yet
        importer.execute(plan)
        copy = Codex().read_back(plan)
        self.assertEqual((copy.id, copy.files), (self.FILE_ID, plan.destinations))
        for wrong in (
            dataclasses.replace(plan, session_id=OTHER),
            dataclasses.replace(plan, units=[*plan.units, importer.Unit(plan.units[0].dst + ".x")]),
        ):
            with self.assertRaises(importer.ImportFailed):
                Codex().read_back(wrong)


if __name__ == "__main__":
    unittest.main()
