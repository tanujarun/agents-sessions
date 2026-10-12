"""Importing into a folder the user chooses (``target``, ``import --to``):
docs/SPEC.md "Import", docs/FORMATS.md "Phase 2 findings" Q8.

The folder is the user's own input and is checked (``importer.chosen_folder``);
what it changes in the copy differs per provider. The machine is the one of
tests/fixture_world.py."""

import contextlib
import io
import json
import os
import shutil
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    CLAUDE_WIN,
    CLAUDE_WIN_PLAIN,
    CODEX_SEGMENTED,
    CODEX_WIN,
    GROK_WIN,
    World,
    files_below,
    tree_state,
)
from support import FIXTURES, state_names

from agent_sessions import api, blake3, cli, importer, journal
from agent_sessions.providers import grok as grok_mod
from agent_sessions.providers.claude import encode_cwd
from agent_sessions.providers.codex import _CwdRewrite

CODEX_ROLLOUT = f"rollout-2026-08-29T13-57-23-{CODEX_WIN}.jsonl"
SEGMENT_BASE = f"rollout-2026-09-28T05-04-58-{CODEX_SEGMENTED}.jsonl"
SEGMENT_LAST = (
    f"rollout-2026-09-28T06-22-25-{CODEX_SEGMENTED}_01a0e57f-b05d-7b55-a44a-dbc2d903cdef.jsonl"
)
RECORDED = "F:\\Proj\\My Site"  # what the Windows fixtures recorded
NOT_FOUND = f"the project folder {RECORDED} was not found on any mounted drive"
NOT_RECORDED = "the project folder of this session is not recorded"


def read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class StubMap:
    """A path map that knows exactly the paths it is given."""

    def __init__(self, table):
        self.table = table

    def to_local(self, recorded, mount=None):
        local = self.table.get(recorded)
        return (local, True) if local else (None, False)


class FolderCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("claude", "codex", "grok")

    def setUp(self):
        self.w = World(self, self.foreign)
        self.home = self.w.home
        self.index = self.w.scan()
        # A folder that has nothing to do with the one the sessions recorded.
        self.chosen = self.home / "Work" / "new place"
        self.chosen.mkdir(parents=True)
        self.record_file = Path(os.environ["AGENT_SESSIONS_STATE"]) / "imports.jsonl"

    def source(self, session_id):
        return self.w.session(session_id, "foreign")

    def lose_the_folder(self):
        """The folder the fixtures recorded is gone (its parent stays)."""
        shutil.rmtree(self.w.site)
        self.index = self.w.scan()

    def refused(self, ref, **kwargs) -> str:
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(ref, **kwargs)
        return caught.exception.reason

    def assert_nothing_written(self):
        for name in (".claude", ".codex", ".grok", ".gemini"):
            self.assertFalse((self.home / name).exists(), name)
        self.assertEqual(state_names(), [])


# --- the folder itself --------------------------------------------------------


