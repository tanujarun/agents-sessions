"""The helpers adapters share: JSONL, SQLite, timestamps, titles, warnings."""

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES

from agent_sessions.model import Candidate, Session, Store, make_key
from agent_sessions.providers import base
from agent_sessions.providers.base import (
    clean_title,
    collect_warnings,
    iter_jsonl,
    open_ro,
    open_sqlite_ro,
    parse_ts,
    warn,
)


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agent-sessions-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class IterJsonlTest(TempCase):
    def write(self, data: bytes) -> Path:
        path = self.tmp / "t.jsonl"
        path.write_bytes(data)
        return path

    def test_plain_lines(self):
        path = self.write(b'{"a": 1}\n{"a": 2}\n')
        self.assertEqual(list(iter_jsonl(path)), [{"a": 1}, {"a": 2}])
        self.assertEqual(list(iter_jsonl(str(path))), [{"a": 1}, {"a": 2}])

    def test_truncated_last_line_is_skipped(self):
        path = self.write(b'{"a": 1}\n{"a": 2, "text": "half wri')
        self.assertEqual(list(iter_jsonl(path)), [{"a": 1}])

    def test_last_line_without_newline_is_read(self):
        path = self.write(b'{"a": 1}\n{"a": 2}')
        self.assertEqual(list(iter_jsonl(path)), [{"a": 1}, {"a": 2}])

    def test_crlf_blank_and_bom(self):
        path = self.write(b'\xef\xbb\xbf{"a": 1}\r\n\r\n   \r\n\n{"a": 2}\r\n')
        self.assertEqual(list(iter_jsonl(path)), [{"a": 1}, {"a": 2}])

    def test_bad_utf8_is_replaced_not_fatal(self):
        path = self.write(b'{"a": "caf\xe9"}\n{"b": "ok"}\n')
        self.assertEqual(list(iter_jsonl(path)), [{"a": "caf�"}, {"b": "ok"}])

    def test_unparseable_and_non_object_lines_are_skipped(self):
        path = self.write(b'not json\n[1, 2]\n"text"\n7\n{"a": 1}\n{"a": \n')
        self.assertEqual(list(iter_jsonl(path)), [{"a": 1}])

    def test_embedded_carriage_return_kept_inside_strings(self):
        path = self.write(b'{"a": "x\\ry"}\r\n')
        self.assertEqual(list(iter_jsonl(path)), [{"a": "x\ry"}])

    def test_is_lazy(self):
        path = self.write(b'{"a": 1}\n' * 3)
        it = iter_jsonl(path)
        self.assertEqual(next(it), {"a": 1})
        it.close()

    def test_missing_file_warns_and_yields_nothing(self):
        missing = self.tmp / "nope.jsonl"
        with collect_warnings() as got:
            self.assertEqual(list(iter_jsonl(missing)), [])
        self.assertEqual(len(got.items), 1)
        self.assertTrue(got.items[0].startswith(f"{missing}: cannot read"))
        self.assertEqual(list(iter_jsonl(missing)), [])  # no collector: still quiet

    def test_reading_leaves_the_access_time_alone(self):
        path = self.write(b'{"a": 1}\n')
        os.utime(path, ns=(1_000_000_000, 2_000_000_000_000_000_000))
        list(iter_jsonl(path))
        with open_ro(path) as fh:
            fh.read()
        if getattr(os, "O_NOATIME", 0):
            self.assertEqual(os.stat(path).st_atime_ns, 1_000_000_000)


