"""The experimental import of an Antigravity conversation: a plain copy,
exactly as the table of docs/FORMATS.md "Phase 2 findings" Q5 says (which
files, in which order, every collision, every precondition), and only when
it is asked for. The machine is the one of tests/fixture_world.py."""

import contextlib
import io
import os
import shutil
import sqlite3
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_NO_CWD,
    AGY_WIN,
    CLAUDE_WIN,
    GEMINI_WIN,
    World,
    age_access_times,
    files_below,
    tree_state,
)
from support import state_names

from agent_sessions import api, cli, importer, journal
from agent_sessions.model import make_key
from agent_sessions.providers import antigravity as agy_mod
from agent_sessions.providers import gemini as gemini_mod
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import ExperimentalOnly, NotSupported

AGY_HEADLESS = "1410b3e9-0fcb-4606-81b1-301f7a833e1a"  # its recorded folder does not exist
LOG = ".system_generated/logs/transcript.jsonl"
SIDECARS = ("-wal", "-shm", "-journal")


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


def copied_files(session_id) -> list[str]:
    """What an import puts into the native store for one conversation."""
    return [
        f"annotations/{session_id}.pbtxt",
        f"brain/{session_id}/{LOG}",
        f"conversations/{session_id}.db",
    ]


class AgyImportCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("antigravity",)
    native: tuple[str, ...] = ()

    def setUp(self):
        self.w = World(self, self.foreign, self.native)
        self.home = self.w.home
        self.src = self.w.profile / ".gemini" / "antigravity-cli"
        self.dst = self.home / ".gemini" / "antigravity-cli"
        self.index = self.w.scan()

    def source(self, session_id=AGY_WIN):
        return self.w.session(session_id, "foreign")

    def refused(self, ref, **kwargs) -> str:
        kwargs.setdefault("experimental", True)
        with self.assertRaises(api.ImportFailed) as caught:
            api.import_session(ref, **kwargs)
        self.assertEqual(api.can_import(ref, **kwargs), (False, caught.exception.reason))
        return caught.exception.reason

    def edit(self, path, sql, params=()):
        """Change a database of the fake drive, leaving it as it is at
        rest: no sidecars."""
        con = sqlite3.connect(str(path))
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()
        self.assertEqual([p for p in (f"{path}-wal", f"{path}-shm") if os.path.exists(p)], [])

    def assert_nothing_written(self):
        self.assertFalse((self.home / ".gemini").exists())
        self.assertEqual(state_names(), [])

    def sidecars(self, root) -> list[str]:
        return [rel for rel in files_below(root) if rel.endswith(SIDECARS)]


class OnlyWhenAskedForTest(AgyImportCase):
    foreign = ("antigravity", "claude", "gemini")

    def test_without_the_flag_it_is_refused_and_the_refusal_says_what_the_flag_is_for(self):
        for session_id in (AGY_WIN, AGY_NO_CWD, AGY_HEADLESS):
            source = self.source(session_id)
            for kwargs in ({}, {"experimental": False}, {"target": str(self.home)}):
                reason = self.refused(source, **{"experimental": False, **kwargs})
                self.assertEqual(reason, agy_mod.EXPERIMENTAL_ONLY)
            with self.assertRaises(api.ImportFailed) as caught:
                api.import_plan(source)
            self.assertEqual(caught.exception.reason, agy_mod.EXPERIMENTAL_ONLY)
        self.assertIn("(import --experimental)", agy_mod.EXPERIMENTAL_ONLY)
        self.assertIn("has been resumed by hand", agy_mod.EXPERIMENTAL_ONLY)
        self.assertIn("starts a new conversation without an error", agy_mod.EXPERIMENTAL_ONLY)
        self.assert_nothing_written()

    def test_the_adapter_itself_refuses_whoever_calls_it(self):
        source = self.source()
        provider = Antigravity()
        for call in (provider.import_plan, provider.import_session):
            with self.assertRaises(ExperimentalOnly) as caught:
                call(source, self.index.pathmap)
            self.assertEqual(
                (caught.exception.reason, caught.exception.warning),
                (agy_mod.EXPERIMENTAL_ONLY, agy_mod.IMPORT_WARNING),
            )
            self.assertIsInstance(caught.exception, NotSupported)
        self.assertIn("Experimental", agy_mod.IMPORT_WARNING)
        self.assertIn("has been resumed by hand", agy_mod.IMPORT_WARNING)
        self.assertIn("starts a new conversation without an error", agy_mod.IMPORT_WARNING)
        self.assert_nothing_written()

    def test_the_flag_changes_nothing_for_the_other_providers(self):
        claude = api.import_session(self.w.session(CLAUDE_WIN, "foreign"), experimental=True)
        self.assertEqual((claude.origin, claude.id), ("native", CLAUDE_WIN))
        (record,) = journal.read()
        self.assertNotIn("experimental", record)
        # Gemini is refused all the same: no CLI and no resumable data.
        gemini = self.w.session(GEMINI_WIN, "foreign")
        self.assertEqual(self.refused(gemini), gemini_mod.IMPORT_UNSAFE)
        self.assertEqual(self.refused(gemini, experimental=False), gemini_mod.IMPORT_UNSAFE)