class ChosenFolderTest(FolderCase):
    foreign = ("claude",)

    def bad_targets(self):
        afile = self.home / "a file"
        afile.write_text("x", encoding="utf-8")
        return (
            ("Work/new place", "must be an absolute path: Work/new place"),
            ("./Work", "must be an absolute path: ./Work"),
            ("~/Work", "must be an absolute path: ~/Work"),  # ~ is the caller's to expand
            ("~", "must be an absolute path: ~"),
            (str(self.home / "nowhere"), f"does not exist: {self.home / 'nowhere'}"),
            (str(self.chosen / "deeper" / "still"), "does not exist: "),
            (str(afile), f"is not a directory: {afile}"),
            (str(afile / "below"), "does not exist: "),
            (str(self.chosen) + "\0", "has a NUL character in its path"),
            ("/\0", "has a NUL character in its path"),
            ("", "is not a path"),
        )

    def test_an_absolute_path_of_a_directory_that_exists(self):
        self.assertEqual(importer.chosen_folder(str(self.chosen)), str(self.chosen))
        # One spelling: no trailing or doubled separator, no "." segment.
        for spelling in (
            f"{self.chosen}/",
            f"{self.chosen}//",
            f"{self.chosen}/.",
            f"{self.home}/Work//new place",
            f"{self.home}/Work/./new place/",
        ):
            self.assertEqual(importer.chosen_folder(spelling), str(self.chosen), spelling)
        self.assertEqual(importer.chosen_folder("/"), "/")
        # A link to a directory is a directory; it keeps its own name.
        link = self.home / "link"
        os.symlink(self.chosen, link)
        self.assertEqual(importer.chosen_folder(str(link)), str(link))

    def test_everything_else_is_refused(self):
        for target, needle in self.bad_targets():
            with self.subTest(target=target):
                with self.assertRaises(importer.ImportFailed) as caught:
                    importer.chosen_folder(target)
                self.assertIn("the folder to import into ", caught.exception.reason)
                self.assertIn(needle, caught.exception.reason)
        for target in (None, 7, b"/tmp", Path("/"), ["/"]):
            with self.assertRaises(importer.ImportFailed) as caught:
                importer.chosen_folder(target)
            self.assertEqual(caught.exception.reason, "the folder to import into is not a path")
        dangling = self.home / "dangling"
        os.symlink(self.home / "gone", dangling)
        with self.assertRaises(importer.ImportFailed) as caught:
            importer.chosen_folder(str(dangling))
        self.assertIn("is not a directory", caught.exception.reason)

    def test_a_bad_folder_is_refused_by_every_entry_point_and_nothing_is_written(self):
        source = self.source(CLAUDE_WIN)
        for target, needle in self.bad_targets():
            with self.subTest(target=target):
                reason = self.refused(source, target=target)
                self.assertIn(needle, reason)
                self.assertEqual(self.refused(source.key, target=target), reason)
                self.assertEqual(api.can_import(source, target=target), (False, reason))
                for call in (api.import_plan, api.import_report):
                    with self.assertRaises(api.ImportFailed) as caught:
                        call(source, target=target)
                    self.assertEqual(caught.exception.reason, reason)
                with self.assertRaises(api.ImportFailed) as caught:
                    importer.start_plan(source, "/native", self.index.pathmap, target=target)
                self.assertEqual(caught.exception.reason, reason)
        self.assert_nothing_written()

    def test_a_bad_folder_is_refused_for_an_imported_session_too(self):
        api.import_session(self.source(CLAUDE_WIN))
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_plan(CLAUDE_WIN, target="relative")
        self.assertIn("must be an absolute path", caught.exception.reason)
        # A good one: the copy that is there is the answer, whatever the folder.
        report = api.import_report(CLAUDE_WIN, target=str(self.chosen))
        self.assertFalse(report.created)
        self.assertEqual(
            api.import_plan(CLAUDE_WIN, target=str(self.chosen))["already"], report.session.key
        )
        self.assertEqual(len(journal.read()), 1)

    def test_a_native_session_is_not_imported_into_a_folder_either(self):
        shutil.copytree(FIXTURES / "claude" / "native" / ".claude", self.home / ".claude")
        self.w.scan()
        native = next(s for s in api.load_index().sessions if s.origin == "native")
        self.assertEqual(
            self.refused(native, target=str(self.chosen)), "this session is already on Linux"
        )
        self.assertFalse(self.record_file.exists())


# --- Claude -------------------------------------------------------------------


