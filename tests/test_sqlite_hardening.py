"""SQLite files from another drive: the connection is hardened before any
statement reads them, a schema with more than ordinary tables is refused,
and a damaged database is one skipped candidate with a warning.
docs/REVIEW.md R-08.

Every damaged database is made from a fixture, in a temp dir. Should one of
them ever take the interpreter down instead of raising, this suite dies with
it: that is a finding to report, not something to work around."""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import AGY_NO_CWD, AGY_WIN, World
from support import FIXTURES

from agent_sessions import api
from agent_sessions.model import Store
from agent_sessions.providers import base
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import collect_warnings, open_sqlite_ro

AGY = FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli"
GOOD_DB = AGY / "conversations" / f"{AGY_WIN}.db"
REPO = Path(__file__).resolve().parent.parent
SEED = 20261012
# Python 3.12 and later can set and read SQLite's defensive mode.
DEFENSIVE = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
NEW = "0f0f0f0f-1111-4222-8333-444444444444"
# The body of a view that stands in for the ``steps`` table: one that
# answers, and one that never ends (that one is only ever read in a child
# process, under a timeout).
MADE_UP = "SELECT 1 AS idx, 14 AS step_type, NULL AS metadata, NULL AS step_payload"
ENDLESS = (
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
    "SELECT n AS idx, 14 AS step_type, NULL AS metadata, NULL AS step_payload FROM r"
)


def edit(path, *statements, writable_schema=False) -> None:
    """Change a copy of a fixture database with plain SQLite, then leave it
    as one file (no ``-wal``, no ``-shm``)."""
    con = sqlite3.connect(path)
    try:
        if writable_schema:
            con.execute("pragma writable_schema = on")
        for statement in statements:
            con.execute(statement)
        con.commit()
        con.execute("pragma wal_checkpoint(truncate)")
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(f"{path}{suffix}")


def page_size(data: bytes) -> int:
    return int.from_bytes(data[16:18], "big")


def truncated(data: bytes) -> bytes:
    """Cut in the middle of the third page."""
    size = page_size(data)
    return data[: 2 * size + size // 3]


def noise(seed: int, count: int) -> bytes:
    """``count`` bytes that look random and are the same on every run."""
    return hashlib.shake_256(f"agent-sessions damaged database {seed}".encode()).digest(count)


def scrambled(data: bytes) -> bytes:
    """The 100-byte header as it is, every byte after it random."""
    return data[:100] + noise(SEED, len(data) - 100)


def scrambled_pages(data: bytes) -> bytes:
    """The first page (header and schema) as it is, every later page random."""
    size = page_size(data)
    return data[:size] + noise(SEED, len(data) - size)


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def copy(self, name="c.db") -> Path:
        path = self.tmp / name
        shutil.copy(GOOD_DB, path)
        return path


class HardenTest(TempCase):
    def settings(self, con) -> dict:
        found = {
            name: con.execute(f"pragma {name}").fetchone()[0]
            for name in ("query_only", "trusted_schema", "cell_size_check")
        }
        if DEFENSIVE is not None:
            found["defensive"] = con.getconfig(DEFENSIVE)
        return found

    def expected(self) -> dict:
        wanted = {"query_only": 1, "trusted_schema": 0, "cell_size_check": 1}
        if DEFENSIVE is not None:
            wanted["defensive"] = True
        return wanted

    def test_both_ways_of_opening_are_hardened(self):
        path = self.copy()
        with contextlib.closing(open_sqlite_ro(path)) as con:
            self.assertEqual(con.execute("pragma database_list").fetchall()[0][2], "")  # in memory
            self.assertEqual(self.settings(con), self.expected())
            self.assertEqual(con.execute("select count(*) from steps").fetchone(), (8,))
        with (
            mock.patch.object(base, "SQLITE_MEMORY_MAX", 1024),  # too large: opened in place
            contextlib.closing(open_sqlite_ro(path)) as con,
        ):
            self.assertNotEqual(con.execute("pragma database_list").fetchall()[0][2], "")
            self.assertEqual(self.settings(con), self.expected())
            self.assertEqual(con.execute("select count(*) from steps").fetchone(), (8,))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["c.db"])

    def test_nothing_runs_on_the_file_before_the_settings_are_made(self):
        """In memory, the settings are in place before the bytes are loaded;
        in place, before the first statement that reads the schema."""
        events: list[str] = []

        class Recording(sqlite3.Connection):
            def execute(self, sql, *args):
                events.append(sql.split("=")[0].strip().lower())
                return super().execute(sql, *args)

            def deserialize(self, *args, **kwargs):
                events.append("deserialize")
                return super().deserialize(*args, **kwargs)

            def setconfig(self, *args):
                events.append("setconfig")
                return super().setconfig(*args)

        real = sqlite3.connect
        path = self.copy()
        with mock.patch.object(
            base.sqlite3,
            "connect",
            lambda target, *a, **k: real(target, *a, factory=Recording, **k),
        ):
            open_sqlite_ro(path).close()
            loaded = events.index("deserialize")
            first_read = next(i for i, e in enumerate(events) if e.startswith("select"))
            for setting in ("pragma query_only", "pragma trusted_schema", "pragma cell_size_check"):
                self.assertLess(events.index(setting), loaded, events)
            self.assertEqual(events[0], "pragma query_only")
            if hasattr(sqlite3.Connection, "setconfig"):
                self.assertLess(events.index("setconfig"), loaded, events)
            self.assertLess(loaded, first_read)
            events.clear()
            with mock.patch.object(base, "SQLITE_MEMORY_MAX", 1024):
                open_sqlite_ro(path).close()
            first_read = next(i for i, e in enumerate(events) if e.startswith("select"))
            self.assertEqual(events[0], "pragma query_only")
            self.assertLess(events.index("pragma cell_size_check"), first_read, events)
            self.assertLess(events.index("pragma trusted_schema"), first_read, events)

    def test_no_python_without_setconfig_is_left_out(self):
        """Python 3.11 has no ``Connection.setconfig``: the rest still applies."""

        class Old(sqlite3.Connection):
            setconfig = None

        con = sqlite3.connect(":memory:", factory=Old)
        base._harden(con)
        self.assertEqual(con.execute("pragma query_only").fetchone(), (1,))
        self.assertEqual(con.execute("pragma trusted_schema").fetchone(), (0,))
        con.close()