class CopyTest(AgyImportCase):
    def test_exactly_the_three_pieces_byte_for_byte_and_the_source_untouched(self):
        age_access_times(self.w.c)
        before = tree_state(self.w.c)
        source = self.source()
        self.assertEqual(api.can_import(source, experimental=True), (True, ""))
        self.assert_nothing_written()

        native = api.import_session(source, experimental=True)

        self.assertEqual(tree_state(self.w.c), before, "source times, sizes and access times")
        self.assertEqual(self.sidecars(self.w.c), [])
        # Nothing but the conversation: no index, no lock, no cache, no history.
        self.assertEqual(files_below(self.dst), copied_files(AGY_WIN))
        self.assertEqual(sorted(os.listdir(self.dst)), ["annotations", "brain", "conversations"])
        self.assertEqual(sorted(os.listdir(self.home / ".gemini")), ["antigravity-cli"])
        for rel in copied_files(AGY_WIN):
            self.assertEqual(read(self.dst / rel), read(self.src / rel), rel)
            copy, original = os.stat(self.dst / rel), os.stat(self.src / rel)
            self.assertEqual(copy.st_mtime_ns, original.st_mtime_ns, rel)
            self.assertEqual(stat.S_IMODE(copy.st_mode), 0o600, rel)
        self.assertEqual(stat.S_IMODE(os.stat(self.dst / "brain" / AGY_WIN).st_mode), 0o700)

        self.assertEqual(
            (native.origin, native.provider, native.id, native.launchable),
            ("native", "antigravity", AGY_WIN, True),
        )
        self.assertEqual(native.key, make_key("antigravity", self.dst, AGY_WIN))
        self.assertEqual(
            native.files,
            [
                str(self.dst / "conversations" / f"{AGY_WIN}.db"),
                str(self.dst / "brain" / AGY_WIN),
                str(self.dst / "annotations" / f"{AGY_WIN}.pbtxt"),
            ],
        )
        # The workspace is inside the database: it reads as it was recorded.
        self.assertEqual(
            (native.title, native.messages, native.cwd, native.started),
            (source.title, source.messages, source.cwd, source.started),
        )
        self.assertEqual(native.cwd_local, str(self.w.site))
        index = api.load_index()
        again = self.w.session(AGY_WIN, "foreign", index)
        self.assertEqual(again.imported_as, native.key)
        self.assertEqual(index.where(again), "C: · imported")
        plan = api.open_session(native, dry_run=True)
        self.assertEqual(plan.command[-3:], ["agy", "--conversation", AGY_WIN])
        # Reading the copy left no trace beside it either.
        self.assertEqual(self.sidecars(self.home), [])

    def test_the_database_arrives_last(self):
        plan = Antigravity().import_plan(self.source(), self.index.pathmap, experimental=True)
        self.assertEqual(
            [(unit.is_dir, os.path.relpath(unit.dst, self.dst)) for unit in plan.units],
            [
                (True, f"brain/{AGY_WIN}"),
                (False, f"annotations/{AGY_WIN}.pbtxt"),
                (False, f"conversations/{AGY_WIN}.db"),
            ],
        )
        self.assertTrue(all(item.rewrite is None for unit in plan.units for item in unit.files))
        self.assertEqual(plan.appends, [])
        placed = []
        real = importer._place

        def place(target, dst, is_dir):
            # When a piece goes in, the database is not there yet.
            placed.append((os.path.relpath(dst, self.dst), self.sidecars(self.dst)))
            self.assertFalse(
                (self.dst / "conversations" / f"{AGY_WIN}.db").exists(), "the .db goes last"
            )
            return real(target, dst, is_dir)

        with mock.patch.object(importer, "_place", place):
            api.import_session(self.source(), experimental=True)
        self.assertEqual(
            [rel for rel, _ in placed],
            [f"brain/{AGY_WIN}", f"annotations/{AGY_WIN}.pbtxt", f"conversations/{AGY_WIN}.db"],
        )

    def test_the_record_says_it_was_experimental(self):
        source = self.source()
        native = api.import_session(source, experimental=True)
        (record,) = journal.read()
        self.assertEqual(
            {key: record[key] for key in ("provider", "id", "source_key", "native_key")},
            {
                "provider": "antigravity",
                "id": AGY_WIN,
                "source_key": source.key,
                "native_key": native.key,
            },
        )
        self.assertIs(record["experimental"], True)
        self.assertEqual((record["rewritten"], record["appended"]), ({}, []))
        self.assertEqual(
            record["destination_files"],
            [
                str(self.dst / "brain" / AGY_WIN),
                str(self.dst / "annotations" / f"{AGY_WIN}.pbtxt"),
                str(self.dst / "conversations" / f"{AGY_WIN}.db"),
            ],
        )
        self.assertEqual(
            [item["path"] for item in record["manifest"]["files"]],
            [str(self.dst / rel) for rel in copied_files(AGY_WIN)],
        )
        # Every directory was made by this import, the store itself included.
        self.assertIn(str(self.home / ".gemini"), record["manifest"]["dirs"])
        self.assertIn(str(self.dst / "conversations"), record["manifest"]["dirs"])
        self.assertEqual(record["bytes"], sum(item["size"] for item in record["manifest"]["files"]))

    def test_a_second_import_reports_the_copy(self):
        first = api.import_report(self.source(), experimental=True)
        before = tree_state(self.dst)
        for kwargs in ({"experimental": True}, {}):  # the copy is there: nothing to ask for
            report = api.import_report(self.source(), **kwargs)
            self.assertEqual((report.created, report.session), (False, first.session))
            self.assertEqual(api.can_import(self.source(), **kwargs), (False, "already imported"))
        self.assertEqual(tree_state(self.dst), before)
        self.assertEqual(len(journal.read()), 1)

    def test_no_index_of_the_native_store_is_written(self):
        self.w = None  # a machine whose native store the tool has used
        w = World(self, ("antigravity",), ("antigravity",))
        dst = w.home / ".gemini" / "antigravity-cli"
        w.scan()
        kept = {rel: read(dst / rel) for rel in files_below(dst)}
        self.assertIn("conversation_summaries.db", kept)
        age_access_times(dst)
        before = tree_state(dst)
        native = api.import_session(w.session(AGY_WIN, "foreign"), experimental=True)
        self.assertEqual(native.origin, "native")
        after = tree_state(dst)
        for path, state in before.items():
            if os.path.basename(path) not in ("annotations", "brain", "conversations"):
                self.assertEqual(after[path], state, path)
        self.assertEqual(sorted(set(files_below(dst)) - set(kept)), copied_files(AGY_WIN))
        for rel, data in kept.items():
            self.assertEqual(read(dst / rel), data, rel)
        self.assertEqual([rel for rel in files_below(dst) if rel.endswith(SIDECARS)], [])