class ClaudeFolderTest(FolderCase):
    foreign = ("claude",)

    def setUp(self):
        super().setUp()
        self.project = self.w.profile / ".claude" / "projects" / "F--Proj-My-Site"
        self.target = self.home / ".claude" / "projects" / encode_cwd(str(self.chosen))

    def assert_copied_into(self, target: Path, session_id=CLAUDE_WIN):
        """Everything of the session, byte for byte, in ``target`` and
        nowhere else in the store."""
        copied = files_below(target)
        self.assertEqual(
            files_below(self.home / ".claude"), [f"projects/{target.name}/{rel}" for rel in copied]
        )
        expected = [rel for rel in files_below(self.project) if rel.startswith(session_id)]
        self.assertEqual(copied, expected)
        for rel in copied:
            self.assertEqual(read(target / rel), read(self.project / rel), rel)

    def test_a_session_whose_folder_is_gone_imports_into_the_chosen_one(self):
        self.lose_the_folder()
        source = self.source(CLAUDE_WIN)
        self.assertEqual((source.cwd_local, source.cwd_exists), (None, False))
        # Without a folder: refused, as before, and with the same words.
        self.assertEqual(self.refused(source), NOT_FOUND)
        self.assertEqual(api.can_import(source), (False, NOT_FOUND))
        self.assertEqual(api.can_import(source, target=str(self.chosen)), (True, ""))
        before = tree_state(self.w.c)

        native = api.import_session(source, target=str(self.chosen))

        self.assertEqual(tree_state(self.w.c), before, "the source is untouched")
        self.assertEqual(self.target.name, encode_cwd(str(self.chosen)))
        self.assertRegex(self.target.name, r"-Work-new-place$")
        self.assert_copied_into(self.target)
        self.assertGreater(len(files_below(self.target)), 1, "the side directory came along")
        self.assertEqual(
            (native.origin, native.id, native.launchable), ("native", CLAUDE_WIN, True)
        )
        self.assertEqual(
            native.files, [str(self.target / f"{CLAUDE_WIN}.jsonl"), str(self.target / CLAUDE_WIN)]
        )
        # Nothing inside the transcript changed: it still records its own folder.
        self.assertEqual(native.cwd, RECORDED)
        self.assertEqual(self.source(CLAUDE_WIN).imported_as, native.key)

    def test_the_tools_own_marker_for_a_moved_session_is_not_written(self):
        self.lose_the_folder()
        api.import_session(self.source(CLAUDE_WIN), target=str(self.chosen))
        transcript = read(self.target / f"{CLAUDE_WIN}.jsonl")
        self.assertEqual(transcript, read(self.project / f"{CLAUDE_WIN}.jsonl"))
        self.assertNotIn(b"relocated", transcript)
        for line in transcript.splitlines():
            self.assertNotEqual(json.loads(line).get("type"), "relocated")

    def test_the_record_keeps_the_recorded_folder_and_the_chosen_one(self):
        self.lose_the_folder()
        source = self.source(CLAUDE_WIN)
        plan = api.import_plan(source, target=f"{self.chosen}/")
        self.assertEqual(
            (plan["cwd"], plan["cwd_local"], plan["chosen_folder"], plan["already"]),
            (RECORDED, str(self.chosen), str(self.chosen), None),
        )
        self.assertEqual(
            {c["to"] for c in plan["copies"]},
            {
                str(self.target / CLAUDE_WIN),
                str(self.target / f"{CLAUDE_WIN}.jsonl"),
            },
        )
        self.assertEqual(plan["rewrites"], [])
        self.assert_nothing_written()
        api.import_session(source, target=f"{self.chosen}/")
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd"], record["cwd_local"], record["chosen_folder"]),
            (RECORDED, str(self.chosen), str(self.chosen)),
        )
        self.assertEqual(record["rewritten"], {})
        self.assertEqual(
            record["destination_files"],
            [str(self.target / CLAUDE_WIN), str(self.target / f"{CLAUDE_WIN}.jsonl")],
        )

    def test_a_chosen_folder_wins_over_a_folder_that_was_found(self):
        source = self.source(CLAUDE_WIN_PLAIN)
        self.assertEqual(source.cwd_local, str(self.w.site))
        api.import_session(source, target=str(self.chosen))
        self.assert_copied_into(self.target, CLAUDE_WIN_PLAIN)
        mapped = self.home / ".claude" / "projects" / encode_cwd(str(self.w.site))
        self.assertFalse(mapped.exists())
        (record,) = journal.read()
        self.assertEqual(record["chosen_folder"], str(self.chosen))

    def test_a_session_without_a_recorded_folder(self):
        source = self.source(CLAUDE_WIN_PLAIN)
        source.cwd = None
        self.assertEqual(self.refused(source), NOT_RECORDED)
        with self.assertRaises(importer.NoFolder):
            importer.start_plan(source, "/native", self.index.pathmap)
        api.import_session(source, target=str(self.chosen))
        self.assert_copied_into(self.target, CLAUDE_WIN_PLAIN)
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd"], record["cwd_local"], record["chosen_folder"]),
            (None, str(self.chosen), str(self.chosen)),
        )

    def test_the_directory_name_follows_the_tools_rule_for_any_folder(self):
        long_folder = self.home / ("long name " * 22).strip()  # its encoding is over 200
        long_folder.mkdir()
        plan = importer.start_plan(
            self.source(CLAUDE_WIN_PLAIN),
            self.home / ".claude",
            self.index.pathmap,
            target=str(long_folder),
        )
        self.assertEqual((plan.cwd_local, plan.chosen_folder), (str(long_folder),) * 2)
        api.import_session(self.source(CLAUDE_WIN_PLAIN), target=str(long_folder))
        (name,) = os.listdir(self.home / ".claude" / "projects")
        self.assertEqual(name, encode_cwd(str(long_folder)))
        self.assertRegex(name, r"^-.{199}-[0-9a-z]+$")  # cut to 200, then the hash


