"""importer.py, the adapters' import plans and api.import_session.

Everything runs on the machine of tests/fixture_world.py: the Windows
fixtures on a fake drive are the foreign sources, a temp home holds the
native stores. Rules: docs/SPEC.md "Import", docs/FORMATS.md section 6 of
each provider.
"""

import collections
import errno
import json
import os
import shutil
import stat
import sys
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_NO_CWD,
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CLAUDE_WIN_PLAIN,
    CODEX_NATIVE,
    CODEX_SEGMENTED,
    CODEX_WIN,
    GEMINI_WIN,
    GROK_WIN,
    World,
    age_access_times,
    files_below,
    tree_state,
)
from support import FIXTURES, state_names

from agent_sessions import api, blake3, importer, journal
from agent_sessions.model import make_key
from agent_sessions.providers import antigravity, gemini
from agent_sessions.providers import grok as grok_mod
from agent_sessions.providers.base import NotSupported
from agent_sessions.providers.claude import Claude, encode_cwd
from agent_sessions.providers.codex import _CwdRewrite
from agent_sessions.providers.grok import Grok, _SummaryRewrite

CODEX_ROLLOUT = "rollout-2026-08-29T13-57-23-01a04ca1-6e27-7817-9f54-07105f962073.jsonl"
SEGMENT_BASE = "rollout-2026-09-28T05-04-58-01a0e538-c7bb-77dd-885b-0c4d54663241.jsonl"
SEGMENT_LAST = (
    "rollout-2026-09-28T06-22-25-01a0e538-c7bb-77dd-885b-0c4d54663241"
    "_01a0e57f-b05d-7b55-a44a-dbc2d903cdef.jsonl"
)
# Payload keys an import may change, per record type (FORMATS "Codex" section 6).
CODEX_ALLOWED = {
    "session_meta": {"cwd", "runtime_workspace_roots"},
    "turn_context": {"cwd", "workspace_roots"},
}


class StubMap:
    """A path map that knows exactly the paths it is given."""

    def __init__(self, table):
        self.table = table

    def to_local(self, recorded, mount=None):
        local = self.table.get(recorded)
        return (local, True) if local else (None, False)