class SqliteTest(TempCase):
    def make_wal_db(self) -> Path:
        path = self.tmp / "dir with space#1" / "state 100%.sqlite"
        path.parent.mkdir()
        con = sqlite3.connect(path)
        con.execute("pragma journal_mode=wal")
        con.execute("create table t (v text)")
        con.execute("insert into t values ('row')")
        con.commit()
        con.close()  # checkpoints and removes the sidecars
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(20)[18], 2, "fixture must be a WAL-mode file")
        return path

    def siblings(self, path: Path) -> list[str]:
        return sorted(p.name for p in path.parent.iterdir())

    def test_open_creates_no_sidecars(self):
        path = self.make_wal_db()
        self.assertEqual(self.siblings(path), [path.name])
        before = os.stat(path)
        con = open_sqlite_ro(path)
        self.assertEqual(con.execute("select v from t").fetchall(), [("row",)])
        self.assertEqual(self.siblings(path), [path.name])
        con.close()
        self.assertEqual(self.siblings(path), [path.name])
        after = os.stat(path)
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))

    def test_plain_ro_would_create_sidecars(self):
        """The reason SQLite itself never opens a store's file (FORMATS
        "Rules common to all providers")."""
        path = self.make_wal_db()
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
        con.execute("select v from t").fetchall()
        made = self.siblings(path)
        con.close()
        if path.name + "-shm" not in made:
            self.skipTest("this SQLite build leaves no sidecar for mode=ro")

    def test_connection_is_read_only(self):
        con = open_sqlite_ro(self.make_wal_db())
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("insert into t values ('x')")
        con.close()

    def test_missing_file_raises_and_is_not_created(self):
        missing = self.tmp / "missing.sqlite"
        with self.assertRaises(sqlite3.Error):
            open_sqlite_ro(missing).execute("select 1 from sqlite_master").fetchall()
        self.assertFalse(missing.exists())

    def test_fixture_db_opens_without_sidecars(self):
        path = FIXTURES / "codex/native/.codex/state_5.sqlite"
        before = sorted(p.name for p in path.parent.iterdir())
        con = open_sqlite_ro(path)
        self.assertEqual(con.execute("select count(*) from threads").fetchone(), (2,))
        con.close()
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), before)

    # --- loaded into memory (the file itself is never opened by SQLite) ------

    def in_memory(self, con) -> bool:
        """True for an in-memory connection: its main database has no file."""
        return con.execute("pragma database_list").fetchall()[0][2] == ""

    def test_wal_file_is_loaded_into_memory_and_left_alone(self):
        path = self.make_wal_db()
        os.utime(path, ns=(1_000_000_000, os.stat(path).st_mtime_ns))
        before = os.stat(path)
        raw = path.read_bytes()
        os.utime(path, ns=(1_000_000_000, before.st_mtime_ns))
        self.assertEqual(raw[18:20], b"\x02\x02")
        con = open_sqlite_ro(path)
        self.assertTrue(self.in_memory(con))
        self.assertEqual(con.execute("select v from t").fetchall(), [("row",)])
        self.assertEqual(con.execute("select count(*) from sqlite_master").fetchone(), (1,))
        # The journal mode of the copy is not WAL: nothing to look for on disk.
        self.assertNotEqual(con.execute("pragma journal_mode").fetchone()[0], "wal")
        con.close()
        after = os.stat(path)
        self.assertEqual(self.siblings(path), [path.name])
        self.assertEqual((after.st_mtime_ns, after.st_size), (before.st_mtime_ns, before.st_size))
        self.assertEqual(path.read_bytes(), raw)  # the file keeps its WAL header
        if getattr(os, "O_NOATIME", 0):
            os.utime(path, ns=(1_000_000_000, before.st_mtime_ns))
            # Closed here: a connection left to the garbage collector reports
            # itself on stderr whenever that runs, in the middle of another test.
            with contextlib.closing(open_sqlite_ro(path)) as con:
                con.execute("select v from t").fetchall()
            self.assertEqual(os.stat(path).st_atime_ns, 1_000_000_000)

    def test_in_memory_copy_refuses_writes_of_every_kind(self):
        con = open_sqlite_ro(self.make_wal_db())
        self.assertTrue(self.in_memory(con))
        for sql in (
            "insert into t values ('x')",
            "update t set v = 'y'",
            "delete from t",
            "create table u (v)",
            "drop table t",
        ):
            with self.assertRaises(sqlite3.OperationalError):
                con.execute(sql)
        self.assertEqual(con.execute("select v from t").fetchall(), [("row",)])
        con.close()

    def test_rollback_journal_file_loads_too(self):
        path = self.tmp / "plain.sqlite"
        con = sqlite3.connect(path)
        con.execute("create table t (v text)")
        con.execute("insert into t values ('caf\u00e9')")
        con.commit()
        con.close()
        self.assertEqual(path.read_bytes()[18:20], b"\x01\x01")
        con = open_sqlite_ro(path)
        self.assertTrue(self.in_memory(con))
        self.assertEqual(con.execute("select v from t").fetchone(), ("caf\u00e9",))
        con.close()

    def test_a_file_too_large_to_load_is_not_opened_at_all(self):
        """AUDIT A-16: a database over the limit used to be opened in place
        by SQLite, which moved its access time and gave out values of any
        size. Nothing of it is read now."""
        path = self.make_wal_db()
        os.utime(path, ns=(1_000_000_000, os.stat(path).st_mtime_ns))
        reads = []

        class Counting(io.BufferedReader):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)

        real = base.open_ro
        with (
            mock.patch.object(base, "SQLITE_MEMORY_MAX", 1024),  # the file is larger
            mock.patch.object(base, "open_ro", lambda p: Counting(real(p).detach())),
            self.assertRaises(sqlite3.DataError) as caught,
        ):
            open_sqlite_ro(path)
        self.assertEqual(
            str(caught.exception),
            "larger than 1.0 kB, which no conversation database is; not opened",
        )
        self.assertEqual(reads, [])
        self.assertEqual(self.siblings(path), [path.name])
        if getattr(os, "O_NOATIME", 0):
            self.assertEqual(os.stat(path).st_atime_ns, 1_000_000_000)
        # At the limit exactly it is read.
        with mock.patch.object(base, "SQLITE_MEMORY_MAX", os.path.getsize(path)):
            open_sqlite_ro(path).close()
        self.assertEqual(base.SQLITE_MEMORY_MAX, 256 * 1024 * 1024)

    def test_a_file_that_grows_past_the_limit_while_it_is_read(self):
        path = self.make_wal_db()
        data = path.read_bytes()
        empty = self.tmp / "empty"
        empty.write_bytes(b"")

        class Growing(io.BytesIO):
            """Its size was nothing when it was asked; then it has it all."""

            def fileno(self):
                return self.small.fileno()

        with open(empty, "rb") as small:
            Growing.small = small
            with (
                mock.patch.object(base, "SQLITE_MEMORY_MAX", len(data) - 1),
                mock.patch.object(base, "open_ro", lambda p: Growing(data)),
                self.assertRaises(sqlite3.DataError) as caught,
            ):
                open_sqlite_ro(path)
        self.assertIn("not opened", str(caught.exception))

    def test_sqlite_never_opens_the_file_itself(self):
        """One ``connect`` in the module, and it is the one in memory: no
        ``immutable=1`` open is left to fall back to."""
        source = Path(base.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("sqlite3.connect("), 1)
        self.assertIn('sqlite3.connect(":memory:")', source)
        self.assertNotIn("uri=True", source)
        opened = []
        real = sqlite3.connect
        good = self.make_wal_db()
        with mock.patch.object(
            base.sqlite3, "connect", lambda target, *a, **k: opened.append(target) or real(target)
        ):
            open_sqlite_ro(good).close()
            for name in ("missing.sqlite", "text.db"):
                with self.assertRaises(sqlite3.Error):
                    open_sqlite_ro(self.tmp / name)
                (self.tmp / "text.db").write_bytes(b"not a database " * 40)
        self.assertEqual(opened, [":memory:"])

    def test_a_load_that_fails_is_an_error_not_another_way_in(self):
        path = self.make_wal_db()

        class NoDeserialize(sqlite3.Connection):
            def deserialize(self, *args, **kwargs):
                raise sqlite3.OperationalError("deserialize is not available")

        real = sqlite3.connect
        with (
            mock.patch.object(
                base.sqlite3, "connect", lambda target, *a, **k: real(target, factory=NoDeserialize)
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            open_sqlite_ro(path)
        with (
            mock.patch.object(base.sqlite3, "Connection", object),  # a build without deserialize
            self.assertRaises(sqlite3.NotSupportedError),
        ):
            open_sqlite_ro(path)
        self.assertEqual(self.siblings(path), [path.name])

    def test_a_value_over_the_limit_fails_the_query_that_meets_it(self):
        """AUDIT A-16: a 300 MB value in one row cost the scan 625 MB."""
        path = self.tmp / "big.sqlite"
        con = sqlite3.connect(path)
        con.execute("create table t (id integer primary key, v blob)")
        con.execute("insert into t values (1, zeroblob(2000)), (2, zeroblob(100))")
        con.commit()
        con.close()
        with mock.patch.object(base, "SQLITE_VALUE_MAX", 1000):
            con = open_sqlite_ro(path)
        self.assertEqual(con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 1000)
        self.assertEqual(con.execute("select length(v) from t where id = 2").fetchone(), (100,))
        self.assertEqual(len(con.execute("select v from t where id = 2").fetchone()[0]), 100)
        with self.assertRaises(sqlite3.DataError) as caught:
            con.execute("select v from t where id = 1").fetchone()
        self.assertIn("too big", str(caught.exception))
        self.assertIsInstance(caught.exception, base.SQLITE_ERRORS)
        con.close()
        with contextlib.closing(open_sqlite_ro(path)) as con:  # the real limit: 16 MiB
            self.assertEqual(con.getlimit(sqlite3.SQLITE_LIMIT_LENGTH), 16 * 1024 * 1024)
            self.assertEqual(len(con.execute("select v from t where id = 1").fetchone()[0]), 2000)

    def test_files_that_are_not_databases(self):
        for name, content in (
            ("empty.db", b""),
            ("short.db", b"SQLite format 3\x00"),
            ("text.db", b"not a database " * 40),
            ("cut.db", self.make_wal_db().read_bytes()[:600]),
        ):
            path = self.tmp / name
            path.write_bytes(content)
            with self.subTest(name=name):
                with self.assertRaises(sqlite3.DatabaseError):
                    open_sqlite_ro(path)
                self.assertEqual(path.read_bytes(), content)
                self.assertFalse((self.tmp / (name + "-wal")).exists())
                self.assertFalse((self.tmp / (name + "-shm")).exists())

    def test_every_fixture_database_reads_as_sqlite_itself_reads_it(self):
        """The in-memory copy holds the rows a plain immutable open of the
        file sees (that open is this test's own; the package has none)."""
        checked = 0
        for path in sorted(FIXTURES.rglob("*")):
            if path.suffix not in (".db", ".sqlite") or not path.is_file():
                continue
            before = sorted(p.name for p in path.parent.iterdir())
            uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
            dumps = []
            # Grok's search index holds FTS virtual tables: the schema check is left out.
            for con in (open_sqlite_ro(path, plain_schema=False), sqlite3.connect(uri, uri=True)):
                tables = [
                    row[0]
                    for row in con.execute(
                        "select name from sqlite_master where type = 'table' order by name"
                    )
                ]
                # FTS shadow tables need their module; plain tables are compared in full.
                dump = {}
                for table in tables:
                    try:
                        dump[table] = con.execute(f'select * from "{table}"').fetchall()
                    except sqlite3.Error as exc:
                        dump[table] = type(exc).__name__
                dumps.append(dump)
                con.close()
            self.assertEqual(dumps[0], dumps[1], path)
            self.assertTrue(dumps[0], path)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), before)
            checked += 1
        self.assertGreaterEqual(checked, 8)