# --- Codex --------------------------------------------------------------------


class CodexFolderTest(FolderCase):
    foreign = ("codex",)

    def setUp(self):
        super().setUp()
        self.day = Path("sessions") / "2026" / "08" / "29"
        self.original = self.w.profile / ".codex" / self.day / CODEX_ROLLOUT
        self.copy = self.home / ".codex" / self.day / CODEX_ROLLOUT

    def changed_values(self, source, copy) -> list[tuple]:
        """``(line, record type, key, old, new)`` for every value that
        differs; every other byte of the two files must be the same, and a
        changed line must be in the tool's own form."""
        old_lines, new_lines = read(source).split(b"\n"), read(copy).split(b"\n")
        self.assertEqual(len(old_lines), len(new_lines))
        allowed = {
            "session_meta": {"cwd", "runtime_workspace_roots"},
            "turn_context": {"cwd", "workspace_roots"},
        }
        changed = []
        for number, (old, new) in enumerate(zip(old_lines, new_lines, strict=True)):
            if old == new:
                continue
            before, after = json.loads(old), json.loads(new)
            kind = before["type"]
            self.assertIn(kind, allowed, f"line {number} changed")
            self.assertEqual(list(before), list(after))
            self.assertEqual(list(before["payload"]), list(after["payload"]))
            for key in before:
                if key != "payload":
                    self.assertEqual(before[key], after[key])
            for key in before["payload"]:
                if before["payload"][key] != after["payload"][key]:
                    self.assertIn(key, allowed[kind], f"line {number}: {key}")
                    changed.append(
                        (number, kind, key, before["payload"][key], after["payload"][key])
                    )
            self.assertEqual(
                new, json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        return changed

    def test_the_recorded_folder_becomes_the_chosen_one_and_nothing_else_changes(self):
        self.lose_the_folder()
        source = self.source(CODEX_WIN)
        self.assertEqual(self.refused(source), NOT_FOUND)
        chosen = str(self.chosen)
        plan = api.import_plan(source, target=chosen)
        self.assertEqual(
            (plan["cwd"], plan["cwd_local"], plan["chosen_folder"]), (RECORDED, chosen, chosen)
        )
        self.assertEqual(
            plan["rewrites"][0]["changes"],
            [
                f"session_meta payload.cwd: {RECORDED} -> {chosen}",
                f"turn_context payload.cwd: {RECORDED} -> {chosen}",
                f"turn_context payload.workspace_roots[]: {RECORDED} -> {chosen}",
            ],
        )
        self.assert_nothing_written()

        native = api.import_session(source, target=chosen)

        # The destination does not depend on the folder.
        self.assertEqual(files_below(self.home / ".codex"), [str(self.day / CODEX_ROLLOUT)])
        self.assertEqual(
            self.changed_values(self.original, self.copy),
            [
                (0, "session_meta", "cwd", RECORDED, chosen),
                (4, "turn_context", "cwd", RECORDED, chosen),
                (4, "turn_context", "workspace_roots", [RECORDED], [chosen]),
            ],
        )
        self.assertEqual((native.cwd, native.cwd_local, native.launchable), (chosen, chosen, True))
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd"], record["cwd_local"], record["chosen_folder"]),
            (RECORDED, chosen, chosen),
        )
        self.assertEqual(list(record["rewritten"]), [str(self.copy)])

    def test_a_chosen_folder_wins_over_the_mapped_one(self):
        chosen = str(self.chosen)
        native = api.import_session(self.source(CODEX_WIN), target=chosen)
        self.assertEqual(native.cwd, chosen)
        self.assertNotIn(str(self.w.site).encode(), read(self.copy))
        self.assertEqual(
            [new for *_rest, new in self.changed_values(self.original, self.copy)],
            [chosen, chosen, [chosen]],
        )

    def test_a_thread_with_segments_keeps_its_base_byte_for_byte(self):
        old = self.w.box.mount("OldLinux")
        old_home = old / "home" / "user"
        shutil.copytree(FIXTURES / "codex" / "native" / ".codex", old_home / ".codex")
        self.w.scan()  # /home/user of that drive is not on this machine
        source = self.source(CODEX_SEGMENTED)
        self.assertEqual((source.cwd, source.cwd_local), ("/home/user", None))
        chosen = str(self.chosen)
        api.import_session(source, target=chosen)
        day = Path("sessions") / "2026" / "09" / "28"
        src, dst = old_home / ".codex" / day, self.home / ".codex" / day
        self.assertEqual(read(dst / SEGMENT_BASE), read(src / SEGMENT_BASE))
        self.assertEqual(
            self.changed_values(src / SEGMENT_LAST, dst / SEGMENT_LAST),
            [
                (0, "session_meta", "cwd", "/home/user", chosen),
                (0, "session_meta", "runtime_workspace_roots", ["/home/user"], [chosen]),
                (4, "turn_context", "cwd", "/home/user", chosen),
                (4, "turn_context", "workspace_roots", ["/home/user"], [chosen]),
            ],
        )


