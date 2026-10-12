"""providers/antigravity.py against tests/fixtures and edge cases on temp copies.

Expected values: tests/fixtures/README.md. Rules: docs/FORMATS.md "Antigravity".
One README value is not asserted as written: the cwd of 1ca54b8b is given
there as ``F:\\Proj\\My Site``, while FORMATS section 4 turns the recorded
``file:///F:/Proj/My%20Site`` into ``F:/Proj/My Site`` (forward slashes).
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FIXTURES, Sandbox

from agent_sessions import api
from agent_sessions.model import Candidate, Session, Store
from agent_sessions.providers.antigravity import (
    IDE_NOTE,
    Antigravity,
    _pbtxt_title,
    pb_all,
    pb_fields,
    pb_first,
    pb_text,
    pb_timestamp,
    uri_to_path,
)
from agent_sessions.providers.base import (
    Turn,
    collect_warnings,
    open_sqlite_ro,
    parse_ts,
)

NATIVE = FIXTURES / "antigravity" / "native" / ".gemini" / "antigravity-cli"
WINDOWS = FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli"
IDE = FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-ide"

NATIVE_EMPTY = "d3936295-e998-4e42-bb0d-b8ecbb30fed3"  # excluded: 0 steps
HEADLESS = "1410b3e9-0fcb-4606-81b1-301f7a833e1a"  # cwd from last_conversations.json
WORKSPACE = "1ca54b8b-127f-4f86-9cf4-98d04217f5ce"  # cwd from workspace_uris
NO_CWD = "45184a9e-8a4e-4f3f-8c4c-f0e85aeecaaf"  # no recoverable cwd

# id: (title, cwd, started, updated, messages)
LISTED = {
    HEADLESS: (
        "title-15",
        "C:\\Users\\user\\sub1\\sub2\\sub3\\sub4",
        "2026-08-29T09:14:43.040448Z",
        "2026-08-29T09:15:05.079779Z",
        3,
    ),
    WORKSPACE: (
        "title-1",
        "F:/Proj/My Site",
        "2026-08-29T08:11:33.927210Z",
        "2026-08-29T08:57:26.263722Z",
        7,
    ),
    NO_CWD: ("title-26", None, "2026-08-29T09:10:34.699091Z", "2026-08-29T09:11:11.228914Z", 3),
}


# --- hand-built protobuf ----------------------------------------------------


def varint(number: int) -> bytes:
    out = bytearray()
    number &= 0xFFFFFFFFFFFFFFFF
    while True:
        low = number & 0x7F
        number >>= 7
        out.append(low | (0x80 if number else 0))
        if not number:
            return bytes(out)


def pb_int(field: int, number: int) -> bytes:
    return varint(field << 3) + varint(number)


def pb_bytes(field: int, payload) -> bytes:
    payload = payload.encode() if isinstance(payload, str) else payload
    return varint(field << 3 | 2) + varint(len(payload)) + payload


def pb_time(seconds: int, nanos: int = 0) -> bytes:
    return pb_int(1, seconds) + pb_int(2, nanos)


class ProtobufReaderTest(unittest.TestCase):
    def test_wire_types(self):
        data = (
            b"\x08\x96\x01"  # 1: varint 150
            + b"\x12\x07testing"  # 2: length-delimited
            + b"\x2d\x01\x02\x03\x04"  # 5: fixed32
            + b"\x49\x01\x02\x03\x04\x05\x06\x07\x08"  # 9: fixed64
            + b"\xa2\x06\x02hi"
        )  # 100: two-byte key
        self.assertEqual(
            pb_fields(data),
            [
                (1, 0, 150),
                (2, 2, b"testing"),
                (5, 5, b"\x01\x02\x03\x04"),
                (9, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"),
                (100, 2, b"hi"),
            ],
        )

    def test_varints(self):
        self.assertEqual(pb_fields(b"\x08\x00"), [(1, 0, 0)])
        self.assertEqual(pb_fields(b"\x08\x7f"), [(1, 0, 127)])
        self.assertEqual(pb_fields(b"\x08\x80\x01"), [(1, 0, 128)])
        self.assertEqual(pb_fields(b"\x08" + b"\xff" * 9 + b"\x01"), [(1, 0, 2**64 - 1)])
        self.assertEqual(pb_fields(pb_int(3, 1_787_994_883)), [(3, 0, 1_787_994_883)])

    def test_empty_and_not_bytes(self):
        for value in (b"", None, "text", 5, [1], {"a": 1}):
            self.assertEqual(pb_fields(value), [])
        self.assertEqual(pb_fields(bytearray(b"\x08\x01")), [(1, 0, 1)])
        self.assertEqual(pb_fields(memoryview(b"\x08\x01")), [(1, 0, 1)])

    def test_stops_at_what_it_cannot_read_and_keeps_the_rest(self):
        good = b"\x08\x05"
        for tail in (
            b"\x12\x0aabc",  # length 10, 3 bytes left
            b"\x12",  # key, no length
            b"\x10\x80",  # varint cut off
            b"\x80",  # key cut off
            b"\x2d\x01\x02",  # fixed32 cut off
            b"\x49\x01\x02\x03",  # fixed64 cut off
            b"\x1b\x08\x01\x1c",  # group (wire type 3)
            b"\x00\x01",  # field number 0
            b"\x10" + b"\xff" * 11,  # varint longer than ten bytes
            b"\x0f",
        ):  # wire type 7
            with self.subTest(tail=tail):
                self.assertEqual(pb_fields(good + tail), [(1, 0, 5)])
        self.assertEqual(pb_fields(b"\xff\xff\xff"), [])

    def test_nested_paths_and_repeated_fields(self):
        call = lambda name: pb_bytes(7, pb_bytes(1, "id") + pb_bytes(2, name))  # noqa: E731
        payload = pb_int(1, 15) + pb_bytes(20, pb_bytes(1, "reply") + call("read") + call("grep"))
        self.assertEqual(pb_text(pb_first(payload, 20, 1)), "reply")
        self.assertEqual([pb_text(v) for v in pb_all(payload, 20, 7, 2)], ["read", "grep"])
        self.assertEqual(pb_first(payload, 1), 15)
        self.assertIsNone(pb_first(payload, 20, 9))
        self.assertIsNone(pb_first(payload, 1, 1))  # a varint has no fields
        self.assertEqual(pb_all(payload, 99), [])
        self.assertIsNone(pb_first(None, 1))
        self.assertIsNone(pb_text(15))
        self.assertEqual(pb_text(b"caf\xc3\xa9 \xff"), "café \ufffd")

    def test_timestamp(self):
        self.assertEqual(
            pb_timestamp(pb_time(1_787_994_883, 40_448_000)), (1_787_994_883, 40_448_000)
        )
        self.assertEqual(
            parse_ts(pb_timestamp(pb_time(1_787_994_883, 40_448_000))),
            "2026-08-29T09:14:43.040448Z",
        )
        self.assertEqual(pb_timestamp(pb_int(1, 1_787_994_883)), (1_787_994_883, 0))
        self.assertEqual(pb_timestamp(pb_time(5, 2_000_000_000)), (5, 0))
        self.assertIsNone(pb_timestamp(pb_time(-62_135_596_800)))  # Go's zero time
        self.assertIsNone(pb_timestamp(pb_int(2, 5)))
        self.assertIsNone(pb_timestamp(b""))
        self.assertIsNone(pb_timestamp(None))
        self.assertIsNone(pb_timestamp(pb_time(1_787_994_883)[:3]))  # cut inside the seconds

    def test_fixture_blobs(self):
        con = open_sqlite_ro(WINDOWS / "conversations" / f"{WORKSPACE}.db")
        self.addCleanup(con.close)
        blob = con.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
        self.assertEqual(pb_text(pb_first(blob, 1, 1)), "file:///F:/Proj/My%20Site")
        self.assertEqual(pb_text(pb_first(blob, 7)), "file:///F:/Proj/My%20Site")
        self.assertEqual(pb_text(pb_first(blob, 6)), WORKSPACE)
        self.assertEqual(parse_ts(pb_timestamp(pb_first(blob, 2))), LISTED[WORKSPACE][2])
        metadata = con.execute("SELECT metadata FROM steps ORDER BY idx LIMIT 1").fetchone()[0]
        self.assertEqual(
            parse_ts(pb_timestamp(pb_first(metadata, 1))), "2026-08-29T08:37:05.396470Z"
        )


class SmallValuesTest(unittest.TestCase):
    def test_uri_to_path(self):
        for uri, path in (
            ("file:///F:/AI%20Projects/Website%20Works", "F:/AI Projects/Website Works"),
            ("file:///c:/Users/user", "c:/Users/user"),
            ("file:///home/user/My%20Proj", "/home/user/My Proj"),
            ("file:///home/user/100%25%23x", "/home/user/100%#x"),
            ("file://localhost/home/user", "/home/user"),
            ("file://server/share/dir", "//server/share/dir"),
            ("/already/a/path", "/already/a/path"),
            ("C:\\Users\\user", "C:\\Users\\user"),
            ("vscode-remote://ssh/host/dir", None),
            ("", None),
            ("   ", None),
            (None, None),
            (5, None),
        ):
            with self.subTest(uri=uri):
                self.assertEqual(uri_to_path(uri), path)

    def test_pbtxt_title(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-agy-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "a.pbtxt"
        for raw, title in (
            (b'title:"Plain title"', "Plain title"),
            (b'other: 1\n  title : "second line"\n', "second line"),
            (b'title:"say \\"hi\\" \\\\ \\n\\tend"', 'say "hi" \\ \n\tend'),
            (b'title:"caf\\303\\251 \\x41\\101"', "café AA"),
            (b'title:"caf\xc3\xa9"', "café"),
            (b'title:"odd \\8 \\x"', "odd 8 x"),
            (b'title:""', ""),
            (b"", ""),
            (b"\x00\x01garbage", ""),
            (b'title:"never closed', ""),
        ):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.assertEqual(_pbtxt_title(path), title)
        self.assertEqual(_pbtxt_title(tmp / "missing.pbtxt"), "")


# --- the adapter -------------------------------------------------------------


def fixture_store(root) -> Store:
    return Store("antigravity", str(root), origin="native" if root == NATIVE else "foreign")


def tree_state(root) -> dict:
    """Every path under ``root`` with its mtime and size."""
    state = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            st = os.stat(path)
            state[os.path.relpath(path, root)] = (st.st_mtime_ns, st.st_size)
    return state


def sidecars(root) -> list[str]:
    return sorted(name for name in tree_state(root) if name.endswith(("-wal", "-shm", "-journal")))


class AgyCase(unittest.TestCase):
    def setUp(self):
        self.provider = Antigravity()
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sessions-agy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def copy(self, source=WINDOWS, name=None) -> Store:
        root = self.tmp / (name or source.name)
        shutil.copytree(source, root)
        return Store("antigravity", str(root), origin="foreign")

    def edit(self, path, sql, params=()):
        """Change a copied database, leaving it as it is at rest: no sidecars."""
        con = sqlite3.connect(str(path))
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()
        self.assertEqual([p for p in (f"{path}-wal", f"{path}-shm") if os.path.exists(p)], [])

    def conv(self, store, ident) -> Path:
        return Path(store.root) / "conversations" / f"{ident}.db"

    def candidate(self, store, hint) -> Candidate:
        found = [c for c in self.provider.candidates(store) if c.id_hint == hint]
        self.assertEqual(len(found), 1, hint)
        return found[0]

    def parse(self, store, hint):
        candidate = self.candidate(store, hint)
        with collect_warnings() as collected:
            session = self.provider.parse(store, candidate)
        return session, collected.items

    def listed(self, store, hint) -> Session:
        session, warnings = self.parse(store, hint)
        self.assertEqual(warnings, [])
        self.assertIsNotNone(session)
        return session

    def sources(self, store, hint) -> dict:
        return self.provider._fields(store, self.candidate(store, hint))["sources"]


class CandidatesTest(AgyCase):
    def test_fixture_candidates(self):
        for root, ids in ((NATIVE, [NATIVE_EMPTY]), (WINDOWS, sorted(LISTED)), (IDE, [])):
            with self.subTest(store=root.name):
                found = self.provider.candidates(fixture_store(root))
                self.assertEqual([c.id_hint for c in found], ids)
                for c in found:
                    self.assertEqual(
                        c.paths,
                        [
                            str(root / "conversations" / f"{c.id_hint}.db"),
                            str(root / "conversation_summaries.db"),
                            str(root / "annotations" / f"{c.id_hint}.pbtxt"),
                            str(root / "cache" / "last_conversations.json"),
                            str(
                                root
                                / "brain"
                                / c.id_hint
                                / ".system_generated"
                                / "logs"
                                / "transcript.jsonl"
                            ),
                        ],
                    )

    def test_counts_match_the_readme(self):
        for root, listed, count in ((NATIVE, 0, 1), (WINDOWS, 3, 3), (IDE, 0, 0)):
            store = fixture_store(root)
            self.assertEqual(len(self.provider.candidates(store)), count)
            self.assertEqual(len(self.provider.list_sessions(store)), listed)

    def test_paths_are_listed_even_when_absent(self):
        paths = self.candidate(fixture_store(NATIVE), NATIVE_EMPTY).paths
        self.assertFalse(os.path.exists(paths[2]))  # no annotations/ in the native store
        self.assertFalse(os.path.exists(paths[4]))  # no brain log

    def test_sidecars_lock_files_and_dot_files_are_not_candidates(self):
        store = self.copy()
        conversations = Path(store.root) / "conversations"
        for name in (f"{HEADLESS}.db-wal", f"{HEADLESS}.db-shm", ".gitkeep", "x.db-journal"):
            (conversations / name).write_bytes(b"")
        self.assertEqual([c.id_hint for c in self.provider.candidates(store)], sorted(LISTED))


class FixtureParseTest(AgyCase):
    def test_listed_sessions(self):
        store = fixture_store(WINDOWS)
        sessions = {s.id: s for s in self.provider.list_sessions(store)}
        self.assertEqual(sorted(sessions), sorted(LISTED))
        for ident, (title, cwd, started, updated, messages) in LISTED.items():
            with self.subTest(id=ident):
                s = sessions[ident]
                self.assertEqual((s.title, s.cwd, s.messages), (title, cwd, messages))
                self.assertEqual((s.started, s.updated), (started, updated))
                self.assertEqual(
                    s.files,
                    [
                        str(WINDOWS / "conversations" / f"{ident}.db"),
                        str(WINDOWS / "brain" / ident),
                        str(WINDOWS / "annotations" / f"{ident}.pbtxt"),
                    ],
                )
                self.assertIsNone(s.note)
                self.assertEqual(
                    (s.provider, s.store, s.origin), ("antigravity", str(WINDOWS), "foreign")
                )

    def test_where_cwd_and_title_came_from(self):
        store = fixture_store(WINDOWS)
        self.assertEqual(
            self.sources(store, WORKSPACE),
            {"title": "conversation_summaries.title", "cwd": "workspace_uris"},
        )
        self.assertEqual(self.sources(store, HEADLESS)["cwd"], "last_conversations.json")
        self.assertEqual(self.sources(store, NO_CWD)["cwd"], "none")

    def test_conversation_without_steps_is_excluded_without_a_warning(self):
        self.assertEqual(self.parse(fixture_store(NATIVE), NATIVE_EMPTY), (None, []))

    def test_empty_ide_store_yields_nothing(self):
        store = fixture_store(IDE)
        self.assertEqual(self.provider.candidates(store), [])
        self.assertEqual(self.provider.list_sessions(store), [])

    def test_resume_argv(self):
        s = self.listed(fixture_store(WINDOWS), WORKSPACE)
        self.assertEqual(self.provider.resume_argv(s), ["agy", "--conversation", WORKSPACE])

    def test_import_is_refused_with_the_documented_reason(self):
        """FORMATS section 6. More in tests/test_importer.py."""
        from agent_sessions.providers import antigravity as module
        from agent_sessions.providers.base import NotSupported

        for call in (self.provider.import_plan, self.provider.import_session):
            with self.assertRaises(NotSupported) as caught:
                call(None, None)
            self.assertEqual(caught.exception.reason, module.IMPORT_UNSAFE)


class ReadOnlyTest(AgyCase):
    def test_fixture_databases_are_wal_mode_files_at_rest(self):
        for root in (NATIVE, WINDOWS):
            for path in [
                root / "conversation_summaries.db",
                *(root / "conversations").glob("*.db"),
            ]:
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(20)[18:20], b"\x02\x02", path)
            self.assertEqual(sidecars(root), [])

    def test_reading_leaves_no_sidecar_and_touches_nothing(self):
        for source in (WINDOWS, NATIVE):
            with self.subTest(store=source.parent.parent.name):
                self.setUp()
                store = self.copy(source)
                before = tree_state(store.root)
                self.assertEqual(sidecars(store.root), [])
                with collect_warnings() as collected:
                    candidates = self.provider.candidates(store)
                    sessions = [self.provider.parse(store, c) for c in candidates]
                    sessions = [s for s in sessions if s is not None]
                    for s in sessions:
                        list(self.provider.transcript(s))
                        os.unlink(
                            Path(store.root)
                            / "brain"
                            / s.id
                            / ".system_generated"
                            / "logs"
                            / "transcript.jsonl"
                        )
                        list(self.provider.transcript(s))  # the steps table this time
                self.assertEqual(collected.items, [])
                self.assertEqual(len(sessions), 3 if source == WINDOWS else 0)
                after = tree_state(store.root)
                self.assertEqual(sidecars(store.root), [])
                changed = {k for k in before if before[k] != after.get(k)}
                # only the logs this test removed, and the directories that held them
                self.assertTrue(all("logs" in k for k in changed), changed)
                for name in before:
                    if name.endswith(".db"):
                        self.assertEqual(before[name], after[name], name)

    def test_connections_are_closed(self):
        store = self.copy()
        opened = []
        real = sqlite3.connect

        def connect(*args, **kwargs):
            con = real(*args, **kwargs)
            opened.append(con)
            return con

        with mock.patch.object(sqlite3, "connect", connect):
            sessions = self.provider.list_sessions(store)
            for s in sessions:
                os.unlink(
                    Path(store.root)
                    / "brain"
                    / s.id
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                list(self.provider.transcript(s))
        self.assertEqual(len(sessions), 3)
        self.assertGreaterEqual(len(opened), 9)  # conversation + summaries + steps, each
        for con in opened:
            with self.assertRaises(sqlite3.ProgrammingError):  # closed
                con.execute("SELECT 1")


class DamagedStoreTest(AgyCase):
    def test_garbage_database_is_a_warning(self):
        for data in (
            b"this is not a database, just bytes " * 40,
            b"",
            b"SQLite format 3\x00" + b"\x00" * 200,
        ):
            with self.subTest(size=len(data)):
                self.setUp()
                store = self.copy()
                self.conv(store, HEADLESS).write_bytes(data)
                session, warnings = self.parse(store, HEADLESS)
                self.assertIsNone(session)
                self.assertEqual(len(warnings), 1)
                self.assertIn(f"{HEADLESS}.db", warnings[0])
                self.assertEqual(
                    sorted(s.id for s in self.provider.list_sessions(store)),
                    sorted([WORKSPACE, NO_CWD]),
                )
                self.assertEqual(sidecars(store.root), [])

    def test_database_that_vanished_is_a_warning(self):
        store = self.copy()
        candidate = self.candidate(store, HEADLESS)
        os.unlink(candidate.paths[0])
        with collect_warnings() as collected:
            self.assertIsNone(self.provider.parse(store, candidate))
        self.assertEqual(len(collected.items), 1)

    def test_database_of_another_schema_is_a_warning(self):
        store = self.copy()
        path = self.conv(store, HEADLESS)
        path.unlink()
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE something_else (a)")
        con.commit()
        con.close()
        session, warnings = self.parse(store, HEADLESS)
        self.assertIsNone(session)
        self.assertEqual(len(warnings), 1)
        self.assertIn("steps", warnings[0])

    def test_missing_summaries_database_uses_the_fallbacks(self):
        store = self.copy()
        os.unlink(Path(store.root) / "conversation_summaries.db")
        sessions = {s.id: s for s in self.provider.list_sessions(store)}
        self.assertEqual(sorted(sessions), sorted(LISTED))
        s = sessions[WORKSPACE]
        self.assertEqual(s.title, "title-1")  # annotations/<id>.pbtxt
        self.assertEqual(s.cwd, "F:/Proj/My Site")  # metadata blob field 7
        self.assertEqual(s.started, LISTED[WORKSPACE][2])  # metadata blob field 2
        self.assertEqual(s.updated, "2026-08-29T08:57:22.335249Z")  # last step metadata
        self.assertEqual(s.messages, 7)
        self.assertEqual(
            self.sources(store, WORKSPACE), {"title": "annotations", "cwd": "metadata blob field 7"}
        )
        self.assertEqual(sessions[HEADLESS].cwd, LISTED[HEADLESS][1])  # last_conversations.json
        self.assertIsNone(sessions[NO_CWD].cwd)

    def test_title_fallback_chain(self):
        store = self.copy()
        summaries = Path(store.root) / "conversation_summaries.db"
        self.edit(summaries, "UPDATE conversation_summaries SET title = ''")
        self.assertEqual(self.listed(store, WORKSPACE).title, "title-1")  # annotation
        os.unlink(Path(store.root) / "annotations" / f"{WORKSPACE}.pbtxt")
        self.assertEqual(self.listed(store, WORKSPACE).title, "text-2")  # preview
        self.edit(summaries, "UPDATE conversation_summaries SET preview = ''")
        self.assertEqual(self.listed(store, WORKSPACE).title, "text-3")  # first USER_INPUT
        self.assertEqual(self.sources(store, WORKSPACE)["title"], "first user input")
        shutil.rmtree(Path(store.root) / "brain" / WORKSPACE)
        s = self.listed(store, WORKSPACE)
        self.assertEqual(s.title, WORKSPACE[:8])
        self.assertEqual(s.files, [str(self.conv(store, WORKSPACE))])

    def test_garbage_summaries_database_is_a_warning_and_fallbacks(self):
        store = self.copy()
        (Path(store.root) / "conversation_summaries.db").write_bytes(b"garbage " * 100)
        session, warnings = self.parse(store, WORKSPACE)
        self.assertEqual(
            (session.title, session.cwd, session.messages), ("title-1", "F:/Proj/My Site", 7)
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("conversation_summaries.db", warnings[0])

    def test_conversation_without_a_summaries_row(self):
        store = self.copy()
        self.edit(
            Path(store.root) / "conversation_summaries.db",
            "DELETE FROM conversation_summaries WHERE conversation_id = ?",
            (NO_CWD,),
        )
        s = self.listed(store, NO_CWD)
        self.assertEqual((s.title, s.cwd, s.messages), ("title-26", None, 3))
        self.assertEqual(s.updated, "2026-08-29T09:11:11.117676Z")  # last step metadata

    def test_empty_workspace_uris(self):
        store = self.copy()
        summaries = Path(store.root) / "conversation_summaries.db"
        for value in ("", "[]", "not json", '["vscode-remote://x/y"]', "{}"):
            with self.subTest(workspace_uris=value):
                self.edit(
                    summaries,
                    "UPDATE conversation_summaries SET workspace_uris = ? "
                    "WHERE conversation_id = ?",
                    (value, WORKSPACE),
                )
                s = self.listed(store, WORKSPACE)
                self.assertEqual(s.cwd, "F:/Proj/My Site")
                self.assertEqual(self.sources(store, WORKSPACE)["cwd"], "metadata blob field 7")
        # no workspace_uris and no field 7: last_conversations.json, then nothing
        self.assertEqual(self.listed(store, HEADLESS).cwd, LISTED[HEADLESS][1])
        cache = Path(store.root) / "cache" / "last_conversations.json"
        cache.write_text(json.dumps({"D:\\b": HEADLESS, "D:\\a": HEADLESS, "E:\\other": NO_CWD}))
        self.assertEqual(self.listed(store, HEADLESS).cwd, "D:\\a")  # sorted: deterministic
        self.assertEqual(self.listed(store, NO_CWD).cwd, "E:\\other")
        cache.write_text("{broken")
        session, warnings = self.parse(store, HEADLESS)
        self.assertIsNone(session.cwd)
        self.assertEqual(len(warnings), 1)
        cache.unlink()
        self.assertIsNone(self.listed(store, HEADLESS).cwd)

    def test_native_uri(self):
        store = self.copy(NATIVE)
        db = self.conv(store, NATIVE_EMPTY)
        self.edit(
            db,
            "INSERT INTO steps (idx, step_type, status, metadata) VALUES (0, 14, 3, ?)",
            (pb_bytes(1, pb_time(1_790_000_000, 5_000)),),
        )
        s = self.listed(store, NATIVE_EMPTY)
        self.assertEqual((s.cwd, s.title, s.messages), ("/home/user", NATIVE_EMPTY[:8], 1))
        self.assertEqual(s.started, "2026-09-27T22:36:20.053832Z")
        # last_modified_time is Go's zero time: the last step's time instead
        self.assertEqual(s.updated, "2026-09-21T14:13:20.000005Z")
        self.assertEqual(s.files[0], str(db))

    def test_truncated_protobuf_blobs(self):
        store = self.copy()
        db = self.conv(store, WORKSPACE)
        con = open_sqlite_ro(db)
        blob = con.execute("SELECT data FROM trajectory_metadata_blob").fetchone()[0]
        con.close()
        os.unlink(Path(store.root) / "conversation_summaries.db")
        for cut in range(len(blob)):
            self.edit(db, "UPDATE trajectory_metadata_blob SET data = ?", (blob[:cut],))
            session, warnings = self.parse(store, WORKSPACE)
            self.assertEqual(warnings, [], cut)
            self.assertEqual((session.id, session.messages), (WORKSPACE, 7), cut)
            self.assertIn(session.started, (LISTED[WORKSPACE][2], "2026-08-29T08:37:05.396470Z"))
            self.assertIn(session.cwd, ("F:/Proj/My Site", None), cut)
        # cut before the created timestamp: the first step's time; no field 7: no cwd
        self.edit(db, "UPDATE trajectory_metadata_blob SET data = ?", (blob[:20],))
        s = self.listed(store, WORKSPACE)
        self.assertEqual((s.started, s.cwd), ("2026-08-29T08:37:05.396470Z", None))
        # steps whose metadata is cut or missing: the file's mtime
        self.edit(db, "UPDATE steps SET metadata = ?", (b"\x0a\x0c\x08",))
        self.edit(db, "UPDATE trajectory_metadata_blob SET data = NULL")
        os.utime(db, (1_790_000_000, 1_790_000_000))
        s = self.listed(store, WORKSPACE)
        self.assertEqual((s.started, s.updated), (parse_ts(1_790_000_000),) * 2)
        self.edit(db, "DELETE FROM trajectory_metadata_blob")
        self.assertEqual(self.listed(store, WORKSPACE).messages, 7)

    def test_sub_agent_conversations_are_excluded(self):
        store = self.copy()
        summaries = Path(store.root) / "conversation_summaries.db"
        self.edit(
            summaries,
            "UPDATE conversation_summaries SET parent_conversation_id = ? "
            "WHERE conversation_id = ?",
            (WORKSPACE, HEADLESS),
        )
        self.edit(
            summaries,
            "UPDATE conversation_summaries SET nesting_depth = 1 WHERE conversation_id = ?",
            (NO_CWD,),
        )
        self.assertEqual(self.parse(store, HEADLESS), (None, []))
        self.assertEqual(self.parse(store, NO_CWD), (None, []))
        self.edit(
            self.conv(store, WORKSPACE),
            "INSERT INTO parent_references (idx, data) VALUES (0, x'0a00')",
        )
        self.assertEqual(self.parse(store, WORKSPACE), (None, []))
        self.assertEqual(self.provider.list_sessions(store), [])

    def test_steps_still_in_a_hot_wal_fall_back_to_step_count(self):
        # An immutable read sees only the main file; the index knows better.
        store = self.copy()
        self.edit(self.conv(store, HEADLESS), "DELETE FROM steps")
        s = self.listed(store, HEADLESS)
        self.assertEqual((s.messages, s.title), (4, "title-15"))  # step_count
        self.edit(
            Path(store.root) / "conversation_summaries.db",
            "UPDATE conversation_summaries SET step_count = 0 WHERE conversation_id = ?",
            (HEADLESS,),
        )
        self.assertEqual(self.parse(store, HEADLESS), (None, []))

    def test_messages_count_only_user_and_planner_steps(self):
        store = self.copy()
        self.edit(
            self.conv(store, NO_CWD),
            "INSERT INTO steps (idx, step_type, status) VALUES (9, 132, 3), (10, 99, 3)",
        )
        self.assertEqual(self.listed(store, NO_CWD).messages, 3)


class IdeStoreTest(AgyCase):
    def test_ide_conversations_are_list_only(self):
        store = self.copy(WINDOWS, name="antigravity-ide")
        sessions = self.provider.list_sessions(store)
        self.assertEqual(sorted(s.id for s in sessions), sorted(LISTED))
        self.assertEqual({s.note for s in sessions}, {IDE_NOTE})
        self.assertEqual(IDE_NOTE, "Antigravity IDE conversation: list only")

    def test_unknown_file_format_is_a_warning_not_a_crash(self):
        store = self.copy(IDE)
        conversations = Path(store.root) / "conversations"
        (conversations / "8a21c259-0000-4000-8000-000000000000.pb").write_bytes(b"\x0a\x03abc")
        (conversations / "a-directory").mkdir()
        hints = [c.id_hint for c in self.provider.candidates(store)]
        self.assertEqual(
            hints, ["unknown:8a21c259-0000-4000-8000-000000000000.pb", "unknown:a-directory"]
        )
        with collect_warnings() as collected:
            self.assertEqual(self.provider.list_sessions(store), [])
        self.assertEqual(len(collected.items), 2)
        self.assertTrue(all("unknown conversation file format" in w for w in collected.items))


class TranscriptTest(AgyCase):
    def test_fixture_transcripts_from_the_brain_log(self):
        store = fixture_store(WINDOWS)
        self.assertEqual(
            list(self.provider.transcript(self.listed(store, HEADLESS))),
            [
                Turn("user", "text-17", "2026-08-29T09:14:43.000000Z"),
                Turn("tool", "name-19", "2026-08-29T09:14:43.000000Z"),
                Turn("assistant", "text-25", "2026-08-29T09:15:02.000000Z"),
            ],
        )
        turns = list(self.provider.transcript(self.listed(store, WORKSPACE)))
        self.assertEqual(
            [(t.role, t.text) for t in turns],
            [
                ("user", "text-3"),
                ("tool", "list_dir"),
                ("assistant", "text-9"),
                ("user", "text-11"),
                ("assistant", "text-12"),
                ("user", "text-13"),
                ("assistant", "text-14"),
            ],
        )
        self.assertEqual([t.ts for t in turns], sorted(t.ts for t in turns))
        self.assertEqual(
            [(t.role, t.text) for t in self.provider.transcript(self.listed(store, NO_CWD))],
            [("user", "text-28"), ("tool", "name-19"), ("tool", "name-35")],
        )

    def test_log_content_and_order(self):
        store = self.copy()
        log = (
            Path(store.root)
            / "brain"
            / HEADLESS
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        records = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-08-29T09:00:00Z",
                "content": "<USER_REQUEST>\nmake it blue\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
                "local time\n</ADDITIONAL_METADATA>\n<USER_SETTINGS_CHANGE>\nx\n"
                "</USER_SETTINGS_CHANGE>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-08-29T09:00:01Z",
                "thinking": "private reasoning",
                "content": "On it.",
                "tool_calls": [
                    {"name": "view_file", "args": {"p": "secret"}},
                    {"name": "run_command", "args": {}},
                    {"args": {}},
                ],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "created_at": "2026-08-29T09:00:02Z",
                "content": "tool output",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-08-29T09:00:03Z",
                "thinking": "only thinking",
            },
            {
                "step_index": 4,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-08-29T09:00:04Z",
                "content": "no wrapper",
            },
            {"step_index": 5, "type": "SOMETHING_NEW", "content": "x"},
        ]
        log.write_bytes(
            b"".join(json.dumps(r).encode() + b"\n" for r in records)
            + b'{"step_index": 6, "type": "USER_INPUT", "content": "half'
        )
        s = self.listed(store, HEADLESS)
        self.assertEqual(
            list(self.provider.transcript(s)),
            [
                Turn("user", "make it blue", "2026-08-29T09:00:00.000000Z"),
                Turn("assistant", "On it.", "2026-08-29T09:00:01.000000Z"),
                Turn("tool", "view_file", "2026-08-29T09:00:01.000000Z"),
                Turn("tool", "run_command", "2026-08-29T09:00:01.000000Z"),
                Turn("user", "no wrapper", "2026-08-29T09:00:04.000000Z"),
            ],
        )

    def test_steps_table_when_there_is_no_log(self):
        store = self.copy()
        db = self.conv(store, HEADLESS)
        shutil.rmtree(Path(store.root) / "brain" / HEADLESS)
        s = self.listed(store, HEADLESS)
        # the fixture payloads hold no text: nothing to show, and no error
        self.assertEqual(list(self.provider.transcript(s)), [])
        tool = pb_bytes(7, pb_bytes(1, "call-1") + pb_bytes(2, "view_file") + pb_bytes(3, "{}"))
        payloads = {
            0: pb_int(1, 14) + pb_bytes(19, pb_bytes(2, "make it blue") + pb_bytes(4, "")),
            1: pb_int(1, 15) + pb_bytes(20, pb_bytes(3, "private reasoning") + tool + tool),
            2: pb_int(1, 132) + pb_bytes(140, pb_bytes(1, "tool output")),
            # the reasoning field is cut off; the reply before it still reads
            3: pb_int(1, 15)
            + pb_bytes(20, pb_bytes(1, "Done: it is blue.") + pb_bytes(3, "more reasoning")[:-1]),
        }
        for idx, payload in payloads.items():
            self.edit(db, "UPDATE steps SET step_payload = ? WHERE idx = ?", (payload, idx))
        self.assertEqual(
            list(self.provider.transcript(s)),
            [
                Turn("user", "make it blue", "2026-08-29T09:14:43.066548Z"),
                Turn("tool", "view_file", "2026-08-29T09:14:43.137965Z"),
                Turn("tool", "view_file", "2026-08-29T09:14:43.137965Z"),
                Turn("assistant", "Done: it is blue.", "2026-08-29T09:15:02.058732Z"),
            ],
        )
        self.assertEqual(sidecars(store.root), [])

    def test_session_without_files_or_with_a_missing_database(self):
        s = self.listed(fixture_store(WINDOWS), HEADLESS)
        s.files = []
        self.assertEqual(list(self.provider.transcript(s)), [])
        s.files = [str(self.tmp / "conversations" / "gone.db")]
        self.assertEqual(list(self.provider.transcript(s)), [])
        (self.tmp / "garbage.db").write_bytes(b"not a database " * 50)
        s.files = [str(self.tmp / "garbage.db")]
        self.assertEqual(list(self.provider.transcript(s)), [])


class IndexIntegrationTest(unittest.TestCase):
    def test_scan_lists_the_fixture_stores_and_writes_nothing(self):
        box = Sandbox(self, {"antigravity": Antigravity})
        disk = box.mount("Win")
        shutil.copytree(NATIVE, box.home / ".gemini" / "antigravity-cli")
        shutil.copytree(WINDOWS.parent, disk / "Users" / "user" / ".gemini")
        before = {root: tree_state(root) for root in (box.home, disk)}
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(
            (summary["stores"], summary["candidates"], summary["sessions"], summary["parsed"]),
            (3, 4, 3, 4),
        )
        sessions = api.list_sessions("antigravity")
        self.assertEqual({s.id for s in sessions}, set(LISTED))
        self.assertEqual({s.origin for s in sessions}, {"foreign"})
        again = api.scan(deep=True)
        self.assertEqual((again["parsed"], again["cached"], again["sessions"]), (0, 4, 3))
        self.assertTrue(api.verify()["ok"])
        for root, state in before.items():
            self.assertEqual(tree_state(root), state)


if __name__ == "__main__":
    unittest.main()