class PlainSchemaTest(TempCase):
    def refused(self, path) -> str:
        for limit in (base.SQLITE_MEMORY_MAX, 1024):  # in memory, then in place
            with (
                mock.patch.object(base, "SQLITE_MEMORY_MAX", limit),
                self.assertRaises(sqlite3.DatabaseError) as caught,
            ):
                open_sqlite_ro(path)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [path.name])
        return str(caught.exception)

    WHY = "the schema holds a view, a trigger or a virtual table; not read"

    def test_a_view_and_a_trigger(self):
        path = self.copy()
        edit(
            path,
            "CREATE VIEW recent AS SELECT * FROM steps",
            "CREATE TRIGGER audit AFTER INSERT ON steps BEGIN SELECT 1; END",
        )
        self.assertEqual(self.refused(path), self.WHY)

    def test_a_view_alone_and_a_trigger_alone(self):
        for name, statement in (
            ("view.db", "CREATE VIEW recent AS SELECT * FROM steps"),
            ("trigger.db", "CREATE TRIGGER audit AFTER INSERT ON steps BEGIN SELECT 1; END"),
        ):
            path = self.copy(name)
            edit(path, statement)
            self.assertEqual(self.refused(path), self.WHY)
            os.unlink(path)

    def test_a_table_replaced_by_a_view_of_the_same_name(self):
        path = self.copy()
        edit(path, "DROP TABLE steps", f"CREATE VIEW steps AS {ENDLESS}")
        self.assertEqual(self.refused(path), self.WHY)

    def test_a_view_that_calls_itself_a_table(self):
        """The ``type`` column of the schema table is whatever the file
        says. A current SQLite notices that it disagrees with the SQL text
        beside it; whatever the reason given, the database is not read."""
        path = self.copy()
        edit(path, "DROP TABLE steps", f"CREATE VIEW steps AS {ENDLESS}")
        edit(
            path,
            "UPDATE sqlite_master SET type = 'table' WHERE name = 'steps'",
            writable_schema=True,
        )
        if sqlite3.sqlite_version_info >= (3, 37):
            self.assertIn(self.refused(path), (self.WHY, "malformed database schema (steps)"))

    def test_a_virtual_table(self):
        path = self.copy()
        try:
            edit(path, "CREATE VIRTUAL TABLE search USING fts5(body)")
        except sqlite3.Error:
            self.skipTest("this SQLite has no fts5")
        if sqlite3.sqlite_version_info >= (3, 37):
            self.assertEqual(self.refused(path), self.WHY)

    def test_the_check_can_be_left_out_for_a_database_known_to_hold_more(self):
        path = self.copy()
        edit(path, "CREATE VIEW recent AS SELECT * FROM steps")
        with contextlib.closing(open_sqlite_ro(path, plain_schema=False)) as con:
            self.assertEqual(con.execute("select count(*) from recent").fetchone(), (8,))

    def test_every_fixture_the_adapters_read_is_plain(self):
        checked = 0
        for path in sorted(FIXTURES.rglob("*.db")):
            open_sqlite_ro(path).close()
            checked += 1
        self.assertGreaterEqual(checked, 6)


