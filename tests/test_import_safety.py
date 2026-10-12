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
from support import write_jsonl

from agent_sessions import api, importer
from agent_sessions.providers.claude import encode_cwd
from agent_sessions.providers.codex import Codex

OTHER = "0f0f0f0f-1111-4222-8333-444444444444"
CODEX_ROLLOUT = f"rollout-2026-08-29T13-57-23-{CODEX_WIN}.jsonl"


def snapshot(*roots) -> dict:
    """Every file and directory below the roots: a regular file with its
    bytes, anything else (a directory, a link, a named pipe) with its kind."""
    found: dict[str, bytes | int] = {}
    for root in roots:
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
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

    def test_a_link_out_of_the_native_root_is_not_followed(self):
        outside = self.w.box.root / "elsewhere"
        outside.mkdir()
        os.makedirs(self.root)
        os.symlink(outside, os.path.join(self.root, "projects"))
        reason = self.refused(self.good)
        self.assertIn("leads out of the native store", reason)
        self.assertIn("through a symbolic link", reason)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(api.can_import(self.source(CLAUDE_WIN)), (False, reason))
        # The same with the link one level down, at the project directory.
        os.unlink(os.path.join(self.root, "projects"))
        os.makedirs(os.path.join(self.root, "projects"))
        os.symlink(outside, os.path.dirname(self.good.units[1].dst))
        self.assertIn("leads out of the native store", self.refused(self.good))
        self.assertEqual(os.listdir(outside), [])

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
        # A link out of it is still refused.
        plan = self.plan("69669cf3-d7ae-4ba4-a028-c6abfd2bd622")
        shutil.rmtree(dotfiles / "projects")
        os.symlink(self.w.box.root, dotfiles / "projects")
        with self.assertRaises(importer.ImportFailed) as caught:
            importer.check(plan)
        self.assertIn("leads out of the native store", caught.exception.reason)


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
        self.assertFalse((self.state / "imports.json").exists())
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
        session = self.codex_session("2026", "08", "29", CODEX_ROLLOUT + "\n")
        self.assertEqual(session.files[0][-1], "\n")
        self.assertIn("is not named after its thread id", self.reason(session))

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
        os.replace(project / f"{plain}.jsonl", project / f"{plain}.jsonl\n")
        session = self.w.session(plain, "foreign", self.w.scan())
        self.assertIn("is not named after its session id", self.reason(session))

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
    sessions and an import record, so "unchanged" means something."""

    FILE_ID = "01a04ca1-6e27-7817-9f54-07105f962073"

    def setUp(self):
        self.w = World(self, foreign=(), native=("codex",))
        self.home = self.w.home
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])
        self.state.mkdir()
        record = {"version": 1, "imports": [{"id": "earlier", "provider": "codex"}]}
        (self.state / "imports.json").write_text(json.dumps(record), encoding="utf-8")
        self.foreign = self.w.profile / ".codex"
        self.native = self.home / ".codex"
        self.native_index = (self.native / "session_index.jsonl").read_bytes()

    def unchanged(self, before):
        self.assertEqual(snapshot(self.home, self.state), before)
        self.assertEqual(sorted(os.listdir(self.state)), ["imports.json"])
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
            seen["records"] = len(importer.read_provenance())

        importer.execute(plan, verify=verify)
        self.assertEqual(seen, {"placed": [True], "index": self.native_index, "records": 1})
        self.assertEqual(len(importer.read_provenance()), 2)
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
        shutil.rmtree(self.state)
        plan = self.good_plan()
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
        self.assertTrue((self.state / "imports.json").is_file())

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