class NoFolderNeededTest(AgyImportCase):
    def test_a_conversation_without_a_folder_imports(self):
        for session_id, cwd in (
            (AGY_NO_CWD, None),  # none recorded
            (AGY_HEADLESS, "C:\\Users\\user\\sub1\\sub2\\sub3\\sub4"),  # recorded, and gone
        ):
            source = self.source(session_id)
            self.assertEqual((source.cwd, source.cwd_local), (cwd, None))
            plan = api.import_plan(source, experimental=True)
            self.assertEqual(
                (plan["cwd"], plan["cwd_local"], plan["chosen_folder"]), (cwd, None, None)
            )
            native = api.import_session(source, experimental=True)
            self.assertEqual((native.id, native.launchable), (session_id, True))
            # The folder of the headless one was known from the tool's cache
            # file alone (``last_conversations.json``), which is never copied.
            self.assertIsNone(native.cwd)
            record = journal.read()[-1]
            self.assertEqual(
                (record["cwd"], record["cwd_local"], record["chosen_folder"]), (cwd, None, None)
            )
        self.assertEqual(
            files_below(self.dst), sorted(copied_files(AGY_NO_CWD) + copied_files(AGY_HEADLESS))
        )

    def test_a_folder_that_is_found_is_noted(self):
        api.import_session(self.source(AGY_WIN), experimental=True)
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd"], record["cwd_local"], record["chosen_folder"]),
            ("F:/Proj/My Site", str(self.w.site), None),
        )

    def test_a_chosen_folder_is_recorded_and_changes_nothing_in_the_copy(self):
        chosen = self.home / "somewhere else"
        chosen.mkdir()
        api.import_session(self.source(AGY_NO_CWD), experimental=True, target=str(chosen))
        (record,) = journal.read()
        self.assertEqual(
            (record["cwd"], record["cwd_local"], record["chosen_folder"]),
            (None, str(chosen), str(chosen)),
        )
        self.assertEqual(files_below(self.dst), copied_files(AGY_NO_CWD))
        for rel in copied_files(AGY_NO_CWD):
            self.assertEqual(read(self.dst / rel), read(self.src / rel), rel)
        self.assertEqual(os.listdir(chosen), [])

    def test_a_chosen_folder_is_still_checked(self):
        reason = self.refused(self.source(AGY_WIN), target="not/absolute")
        self.assertIn("must be an absolute path", reason)
        self.assert_nothing_written()