class DamagedDatabaseTest(TempCase):
    """Through ``open_sqlite_ro`` and a query: only the errors a caller is
    told to catch."""

    def read(self, data: bytes, name="d.db"):
        path = self.tmp / name
        path.write_bytes(data)
        outcomes = []
        for limit in (base.SQLITE_MEMORY_MAX, 1024):
            with mock.patch.object(base, "SQLITE_MEMORY_MAX", limit):
                try:
                    with contextlib.closing(open_sqlite_ro(path)) as con:
                        con.execute("SELECT count(*) FROM steps").fetchone()
                        con.execute("SELECT metadata FROM steps ORDER BY idx").fetchall()
                    outcomes.append("read")
                except base.SQLITE_ERRORS as exc:
                    outcomes.append(type(exc).__name__)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [name])
        return outcomes

    def test_truncated_and_scrambled(self):
        good = GOOD_DB.read_bytes()
        self.assertEqual(self.read(good), ["read", "read"])
        for damage in (truncated, scrambled, scrambled_pages):
            outcomes = self.read(damage(good))
            self.assertNotIn("read", outcomes, damage.__name__)

    def test_random_damage_never_raises_anything_else(self):
        good = GOOD_DB.read_bytes()
        seen = set()
        for seed in range(60):
            changes = (1, 8, 200)[seed % 3]
            stream = noise(SEED + seed, 4 * changes)
            data = bytearray(good)
            for at in range(0, len(stream), 4):
                data[int.from_bytes(stream[at : at + 3], "big") % len(data)] = stream[at + 3]
            seen.update(self.read(bytes(data)))
        self.assertTrue(
            seen <= {"read", "DatabaseError", "OperationalError", "UnicodeDecodeError"}, seen
        )