class ParseTsTest(unittest.TestCase):
    def test_every_recorded_form(self):
        cases = {
            "2026-10-03T14:25:33.100Z": "2026-10-03T14:25:33.100000Z",  # ms
            "2026-09-27T23:35:21.831252584Z": "2026-09-27T23:35:21.831252Z",  # ns
            "2026-08-29T08:31:15.3316788Z": "2026-08-29T08:31:15.331678Z",  # 100 ns
            "2026-08-29 09:11:11.2289146+00:00": "2026-08-29T09:11:11.228914Z",  # SQLite, 7
            "2026-08-29 09:11:11.228914+00:00": "2026-08-29T09:11:11.228914Z",  # SQLite, 6
            "2026-10-03T14:25:33Z": "2026-10-03T14:25:33.000000Z",
            "2026-10-03T14:25:33": "2026-10-03T14:25:33.000000Z",  # no zone = UTC
            "2026-10-03T16:25:33.5+02:00": "2026-10-03T14:25:33.500000Z",
            "2026-10-03T09:55:33-0430": "2026-10-03T14:25:33.000000Z",
            " 2026-10-03T14:25:33.100Z\n": "2026-10-03T14:25:33.100000Z",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_ts(value), expected)

    def test_epoch_numbers(self):
        self.assertEqual(parse_ts(1790000000), "2026-09-21T14:13:20.000000Z")  # s
        self.assertEqual(parse_ts(1790000000123), "2026-09-21T14:13:20.123000Z")  # ms
        self.assertEqual(parse_ts(1790000000.25), "2026-09-21T14:13:20.250000Z")
        self.assertEqual(parse_ts("1790000000"), "2026-09-21T14:13:20.000000Z")
        self.assertEqual(parse_ts(1790000000123456), "2026-09-21T14:13:20.123456Z")  # us
        self.assertEqual(parse_ts(1790000000123456789), "2026-09-21T14:13:20.123456Z")  # ns
        self.assertEqual(parse_ts("1790000000.25"), "2026-09-21T14:13:20.250000Z")

    def test_a_time_outside_1990_to_2100_is_not_a_time(self):
        """docs/REVIEW.md R-11: whatever the form, and whatever unit a
        number was taken for."""
        self.assertEqual(parse_ts("1990-01-01T00:00:00Z"), "1990-01-01T00:00:00.000000Z")
        self.assertEqual(parse_ts("2100-12-31T23:59:59.999999Z"), "2100-12-31T23:59:59.999999Z")
        self.assertEqual(parse_ts(631152000), "1990-01-01T00:00:00.000000Z")
        for value in (
            "1989-12-31T23:59:59.999999Z",
            "2101-01-01T00:00:00Z",
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
            "1990-01-01T00:00:30+00:01",  # the offset is applied first: 1989 in UTC
            "2101-01-01T00:30:00+01:00",  # 2100 in UTC ...
            0,
            1,
            -1,
            631151999,  # one second before 1990
            4133980800,  # 2101-01-01
            -1790000000,
            99999999999,  # seconds: the year 5138
            100000000000,  # milliseconds: 1973
            99999999999999,  # milliseconds: 5138
            100000000000000,  # microseconds: 1973
            99999999999999999,  # microseconds: 5138
            100000000000000000,  # nanoseconds: 1973
            9223372036854775807,  # nanoseconds: 2262
            "0",
            "100000000000",
            (0, 0),
            (-1, 0),
            {1: 4133980800},
            {1: 253402300800, 2: 0},
            (10**18, 0),
        ):
            with self.subTest(value=value):
                if value == "2101-01-01T00:30:00+01:00":
                    self.assertEqual(parse_ts(value), "2100-12-31T23:30:00.000000Z")
                else:
                    self.assertIsNone(parse_ts(value))

    def test_the_guessed_unit_cannot_be_a_wrong_one(self):
        """For a number that is a time between 1990 and 2100 in one unit,
        no other unit gives a time in that range: the magnitude names the
        unit, and the range catches what it gets wrong."""
        scales = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}
        for seconds in (631152000, 1790000000, 4133980799):  # 1990, 2026, the end of 2100
            expected = parse_ts(seconds, unit="s")
            self.assertIsNotNone(expected)
            for unit, scale in scales.items():
                number = seconds * scale
                self.assertEqual(parse_ts(number), expected, (seconds, unit))  # guessed
                self.assertEqual(parse_ts(number, unit=unit), expected)  # told
                for other in scales:
                    if other != unit:
                        self.assertIsNone(parse_ts(number, unit=other), (number, unit, other))

    def test_a_unit_that_the_source_fixes_is_used(self):
        self.assertEqual(parse_ts(1790000000.5, unit="s"), "2026-09-21T14:13:20.500000Z")
        self.assertEqual(parse_ts(1790000000123, unit="ms"), "2026-09-21T14:13:20.123000Z")
        self.assertEqual(parse_ts(1790000000123456, unit="us"), "2026-09-21T14:13:20.123456Z")
        self.assertEqual(parse_ts(1790000000123456789, unit="ns"), "2026-09-21T14:13:20.123456Z")
        self.assertEqual(parse_ts("1790000000123", unit="ms"), "2026-09-21T14:13:20.123000Z")
        # A number that is no time in that unit is none, however plausible in another.
        self.assertIsNone(parse_ts(1790000000123, unit="s"))
        self.assertIsNone(parse_ts(1790000000, unit="ms"))  # 1970-01-21
        # The unit is for numbers; text in a date form is read as before.
        self.assertEqual(parse_ts("2026-10-03T14:25:33Z", unit="s"), "2026-10-03T14:25:33.000000Z")
        self.assertIsNone(parse_ts(None, unit="s"))
        with self.assertRaises(ValueError):
            parse_ts(1790000000, unit="minutes")

    def test_a_files_modification_time_is_seconds(self):
        """Every adapter's fallback time is a file's mtime, and says so."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (1790000000, 1790000000))
            self.assertEqual(base.mtime(str(path)), "2026-09-21T14:13:20.000000Z")
            self.assertEqual(base.mtime(path), "2026-09-21T14:13:20.000000Z")
            self.assertIsNone(base.mtime(path / "missing"))
            # A clock that was at zero when the file was written: no time, not 1970.
            os.utime(path, (5, 5))
            self.assertIsNone(base.mtime(str(path)))
            # A damaged time far in the future is seconds too, and so no
            # time: taken for milliseconds it would read as the year 2026.
            try:
                os.utime(path, (1790000000123, 1790000000123))
            except (OSError, OverflowError):
                return  # this filesystem cannot hold such a time
            if os.stat(path).st_mtime == 1790000000123:
                self.assertIsNone(base.mtime(str(path)))
                self.assertEqual(parse_ts(os.stat(path).st_mtime), "2026-09-21T14:13:20.123000Z")

    def test_the_adapters_share_that_one_helper(self):
        """AUDIT N-17: each of the five had a copy of its own."""
        from agent_sessions.providers import antigravity, claude, codex, gemini, grok

        for module in (antigravity, claude, codex, gemini, grok):
            with self.subTest(module=module.__name__):
                self.assertIs(module.mtime, base.mtime)
                self.assertFalse(hasattr(module, "_mtime"))

    def test_protobuf_pair(self):
        self.assertEqual(parse_ts({1: 1790000000, 2: 987654321}), "2026-09-21T14:13:20.987654Z")
        self.assertEqual(parse_ts({1: 1790000000}), "2026-09-21T14:13:20.000000Z")
        self.assertEqual(parse_ts((1790000000, 5000)), "2026-09-21T14:13:20.000005Z")

    def test_rejects_everything_else(self):
        for value in (
            None,
            True,
            "",
            "yesterday",
            "2026-13-40T00:00:00Z",
            "2026-10-03",
            [],
            {},
            {2: 5},
            (1, 2, 3),
            float("nan"),
            float("inf"),
            10**30,
            b"1",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_ts(value))

    def test_output_sorts_in_time_order(self):
        stamps = [
            "2026-10-03T14:25:33Z",
            "2026-10-03T14:25:33.100Z",
            "2026-10-03 14:25:33.0500000+00:00",
            1790000000,
        ]
        got = [parse_ts(s) for s in stamps]
        self.assertEqual(len({len(g) for g in got}), 1)
        self.assertEqual(sorted(got), [got[3], got[0], got[2], got[1]])


class CleanTitleTest(unittest.TestCase):
    def test_collapses_to_one_line(self):
        self.assertEqual(clean_title("  Fix\tthe \r\n\n  menu   lag \n"), "Fix the menu lag")

    def test_caps_at_80(self):
        self.assertEqual(clean_title("x" * 200), "x" * 80)
        self.assertEqual(len(clean_title("word " * 40)), 79)  # no trailing space
        self.assertEqual(clean_title("y" * 80), "y" * 80)

    def test_none_empty_and_control_characters(self):
        self.assertEqual(clean_title(None), "")
        self.assertEqual(clean_title(" \n "), "")
        self.assertEqual(clean_title("a\x1b[31mb\x00c\x7f"), "a [31mb c")
        self.assertEqual(clean_title(42), "42")


class WarningsTest(unittest.TestCase):
    def test_collects_inside_the_block_only(self):
        warn("dropped")
        with collect_warnings() as got:
            warn("bad line", "/s/a.jsonl")
            warn("no path")
        warn("dropped too")
        self.assertEqual(got.items, ["/s/a.jsonl: bad line", "no path"])

    def test_nested_blocks_do_not_mix(self):
        with collect_warnings() as outer:
            warn("one")
            with collect_warnings() as inner:
                warn("two")
            warn("three")
        self.assertEqual((outer.items, inner.items), (["one", "three"], ["two"]))

    def test_threads_do_not_mix(self):
        seen = []

        def other():
            warn("from the other thread")
            with collect_warnings() as got:
                warn("mine")
            seen.append(got.items)

        with collect_warnings() as got:
            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
        self.assertEqual((got.items, seen), ([], [["mine"]]))


class ModelTest(unittest.TestCase):
    def test_for_store_fills_conversation_fields_and_defaults(self):
        store = Store("claude", "/home/u/.claude", "native", "/home", "Linux")
        s = Session.for_store(
            store,
            id="abc",
            title="T",
            cwd="/home/u/p",
            started="2026-10-01T00:00:00.000000Z",
            updated="2026-10-02T00:00:00.000000Z",
            messages=3,
            files=["/home/u/.claude/projects/x/abc.jsonl"],
            note="n",
        )
        self.assertEqual(s.key, make_key("claude", "/home/u/.claude", "abc"))
        self.assertEqual(
            (s.provider, s.store, s.origin, s.mount, s.mount_label),
            ("claude", "/home/u/.claude", "native", "/home", "Linux"),
        )
        self.assertEqual(
            (s.cwd_local, s.cwd_exists, s.launchable, s.imported_as), (None, False, False, None)
        )
        self.assertEqual((s.title, s.cwd, s.messages, s.note), ("T", "/home/u/p", 3, "n"))

    def test_for_store_minimal(self):
        s = Session.for_store(Store("grok", "/x/.grok"), id="i")
        self.assertEqual(
            (s.title, s.cwd, s.started, s.updated, s.messages, s.files, s.note),
            ("", None, None, None, None, [], None),
        )
        self.assertEqual(s.origin, "foreign")

    def test_from_dict_round_trip(self):
        s = Session.for_store(Store("grok", "/x/.grok"), id="i", files=["a", "b"])
        again = Session.from_dict(dict(s.to_dict(), unknown_key=1))
        self.assertEqual(again, s)
        self.assertIsNot(again.files, s.files)

    def test_candidate(self):
        c = Candidate("id-1", ["/a", "/b"])
        self.assertEqual((c.id_hint, c.paths), ("id-1", ["/a", "/b"]))
        self.assertEqual(Candidate("x").paths, [])


if __name__ == "__main__":
    unittest.main()
