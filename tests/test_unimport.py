"""unimport.py and api.unimport: taking an import back (docs/SPEC.md
"Import").

The property that matters is held for every provider that can import, on
the fixtures: import, then unimport, and the fake home is byte for byte
what it was. The rest is what an undo refuses, what it leaves and says so,
and that a journal record cannot send it outside the native store. The
machine is the one of tests/fixture_world.py."""

import contextlib
import copy
import io
import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CLAUDE_WIN_PLAIN,
    CODEX_SEGMENTED,
    CODEX_WIN,
    GROK_WIN,
    World,
    files_below,
)
from support import FIXTURES, LOG_NAME, finish, launcher_child, python_child, state_names

from agent_sessions import api, cli, fsutil, importer, journal, logs, unimport
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.claude import Claude, encode_cwd
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok

POSIX = os.name == "posix"
HOLDER = """
from agent_sessions import fsutil
with fsutil.lock():
    print("held", flush=True)
    sys.stdin.read()
"""


class Terminal(io.StringIO):
    """A text stream that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def picture(root, leave_out=()) -> dict:
    """Everything below ``root``, by relative path: a directory with its
    mode, a regular file with its mode, size and bytes, anything else with
    its kind (a link with where it points). Names that start with one of
    ``leave_out`` are not looked at."""
    found: dict[str, tuple] = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            if name.startswith(tuple(leave_out)):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            st = os.lstat(path)
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                found[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                found[rel] = ("dir", mode)
            elif stat.S_ISREG(st.st_mode):
                with open(path, "rb") as fh:
                    data = fh.read()
                found[rel] = ("file", mode, st.st_size, data)
            else:
                found[rel] = ("other", stat.S_IFMT(st.st_mode))
    return found


def read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class UndoCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("claude",)
    native: tuple[str, ...] = ()

    def setUp(self):
        self.w = World(self, self.foreign, self.native)
        self.home = self.w.home
        self.state = Path(os.environ["AGENT_SESSIONS_STATE"])
        self.index = self.w.scan()

    def source(self, session_id=CLAUDE_WIN):
        return self.w.session(session_id, "foreign")

    def records(self) -> list[dict]:
        """The import and undo records of the journal. The ``begin`` line
        that every import starts with is left out."""
        return [record for record in journal.read() if record.get("record") != "begin"]

    def rewrite_journal(self, change):
        """Change the last record of the journal by hand, as a damaged or a
        made-up journal would be."""
        records = self.records()
        change(records[-1])
        with open(journal.path(), "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def refused(self, ref, **kwargs) -> str:
        """The undo is refused, for a dry run too, and nothing changes: not
        in the home, not in the journal."""
        before = picture(self.w.box.root, (LOG_NAME,))
        reasons = []
        for dry_run in (True, False):
            with self.assertRaises(api.UndoFailed) as caught:
                api.unimport(ref, dry_run=dry_run, **kwargs)
            reasons.append(caught.exception.reason)
        self.assertEqual(len(set(reasons)), 1, reasons)
        self.assertEqual(picture(self.w.box.root, (LOG_NAME,)), before)
        return reasons[0]


# --- import, unimport: the store is what it was --------------------------------


class RoundTripTest(UndoCase):
    """For every provider that can import, with the native store absent and
    with one the tool has used."""

    foreign = ("antigravity", "claude", "codex", "grok")

    def round_trips(self, provider, native):
        if native:
            shutil.copytree(FIXTURES / provider / "native", self.home, dirs_exist_ok=True)
            self.w.scan()
        home_before = picture(self.home)
        state_before = picture(self.state, (LOG_NAME,))
        self.assertEqual(state_before, {})
        sources = [
            s for s in api.load_index().sessions if s.origin == "foreign" and s.provider == provider
        ]
        self.assertTrue(sources)
        done = 0
        for source in sources:
            options = api.import_options(source)
            if not (options.possible or options.experimental):
                continue  # its folder does not exist on this machine
            with self.subTest(session=source.id):
                native_copy = api.import_session(source, experimental=options.experimental)
                self.assertNotEqual(picture(self.home), home_before)
                self.assertEqual(self.w.session(source.id, "foreign").imported_as, native_copy.key)

                report = api.unimport(source.key)

                self.assertEqual(picture(self.home), home_before, "the home is what it was")
                self.assertEqual((report.left, report.differences), ([], []))
                self.assertFalse(report.forced or report.dry_run)
                done += 1
                # The state directory: the journal's two lines more, and the log.
                self.assertEqual(state_names(), ["imports.jsonl"])
                self.assertEqual(stat.S_IMODE(os.stat(journal.path()).st_mode), 0o600)
                kinds = [(r["record"], r["id"]) for r in self.records()]
                self.assertEqual(kinds[-2:], [("import", source.id), ("undo", source.id)])
                self.assertEqual(len(kinds), 2 * done)
                # And the listing: not imported any more, no native copy.
                index = api.load_index()
                self.assertIsNone(self.w.session(source.id, "foreign", index).imported_as)
                self.assertEqual(
                    [s for s in index.sessions if s.id == source.id and s.origin == "native"], []
                )
        return done

    def test_claude_into_a_home_without_the_store(self):
        self.assertGreaterEqual(self.round_trips("claude", native=False), 2)

    def test_claude_into_a_store_the_tool_has_used(self):
        self.assertGreaterEqual(self.round_trips("claude", native=True), 2)

    def test_codex_into_a_home_without_the_store(self):
        self.assertEqual(self.round_trips("codex", native=False), 1)

    def test_codex_into_a_store_the_tool_has_used(self):
        self.assertEqual(self.round_trips("codex", native=True), 1)

    def test_grok_into_a_home_without_the_store(self):
        self.assertEqual(self.round_trips("grok", native=False), 1)

    def test_grok_into_a_store_the_tool_has_used(self):
        self.assertEqual(self.round_trips("grok", native=True), 1)

    def test_antigravity_into_a_home_without_the_store(self):
        self.assertEqual(self.round_trips("antigravity", native=False), 3)

    def test_antigravity_into_a_store_the_tool_has_used(self):
        self.assertEqual(self.round_trips("antigravity", native=True), 3)

    def test_an_import_into_a_chosen_folder_goes_the_same_way(self):
        chosen = self.home / "Work"
        chosen.mkdir()
        home_before = picture(self.home)
        for session_id in (CLAUDE_WIN, CODEX_WIN, GROK_WIN):
            api.import_session(self.source(session_id), target=str(chosen))
            report = api.unimport(session_id)
            self.assertEqual(report.left, [])
            self.assertEqual(picture(self.home), home_before)

    def test_it_can_be_imported_and_undone_again(self):
        home_before = picture(self.home)
        for _round in range(3):
            native = api.import_session(self.source())
            api.unimport(native.key)
            self.assertEqual(picture(self.home), home_before)
        self.assertEqual([r["record"] for r in self.records()], ["import", "undo"] * 3)
        self.assertEqual(unimport.not_undone(self.records()), [])


class CodexIndexLineTest(UndoCase):
    """A thread with a name: the import adds a line to the tool's own
    ``session_index.jsonl``. That line is the one thing an undo leaves."""

    foreign = ()

    def setUp(self):
        super().setUp()
        old = self.w.box.mount("OldLinux")
        self.old_home = old / "home" / "user"
        shutil.copytree(FIXTURES / "codex" / "native" / ".codex", self.old_home / ".codex")
        self.w.override({"/home/user": str(self.old_home)})
        self.index = self.w.scan()
        self.index_file = self.home / ".codex" / "session_index.jsonl"
        self.line = (
            b'{"id":"01a0e538-c7bb-77dd-885b-0c4d54663241","thread_name":"title-168",'
            b'"updated_at":"2026-09-27T23:35:21.831252Z"}\n'
        )

    def test_the_store_differs_by_the_one_reported_line(self):
        self.index_file.parent.mkdir()
        mine = b'{"id":"mine","thread_name":"kept","updated_at":"z"}\n'
        self.index_file.write_bytes(mine)
        os.chmod(self.index_file, 0o640)
        home_before = picture(self.home)
        api.import_session(self.source(CODEX_SEGMENTED))
        self.assertEqual(self.records()[0]["appended"], [str(self.index_file)])

        report = api.unimport(CODEX_SEGMENTED)

        self.assertEqual(report.left, [(str(self.index_file), unimport.APPENDED)])
        self.assertEqual(len(report.removed), 2 + 4)  # two rollouts, sessions/YYYY/MM/DD
        after = picture(self.home)
        rel = os.path.relpath(self.index_file, self.home)
        self.assertEqual(after[rel], ("file", 0o640, len(mine + self.line), mine + self.line))
        self.assertEqual(
            {k: v for k, v in after.items() if k != rel},
            {k: v for k, v in home_before.items() if k != rel},
        )
        self.assertEqual(
            self.records()[1]["left"], [{"path": str(self.index_file), "why": unimport.APPENDED}]
        )

    def test_an_index_the_import_created_stays_with_its_line(self):
        home_before = picture(self.home)
        self.assertNotIn(".codex", home_before)
        api.import_session(self.source(CODEX_SEGMENTED))
        report = api.unimport(CODEX_SEGMENTED)
        # The file holds the line; the store's directory holds the file.
        self.assertEqual(
            report.left,
            [
                (str(self.home / ".codex"), unimport.NOT_EMPTY),
                (str(self.index_file), unimport.APPENDED),
            ],
        )
        after = picture(self.home)
        self.assertEqual(
            {k: v for k, v in after.items() if k not in home_before},
            {
                ".codex": ("dir", 0o700),
                ".codex/session_index.jsonl": ("file", 0o600, len(self.line), self.line),
            },
        )
        self.assertEqual({k: v for k, v in after.items() if k in home_before}, home_before)


# --- which record ---------------------------------------------------------------


class WhichRecordTest(UndoCase):
    def test_the_native_key_the_foreign_key_and_the_id_all_name_it(self):
        source = self.source()
        native = api.import_session(source)
        plans = [
            api.unimport(ref, dry_run=True)
            for ref in (native.key, source.key, CLAUDE_WIN, native, self.source(), native.key[:25])
        ]
        for planned in plans:
            self.assertEqual(planned, plans[0])
        self.assertEqual(
            (plans[0].provider, plans[0].id, plans[0].native_key, plans[0].source_key),
            ("claude", CLAUDE_WIN, native.key, source.key),
        )
        self.assertTrue(plans[0].dry_run)
        self.assertEqual(len(self.records()), 1)  # a dry run is not recorded

    def test_the_last_import_that_was_not_undone(self):
        api.import_session(self.source())
        api.unimport(CLAUDE_WIN)
        api.import_session(self.source(CLAUDE_WIN_PLAIN))
        api.import_session(self.source())
        records = self.records()
        self.assertEqual(
            [(r["record"], r["id"]) for r in records],
            [
                ("import", CLAUDE_WIN),
                ("undo", CLAUDE_WIN),
                ("import", CLAUDE_WIN_PLAIN),
                ("import", CLAUDE_WIN),
            ],
        )
        self.assertEqual(unimport.not_undone(records), [records[2], records[3]])
        self.assertIs(unimport.find_record(records, "claude", CLAUDE_WIN), records[3])
        self.assertIs(unimport.find_record(records, "claude", CLAUDE_WIN_PLAIN), records[2])
        api.unimport(CLAUDE_WIN)
        self.assertEqual(self.records()[-1]["imported_at"], records[3]["imported_at"])
        self.assertIsNone(unimport.find_record(self.records(), "claude", CLAUDE_WIN))
        self.assertEqual(self.refused(CLAUDE_WIN), unimport.NO_RECORD)
        # The other session's import still stands.
        api.unimport(CLAUDE_WIN_PLAIN)
        self.assertEqual(unimport.not_undone(self.records()), [])

    def test_records_are_settled_in_order(self):
        imp = lambda provider, ident, n: {  # noqa: E731
            "record": "import",
            "provider": provider,
            "id": ident,
            "n": n,
        }
        undo = lambda provider, ident: {"record": "undo", "provider": provider, "id": ident}  # noqa: E731
        records = [
            imp("claude", "a", 1),
            imp("codex", "a", 2),  # the same id under another provider
            imp("claude", "a", 3),
            undo("claude", "a"),  # takes back 3
            imp("claude", "b", 4),
            undo("grok", "a"),  # of nothing
            {"record": "undo", "provider": "claude"},  # no id
            {"record": "import", "provider": 7, "id": "a"},
            {"record": "other", "provider": "claude", "id": "a"},
            "not a record",
        ]
        standing = unimport.not_undone([r for r in records if isinstance(r, dict)])
        self.assertEqual([r["n"] for r in standing], [1, 2, 4])
        self.assertEqual(unimport.find_record(standing, "claude", "a")["n"], 1)
        self.assertEqual(unimport.find_record(standing, "codex", "a")["n"], 2)
        self.assertIsNone(unimport.find_record(standing, "grok", "a"))
        self.assertEqual(
            unimport.not_undone([undo("claude", "a"), imp("claude", "a", 9)]),
            [imp("claude", "a", 9)],
        )

    def test_no_import_is_recorded(self):
        # Never imported.
        self.assertEqual(self.refused(self.source()), unimport.NO_RECORD)
        self.assertEqual(self.refused(CLAUDE_WIN), unimport.NO_RECORD)
        self.assertFalse(self.state.joinpath("imports.jsonl").exists())
        # A session that was made on this machine.
        shutil.copytree(FIXTURES / "claude" / "native" / ".claude", self.home / ".claude")
        self.w.scan()
        self.assertEqual(self.refused(CLAUDE_NATIVE, force=True), unimport.NO_RECORD)
        # A reference that names nothing.
        for dry_run in (True, False):
            with self.assertRaises(api.NotFound) as caught:
                api.unimport("claude:nope", dry_run=dry_run)
            self.assertIn("no session matches 'claude:nope'", caught.exception.reason)

    def test_the_refusal_says_that_it_is_this_one_without_its_text(self):
        """What a front end reads: ``no_record``, in every place it is
        raised, and nothing that reads as a copy that was used."""
        for ref in (self.source(), CLAUDE_WIN):  # nothing recorded
            for dry_run in (True, False):
                with self.assertRaises(api.UndoFailed) as caught:
                    api.unimport(ref, dry_run=dry_run)
                self.assertIs(caught.exception.no_record, True)
                self.assertEqual(caught.exception.differences, [])
        api.import_session(self.source())
        api.unimport(CLAUDE_WIN)
        with self.assertRaises(api.UndoFailed) as caught:  # recorded, and undone since
            api.unimport(CLAUDE_WIN, dry_run=True)
        self.assertIs(caught.exception.no_record, True)

    def test_a_provider_without_a_native_store_has_no_record_either(self):
        source = self.source()
        with (
            mock.patch.object(Claude, "native_store", return_value=None),
            self.assertRaises(api.UndoFailed) as caught,
        ):
            api.unimport(source, dry_run=True)
        self.assertEqual(
            (caught.exception.reason, caught.exception.no_record), (unimport.NO_RECORD, True)
        )

    def test_any_other_refusal_is_marked_as_neither(self):
        plain = api.UndoFailed("the import record is damaged")
        self.assertEqual(plain.reason, "the import record is damaged")
        self.assertEqual((plain.no_record, plain.differences), (False, []))
        api.import_session(self.source())
        os.unlink(journal.path())
        journal.path().mkdir()
        with self.assertRaises(api.UndoFailed) as caught:
            api.unimport(CLAUDE_WIN, dry_run=True)
        self.assertIn("cannot be read", caught.exception.reason)
        self.assertEqual((caught.exception.no_record, caught.exception.differences), (False, []))

    def test_a_copy_whose_session_is_not_listed_any_more_is_found_in_the_journal(self):
        source = self.source()
        native = api.import_session(source)
        os.unlink(native.files[0])  # the transcript, removed by hand
        self.w.box.unmount(self.w.c)
        api.scan()
        api.forget_drive("WinC")
        self.assertEqual(api.load_index().sessions, [])
        with self.assertRaises(api.NotFound):
            api.find_session(native.key)
        for ref in (native.key, source.key, CLAUDE_WIN):
            reason = self.refused(ref)
            self.assertIn("was used since the import", reason)
            self.assertIn(f"{native.files[0]} is gone", reason)
        with self.assertRaises(api.NotFound):  # a start of a key is not enough here
            api.unimport(native.key[:20], dry_run=True)
        report = api.unimport(source.key, force=True)
        self.assertEqual(report.native_key, native.key)
        self.assertFalse((self.home / ".claude").exists())

    def test_an_unreadable_journal(self):
        api.import_session(self.source())
        os.unlink(journal.path())
        journal.path().mkdir()
        with self.assertRaises(api.UndoFailed) as caught:
            api.unimport(CLAUDE_WIN)
        self.assertIn("cannot be read", caught.exception.reason)
        self.assertIn("nothing was removed", caught.exception.reason)


# --- used since the import --------------------------------------------------------


class UsedSinceTest(UndoCase):
    def setUp(self):
        super().setUp()
        self.imported = api.import_session(self.source())
        self.target = self.home / ".claude" / "projects" / encode_cwd(str(self.w.site))
        self.transcript = self.target / f"{CLAUDE_WIN}.jsonl"
        self.side = self.target / CLAUDE_WIN
        self.side_file = self.side / files_below(self.side)[0]

    def test_a_file_that_grew_is_named_and_nothing_is_removed(self):
        size = os.path.getsize(self.transcript)
        with open(self.transcript, "ab") as fh:
            fh.write(b'{"type":"user"}\n')
        reason = self.refused(CLAUDE_WIN)
        self.assertEqual(
            reason,
            "the imported copy was used since the import: "
            f"{self.transcript} was {size:,} bytes and is {size + 16:,}. "
            "Nothing was removed; a forced undo removes it all the same",
        )
        self.assertEqual(len(self.records()), 1)
        self.assertIn(f"unimport claude {CLAUDE_WIN} refused: the imported copy", self.log())

    def log(self) -> str:
        logs.close()
        return (self.state / LOG_NAME).read_text(encoding="utf-8")

    def test_every_kind_of_difference(self):
        recorded = os.stat(self.transcript)
        os.utime(self.transcript, ns=(recorded.st_atime_ns, recorded.st_mtime_ns + 1))
        self.assertIn(f"{self.transcript} was modified", self.refused(CLAUDE_WIN))
        os.utime(self.transcript, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))
        self.assertTrue(api.unimport(CLAUDE_WIN, dry_run=True).removed)  # as recorded again

        os.unlink(self.side_file)
        self.assertIn(f"{self.side_file} is gone", self.refused(CLAUDE_WIN))
        self.side_file.mkdir()
        self.assertIn(f"{self.side_file} is not a regular file any more", self.refused(CLAUDE_WIN))
        self.side_file.rmdir()
        os.symlink(self.transcript, self.side_file)
        self.assertIn(f"{self.side_file} is not a regular file any more", self.refused(CLAUDE_WIN))

    def test_the_first_few_differences_are_named(self):
        for number in range(8):
            self.rewrite_journal(
                lambda record, number=number: record["manifest"]["files"].append(
                    {"path": str(self.target / f"gone-{number}"), "size": 1, "mtime_ns": 1}
                )
            )
        reason = self.refused(CLAUDE_WIN)
        self.assertEqual(reason.count(" is gone"), unimport.DIFFERENCES_SHOWN)
        self.assertIn("gone-4 is gone; and 3 more. Nothing was removed", reason)

    def test_the_refusal_carries_every_difference(self):
        """All of them, as a list: the reason names the first few only, and
        a front end counts them without reading it."""
        for number in range(8):
            self.rewrite_journal(
                lambda record, number=number: record["manifest"]["files"].append(
                    {"path": str(self.target / f"gone-{number}"), "size": 1, "mtime_ns": 1}
                )
            )
        with open(self.transcript, "ab") as fh:
            fh.write(b"more\n")
        forced = api.unimport(CLAUDE_WIN, force=True, dry_run=True).differences
        for dry_run in (True, False):
            with self.assertRaises(api.UndoFailed) as caught:
                api.unimport(CLAUDE_WIN, dry_run=dry_run)
            refusal = caught.exception
            self.assertIs(refusal.no_record, False)
            self.assertEqual(len(refusal.differences), 9)
            self.assertEqual(refusal.differences, forced)
            for text in refusal.differences[: unimport.DIFFERENCES_SHOWN]:
                self.assertIn(text, refusal.reason)  # the message is what it was
        self.assertTrue(self.transcript.exists())

    def test_the_report_counts_the_files_apart_from_the_directories(self):
        planned = api.unimport(CLAUDE_WIN, dry_run=True)
        self.assertGreater(len(planned.removed), planned.files)  # directories go too
        self.assertEqual(planned.files, len([p for p in planned.removed if os.path.isfile(p)]))
        self.assertTrue(all(os.path.isfile(p) for p in planned.removed[: planned.files]))
        report = api.unimport(CLAUDE_WIN)
        self.assertEqual((report.files, report.removed), (planned.files, planned.removed))

    def test_force_removes_it_all_the_same_and_says_what_differed(self):
        with open(self.transcript, "ab") as fh:
            fh.write(b"more\n")
        os.unlink(self.side_file)
        planned = api.unimport(CLAUDE_WIN, force=True, dry_run=True)
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual(
            (report.removed, report.left, report.differences),
            (planned.removed, planned.left, planned.differences),
        )
        self.assertTrue(report.forced)
        self.assertEqual(len(report.differences), 2)
        self.assertIn(f"{self.side_file} is gone", report.differences)
        self.assertEqual(report.left, [])
        self.assertFalse((self.home / ".claude").exists())
        undone = self.records()[-1]
        self.assertEqual((undone["record"], undone["forced"]), ("undo", True))
        self.assertEqual(undone["differences"], report.differences)
        self.assertIn(str(self.transcript), undone["removed"])
        self.assertNotIn(str(self.side_file), undone["removed"])  # it was gone already

    def test_a_file_the_import_did_not_create_stays_and_so_does_what_holds_it(self):
        mine = self.side / "mine.txt"  # in a directory the import made
        mine.write_text("keep", encoding="utf-8")
        other = self.target / "another-session.jsonl"
        other.write_text("{}\n", encoding="utf-8")
        for force in (False, True):
            planned = api.unimport(CLAUDE_WIN, force=force, dry_run=True)
            self.assertNotIn(str(mine), planned.removed)
            self.assertNotIn(str(other), planned.removed)
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual((report.removed, report.left), (planned.removed, planned.left))
        # Never a directory with what it holds: the two files are all that is left.
        self.assertEqual(
            files_below(self.home / ".claude"),
            sorted(os.path.relpath(path, self.home / ".claude") for path in (mine, other)),
        )
        self.assertEqual((mine.read_text(), other.read_text()), ("keep", "{}\n"))
        self.assertEqual(
            report.left,
            [
                (str(directory), unimport.NOT_EMPTY)
                for directory in (
                    self.home / ".claude",
                    self.target.parent,
                    self.target,
                    self.side,
                )
            ],
        )
        self.assertFalse(self.transcript.exists())
        self.assertFalse(self.side_file.exists())

    def test_a_directory_where_the_import_put_a_file(self):
        os.unlink(self.transcript)
        self.transcript.mkdir()
        (self.transcript / "inside").write_text("x", encoding="utf-8")
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertIn(
            (str(self.transcript), "a directory now, where the import put a file"), report.left
        )
        self.assertTrue((self.transcript / "inside").exists())
        self.assertFalse(self.side.exists())

    def test_what_cannot_be_removed_is_left_and_the_rest_still_goes(self):
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if os.fspath(path) == str(self.transcript):
                raise PermissionError(13, "Permission denied", str(self.transcript))
            return real(path, *args, **kwargs)

        with mock.patch.object(os, "unlink", unlink):
            report = api.unimport(CLAUDE_WIN)
        self.assertIn(
            (str(self.transcript), "could not be removed: Permission denied"), report.left
        )
        self.assertIn((str(self.target), unimport.NOT_EMPTY), report.left)
        self.assertNotIn(str(self.transcript), report.removed)
        self.assertEqual(
            files_below(self.home / ".claude"),
            [os.path.relpath(self.transcript, self.home / ".claude")],
        )
        self.assertFalse(self.side.exists())
        undone = self.records()[-1]
        self.assertEqual(undone["removed"], sorted(report.removed))
        self.assertEqual([item["path"] for item in undone["left"]], [p for p, _ in report.left])


class OddFilesAndRacesTest(UndoCase):
    """What an undo meets when the store changed under it, or cannot be
    looked at."""

    def setUp(self):
        super().setUp()
        self.imported = api.import_session(self.source())
        self.root = self.home / ".claude"
        self.target = self.root / "projects" / encode_cwd(str(self.w.site))
        self.transcript = self.target / f"{CLAUDE_WIN}.jsonl"
        self.side = self.target / CLAUDE_WIN
        self.side_file = self.side / files_below(self.side)[0]

    @unittest.skipIf(os.geteuid() == 0, "root can look at everything")
    def test_what_cannot_be_looked_at_is_a_difference_and_is_left_when_forced(self):
        os.chmod(self.side, 0)
        self.addCleanup(os.chmod, self.side, 0o700)
        reason = self.refused(CLAUDE_WIN)
        self.assertIn(f"{self.side_file} cannot be read (Permission denied)", reason)
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual(
            report.left,
            [
                (str(self.root), unimport.NOT_EMPTY),
                (str(self.target.parent), unimport.NOT_EMPTY),
                (str(self.target), unimport.NOT_EMPTY),
                (str(self.side), "cannot be read: Permission denied"),
                (str(self.side_file), "cannot be read: Permission denied"),
            ],
        )
        self.assertEqual(report.removed, [str(self.transcript)])
        os.chmod(self.side, 0o700)
        self.assertTrue(self.side_file.exists())

    def test_a_link_where_the_import_made_a_directory(self):
        """The native store is the user's own: a directory they moved and
        linked back is followed for its files, as an import follows it, and
        the link itself is left."""
        moved = self.home / "moved"
        os.rename(self.side, moved)
        os.symlink(moved, self.side)
        report = api.unimport(CLAUDE_WIN)
        self.assertEqual(report.differences, [])
        self.assertIn(str(self.side_file), report.removed)
        self.assertEqual(files_below(moved), [])
        self.assertTrue(os.path.islink(self.side))
        self.assertIn(
            (str(self.side), "a link now, where the import made a directory"), report.left
        )
        self.assertIn((str(self.target), unimport.NOT_EMPTY), report.left)

    def test_what_went_between_the_plan_and_the_removal_is_not_counted(self):
        record = self.records()[0]
        made = unimport.plan(record, self.root)
        self.assertIn(str(self.side_file), made.files)
        self.assertIn(str(self.side), made.dirs)
        shutil.rmtree(self.side)  # by someone else, meanwhile
        removed, left = unimport._carry_out(made)
        self.assertEqual(left, [])
        self.assertNotIn(str(self.side_file), removed)
        self.assertNotIn(str(self.side), removed)
        gone = (str(self.side_file), str(self.side))
        self.assertEqual(removed, [p for p in made.files + made.dirs if p not in gone])
        self.assertIn(str(self.transcript), removed)
        self.assertFalse(self.root.exists())

    def test_a_directory_that_filled_up_or_will_not_go_is_left(self):
        made = unimport.plan(self.records()[0], self.root)
        late = self.side / "late.txt"
        late.write_text("just arrived", encoding="utf-8")  # after the plan
        real = os.rmdir

        def rmdir(path, *args, **kwargs):
            if os.fspath(path) == str(self.target.parent):
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(os, "rmdir", rmdir):
            removed, left = unimport._carry_out(made)
        self.assertEqual(late.read_text(), "just arrived")
        self.assertEqual(
            left,
            [
                (str(self.root), unimport.NOT_EMPTY),
                (str(self.target.parent), "could not be removed: Permission denied"),
                (str(self.target), unimport.NOT_EMPTY),
                (str(self.side), unimport.NOT_EMPTY),
            ],
        )
        self.assertEqual(removed, made.files)

    def test_an_import_that_someone_else_undid_while_this_one_waited(self):
        record = self.records()[0]
        with (
            mock.patch.object(unimport, "find_record", side_effect=[record, None]),
            self.assertRaises(api.UndoFailed) as caught,
        ):
            api.unimport(CLAUDE_WIN)
        self.assertEqual(caught.exception.reason, unimport.NO_RECORD)
        self.assertTrue(self.transcript.exists())
        self.assertEqual(len(self.records()), 1)

    def test_a_journal_that_cannot_be_opened_stops_the_undo_before_it_removes_anything(self):
        before = picture(self.home)
        with (
            mock.patch.object(
                journal,
                "open_for_append",
                side_effect=PermissionError(13, "Permission denied", "j"),
            ),
            self.assertRaises(api.UndoFailed) as caught,
        ):
            api.unimport(CLAUDE_WIN)
        self.assertEqual(
            caught.exception.reason,
            "the undo could not start: Permission denied (j); nothing was removed",
        )
        self.assertEqual(picture(self.home), before)
        self.assertEqual(len(self.records()), 1)
        if POSIX:
            self.assertFalse(fsutil.lock_file().exists())

    def test_a_record_of_a_provider_or_an_id_that_is_not_one(self):
        with open(journal.path(), "a", encoding="utf-8") as fh:
            for record in (
                {
                    "record": "import",
                    "provider": "nope",
                    "id": CLAUDE_WIN,
                    "native_key": "nope:1:x",
                },
                {"record": "import", "provider": "claude", "id": "--yolo", "native_key": "odd:key"},
            ):
                fh.write(json.dumps(record) + "\n")
        for dry_run in (True, False):
            with self.assertRaises(api.NotFound) as caught:
                api.unimport("nope:1:x", dry_run=dry_run)
            self.assertEqual(caught.exception.reason, "no reader for provider 'nope'")
            with self.assertRaises(api.UndoFailed) as refused:
                api.unimport("odd:key", force=True, dry_run=dry_run)
            self.assertEqual(refused.exception.reason, unimport.NO_RECORD)
        self.assertTrue(self.transcript.exists())
        self.assertEqual(len(self.records()), 3)


# --- what the journal may say -----------------------------------------------------


class JournalIsNotTrustedTest(UndoCase):
    """The journal is the user's own file, and it may be malformed: no path
    in it takes an undo outside the native store."""

    def setUp(self):
        super().setUp()
        self.imported = api.import_session(self.source())
        self.root = self.home / ".claude"
        self.outside = self.home / "precious.txt"
        self.outside.write_text("mine", encoding="utf-8")
        self.elsewhere = self.home / "elsewhere"
        self.elsewhere.mkdir()

    def refused_whatever_the_force(self, change) -> str:
        self.rewrite_journal(change)
        reasons = {self.refused(CLAUDE_WIN, force=force) for force in (False, True)}
        self.assertEqual(len(reasons), 1, reasons)
        self.assertEqual(self.outside.read_text(), "mine")
        self.assertTrue(self.elsewhere.is_dir())
        return reasons.pop()

    def entry(self, path) -> dict:
        st = os.lstat(path) if os.path.lexists(path) else None
        return {
            "path": str(path),
            "size": st.st_size if st else 0,
            "mtime_ns": st.st_mtime_ns if st else 0,
        }

    def test_a_file_outside_the_native_store_is_never_removed(self):
        inside = self.root / "projects"
        for path in (
            self.outside,  # a file that is there, with the right size and time
            f"{inside}/../../precious.txt",
            f"{self.root}/../precious.txt",
            f"{self.root}//projects/x",
            f"{self.root}/./x",
            str(self.root),  # the store itself is not a file below it
            f"{self.root}/",
            "precious.txt",
            f"{self.root}\0/x",
            "",
            str(self.home / ".claude-other" / "x"),  # a name that starts like the store's
        ):
            with self.subTest(path=path):
                saved = copy.deepcopy(self.records())
                entry = dict(self.entry(self.outside), path=str(path))
                reason = self.refused_whatever_the_force(
                    lambda record, entry=entry: record["manifest"]["files"].append(entry)
                )
                self.assertIn("is not a plain path below the native store", reason)
                self.assertIn("nothing was removed", reason)
                with open(journal.path(), "w", encoding="utf-8") as fh:
                    fh.writelines(json.dumps(record) + "\n" for record in saved)

    def test_a_directory_outside_the_native_store_is_never_removed(self):
        for path in (
            self.elsewhere,  # empty, and not ours
            f"{self.root}/../elsewhere",
            f"{self.root}/projects/..",
            "elsewhere",
            f"{self.root}-other",
        ):
            with self.subTest(path=path):
                saved = copy.deepcopy(self.records())
                reason = self.refused_whatever_the_force(
                    lambda record, path=path: record["manifest"]["dirs"].append(str(path))
                )
                self.assertIn("is not a plain path below the native store", reason)
                with open(journal.path(), "w", encoding="utf-8") as fh:
                    fh.writelines(json.dumps(record) + "\n" for record in saved)

    def test_the_store_itself_and_what_is_above_it_go_only_when_empty(self):
        """An import that had to make the native store recorded it, and the
        directories above it that were missing."""
        record = self.records()[0]
        self.assertIn(str(self.root), record["manifest"]["dirs"])
        # Made up: the home and the root of the disk as directories "it made".
        self.rewrite_journal(
            lambda record: record["manifest"]["dirs"].extend([str(self.home), os.sep])
        )
        report = api.unimport(CLAUDE_WIN)
        self.assertFalse(self.root.exists())
        self.assertIn(str(self.root), report.removed)
        self.assertEqual(
            report.left, [(os.sep, unimport.NOT_EMPTY), (str(self.home), unimport.NOT_EMPTY)]
        )
        self.assertEqual(self.outside.read_text(), "mine")

    def test_a_record_for_another_native_store(self):
        reason = self.refused_whatever_the_force(
            lambda record: record.update(destination_store=str(self.home / ".claude-old"))
        )
        self.assertIn("was made into another native store", reason)
        self.assertIn("nothing was removed", reason)

    def test_a_damaged_manifest(self):
        good = self.records()[0]["manifest"]
        for manifest in (
            None,
            [],
            "x",
            {},
            {"files": good["files"]},
            {"files": "x", "dirs": []},
            {"files": [], "dirs": {}},
            {"files": ["x"], "dirs": []},
            {"files": [{"size": 1, "mtime_ns": 1}], "dirs": []},
            {"files": [{"path": 7, "size": 1, "mtime_ns": 1}], "dirs": []},
            {"files": [{"path": str(self.root / "x"), "size": "1", "mtime_ns": 1}], "dirs": []},
            {"files": [{"path": str(self.root / "x"), "size": True, "mtime_ns": 1}], "dirs": []},
            {"files": [{"path": str(self.root / "x"), "size": 1}], "dirs": []},
            {"files": [], "dirs": [7]},
        ):
            with self.subTest(manifest=manifest):
                reason = self.refused_whatever_the_force(
                    lambda record, manifest=manifest: record.update(manifest=manifest)
                )
                self.assertEqual(
                    reason, "the import record is damaged (its manifest); nothing was removed"
                )

    def test_plan_checks_what_it_is_given(self):
        record = self.records()[0]
        for broken, needle in (
            (dict(record, provider=None), "no provider or id"),
            (dict(record, id=7), "no provider or id"),
        ):
            with self.assertRaises(api.UndoFailed) as caught:
                unimport.plan(broken, self.root)
            self.assertIn(needle, caught.exception.reason)
        for root in ("", "relative/.claude", f"{self.root}\0"):
            with self.assertRaises(api.UndoFailed) as caught:
                unimport.plan(record, root)
            self.assertIn("is not an absolute path", caught.exception.reason)
        with self.assertRaises(api.UndoFailed) as caught:
            unimport.plan(record, self.root, extras=[str(self.outside)], force=True)
        self.assertIn("is not a plain path below the native store", caught.exception.reason)
        self.assertEqual(self.outside.read_text(), "mine")

    def test_below_is_the_rule_of_an_import_destination(self):
        root = str(self.root)
        for path in (f"{root}/a", f"{root}/a/b.jsonl", f"{root}/a b/\u00e9"):
            self.assertTrue(importer.below(root, path), path)
        for path in (
            root,
            f"{root}/",
            f"{root}/a/../b",
            f"{root}/./a",
            f"{root}//a",
            f"{root}/a/",
            f"{root}x/a",
            "/elsewhere/a",
            "a",
            "",
            f"{root}/a\0",
            None,
            7,
            Path(root) / "a",
        ):
            self.assertFalse(importer.below(root, path), path)
            if isinstance(path, str):
                # And ``contained`` refuses the same path as a destination.
                plan = api._plan(self.source(CLAUDE_WIN_PLAIN), api.load_index())
                plan.units[0].dst = path
                with self.assertRaises(importer.ImportFailed):
                    importer.contained(plan)


class LegacyRecordTest(UndoCase):
    """A record from before imports listed what they created: the old
    ``imports.json``, or a line without a manifest."""

    def setUp(self):
        super().setUp()
        self.imported = api.import_session(self.source())
        self.target = self.home / ".claude" / "projects" / encode_cwd(str(self.w.site))
        record = self.records()[0]
        for key in ("manifest", "record", "version"):  # what such a record did not have
            del record[key]
        os.unlink(journal.path())
        journal.legacy_path().write_text(json.dumps({"imports": [record]}), encoding="utf-8")
        self.old = journal.legacy_path().read_bytes()
        self.assertEqual([r["version"] for r in self.records()], [1])

    def test_it_is_undone_only_when_forced(self):
        self.assertEqual(self.refused(CLAUDE_WIN), unimport.LEGACY)
        self.assertIn("can only be undone by force", unimport.LEGACY)
        extra = self.target / CLAUDE_WIN / "added later.txt"
        extra.write_text("by the tool", encoding="utf-8")
        planned = api.unimport(CLAUDE_WIN, force=True, dry_run=True)
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual((report.removed, report.left), (planned.removed, planned.left))
        # The two units it names, whole: what was added to the directory since goes too.
        self.assertIn(str(extra), report.removed)
        self.assertIn(str(self.target / f"{CLAUDE_WIN}.jsonl"), report.removed)
        self.assertIn(str(self.target / CLAUDE_WIN), report.removed)
        self.assertEqual(files_below(self.home / ".claude"), [])
        # The directories above the units are not in such a record: they stay.
        self.assertTrue(self.target.is_dir())
        self.assertEqual(report.left, [])
        # The old file is never rewritten; the undo is a line of the journal.
        self.assertEqual(journal.legacy_path().read_bytes(), self.old)
        self.assertEqual([r["record"] for r in self.records()], ["import", "undo"])
        self.assertEqual(self.refused(CLAUDE_WIN, force=True), unimport.NO_RECORD)

    def test_a_directory_it_names_goes_with_everything_below_it(self):
        deep = self.target / CLAUDE_WIN / "deeper" / "still"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("x", encoding="utf-8")
        (deep.parent / "empty").mkdir()
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual(report.left, [])
        for path in (deep / "file.txt", deep, deep.parent / "empty", deep.parent):
            self.assertIn(str(path), report.removed)
        self.assertEqual(os.listdir(self.target), [])

    @unittest.skipIf(os.geteuid() == 0, "root can look at everything")
    def test_a_directory_below_it_that_cannot_be_listed_stays(self):
        locked = self.target / CLAUDE_WIN / "locked"
        locked.mkdir()
        (locked / "file.txt").write_text("x", encoding="utf-8")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o700)
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual(
            report.left,
            [
                (str(self.target / CLAUDE_WIN), unimport.NOT_EMPTY),
                (str(locked), "cannot be read: Permission denied"),
            ],
        )
        self.assertFalse((self.target / f"{CLAUDE_WIN}.jsonl").exists())
        os.chmod(locked, 0o700)
        self.assertEqual(os.listdir(locked), ["file.txt"])

    def test_only_what_is_named_after_the_session_is_touched(self):
        keep = self.target / "another.jsonl"
        keep.write_text("{}\n", encoding="utf-8")
        record = json.loads(self.old)["imports"][0]
        record["destination_files"] += [str(keep), str(self.target)]
        journal.legacy_path().write_text(json.dumps({"imports": [record]}), encoding="utf-8")
        report = api.unimport(CLAUDE_WIN, force=True)
        self.assertEqual(
            report.left, [(str(self.target), unimport.NOT_NAMED), (str(keep), unimport.NOT_NAMED)]
        )
        self.assertEqual(
            files_below(self.home / ".claude"), [os.path.relpath(keep, self.home / ".claude")]
        )

    def test_its_paths_are_checked_like_any_other(self):
        outside = self.home / f"{CLAUDE_WIN}.jsonl"
        outside.write_text("mine", encoding="utf-8")
        record = json.loads(self.old)["imports"][0]
        for units in ([str(outside)], [f"{self.home}/.claude/../{CLAUDE_WIN}.jsonl"], "x", [7]):
            record["destination_files"] = units
            journal.legacy_path().write_text(json.dumps({"imports": [record]}), encoding="utf-8")
            with self.assertRaises(api.UndoFailed) as caught:
                api.unimport(CLAUDE_WIN, force=True)
            self.assertIn("nothing was removed", caught.exception.reason)
        self.assertEqual(outside.read_text(), "mine")
        self.assertTrue((self.target / f"{CLAUDE_WIN}.jsonl").exists())

    def test_links_in_a_directory_are_removed_not_followed(self):
        precious = self.home / "precious"
        precious.mkdir()
        (precious / "file.txt").write_text("mine", encoding="utf-8")
        os.symlink(precious, self.target / CLAUDE_WIN / "link")
        api.unimport(CLAUDE_WIN, force=True)
        self.assertFalse((self.target / CLAUDE_WIN).exists())
        self.assertEqual((precious / "file.txt").read_text(), "mine")


# --- what the tool adds beside an imported session --------------------------------


class ExtrasTest(UndoCase):
    foreign = ("antigravity",)

    def setUp(self):
        super().setUp()
        self.dst = self.home / ".gemini" / "antigravity-cli"
        api.import_session(self.source(AGY_WIN), experimental=True)
        # What agy leaves once it has opened the conversation, the database untouched.
        self.db = self.dst / "conversations" / f"{AGY_WIN}.db"
        self.extras = [
            Path(f"{self.db}-wal"),
            Path(f"{self.db}-shm"),
            self.dst / "presence" / f"{AGY_WIN}.lock",
        ]
        (self.dst / "presence").mkdir()
        for path in self.extras:
            path.write_bytes(b"")

    def test_the_adapters_say_what_their_tool_adds(self):
        self.assertEqual(
            Antigravity().undo_extras(AGY_WIN, self.dst),
            [f"{self.db}-wal", f"{self.db}-shm", str(self.dst / "presence" / f"{AGY_WIN}.lock")],
        )
        for provider in (Claude(), Codex(), Grok(), Gemini()):
            self.assertEqual(provider.undo_extras(AGY_WIN, self.home / "x"), [])

    def test_without_force_they_are_left_and_reported(self):
        report = api.unimport(AGY_WIN)
        self.assertFalse(self.db.exists())
        for path in self.extras:
            self.assertTrue(path.exists())
            self.assertIn((str(path), unimport.EXTRA), report.left)
        self.assertIn((str(self.dst / "conversations"), unimport.NOT_EMPTY), report.left)
        self.assertEqual(
            files_below(self.dst), sorted(os.path.relpath(p, self.dst) for p in self.extras)
        )
        self.assertIn("removed only by a forced undo", unimport.EXTRA)

    def test_with_force_they_go_too(self):
        planned = api.unimport(AGY_WIN, force=True, dry_run=True)
        for path in self.extras:
            self.assertTrue(path.exists(), "a dry run removes nothing")
        report = api.unimport(AGY_WIN, force=True)
        self.assertEqual((report.removed, report.left), (planned.removed, planned.left))
        for path in self.extras:
            self.assertIn(str(path), report.removed)
        self.assertEqual(files_below(self.dst), [])
        # ``presence/`` was not made by the import: it stays, and the store with it.
        self.assertEqual(os.listdir(self.dst), ["presence"])
        self.assertEqual(
            report.left,
            [
                (str(self.home / ".gemini"), unimport.NOT_EMPTY),
                (str(self.dst), unimport.NOT_EMPTY),
            ],
        )

    def test_a_database_the_tool_wrote_to_is_a_difference(self):
        with open(self.db, "ab") as fh:
            fh.write(b"\0" * 4096)
        reason = self.refused(AGY_WIN)
        self.assertIn("was used since the import", reason)
        self.assertIn(str(self.db), reason)
        api.unimport(AGY_WIN, force=True)
        self.assertEqual(files_below(self.dst), [])

    def test_extras_that_are_not_there_are_not_reported(self):
        for path in self.extras:
            path.unlink()
        (self.dst / "presence").rmdir()
        for force in (False, True):
            self.assertEqual(api.unimport(AGY_WIN, force=force, dry_run=True).left, [])
        api.unimport(AGY_WIN)
        self.assertFalse((self.home / ".gemini").exists())


# --- one at a time, and the record --------------------------------------------------


class LockAndRecordTest(UndoCase):
    def setUp(self):
        super().setUp()
        self.source_key = self.source().key
        self.imported = api.import_session(self.source())

    def test_the_comparison_and_the_removal_happen_under_the_lock(self):
        seen = {}
        real_plan, real_carry, real_write = unimport.plan, unimport._carry_out, journal.write

        def plan(*args, **kwargs):
            seen["plan"] = fsutil.lock_file().exists()
            return real_plan(*args, **kwargs)

        def carry_out(made):
            seen["remove"] = fsutil.lock_file().exists()
            return real_carry(made)

        def write(fd, record):
            seen["record"] = fsutil.lock_file().exists()
            return real_write(fd, record)

        with (
            mock.patch.object(unimport, "plan", plan),
            mock.patch.object(unimport, "_carry_out", carry_out),
            mock.patch.object(journal, "write", write),
        ):
            api.unimport(CLAUDE_WIN)
        self.assertEqual(seen, {"plan": True, "remove": True, "record": True})
        if POSIX:
            self.assertFalse(fsutil.lock_file().exists())
        self.assertEqual(state_names(), ["imports.jsonl"])

    def test_a_dry_run_takes_no_lock_and_writes_nothing(self):
        before = picture(self.w.box.root, (LOG_NAME,))
        real = fsutil.lock
        with mock.patch.object(fsutil, "lock", side_effect=AssertionError("a dry run locked")):
            planned = api.unimport(CLAUDE_WIN, dry_run=True)
        self.assertIs(fsutil.lock, real)
        self.assertTrue(planned.dry_run)
        self.assertEqual(picture(self.w.box.root, (LOG_NAME,)), before)
        report = api.unimport(CLAUDE_WIN)
        self.assertEqual(
            (report.removed, report.left, report.bytes),
            (planned.removed, planned.left, planned.bytes),
        )
        self.assertGreater(report.bytes, 0)

    def test_an_undo_that_cannot_get_the_lock_is_refused(self):
        holder = python_child(HOLDER, stdin=-1)  # -1: a pipe; `finish` closes it, which lets it go
        try:
            self.assertEqual(holder.stdout.readline(), "held\n")
            with (
                mock.patch.object(fsutil, "LOCK_TIMEOUT", 0.3),
                self.assertRaises(api.UndoFailed) as caught,
            ):
                api.unimport(CLAUDE_WIN)
        finally:
            finish(self, [holder])
        self.assertEqual(
            caught.exception.reason,
            f"an import or another undo is still running (process {holder.pid}); "
            "nothing was removed",
        )
        self.assertTrue(Path(self.imported.files[0]).exists())
        self.assertEqual(len(self.records()), 1)
        api.unimport(CLAUDE_WIN)  # and afterwards it goes through

    def test_two_processes_undoing_the_same_import(self):
        children = [launcher_child("unimport", self.imported.key, "--yes") for _ in range(2)]
        results = finish(self, children)
        self.assertEqual(sorted(status for status, _out, _err in results), [0, 1], results)
        (lost,) = [err for status, _out, err in results if status == 1]
        self.assertEqual(lost, f"agent-sessions: {unimport.NO_RECORD}\n")
        self.assertEqual([r["record"] for r in self.records()], ["import", "undo"])
        self.assertFalse((self.home / ".claude").exists())

    def test_the_undo_record(self):
        imported = self.records()[0]
        report = api.unimport(CLAUDE_WIN)
        undone = self.records()[1]
        self.assertRegex(undone.pop("undone_at"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(
            undone,
            {
                "record": "undo",
                "version": 2,
                "provider": "claude",
                "id": CLAUDE_WIN,
                "source_key": self.source_key,
                "native_key": self.imported.key,
                "destination_store": str(self.home / ".claude"),
                "imported_at": imported["imported_at"],
                "forced": False,
                "differences": [],
                "removed": sorted(
                    [item["path"] for item in imported["manifest"]["files"]]
                    + imported["manifest"]["dirs"]
                ),
                "left": [],
                "agent_sessions_version": "0.2.0",
            },
        )
        # In the report: the files, then the directories from the deepest up.
        files = [item["path"] for item in imported["manifest"]["files"]]
        self.assertEqual(report.removed[: len(files)], files)
        depths = [path.count(os.sep) for path in report.removed[len(files) :]]
        self.assertEqual(depths, sorted(depths, reverse=True))
        self.assertEqual(report.removed[-1], str(self.home / ".claude"))
        self.assertEqual(report.bytes, sum(item["size"] for item in imported["manifest"]["files"]))
        self.assertEqual(stat.S_IMODE(os.stat(journal.path()).st_mode), 0o600)

    def test_a_journal_that_cannot_take_the_record_is_said(self):
        with (
            mock.patch.object(journal, "write", side_effect=OSError(28, "No space left on device")),
            self.assertRaises(api.UndoFailed) as caught,
        ):
            api.unimport(CLAUDE_WIN)
        self.assertIn(
            "were removed, but the journal could not take the record", caught.exception.reason
        )
        self.assertIn("No space left on device", caught.exception.reason)
        self.assertFalse((self.home / ".claude").exists())
        # The import still stands in the journal; a forced undo settles it.
        self.assertIn("is gone", self.refused(CLAUDE_WIN))
        report = api.unimport(self.source_key, force=True)
        self.assertEqual(report.removed, [])
        self.assertEqual([r["record"] for r in self.records()], ["import", "undo"])

    def test_the_log_has_the_undo(self):
        api.unimport(CLAUDE_WIN)
        logs.close()
        text = (self.state / LOG_NAME).read_text(encoding="utf-8")
        self.assertIn(f"INFO unimport claude {CLAUDE_WIN} from {self.home / '.claude'}\n", text)
        self.assertRegex(
            text, rf"INFO unimport claude {CLAUDE_WIN}: removed \d+ files and directories, left 0"
        )

    def test_afterwards_the_foreign_session_is_not_imported_any_more(self):
        self.assertEqual(self.source().imported_as, self.imported.key)
        stale = api.load_index()
        api.unimport(self.imported, index=stale)
        source = self.source()
        self.assertIsNone(source.imported_as)
        self.assertEqual(api.load_index().where(source), "C:")
        with self.assertRaises(api.LaunchFailed) as caught:
            api.open_session(source, dry_run=True)
        self.assertIn("import it first", caught.exception.reason)
        self.assertEqual(api.import_options(source), api.ImportOptions(True))


# --- the command ----------------------------------------------------------------------


class UnimportCommandTest(UndoCase):
    foreign = ("claude", "codex")

    def setUp(self):
        super().setUp()
        self.imported = api.import_session(self.source())
        self.root = self.home / ".claude"
        self.target = self.root / "projects" / encode_cwd(str(self.w.site))
        self.transcript = self.target / f"{CLAUDE_WIN}.jsonl"
        record = self.records()[0]["manifest"]
        self.count = len(record["files"]) + len(record["dirs"])
        self.size = cli._size(sum(item["size"] for item in record["files"]))

    def answer(self, text, *argv):
        """Run on a terminal, with ``text`` typed at the prompt."""
        out, err = io.StringIO(), Terminal()
        with (
            mock.patch.object(sys, "stdin", Terminal(text)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    @property
    def question(self):
        return (
            f"Remove the imported copy {self.imported.key}, {self.count} files and directories "
            f"({self.size})? [y/N] "
        )

    def test_dry_run_lists_what_would_go_and_removes_nothing(self):
        before = picture(self.w.box.root, (LOG_NAME,))
        code, out, err = run("unimport", self.imported.key, "--dry-run")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), self.count + 1)
        self.assertTrue(all(line.startswith("would remove ") for line in lines[:-1]), lines)
        self.assertIn(f"would remove {self.transcript}", lines)
        self.assertEqual(lines[-2], f"would remove {self.root}")
        self.assertEqual(lines[-1], "dry run: nothing was removed")
        self.assertEqual(picture(self.w.box.root, (LOG_NAME,)), before)

    def test_it_asks_on_a_terminal(self):
        for text in ("n\n", "\n", "", "maybe\n"):
            with self.subTest(text=text):
                self.assertEqual(
                    self.answer(text, "unimport", CLAUDE_WIN),
                    (1, "", self.question + "agent-sessions: nothing was removed\n"),
                )
        self.assertTrue(self.transcript.exists())
        self.assertEqual(len(self.records()), 1)
        code, out, err = self.answer("y\n", "unimport", CLAUDE_WIN)
        self.assertEqual(code, 0)
        self.assertEqual(
            err,
            self.question + f"agent-sessions: removed the imported copy {self.imported.key}: "
            f"{self.count} files and directories\n",
        )
        lines = out.splitlines()
        self.assertEqual(len(lines), self.count)
        self.assertIn(f"removed {self.transcript}", lines)
        self.assertEqual(lines[-1], f"removed {self.root}")
        self.assertFalse(self.root.exists())

    def test_with_yes_it_does_not_ask(self):
        code, out, err = self.answer("", "unimport", self.source().key, "--yes")
        self.assertEqual((code, len(out.splitlines())), (0, self.count))
        self.assertNotIn("[y/N]", err)
        self.assertFalse(self.root.exists())

    def test_where_nobody_can_be_asked_it_is_refused_unless_yes(self):
        code, out, err = run("unimport", CLAUDE_WIN)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            f"agent-sessions: unimport {self.imported.key}: not a terminal to ask on; pass --yes\n",
        )
        self.assertTrue(self.transcript.exists())
        code, out, err = run("unimport", CLAUDE_WIN, "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self.root.exists())

    def test_a_refusal_comes_before_any_question(self):
        with open(self.transcript, "ab") as fh:
            fh.write(b"used\n")
        for extra in ((), ("--yes",), ("--dry-run",)):
            code, out, err = self.answer("y\n", "unimport", CLAUDE_WIN, *extra)
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(
                err.startswith("agent-sessions: the imported copy was used since the import: "), err
            )
            self.assertNotIn("[y/N]", err)
        self.assertTrue(self.transcript.exists())
        # Forced: the question says so, and the output says what differed.
        code, out, err = self.answer("y\n", "unimport", CLAUDE_WIN, "--force")
        self.assertEqual(code, 0)
        self.assertIn("? It was used since the import. [y/N] ", err)
        self.assertTrue(out.startswith(f"differs from the import record: {self.transcript} was "))
        self.assertFalse(self.root.exists())

    def test_what_is_left_is_printed_with_the_reason(self):
        keep = self.target / "mine.jsonl"
        keep.write_text("{}\n", encoding="utf-8")
        code, out, err = run("unimport", CLAUDE_WIN, "--yes")
        self.assertEqual(code, 0)
        self.assertIn(f"left {self.target} ({unimport.NOT_EMPTY})\n", out)
        self.assertIn(f"left {self.root} ({unimport.NOT_EMPTY})\n", out)
        self.assertTrue(err.endswith("; 3 left in place\n"), err)
        self.assertEqual(keep.read_text(), "{}\n")

    def test_refusals_exit_one(self):
        for argv, needle in (
            (("unimport", CODEX_WIN, "--yes"), unimport.NO_RECORD),
            (("unimport", "claude:nope", "--yes"), "no session matches 'claude:nope'"),
            (("unimport", "", "--yes"), "no session key given"),
        ):
            code, out, err = run(*argv)
            self.assertEqual((code, out), (1, ""))
            self.assertIn(needle, err)
        self.assertEqual(run("unimport")[0], 2)
        self.assertIn("unimport", run("--help")[1])
        self.assertTrue(self.transcript.exists())


if __name__ == "__main__":
    unittest.main()