class DamagedStoreScanTest(unittest.TestCase):
    """A scan of a foreign Antigravity store: the damaged database is one
    skipped candidate with a warning, the others are listed as before."""

    def setUp(self):
        self.w = World(self, foreign=("antigravity",))
        self.store = self.w.profile / ".gemini" / "antigravity-cli"
        self.before = sorted(s.id for s in self.w.scan().sessions)
        self.assertIn(AGY_WIN, self.before)

    def scan_with(self, name: str, make) -> list[str]:
        """Scan with ``conversations/<NEW>.db`` made by ``make``; return
        the warnings about it."""
        path = self.store / "conversations" / f"{NEW}.db"
        make(path)
        summary = api.scan(deep=True)
        self.assertEqual(sorted(s.id for s in api.load_index().sessions), self.before, name)
        mine = [w for w in summary["warnings"] if str(path) in w]
        self.assertEqual(len(mine), 1, (name, summary["warnings"]))
        self.assertIn("conversation database not readable", mine[0])
        self.assertEqual(
            sorted(p.name for p in path.parent.iterdir() if p.name.startswith(NEW)), [path.name]
        )
        os.unlink(path)
        return mine

    def test_every_kind_of_damage_is_one_skipped_candidate(self):
        good = GOOD_DB.read_bytes()

        def schema(*statements, lie=None):
            def make(path):
                shutil.copy(GOOD_DB, path)
                edit(path, *statements)
                if lie:
                    edit(path, lie, writable_schema=True)

            return make

        cases = {
            "truncated mid-page": lambda path: path.write_bytes(truncated(good)),
            "header kept, the rest random": lambda path: path.write_bytes(scrambled(good)),
            "first page kept, later pages random": lambda path: path.write_bytes(
                scrambled_pages(good)
            ),
            "a view and a trigger": schema(
                "CREATE VIEW recent AS SELECT * FROM steps",
                "CREATE TRIGGER audit AFTER INSERT ON steps BEGIN SELECT 1; END",
            ),
            "steps replaced by a view": schema(
                "DROP TABLE steps", f"CREATE VIEW steps AS {MADE_UP}"
            ),
        }
        if sqlite3.sqlite_version_info >= (3, 37):
            cases["steps replaced by a view called a table"] = schema(
                "DROP TABLE steps",
                f"CREATE VIEW steps AS {MADE_UP}",
                lie="UPDATE sqlite_master SET type = 'table' WHERE name = 'steps'",
            )
        for name, make in cases.items():
            with self.subTest(name=name):
                self.scan_with(name, make)

    def test_a_damaged_summaries_index_does_not_hide_the_conversations(self):
        summaries = self.store / "conversation_summaries.db"
        for name, damage in (("truncated", truncated), ("scrambled", scrambled)):
            summaries.write_bytes(damage((AGY / "conversation_summaries.db").read_bytes()))
            summary = api.scan(deep=True)
            listed = sorted(s.id for s in api.load_index().sessions)
            self.assertIn(AGY_WIN, listed, name)
            self.assertIn(AGY_NO_CWD, listed, name)
            mine = sorted({w for w in summary["warnings"] if str(summaries) in w})
            self.assertEqual(len(mine), 1, summary["warnings"])
            self.assertIn("conversation summaries not readable", mine[0])
        shutil.copy(AGY / "conversation_summaries.db", summaries)
        edit(summaries, "CREATE VIEW v AS SELECT * FROM conversation_summaries")
        summary = api.scan(deep=True)
        self.assertTrue(any("holds a view" in w for w in summary["warnings"]), summary["warnings"])

    def test_the_transcript_of_a_database_damaged_after_the_scan(self):
        index = self.w.scan()
        session = self.w.session(AGY_NO_CWD, "foreign", index)
        db = Path(next(f for f in session.files if f.endswith(".db")))
        log = Antigravity._log(db.parent.parent, db.stem)
        if log.exists():
            os.unlink(log)  # so that the steps table is what gets read
        for damage in (truncated, scrambled):
            db.write_bytes(damage(GOOD_DB.read_bytes()))
            self.assertEqual(list(api.transcript(session)), [])

    @unittest.skipUnless(hasattr(os, "fork"), "runs the scan in a child, under a timeout")
    def test_a_view_that_never_ends_cannot_hang_the_scan(self):
        """Before the schema check, reading ``steps`` ran the view."""
        path = self.store / "conversations" / f"{NEW}.db"
        shutil.copy(GOOD_DB, path)
        edit(path, "DROP TABLE steps", f"CREATE VIEW steps AS {ENDLESS}")
        try:
            done = subprocess.run(
                [sys.executable, str(REPO / "bin" / "agent-sessions"), "scan", "--deep", "--json"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("the scan did not finish: it is running the view")
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertIn("holds a view", done.stdout)


class AdapterTest(TempCase):
    def test_the_adapter_reports_and_goes_on(self):
        root = self.tmp / "antigravity-cli"
        shutil.copytree(AGY, root)
        good = sorted(s.id for s in Antigravity().list_sessions(Store("antigravity", str(root))))
        bad = root / "conversations" / f"{NEW}.db"
        bad.write_bytes(truncated(GOOD_DB.read_bytes()))
        with collect_warnings() as seen:
            found = Antigravity().list_sessions(Store("antigravity", str(root)))
        self.assertEqual(sorted(s.id for s in found), good)
        self.assertEqual(len(seen.items), 1)
        self.assertTrue(seen.items[0].startswith(f"{bad}: conversation database not readable: "))


if __name__ == "__main__":
    unittest.main()