class CollisionTest(AgyImportCase):
    def taken(self) -> dict[str, bool]:
        """Every native path whose presence refuses the import: relative
        path -> is it a directory."""
        return {
            f"conversations/{AGY_WIN}.db": False,
            f"conversations/{AGY_WIN}.db-wal": False,
            f"conversations/{AGY_WIN}.pb": False,  # the older form of a conversation
            f"brain/{AGY_WIN}": True,
            f"annotations/{AGY_WIN}.pbtxt": False,
            f"presence/{AGY_WIN}.lock": False,  # the id was opened in this store before
        }

    def test_any_trace_of_the_id_in_the_native_store_refuses_the_import(self):
        source = self.source()
        journal_file = Path(os.environ["AGENT_SESSIONS_STATE"]) / "imports.jsonl"
        for rel, is_dir in self.taken().items():
            with self.subTest(rel=rel):
                path = self.dst / rel
                self.addCleanup(shutil.rmtree, self.home / ".gemini", True)
                path.parent.mkdir(parents=True)
                if is_dir:
                    path.mkdir()
                    (path / "mine.txt").write_text("keep", encoding="utf-8")
                else:
                    path.write_bytes(b"mine")
                before = {rel: read(self.dst / rel) for rel in files_below(self.dst)}
                try:
                    reason = self.refused(source)
                    self.assertEqual(
                        reason, f"already in the native store: {path}; nothing was overwritten"
                    )
                    plan = Antigravity().import_plan(source, self.index.pathmap, experimental=True)
                    with self.assertRaises(importer.Exists) as caught:
                        importer.check(plan)
                    self.assertEqual(caught.exception.paths, [str(path)])
                    after = {rel: read(self.dst / rel) for rel in files_below(self.dst)}
                    self.assertEqual(after, before, "nothing added or changed")
                    self.assertFalse(journal_file.exists())
                finally:
                    shutil.rmtree(self.home / ".gemini")

    def test_an_empty_lock_file_counts_and_so_does_a_dangling_link(self):
        lock = self.dst / "presence" / f"{AGY_WIN}.lock"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b"")  # as the tool leaves it
        self.assertIn(str(lock), self.refused(self.source()))
        lock.unlink()
        link = self.dst / "brain" / AGY_WIN
        link.parent.mkdir()
        os.symlink(self.home / "nowhere", link)
        self.assertIn(str(link), self.refused(self.source()))
        self.assertTrue(os.path.islink(link))

    def test_the_sidecars_of_another_conversation_do_not_count(self):
        other = self.dst / "conversations" / f"{AGY_NO_CWD}.db-wal"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"x")
        (self.dst / "conversations" / f"{AGY_WIN}.db-shm").write_bytes(b"x")  # not in the table
        native = api.import_session(self.source(), experimental=True)
        self.assertEqual(native.id, AGY_WIN)


