"""index.py and api.py: the parse cache, remembered stores, derived fields."""

import json
import os
import re
import stat
import sys
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakePath, FakeProvider, OtherProvider, Sandbox, finish, python_child

from agent_sessions import api, discover, fsutil, providers
from agent_sessions import index as index_mod
from agent_sessions.fsutil import cache_file
from agent_sessions.index import Index
from agent_sessions.model import make_key
from agent_sessions.providers.base import Provider
from agent_sessions.providers.claude import Claude


def listing() -> str:
    return json.dumps([s.to_dict() for s in api.list_sessions()], sort_keys=True)


def deep_scan_after_the_stores() -> None:
    """Move the last deep scan two seconds later in the cache. Its time is
    kept to the second, and a store made in the second of the scan counts as
    one that may have appeared since (``api._appeared_since_deep``)."""
    data = json.loads(cache_file().read_text())
    later = datetime.now(UTC) + timedelta(seconds=2)
    data["last_deep"] = later.isoformat(timespec="seconds").replace("+00:00", "Z")
    cache_file().write_text(json.dumps(data))


def bump(path: Path, seconds: int = 5) -> None:
    """Move a file's mtime without changing its size."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 10**9))


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.native = self.box.store(self.box.home)
        self.disk = self.box.mount("Win Disk")
        self.foreign = self.box.store(self.disk / "Users" / "arun")
        self.path = FakePath(self, self.box.root / "bin")  # every command is installed

    def by_id(self) -> dict:
        return {s.id: s for s in api.list_sessions()}


class ParseCacheTest(IndexCase):
    def test_cache_hit_avoids_parse(self):
        self.box.session(self.native, "a")
        self.box.session(self.foreign, "b")
        first = api.scan(deep=True)
        self.assertEqual((first["parsed"], first["cached"], first["sessions"]), (2, 0, 2))
        self.assertEqual(sorted(FakeProvider.parsed), ["a", "b"])
        second = api.scan(deep=False)
        self.assertEqual((second["parsed"], second["cached"], second["sessions"]), (0, 2, 2))
        third = api.scan(deep=True)
        self.assertEqual((third["parsed"], third["cached"]), (0, 2))
        self.assertEqual(len(FakeProvider.parsed), 2)
        api.list_sessions()
        api.status()
        self.assertEqual(len(FakeProvider.parsed), 2)  # listing never parses

    def test_changed_mtime_or_size_reparses_only_that_candidate(self):
        a = self.box.session(self.native, "a", title="one")
        self.box.session(self.native, "b")
        api.scan()
        FakeProvider.parsed.clear()
        bump(a)  # mtime only
        self.assertEqual(api.scan()["parsed"], 1)
        st = os.stat(a)
        a.write_text(json.dumps({"title": "one, longer", "updated": "2026-10-01T10:00:00Z"}))
        os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns))  # size only
        summary = api.scan()
        self.assertEqual((summary["parsed"], summary["cached"]), (1, 1))
        self.assertEqual(FakeProvider.parsed, ["a", "a"])
        self.assertEqual(self.by_id()["a"].title, "one, longer")

    def test_not_a_session_is_cached_too(self):
        self.box.session(self.native, "empty", listed=False)
        self.box.session(self.native, "real")
        summary = api.scan()
        self.assertEqual((summary["sessions"], summary["candidates"]), (1, 2))
        FakeProvider.parsed.clear()
        self.assertEqual(api.scan()["parsed"], 0)
        self.assertEqual(FakeProvider.parsed, [])
        row = next(r for r in api.status()["stores"] if r["root"] == str(self.native))
        self.assertEqual((row["listed"], row["candidates"], row["skipped"]), (1, 2, 1))

    def test_new_and_removed_candidates(self):
        a = self.box.session(self.native, "a")
        api.scan()
        self.box.session(self.native, "b")
        self.assertEqual(api.scan()["parsed"], 1)
        self.assertEqual(sorted(self.by_id()), ["a", "b"])
        a.unlink()
        summary = api.scan()
        self.assertEqual((summary["parsed"], summary["sessions"]), (0, 1))
        self.assertEqual(sorted(self.by_id()), ["b"])

    def test_candidate_with_several_paths(self):
        """A session directory: its own stat does not move when a file in it
        grows, so the adapter lists the files."""

        class DirProvider(FakeProvider):
            def candidates(self, store):
                return [
                    index_mod.Candidate(
                        d.name, [str(d), str(d / "chat.jsonl"), str(d / "missing.json")]
                    )
                    for d in Path(store.root, "sessions").iterdir()
                    if d.is_dir()
                ]

            def parse(self, store, c):
                type(self).parsed.append(c.id_hint)
                return index_mod.Session.for_store(store, id=c.id_hint, title="dir", files=c.paths)

        self.box = Sandbox(self, {"fake": DirProvider})
        store = self.box.store(self.box.home)
        chat = store / "sessions" / "s1" / "chat.jsonl"
        chat.parent.mkdir()
        chat.write_text("{}\n")
        self.assertEqual(api.scan()["parsed"], 1)
        self.assertEqual(api.scan()["parsed"], 0)
        with open(chat, "a") as fh:
            fh.write("{}\n")
        self.assertEqual(api.scan()["parsed"], 1)
        (chat.parent / "missing.json").write_text("{}")
        self.assertEqual(api.scan()["parsed"], 1)
        self.assertEqual(api.scan()["parsed"], 0)

    def test_reader_change_drops_cached_parses(self):
        self.box.session(self.native, "a")
        api.scan()
        with mock.patch("agent_sessions.index.reader_id", return_value="new reader"):
            self.assertEqual(api.scan()["parsed"], 1)
            self.assertEqual(api.scan()["parsed"], 0)

    def test_warnings_are_cached_with_the_candidate(self):
        bad = self.box.session(self.native, "bad")
        bad.write_text("{nope")
        first = api.scan()
        self.assertEqual(first["warnings"], [f"{bad}: not JSON"])
        self.assertEqual(first["sessions"], 0)
        FakeProvider.parsed.clear()
        self.assertEqual(api.scan()["warnings"], [f"{bad}: not JSON"])  # from the cache
        self.assertEqual(FakeProvider.parsed, [])
        self.assertEqual(api.status()["warnings"], [f"{bad}: not JSON"])

    def test_parse_exception_is_a_warning_and_is_retried(self):
        self.box.session(self.native, "boom", boom=True)
        self.box.session(self.native, "fine")
        first = api.scan()
        self.assertEqual(first["sessions"], 1)
        self.assertEqual(
            first["warnings"], [f"{self.native}: boom: parse failed: RuntimeError: boom"]
        )
        FakeProvider.parsed.clear()
        api.scan()
        self.assertEqual(FakeProvider.parsed, ["boom"])

    def test_unusable_adapter_result(self):
        class Sloppy(FakeProvider):
            def parse(self, store, c):
                return {"id": c.id_hint}

        self.box = Sandbox(self, {"fake": Sloppy})
        store = self.box.store(self.box.home)
        self.box.session(store, "a")
        summary = api.scan()
        self.assertEqual(summary["sessions"], 0)
        self.assertEqual(summary["warnings"], [f"{store}: a: adapter returned an unusable session"])

    def test_candidates_failure_is_a_warning(self):
        class Broken(FakeProvider):
            def candidates(self, store):
                raise OSError("disk went away")

        self.box = Sandbox(self, {"fake": Broken})
        store = self.box.store(self.box.home)
        summary = api.scan()
        self.assertEqual(
            summary["warnings"], [f"{store}: cannot list sessions: OSError: disk went away"]
        )
        self.assertEqual((summary["stores"], summary["sessions"]), (1, 0))


class NotImplementedReaderTest(unittest.TestCase):
    def test_skeleton_adapter_lists_the_store_with_one_warning(self):
        class Skeleton(Claude):  # an adapter whose reader is not written yet
            def candidates(self, store):
                raise NotImplementedError

        box = Sandbox(self, {"claude": Skeleton})
        disk = box.mount("Win")
        for base in (box.home, disk / "Users" / "a", disk / "Users" / "b"):
            (base / ".claude" / "projects" / "x").mkdir(parents=True)
        summary = api.scan(deep=True)
        self.assertEqual((summary["stores"], summary["sessions"]), (3, 0))
        self.assertEqual(summary["warnings"], ["claude: reader not implemented"])
        status = api.status()
        self.assertEqual(
            [(s["root"], s["reader"], s["listed"], s["candidates"]) for s in status["stores"]],
            [
                (str(box.home / ".claude"), False, 0, 0),
                (str(disk / "Users/a/.claude"), False, 0, 0),
                (str(disk / "Users/b/.claude"), False, 0, 0),
            ],
        )
        self.assertEqual(status["providers"][0]["reader"], False)
        self.assertEqual(api.list_sessions(), [])
        self.assertTrue(api.verify()["ok"])

    def test_parse_not_implemented_counts_the_same(self):
        class Half(FakeProvider):
            def parse(self, store, c):
                raise NotImplementedError

        box = Sandbox(self, {"fake": Half})
        store = box.store(box.home)
        box.session(store, "a")
        box.session(store, "b")
        summary = api.scan()
        self.assertEqual(summary["warnings"], ["fake: reader not implemented"])
        self.assertEqual((summary["sessions"], summary["candidates"]), (0, 0))


class CacheFileTest(IndexCase):
    def test_written_atomically_under_the_cache_dir(self):
        self.box.session(self.native, "a")
        api.scan()
        self.assertEqual(cache_file(), self.box.cache / "index.json")
        self.assertEqual(sorted(p.name for p in self.box.cache.iterdir()), ["index.json"])
        data = json.loads(cache_file().read_text())
        self.assertEqual(data["version"], index_mod.CACHE_VERSION)
        self.assertEqual(
            [s["root"] for s in data["stores"]], sorted([str(self.native), str(self.foreign)])
        )

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_the_cache_is_private(self):
        """It holds the title and the folder of every session."""

        def mode(path) -> int:
            return stat.S_IMODE(os.stat(path).st_mode)

        self.box.session(self.native, "a", title="a private title")
        self.assertFalse(self.box.cache.exists())
        api.scan()
        self.assertEqual((mode(self.box.cache), mode(cache_file())), (0o700, 0o600))
        self.assertIn("a private title", cache_file().read_text())
        # A cache written before this, readable by everyone, in a directory
        # that was made the ordinary way (0.1.0 made it 0755): both are
        # tightened when the cache is rewritten (AUDIT A-25).
        text = cache_file().read_text()
        cache_file().unlink()
        self.box.cache.rmdir()
        self.box.cache.mkdir()
        os.chmod(self.box.cache, 0o755)  # noqa: S103  (the mode 0.1.0 left: what is tightened)
        cache_file().write_text(text)
        os.chmod(cache_file(), 0o644)
        api.scan()
        self.assertEqual(mode(cache_file()), 0o600)
        self.assertEqual(mode(self.box.cache), 0o700)
        # A temp file of that name left behind, with a wide mode: not inherited.
        stale = self.box.cache / f"index.json.{os.getpid()}.tmp"
        stale.write_text("left over")
        os.chmod(stale, 0o644)
        os.chmod(cache_file(), 0o644)
        api.scan()
        self.assertEqual(mode(cache_file()), 0o600)
        self.assertEqual(sorted(p.name for p in self.box.cache.iterdir()), ["index.json"])
        # Reading changes nothing.
        os.chmod(cache_file(), 0o640)
        os.chmod(self.box.cache, 0o750)  # noqa: S103  (wide on purpose: reading must not change it)
        api.load_index()
        api.status()
        self.assertEqual((mode(cache_file()), mode(self.box.cache)), (0o640, 0o750))
        os.chmod(self.box.cache, 0o700)
        # A directory the user named for the index file itself is not ours to tighten.
        theirs = self.box.root / "theirs"
        theirs.mkdir(mode=0o755)
        os.chmod(theirs, 0o755)  # noqa: S103  (the user's own mode, which stays)
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_CACHE": str(theirs / "i.json")}):
            api.scan()
        self.assertEqual((mode(theirs), mode(theirs / "i.json")), (0o755, 0o600))

    @unittest.skipUnless(os.name == "posix", "file modes")
    def test_a_cache_directory_that_is_made_is_private_and_its_parents_are_not_ours(self):
        target = self.box.root / "a" / "b" / "cache"
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_CACHE": str(target)}):
            api.scan()
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(target.parent).st_mode), 0o777 & ~umask)
        self.assertEqual(stat.S_IMODE(os.stat(target / "index.json").st_mode), 0o600)

    def test_cache_env_may_name_the_file(self):
        target = self.box.root / "elsewhere" / "my-index.json"
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_CACHE": str(target)}):
            api.scan()
            self.assertTrue(target.is_file())

    def test_a_file_that_is_no_cache_starts_empty_and_is_kept_aside(self):
        """AUDIT A-19: an unusable cache was treated as absent and replaced by
        the next save, with the only record of the drives that are away."""
        self.box.session(self.native, "a")
        api.scan()
        good = cache_file().read_text()
        unusable = cache_file().with_name("index.json.unusable")
        broken = [
            "",
            "{",
            "[]",
            "null",
            '"text"',
            good[: len(good) // 2],
            json.dumps(dict(json.loads(good), version=index_mod.CACHE_VERSION + 1)),
            json.dumps(dict(json.loads(good), version=True)),
            json.dumps({"no": "version"}),
        ]
        for text in broken:
            with self.subTest(text=text[:40]):
                cache_file().write_text(text)
                index = Index.load()
                self.assertRegex(index.load_error, r"^unreadable: .+")
                self.assertEqual(api.list_sessions(), [])
                self.assertEqual(api.status()["stores"], [])
                self.assertEqual(cache_file().read_text(), text)  # reading moves nothing
                FakeProvider.parsed.clear()
                summary = api.scan()
                self.assertEqual((summary["mode"], summary["sessions"]), ("deep", 1))
                self.assertEqual(FakeProvider.parsed, ["a"])
                self.assertEqual(unusable.read_text(), text)  # kept, the older one replaced
                kept = [w for w in summary["warnings"] if "could not be used" in w]
                self.assertEqual(
                    kept,
                    [
                        f"{cache_file()}: the cache could not be used ({index.load_error}); "
                        f"it was kept as {unusable} and a new one was written"
                    ],
                )
                self.assertIsNone(Index.load().load_error)
        cache_file().write_bytes(b"\xff\xfe\x00garbage")
        self.assertEqual(api.list_sessions(), [])
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(unusable.stat().st_mode), 0o600)  # moved, as it was

    def test_a_file_that_cannot_be_kept_aside_is_not_overwritten(self):
        cache_file().parent.mkdir(parents=True, exist_ok=True)
        cache_file().write_text("{")
        (self.box.cache / "index.json.unusable").mkdir()  # in the way
        self.box.session(self.native, "a")
        summary = api.scan()
        self.assertEqual(cache_file().read_text(), "{")
        self.assertTrue(any("cache not saved" in w for w in summary["warnings"]))

    def test_a_damaged_entry_is_left_out_and_only_it(self):
        """AUDIT A-19: one wrong value anywhere cost the whole cache (3,628 of
        6,000 single-value mutations in the audit's fuzz)."""
        self.box.session(self.native, "a")
        self.box.session(self.foreign, "b")
        api.scan()
        good = json.loads(cache_file().read_text())
        key = f"fake:{self.foreign}"
        native_key = f"fake:{self.native}"

        def with_candidate(**changes):
            entry = json.loads(json.dumps(good["parsed"][key]))
            entry["candidates"]["b"].update(changes)
            return dict(good, parsed=dict(good["parsed"], **{key: entry}))

        cases = {
            "a store that is no store": dict(good, stores=[*good["stores"], {"provider": 1}]),
            "a list that is no list": dict(good, skipped="x"),
            "a parse entry that is no entry": dict(good, parsed=dict(good["parsed"], k={"x": 1})),
            "a session of the wrong form": with_candidate(session={"id": 5}),
            "a signature of the wrong form": with_candidate(sig=[[1, 2]]),
            "a signature with a number that is text": with_candidate(sig=[["/p", "1", 2]]),
            "warnings that are no text": with_candidate(warnings=[1]),
            "a time that is no time": dict(good, last_deep="soon"),
            "a walk that is no walk": dict(good, walk={"/": {"dirs": "many"}}),
            "a volume that is no volume": dict(good, volumes={"/x": {"uuid": 5}}),
            "deep_mounts with numbers": dict(good, deep_mounts=[1]),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                cache_file().write_text(json.dumps(data))
                index = Index.load()
                self.assertIsNone(index.load_error)
                self.assertGreaterEqual(index.damaged, 1)
                self.assertIn(native_key, index.parsed)  # what is sound is kept
                ids = sorted(s.id for s in api.list_sessions())
                self.assertIn("a", ids)
                warning = index.damage_warning()
                self.assertRegex(
                    warning,
                    rf"^{re.escape(str(cache_file()))}: [0-9,]+ damaged entr(y|ies) of the cache "
                    "were left out; a scan reads what is on the mounted drives again$",
                )
                self.assertIn(warning, api.status()["warnings"])
                summary = api.scan()
                self.assertEqual(summary["sessions"], 2)
                self.assertEqual(Index.load().damaged, 0)  # the next save is whole again
                self.assertFalse(cache_file().with_name("index.json.unusable").exists())

    def test_temp_files_of_a_save_that_was_killed_are_removed(self):
        """AUDIT A-19 (``feat/t4_kill.py``, "cache-write"): ``index.json.<pid>.tmp``
        stayed for good, with every title and folder in it."""
        self.box.session(self.native, "a")
        api.scan()
        gone = 4_000_000  # no process has this id
        with mock.patch.object(index_mod.fsutil, "process_alive", lambda pid: pid != gone):
            for name in (
                f"index.json.{gone}.tmp",
                "index.json.1.tmp",
                "index.json.x.tmp",
                "other.tmp",
            ):
                (self.box.cache / name).write_text("{}")
            api.scan()
        self.assertEqual(
            sorted(p.name for p in self.box.cache.iterdir()),
            ["index.json", "index.json.1.tmp", "index.json.x.tmp", "other.tmp"],
        )
        self.assertTrue(fsutil.process_alive(os.getpid()))
        if os.name == "posix":
            self.assertFalse(fsutil.process_alive(gone))

    def test_an_unusable_cache_is_emptied_without_building_the_index_twice(self):
        """``load`` used to call ``__init__`` a second time on the object to
        empty it; a subclass's ``__init__`` ran twice with it."""

        class Counting(Index):
            made = 0

            def __init__(self, path=None, label="mine"):
                type(self).made += 1
                self.label = label
                super().__init__(path)

        self.box.session(self.native, "a")
        api.scan()
        good = json.loads(cache_file().read_text())
        loaded = Counting.load()
        self.assertEqual((Counting.made, loaded.load_error, len(loaded.stores)), (1, None, 2))
        # Stores are read before the part that is wrong: taken over, then dropped again.
        cache_file().write_text(json.dumps(dict(good, version=99)))
        Counting.made = 0
        broken = Counting.load()
        self.assertEqual(Counting.made, 1)
        self.assertTrue(broken.load_error.startswith("unreadable: "))
        self.assertEqual(
            (broken.stores, broken.mounts, broken.parsed, broken.last_quick, broken.sessions),
            ([], [], {}, None, []),
        )
        self.assertEqual((broken.path, broken.label), (cache_file(), "mine"))
        cache_file().unlink()
        Counting.made = 0
        missing = Counting.load()
        self.assertEqual((Counting.made, missing.load_error, missing.label), (1, "missing", "mine"))
        self.assertEqual(missing.stores, [])

    def test_missing_cache(self):
        self.assertEqual(api.list_sessions(), [])
        status = api.status()
        self.assertEqual(
            (status["last_quick"], status["last_deep"], status["stores"]), (None, None, [])
        )
        self.assertFalse(self.box.cache.exists())  # reading creates nothing

    def test_unwritable_cache_is_a_warning(self):
        self.box.session(self.native, "a")
        self.box.cache.write_text("a file where the directory should be")
        summary = api.scan()
        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(len(summary["warnings"]), 1)
        self.assertIn("cache not saved", summary["warnings"][0])


class CopiesTest(IndexCase):
    """AUDIT A-17: of copies of one session with the same time, the one on a
    volume that is there is listed, not the one whose drive is away."""

    def test_a_mounted_copy_wins_over_an_offline_one(self):
        first = self.box.mount("aaa Backup")
        second = self.box.mount("bbb Data")
        for disk in (first, second):
            store = self.box.store(disk / "Users" / "u")
            self.box.session(store, "same", updated="2026-10-01T10:00:00Z", title="same")
        api.scan(deep=True)
        (listed,) = [s for s in api.list_sessions() if s.id == "same"]
        self.assertEqual(listed.mount, str(first))  # both there: the smaller store key
        self.box.unmount(first)
        (listed,) = [s for s in api.list_sessions() if s.id == "same"]
        self.assertEqual((listed.mount, listed.offline), (str(second), False))
        index = api.load_index()
        self.assertEqual(api.find_session("same", index).mount, str(second))
        # A newer copy still wins, mounted or not.
        self.box.set_mounts(*self.box.mounts[1:], {"path": first, "label": "aaa Backup"})
        self.box.session(
            self.box.root / "media" / "aaa Backup" / "Users" / "u" / ".fake",
            "same",
            updated="2026-10-02T10:00:00Z",
        )
        api.scan(deep=True)
        self.box.unmount(first)
        (listed,) = [s for s in api.list_sessions() if s.id == "same"]
        self.assertEqual((listed.mount, listed.offline), (str(first), True))


class ScanLockTest(IndexCase):
    """AUDIT A-19: a scan that read the cache before an import's scan saved
    it wrote its older view back (``feat/t5_concurrent.py``, 4 of 20)."""

    def test_a_scan_reads_again_what_was_saved_since_it_loaded(self):
        self.box.session(self.native, "a")
        api.scan(deep=True)
        mine = Index.load()
        theirs = Index.load()
        self.box.session(self.native, "new")
        FakeProvider.parsed.clear()
        theirs.scan(providers.all_providers(), api.list_mounts())  # saves "new"
        self.assertEqual(FakeProvider.parsed, ["new"])
        summary = mine.scan(providers.all_providers(), api.list_mounts())
        self.assertEqual(FakeProvider.parsed, ["new"])  # not parsed a second time
        self.assertEqual((summary["parsed"], summary["sessions"]), (0, 2))
        self.assertIn("new", {s.id for s in api.list_sessions()})

    def test_only_index_lock_is_taken_and_it_is_gone_afterwards(self):
        self.box.session(self.native, "a")
        taken = []
        real = fsutil.lock

        def recording(path=None, timeout=None):
            taken.append(os.fspath(path) if path is not None else "state")
            return real(path, timeout)

        with mock.patch.object(index_mod.fsutil, "lock", recording):
            api.scan()
            api.load_index()
            api.status()
            api.list_sessions()
        self.assertEqual(taken, [str(self.box.cache / "index.lock")])  # the scan's alone
        self.assertEqual(fsutil.index_lock_file(), self.box.cache / "index.lock")
        self.assertEqual(sorted(p.name for p in self.box.cache.iterdir()), ["index.json"])

    def test_a_scan_waits_for_another_and_then_saves(self):
        """Real processes: the other holds the lock for a second."""
        self.box.session(self.native, "a")
        api.scan()
        lock = fsutil.index_lock_file()
        holder = python_child(
            "import time\nfrom agent_sessions import fsutil\n"
            "with fsutil.lock(sys.argv[1]):\n    print('held', flush=True)\n    time.sleep(1.5)\n",
            lock,
        )
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline(), "held\n")
        self.box.session(self.native, "b")
        started = time.monotonic()
        summary = api.scan()
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertEqual(summary["sessions"], 2)
        self.assertFalse(any("not saved" in w for w in summary["warnings"]))
        self.assertEqual(len(Index.load().sessions or api.list_sessions()), 2)
        finish(self, [holder])

    def test_a_lock_held_too_long_means_a_scan_that_is_not_saved(self):
        self.box.session(self.native, "a")
        api.scan()
        before = cache_file().read_bytes()
        held = threading.Event()
        release = threading.Event()

        def hold():
            with fsutil.lock(fsutil.index_lock_file()):
                held.set()
                release.wait(10)

        other = threading.Thread(target=hold)
        other.start()
        self.addCleanup(other.join)
        self.addCleanup(release.set)
        held.wait(5)
        self.box.session(self.native, "b")
        with mock.patch.object(index_mod, "SCAN_LOCK_WAIT", 0.3):
            summary = api.scan()
        self.assertEqual(summary["sessions"], 2)  # the listing of this scan is right
        self.assertIn(
            f"{cache_file()}: cache not saved: another scan is still running "
            f"(process {os.getpid()})",
            summary["warnings"],
        )
        self.assertEqual(cache_file().read_bytes(), before)
        # A cancel ends the wait at once.
        cancel = threading.Event()
        cancel.set()
        started = time.monotonic()
        summary = api.scan(cancel=cancel)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(any("not saved" in w for w in summary["warnings"]))
        # Listing and status never wait for it.
        started = time.monotonic()
        api.load_index()
        api.status()
        self.assertLess(time.monotonic() - started, 1)
        release.set()
        other.join()
        api.scan()
        self.assertEqual(len(api.list_sessions()), 2)

    def test_a_cache_directory_that_cannot_be_written_scans_without_a_lock(self):
        self.box.session(self.native, "a")
        self.box.cache.write_text("a file where the directory should be")
        summary = api.scan()
        self.assertEqual(summary["sessions"], 1)
        self.assertTrue(any("cache not saved" in w for w in summary["warnings"]))


class ScanPolicyTest(IndexCase):
    def test_deep_runs_when_due(self):
        self.assertEqual(api.scan()["mode"], "deep")  # never walked
        self.assertEqual(api.scan()["mode"], "quick")
        self.assertEqual(api.scan(deep=True)["mode"], "deep")  # asked for
        self.assertEqual(api.scan(deep=False)["mode"], "quick")
        other = self.box.mount("Second")
        self.assertEqual(api.scan()["mode"], "deep")  # mount added
        self.assertEqual(api.scan()["mode"], "quick")
        self.box.unmount(other)
        self.assertEqual(api.scan()["mode"], "deep")  # mount removed
        self.assertEqual(api.scan()["mode"], "quick")

    def test_deep_pass_older_than_a_day_is_repeated(self):
        api.scan()
        data = json.loads(cache_file().read_text())
        for stamp, mode in (("2020-01-01T00:00:00Z", "deep"), ("2099-01-01T00:00:00Z", "deep")):
            data["last_deep"] = stamp
            cache_file().write_text(json.dumps(data))
            self.assertEqual(Index.load().last_deep, stamp)  # the cache was taken
            self.assertEqual(api.scan()["mode"], mode)
            self.assertEqual(api.scan()["mode"], "quick")

    def test_a_last_deep_pass_that_is_no_time_is_due(self):
        """``needs_deep`` used the parsed time without looking whether there
        was one (the type checker's finding)."""
        api.scan()
        index = Index.load()
        mounts = api.list_mounts()
        self.assertFalse(index.needs_deep(mounts))
        for stamp in ("soon", "", "1980-01-01T00:00:00Z", "2999-01-01T00:00:00Z", "12"):
            with self.subTest(stamp=stamp):
                index.last_deep = stamp
                self.assertTrue(index.needs_deep(mounts))
        index.last_deep = None
        self.assertTrue(index.needs_deep(mounts))
        index.last_deep = index_mod.utc_now()
        self.assertFalse(index.needs_deep(mounts))

    def test_quick_scan_finds_only_known_places(self):
        tucked = self.box.store(self.disk / "Projects" / "old profile")
        self.box.session(tucked, "t")
        api.scan(deep=False)
        self.assertEqual(api.list_sessions(), [])
        self.assertEqual(api.scan(deep=True)["sessions"], 1)
        self.assertEqual(api.scan(deep=False)["sessions"], 1)  # remembered now

    def test_deleted_store_is_forgotten(self):
        tucked = self.box.store(self.disk / "Projects" / "old")
        self.box.session(tucked, "t")
        api.scan(deep=True)
        (tucked / "sessions" / "t.json").unlink()
        (tucked / "sessions").rmdir()
        summary = api.scan(deep=False)
        self.assertEqual(summary["sessions"], 0)
        self.assertNotIn(str(tucked), [s["root"] for s in api.status()["stores"]])
        self.assertNotIn(f"fake:{tucked}", json.loads(cache_file().read_text())["parsed"])

    def test_cancelled_walk_is_not_a_deep_pass(self):
        self.box.session(self.native, "a")
        api.scan(deep=True)
        before = json.loads(cache_file().read_text())
        cancel = threading.Event()
        cancel.set()
        self.box.mount("New")
        summary = api.scan(deep=True, cancel=cancel)
        self.assertTrue(summary["cancelled"])
        after = json.loads(cache_file().read_text())
        self.assertEqual(
            (after["last_deep"], after["deep_mounts"], after["last_quick"]),
            (before["last_deep"], before["deep_mounts"], before["last_quick"]),
        )
        self.assertEqual(after["parsed"], before["parsed"])
        self.assertEqual(len(api.list_sessions()), 1)
        self.assertEqual(api.scan()["mode"], "deep")  # still due

    def test_cancel_while_parsing_keeps_what_was_done(self):
        for hint in ("a", "b", "c"):
            self.box.session(self.native, hint)
        cancel = threading.Event()
        real = FakeProvider.parse

        def parse_then_cancel(self_, store, c):
            cancel.set()
            return real(self_, store, c)

        with mock.patch.object(FakeProvider, "parse", parse_then_cancel):
            summary = api.scan(deep=False, cancel=cancel)
        self.assertTrue(summary["cancelled"])
        self.assertEqual((summary["parsed"], summary["sessions"]), (1, 1))
        self.assertIsNone(json.loads(cache_file().read_text())["last_quick"])
        summary = api.scan(deep=False)
        self.assertEqual((summary["parsed"], summary["cached"], summary["sessions"]), (2, 1, 3))

    def test_progress_reaches_the_caller(self):
        calls = []
        api.scan(deep=True, progress=lambda label, dirs: calls.append(label))
        self.assertEqual(sorted(set(calls)), ["Linux", "Win Disk"])


class VanishedMountTest(IndexCase):
    """The store of a mount that went away stays remembered; its sessions
    stay listed, as offline (tests/test_offline.py has the rest)."""

    def test_store_stays_remembered_sessions_are_listed_as_offline(self):
        self.box.session(self.native, "n")
        self.box.session(self.foreign, "f")
        api.scan(deep=True)
        self.assertEqual(sorted(self.by_id()), ["f", "n"])
        FakeProvider.parsed.clear()

        self.box.unmount(self.disk)
        self.assertEqual({i: s.offline for i, s in self.by_id().items()}, {"f": True, "n": False})
        self.assertEqual([s.id for s in api.load_index(offline=False).sessions], ["n"])
        summary = api.scan()
        self.assertEqual(
            (
                summary["stores"],
                summary["stores_not_mounted"],
                summary["sessions"],
                summary["offline"],
            ),
            (1, 1, 2, 1),
        )
        self.assertEqual({i: s.offline for i, s in self.by_id().items()}, {"f": True, "n": False})
        rows = {r["root"]: r for r in api.status()["stores"]}
        self.assertEqual(
            (rows[str(self.foreign)]["present"], rows[str(self.native)]["present"]), (False, True)
        )
        gone = [m for m in api.status()["mounts"] if not m["mounted"]]
        self.assertEqual([(m["path"], m["label"]) for m in gone], [(str(self.disk), "Win Disk")])
        api.scan(deep=True)
        self.assertIn(str(self.foreign), [s["root"] for s in api.status()["stores"]])

        self.box.set_mounts((self.disk, "Win Disk"))  # plugged back in
        self.assertEqual({i: s.offline for i, s in self.by_id().items()}, {"f": False, "n": False})
        summary = api.scan(deep=False)
        self.assertEqual(
            (summary["sessions"], summary["offline"], summary["parsed"], summary["cached"]),
            (2, 0, 0, 2),
        )
        self.assertEqual(FakeProvider.parsed, [])

    def test_mount_dir_gone_entirely(self):
        self.box.session(self.foreign, "f")
        api.scan(deep=True)
        self.box.unmount(self.disk)
        os.rename(self.disk, self.box.root / "moved away")
        self.assertEqual(api.scan(deep=True)["stores_not_mounted"], 1)
        self.assertEqual([(s.id, s.offline) for s in api.list_sessions()], [("f", True)])
        self.assertEqual(api.load_index(offline=False).sessions, [])
        self.assertTrue(api.verify()["ok"])

    def test_skipped_store_on_a_vanished_mount_is_remembered(self):
        box = Sandbox(self, {"claude": Claude})
        disk = box.mount("Win")
        desktop = disk / "AppData" / "local-agent-mode-sessions" / "x" / ".claude"
        (desktop / "projects").mkdir(parents=True)
        api.scan(deep=True)
        self.assertEqual(
            [(s["root"], s["mounted"]) for s in api.status()["skipped_stores"]],
            [(str(desktop), True)],
        )
        box.unmount(disk)
        api.scan(deep=True)
        self.assertEqual(
            [(s["root"], s["mounted"]) for s in api.status()["skipped_stores"]],
            [(str(desktop), False)],
        )
        box.set_mounts((disk, "Win"))
        (desktop / "projects").rmdir()
        api.scan(deep=False)
        self.assertEqual(api.status()["skipped_stores"], [])


class DerivedFieldsTest(IndexCase):
    def test_index_owned_fields(self):
        project = self.box.home / "My Proj"
        project.mkdir()
        self.box.session(
            self.native, "n", cwd=str(project), messages=4, started="2026-09-30T08:00:00Z"
        )
        self.box.session(self.foreign, "f", cwd="/nowhere/at/all")
        api.scan()
        got = self.by_id()
        n, f = got["n"], got["f"]
        self.assertEqual(n.key, make_key("fake", self.native, "n"))
        self.assertEqual(
            (n.provider, n.store, n.origin, n.mount, n.mount_label),
            ("fake", str(self.native), "native", str(self.box.root / "home"), "Linux"),
        )
        self.assertEqual((n.cwd, n.cwd_local, n.cwd_exists), (str(project), str(project), True))
        self.assertEqual(
            (n.messages, n.started, n.updated, n.files),
            (
                4,
                "2026-09-30T08:00:00.000000Z",
                "2026-10-01T10:00:00.000000Z",
                [str(self.native / "sessions" / "n.json")],
            ),
        )
        self.assertEqual(
            (f.origin, f.mount, f.mount_label, f.store),
            ("foreign", str(self.disk), "Win Disk", str(self.foreign)),
        )
        self.assertEqual((f.cwd_local, f.cwd_exists, f.launchable), (None, False, False))

    def test_launchable_rules(self):
        self.box.session(self.native, "n")
        self.box.session(self.foreign, "f")
        api.scan()
        got = self.by_id()
        self.assertEqual((got["n"].launchable, got["n"].note), (True, None))
        self.assertEqual((got["f"].launchable, got["f"].note), (False, None))  # foreign: never
        with mock.patch("shutil.which", return_value=None):
            n = self.by_id()["n"]  # binary missing
            self.assertEqual((n.launchable, n.note), (False, "Fake CLI is not installed"))
        with mock.patch.object(FakeProvider, "resumable", False):  # not resumable
            n = self.by_id()["n"]
            self.assertEqual((n.launchable, n.note), (False, "Fake CLI is not installed"))

    def test_adapter_note_is_kept(self):
        self.box.session(self.native, "n", note="continued in x")
        api.scan()
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(self.by_id()["n"].note, "continued in x")

    def test_imported_as(self):
        self.box.session(self.native, "same")
        self.box.session(self.native, "native-only")
        self.box.session(self.foreign, "same")
        self.box.session(self.foreign, "foreign-only")
        api.scan()
        got = {(s.origin, s.id): s for s in api.list_sessions()}
        self.assertEqual(len(got), 4)
        native_key = make_key("fake", self.native, "same")
        self.assertEqual(got[("foreign", "same")].imported_as, native_key)
        self.assertEqual(got[("native", "same")].key, native_key)
        self.assertIsNone(got[("native", "same")].imported_as)
        self.assertIsNone(got[("foreign", "foreign-only")].imported_as)
        index = api.load_index()
        self.assertEqual(index.where(got[("foreign", "same")]), "Win Disk · imported")
        self.assertEqual(index.where(got[("foreign", "foreign-only")]), "Win Disk")
        self.assertEqual(index.where(got[("native", "same")]), "Linux")

    def test_imported_as_is_per_provider(self):
        self.box = Sandbox(self, {"fake": FakeProvider, "other": OtherProvider})
        native = self.box.store(self.box.home)
        disk = self.box.mount("Win")
        foreign_other = self.box.store(disk / "Users" / "a", ".other")
        self.box.session(native, "same")
        self.box.session(foreign_other, "same")
        api.scan()
        self.assertEqual([s.imported_as for s in api.list_sessions()], [None, None])
        self.assertEqual([s.provider for s in api.list_sessions("other")], ["other"])
        with self.assertRaises(ValueError):
            api.list_sessions("claude")

    def test_mirror_stores_do_not_double_list(self):
        mirror = self.box.store(self.disk / "Users" / "arun" / "AppData" / "orca")
        real = self.box.session(self.foreign, "t1", title="thread")
        os.link(real, mirror / "sessions" / "t1.json")
        self.box.session(mirror, "only-in-mirror")
        self.box.session(self.native, "t1")  # another origin: not a duplicate
        summary = api.scan(deep=True)
        self.assertEqual(summary["candidates"], 4)
        got = [(s.origin, s.id, s.store) for s in api.list_sessions()]
        self.assertEqual(
            sorted(got),
            [
                ("foreign", "only-in-mirror", str(mirror)),
                ("foreign", "t1", str(self.foreign)),  # first store in path order wins
                ("native", "t1", str(self.native)),
            ],
        )
        rows = {r["root"]: r for r in api.status()["stores"]}
        self.assertEqual((rows[str(mirror)]["listed"], rows[str(mirror)]["duplicates"]), (1, 1))
        self.assertEqual(rows[str(self.foreign)]["duplicates"], 0)

    def test_of_two_copies_the_newest_is_listed(self):
        """The same session id in two foreign stores: the copy with the
        newest ``updated`` is the one listed, wherever its store sorts."""
        later = self.box.mount("Zed Disk")
        second = self.box.store(later / "Users" / "arun")
        self.assertLess(str(self.foreign), str(second))
        self.box.session(self.foreign, "s1", title="stale", updated="2026-10-01T10:00:00Z")
        self.box.session(second, "s1", title="current", updated="2026-10-05T10:00:00Z")
        # the other way round, a tie, and a copy with no date at all
        self.box.session(self.foreign, "s2", title="current", updated="2026-10-06T10:00:00Z")
        self.box.session(second, "s2", title="stale", updated="2026-10-02T10:00:00Z")
        self.box.session(self.foreign, "s3", title="first", updated="2026-10-03T10:00:00Z")
        self.box.session(second, "s3", title="second", updated="2026-10-03T10:00:00Z")
        self.box.session(self.foreign, "s4", title="undated", updated=None)
        self.box.session(second, "s4", title="dated", updated="2020-01-01T00:00:00Z")
        self.box.session(self.foreign, "s5", title="undated a", updated=None)
        self.box.session(second, "s5", title="undated b", updated=None)
        summary = api.scan(deep=True)
        self.assertEqual((summary["candidates"], summary["sessions"]), (10, 5))
        got = {s.id: (s.title, s.store) for s in api.list_sessions()}
        self.assertEqual(
            got,
            {
                "s1": ("current", str(second)),
                "s2": ("current", str(self.foreign)),
                "s3": ("first", str(self.foreign)),  # tie: the smaller store key
                "s4": ("dated", str(second)),  # no date sorts oldest
                "s5": ("undated a", str(self.foreign)),
            },
        )
        rows = {r["root"]: r for r in api.status()["stores"]}
        self.assertEqual(
            (rows[str(self.foreign)]["listed"], rows[str(self.foreign)]["duplicates"]), (3, 2)
        )
        self.assertEqual((rows[str(second)]["listed"], rows[str(second)]["duplicates"]), (2, 3))
        for row in rows.values():
            self.assertEqual(row["listed"] + row["duplicates"] + row["skipped"], row["candidates"])
        first = listing()
        api.scan(deep=True)
        self.assertEqual(listing(), first)

    def test_drive_letters_reach_the_sessions(self):
        (self.disk / "Windows").mkdir()
        target = self.disk / "Users" / "arun" / "Site Work"
        target.mkdir()
        self.box.session(self.foreign, "w", cwd=r"C:\users\ARUN\site work")
        self.box.session(self.foreign, "gone", cwd=r"C:\Users\arun\deleted")
        self.box.session(self.foreign, "e", cwd=r"E:\elsewhere")
        api.scan()
        got = self.by_id()
        self.assertEqual((got["w"].cwd_local, got["w"].cwd_exists), (str(target), True))
        self.assertEqual((got["gone"].cwd_local, got["gone"].cwd_exists), (None, False))
        index = api.load_index()
        self.assertEqual(index.where(got["w"]), "C:")
        status = api.status()
        self.assertEqual(
            [(r["letter"], r["mount"]) for r in status["letters"]], [("C:", str(self.disk))]
        )
        self.assertEqual(
            [(r["letter"], r["reason"]) for r in status["unresolved_letters"]],
            [("E:", "no evidence")],
        )
        self.assertEqual(
            {m["path"]: m["letter"] for m in status["mounts"]},
            {str(self.box.root / "home"): None, str(self.disk): "C:"},
        )
        target.rmdir()  # folder deleted after the scan
        self.assertEqual(self.by_id()["w"].cwd_exists, False)


class OrderTest(IndexCase):
    def test_newest_first_ties_by_key_undated_last(self):
        self.box.session(self.native, "old", updated="2026-01-01T00:00:00Z")
        self.box.session(self.native, "new", updated="2026-10-10T00:00:00.5Z")
        self.box.session(self.native, "tie-b", updated="2026-05-05T00:00:00Z")
        self.box.session(self.foreign, "tie-a", updated="2026-05-05 00:00:00.000000+00:00")
        self.box.session(self.native, "undated", updated=None)
        api.scan()
        got = api.list_sessions()
        ties = sorted(s.key for s in got if s.id.startswith("tie"))
        self.assertEqual(
            [s.key for s in got],
            [
                make_key("fake", self.native, "new"),
                ties[0],
                ties[1],
                make_key("fake", self.native, "old"),
                make_key("fake", self.native, "undated"),
            ],
        )

    def test_two_scans_give_byte_identical_json(self):
        for i in range(12):
            self.box.session(
                self.native if i % 2 else self.foreign,
                f"s{i:02}",
                cwd=str(self.box.home),
                updated=f"2026-10-{i % 3 + 1:02}T00:00:00Z",
            )
        api.scan(deep=True)
        first = listing()
        self.assertEqual(listing(), first)
        api.scan(deep=True)
        self.assertEqual(listing(), first)
        api.scan(deep=False)
        self.assertEqual(listing(), first)
        cache_file().unlink()  # from scratch: same again
        api.scan()
        self.assertEqual(listing(), first)
        self.assertEqual(len(json.loads(first)), 12)

    def test_status_is_stable(self):
        self.box.session(self.native, "a")
        api.scan(deep=True)
        dump = lambda: json.dumps(api.status(), sort_keys=True)  # noqa: E731
        self.assertEqual(dump(), dump())
        stores = api.status()["stores"]
        self.assertEqual(stores, sorted(stores, key=lambda r: (r["provider"], r["root"])))


class VerifyTest(IndexCase):
    def setUp(self):
        super().setUp()
        self.box.session(self.native, "n")
        self.box.session(self.foreign, "f")
        api.scan(deep=True)

    def test_matches_after_a_scan(self):
        result = api.verify()
        self.assertEqual((result["ok"], result["differences"], result["stale"]), (True, [], []))
        self.assertEqual((result["stores"], result["sessions"]), (2, 2))
        self.assertEqual(
            sorted(result["walk"]), sorted([str(self.box.root / "home"), str(self.disk)])
        )

    def test_newer_than(self):
        """A file's time against the time of the last scan, as numbers."""
        second = 1_000_000_000
        scan = "2026-10-01T10:00:00Z"
        at = 1790848800 * second  # that moment
        sig = lambda *times: [[f"/f{n}", t, 1] for n, t in enumerate(times)]  # noqa: E731
        self.assertFalse(api._newer_than(sig(at - 1), scan))
        self.assertTrue(api._newer_than(sig(at), scan))
        self.assertTrue(api._newer_than(sig(at - 5 * second, at + 1), scan))
        self.assertTrue(api._newer_than(sig(at - 5 * second, None), scan))  # a file is gone
        self.assertFalse(api._newer_than(sig(), scan))
        # Times that are no session times are still times of files.
        self.assertFalse(api._newer_than(sig(0), scan))  # 1970
        self.assertFalse(api._newer_than(sig(5 * second), scan))
        self.assertTrue(api._newer_than(sig(32_503_680_000 * second), scan))  # the year 3000
        # No usable time of the last scan: nothing is older than it.
        for moment in (None, "", "soon", "1980-01-01T00:00:00Z"):
            self.assertTrue(api._newer_than(sig(at - 1), moment))

    def test_a_file_from_1970_does_not_break_verify(self):
        """A candidate written while the clock was unset, and missing from
        the index: a difference, as for any other old file."""
        path = self.foreign / "sessions" / "f.json"
        os.utime(path, (5, 5))
        api.scan()
        data = json.loads(cache_file().read_text())
        del data["parsed"][f"fake:{self.foreign}"]["candidates"]["f"]
        cache_file().write_text(json.dumps(data))
        result = api.verify()
        self.assertEqual(
            result["differences"],
            [
                f"{self.foreign}: f: candidate on disk but not in the index",
                f"session on disk but not listed: {make_key('fake', self.foreign, 'f')}",
            ],
        )
        self.assertEqual(result["stale"], [])

    def test_does_not_use_or_write_the_cache(self):
        before = cache_file().read_bytes()
        FakeProvider.parsed.clear()
        api.verify()
        self.assertEqual(sorted(FakeProvider.parsed), ["f", "n"])  # parsed afresh
        self.assertEqual(cache_file().read_bytes(), before)

    def test_store_the_prune_list_hides(self):
        hidden = self.box.store(self.disk / "node_modules" / "pkg")
        api.scan(deep=True)
        deep_scan_after_the_stores()
        result = api.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["differences"], [f"store on disk but not in the index: fake {hidden}"]
        )

    def test_a_store_that_appeared_since_the_last_deep_scan_is_stale(self):
        """AUDIT A-20 (``d/real.py``): 44 fixture stores in agent worktrees,
        made after the last deep scan, made ``verify`` fail on the real
        machine until a deep scan."""
        deep_scan_after_the_stores()
        self.assertTrue(api.verify()["ok"])
        data = json.loads(cache_file().read_text())
        data["last_deep"] = "2020-01-01T00:00:00Z"  # the stores below are newer than that
        cache_file().write_text(json.dumps(data))
        hidden = self.box.store(
            self.disk / "node_modules" / "pkg"
        )  # where a quick scan never looks
        self.box.session(hidden, "h")
        skipped = self.box.store(self.disk / "tests" / "fixtures" / "x")
        result = api.verify()
        self.assertEqual(result["differences"], [])
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["stale_stores"],
            [
                f"store new since the last deep scan: fake {hidden}",
                f"skipped store new since the last deep scan: fake {skipped}",
            ],
        )
        # A drive that the last deep scan did not walk: the same.
        late = self.box.mount("Late")
        later = self.box.store(late / "x")
        deep_scan_after_the_stores()
        result = api.verify()
        self.assertEqual(
            result["stale_stores"], [f"store new since the last deep scan: fake {later}"]
        )
        self.assertEqual(
            sorted(result["differences"]),
            [
                f"session on disk but not listed: {make_key('fake', hidden, 'h')}",
                f"skipped store on disk but not in the index: fake {skipped}",
                f"store on disk but not in the index: fake {hidden}",
            ],
        )  # older than the deep scan now: a fault
        # After a deep scan, everything is compared again.
        api.scan(deep=True)
        deep_scan_after_the_stores()  # the scan's own second: see there
        result = api.verify()
        self.assertEqual((result["stale_stores"], result["ok"]), ([], False))

    def test_without_any_deep_scan_a_missing_store_is_a_fault(self):
        data = json.loads(cache_file().read_text())
        data["last_deep"] = None
        cache_file().write_text(json.dumps(data))
        hidden = self.box.store(self.disk / "node_modules" / "pkg")
        result = api.verify()
        self.assertEqual(result["stale_stores"], [])
        self.assertIn(f"store on disk but not in the index: fake {hidden}", result["differences"])

    def test_store_inside_a_store_is_a_note(self):
        inner = self.box.store(self.foreign / "plugins")
        result = api.verify()
        self.assertTrue(result["ok"])
        self.assertEqual(result["nested"], [f"fake {inner}"])

    def test_wrong_cached_parse(self):
        data = json.loads(cache_file().read_text())
        data["parsed"][f"fake:{self.native}"]["candidates"]["n"]["session"]["title"] = "tampered"
        cache_file().write_text(json.dumps(data))
        result = api.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["differences"][0], f"{self.native}: n: cached parse differs from a fresh parse"
        )
        self.assertIn(
            "title is 'tampered' in the index, 'title n' on disk", result["differences"][1]
        )

    def test_store_missing_from_the_index(self):
        deep_scan_after_the_stores()
        data = json.loads(cache_file().read_text())
        data["stores"] = [s for s in data["stores"] if s["root"] != str(self.foreign)]
        cache_file().write_text(json.dumps(data))
        result = api.verify()
        self.assertFalse(result["ok"])
        self.assertIn(
            f"store on disk but not in the index: fake {self.foreign}", result["differences"]
        )
        self.assertIn(
            f"session on disk but not listed: {make_key('fake', self.foreign, 'f')}",
            result["differences"],
        )

    def test_candidate_missing_from_the_index(self):
        data = json.loads(cache_file().read_text())
        del data["parsed"][f"fake:{self.foreign}"]["candidates"]["f"]
        data["last_quick"] = "2099-01-01T00:00:00Z"
        cache_file().write_text(json.dumps(data))
        result = api.verify()
        self.assertEqual(
            result["differences"],
            [
                f"{self.foreign}: f: candidate on disk but not in the index",
                f"session on disk but not listed: {make_key('fake', self.foreign, 'f')}",
            ],
        )

    def test_changes_after_the_scan_are_stale_not_wrong(self):
        self.box.session(self.native, "n", title="grew")  # changed
        (self.foreign / "sessions" / "f.json").unlink()  # gone
        data = json.loads(cache_file().read_text())
        data["last_quick"] = "2020-01-01T00:00:00Z"
        cache_file().write_text(json.dumps(data))
        self.box.session(self.foreign, "late")  # new
        result = api.verify()
        self.assertEqual((result["ok"], result["differences"]), (True, []))
        self.assertEqual(
            result["stale"],
            [
                f"{self.native}: n: changed since the last scan",
                f"{self.foreign}: f: gone since the last scan",
                f"{self.foreign}: late: new since the last scan",
            ],
        )
        api.scan()
        result = api.verify()
        self.assertEqual((result["ok"], result["stale"]), (True, []))

    def test_cancel(self):
        cancel = threading.Event()
        cancel.set()
        result = api.verify(cancel=cancel)
        self.assertEqual((result["ok"], result["cancelled"]), (False, True))


