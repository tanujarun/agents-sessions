"""An imported session is resumed in the folder of its import
(``Index.finalise`` and the standing import record, ``journal.find_record``).

Claude's and Antigravity's imports keep the folder as the other machine
recorded it inside the copied files, so the native copy's ``cwd`` is still
``F:\\…``. The path map leads from there to where that folder is found today:
not the folder the user chose for the import, and nowhere when the drive that
resolved the letter is away. The machine is the one of tests/fixture_world.py."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import AGY_WIN, CLAUDE_WIN, CLAUDE_WIN_PLAIN, World
from support import claude_transcript

from agent_sessions import api, journal

RECORDED = "F:\\Proj\\My Site"  # what the Windows fixtures recorded
ONLY = "dddddddd-1111-4222-8333-444444444444"


class ResumeFolderCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("claude",)

    def setUp(self):
        self.w = World(self, self.foreign)
        self.home = self.w.home
        self.w.scan()
        # A folder that has nothing to do with the one the sessions recorded.
        self.chosen = self.home / "Work" / "new place"
        self.chosen.mkdir(parents=True)
        self.journal = Path(os.environ["AGENT_SESSIONS_STATE"]) / "imports.jsonl"

    def native(self, session_id=CLAUDE_WIN_PLAIN):
        return self.w.session(session_id, "native")

    def folder(self, session_id=CLAUDE_WIN_PLAIN):
        """``(cwd_local, cwd_exists, where a resume would start)`` of the copy."""
        copy = self.native(session_id)
        return copy.cwd_local, copy.cwd_exists, api.open_session(copy.key, dry_run=True).cwd

    def rewrite(self, change):
        """Change the one record of the journal with ``change(record)``."""
        (record,) = journal.read()
        change(record)
        self.journal.write_text(json.dumps(record) + "\n", encoding="utf-8")


class ChosenFolderTest(ResumeFolderCase):
    foreign = ("claude", "antigravity")

    def test_a_copy_imported_into_a_chosen_folder_is_resumed_there(self):
        copy = api.import_session(CLAUDE_WIN_PLAIN, target=str(self.chosen))
        self.assertEqual(copy.cwd, RECORDED)  # the copy still says what Windows recorded
        self.assertEqual((copy.cwd_local, copy.cwd_exists), (str(self.chosen), True))
        self.assertEqual(self.folder(), (str(self.chosen), True, str(self.chosen)))
        # Through the session it was imported from, too, and when it is started.
        foreign = self.w.session(CLAUDE_WIN_PLAIN, "foreign")
        self.assertEqual(foreign.cwd_local, str(self.w.site))  # where that folder is found
        plan = api.open_session(foreign.key)
        self.assertEqual(plan.argv, ["claude", "--resume", CLAUDE_WIN_PLAIN])
        self.assertEqual(self.w.started, [(plan.command, str(self.chosen))])
        # A scan changes nothing about it.
        self.w.scan()
        self.assertEqual(self.folder(), (str(self.chosen), True, str(self.chosen)))

    def test_antigravity(self):
        copy = api.import_session(AGY_WIN, target=str(self.chosen), experimental=True)
        self.assertEqual((copy.cwd_local, copy.cwd_exists), (str(self.chosen), True))
        self.assertEqual(api.open_session(copy.key, dry_run=True).cwd, str(self.chosen))

    def test_without_a_chosen_folder_it_is_the_mapped_one(self):
        copy = api.import_session(CLAUDE_WIN_PLAIN)
        self.assertEqual(journal.read()[0]["cwd_local"], str(self.w.site))
        self.assertEqual((copy.cwd_local, copy.cwd_exists), (str(self.w.site), True))

    def test_only_the_copy_of_that_import_is_changed(self):
        api.import_session(CLAUDE_WIN_PLAIN, target=str(self.chosen))
        api.import_session(CLAUDE_WIN)
        self.assertEqual(self.folder(CLAUDE_WIN)[0], str(self.w.site))
        # The sessions on the drive keep the folder the path map finds.
        index = api.load_index()
        self.assertEqual(
            sorted(s.id for s in index.sessions if s.cwd_local == str(self.chosen)),
            [CLAUDE_WIN_PLAIN],
        )

    def test_the_journal_is_read_once_for_a_listing(self):
        api.import_session(CLAUDE_WIN_PLAIN, target=str(self.chosen))
        api.import_session(CLAUDE_WIN, target=str(self.chosen))
        with mock.patch.object(journal, "read", wraps=journal.read) as read:
            index = api.load_index()
        read.assert_called_once_with(quiet=True)
        self.assertEqual(
            sorted(s.id for s in index.sessions if s.cwd_local == str(self.chosen)),
            sorted([CLAUDE_WIN, CLAUDE_WIN_PLAIN]),
        )


class SourceVolumeAwayTest(ResumeFolderCase):
    def setUp(self):
        super().setUp()
        # A second drive that has a folder of the same name: ``F:`` is told
        # from the one folder only DataF has, which a session of the Windows
        # store recorded. Without that store nothing says which drive F: is.
        self.g = self.w.box.mount("DataG")
        (self.g / "Proj" / "My Site").mkdir(parents=True)
        (self.w.f / "Proj" / "Only").mkdir()
        claude_transcript(
            self.w.profile / ".claude", ONLY, "F:\\Proj\\Only", project="F--Proj-Only"
        )
        self.w.scan()

    def test_the_mapped_folder_is_kept_while_the_source_drive_is_away(self):
        copy = api.import_session(CLAUDE_WIN_PLAIN)
        self.assertEqual(copy.cwd_local, str(self.w.site))
        self.w.box.unmount(self.w.c)
        for _listing in ("from the cache", "after a scan"):
            index = api.load_index()
            # The letter cannot be resolved any more ...
            self.assertEqual(
                [(row["letter"], row["reason"]) for row in index.pathmap.unresolved()],
                [("F:", "ambiguous")],
            )
            self.assertEqual(index.pathmap.to_local(RECORDED, copy.mount), (None, False))
            # ... and the copy is still resumed where it was imported for.
            self.assertEqual(self.folder(), (str(self.w.site), True, str(self.w.site)))
            api.scan()

    def test_without_the_record_the_folder_is_not_found(self):
        """What the record is for: the same machine without it."""
        api.import_session(CLAUDE_WIN_PLAIN)
        self.w.box.unmount(self.w.c)
        self.journal.unlink()
        copy = self.native()
        self.assertEqual((copy.cwd_local, copy.cwd_exists), (None, False))
        plan = api.open_session(copy.key, dry_run=True)
        self.assertEqual(
            (plan.cwd, plan.note),
            (str(self.home), "project folder is gone; starting in the home directory"),
        )


class RecordTest(ResumeFolderCase):
    """The journal is the user's own file: trusted, possibly malformed."""

    def setUp(self):
        super().setUp()
        api.import_session(CLAUDE_WIN_PLAIN, target=str(self.chosen))
        self.mapped = (str(self.w.site), True, str(self.w.site))  # what the path map gives

    def test_an_import_that_was_undone_says_nothing(self):
        journal.append({"record": journal.UNDO, "provider": "claude", "id": CLAUDE_WIN_PLAIN})
        self.assertEqual(self.folder(), self.mapped)
        # An undo of another session, or of another provider's, takes nothing back.
        self.journal.write_text(self.journal.read_text(encoding="utf-8").splitlines()[0] + "\n")
        journal.append({"record": journal.UNDO, "provider": "claude", "id": CLAUDE_WIN})
        journal.append({"record": journal.UNDO, "provider": "codex", "id": CLAUDE_WIN_PLAIN})
        self.assertEqual(self.folder()[0], str(self.chosen))

    def test_the_last_import_that_stands_is_the_one(self):
        other = self.home / "Work" / "other"
        other.mkdir()
        (first,) = journal.read()
        journal.append({"record": journal.UNDO, "provider": "claude", "id": CLAUDE_WIN_PLAIN})
        journal.append(dict(first, chosen_folder=str(other), cwd_local=str(other)))
        self.assertEqual(self.folder()[0], str(other))

    def test_a_folder_that_is_gone_falls_back(self):
        # The chosen folder is gone, the one the record calls ``cwd_local`` is there.
        self.rewrite(lambda record: record.update(cwd_local=str(self.home)))
        os.rmdir(self.chosen)
        self.assertEqual(self.folder()[0], str(self.home))
        # Both are gone: the path map, as for any other session.
        self.rewrite(lambda record: record.update(cwd_local=str(self.chosen)))
        self.assertEqual(self.folder(), self.mapped)

    def test_a_folder_that_is_not_one_is_not_used(self):
        afile = self.home / "a file"
        afile.write_text("x", encoding="utf-8")
        for value in (
            None,
            7,
            True,
            ["/"],
            {"path": str(self.home)},
            "",
            "Work/new place",  # relative
            "~",
            str(afile),
            str(self.home / "nowhere"),
            str(self.chosen) + "\0",
            "\0",
            "\ud800",  # not text a file name can be
        ):
            with self.subTest(value=value):
                self.rewrite(lambda record, value=value: record.update(chosen_folder=value))
                self.rewrite(lambda record, value=value: record.update(cwd_local=value))
                self.assertEqual(self.folder(), self.mapped)
                # One of the two is enough.
                self.rewrite(lambda record: record.update(cwd_local=str(self.chosen)))
                self.assertEqual(self.folder()[0], str(self.chosen))

    def test_a_record_for_another_native_store_is_not_this_copys(self):
        for value in (str(self.home / ".claude-old"), None, 7):
            self.rewrite(lambda record, value=value: record.update(destination_store=value))
            self.assertEqual(self.folder(), self.mapped)
        self.rewrite(lambda record: record.pop("destination_store"))
        self.assertEqual(self.folder(), self.mapped)

    def test_a_record_without_the_fields_or_of_another_kind(self):
        for change in (
            lambda record: [record.pop(key) for key in ("chosen_folder", "cwd_local")],
            lambda record: record.update(record="other"),
            lambda record: record.update(provider=7),
            lambda record: record.pop("id"),
            lambda record: record.update(id=CLAUDE_WIN),  # of a session that is not this one
        ):
            before = self.journal.read_bytes()
            self.rewrite(change)
            self.assertEqual(self.folder(), self.mapped)
            self.journal.write_bytes(before)
        self.assertEqual(self.folder()[0], str(self.chosen))

    def test_a_journal_that_cannot_be_used_is_no_failure_and_no_warning(self):
        good = self.journal.read_bytes()
        cases = {
            "torn last line": good + b'{"record": "import", "provi',
            "lines that are no records": b'[1, 2]\n"text"\nnot json\n\xff\xfe\n' + good,
            "nothing but noise": b"\x00\x01\x02 not a journal\n",
            "empty": b"",
        }
        expected = {"nothing but noise": self.mapped, "empty": self.mapped}
        with self.assertNoLogs("agent_sessions", "WARNING"):
            for name, data in cases.items():
                with self.subTest(name):
                    self.journal.write_bytes(data)
                    wanted = expected.get(name, (str(self.chosen), True, str(self.chosen)))
                    for _again in range(2):
                        self.assertEqual(self.folder(), wanted)
            # No journal at all, and something that cannot be read as one.
            self.journal.unlink()
            self.assertEqual(self.folder(), self.mapped)
            self.journal.mkdir()
            self.assertEqual(self.folder(), self.mapped)
            api.scan()
            self.assertEqual(self.folder(), self.mapped)
        # The reader that is meant to say so still does.
        self.journal.rmdir()
        self.journal.write_bytes(cases["torn last line"])
        with self.assertLogs("agent_sessions", "WARNING") as logged:
            self.assertEqual(len(journal.read()), 1)
        self.assertIn("the last line is incomplete", logged.output[0])

    def test_an_old_record_file_that_cannot_be_read_is_passed_over_quietly(self):
        old = self.journal.with_name("imports.json")
        for text in ("{not json", '{"imports": 7}', "[" * 100_000):
            old.write_text(text, encoding="utf-8")
            with self.assertNoLogs("agent_sessions", "WARNING"):
                self.assertEqual(self.folder()[0], str(self.chosen))


if __name__ == "__main__":
    unittest.main()