def read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def codex_line_diff(case, source, copy, allowed=CODEX_ALLOWED) -> list[tuple]:
    """Compare two rollouts line by line. Every line must be byte-identical
    or differ only in the allowed payload keys; returns the differences as
    ``(line number, record type, key, old, new)``."""
    old_lines, new_lines = read(source).split(b"\n"), read(copy).split(b"\n")
    case.assertEqual(len(old_lines), len(new_lines))
    changed = []
    for number, (old, new) in enumerate(zip(old_lines, new_lines, strict=False)):
        if old == new:
            continue
        before, after = json.loads(old), json.loads(new)
        kind = before["type"]
        case.assertIn(kind, allowed, f"line {number} changed")
        case.assertEqual(list(before), list(after), "key order kept")
        case.assertEqual(
            {k: v for k, v in before.items() if k != "payload"},
            {k: v for k, v in after.items() if k != "payload"},
        )
        case.assertEqual(list(before["payload"]), list(after["payload"]))
        for key in before["payload"]:
            if before["payload"][key] != after["payload"][key]:
                case.assertIn(key, allowed[kind], f"line {number}: {key} changed")
                changed.append((number, kind, key, before["payload"][key], after["payload"][key]))
        # The rewritten line is in the tool's own form: compact, non-ASCII as it is.
        case.assertEqual(
            new, json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    return changed


class ImportCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("antigravity", "claude", "codex", "gemini", "grok")
    native: tuple[str, ...] = ()

    def setUp(self):
        self.w = World(self, self.foreign, self.native)
        self.home = self.w.home
        self.index = self.w.scan()

    def source(self, session_id):
        return self.w.session(session_id, "foreign")

    @property
    def record_file(self) -> Path:
        return Path(os.environ["AGENT_SESSIONS_STATE"]) / "imports.jsonl"

    def journal_kinds(self) -> list[str]:
        """What each line of the journal is, read by hand: one JSON object
        per line. None when there is no journal."""
        if not self.record_file.exists():
            return []
        lines = self.record_file.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        self.assertEqual(records, journal.read())
        return [record["record"] for record in records]

    def records(self) -> list[dict]:
        """The import records of the journal. Every one of them follows the
        ``begin`` line of its import, which is not returned."""
        kinds = self.journal_kinds()
        self.assertEqual(kinds[0::2], ["begin"] * (len(kinds) // 2), kinds)
        self.assertEqual(len(kinds) % 2, 0, kinds)
        return [record for record in journal.read() if record["record"] == "import"]

    def refused(self, ref, **kwargs) -> str:
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(ref, **kwargs)
        return caught.exception.reason

    def assert_nothing_written(self, *stores):
        """Nothing in the native stores, and no import in the journal: none
        that stands, and none left open. One that had begun to put its
        copy in place has its two lines there, ``begin`` and ``rollback``."""
        for name in stores or (".claude", ".codex", ".grok", ".gemini"):
            self.assertFalse((self.home / name).exists(), name)
        kinds = self.journal_kinds()
        self.assertEqual(kinds, ["begin", "rollback"] * (len(kinds) // 2))
        self.assertEqual(journal.unsettled(journal.read()), [])


# --- Claude -----------------------------------------------------------------


class ClaudeImportTest(ImportCase):
    foreign = ("claude",)

    def setUp(self):
        super().setUp()
        self.store = self.w.profile / ".claude"
        self.project = self.store / "projects" / "F--Proj-My-Site"
        self.target = self.home / ".claude" / "projects" / encode_cwd(str(self.w.site))

    def test_copy_is_byte_identical_and_source_is_untouched(self):
        # Things that belong to the session but are not copied (FORMATS 6.5).
        for extra in (
            f"file-history/{CLAUDE_WIN}/a@v1",
            f"session-env/{CLAUDE_WIN}/env",
            "history.jsonl",
            "sessions/123.json",
        ):
            path = self.store / extra
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        age_access_times(self.w.c)
        before = tree_state(self.w.c)
        source = self.source(CLAUDE_WIN)
        self.assertEqual(api.can_import(source), (True, ""))

        native = api.import_session(source.key)

        self.assertEqual(tree_state(self.w.c), before, "source times and sizes")
        copied = files_below(self.target)
        self.assertEqual(
            files_below(self.home / ".claude"),
            [f"projects/{self.target.name}/{rel}" for rel in copied],
        )
        self.assertEqual(
            copied,
            sorted(
                [f"{CLAUDE_WIN}.jsonl"]
                + [f"{CLAUDE_WIN}/{rel}" for rel in files_below(self.project / CLAUDE_WIN)]
            ),
        )
        self.assertGreater(len(copied), 1, "the fixture has a side directory")
        for rel in copied:
            self.assertEqual(read(self.target / rel), read(self.project / rel), rel)
            copy, original = os.stat(self.target / rel), os.stat(self.project / rel)
            self.assertEqual(copy.st_mtime_ns, original.st_mtime_ns, rel)
            self.assertEqual(stat.S_IMODE(copy.st_mode), 0o600, rel)
        self.assertEqual(sorted(os.listdir(self.home / ".claude")), ["projects"])

        self.assertEqual(
            (native.origin, native.provider, native.id, native.launchable),
            ("native", "claude", CLAUDE_WIN, True),
        )
        self.assertEqual(native.key, make_key("claude", self.home / ".claude", CLAUDE_WIN))
        self.assertEqual((native.cwd, native.cwd_local), (source.cwd, str(self.w.site)))
        self.assertEqual(
            (native.title, native.messages, native.started, native.updated),
            (source.title, source.messages, source.started, source.updated),
        )
        self.assertEqual(
            native.files, [str(self.target / f"{CLAUDE_WIN}.jsonl"), str(self.target / CLAUDE_WIN)]
        )
        index = api.load_index()
        again = self.w.session(CLAUDE_WIN, "foreign", index)
        self.assertEqual(again.imported_as, native.key)
        self.assertEqual(index.where(again), "C: · imported")
        self.assertEqual(self.w.session(CLAUDE_WIN, "native", index), native)
        self.assertEqual(api.can_import(again), (False, "already imported"))

    def test_provenance_record(self):
        source = self.source(CLAUDE_WIN)
        native = api.import_session(source)
        (record,) = self.records()
        self.assertRegex(record.pop("imported_at"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        manifest = record.pop("manifest")
        self.assertEqual(
            record,
            {
                "record": "import",
                "version": 2,
                "provider": "claude",
                "id": CLAUDE_WIN,
                "source_key": source.key,
                "native_key": native.key,
                "source_store": str(self.store),
                "source_mount": str(self.w.c),
                "source_files": source.files,
                "destination_store": str(self.home / ".claude"),
                "destination_files": [
                    str(self.target / CLAUDE_WIN),
                    str(self.target / f"{CLAUDE_WIN}.jsonl"),
                ],
                "cwd": "F:\\Proj\\My Site",
                "cwd_local": str(self.w.site),
                "chosen_folder": None,
                "bytes": sum(
                    os.path.getsize(self.project / rel)
                    for rel in files_below(self.project)
                    if rel.startswith(CLAUDE_WIN)
                ),
                "rewritten": {},
                "appended": [],
                "agent_sessions_version": "0.2.0",
                "source_tool_version": "2.1.293",
            },
        )
        # The manifest: everything this import created, as it lies there.
        created = {str(self.home / ".claude"), str(self.target.parent), str(self.target)}
        files = [rel for rel in files_below(self.target) if rel.startswith(CLAUDE_WIN)]
        for rel in files:
            parent = os.path.dirname(rel)
            while parent:
                created.add(str(self.target / parent))
                parent = os.path.dirname(parent)
        self.assertEqual(manifest["dirs"], sorted(created))
        self.assertEqual(
            manifest["files"],
            [
                {
                    "path": path,
                    "size": os.path.getsize(path),
                    "mtime_ns": os.stat(path).st_mtime_ns,
                }
                for path in sorted(str(self.target / rel) for rel in files)
            ],
        )
        self.assertGreater(len(manifest["files"]), 1)
        api.import_session(self.source(CLAUDE_WIN_PLAIN))
        second = self.records()
        self.assertEqual([r["id"] for r in second], [CLAUDE_WIN, CLAUDE_WIN_PLAIN])
        # This time only the transcript was created: its folder was there.
        self.assertEqual(
            second[1]["manifest"],
            {
                "dirs": [],
                "files": [
                    {
                        "path": str(self.target / f"{CLAUDE_WIN_PLAIN}.jsonl"),
                        "size": os.path.getsize(self.project / f"{CLAUDE_WIN_PLAIN}.jsonl"),
                        "mtime_ns": os.stat(self.project / f"{CLAUDE_WIN_PLAIN}.jsonl").st_mtime_ns,
                    }
                ],
            },
        )
        # The journal is private, and no lock file stays behind.
        self.assertEqual(state_names(), ["imports.jsonl"])
        self.assertEqual(stat.S_IMODE(os.stat(self.record_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.record_file.parent).st_mode), 0o700)

    def test_side_directory_goes_first_and_the_transcript_last(self):
        plan = Claude().import_plan(self.source(CLAUDE_WIN), self.index.pathmap)
        self.assertEqual(
            [(unit.is_dir, os.path.basename(unit.dst)) for unit in plan.units],
            [(True, CLAUDE_WIN), (False, f"{CLAUDE_WIN}.jsonl")],
        )
        self.assertTrue(all(item.rewrite is None for unit in plan.units for item in unit.files))
        placed = []
        real = importer._place

        def place(target, dst, is_dir):
            placed.append((dst, is_dir))
            return real(target, dst, is_dir)

        with mock.patch.object(importer, "_place", place):
            api.import_session(self.source(CLAUDE_WIN))
        self.assertEqual(
            placed,
            [
                (str(self.target / CLAUDE_WIN), True),
                (str(self.target / f"{CLAUDE_WIN}.jsonl"), False),
            ],
        )
        plain = Claude().import_plan(self.source(CLAUDE_WIN_PLAIN), self.index.pathmap)
        self.assertEqual([unit.is_dir for unit in plain.units], [False])

    def test_second_import_returns_the_existing_copy(self):
        source = self.source(CLAUDE_WIN)
        stale = self.index  # taken before the import
        first = api.import_report(source)
        self.assertTrue(first.created)
        self.assertEqual(len(first.files), 2)
        before = tree_state(self.home / ".claude")
        second = api.import_report(self.source(CLAUDE_WIN))
        self.assertEqual((second.created, second.session, second.files), (False, first.session, []))
        # A caller with an old index: the copy is found on disk, not copied again.
        third = api.import_report(source, index=stale)
        self.assertEqual((third.created, third.session), (False, first.session))
        self.assertEqual(api.import_session(source.key), first.session)
        self.assertEqual(tree_state(self.home / ".claude"), before)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(api.import_plan(source.key)["already"], first.session.key)

    def test_an_id_already_in_the_native_store_is_never_overwritten(self):
        elsewhere = self.home / ".claude" / "projects" / "-some-other-dir"
        elsewhere.mkdir(parents=True)
        taken = elsewhere / f"{CLAUDE_WIN}.jsonl"
        taken.write_bytes(b"not a transcript\n")
        reason = self.refused(self.source(CLAUDE_WIN))
        self.assertIn("already in the native store", reason)
        self.assertIn(str(taken), reason)
        self.assertEqual(api.can_import(self.source(CLAUDE_WIN)), (False, reason))
        self.assertEqual(read(taken), b"not a transcript\n")
        self.assertEqual(
            files_below(self.home / ".claude"), [f"projects/-some-other-dir/{CLAUDE_WIN}.jsonl"]
        )
        self.assertFalse(self.record_file.exists())

        # The same id as a real session of the native store: that one is the copy.
        shutil.copy(self.project / f"{CLAUDE_WIN_PLAIN}.jsonl", taken)
        kept = read(taken)
        report = api.import_report(self.source(CLAUDE_WIN))
        self.assertEqual(
            (report.created, report.session.origin, report.session.files),
            (False, "native", [str(taken)]),
        )
        self.assertEqual(read(taken), kept)
        self.assertFalse(self.target.exists())

    def test_a_leftover_side_directory_is_not_overwritten(self):
        leftover = self.target / CLAUDE_WIN
        leftover.mkdir(parents=True)
        (leftover / "mine.txt").write_text("keep", encoding="utf-8")
        self.assertIn(str(leftover), self.refused(self.source(CLAUDE_WIN)))
        self.assertEqual(
            files_below(self.home / ".claude"),
            [f"projects/{self.target.name}/{CLAUDE_WIN}/mine.txt"],
        )

    def test_failure_while_copying_leaves_nothing_behind(self):
        real, calls = importer._copy, []

        def failing(item, target, tick):
            calls.append(item.src)
            if len(calls) == 2:
                raise OSError(errno.EIO, "Input/output error", item.src)
            return real(item, target, tick)

        with mock.patch.object(importer, "_copy", failing):
            reason = self.refused(self.source(CLAUDE_WIN))
        self.assertEqual(len(calls), 2)
        self.assertIn("Input/output error", reason)
        self.assertIn("nothing was left", reason)
        self.assert_nothing_written()
        self.assertIsNone(self.w.scan().sessions[0].imported_as)
        api.import_session(self.source(CLAUDE_WIN))  # and it works afterwards
        self.assertEqual(len(self.records()), 1)

    def test_failure_while_putting_in_place_rolls_back(self):
        keep = self.home / ".claude" / "projects" / "-kept" / "note.txt"
        keep.parent.mkdir(parents=True)
        keep.write_text("mine", encoding="utf-8")
        real, calls = importer._place, []

        def failing(src, dst, is_dir):
            calls.append(dst)
            if len(calls) == 2:  # the side directory is in place, the transcript is not
                raise OSError(errno.ENOSPC, "No space left on device", dst)
            return real(src, dst, is_dir)

        with mock.patch.object(importer, "_place", failing):
            reason = self.refused(self.source(CLAUDE_WIN))
        self.assertEqual(
            calls, [str(self.target / CLAUDE_WIN), str(self.target / f"{CLAUDE_WIN}.jsonl")]
        )
        self.assertIn("No space left on device", reason)
        self.assertEqual(files_below(self.home / ".claude"), ["projects/-kept/note.txt"])
        self.assertEqual(sorted(os.listdir(self.home / ".claude")), ["projects"])
        self.assertEqual(os.listdir(self.home / ".claude" / "projects"), ["-kept"])
        # It had begun: the journal says so, and that it was taken out again.
        self.assertEqual(state_names(), ["imports.jsonl"])
        self.assertEqual(self.journal_kinds(), ["begin", "rollback"])
        begin, rolled_back = journal.read()
        self.assertEqual(begin["destinations"], calls)
        self.assertEqual(
            rolled_back["removed"], sorted([str(self.target), str(self.target / CLAUDE_WIN)])
        )
        self.assertEqual((rolled_back["recovered"], rolled_back["id"]), (False, CLAUDE_WIN))
        self.assertEqual(self.records(), [])

    def test_an_interrupted_copy_leaves_nothing_behind(self):
        class Stop(Exception):
            pass

        def progress(done, total):
            raise Stop()

        with self.assertRaises(Stop):
            api.import_session(self.source(CLAUDE_WIN), progress=progress)
        self.assert_nothing_written()

    def test_progress_reports_bytes(self):
        seen = []
        native = api.import_session(
            self.source(CLAUDE_WIN), progress=lambda done, total: seen.append((done, total))
        )
        total = self.records()[0]["bytes"]
        self.assertEqual(seen[-1], (total, total))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual({t for _d, t in seen}, {total})
        self.assertTrue(native.launchable)

    def test_not_enough_free_space(self):
        usage = collections.namedtuple("usage", "total used free")
        need = importer.free_space(
            Claude().import_plan(self.source(CLAUDE_WIN), self.index.pathmap)
        )[0]
        size = sum(
            os.path.getsize(self.project / rel)
            for rel in files_below(self.project)
            if rel.startswith(CLAUDE_WIN)
        )
        self.assertEqual(need, size + -(-size // 10))  # the copy plus 10 %, rounded up
        with mock.patch.object(
            importer.shutil, "disk_usage", return_value=usage(10**9, 10**9, need - 1)
        ) as probe:
            reason = self.refused(self.source(CLAUDE_WIN))
            self.assertEqual(api.can_import(self.source(CLAUDE_WIN)), (False, reason))
            with self.assertRaises(api.ImportFailed):
                api.import_plan(CLAUDE_WIN)
        self.assertIn("not enough free space", reason)
        # Asked of the nearest directory that exists: the store is not there yet.
        self.assertEqual(probe.call_args[0][0], str(self.home))
        self.assert_nothing_written()
        with mock.patch.object(
            importer.shutil, "disk_usage", return_value=usage(10**9, 10**9, need)
        ):
            api.import_session(self.source(CLAUDE_WIN))

    def test_missing_project_folder_is_refused(self):
        shutil.rmtree(self.w.site)  # its parent Proj/ stays
        self.w.scan()
        source = self.source(CLAUDE_WIN)
        self.assertEqual((source.cwd_local, source.cwd_exists), (None, False))
        reason = self.refused(source)
        self.assertEqual(
            reason, "the project folder F:\\Proj\\My Site was not found on any mounted drive"
        )
        self.assertEqual(api.can_import(source), (False, reason))
        with self.assertRaises(api.ImportFailed):
            api.import_plan(source.key)
        self.assert_nothing_written()

    def test_a_session_without_a_recorded_folder_is_refused(self):
        source = self.source(CLAUDE_WIN)
        source.cwd = None
        self.assertEqual(self.refused(source), "the project folder of this session is not recorded")

    def test_a_native_session_is_not_imported(self):
        shutil.copytree(FIXTURES / "claude" / "native" / ".claude", self.home / ".claude")
        self.w.scan()
        own = self.w.session(CLAUDE_NATIVE, "native")
        self.assertEqual(self.refused(own), "this session is already on Linux")
        self.assertEqual(self.refused(CLAUDE_NATIVE), "this session is already on Linux")
        self.assertEqual(api.can_import(own), (False, "this session is already on Linux"))
        self.assertFalse(self.record_file.exists())

    def test_the_native_side_of_an_imported_pair_counts_as_imported(self):
        native = api.import_session(self.source(CLAUDE_WIN))
        # A bare id names the native copy once there is one.
        self.assertEqual(api.find_session(CLAUDE_WIN), native)
        for ref in (native, native.key, CLAUDE_WIN):
            report = api.import_report(ref)
            self.assertEqual((report.created, report.session), (False, native))
            self.assertEqual(api.can_import(ref), (False, "already imported"))
            self.assertEqual(api.import_plan(ref)["already"], native.key)
        self.assertEqual(len(self.records()), 1)

    def test_an_unreadable_journal_stops_the_import_first(self):
        """A journal that cannot be read could not take the record either."""
        self.record_file.mkdir(parents=True)  # a directory where the journal belongs
        reason = self.refused(self.source(CLAUDE_WIN))
        self.assertIn(str(self.record_file), reason)
        self.assertIn("cannot be read", reason)
        self.assertEqual(api.can_import(self.source(CLAUDE_WIN)), (False, reason))
        self.assertFalse((self.home / ".claude").exists())
        self.assertEqual(state_names(), ["imports.jsonl"])

    def test_symlinks_in_the_side_directory_are_not_followed(self):
        outside = self.w.c / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        os.symlink(outside, self.project / CLAUDE_WIN / "link.txt")
        os.symlink(self.w.c, self.project / CLAUDE_WIN / "loop")
        api.import_session(self.source(CLAUDE_WIN))
        self.assertEqual(
            files_below(self.target / CLAUDE_WIN),
            [rel for rel in files_below(self.project / CLAUDE_WIN) if rel != "link.txt"],
        )
        self.assertFalse((self.target / CLAUDE_WIN / "loop").exists())


class ClaudeEncodingTest(unittest.TestCase):
    """Expected names were produced by the tool's own functions (``OO``,
    ``Mse`` of 2.1.296) run under node; see FORMATS "Claude Code" section 2."""

    def test_short_paths(self):
        for cwd, expected in (
            ("/home/user/Work", "-home-user-Work"),
            ("F:\\Proj\\My Site", "F--Proj-My-Site"),
            ("d:/Fable 5/Crime YT", "d--Fable-5-Crime-YT"),
            ("/a.b_c~d", "-a-b-c-d"),
            ("", ""),
        ):
            self.assertEqual(encode_cwd(cwd), expected)

    def test_non_ascii(self):
        self.assertEqual(encode_cwd("/home/u/café/日本語 プロジェクト"), "-home-u-caf------------")
        # A character outside the BMP is two UTF-16 units: two dashes.
        self.assertEqual(encode_cwd("/home/u/😀 emoji/x"), "-home-u----emoji-x")
        self.assertEqual(encode_cwd("/x/\udcff"), "-x--")  # undecodable byte in a name

    def test_long_paths_are_cut_and_hashed(self):
        self.assertEqual(encode_cwd("/" + "a" * 199), "-" + "a" * 199)  # exactly 200
        self.assertEqual(encode_cwd("/" + "a" * 200), "-" + "a" * 199 + "-b6ymvl")
        self.assertEqual(
            encode_cwd(
                "/run/media/user/0123456789ABCDEF/Users/user/AppData/Local/Packages/"
                "Claude_abcdefghijklm/LocalCache/Roaming/Claude/scratch-workspaces/"
                "8a21c259-d093-4e80-a321-f34fea7db796/"
                "52e70e85-b186-4c88-a223-8e60f51572d4/scratch-2026-10-08-e01e1a"
            ),
            "-run-media-user-0123456789ABCDEF-Users-user-AppData-Local-Packages-"
            "Claude-abcdefghijklm-LocalCache-Roaming-Claude-scratch-workspaces-"
            "8a21c259-d093-4e80-a321-f34fea7db796-52e70e85-b186-4c88-a223-8e60f5-nw6s0t",
        )
        self.assertEqual(encode_cwd("/x/" + "Zz" * 150), "-x-" + "Zz" * 98 + "Z-2ol2fu")

    def test_long_non_ascii_path(self):
        # The hash is over the original text, the cut over the encoded one.
        encoded = encode_cwd("/data/" + "long-segment_é😀/" * 20 + "end")
        self.assertEqual(len(encoded), 207)
        self.assertTrue(encoded.endswith("-long-se-30zep1"), encoded)
        self.assertTrue(encoded.startswith("-data-long-segment-----long-segment-----"))


# --- Codex ------------------------------------------------------------------


class CodexImportTest(ImportCase):
    foreign = ("codex",)

    def setUp(self):
        super().setUp()
        self.day = Path("sessions") / "2026" / "08" / "29"
        self.original = self.w.profile / ".codex" / self.day / CODEX_ROLLOUT
        self.copy = self.home / ".codex" / self.day / CODEX_ROLLOUT

    def test_only_the_working_directories_change(self):
        age_access_times(self.w.c)
        before = tree_state(self.w.c)
        source = self.source(CODEX_WIN)
        native = api.import_session(source.key)
        self.assertEqual(tree_state(self.w.c), before, "source times and sizes")
        # Same YYYY/MM/DD and name; no index (the source has none), no database.
        self.assertEqual(files_below(self.home / ".codex"), [str(self.day / CODEX_ROLLOUT)])
        site = str(self.w.site)
        changed = codex_line_diff(self, self.original, self.copy)
        self.assertEqual(
            changed,
            [
                (0, "session_meta", "cwd", "F:\\Proj\\My Site", site),
                (4, "turn_context", "cwd", "F:\\Proj\\My Site", site),
                (4, "turn_context", "workspace_roots", ["F:\\Proj\\My Site"], [site]),
            ],
        )
        self.assertEqual(os.stat(self.copy).st_mtime_ns, os.stat(self.original).st_mtime_ns)
        self.assertEqual(
            (native.origin, native.launchable, native.id, native.files),
            ("native", True, CODEX_WIN, [str(self.copy)]),
        )
        self.assertEqual((native.cwd, native.cwd_local), (site, site))
        self.assertEqual(
            (native.title, native.messages, native.started, native.updated),
            (source.title, source.messages, source.started, source.updated),
        )
        self.assertEqual(self.source(CODEX_WIN).imported_as, native.key)
        (record,) = self.records()
        self.assertEqual(record["source_tool_version"], "0.150.1")
        self.assertEqual(record["appended"], [])
        # The manifest has the file as it was placed, not as it was planned:
        # the rewrite changed its length.
        placed = os.stat(self.copy)
        self.assertEqual(
            record["manifest"]["files"],
            [{"path": str(self.copy), "size": placed.st_size, "mtime_ns": placed.st_mtime_ns}],
        )
        self.assertNotEqual(placed.st_size, record["bytes"])
        self.assertEqual(record["bytes"], os.path.getsize(source.files[0]))
        day = self.copy.parent
        self.assertEqual(
            record["manifest"]["dirs"],
            sorted(str(path) for path in (day, *list(day.parents)[:4])),  # up to ~/.codex
        )
        self.assertEqual(Path(record["manifest"]["dirs"][0]), self.home / ".codex")
        self.assertEqual(
            record["rewritten"],
            {
                str(self.copy): [
                    f"session_meta payload.cwd: F:\\Proj\\My Site -> {site}",
                    f"turn_context payload.cwd: F:\\Proj\\My Site -> {site}",
                    f"turn_context payload.workspace_roots[]: F:\\Proj\\My Site -> {site}",
                ]
            },
        )

    def test_idempotent_and_no_overwrite(self):
        first = api.import_report(self.source(CODEX_WIN))
        kept = read(self.copy)
        second = api.import_report(self.source(CODEX_WIN))
        self.assertEqual(
            (first.created, second.created, second.session), (True, False, first.session)
        )
        self.assertEqual(read(self.copy), kept)
        self.assertEqual(len(self.records()), 1)

    def test_a_rollout_with_the_thread_id_anywhere_is_a_collision(self):
        other_day = self.home / ".codex" / "sessions" / "2025" / "01" / "02"
        other_day.mkdir(parents=True)
        taken = other_day / f"rollout-2025-01-02T03-04-05-{CODEX_WIN}.jsonl"
        taken.write_bytes(b"{}\n")
        reason = self.refused(self.source(CODEX_WIN))
        self.assertIn(str(taken), reason)
        self.assertEqual(read(taken), b"{}\n")
        self.assertFalse(self.copy.exists())
        self.assertFalse((self.home / ".codex" / "sessions" / "2026").exists())

    def test_name_comes_from_the_orca_mirror_and_is_appended(self):
        mirror = self.w.profile / "AppData" / "Roaming" / "orca" / "codex-runtime-home" / "home"
        mirror.mkdir(parents=True)
        last = (
            b'{"id":"%s","thread_name":"Site r\xc3\xa9vision","updated_at":'
            b'"2026-08-29T08:31:20.364482Z"}' % CODEX_WIN.encode()
        )
        (mirror / "session_index.jsonl").write_bytes(
            b'{"id":"%s","thread_name":"first name","updated_at":"x"}\r\n' % CODEX_WIN.encode()
            + b'{"id":"other","thread_name":"not this one","updated_at":"y"}\n'
            # As a Windows Codex writes it: seven fraction digits.
            + last.replace(b"364482Z", b"3644826Z")
            + b"\n"
        )
        index = self.home / ".codex" / "session_index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_bytes(b'{"id":"mine","thread_name":"kept","updated_at":"z"}')  # no newline
        self.w.scan()
        plan = api.import_plan(CODEX_WIN)
        self.assertEqual(plan["appends"], [{"file": str(index), "what": "the thread's name"}])
        self.assertEqual(read(index), b'{"id":"mine","thread_name":"kept","updated_at":"z"}')
        native = api.import_session(CODEX_WIN)
        self.assertEqual(
            read(index), b'{"id":"mine","thread_name":"kept","updated_at":"z"}\n' + last + b"\n"
        )
        self.assertEqual(native.title, "Site révision")
        self.assertEqual(self.records()[0]["appended"], [str(index)])

    def name_line(self, line: bytes, native: bytes | None = None) -> bytes:
        """Import the thread with ``line`` as the last line of the source's
        index; returns what the import added to the native one."""
        own = self.w.profile / ".codex" / "session_index.jsonl"
        own.write_bytes(
            b'{"id":"%s","thread_name":"an older name","updated_at":"2026-08-01T00:00:00Z"}\n'
            % CODEX_WIN.encode()
            + line
            + b"\n"
        )
        index = self.home / ".codex" / "session_index.jsonl"
        if native is not None:
            index.parent.mkdir(parents=True)
            index.write_bytes(native)
        self.w.scan()
        api.import_session(CODEX_WIN)
        self.assertTrue(self.copy.exists())
        after = read(index) if index.exists() else b""
        self.assertTrue(after.startswith(native or b""))
        return after[len(native or b"") :]

    MINE = b'{"id":"%s","thread_name":"mine","updated_at":"2026-10-01T00:00:00Z"}\n' % (
        CODEX_NATIVE.encode()
    )

    def test_the_name_line_is_written_anew_and_never_copied(self):
        """docs/AUDIT.md A-11: the line is the one write an undo never takes
        back, and it was the source's bytes as they were. Now it is a line
        of our own, made of three checked values and nothing else."""
        added = self.name_line(
            b'  {"updated_at" : "2026-08-29T09:31:15.3316788+02:00", "anything": {"else": [1, 2]},'
            b' "thread_name": "RENAMED\\u001b[2J BY \xe2\x80\xaeTHE DRIVE", "id": "%s"}\r'
            % CODEX_WIN.encode(),
            self.MINE,
        )
        self.assertEqual(
            added,
            b'{"id":"%s","thread_name":"RENAMED [2J BY  THE DRIVE",'
            b'"updated_at":"2026-08-29T07:31:15.331678Z"}\n' % CODEX_WIN.encode(),
        )

    def test_a_line_with_a_key_twice_is_not_a_name_line(self):
        """The auditor's line: the first ``id`` is one of the user's own
        threads, the last the thread's. Which of the two a parser takes is
        the parser's choice; nothing of such a line is written."""
        added = self.name_line(
            b'{"id":"%s","thread_name":"RENAMED BY THE DRIVE","updated_at":"2099-01-01T00:00:00Z",'
            b'"id":"%s"}' % (CODEX_NATIVE.encode(), CODEX_WIN.encode()),
            self.MINE,
        )
        self.assertEqual(added, b"")
        self.assertEqual(self.records()[-1]["appended"], [])

    def test_a_name_that_is_not_utf8_is_not_written(self):
        added = self.name_line(
            b'{"id":"%s","thread_name":"caf\xe9 \xff\xfe name"}' % CODEX_WIN.encode(), self.MINE
        )
        self.assertEqual(added, b"")
        read(self.home / ".codex" / "session_index.jsonl").decode("utf-8")  # still text

    def test_a_name_that_cannot_be_written_as_text_is_not_written(self):
        # A lone surrogate, escaped: valid JSON, and no text a tool can read back.
        added = self.name_line(
            b'{"id":"%s","thread_name":"a \\ud800 name"}' % CODEX_WIN.encode(), self.MINE
        )
        self.assertEqual(added, b"")

    def test_the_name_is_bounded_and_a_time_that_is_none_is_left_out(self):
        for line, expected in (
            (
                b'{"id":"%s","thread_name":"' + b"n" * 5000 + b'","updated_at":"tomorrow"}',
                b'{"id":"%s","thread_name":"' + b"n" * 200 + b'"}\n',
            ),
            (
                b'{"id":"%s","thread_name":"a name","updated_at":"9999-01-01T00:00:00Z"}',
                b'{"id":"%s","thread_name":"a name"}\n',
            ),
            (
                b'{"id":"%s","thread_name":"a name","updated_at":{"deep":[1]}}',
                b'{"id":"%s","thread_name":"a name"}\n',
            ),
            (b'{"id":"%s","thread_name":"\\u0007 \\t "}', b""),  # nothing left of the name
            (  # not a name line at all: the one before it is the thread's last
                b'{"id":"%s","thread_name":["a name"]}',
                b'{"id":"%s","thread_name":"an older name",'
                b'"updated_at":"2026-08-01T00:00:00.000000Z"}\n',
            ),
        ):
            with self.subTest(line=line[:60]):
                thread = CODEX_WIN.encode()
                added = self.name_line(line.replace(b"%s", thread))
                self.assertEqual(added, expected.replace(b"%s", thread))
                api.unimport(CODEX_WIN)
                index = self.home / ".codex" / "session_index.jsonl"
                if index.exists():
                    index.unlink()

    def test_an_index_the_import_creates_is_private(self):
        """docs/REVIEW.md R-10: thread names are session data like the rest."""
        own = self.w.profile / ".codex" / "session_index.jsonl"
        own.write_bytes(
            b'{"id":"%s","thread_name":"from the store","updated_at":"t"}\n' % CODEX_WIN.encode()
        )
        self.w.scan()
        index = self.home / ".codex" / "session_index.jsonl"
        self.assertFalse(index.exists())
        native = api.import_session(CODEX_WIN)
        self.assertEqual(native.title, "from the store")
        self.assertEqual(stat.S_IMODE(os.stat(index).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.copy).st_mode), 0o600)

    def test_an_index_that_exists_keeps_its_mode(self):
        own = self.w.profile / ".codex" / "session_index.jsonl"
        own.write_bytes(
            b'{"id":"%s","thread_name":"from the store","updated_at":"t"}\n' % CODEX_WIN.encode()
        )
        index = self.home / ".codex" / "session_index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_bytes(b'{"id":"mine","thread_name":"kept","updated_at":"z"}\n')
        os.chmod(index, 0o640)
        self.w.scan()
        api.import_session(CODEX_WIN)
        self.assertEqual(read(index).count(b"\n"), 2)
        self.assertEqual(stat.S_IMODE(os.stat(index).st_mode), 0o640)

    def test_a_name_the_native_index_already_has_is_not_appended_again(self):
        own = self.w.profile / ".codex" / "session_index.jsonl"
        own.write_bytes(
            b'{"id":"%s","thread_name":"from the store","updated_at":"t"}\n' % CODEX_WIN.encode()
        )
        index = self.home / ".codex" / "session_index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_bytes(
            b'{"id":"%s","thread_name":"local name","updated_at":"u"}\n' % CODEX_WIN.encode()
        )
        self.w.scan()
        native = api.import_session(CODEX_WIN)
        self.assertEqual(read(index).count(b"\n"), 1)
        self.assertEqual(native.title, "local name")

    def test_failed_append_rolls_the_rollout_back(self):
        own = self.w.profile / ".codex" / "session_index.jsonl"
        own.write_bytes(b'{"id":"%s","thread_name":"n","updated_at":"t"}\n' % CODEX_WIN.encode())
        self.w.scan()
        with mock.patch.object(
            importer, "_append", side_effect=OSError(errno.EACCES, "Permission denied", "idx")
        ):
            reason = self.refused(self.source(CODEX_WIN))
        self.assertIn("Permission denied", reason)
        self.assert_nothing_written()

    def test_a_line_that_cannot_be_written_back_stops_the_import(self):
        # A lone surrogate escape: valid JSON, but not text any tool writes.
        lines = read(self.original).split(b"\n")
        lines[0] = lines[0].replace(b'"cwd":', b'"odd":"\\ud800","cwd":', 1)
        self.original.write_bytes(b"\n".join(lines))
        self.w.scan()
        for call in (api.import_session, api.import_plan):
            with self.assertRaises(api.ImportFailed) as caught:
                call(self.source(CODEX_WIN))
            self.assertIn("cannot be rewritten", caught.exception.reason)
            self.assertIn(str(self.original), caught.exception.reason)
        self.assert_nothing_written()

    def test_sqlite_files_are_never_written(self):
        store = self.home / ".codex"
        store.mkdir()
        shutil.copy(FIXTURES / "codex" / "native" / ".codex" / "state_5.sqlite", store)
        before = tree_state(store)
        api.import_session(self.source(CODEX_WIN))
        after = tree_state(store)
        self.assertEqual(
            after[str(store / "state_5.sqlite")], before[str(store / "state_5.sqlite")]
        )
        self.assertEqual(files_below(store), [str(self.day / CODEX_ROLLOUT), "state_5.sqlite"])


class CodexSegmentImportTest(ImportCase):
    """A thread with a continuation segment, as another Linux home on a drive
    (the only segmented thread is in the native fixture)."""

    foreign = ()

    def setUp(self):
        super().setUp()
        self.old = self.w.box.mount("OldLinux")
        self.old_home = self.old / "home" / "user"
        shutil.copytree(FIXTURES / "codex" / "native" / ".codex", self.old_home / ".codex")
        self.w.override({"/home/user": str(self.old_home)})
        self.index = self.w.scan()
        self.day = Path("sessions") / "2026" / "09" / "28"
        self.src = self.old_home / ".codex" / self.day
        self.dst = self.home / ".codex" / self.day

    def test_base_stays_byte_identical_and_only_the_last_segment_is_rewritten(self):
        source = self.source(CODEX_SEGMENTED)
        self.assertEqual([os.path.basename(f) for f in source.files], [SEGMENT_BASE, SEGMENT_LAST])
        self.assertEqual(source.cwd_local, str(self.old_home))
        plan = api.import_plan(source)
        self.assertEqual([os.path.basename(r["file"]) for r in plan["rewrites"]], [SEGMENT_LAST])
        age_access_times(self.old)
        before = tree_state(self.old)

        native = api.import_session(source)

        self.assertEqual(tree_state(self.old), before)
        self.assertEqual(read(self.dst / SEGMENT_BASE), read(self.src / SEGMENT_BASE))
        where = str(self.old_home)
        changed = codex_line_diff(self, self.src / SEGMENT_LAST, self.dst / SEGMENT_LAST)
        self.assertEqual(
            changed,
            [
                (0, "session_meta", "cwd", "/home/user", where),
                (0, "session_meta", "runtime_workspace_roots", ["/home/user"], [where]),
                (4, "turn_context", "cwd", "/home/user", where),
                (4, "turn_context", "workspace_roots", ["/home/user"], [where]),
            ],
        )
        # history_base still describes the untouched base file.
        meta = json.loads(read(self.dst / SEGMENT_LAST).split(b"\n")[0])["payload"]
        self.assertEqual(meta["history_base"]["end_byte_offset"], 802888)
        self.assertEqual(native.files, [str(self.dst / SEGMENT_BASE), str(self.dst / SEGMENT_LAST)])
        self.assertEqual(
            (native.messages, native.title, native.launchable), (source.messages, "title-168", True)
        )
        # The name line: the values of the last one of the source index,
        # written anew (the time in our own form, six fraction digits).
        self.assertEqual(
            read(self.home / ".codex" / "session_index.jsonl"),
            b'{"id":"01a0e538-c7bb-77dd-885b-0c4d54663241","thread_name":"title-168",'
            b'"updated_at":"2026-09-27T23:35:21.831252Z"}\n',
        )
        self.assertEqual(
            files_below(self.home / ".codex"),
            sorted(
                [str(self.day / SEGMENT_BASE), str(self.day / SEGMENT_LAST), "session_index.jsonl"]
            ),
        )

    def test_base_goes_in_first_and_a_failure_takes_it_out_again(self):
        real, calls = importer._place, []

        def failing(src, dst, is_dir):
            calls.append(os.path.basename(dst))
            if len(calls) == 2:
                raise OSError(errno.EIO, "Input/output error", dst)
            return real(src, dst, is_dir)

        with mock.patch.object(importer, "_place", failing):
            self.refused(self.source(CODEX_SEGMENTED))
        self.assertEqual(calls, [SEGMENT_BASE, SEGMENT_LAST])
        self.assert_nothing_written()


class CodexRewriteTest(unittest.TestCase):
    def rewrite(self, data: bytes, table=None):
        stub = StubMap(table or {"C:\\w": "/mnt/w", "D:\\x y": "/mnt/x y"})
        # A stand-in with to_local only, and no mount: all the rewrite uses.
        rewrite = _CwdRewrite(stub, None)  # type: ignore[arg-type]
        import io

        out = b"".join(rewrite.apply(io.BytesIO(data)))
        return out, rewrite.changes

    @staticmethod
    def line(kind, payload, ending=b"\n", **extra) -> bytes:
        record = {
            "timestamp": "2026-08-29T08:27:23.048Z",
            "ordinal": 0,
            "type": kind,
            "payload": payload,
            **extra,
        }
        return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + ending

    def test_only_the_named_keys_of_the_two_record_types(self):
        untouched = [
            self.line("response_item", {"cwd": "C:\\w", "text": '"session_meta" C:\\w'}),
            self.line(
                "event_msg",
                {"type": "thread_settings_applied", "thread_settings": {"cwd": "C:\\w"}},
            ),
            self.line("turn_context", {"cwd": "E:\\not mapped", "workspace_roots": []}),
            b'not json at all "turn_context"\n',
            b"\n",
            b'["turn_context"]\n',
            self.line("turn_context", "payload is a string"),
        ]
        meta = self.line(
            "session_meta",
            {
                "id": "t",
                "cwd": "C:\\w",
                "note": "café 😀 \u2028",
                "n": 1.5,
                "big": 2**60,
                "runtime_workspace_roots": ["C:\\w", "E:\\gone", 7, "D:\\x y"],
                "nested": {"cwd": "C:\\w"},
                "base_instructions": {"text": "C:\\w"},
            },
        )
        turn = self.line(
            "turn_context",
            {"turn_id": "1", "cwd": "D:\\x y", "workspace_roots": ["D:\\x y"], "model": "m"},
            ending=b"\r\n",
        )
        tail = self.line("turn_context", {"cwd": "C:\\w"}, ending=b"")  # no final newline
        out, changes = self.rewrite(meta + b"".join(untouched) + turn + tail)
        lines = out.split(b"\n")
        self.assertEqual(b"\n".join(lines[1:8]) + b"\n", b"".join(untouched))
        new_meta = json.loads(lines[0])["payload"]
        self.assertEqual(
            new_meta,
            {
                "id": "t",
                "cwd": "/mnt/w",
                "note": "café 😀 \u2028",
                "n": 1.5,
                "big": 2**60,
                "runtime_workspace_roots": ["/mnt/w", "E:\\gone", 7, "/mnt/x y"],
                "nested": {"cwd": "C:\\w"},
                "base_instructions": {"text": "C:\\w"},
            },
        )
        self.assertIn("café 😀".encode(), lines[0])  # not \\u-escaped
        self.assertTrue(lines[8].endswith(b"\r"))
        self.assertEqual(
            json.loads(lines[8])["payload"],
            {"turn_id": "1", "cwd": "/mnt/x y", "workspace_roots": ["/mnt/x y"], "model": "m"},
        )
        self.assertEqual(json.loads(lines[9])["payload"], {"cwd": "/mnt/w"})
        self.assertFalse(out.endswith(b"\n"))
        self.assertEqual(
            changes,
            [
                "session_meta payload.cwd: C:\\w -> /mnt/w",
                "session_meta payload.runtime_workspace_roots[]: C:\\w -> /mnt/w",
                "session_meta payload.runtime_workspace_roots[]: D:\\x y -> /mnt/x y",
                "turn_context payload.cwd: C:\\w -> /mnt/w",
                "turn_context payload.cwd: D:\\x y -> /mnt/x y",
                "turn_context payload.workspace_roots[]: D:\\x y -> /mnt/x y",
            ],
        )

    def test_nothing_mapped_means_byte_identical(self):
        data = self.line("session_meta", {"cwd": "/already/local"}) + self.line(
            "turn_context", {"cwd": "/already/local"}
        )
        # Spaced out as no tool writes it: still left alone when nothing maps.
        data += b'{ "type": "turn_context", "payload": { "cwd": "Q:\\\\q" } }\n'
        out, changes = self.rewrite(data)
        self.assertEqual((out, changes), (data, []))

    def test_repeated_changes_are_counted(self):
        data = self.line("turn_context", {"cwd": "C:\\w"}) * 3
        _out, changes = self.rewrite(data)
        self.assertEqual(changes, ["turn_context payload.cwd: C:\\w -> /mnt/w (3 records)"])

    def test_only_changed(self):
        before = {"a": 1, "payload": {"cwd": "x", "roots": ["x"], "keep": [1, {"cwd": "x"}]}}
        after = {"a": 1, "payload": {"cwd": "y", "roots": ["y", "z"], "keep": [1, {"cwd": "x"}]}}
        allowed = [("payload", "cwd"), ("payload", "roots")]
        self.assertTrue(importer.only_changed(before, after, allowed))
        self.assertFalse(importer.only_changed(before, after, [("payload", "cwd")]))
        self.assertFalse(importer.only_changed(before, dict(after, a=2), allowed))
        self.assertFalse(
            importer.only_changed(
                before, {"a": 1, "payload": dict(after["payload"], extra=1)}, allowed
            )
        )
        self.assertTrue(importer.only_changed({"k": float("nan")}, {"k": float("nan")}, []))


# --- Grok -------------------------------------------------------------------


class GrokImportTest(ImportCase):
    foreign = ("grok",)

    def setUp(self):
        super().setUp()
        self.group = urllib.parse.quote(str(self.w.site), safe="")
        self.original = self.w.profile / ".grok" / "sessions" / "F%3A%5CProj%5CMy%20Site" / GROK_WIN
        self.copy = self.home / ".grok" / "sessions" / self.group / GROK_WIN

    def assert_copied(self, copy: Path, cwd: str, compact: bool):
        """The copy holds every file but the locks, byte for byte, and a
        ``summary.json`` that differs in ``info.cwd`` and ``grok_home`` only."""
        expected = [rel for rel in files_below(self.original) if not rel.endswith(".lock")]
        self.assertLess(len(expected), len(files_below(self.original)), "fixture has locks")
        self.assertEqual(files_below(copy), expected)
        for rel in expected:
            if rel != "summary.json":
                self.assertEqual(read(copy / rel), read(self.original / rel), rel)
                self.assertEqual(
                    os.stat(copy / rel).st_mtime_ns, os.stat(self.original / rel).st_mtime_ns
                )
        before = json.loads(read(self.original / "summary.json"))
        raw = read(copy / "summary.json")
        after = json.loads(raw)
        self.assertEqual(list(before), list(after))
        self.assertEqual(
            (before["info"]["cwd"], before["grok_home"]),
            ("F:\\Proj\\My Site", "C:\\Users\\user\\.grok"),
        )
        self.assertEqual(
            (after["info"], after["grok_home"]),
            ({"id": GROK_WIN, "cwd": cwd}, str(self.home / ".grok")),
        )
        for key in before:
            if key not in ("info", "grok_home"):
                self.assertEqual(after[key], before[key], key)
        if compact:
            self.assertEqual(
                raw, json.dumps(after, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )

    def test_import_into_the_group_the_tool_itself_would_use(self):
        # The temp directory of this test has '-' or '_' in its path: kept as
        # they are in the group name, the rest percent-encoded.
        self.assertRegex(str(self.w.site), r"[-._~]")
        self.assertRegex(self.group, r"^%2F[A-Za-z0-9%._~-]+%2FProj%2FMy%20Site$")
        self.assertEqual(grok_mod.group_name(str(self.w.site)), self.group)
        age_access_times(self.w.c)
        before = tree_state(self.w.c)
        source = self.source(GROK_WIN)
        self.assertEqual(api.can_import(source), (True, ""))
        plan = api.import_plan(source)
        self.assertEqual([(c["dir"], c["to"]) for c in plan["copies"]], [(True, str(self.copy))])
        self.assertEqual(
            plan["rewrites"],
            [
                {
                    "file": str(self.copy / "summary.json"),
                    "what": "info.cwd and grok_home in summary.json",
                    "changes": [
                        f"info.cwd: F:\\Proj\\My Site -> {self.w.site}",
                        f"grok_home: C:\\Users\\user\\.grok -> {self.home / '.grok'}",
                    ],
                }
            ],
        )
        self.assert_nothing_written()

        native = api.import_session(source)

        self.assertEqual(tree_state(self.w.c), before, "source times and sizes")
        self.assert_copied(self.copy, str(self.w.site), compact=True)
        self.assertEqual(os.listdir(self.home / ".grok"), ["sessions"])
        self.assertEqual(os.listdir(self.home / ".grok" / "sessions"), [self.group])
        self.assertEqual(
            (native.origin, native.launchable, native.id, native.files),
            ("native", True, GROK_WIN, [str(self.copy)]),
        )
        self.assertEqual((native.cwd, native.cwd_local), (str(self.w.site), str(self.w.site)))
        self.assertEqual((native.title, native.messages), (source.title, source.messages))
        self.assertEqual(self.source(GROK_WIN).imported_as, native.key)
        (record,) = self.records()
        self.assertEqual(record["destination_files"], [str(self.copy)])
        self.assertEqual(list(record["rewritten"]), [str(self.copy / "summary.json")])
        # Again: the existing copy, nothing new.
        report = api.import_report(self.source(GROK_WIN))
        self.assertEqual((report.created, report.session), (False, native))
        self.assertEqual(len(self.records()), 1)

    def test_a_group_directory_that_is_there_already_is_used(self):
        group = self.home / ".grok" / "sessions" / self.group
        group.mkdir(parents=True)
        (group / "prompt_history.jsonl").write_bytes(b"{}\n")
        self.w.scan()
        native = api.import_session(self.source(GROK_WIN))
        self.assertEqual(native.files, [str(self.copy)])
        self.assertEqual(sorted(os.listdir(group)), [GROK_WIN, "prompt_history.jsonl"])
        self.assertEqual(read(group / "prompt_history.jsonl"), b"{}\n")
        # The group was not made by the import, so its record does not list it.
        (record,) = self.records()
        self.assertNotIn(str(group), record["manifest"]["dirs"])
        self.assertIn(str(self.copy), record["manifest"]["dirs"])

    def test_plain_folder_gets_the_percent_encoded_group(self):
        source = self.source(GROK_WIN)
        plain = "/Plain Dir/My Site 2"
        native = Grok().import_session(source, StubMap({source.cwd: plain}))
        copy = self.home / ".grok" / "sessions" / "%2FPlain%20Dir%2FMy%20Site%202" / GROK_WIN
        self.assert_copied(copy, plain, compact=True)
        self.assertEqual((native.id, native.cwd, native.origin), (GROK_WIN, plain, "native"))
        self.assertEqual(stat.S_IMODE(os.stat(copy).st_mode), 0o700)

    def planned_group(self, folder):
        """The group directory the adapter plans for a mapped ``folder``."""
        source = self.source(GROK_WIN)
        plan = Grok().import_plan(source, StubMap({source.cwd: folder}))
        group = os.path.basename(os.path.dirname(plan.units[0].dst))
        self.assertEqual(plan.units[0].dst, str(self.copy.parent.parent / group / GROK_WIN))
        return group

    def test_cwd_shapes(self):
        """FORMATS "Phase 2 findings", Q1: the names the tool was seen to list."""
        for folder, group in (
            ("/a/B c/9", "%2Fa%2FB%20c%2F9"),
            ("/", "%2F"),
            ("/a/b-c", "%2Fa%2Fb-c"),  # - . _ ~ are kept
            ("/a/b.c", "%2Fa%2Fb.c"),
            ("/a/b_c", "%2Fa%2Fb_c"),
            ("/a/~b", "%2Fa%2F~b"),
            ("/a/caf\u00e9", "%2Fa%2Fcaf%C3%A9"),  # UTF-8 bytes, upper-case hex
            ("/a/u\u0308", "%2Fa%2Fu%CC%88"),  # no Unicode normalisation
            ("/a/\u6f22\U0001f600", "%2Fa%2F%E6%BC%A2%F0%9F%98%80"),
            ("/a/(x)", "%2Fa%2F%28x%29"),
            ("/a/b%20c", "%2Fa%2Fb%2520c"),
            ("/a/b+c", "%2Fa%2Fb%2Bc"),  # a space is %20, never +
            ("/a\\b", "%2Fa%5Cb"),
            ("/a:b", "%2Fa%3Ab"),
            (
                "/a/!'*,=@&$#[]{};^`|\"<>?",
                "%2Fa%2F%21%27%2A%2C%3D%40%26%24%23%5B%5D%7B%7D%3B%5E%60%7C%22%3C%3E%3F",
            ),
            (
                "/tmp/ags/work/my-proj_v1.2~x y/\u00dcn\u00ef",  # the sandbox's own example
                "%2Ftmp%2Fags%2Fwork%2Fmy-proj_v1.2~x%20y%2F%C3%9Cn%C3%AF",
            ),
        ):
            with self.subTest(folder=folder):
                self.assertEqual(grok_mod.group_name(folder), group)
                self.assertEqual(self.planned_group(folder), group)
                self.assertEqual(urllib.parse.unquote(group), folder)
        self.assert_nothing_written()

    def test_a_name_over_255_bytes_is_a_slug_and_a_hash(self):
        base = "/" + "d" * 84 + "/" + "e" * 84 + "/"
        self.assertEqual(grok_mod.GROUP_NAME_MAX, 255)
        at_limit = base + "f" * 78
        self.assertEqual(len(urllib.parse.quote(at_limit, safe="")), 255)
        self.assertEqual(
            self.planned_group(at_limit), "%2F" + "d" * 84 + "%2F" + "e" * 84 + "%2F" + "f" * 78
        )
        over = base + "f" * 79  # one byte more
        self.assertEqual(self.planned_group(over), "f" * 40 + "-562223fd3889f191")
        # Bytes are counted, not characters: 29 times "é" is 174 bytes as %XX.
        accented = "/" + "d" * 80 + "/" + "\u00e9" * 29
        self.assertEqual(len(accented), 111)
        self.assertEqual(
            self.planned_group(accented),
            "workspace-" + blake3.hexdigest(accented.encode("utf-8"))[:16],
        )
        long_name = "/" + "x" * 250 + "/My Proj.v2 (final)"
        self.assertRegex(self.planned_group(long_name), r"^my-proj-v2-final-[0-9a-f]{16}$")
        self.assert_nothing_written()

    def test_the_slug_of_a_long_name(self):
        """The examples of FORMATS Q1, each named by the rule before the tool
        was asked."""
        for name, slug in (
            ("MyProj", "myproj"),
            ("my proj.v1_2~x", "my-proj-v1-2-x"),
            ("\u00dcn\u00ef K\u00f6ln", "n-k-ln"),
            ("a  b__c", "a-b-c"),
            (".hidden", "hidden"),
            ("name.", "name"),
            ("--x--", "x"),
            ("p" * 59, "p" * 40),
            ("x" * 39 + "\u00dctail", "x" * 39 + "-"),  # cut after trimming, not trimmed again
            ("a" + " " * 45 + "b" * 30, "a-" + "b" * 30),  # runs collapse before the cut
            ("\u6f22\u5b57", "workspace"),
            ("---", "workspace"),
        ):
            with self.subTest(name=name):
                self.assertEqual(grok_mod.group_slug("/some/where/" + name), slug)
        self.assertEqual(grok_mod.group_slug("/"), "workspace")

    def test_folders_whose_group_name_is_not_known_are_refused(self):
        source = self.source(GROK_WIN)
        too_long = "/" + "/".join(["s" * 200] * 5) + "/" + "t" * 19  # 1,025 bytes
        self.assertEqual(len(too_long.encode()), 1025)
        for folder, needle in (
            (too_long, "1,025 bytes long"),
            ("/a/not\udcff-utf8", "is not valid UTF-8"),
        ):
            with self.subTest(needle=needle):
                with self.assertRaises(NotSupported) as caught:
                    grok_mod.group_name(folder)
                self.assertIn(needle, caught.exception.reason)
                with self.assertRaises(NotSupported) as planned:
                    Grok().import_plan(source, StubMap({source.cwd: folder}))
                self.assertEqual(planned.exception.reason, caught.exception.reason)
        with self.assertRaises(NotSupported) as caught:
            grok_mod.group_name("")
        self.assertIn("has no name", caught.exception.reason)
        # One byte less, the most the hash takes, is still named.
        self.assertRegex(grok_mod.group_name(too_long[:-1]), r"^t{18}-[0-9a-f]{16}$")
        self.assert_nothing_written()

    def test_a_group_that_only_decodes_to_the_folder_is_not_used(self):
        """docs/AUDIT.md A-24: the tool lists a folder's sessions from one
        name only (FORMATS Q1). A directory that merely decodes to the
        folder (lower-case hex, an escaped ``-``) is not that name, and a
        session filed there would not be listed."""
        source = self.source(GROK_WIN)
        sessions = self.home / ".grok" / "sessions"
        for other in ("%2Fwith%2Dhyphen", "%2fwith-hyphen"):
            (sessions / other).mkdir(parents=True)  # another spelling of the folder
            self.assertEqual(urllib.parse.unquote(other), "/with-hyphen")
        plan = Grok().import_plan(source, StubMap({source.cwd: "/with-hyphen"}))
        self.assertEqual(plan.units[0].dst, str(sessions / "%2Fwith-hyphen" / GROK_WIN))
        # The same once the tool's own name is there too.
        (sessions / "%2Fwith-hyphen").mkdir()
        plan = Grok().import_plan(source, StubMap({source.cwd: "/with-hyphen"}))
        self.assertEqual(plan.units[0].dst, str(sessions / "%2Fwith-hyphen" / GROK_WIN))

    def test_a_summary_that_starts_with_a_byte_order_mark_is_imported(self):
        """docs/AUDIT.md A-24: such a session was listed and could not be
        imported. The copy's summary is the same JSON without the mark."""
        summary = self.original / "summary.json"
        plain = read(summary)
        summary.write_bytes(b"\xef\xbb\xbf" + plain)
        self.w.scan()
        source = self.source(GROK_WIN)
        self.assertEqual(source.id, GROK_WIN)  # listed, as before
        planned = api.import_plan(source)
        self.assertIn(
            "the byte order mark at the start is left out", planned["rewrites"][0]["changes"]
        )
        native = api.import_session(source)
        self.assertEqual(native.id, GROK_WIN)
        copied = read(self.copy / "summary.json")
        self.assertFalse(copied.startswith(b"\xef\xbb\xbf"))
        after, before = json.loads(copied), json.loads(plain)
        self.assertEqual(after["info"]["cwd"], str(self.w.site))
        for key in before:
            if key not in ("info", "grok_home"):
                self.assertEqual(after[key], before[key], key)
        # A summary in the tool's own layout keeps it, mark or no mark.
        pretty = json.dumps(before, ensure_ascii=False, indent=2).encode("utf-8")
        rewrite = _SummaryRewrite("/c", "/g")
        for mark in (b"", b"\xef\xbb\xbf"):
            with open(self.w.box.root / "summary.json", "w+b") as fh:
                fh.write(mark + pretty)
                fh.seek(0)
                out = b"".join(rewrite.apply(fh))
            self.assertTrue(out.startswith(b'{\n  "'), out[:20])
            self.assertEqual(json.loads(out)["info"]["cwd"], "/c")

    def test_an_id_in_any_group_is_a_collision(self):
        taken = self.home / ".grok" / "sessions" / "%2Felsewhere" / GROK_WIN
        taken.mkdir(parents=True)
        (taken / "mine.txt").write_text("keep", encoding="utf-8")
        (self.home / ".grok" / "sessions" / self.group).mkdir()
        self.w.scan()
        reason = self.refused(self.source(GROK_WIN))
        self.assertIn(str(taken), reason)
        self.assertEqual(
            files_below(self.home / ".grok"), [f"sessions/%2Felsewhere/{GROK_WIN}/mine.txt"]
        )

    def test_failure_leaves_no_partial_session_directory(self):
        (self.home / ".grok" / "sessions" / self.group).mkdir(parents=True)
        self.w.scan()
        real, calls = importer._copy, []

        def failing(item, target, tick):
            calls.append(item.rel)
            if item.rel == "summary.json":  # the last file of the directory
                raise OSError(errno.EIO, "Input/output error", item.src)
            return real(item, target, tick)

        with mock.patch.object(importer, "_copy", failing):
            self.refused(self.source(GROK_WIN))
        self.assertEqual(calls[-1], "summary.json")
        self.assertGreater(len(calls), 3)
        self.assertEqual(files_below(self.home / ".grok"), [])
        self.assertEqual(os.listdir(self.home / ".grok"), ["sessions"])
        self.assertEqual(os.listdir(self.home / ".grok" / "sessions"), [self.group])
        self.assertFalse(self.record_file.exists())

    def test_summary_keeps_the_tools_own_layout(self):
        import io

        data = {
            "info": {"id": "i", "cwd": "F:\\P"},
            "session_summary": "Révision ✓",
            "grok_home": "C:\\Users\\u\\.grok",
            "num_messages": 3,
            "last_recap": None,
        }
        pretty = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")  # as Grok writes
        rewrite = _SummaryRewrite("/mnt/P", "/home/u/.grok")
        out = b"".join(rewrite.apply(io.BytesIO(pretty)))
        old_lines, new_lines = pretty.split(b"\n"), out.split(b"\n")
        self.assertEqual(len(old_lines), len(new_lines))
        self.assertEqual(
            [
                n
                for n, pair in enumerate(zip(old_lines, new_lines, strict=False))
                if pair[0] != pair[1]
            ],
            [3, 6],
        )
        self.assertEqual(
            json.loads(out),
            dict(data, info={"id": "i", "cwd": "/mnt/P"}, grok_home="/home/u/.grok"),
        )
        self.assertIn("Révision ✓".encode(), out)
        self.assertEqual(
            rewrite.changes,
            ["info.cwd: F:\\P -> /mnt/P", "grok_home: C:\\Users\\u\\.grok -> /home/u/.grok"],
        )
        # Any other layout: compact JSON. Keys that are absent are not added.
        loose = b'{"info": {"id": "i"}, "x": 1}\n'
        self.assertEqual(
            b"".join(_SummaryRewrite("/mnt/P", "/h").apply(io.BytesIO(loose))),
            b'{"info":{"id":"i"},"x":1}',
        )
        for bad in (b"", b"[1]", b"{ nope"):
            with self.assertRaises(api.ImportFailed):
                b"".join(_SummaryRewrite("/mnt/P", "/h").apply(io.BytesIO(bad)))


# --- providers that cannot be imported --------------------------------------


class NotSupportedTest(ImportCase):
    def test_antigravity_and_gemini_state_the_documented_reason(self):
        """Antigravity as it is asked for by default: more, and the
        experimental import itself, in tests/test_import_antigravity.py."""
        for session_id, reason in (
            (AGY_WIN, antigravity.EXPERIMENTAL_ONLY),
            (AGY_NO_CWD, antigravity.EXPERIMENTAL_ONLY),
            (GEMINI_WIN, gemini.IMPORT_UNSAFE),
        ):
            source = self.source(session_id)
            self.assertEqual(self.refused(source), reason)
            self.assertEqual(self.refused(source.key), reason)
            self.assertEqual(api.can_import(source), (False, reason))
            with self.assertRaises(api.ImportFailed) as caught:
                api.import_plan(source)
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(
            antigravity.EXPERIMENTAL_ONLY,
            "importing an Antigravity conversation is experimental and is done only when "
            "asked for: no imported copy has been resumed by hand yet, and agy starts a new "
            "conversation without an error when it cannot read one",
        )
        self.assertEqual(
            gemini.IMPORT_UNSAFE,
            "Gemini CLI is not installed on this machine, and no resumable Gemini session "
            "exists to import",
        )
        self.assert_nothing_written()

    def test_can_import_over_every_foreign_session(self):
        verdicts = {
            s.provider: api.can_import(s, self.index)[0]
            for s in self.index.sessions
            if s.origin == "foreign"
        }
        self.assertEqual(
            verdicts,
            {"antigravity": False, "claude": True, "codex": True, "gemini": False, "grok": True},
        )
        self.assert_nothing_written()

    def test_unknown_reference(self):
        with self.assertRaises(api.NotFound):
            api.import_session("claude:nope")
        self.assertEqual(api.can_import("claude:nope")[0], False)


def dead_pid() -> int:
    """A process id nothing has: above what this system gives out."""
    try:
        limit = int(Path("/proc/sys/kernel/pid_max").read_text())
    except (OSError, ValueError):
        limit = 2**22
    return limit + 1


class StagingSweepTest(ImportCase):
    foreign = ("claude",)
    HOUR = 3600

    def make(self, name, age, is_dir=True) -> Path:
        path = self.home / ".claude" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_dir:
            (path / "0").mkdir(parents=True)
            (path / "0" / "part.jsonl").write_text("half", encoding="utf-8")
        else:
            path.write_text("x", encoding="utf-8")
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def leftovers(self):
        prefix = importer.STAGING_PREFIX
        dead, live = dead_pid(), os.getpid()
        self.assertFalse(importer._pid_alive(dead))
        self.assertTrue(importer._pid_alive(live))
        stale = [
            self.make(f"{prefix}{dead}-18a3f0", 2 * self.HOUR),
            self.make(f"{prefix}{dead}-0", 30 * 24 * self.HOUR),
        ]
        kept = [
            self.make(f"{prefix}{dead}-18a3f1", self.HOUR - 120),  # fresh
            self.make(f"{prefix}{live}-18a3f2", 2 * self.HOUR),  # its process lives
            self.make(f"{prefix}{dead}-18a3f3", 2 * self.HOUR, is_dir=False),  # a file
            self.make(f"{prefix}{dead}", 2 * self.HOUR),  # not the pattern
            self.make(f"{prefix}{dead}-18A3F4", 2 * self.HOUR),
            self.make(f"{prefix}{dead}-18a3f5.bak", 2 * self.HOUR),
            self.make(f"{prefix}abc-18a3f6", 2 * self.HOUR),
            self.make(f"x{prefix}{dead}-18a3f7", 2 * self.HOUR),
            self.make(f"{prefix[1:]}{dead}-18a3f8", 2 * self.HOUR),
            self.make("projects", 2 * self.HOUR),
            self.make("sessions", 2 * self.HOUR),
        ]
        link = self.home / ".claude" / f"{prefix}{dead}-18a3f9"  # a link to a directory
        os.symlink(kept[-1], link)
        return stale, kept + [link]

    def test_sweep_removes_only_dead_old_staging_directories(self):
        stale, kept = self.leftovers()
        removed = importer.sweep_staging(self.home / ".claude")
        self.assertEqual(removed, sorted(str(p) for p in stale))
        self.assertEqual([p for p in stale if os.path.lexists(p)], [])
        self.assertEqual([p for p in kept if not os.path.lexists(p)], [])
        self.assertEqual(importer.sweep_staging(self.home / ".claude"), [])
        self.assertEqual(importer.sweep_staging(self.home / "no such store"), [])

    def test_an_import_sweeps_and_a_dry_run_does_not(self):
        stale, kept = self.leftovers()
        api.import_plan(CLAUDE_WIN, index=self.index)  # `import --dry-run`
        self.assertEqual(api.can_import(CLAUDE_WIN, index=self.index), (True, ""))
        self.assertEqual([p for p in stale + kept if not os.path.lexists(p)], [])
        copy = api.import_session(CLAUDE_WIN, index=self.index)
        self.assertEqual(copy.origin, "native")
        self.assertEqual([p for p in stale if os.path.lexists(p)], [])
        self.assertEqual([p for p in kept if not os.path.lexists(p)], [])

    @unittest.skipIf(os.geteuid() == 0, "root can remove everything")
    def test_a_staging_directory_that_cannot_be_removed_is_ignored(self):
        stuck = self.make(f"{importer.STAGING_PREFIX}{dead_pid()}-1", 2 * self.HOUR)
        os.chmod(stuck / "0", 0o500)
        self.addCleanup(os.chmod, stuck / "0", 0o700)
        then = time.time() - 2 * self.HOUR
        os.utime(stuck, (then, then))
        self.assertEqual(importer.sweep_staging(self.home / ".claude"), [])
        copy = api.import_session(CLAUDE_WIN, index=self.index)  # and the import goes on
        self.assertEqual(copy.origin, "native")
        self.assertTrue(stuck.exists())


class HelpersTest(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(
            [importer.human_size(n) for n in (0, 999, 1000, 116701, 123697588, 5 * 10**12)],
            ["0 B", "999 B", "1.0 kB", "116.7 kB", "123.7 MB", "5000.0 GB"],
        )

    def test_tree_lists_without_locks_and_in_order(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("b/c/deep.txt", "b/x.lock", "a.txt", "z.lock", "empty/"):
                path = root / rel
                if rel.endswith("/"):
                    path.mkdir(parents=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(rel, encoding="utf-8")
            dirs, files = importer.tree(root, skip=lambda name: name.endswith(".lock"))
            self.assertEqual(dirs, ["b", os.path.join("b", "c"), "empty"])
            self.assertEqual(
                [(f.rel, f.size) for f in files],
                [("a.txt", 5), (os.path.join("b", "c", "deep.txt"), 12)],
            )
            with self.assertRaises(api.ImportFailed):
                importer.tree(root / "missing")
            with self.assertRaises(api.ImportFailed):
                importer.file_copy(root / "b")


if __name__ == "__main__":
    unittest.main()