class PreconditionTest(AgyImportCase):
    def db(self, session_id=AGY_WIN) -> Path:
        return self.src / "conversations" / f"{session_id}.db"

    def test_a_write_ahead_log_that_is_not_empty_is_refused(self):
        source = self.source()
        wal = Path(f"{self.db()}-wal")
        wal.write_bytes(b"\x37\x7f\x06\x82 frames")
        reason = self.refused(source)
        self.assertIn("has a write-ahead log that is not empty", reason)
        self.assertIn(str(wal), reason)
        self.assert_nothing_written()
        # Something else under that name is not an empty log either.
        wal.unlink()
        wal.mkdir()
        self.assertIn("write-ahead log", self.refused(source))
        wal.rmdir()
        os.symlink(self.db(), wal)
        self.assertIn("write-ahead log", self.refused(source))
        self.assert_nothing_written()

    @unittest.skipIf(os.geteuid() == 0, "root can look at everything")
    def test_a_store_whose_conversations_cannot_be_looked_at(self):
        source = self.source()
        conversations = self.src / "conversations"
        os.chmod(conversations, 0)
        self.addCleanup(os.chmod, conversations, 0o700)
        self.assertEqual(self.refused(source), f"cannot read {self.db()}-wal: Permission denied")
        os.chmod(conversations, 0o700)
        self.assert_nothing_written()

    def test_an_empty_one_is_the_state_at_rest_and_no_sidecar_is_ever_copied(self):
        source = self.source()
        for suffix in SIDECARS:
            Path(f"{self.db()}{suffix}").write_bytes(b"" if suffix == "-wal" else b"index")
        native = api.import_session(source, experimental=True)
        self.assertEqual(native.id, AGY_WIN)
        self.assertEqual(files_below(self.dst), copied_files(AGY_WIN))

    def test_a_sub_agent_conversation_is_refused(self):
        source = self.source()
        summaries = self.src / "conversation_summaries.db"
        for column, value in (("parent_conversation_id", AGY_NO_CWD), ("nesting_depth", 1)):
            with self.subTest(column=column):
                self.edit(
                    summaries,
                    f"UPDATE conversation_summaries SET {column} = ? WHERE conversation_id = ?",
                    (value, AGY_WIN),
                )
                self.assertIn("is a sub-agent conversation", self.refused(source))
                self.edit(
                    summaries,
                    f"UPDATE conversation_summaries SET {column} = ? WHERE conversation_id = ?",
                    ("" if column == "parent_conversation_id" else 0, AGY_WIN),
                )
        self.assertEqual(api.can_import(source, experimental=True), (True, ""))
        # The conversation's own table of parents says it too.
        columns = [row[1] for row in self.rows(self.db(), "PRAGMA table_info(parent_references)")]
        self.assertTrue(columns)
        marks = ", ".join("?" for _ in columns)
        self.edit(
            self.db(), f"INSERT INTO parent_references VALUES ({marks})", [None] * len(columns)
        )
        self.assertIn("is a sub-agent conversation", self.refused(source))
        self.assert_nothing_written()

    def rows(self, path, sql) -> list:
        con = sqlite3.connect(str(path))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_a_conversation_with_no_steps_is_refused(self):
        source = self.source()
        self.edit(self.db(), "DELETE FROM steps")
        self.assertEqual(
            self.refused(source), "this conversation has no steps in its database file"
        )
        self.assert_nothing_written()

    def test_a_database_that_cannot_be_read_is_refused(self):
        source = self.source()
        for damage in ("truncated", "not a database", "missing"):
            with self.subTest(damage=damage):
                data = read(self.db())
                if damage == "truncated":
                    self.db().write_bytes(data[:5000])
                elif damage == "not a database":
                    self.db().write_bytes(b"x" * len(data))
                else:
                    self.db().unlink()
                self.assertEqual(
                    self.refused(source),
                    f"the conversation database cannot be read: {self.db()}",
                )
                self.db().write_bytes(data)
        self.assert_nothing_written()

    def test_a_database_with_a_trigger_or_a_view_is_refused(self):
        source = self.source()
        self.edit(self.db(), "CREATE VIEW sneaky AS SELECT 1")
        self.assertIn("the conversation database cannot be read", self.refused(source))
        self.assert_nothing_written()

    def test_a_conversation_of_the_ide_store_is_refused(self):
        ide = self.w.profile / ".gemini" / "antigravity-ide"
        os.rename(self.db(), ide / "conversations" / f"{AGY_WIN}.db")
        index = self.w.scan()
        (session,) = [s for s in index.sessions if s.store == str(ide)]
        self.assertEqual((session.id, session.note), (AGY_WIN, agy_mod.IDE_NOTE))
        self.assertIn("is an Antigravity IDE conversation", self.refused(session))
        self.assert_nothing_written()

    def test_the_file_must_be_named_after_the_conversation(self):
        source = self.source()
        for files, needle in (
            ([], "has no database file to copy"),
            ([str(self.db(AGY_NO_CWD))], "is not named after its conversation id"),
            ([str(self.src / "conversations" / "x.db")], "is not named after its conversation id"),
        ):
            source.files = files
            with self.assertRaises(NotSupported) as caught:
                Antigravity().import_plan(source, self.index.pathmap, experimental=True)
            self.assertIn(needle, caught.exception.reason)
        self.assert_nothing_written()

    def test_what_is_not_there_is_not_copied(self):
        shutil.rmtree(self.src / "brain" / AGY_WIN)
        (self.src / "annotations" / f"{AGY_WIN}.pbtxt").unlink()
        self.w.scan()
        native = api.import_session(self.source(), experimental=True)
        self.assertEqual(files_below(self.dst), [f"conversations/{AGY_WIN}.db"])
        self.assertEqual(native.files, [str(self.dst / "conversations" / f"{AGY_WIN}.db")])
        self.assertEqual(sorted(os.listdir(self.dst)), ["conversations"])

    def test_links_and_special_files_are_left_out(self):
        outside = self.w.c / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        brain = self.src / "brain" / AGY_WIN
        os.symlink(outside, brain / "link.txt")
        os.symlink(self.w.c, brain / "loop")
        annotation = self.src / "annotations" / f"{AGY_WIN}.pbtxt"
        annotation.unlink()
        os.symlink(outside, annotation)  # a link under the annotation's name
        self.w.scan()
        api.import_session(self.source(), experimental=True)
        self.assertEqual(
            files_below(self.dst), [f"brain/{AGY_WIN}/{LOG}", f"conversations/{AGY_WIN}.db"]
        )
        # And a brain directory that is itself a link is not followed.
        shutil.rmtree(self.home / ".gemini")
        shutil.rmtree(Path(os.environ["AGENT_SESSIONS_STATE"]), ignore_errors=True)
        moved = self.w.c / "elsewhere"
        os.rename(brain, moved)
        os.symlink(moved, brain)
        self.w.scan()
        api.import_session(self.source(), experimental=True)
        self.assertEqual(files_below(self.dst), [f"conversations/{AGY_WIN}.db"])