class CodexRepointTest(unittest.TestCase):
    """``_CwdRewrite`` with a chosen folder: FORMATS Q8, "Codex"."""

    TARGET = "/home/u/new place"

    def rewrite(self, data: bytes, recorded="F:\\Proj\\My Site", target=TARGET, table=None):
        stub = StubMap(table or {"D:\\other": "/mnt/other"})
        # A stand-in with to_local only, and no mount: all the rewrite uses.
        rewrite = _CwdRewrite(stub, None, recorded, target)  # type: ignore[arg-type]
        out = b"".join(rewrite.apply(io.BytesIO(data)))
        return out, rewrite.changes

    @staticmethod
    def line(kind, payload, ending=b"\n") -> bytes:
        record = {"timestamp": "2026-08-29T08:27:23.048Z", "type": kind, "payload": payload}
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + ending

    def mapped(self, value, **kwargs):
        """What one recorded value becomes."""
        out, _changes = self.rewrite(self.line("turn_context", {"cwd": value}), **kwargs)
        return json.loads(out)["payload"]["cwd"]

    def test_every_spelling_of_the_recorded_folder_becomes_the_chosen_one(self):
        for value in (
            "F:\\Proj\\My Site",
            "F:/Proj/My Site",
            "f:\\proj\\my site",  # any case
            "F:\\PROJ/MY SITE",
            "F:\\Proj\\My Site\\",
            "F:\\\\Proj\\.\\My Site",
            "\\\\?\\F:\\Proj\\My Site",  # the extended-length prefix
            "//?/f:/proj/my site",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.mapped(value), self.TARGET)

    def test_a_folder_below_it_keeps_its_place_below_the_chosen_one(self):
        for value, expected in (
            ("F:\\Proj\\My Site\\src", f"{self.TARGET}/src"),
            ("f:/proj/my site/Src/Deep Dir", f"{self.TARGET}/Src/Deep Dir"),  # its own spelling
            ("\\\\?\\F:\\Proj\\My Site\\a\\b\\", f"{self.TARGET}/a/b"),
            ("F:\\Proj\\My Site\\caf\u00e9 \U0001f600", f"{self.TARGET}/caf\u00e9 \U0001f600"),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.mapped(value), expected)
        # No check that it exists: the chosen folder need not hold it yet.
        self.assertFalse(os.path.exists(f"{self.TARGET}/src"))

    def test_any_other_value_goes_through_the_path_map_as_before(self):
        for value, expected in (
            ("D:\\other", "/mnt/other"),  # mapped
            ("E:\\gone", "E:\\gone"),  # not mapped: left as recorded
            ("F:\\Proj", "F:\\Proj"),  # above the recorded folder
            ("F:\\Proj\\My Site 2", "F:\\Proj\\My Site 2"),  # a sibling with the same start
            ("F:\\Proj\\My", "F:\\Proj\\My"),
            ("G:\\Proj\\My Site", "G:\\Proj\\My Site"),  # another drive
            ("/Proj/My Site", "/Proj/My Site"),  # another root
            ("F:\\Proj\\My Site\\..\\..\\etc", "F:\\Proj\\My Site\\..\\..\\etc"),  # not below it
            ("Proj\\My Site", "Proj\\My Site"),  # relative
            ("\\\\server\\share\\My Site", "\\\\server\\share\\My Site"),
            ("", ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(self.mapped(value), expected)
        for value in (None, 7, ["F:\\Proj\\My Site"], {"cwd": "F:\\Proj\\My Site"}):
            out, changes = self.rewrite(self.line("turn_context", {"cwd": value}))
            if isinstance(value, list):  # a list is a list of roots: its values are mapped
                self.assertEqual(json.loads(out)["payload"]["cwd"], [self.TARGET])
            else:
                self.assertEqual(json.loads(out)["payload"]["cwd"], value)
                self.assertEqual(changes, [])

    def test_a_recorded_folder_of_this_kind_of_machine(self):
        self.assertEqual(self.mapped("/home/user", recorded="/home/user"), self.TARGET)
        self.assertEqual(self.mapped("/home/user/x/y", recorded="/home/user"), f"{self.TARGET}/x/y")
        self.assertEqual(self.mapped("/home/user2", recorded="/home/user"), "/home/user2")
        self.assertEqual(self.mapped("/home", recorded="/home/user"), "/home")

    def test_without_a_recorded_folder_nothing_is_re_pointed(self):
        for recorded in (None, "", "relative\\path", "F:\\a\\..\\b"):
            with self.subTest(recorded=recorded):
                self.assertEqual(
                    self.mapped("F:\\Proj\\My Site", recorded=recorded), "F:\\Proj\\My Site"
                )
                self.assertEqual(self.mapped("D:\\other", recorded=recorded), "/mnt/other")

    def test_without_a_chosen_folder_the_recorded_one_plays_no_part(self):
        self.assertEqual(self.mapped("F:\\Proj\\My Site", target=None), "F:\\Proj\\My Site")
        self.assertEqual(
            self.mapped("F:\\Proj\\My Site", target=None, table={"F:\\Proj\\My Site": "/mnt/s"}),
            "/mnt/s",
        )

    def test_only_the_four_fields_change_and_every_other_byte_stays(self):
        untouched = [
            self.line("response_item", {"cwd": "F:\\Proj\\My Site", "text": '"turn_context"'}),
            self.line(
                "event_msg",
                {
                    "type": "thread_settings_applied",
                    "thread_settings": {"cwd": "F:\\Proj\\My Site"},
                },
            ),
            self.line("turn_context", {"cwd": "E:\\elsewhere", "workspace_roots": []}),
            b'not json "session_meta" F:\\Proj\\My Site\n',
            b'{ "type": "turn_context", "payload": { "cwd": "E:\\\\spaced" } }\n',
        ]
        meta = self.line(
            "session_meta",
            {
                "id": "t",
                "cwd": "F:\\Proj\\My Site",
                "note": "caf\u00e9 \U0001f600 \u2028",
                "runtime_workspace_roots": [
                    "F:/Proj/My Site",
                    "F:\\Proj\\My Site\\lib",
                    7,
                    "D:\\other",
                ],
                "nested": {"cwd": "F:\\Proj\\My Site"},
                "instructions": "cd F:\\Proj\\My Site",
            },
        )
        turn = self.line(
            "turn_context",
            {
                "cwd": "f:\\proj\\my site\\sub",
                "workspace_roots": ["F:\\Proj\\My Site"],
                "model": "m",
            },
            ending=b"\r\n",
        )
        out, changes = self.rewrite(meta + b"".join(untouched) + turn)
        lines = out.split(b"\n")
        self.assertEqual(b"\n".join(lines[1:6]) + b"\n", b"".join(untouched))
        self.assertEqual(
            json.loads(lines[0])["payload"],
            {
                "id": "t",
                "cwd": self.TARGET,
                "note": "caf\u00e9 \U0001f600 \u2028",
                "runtime_workspace_roots": [self.TARGET, f"{self.TARGET}/lib", 7, "/mnt/other"],
                "nested": {"cwd": "F:\\Proj\\My Site"},
                "instructions": "cd F:\\Proj\\My Site",
            },
        )
        self.assertIn("caf\u00e9 \U0001f600".encode(), lines[0])  # not \u-escaped
        self.assertTrue(lines[6].endswith(b"\r"))
        self.assertEqual(
            json.loads(lines[6])["payload"],
            {"cwd": f"{self.TARGET}/sub", "workspace_roots": [self.TARGET], "model": "m"},
        )
        self.assertEqual(
            changes,
            [
                f"session_meta payload.cwd: F:\\Proj\\My Site -> {self.TARGET}",
                "session_meta payload.runtime_workspace_roots[]: D:\\other -> /mnt/other",
                f"session_meta payload.runtime_workspace_roots[]: F:/Proj/My Site -> {self.TARGET}",
                "session_meta payload.runtime_workspace_roots[]: F:\\Proj\\My Site\\lib -> "
                f"{self.TARGET}/lib",
                f"turn_context payload.cwd: f:\\proj\\my site\\sub -> {self.TARGET}/sub",
                f"turn_context payload.workspace_roots[]: F:\\Proj\\My Site -> {self.TARGET}",
            ],
        )

    def test_nothing_to_re_point_means_byte_identical(self):
        data = self.line("session_meta", {"cwd": "E:\\x"}) + self.line(
            "turn_context", {"cwd": "E:\\x"}
        )
        self.assertEqual(self.rewrite(data), (data, []))


# --- Grok ---------------------------------------------------------------------


class GrokFolderTest(FolderCase):
    foreign = ("grok",)

    def setUp(self):
        super().setUp()
        self.original = self.w.profile / ".grok" / "sessions" / "F%3A%5CProj%5CMy%20Site" / GROK_WIN
        self.sessions = self.home / ".grok" / "sessions"

    def assert_imported_for(self, folder: str, group: str):
        """The session lies in ``group``, and its summary names ``folder``;
        every other file is the source's, byte for byte."""
        self.assertEqual(os.listdir(self.sessions), [group])
        copy = self.sessions / group / GROK_WIN
        expected = [rel for rel in files_below(self.original) if not rel.endswith(".lock")]
        self.assertEqual(files_below(copy), expected)
        for rel in expected:
            if rel != "summary.json":
                self.assertEqual(read(copy / rel), read(self.original / rel), rel)
        before = json.loads(read(self.original / "summary.json"))
        after = json.loads(read(copy / "summary.json"))
        self.assertEqual(list(before), list(after))
        self.assertEqual(after["info"], {"id": GROK_WIN, "cwd": folder})
        self.assertEqual(after["grok_home"], str(self.home / ".grok"))
        for key in before:
            if key not in ("info", "grok_home"):
                self.assertEqual(after[key], before[key], key)

    def test_group_name_and_summary_are_made_for_the_chosen_folder(self):
        self.lose_the_folder()
        source = self.source(GROK_WIN)
        self.assertEqual(self.refused(source), NOT_FOUND)
        chosen = str(self.chosen)
        native = api.import_session(source, target=chosen)
        group = urllib.parse.quote(chosen, safe="")
        self.assertTrue(group.endswith("%2FWork%2Fnew%20place"), group)
        self.assert_imported_for(chosen, group)
        self.assertEqual((native.cwd, native.cwd_local, native.launchable), (chosen, chosen, True))
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd"], record["cwd_local"], record["chosen_folder"]),
            (RECORDED, chosen, chosen),
        )

    def test_any_folder_name_is_named_the_way_the_tool_names_it(self):
        folder = self.home / "my-proj_v1.2~x y" / "\u00dcn\u00ef \u6f22"
        folder.mkdir(parents=True)
        api.import_session(self.source(GROK_WIN), target=str(folder))
        group = grok_mod.group_name(str(folder))
        self.assertTrue(group.endswith("%2Fmy-proj_v1.2~x%20y%2F%C3%9Cn%C3%AF%20%E6%BC%A2"), group)
        self.assert_imported_for(str(folder), group)

    def test_a_folder_whose_encoded_name_is_too_long_gets_the_slug_and_the_hash(self):
        folder = self.home / ("d" * 120) / ("My Long Project " * 9).strip()
        folder.mkdir(parents=True)
        self.assertGreater(len(urllib.parse.quote(str(folder), safe="")), 255)
        api.import_session(self.source(GROK_WIN), target=str(folder))
        # 40 characters of the slug (its cut is not trimmed again), "-", the hash.
        slug = "my-long-project-my-long-project-my-long-"
        self.assertEqual(len(slug), 40)
        digest = blake3.hexdigest(str(folder).encode("utf-8"))
        self.assert_imported_for(str(folder), f"{slug}-{digest[:16]}")

    def test_the_real_path_of_the_chosen_folder_is_what_counts(self):
        link = self.home / "link to it"
        os.symlink(self.chosen, link)
        plan = api.import_plan(self.source(GROK_WIN), target=str(link))
        self.assertEqual((plan["cwd_local"], plan["chosen_folder"]), (str(self.chosen), str(link)))
        api.import_session(self.source(GROK_WIN), target=str(link))
        # Filed under the folder itself: the tool goes by the physical path.
        self.assert_imported_for(str(self.chosen), urllib.parse.quote(str(self.chosen), safe=""))
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd_local"], record["chosen_folder"]), (str(self.chosen), str(link))
        )

    def test_a_group_that_exists_for_the_chosen_folder_is_used(self):
        group = urllib.parse.quote(str(self.chosen), safe="")
        (self.sessions / group).mkdir(parents=True)
        api.import_session(self.source(GROK_WIN), target=str(self.chosen))
        self.assert_imported_for(str(self.chosen), group)


