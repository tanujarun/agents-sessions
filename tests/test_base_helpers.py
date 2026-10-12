"""The helpers adapters share: JSONL, SQLite, timestamps, titles, warnings."""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES  # noqa: E402

from agent_sessions.model import Candidate, Session, Store, make_key  # noqa: E402
from agent_sessions.providers import base  # noqa: E402
from agent_sessions.providers.base import (  # noqa: E402
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
        """The reason for immutable=1 (FORMATS "Rules common to all providers")."""
        path = self.make_wal_db()
        con = sqlite3.connect(f"file:{base.urllib.parse.quote(str(path))}?mode=ro", uri=True)
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
            open_sqlite_ro(path).execute("select v from t").fetchall()
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

    def test_large_file_falls_back_to_the_immutable_open(self):
        path = self.make_wal_db()
        with mock.patch.object(base, "SQLITE_MEMORY_MAX", 1024):  # the file is larger
            con = open_sqlite_ro(path)
        self.assertFalse(self.in_memory(con))
        self.assertEqual(con.execute("select v from t").fetchall(), [("row",)])
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("insert into t values ('x')")
        con.close()
        self.assertEqual(self.siblings(path), [path.name])

    def test_failed_load_falls_back_to_the_immutable_open(self):
        path = self.make_wal_db()

        class NoDeserialize(sqlite3.Connection):
            def deserialize(self, *args, **kwargs):
                raise sqlite3.OperationalError("deserialize is not available")

        real = sqlite3.connect
        with mock.patch.object(
            base.sqlite3,
            "connect",
            lambda target, *a, **k: real(
                target,
                *a,
                **dict(k, **({"factory": NoDeserialize} if target == ":memory:" else {})),
            ),
        ):
            con = open_sqlite_ro(path)
        self.assertFalse(self.in_memory(con))
        self.assertEqual(con.execute("select v from t").fetchall(), [("row",)])
        con.close()
        self.assertEqual(self.siblings(path), [path.name])

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
                try:
                    con = open_sqlite_ro(path)
                    rows = con.execute("select name from sqlite_master").fetchall()
                    con.close()
                except sqlite3.Error:
                    rows = None  # as before: the error comes on open or first query
                self.assertIn(rows, (None, []))
                self.assertEqual(path.read_bytes(), content)
                self.assertFalse((self.tmp / (name + "-wal")).exists())
                self.assertFalse((self.tmp / (name + "-shm")).exists())

    def test_every_fixture_database_reads_the_same_both_ways(self):
        """The in-memory copy and the immutable open see the same rows."""
        checked = 0
        for path in sorted(FIXTURES.rglob("*")):
            if path.suffix not in (".db", ".sqlite") or not path.is_file():
                continue
            before = sorted(p.name for p in path.parent.iterdir())
            uri = "file:" + base.urllib.parse.quote(str(path)) + "?mode=ro&immutable=1"
            dumps = []
            for con in (open_sqlite_ro(path), sqlite3.connect(uri, uri=True)):
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
        self.assertEqual(parse_ts(0), "1970-01-01T00:00:00.000000Z")

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