class RegistryTest(unittest.TestCase):
    def test_store_of_an_unregistered_provider_is_kept_quiet(self):
        box = Sandbox(self, {"fake": FakeProvider, "other": OtherProvider})
        store = box.store(box.mount("Disk") / "Users" / "a", ".other")
        box.session(store, "o")
        self.assertEqual(api.scan(deep=True)["sessions"], 1)
        with mock.patch.dict(providers._REGISTRY, {"fake": FakeProvider}, clear=True):
            self.assertEqual(api.list_sessions(), [])
            status = api.status()
            self.assertEqual(
                [(s["provider"], s["reader"]) for s in status["stores"]], [("other", False)]
            )

    def test_base_provider_contract(self):
        p = Provider()
        for call in (
            p.native_store,
            lambda: p.is_store(Path("/")),
            lambda: p.candidates(None),
            lambda: p.parse(None, None),
            lambda: p.resume_argv(None),
            lambda: p.list_sessions(None),
        ):
            with self.assertRaises(NotImplementedError):
                call()
        # A provider that says nothing about importing refuses, with a reason.
        for call in (p.import_plan, p.import_session):
            with self.assertRaises(providers.NotSupported) as caught:
                call(None, None)
            self.assertIn("cannot be imported", caught.exception.reason)
        self.assertTrue(Provider.resumable)
        self.assertFalse(Provider.resume_needs_cwd)

    def test_list_sessions_convenience(self):
        box = Sandbox(self)
        store_root = box.store(box.home)
        box.session(store_root, "b")
        box.session(store_root, "a")
        box.session(store_root, "skip", listed=False)
        store = discover.fast_pass([], providers.all_providers()).stores[0]
        self.assertEqual([s.id for s in FakeProvider().list_sessions(store)], ["a", "b"])


class IndexObjectTest(IndexCase):
    def test_load_never_raises_and_save_round_trips(self):
        self.box.session(self.native, "a")
        api.scan(deep=True)
        index = Index.load()
        again = Index.load()
        self.assertEqual(
            (index.stores, index.parsed, index.walk, index.deep_mounts),
            (again.stores, again.parsed, again.walk, again.deep_mounts),
        )
        before = cache_file().read_bytes()
        index.save()
        self.assertEqual(cache_file().read_bytes(), before)
        self.assertEqual(Index.load(self.box.root / "no such file.json").stores, [])
        self.assertEqual(Index.load(self.box.root).stores, [])  # a directory

    def test_signature(self):
        a = self.box.session(self.native, "a")
        st = os.stat(a)
        missing = str(self.native / "nope")
        self.assertEqual(
            index_mod.signature([str(a), missing, str(a)]),
            [[missing, None, None], [str(a), st.st_mtime_ns, st.st_size]],
        )


if __name__ == "__main__":
    unittest.main()