class ReadBackTest(AgyImportCase):
    def test_the_copy_is_read_back_like_any_conversation(self):
        provider = Antigravity()
        source = self.source()
        plan = provider.import_plan(source, self.index.pathmap, experimental=True)
        with self.assertRaises(importer.ImportFailed):
            provider.read_back(plan)  # nothing is there yet
        importer.execute(plan)
        copy = provider.read_back(plan)
        self.assertEqual(
            (copy.id, copy.title, copy.messages), (AGY_WIN, source.title, source.messages)
        )
        self.assertEqual(set(copy.files), set(plan.destinations))
        self.assertEqual(self.sidecars(self.dst), [])

    def test_a_copy_that_does_not_read_back_is_taken_out_again(self):
        real = importer._copy

        def damaged(item, target, tick):
            real(item, target, tick)
            if item.src.endswith(".db"):
                with open(target, "r+b") as fh:  # the database arrives cut short
                    fh.truncate(4096)

        with (
            mock.patch.object(importer, "_copy", damaged),
            self.assertRaises(api.ImportFailed) as caught,
        ):
            api.import_session(self.source(), experimental=True)
        self.assertIn(f"the copy does not read as session {AGY_WIN}", caught.exception.reason)
        self.assert_nothing_written()
        api.import_session(self.source(), experimental=True)  # and it works afterwards
        self.assertEqual(len(journal.read()), 1)

    def test_a_failure_half_way_leaves_nothing_behind(self):
        real, calls = importer._place, []

        def failing(src, dst, is_dir):
            calls.append(os.path.relpath(dst, self.dst))
            if len(calls) == 3:  # brain and annotation are in, the database is not
                raise OSError(28, "No space left on device", dst)
            return real(src, dst, is_dir)

        with mock.patch.object(importer, "_place", failing), self.assertRaises(api.ImportFailed):
            api.import_session(self.source(), experimental=True)
        self.assertEqual(len(calls), 3)
        self.assert_nothing_written()