# --- the command --------------------------------------------------------------


class ImportToCommandTest(FolderCase):
    def setUp(self):
        super().setUp()
        self.key = self.source(CLAUDE_WIN).key

    def test_dry_run_shows_the_folder_and_that_it_was_chosen(self):
        code, out, err = run("import", self.key, "--to", str(self.chosen), "--dry-run")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(
            lines[1], f"project folder: {RECORDED} -> {self.chosen} (chosen with --to)"
        )
        self.assertIn(f"/projects/{encode_cwd(str(self.chosen))}/", lines[2])
        self.assertEqual(lines[-1], "dry run: nothing was written")
        # Without --to the line is what it was.
        code, out, err = run("import", self.key, "--dry-run")
        self.assertEqual(out.splitlines()[1], f"project folder: {RECORDED} -> {self.w.site}")
        # Codex: the values that would be written name the chosen folder.
        code, out, err = run("import", CODEX_WIN, "--to", str(self.chosen), "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"\n  session_meta payload.cwd: {RECORDED} -> {self.chosen}\n", out)
        # Grok through a link: the real path is used, and the line says so.
        link = self.home / "link"
        os.symlink(self.chosen, link)
        code, out, err = run("import", GROK_WIN, "--to", str(link), "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(
            f"project folder: {RECORDED} -> {self.chosen} (chosen with --to {link})\n", out
        )
        self.assert_nothing_written()

    def test_a_session_without_a_folder_says_so_in_the_dry_run(self):
        line = cli._folder_line({"cwd": None, "cwd_local": "/t", "chosen_folder": "/t"})
        self.assertEqual(line, "project folder: not recorded -> /t (chosen with --to)")

    def test_import_into_a_chosen_folder(self):
        self.lose_the_folder()
        self.assertEqual(run("import", self.key), (1, "", f"agent-sessions: {NOT_FOUND}\n"))
        code, out, err = run("import", self.key, "--to", str(self.chosen))
        native = self.w.session(CLAUDE_WIN, "native")
        self.assertEqual((code, out), (0, native.key + "\n"))
        self.assertIn(f"into {self.home / '.claude'}", err)
        self.assertEqual(
            os.listdir(self.home / ".claude" / "projects"), [encode_cwd(str(self.chosen))]
        )
        self.assertEqual(journal.read()[0]["chosen_folder"], str(self.chosen))

    def test_a_tilde_is_expanded_by_the_command_and_only_there(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            code, out, err = run("import", self.key, "--to", "~/Work/new place", "--dry-run")
            self.assertEqual((code, err), (0, ""))
            self.assertIn(f" -> {self.chosen} (chosen with --to)\n", out)
            self.assertFalse(api.can_import(self.key, target="~/Work/new place")[0])

    def test_a_folder_that_will_not_do_exits_one_with_the_reason(self):
        afile = self.home / "a file"
        afile.write_text("x", encoding="utf-8")
        for target, needle in (
            ("Work", "the folder to import into must be an absolute path: Work"),
            (str(self.home / "nowhere"), "the folder to import into does not exist: "),
            (str(afile), "the folder to import into is not a directory: "),
        ):
            for extra in ((), ("--dry-run",), ("--open",)):
                with self.subTest(target=target, extra=extra):
                    code, out, err = run("import", self.key, "--to", target, *extra)
                    self.assertEqual((code, out), (1, ""))
                    self.assertTrue(err.startswith(f"agent-sessions: {needle}"), err)
        self.assertEqual(run("import", self.key, "--to")[0], 2)  # the option needs its value
        self.assertEqual(self.w.started, [])
        self.assert_nothing_written()


if __name__ == "__main__":
    unittest.main()