class WhatTheBrowserShowsTest(AgyImportCase):
    """The TUI asks the engine and shows its answer (``tui/engine.py``,
    ``tui/view.py``; neither needs Textual). Its own prompt for an
    experimental import comes later: until then ``i`` on an Antigravity row
    ends in the refusal, which names the flag."""

    def test_the_detail_pane_and_the_import_key_show_the_refusal(self):
        from agent_sessions.tui import engine as engine_mod
        from agent_sessions.tui import view

        engine = engine_mod.Engine()
        source = self.source()
        verdict = engine.can_import(source, index=self.index)
        self.assertEqual(verdict, (False, agy_mod.EXPERIMENTAL_ONLY))
        line = view.verdict_text(source, verdict)
        self.assertEqual(line, f"not possible: {agy_mod.EXPERIMENTAL_ONLY}")
        self.assertIn("--experimental", line)
        # What `i`, then `y`, does: the engine's import, and its reason in the status line.
        with self.assertRaises(api.ImportFailed) as caught:
            engine.import_session(source.key, progress=None, index=self.index)
        self.assertEqual(view.error_text(caught.exception), agy_mod.EXPERIMENTAL_ONLY)
        self.assert_nothing_written()


class ExperimentalCommandTest(AgyImportCase):
    def test_import_without_the_flag_exits_one_and_names_it(self):
        for extra in ((), ("--dry-run",), ("--open",)):
            code, out, err = run("import", AGY_WIN, *extra)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(err, f"agent-sessions: {agy_mod.EXPERIMENTAL_ONLY}\n")
        self.assertIn("--experimental", run("import", "--help")[1])
        self.assert_nothing_written()

    def test_dry_run_with_the_flag(self):
        code, out, _ = run("import", AGY_NO_CWD, "--experimental", "--dry-run")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[1], "project folder: not recorded (none is needed)")
        self.assertEqual(
            [line.split(" -> ")[1].split(" (")[0] for line in lines if line.startswith("copy ")],
            [
                f"{self.dst / 'brain' / AGY_NO_CWD}/",
                str(self.dst / "annotations" / f"{AGY_NO_CWD}.pbtxt"),
                str(self.dst / "conversations" / f"{AGY_NO_CWD}.db"),
            ],
        )
        self.assertNotIn("rewrite", out)
        self.assertEqual(lines[-1], "dry run: nothing was written")
        code, out, _ = run("import", AGY_HEADLESS, "--experimental", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(
            "project folder: C:\\Users\\user\\sub1\\sub2\\sub3\\sub4 (not found; none is needed)\n",
            out,
        )
        self.assert_nothing_written()

    def test_import_with_the_flag(self):
        code, out, err = run("import", AGY_WIN, "--experimental")
        native = self.w.session(AGY_WIN, "native")
        self.assertEqual((code, out), (0, native.key + "\n"))
        self.assertIn("imported 3 items (", err)
        self.assertIn(f"into {self.dst}", err)
        self.assertEqual(files_below(self.dst), copied_files(AGY_WIN))
        self.assertEqual(
            run("import", AGY_WIN),
            (0, native.key + "\n", "agent-sessions: already imported; nothing was copied\n"),
        )


if __name__ == "__main__":
    unittest.main()
