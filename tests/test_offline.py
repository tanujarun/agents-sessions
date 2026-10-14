"""Drives that are not mounted (docs/SPEC.md "Offline drives"): the volumes
table and the cache versions, sessions listed from the cache, what is
refused for them, a volume that comes back somewhere else, and the engine's
side of mounting and forgetting a drive.

Nothing is mounted: a "drive" is a directory of the sandbox, in or out of the
fake mount list, and where a device matters it is one of ``FakeDisks``."""

import contextlib
import io
import json
import os
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (
    LOG_NAME,
    FakeDisks,
    FakePath,
    FakeProcess,
    FakeProvider,
    OtherProvider,
    Sandbox,
)

from agent_sessions import api, cli, launch, mounts
from agent_sessions import index as index_mod
from agent_sessions.errors import ApiError, MountFailed, NotFound, Offline
from agent_sessions.fsutil import cache_file
from agent_sessions.index import Index
from agent_sessions.model import Session, make_key

WIN = "22EEA841EEA80F5B"
OTHER = "583E82883E825EBE"
TIME = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
NOT_MOUNTED = "the drive Win Disk is not mounted"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def cache() -> dict:
    return json.loads(cache_file().read_text(encoding="utf-8"))


def as_version_1(data: dict) -> dict:
    """A version 2 cache as the version 1 program would have written it: no
    volumes, no identity in the mounts, no path evidence."""
    old = json.loads(json.dumps(data))
    old["version"] = 1
    del old["volumes"]
    old["mounts"] = [
        {name: mount[name] for name in ("path", "label", "fstype", "device")}
        for mount in old["mounts"]
    ]
    for entry in old["parsed"].values():
        del entry["evidence"]
    return old


def write_cache(data: dict) -> None:
    cache_file().write_text(json.dumps(data), encoding="utf-8")


class Terminal(io.StringIO):
    """A text stream that says it is a terminal."""

    def isatty(self) -> bool:
        return True


class Resumable(FakeProvider):
    """The fake provider with a resume command, so a session can be opened."""

    def resume_argv(self, s):
        return [self.binary, "--resume", s.id]


class OfflineCase(unittest.TestCase):
    """A native store, and a Windows system volume ``Win Disk`` with a
    profile's store: one session there recorded a folder that exists
    (``C:`` resolves to the volume), one a folder on ``E:``."""

    def setUp(self):
        self.box = Sandbox(self, {"fake": Resumable, "other": OtherProvider})
        self.native = self.box.store(self.box.home)
        self.disk = self.mount_disk()
        (self.disk / "Windows").mkdir()
        self.site = self.disk / "Users" / "arun" / "Site"
        self.site.mkdir(parents=True)
        self.foreign = self.box.store(self.disk / "Users" / "arun")
        self.path = FakePath(self, self.box.root / "bin")  # every command is installed
        self.box.session(self.native, "n", updated="2026-10-01T10:00:00Z")
        self.box.session(
            self.foreign, "f", cwd="C:\\Users\\arun\\Site", updated="2026-10-03T10:00:00Z"
        )
        self.box.session(self.foreign, "e", cwd="E:\\gone", updated="2026-10-02T10:00:00Z")
        self.started: list = []
        self.notified: list = []
        for name, hook in (
            ("starter", lambda command, cwd: self.started.append((command, cwd))),
            ("notifier", self.notified.append),
        ):
            patch = mock.patch.object(launch, name, hook)
            patch.start()
            self.addCleanup(patch.stop)

    def mount_disk(self) -> Path:
        return self.box.mount("Win Disk", uuid=WIN, fstype="ntfs3")

    def remount(self) -> None:
        self.mount_disk()

    def by_id(self, **options) -> dict[str, Session]:
        return {s.id: s for s in api.load_index(**options).sessions}

    def log(self) -> str:
        path = Path(os.environ["AGENT_SESSIONS_STATE"]) / LOG_NAME
        return path.read_text(encoding="utf-8") if path.exists() else ""


# --- the listing ----------------------------------------------------------------


class ListingTest(OfflineCase):
    def test_sessions_of_a_volume_that_is_not_mounted_are_listed_from_the_cache(self):
        api.scan(deep=True)
        mounted = self.by_id()
        self.assertEqual(
            (mounted["f"].offline, mounted["f"].cwd_local, mounted["f"].note),
            (False, str(self.site), None),
        )
        FakeProvider.parsed.clear()
        self.box.unmount(self.disk)
        # No file of the volume is there to be touched: the directory is gone.
        os.rename(self.disk, self.box.root / "away")

        sessions = self.by_id()  # before any rescan
        self.assertEqual(sorted(sessions), ["e", "f", "n"])
        f = sessions["f"]
        self.assertEqual(
            (f.offline, f.cwd_local, f.cwd_exists, f.launchable, f.note, f.imported_as),
            (True, None, False, False, "drive not mounted", None),
        )
        # What the parse found stays as it was.
        for name in ("key", "title", "cwd", "started", "updated", "messages", "files", "store"):
            self.assertEqual(getattr(f, name), getattr(mounted["f"], name), name)
        self.assertEqual(
            (f.origin, f.mount, f.mount_label), ("foreign", str(self.disk), "Win Disk")
        )
        self.assertEqual((sessions["n"].offline, sessions["n"].launchable), (False, True))
        self.assertEqual(index_mod.OFFLINE_NOTE, "drive not mounted")
        self.assertEqual(FakeProvider.parsed, [])

        for deep in (False, True, None):
            summary = api.scan(deep=deep)
            self.assertEqual(
                (
                    summary["stores"],
                    summary["stores_not_mounted"],
                    summary["sessions"],
                    summary["offline"],
                    summary["candidates"],
                    summary["by_provider"]["fake"],
                ),
                (1, 1, 3, 2, 1, {"stores": 1, "sessions": 3, "offline": 2}),
            )
            self.assertEqual(self.by_id()["f"].to_dict(), f.to_dict())
        self.assertEqual(FakeProvider.parsed, [])
        rows = {row["root"]: row for row in api.status()["stores"]}
        self.assertEqual(
            (rows[str(self.foreign)]["present"], rows[str(self.foreign)]["listed"]), (False, 2)
        )

    def test_back_again_they_are_ordinary_sessions_and_nothing_is_parsed_twice(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        api.scan()
        FakeProvider.parsed.clear()
        self.remount()
        f = self.by_id()["f"]  # before a scan: the mounts decide, not the cache
        self.assertEqual((f.offline, f.cwd_local, f.note), (False, str(self.site), None))
        summary = api.scan(deep=False)
        self.assertEqual(
            (summary["sessions"], summary["offline"], summary["parsed"], summary["cached"]),
            (3, 0, 0, 3),
        )
        self.assertEqual(FakeProvider.parsed, [])

    def test_the_note_of_the_adapter_gives_way_and_comes_back(self):
        self.box.session(self.foreign, "f", note="continued in x", cwd="C:\\Users\\arun\\Site")
        api.scan(deep=True)
        self.assertEqual(self.by_id()["f"].note, "continued in x")
        self.box.unmount(self.disk)
        self.assertEqual(self.by_id()["f"].note, "drive not mounted")
        self.remount()
        self.assertEqual(self.by_id()["f"].note, "continued in x")

    def test_order_is_by_time_whatever_the_drive(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        self.assertEqual([s.id for s in api.list_sessions()], ["f", "e", "n"])
        self.assertEqual([s.offline for s in api.list_sessions()], [True, True, False])

    def test_without_offline_the_listing_is_that_of_the_mounted_volumes(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        self.assertEqual(sorted(self.by_id(offline=False)), ["n"])
        index = api.load_index(offline=False)
        rows = {row["root"]: row for row in index.store_rows}
        self.assertEqual(
            (rows[str(self.foreign)]["present"], rows[str(self.foreign)]["listed"]), (False, 0)
        )
        self.remount()
        self.assertEqual(sorted(self.by_id(offline=False)), ["e", "f", "n"])

    def test_of_two_copies_the_newest_is_listed_offline_or_not(self):
        """Dedupe is the same as for any session: one of a provider and id
        per origin, the newest; the other is counted as a duplicate."""
        other = self.box.mount("Backup")
        mirror = self.box.store(other / "Users" / "arun")
        self.box.session(mirror, "f", title="older copy", updated="2026-09-01T10:00:00Z")
        api.scan(deep=True)
        self.box.unmount(self.disk)
        index = api.load_index()
        (f,) = [s for s in index.sessions if s.id == "f"]
        self.assertEqual((f.offline, f.store), (True, str(self.foreign)))
        rows = {row["root"]: row for row in index.store_rows}
        self.assertEqual(rows[str(mirror)]["duplicates"], 1)
        # The mounted volumes alone: the copy that is there.
        (f,) = [s for s in api.load_index(offline=False).sessions if s.id == "f"]
        self.assertEqual((f.offline, f.store, f.title), (False, str(mirror), "older copy"))
        # And the other way round: the mounted copy is the newer one.
        self.box.session(mirror, "f", title="newer copy", updated="2026-10-09T10:00:00Z")
        api.scan()
        (f,) = [s for s in api.load_index().sessions if s.id == "f"]
        self.assertEqual((f.offline, f.title), (False, "newer copy"))

    def test_imported_as_and_resuming_the_native_copy(self):
        self.box.session(self.native, "f", title="the copy", updated="2026-10-04T10:00:00Z")
        api.scan(deep=True)
        self.box.unmount(self.disk)
        index = api.load_index()
        foreign = next(s for s in index.sessions if s.id == "f" and s.origin == "foreign")
        native_key = make_key("fake", self.native, "f")
        self.assertEqual((foreign.offline, foreign.imported_as), (True, native_key))
        self.assertEqual(index.where(foreign), "C: · imported")  # imported wins over offline
        self.assertEqual(api.can_import(foreign), (False, "already imported"))
        self.assertEqual(api.import_plan(foreign.key)["already"], native_key)
        report = api.import_report(foreign.key)
        self.assertEqual((report.created, report.session.key), (False, native_key))
        plan = api.open_session(foreign.key)
        self.assertEqual(plan.argv, ["fake-agent", "--resume", "f"])
        self.assertEqual(self.started, [(plan.command, plan.cwd)])
        self.assertEqual(self.notified, [])

    def test_a_store_whose_reader_is_gone_lists_nothing_and_says_nothing(self):
        other = self.box.store(self.disk / "Users" / "arun", ".other")
        self.box.session(other, "o")
        api.scan(deep=True)
        self.assertIn("o", self.by_id())
        fake_only = [Resumable()]
        index = Index.load()
        index.finalise(fake_only, mounts.list_mounts())
        self.assertEqual(index.warnings, ["other: reader not implemented"])
        self.box.unmount(self.disk)
        index.finalise(fake_only, mounts.list_mounts())
        self.assertEqual(
            (sorted(s.id for s in index.sessions), index.warnings), (["e", "f", "n"], [])
        )

    def test_warnings_of_a_volume_that_is_away_are_not_repeated(self):
        (self.foreign / "sessions" / "broken.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(len(api.scan(deep=True)["warnings"]), 1)
        self.box.unmount(self.disk)
        self.assertEqual(api.load_index().warnings, [])
        self.assertEqual(api.scan()["warnings"], [])
        self.remount()
        self.assertEqual(len(api.load_index().warnings), 1)

    def test_an_id_that_is_not_one_is_refused_in_an_offline_store_too(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        data = cache()
        entry = data["parsed"][f"fake:{self.foreign}"]["candidates"]["f"]
        entry["session"]["id"] = "--resume\x1b[2J"
        write_cache(data)
        index = api.load_index()
        self.assertEqual(sorted(s.id for s in index.sessions), ["e", "n"])
        (warning,) = index.warnings
        self.assertIn("is not in the form Fake ids have; not listed", warning)


class WhereTest(OfflineCase):
    def test_letter_from_the_volumes_table(self):
        api.scan(deep=True)
        index = api.load_index()
        f = self.by_id()["f"]
        self.assertEqual(index.where(f), "C:")
        self.assertEqual(index.volumes[str(self.disk)]["letters"], {"C:": str(self.disk)})
        self.box.unmount(self.disk)
        index = api.load_index()
        f = next(s for s in index.sessions if s.id == "f")
        self.assertEqual(index.where(f), "C: · offline")
        self.assertEqual(index.letter_for_mount(str(self.disk)), "C:")
        # The path map itself knows nothing of a drive that is away.
        self.assertIsNone(index.pathmap.letter_for_mount(str(self.disk)))
        self.assertEqual((index.pathmap.letters(), index.pathmap.unresolved()), ([], []))
        api.scan(deep=True)  # scanning while it is away keeps the letter
        self.assertEqual(api.load_index().where(f), "C: · offline")

    def test_label_when_no_letter_is_known(self):
        shutil.rmtree(self.site)
        (self.disk / "Windows").rmdir()
        self.box.session(self.foreign, "f", cwd=None)
        api.scan(deep=True)
        self.box.unmount(self.disk)
        index = api.load_index()
        self.assertEqual(
            index.where(next(s for s in index.sessions if s.id == "f")), "Win Disk · offline"
        )
        self.assertIsNone(index.letter_for_mount(str(self.disk)))

    def test_a_letter_set_by_hand_wins(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        config = self.box.home / ".config" / "agent-sessions"
        config.mkdir(parents=True)
        (config / "pathmap.json").write_text(json.dumps({"Q:": str(self.disk)}), encoding="utf-8")
        index = api.load_index()
        self.assertEqual(
            index.where(next(s for s in index.sessions if s.id == "f")), "Q: · offline"
        )

    def test_native_and_mounted_rows_are_as_before(self):
        api.scan(deep=True)
        index = api.load_index()
        where = {s.id: index.where(s) for s in index.sessions}
        self.assertEqual(where, {"n": "Linux", "f": "C:", "e": "C:"})

    def test_letters_of_a_mounted_volume_are_those_of_the_last_scan(self):
        """What the stores of a volume call the drives, as resolved when it
        was last scanned: a letter that is not resolved any more goes."""
        data = self.box.mount("Data")
        (data / "proj").mkdir()
        self.box.session(self.foreign, "g", cwd="F:\\proj")
        api.scan(deep=True)
        self.assertEqual(
            api.load_index().volumes[str(self.disk)]["letters"],
            {"C:": str(self.disk), "F:": str(data)},
        )
        self.box.unmount(data)
        api.scan(deep=True)
        self.assertEqual(
            api.load_index().volumes[str(self.disk)]["letters"], {"C:": str(self.disk)}
        )


# --- what needs the files ---------------------------------------------------------


class RefusalTest(OfflineCase):
    def setUp(self):
        super().setUp()
        api.scan(deep=True)
        self.box.unmount(self.disk)
        self.index = api.load_index()
        self.f = next(s for s in self.index.sessions if s.id == "f")

    def refused(self, call, *args, **kwargs) -> str:
        with self.assertRaises(Offline) as caught:
            call(*args, **kwargs)
        self.assertIsInstance(caught.exception, ApiError)
        self.assertEqual(str(caught.exception), caught.exception.reason)
        return caught.exception.reason

    def test_everything_that_reads_the_files_says_the_drive_is_not_mounted(self):
        provider = mock.Mock(wraps=FakeProvider())
        with mock.patch.object(api, "_provider", return_value=provider):
            for ref in (self.f, self.f.key):
                # At the call, not when the first turn is asked for.
                self.assertEqual(self.refused(api.transcript, ref), NOT_MOUNTED)
                self.assertEqual(self.refused(api.import_plan, ref), NOT_MOUNTED)
                self.assertEqual(self.refused(api.import_report, ref), NOT_MOUNTED)
                self.assertEqual(self.refused(api.import_session, ref), NOT_MOUNTED)
                self.assertEqual(self.refused(api.open_session, ref), NOT_MOUNTED)
                self.assertEqual(self.refused(api.open_session, ref, dry_run=True), NOT_MOUNTED)
                self.assertEqual(self.refused(api.open_session, ref, here=True), NOT_MOUNTED)
                self.assertEqual(api.can_import(ref), (False, NOT_MOUNTED))
            self.assertEqual(self.refused(api.transcript, self.f, index=self.index), NOT_MOUNTED)
        self.assertEqual(provider.mock_calls, [])  # no adapter was asked for anything
        self.assertEqual((self.started, self.notified), ([], []))
        self.assertEqual(
            sorted(os.listdir(self.native / "sessions")), ["n.json"]
        )  # nothing was imported
        self.assertIn(
            "WARNING launch " + self.f.key + " refused: its drive is not mounted", self.log()
        )
        self.assertIn(f"WARNING import {self.f.key} refused: {NOT_MOUNTED}", self.log())

    def test_native_sessions_and_mounted_drives_are_not_refused(self):
        native = next(s for s in self.index.sessions if s.id == "n")
        self.assertEqual(api.open_session(native, dry_run=True).argv[0], "fake-agent")
        self.remount()
        self.assertEqual(api.can_import(self.f.key)[1], "Fake sessions cannot be imported")

    def test_a_session_object_from_before_the_drive_went_keeps_its_answer(self):
        """The session carries what the index said when it was listed."""
        self.remount()
        with self.assertRaises(Offline):
            api.transcript(self.f)  # listed while offline
        fresh = self.by_id()["f"]
        with self.assertRaises(NotImplementedError):
            api.transcript(fresh)  # the fake adapter has no transcript; it was asked

    def test_cli_exit_one_with_the_reason_and_nothing_on_stdout(self):
        line = f"agent-sessions: {NOT_MOUNTED}\n"
        for argv in (
            ("show", self.f.key),
            ("open", self.f.key),
            ("open", self.f.key, "--dry-run"),
            ("open", self.f.key, "--here"),
            ("import", self.f.key),
            ("import", self.f.key, "--dry-run"),
            ("import", self.f.key, "--open"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(run(*argv), (1, "", line))
        self.assertEqual((self.started, self.notified), ([], []))

    def test_a_label_with_escape_sequences_is_cleaned_on_the_way_out(self):
        data = cache()
        for store in data["stores"]:
            if store["origin"] == "foreign":
                store["mount_label"] = "Evil\x1b[2J\u202eDisk"
        write_cache(data)
        key = next(s.key for s in api.load_index().sessions if s.id == "f")
        with self.assertRaises(Offline) as caught:
            api.transcript(key)
        self.assertEqual(caught.exception.reason, "the drive Evil\x1b[2J\u202eDisk is not mounted")
        self.assertEqual(
            run("show", key), (1, "", "agent-sessions: the drive Evil [2J Disk is not mounted\n")
        )
        code, out, _err = run("list")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)


class VerifyTest(OfflineCase):
    def test_only_mounted_volumes_are_compared(self):
        api.scan(deep=True)
        self.assertTrue(api.verify()["ok"])
        self.box.unmount(self.disk)
        result = api.verify()
        self.assertEqual(
            (result["ok"], result["differences"], result["stale"], result["sessions"]),
            (True, [], [], 1),
        )
        os.rename(self.disk, self.box.root / "away")
        api.scan(deep=True)
        self.assertTrue(api.verify()["ok"])
        self.assertEqual(run("verify")[0], 0)

    def test_a_copy_on_a_mounted_volume_behind_a_newer_offline_one(self):
        other = self.box.mount("Backup")
        mirror = self.box.store(other / "Users" / "arun")
        self.box.session(mirror, "f", updated="2026-09-01T10:00:00Z")
        api.scan(deep=True)
        self.box.unmount(self.disk)
        self.assertTrue(next(s for s in api.list_sessions() if s.id == "f").offline)
        result = api.verify()
        self.assertEqual((result["ok"], result["differences"]), (True, []))

    def test_a_fault_on_a_mounted_volume_is_still_found(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        data = cache()
        data["parsed"][f"fake:{self.native}"]["candidates"]["n"]["session"]["title"] = "wrong"
        write_cache(data)
        self.assertFalse(api.verify()["ok"])


# --- the cache ----------------------------------------------------------------------


class VolumesTableTest(OfflineCase):
    def test_what_a_scan_writes(self):
        summary = api.scan(deep=True)
        data = cache()
        self.assertEqual(data["version"], 2)
        self.assertEqual(index_mod.CACHE_VERSION, 2)
        home = str(self.box.root / "home")
        self.assertEqual(sorted(data["volumes"]), sorted([home, str(self.disk)]))
        row = data["volumes"][str(self.disk)]
        self.assertRegex(row.pop("last_seen"), TIME)
        self.assertEqual(
            row,
            {
                "label": "Win Disk",
                "fs_label": "",
                "uuid": WIN,
                "fstype": "ntfs3",
                "letters": {"C:": str(self.disk)},
            },
        )
        self.assertEqual(data["volumes"][home]["label"], "Linux")
        self.assertEqual(data["volumes"][home]["letters"], {})
        self.assertEqual(
            [sorted(mount) for mount in data["mounts"]],
            [["device", "fs_label", "fstype", "label", "path", "uuid"]] * 2,
        )
        self.assertEqual(summary["mounts"], data["mounts"])
        self.assertEqual([m["uuid"] for m in summary["mounts"]], ["", WIN])
        for entry in data["parsed"].values():
            self.assertEqual(entry["evidence"], {"sig": [], "paths": [], "warnings": []})

    def test_a_mount_without_a_store_has_no_row(self):
        empty = self.box.mount("Empty", uuid=OTHER)
        api.scan(deep=True)
        self.assertNotIn(str(empty), api.load_index().volumes)
        self.assertIn(str(empty), [m["path"] for m in api.status()["mounts"]])

    def test_the_row_stays_while_the_volume_is_away_and_is_renewed_when_it_is_back(self):
        api.scan(deep=True)
        before = cache()["volumes"][str(self.disk)]
        self.box.unmount(self.disk)
        api.scan(deep=True)
        api.scan()
        self.assertEqual(cache()["volumes"][str(self.disk)], before)
        self.box.mount("Win Disk", uuid=OTHER, fs_label="Renamed", fstype="exfat")
        with mock.patch.object(index_mod, "utc_now", return_value="2030-01-02T03:04:05Z"):
            api.scan()
        self.assertEqual(
            cache()["volumes"][str(self.disk)],
            {
                "label": "Win Disk",
                "fs_label": "Renamed",
                "uuid": OTHER,
                "fstype": "exfat",
                "letters": {"C:": str(self.disk)},
                "last_seen": "2030-01-02T03:04:05Z",
            },
        )

    def test_a_volume_whose_last_store_is_gone_loses_its_row(self):
        api.scan(deep=True)
        shutil.rmtree(self.foreign)
        api.scan()
        self.assertEqual(list(cache()["volumes"]), [str(self.box.root / "home")])

    def test_a_skipped_store_keeps_its_volume_remembered(self):
        from agent_sessions.providers.claude import Claude

        box = Sandbox(self, {"claude": Claude})
        disk = box.mount("Win", uuid=WIN)
        desktop = disk / "AppData" / "local-agent-mode-sessions" / "x" / ".claude"
        (desktop / "projects").mkdir(parents=True)
        api.scan(deep=True)
        self.assertEqual(list(cache()["volumes"]), [str(disk)])
        box.unmount(disk)
        api.scan(deep=True)
        self.assertEqual(list(cache()["volumes"]), [str(disk)])
        self.assertEqual([v["path"] for v in api.volumes()], [str(disk)])

    def test_load_and_save_round_trip(self):
        api.scan(deep=True)
        before = cache_file().read_bytes()
        index = Index.load()
        self.assertIsNone(index.load_error)
        index.save()
        self.assertEqual(cache_file().read_bytes(), before)
        self.assertEqual(Index.load().volumes, index.volumes)

    def test_a_cache_with_wrong_types_in_the_new_fields_starts_empty(self):
        api.scan(deep=True)
        good = cache()
        path = str(self.disk)

        def volume(**changes):
            return dict(good, volumes=dict(good["volumes"], **{path: changes_to(changes)}))

        def changes_to(changes):
            row = dict(good["volumes"][path], **changes)
            return {k: v for k, v in row.items() if v is not ...}

        def parsed(evidence):
            key = f"fake:{self.foreign}"
            entry = dict(good["parsed"][key])
            if evidence is ...:
                del entry["evidence"]
            else:
                entry["evidence"] = evidence
            return dict(good, parsed=dict(good["parsed"], **{key: entry}))

        broken = {
            "no volumes": {k: v for k, v in good.items() if k != "volumes"},
            "volumes a list": dict(good, volumes=[]),
            "volumes null": dict(good, volumes=None),
            "row a string": dict(good, volumes={path: "x"}),
            "row without uuid": volume(uuid=...),
            "row without letters": volume(letters=...),
            "label a number": volume(label=5),
            "fs_label null": volume(fs_label=None),
            "uuid a number": volume(uuid=7),
            "uuid not in form": volume(uuid="../../etc"),
            "uuid with a space": volume(uuid="AB CD"),
            "fstype a list": volume(fstype=[]),
            "letters a list": volume(letters=["C:"]),
            "letters to a number": volume(letters={"C:": 1}),
            "last_seen no time": volume(last_seen="soon"),
            "last_seen a number": volume(last_seen=5),
            "mount without uuid": dict(
                good, mounts=[{k: v for k, v in m.items() if k != "uuid"} for m in good["mounts"]]
            ),
            "mount uuid a number": dict(good, mounts=[dict(m, uuid=1) for m in good["mounts"]]),
            "no evidence": parsed(...),
            "evidence a list": parsed([]),
            "evidence sig a string": parsed({"sig": "x", "paths": [], "warnings": []}),
            "evidence paths numbers": parsed({"sig": [], "paths": [1], "warnings": []}),
            "evidence without warnings": parsed({"sig": [], "paths": []}),
            "a later version": dict(good, version=3),
            "version zero": dict(good, version=0),
            "version text": dict(good, version="2"),
        }
        versions = {"a later version", "version zero", "version text"}
        for name, data in broken.items():
            with self.subTest(name=name):
                write_cache(data)
                index = Index.load()
                if name in versions:  # not a cache this program reads: nothing of it
                    self.assertRegex(index.load_error or "", r"^unreadable: .+")
                    self.assertEqual((index.stores, index.volumes, index.parsed), ([], {}, {}))
                    self.assertEqual(api.list_sessions(), [])
                    continue
                # AUDIT A-19: what is damaged is left out, and only it.
                self.assertIsNone(index.load_error)
                self.assertGreaterEqual(index.damaged, 1)
                self.assertEqual(len(index.stores), len(good["stores"]))
                self.assertIn("left out", api.status()["warnings"][0])
                self.assertEqual(len(api.list_sessions()), 3)  # the store's row is made again
        write_cache(good)
        self.assertEqual(len(api.list_sessions()), 3)
        self.assertEqual(Index.load().damaged, 0)

    def test_a_store_without_a_row_gets_one(self):
        """A version 2 cache that lost a row: the store is still listed."""
        api.scan(deep=True)
        self.box.unmount(self.disk)
        data = cache()
        del data["volumes"][str(self.disk)]
        write_cache(data)
        index = api.load_index()
        self.assertEqual(
            index.volumes[str(self.disk)],
            {
                "label": "Win Disk",
                "fs_label": "",
                "uuid": "",
                "fstype": "",
                "letters": {},
                "last_seen": None,
            },
        )
        self.assertEqual(sorted(s.id for s in index.sessions if s.offline), ["e", "f"])


class Version1Test(OfflineCase):
    """A cache the version 1 program wrote is taken over, with its
    remembered stores: they are what there is to list of a drive that is
    not mounted when the first version 2 program runs."""

    def setUp(self):
        # The volume is where udisks mounts one without a label: named after its UUID.
        self.disks = None
        super().setUp()
        # One session was started in the profile folder itself.
        self.box.session(self.foreign, "e", cwd="C:\\Users\\arun", updated="2026-10-02T10:00:00Z")

    def mount_disk(self) -> Path:
        if self.disks is None:
            self.disks = FakeDisks(self, self.box.root / "machine")
            self.device = self.disks.device("nvme2n1p3", WIN)
        # Version 1 knew no UUID: the mount carries none.
        return self.box.mount(WIN, at=self.disks.media / "user" / WIN, fstype="ntfs3")

    def old_cache(self) -> dict:
        api.scan(deep=True)
        old = as_version_1(cache())
        write_cache(old)
        return old

    def test_it_is_migrated_in_memory_not_thrown_away(self):
        old = self.old_cache()
        before = cache_file().read_bytes()
        self.box.unmount(self.disk)
        index = api.load_index()
        self.assertIsNone(index.load_error)
        self.assertEqual([vars(s) for s in index.stores], old["stores"])
        self.assertEqual((index.last_quick, index.last_deep), (old["last_quick"], old["last_deep"]))
        self.assertEqual(
            {key: entry["candidates"] for key, entry in index.parsed.items()},
            {key: entry["candidates"] for key, entry in old["parsed"].items()},
        )
        self.assertEqual(sorted(s.id for s in index.sessions if s.offline), ["e", "f"])
        self.assertEqual(cache_file().read_bytes(), before)  # loading writes nothing

    def test_the_volume_row_is_built_from_what_the_old_cache_holds(self):
        old = self.old_cache()
        self.box.unmount(self.disk)
        index = api.load_index()
        home = str(self.box.root / "home")
        self.assertEqual(sorted(index.volumes), sorted([home, str(self.disk)]))
        self.assertEqual(
            index.volumes[str(self.disk)],
            {
                "label": WIN,  # as the last scan saw the mount
                "fs_label": "",
                "uuid": WIN,  # the mount point's name is a UUID that is attached
                "fstype": "ntfs3",
                "letters": {"C:": str(self.disk)},  # a session recorded C:\Users\arun\…
                "last_seen": old["last_quick"],
            },
        )
        self.assertEqual(index.volumes[home]["uuid"], "")
        f = next(s for s in index.sessions if s.id == "f")
        self.assertEqual(index.where(f), "C: · offline")
        (volume,) = [v for v in api.volumes(index) if v["path"] == str(self.disk)]
        self.assertEqual(
            (volume["state"], volume["letter"], volume["uuid"]), ("attached", "C:", WIN)
        )

    def test_a_labelled_volume_is_found_by_its_label(self):
        device = self.disks.device("nvme0n1p2", OTHER, "No Other Way")
        self.assertTrue(device)
        labelled = self.box.mount("No Other Way", at=self.disks.media / "user" / "No Other Way")
        store = self.box.store(labelled / "Users" / "x")
        self.box.session(store, "l", cwd="D:\\Users\\x")
        self.old_cache()
        self.box.unmount(labelled)
        row = api.load_index().volumes[str(labelled)]
        self.assertEqual(
            (row["uuid"], row["fs_label"], row["label"], row["letters"]),
            (OTHER, "No Other Way", "No Other Way", {"D:": str(labelled)}),
        )

    def test_a_volume_that_is_not_attached_or_not_under_the_media_directory(self):
        elsewhere = self.box.mount("Plain")
        store = self.box.store(elsewhere / "Users" / "x")
        self.box.session(store, "p")
        self.old_cache()
        self.box.unmount(self.disk)
        self.box.unmount(elsewhere)
        self.disks.detach("nvme2n1p3")
        index = api.load_index()
        self.assertEqual(index.volumes[str(self.disk)]["uuid"], "")
        self.assertEqual(index.volumes[str(elsewhere)]["uuid"], "")
        self.assertEqual({v["state"] for v in api.volumes(index)}, {"mounted", "absent"})
        self.assertEqual(sorted(s.id for s in index.sessions if s.offline), ["e", "f", "p"])

    def test_a_mount_the_old_scan_did_not_see(self):
        """A store remembered on a drive that was away at the last scan."""
        api.scan(deep=True)
        self.box.unmount(self.disk)
        api.scan(deep=True)
        old = as_version_1(cache())
        self.assertNotIn(str(self.disk), [m["path"] for m in old["mounts"]])
        write_cache(old)
        row = api.load_index().volumes[str(self.disk)]
        self.assertEqual(
            (row["label"], row["fstype"], row["last_seen"], row["uuid"]), (WIN, "", None, WIN)
        )

    def test_the_next_scan_writes_version_2_and_keeps_the_offline_sessions(self):
        self.old_cache()
        self.box.unmount(self.disk)
        summary = api.scan()
        self.assertEqual((summary["sessions"], summary["offline"]), (3, 2))
        data = cache()
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["volumes"][str(self.disk)]["uuid"], WIN)
        self.assertEqual(data["volumes"][str(self.disk)]["letters"], {"C:": str(self.disk)})
        self.assertEqual(sorted(s.id for s in api.list_sessions() if s.offline), ["e", "f"])
        self.assertTrue(api.verify()["ok"])

    def test_mounted_it_reads_like_any_cache(self):
        self.old_cache()
        self.assertEqual(sorted(self.by_id()), ["e", "f", "n"])
        self.assertEqual({s.offline for s in api.list_sessions()}, {False})
        FakeProvider.parsed.clear()
        summary = api.scan(deep=False)
        self.assertEqual((summary["parsed"], summary["cached"]), (0, 3))  # the parses were kept

    def test_wrong_types_in_an_old_cache_start_empty(self):
        good = self.old_cache()
        key = f"fake:{self.foreign}"

        def mounts_with(change):
            return dict(good, mounts=[change(dict(m)) for m in good["mounts"]])

        def drop(name):
            def change(row):
                del row[name]
                return row

            return change

        broken = {
            "mounts a string": dict(good, mounts="x"),
            "mount a list": dict(good, mounts=[[]]),
            "mount without device": mounts_with(drop("device")),
            "mount label a number": mounts_with(lambda m: dict(m, label=5)),
            "mount path null": mounts_with(lambda m: dict(m, path=None)),
            "stores null": dict(good, stores=None),
            "store mount a number": dict(good, stores=[dict(s, mount=1) for s in good["stores"]]),
            "store without mount_label": dict(
                good,
                stores=[{k: v for k, v in s.items() if k != "mount_label"} for s in good["stores"]],
            ),
            "last_quick a number": dict(good, last_quick=5),
            "last_quick no time": dict(good, last_quick="yesterday"),
            "parsed a list": dict(good, parsed=[]),
            "session cwd a number": dict(
                good,
                parsed=dict(
                    good["parsed"],
                    **{
                        key: dict(
                            good["parsed"][key],
                            candidates={
                                "f": dict(
                                    good["parsed"][key]["candidates"]["f"],
                                    session=dict(
                                        good["parsed"][key]["candidates"]["f"]["session"], cwd=5
                                    ),
                                )
                            },
                        )
                    },
                ),
            ),
            "skipped numbers": dict(good, skipped=[5]),
            "deep_mounts numbers": dict(good, deep_mounts=[1]),
        }
        self.box.unmount(self.disk)
        for name, data in broken.items():
            with self.subTest(name=name):
                write_cache(data)
                index = Index.load()
                # AUDIT A-19: what is damaged is left out, and only it.
                self.assertIsNone(index.load_error)
                self.assertGreaterEqual(index.damaged, 1)
                self.assertEqual(run("status", "--json")[0], 0)
                self.assertEqual(run("list", "--json")[0], 0)
                kept = len(good["stores"]) - (1 if name.startswith("store") else 0)
                if name in ("stores null", "store mount a number", "store without mount_label"):
                    kept = 0  # every row of the list is damaged
                self.assertEqual(len(index.stores), kept)
        write_cache(good)
        self.assertEqual(len(api.list_sessions()), 3)

    def test_extra_keys_of_a_newer_old_cache_do_not_matter(self):
        good = self.old_cache()
        write_cache(dict(good, volumes="ignored in version 1", something="else"))
        self.assertIsNone(Index.load().load_error)


class LettersFromTheCacheTest(unittest.TestCase):
    """``Index._letters_from_cache``: the one rule that tells a letter with
    no file to look at."""

    MOUNT = "/run/media/u/WIN"

    def letters(
        self, cwds, root="/run/media/u/WIN/Users/arun/.fake", mount=MOUNT, origin="foreign"
    ):
        from agent_sessions.model import Store

        index = Index()
        index.stores = [Store("fake", root, origin, mount, "WIN")]
        index.parsed = {
            f"fake:{root}": {
                "candidates": {
                    str(n): {"session": None if cwd is ... else {"cwd": cwd}}
                    for n, cwd in enumerate(cwds)
                }
            }
        }
        return index._letters_from_cache(self.MOUNT)

    def test_a_session_that_recorded_the_folder_its_store_is_in(self):
        here = {"C:": self.MOUNT}
        for cwd in (
            "C:\\Users\\arun",
            "c:/users/ARUN/",
            "C:\\Users",
            "C:\\Users\\arun\\.fake",
            "\\\\?\\C:\\Users\\arun",
            "file:///C:/Users/arun",
        ):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.letters([cwd]), here)
        self.assertEqual(self.letters(["C:\\Users\\arun", "F:\\Proj", None, ...]), here)

    def test_what_proves_nothing(self):
        for cwds in (
            [],
            [None, ...],
            ["C:\\"],  # the bare root is on every drive
            ["C:"],
            ["C:\\Users\\arun\\Downloads"],  # below the profile: could be anywhere
            ["C:\\Users\\other"],
            ["F:\\Proj\\Site"],
            ["/home/u/x", "/run/media/u/WIN/Users/arun"],  # this machine's own paths
            ["relative\\path", "\\\\server\\share\\Users\\arun", 5],
            ["C:\\Users\\arun", "D:\\Users\\arun"],  # two letters fit: neither is taken
        ):
            with self.subTest(cwds=cwds):
                self.assertEqual(self.letters(cwds), {})

    def test_only_foreign_stores_of_that_mount_below_it(self):
        self.assertEqual(self.letters(["C:\\Users\\arun"], origin="native"), {})
        self.assertEqual(self.letters(["C:\\Users\\arun"], mount="/run/media/u/OTHER"), {})
        self.assertEqual(self.letters(["C:\\Users\\arun"], root="/elsewhere/Users/arun/.fake"), {})
        self.assertEqual(self.letters(["C:\\Users\\arun"], root=self.MOUNT), {})


# --- a volume that comes back at another mount path -----------------------------


class MovedVolumeTest(OfflineCase):
    def move(self, **identity) -> Path:
        """Unmount the volume and mount the same files somewhere else."""
        self.box.unmount(self.disk)
        new = self.box.root / "media" / "Win Disk1"
        os.rename(self.disk, new)
        self.box.mount("Win Disk1", **identity)
        return new

    def test_the_stores_at_the_old_path_are_dropped_and_found_at_the_new_one(self):
        api.scan(deep=True)
        old_keys = {s.id: s.key for s in api.list_sessions()}
        new = self.move(uuid=WIN, fstype="ntfs3")
        before = api.load_index()  # nothing was scanned yet: the old path is all it knows
        self.assertEqual(sorted(s.id for s in before.sessions if s.offline), ["e", "f"])
        FakeProvider.parsed.clear()

        summary = api.scan()  # the set of mounts changed: this is a deep pass
        self.assertEqual(
            (summary["mode"], summary["stores"], summary["stores_not_mounted"], summary["offline"]),
            ("deep", 2, 0, 0),
        )
        index = api.load_index()
        self.assertEqual(
            sorted((s.provider, s.root) for s in index.stores),
            sorted([("fake", str(self.native)), ("fake", str(new / "Users" / "arun" / ".fake"))]),
        )
        self.assertNotIn(str(self.disk), index.volumes)
        self.assertEqual(index.volumes[str(new)]["uuid"], WIN)
        self.assertNotIn(f"fake:{self.foreign}", index.parsed)
        sessions = {s.id: s for s in index.sessions}
        self.assertEqual(sorted(sessions), ["e", "f", "n"])
        self.assertEqual({s.offline for s in sessions.values()}, {False})
        self.assertEqual(sessions["f"].mount, str(new))
        self.assertNotEqual(sessions["f"].key, old_keys["f"])  # the key names the store's path
        self.assertEqual(sorted(FakeProvider.parsed), ["e", "f"])  # read at the new place
        self.assertNotIn(str(self.disk), index.deep_mounts)
        self.assertNotIn(str(self.disk), index.walk)
        self.assertTrue(api.verify()["ok"])

    def test_a_quick_scan_drops_them_too(self):
        api.scan(deep=True)
        new = self.move(uuid=WIN)
        summary = api.scan(deep=False)
        # The fast pass looks under <mount>/Users/*: this store is found at once.
        self.assertEqual((summary["stores_not_mounted"], summary["offline"]), (0, 0))
        self.assertEqual(
            {s.mount for s in api.list_sessions() if s.origin == "foreign"}, {str(new)}
        )

    def test_a_volume_with_no_uuid_is_never_matched(self):
        self.box.unmount(self.disk)
        self.box.mount("Win Disk")  # as a version 1 scan, or a system without /dev/disk
        api.scan(deep=True)
        self.assertEqual(api.load_index().volumes[str(self.disk)]["uuid"], "")
        self.box.unmount(self.disk)
        nothing = self.box.mount("Unrelated")  # no UUID either
        api.scan(deep=True)
        index = api.load_index()
        self.assertIn(str(self.disk), index.volumes)
        self.assertNotIn(str(nothing), index.volumes)
        self.assertEqual(sorted(s.id for s in index.sessions if s.offline), ["e", "f"])

    def test_a_volume_with_no_uuid_that_holds_the_same_stores_at_another_path(self):
        """AUDIT A-17 (``feat/t6_offline.py``, b): a volume whose id is not
        known came back at ``…1``; its 8 sessions were shown offline, and
        the copies on the mounted volume were hidden behind them."""
        self.box.unmount(self.disk)
        self.box.mount("Win Disk")  # no identity
        api.scan(deep=True)
        self.assertEqual(api.load_index().volumes[str(self.disk)]["uuid"], "")
        new = self.move()  # no identity at the new path either
        summary = api.scan()
        self.assertEqual((summary["stores_not_mounted"], summary["offline"]), (0, 0))
        index = api.load_index()
        self.assertNotIn(str(self.disk), index.volumes)
        foreign = [s for s in index.sessions if s.origin == "foreign"]
        self.assertEqual(sorted(s.id for s in foreign), ["e", "f"])
        self.assertEqual({(s.mount, s.offline) for s in foreign}, {(str(new), False)})

    def test_one_that_holds_other_stores_or_fits_two_volumes_is_kept(self):
        self.box.unmount(self.disk)
        self.box.mount("Win Disk")
        api.scan(deep=True)
        new = self.move()
        self.box.store(new / "Users" / "other")  # one store more than was remembered
        api.scan(deep=True)
        self.assertIn(str(self.disk), api.load_index().volumes)
        # Two volumes that both hold exactly what was remembered: not told apart.
        twin = self.box.root / "elsewhere" / "twin"
        twin.parent.mkdir()
        os.rename(new, twin)
        (twin / "Users" / "other" / ".fake" / "sessions").rmdir()
        (twin / "Users" / "other" / ".fake").rmdir()
        shutil.copytree(twin, self.box.root / "elsewhere" / "twin2")
        self.box.unmount(new)
        self.box.mount("twin", at=twin)
        self.box.mount("twin2", at=self.box.root / "elsewhere" / "twin2")
        api.scan(deep=True)
        self.assertIn(str(self.disk), api.load_index().volumes)

    def test_another_volume_at_another_path_changes_nothing(self):
        api.scan(deep=True)
        self.box.unmount(self.disk)
        self.box.mount("Unrelated", uuid=OTHER)
        api.scan(deep=True)
        self.assertEqual(sorted(s.id for s in api.list_sessions() if s.offline), ["e", "f"])

    def test_a_volume_mounted_at_two_paths_keeps_both(self):
        """btrfs: / and /home are one volume. Neither is "moved"."""
        api.scan(deep=True)
        twin = self.box.mount("Twin", uuid=WIN)
        store = self.box.store(twin / "Users" / "t")
        self.box.session(store, "t")
        api.scan(deep=True)
        self.assertEqual(sorted(s.id for s in api.list_sessions()), ["e", "f", "n", "t"])
        self.assertEqual({s.offline for s in api.list_sessions()}, {False})

    def test_the_native_store_is_never_dropped(self):
        api.scan(deep=True)
        index = Index.load()
        home = str(self.box.root / "home")
        index.volumes[home]["uuid"] = WIN
        self.assertEqual(index.forget(home), 0)
        self.assertEqual([s.origin for s in index.stores], ["native", "foreign"])


# --- volumes, mount, forget -----------------------------------------------------------


class DrivesCase(OfflineCase):
    """With devices: the volume is ``nvme2n1p3``, mounted where udisks would
    mount it. ``mount`` is the recorder of ``FakeDisks``; a successful start
    puts the volume back into the mount list."""

    def setUp(self):
        self.disks = None
        super().setUp()
        api.scan(deep=True)
        self.box.unmount(self.disk)
        self.disks.on_start = self.start
        self.mount_at = self.disk

    def mount_disk(self) -> Path:
        if self.disks is None:
            self.disks = FakeDisks(self, self.box.root / "machine")
            self.disks.device("nvme2n1p3", WIN)
        return self.box.mount(
            "Win Disk", at=self.disks.media / "user" / "Win Disk", uuid=WIN, fstype="ntfs3"
        )

    def start(self, argv):
        self.box.mount(self.mount_at.name, at=self.mount_at, uuid=WIN, fstype="ntfs3")
        return FakeProcess(out=b"Mounted /dev/nvme2n1p3 at somewhere\n")

    def volume(self, index=None) -> dict:
        (row,) = [v for v in api.volumes(index) if v["path"] == str(self.disk)]
        return row


class VolumesTest(DrivesCase):
    def test_rows_and_states(self):
        rows = api.volumes()
        home = str(self.box.root / "home")
        self.assertEqual([row["path"] for row in rows], sorted([home, str(self.disk)]))
        row = self.volume()
        self.assertRegex(row.pop("last_seen"), TIME)
        self.assertEqual(
            row,
            {
                "path": str(self.disk),
                "label": "Win Disk",
                "fs_label": "",
                "uuid": WIN,
                "fstype": "ntfs3",
                "letter": "C:",
                "state": "attached",
                "stores": 1,
                "sessions": 2,
            },
        )
        (linux,) = [v for v in rows if v["path"] == home]
        self.assertEqual(
            (linux["label"], linux["state"], linux["letter"], linux["stores"], linux["sessions"]),
            ("Linux", "mounted", None, 1, 1),
        )
        self.disks.detach("nvme2n1p3")
        self.assertEqual(self.volume()["state"], "absent")
        self.remount()
        self.assertEqual(self.volume()["state"], "mounted")

    def test_a_volume_mounted_somewhere_else_counts_as_mounted(self):
        self.box.mount("Elsewhere", uuid=WIN)
        self.assertEqual(self.volume()["state"], "mounted")

    def test_a_volume_without_a_known_id_is_absent(self):
        index = api.load_index()
        index.volumes[str(self.disk)]["uuid"] = ""
        self.assertEqual(self.volume(index)["state"], "absent")

    def test_nothing_is_started_to_tell_a_state(self):
        api.volumes()
        api.status()
        self.assertEqual(self.disks.started, [])

    def test_find_volume(self):
        path = str(self.disk)
        for ref in (path, path + "/", WIN, "Win Disk", "C:", "c:", f"  {WIN}\n"):
            with self.subTest(ref=ref):
                self.assertEqual(api.find_volume(ref)["path"], path)
        self.assertEqual(api.find_volume("Linux")["path"], str(self.box.root / "home"))

    def test_find_volume_by_the_filesystems_label(self):
        index = api.load_index()
        index.volumes[str(self.disk)]["fs_label"] = "Windows"
        self.assertEqual(api.find_volume("Windows", index)["path"], str(self.disk))

    def test_unknown_and_ambiguous(self):
        for ref in ("nope", "D:", "win disk", WIN.lower(), "/", "C", "0123456789ABCDEF"):
            with self.subTest(ref=ref), self.assertRaises(NotFound) as caught:
                api.find_volume(ref)
            self.assertEqual(
                caught.exception.reason, f"no remembered drive matches {ref!r}; `status` lists them"
            )
        for ref in ("", "  ", None, 5):
            with self.subTest(ref=ref), self.assertRaises(NotFound) as caught:
                api.find_volume(ref)
            self.assertEqual(caught.exception.reason, "no drive given")
        index = api.load_index()
        twin = str(self.box.root / "media" / "Twin")
        index.volumes[twin] = dict(index.volumes[str(self.disk)])
        with self.assertRaises(NotFound) as caught:
            api.find_volume("Win Disk", index)
        self.assertEqual(
            caught.exception.reason,
            f"'Win Disk' matches 2 drives: {self.disk}, {twin}; name one by its mount path",
        )
        self.assertEqual(api.find_volume(twin, index)["path"], twin)  # a path is exact
        with self.assertRaises(NotFound):
            api.find_volume(WIN, index)


class MountDriveTest(DrivesCase):
    def test_mounts_scans_and_returns_both(self):
        FakeProvider.parsed.clear()
        result = api.mount_drive("C:")
        self.assertEqual(sorted(result), ["mount_point", "scan"])
        self.assertEqual(result["mount_point"], str(self.disk))
        self.assertEqual(
            self.disks.started,
            [["udisksctl", "mount", "--block-device", str(self.disks.by_uuid / WIN)]],
        )
        scan = result["scan"]
        self.assertEqual((scan["sessions"], scan["offline"], scan["stores_not_mounted"]), (3, 0, 0))
        self.assertEqual(scan["mode"], "quick")  # the same mounts as at the last deep pass
        self.assertEqual({s.offline for s in api.list_sessions()}, {False})
        log = self.log()
        self.assertIn(f"INFO mount Win Disk (volume id {WIN}, last at {self.disk})", log)
        self.assertIn(f"INFO mount Win Disk: mounted at {self.disk}", log)
        self.assertRegex(log, r"INFO scan quick: .* 3 sessions \(0 on drives not mounted\)")

    def test_the_scan_follows_the_engines_own_policy(self):
        self.box.mount("New Stick")  # the set of mounts is not that of the last deep pass
        self.assertEqual(api.mount_drive(WIN)["scan"]["mode"], "deep")

    def test_mounted_at_another_path_than_it_was_remembered_at(self):
        self.mount_at = self.disks.media / "user" / "Win Disk1"
        os.rename(self.disk, self.mount_at)
        result = api.mount_drive("Win Disk")
        self.assertEqual(result["mount_point"], str(self.mount_at))
        self.assertEqual(result["scan"]["mode"], "deep")
        index = api.load_index()
        self.assertEqual(
            sorted(index.volumes), sorted([str(self.box.root / "home"), str(self.mount_at)])
        )
        self.assertEqual(
            {s.mount for s in index.sessions if s.origin == "foreign"}, {str(self.mount_at)}
        )
        self.assertEqual({s.offline for s in index.sessions}, {False})

    def test_a_failure_is_passed_on_and_nothing_is_scanned(self):
        self.disks.on_start = lambda argv: FakeProcess(1, err=b"Error mounting: Not authorized\n")
        scans = len(self.log().split(" INFO scan "))
        with self.assertRaises(MountFailed) as caught:
            api.mount_drive("C:")
        self.assertEqual(caught.exception.reason, "Error mounting: Not authorized")
        self.assertIn("WARNING mount Win Disk failed: Error mounting: Not authorized", self.log())
        self.assertEqual(len(self.log().split(" INFO scan ")), scans)
        self.assertEqual(self.volume()["state"], "attached")

    def test_no_answer_to_the_password_prompt(self):
        self.disks.on_start = lambda argv: FakeProcess(hangs=True)
        with self.assertRaises(MountFailed) as caught:
            api.mount_drive(WIN)
        self.assertEqual(
            caught.exception.reason, "no answer after 120 s (was the password prompt answered?)"
        )

    def test_the_cancel_reaches_the_wait_for_the_mount(self):
        cancel = threading.Event()
        cancel.set()  # the TUI quit before the mount began
        scans = len(self.log().split(" INFO scan "))
        with self.assertRaises(MountFailed) as caught:
            api.mount_drive("C:", cancel=cancel)
        self.assertEqual(caught.exception.reason, "cancelled")
        self.assertEqual(self.disks.started, [])
        self.assertEqual(len(self.log().split(" INFO scan ")), scans)

    def test_a_drive_that_is_not_attached(self):
        self.disks.detach("nvme2n1p3")
        with self.assertRaises(MountFailed) as caught:
            api.mount_drive("Win Disk")
        self.assertEqual(caught.exception.reason, f"no drive with the volume id {WIN} is attached")
        self.assertEqual(self.disks.started, [])

    def test_a_volume_whose_id_is_not_known_cannot_be_mounted_from_here(self):
        data = cache()
        data["volumes"][str(self.disk)]["uuid"] = ""
        write_cache(data)
        with self.assertRaises(MountFailed) as caught:
            api.mount_drive("Win Disk")
        self.assertEqual(
            caught.exception.reason,
            "the volume id of Win Disk is not known; mount the drive from your file manager",
        )
        self.assertEqual(self.disks.started, [])

    def test_one_that_is_mounted_is_only_scanned(self):
        self.remount()
        result = api.mount_drive("C:")
        self.assertEqual(result["mount_point"], str(self.disk))
        self.assertEqual(self.disks.started, [])
        self.assertEqual(result["scan"]["offline"], 0)
        self.assertIn(f"INFO mount Win Disk: already mounted at {self.disk}", self.log())

    def test_an_unknown_drive(self):
        with self.assertRaises(NotFound):
            api.mount_drive("Zephyro")
        self.assertEqual(self.disks.started, [])

    def test_a_hostile_volume_id_in_the_cache_never_reaches_a_command(self):
        """The cache is the user's own file, but what it says is checked
        again where it is used."""
        index = api.load_index()
        for uuid in ("$(reboot)", "a b", "../../sda1", "x;y"):
            index.volumes[str(self.disk)]["uuid"] = uuid
            with (
                self.subTest(uuid=uuid),
                mock.patch.object(api, "load_index", return_value=index),
                self.assertRaises(MountFailed),
            ):
                api.mount_drive(str(self.disk))
        self.assertEqual(self.disks.started, [])


class ForgetDriveTest(DrivesCase):
    def test_drops_the_volume_its_stores_and_their_parses(self):
        result = api.forget_drive("C:")
        self.assertEqual(
            result, {"path": str(self.disk), "label": "Win Disk", "stores": 1, "sessions": 2}
        )
        data = cache()
        self.assertEqual(list(data["volumes"]), [str(self.box.root / "home")])
        self.assertEqual([s["root"] for s in data["stores"]], [str(self.native)])
        self.assertEqual(list(data["parsed"]), [f"fake:{self.native}"])
        self.assertNotIn(str(self.disk), data["deep_mounts"])
        self.assertNotIn(str(self.disk), data["walk"])
        self.assertEqual([s.id for s in api.list_sessions()], ["n"])
        self.assertIn(
            f"INFO forget Win Disk ({self.disk}): 1 stores and 2 sessions dropped from the cache",
            self.log(),
        )
        self.assertEqual(sorted(os.listdir(self.foreign / "sessions")), ["e.json", "f.json"])
        with self.assertRaises(NotFound):
            api.forget_drive("C:")  # it is gone
        summary = api.scan()
        self.assertEqual((summary["sessions"], summary["offline"]), (1, 0))

    def test_back_and_scanned_it_is_found_again(self):
        api.forget_drive(WIN)
        self.remount()
        summary = api.scan()
        self.assertEqual(summary["mode"], "deep")  # it is not part of the last walk any more
        self.assertEqual(sorted(s.id for s in api.list_sessions()), ["e", "f", "n"])

    def test_skipped_stores_go_with_it(self):
        index = Index.load()
        index.skipped = [index_mod.Skipped("fake", str(self.disk / "x"), "why", str(self.disk))]
        index.save()
        api.forget_drive(str(self.disk))
        self.assertEqual(cache()["skipped"], [])

    def test_a_mounted_volume_is_refused(self):
        self.remount()
        before = cache_file().read_bytes()
        for ref in ("C:", "Linux"):
            with self.subTest(ref=ref), self.assertRaises(ApiError) as caught:
                api.forget_drive(ref)
            self.assertNotIsInstance(caught.exception, NotFound)
            self.assertRegex(
                caught.exception.reason,
                r"^the drive (Win Disk|Linux) is mounted; a scan would find it again\. "
                r"Nothing was forgotten$",
            )
        self.assertEqual(cache_file().read_bytes(), before)

    def test_one_that_is_mounted_at_another_path_is_refused_too(self):
        self.box.mount("Elsewhere", uuid=WIN)
        with self.assertRaises(ApiError):
            api.forget_drive(str(self.disk))
        self.assertEqual(len(api.list_sessions()), 3)

    def test_attached_or_not_it_can_be_forgotten(self):
        self.disks.detach("nvme2n1p3")
        self.assertEqual(api.forget_drive("Win Disk")["sessions"], 2)

    def test_a_cache_that_cannot_be_written(self):
        denied = PermissionError(13, "Permission denied")
        with (
            mock.patch.object(Index, "save", side_effect=denied),
            self.assertRaises(ApiError) as caught,
        ):
            api.forget_drive("C:")
        self.assertEqual(
            caught.exception.reason, f"{cache_file()}: cache not saved: Permission denied"
        )
        self.assertEqual(len(api.list_sessions()), 3)


# --- the command line ---------------------------------------------------------------


class CliTest(DrivesCase):
    def test_list_shows_them_and_mounted_leaves_them_out(self):
        code, out, err = run("list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"C: · offline\s+fake\s+title f\s+Site$")
        self.assertRegex(lines[1], r"C: · offline\s+fake\s+title e\s+gone$")
        self.assertRegex(lines[2], r"Linux\s+fake\s+title n")
        code, out, err = run("list", "--mounted")
        self.assertEqual((code, err, len(out.splitlines())), (0, "", 1))
        self.assertNotIn("offline", out)
        rows = json.loads(run("list", "--json")[1])
        self.assertEqual(
            [(r["id"], r["offline"]) for r in rows], [("f", True), ("e", True), ("n", False)]
        )
        self.assertEqual(
            [(r["cwd_local"], r["cwd_exists"], r["launchable"], r["note"]) for r in rows[:2]],
            [(None, False, False, "drive not mounted")] * 2,
        )
        mounted = json.loads(run("list", "--mounted", "--json")[1])
        self.assertEqual(mounted, rows[2:])
        self.assertEqual(run("list", "--mounted", "--provider", "fake", "--keys")[0], 0)
        self.assertIn("--mounted", run("list", "--help")[1])

    def test_list_mounted_without_a_cache_scans_first(self):
        cache_file().unlink()
        code, out, err = run("list", "--mounted", "--json")
        self.assertEqual((code, err), (0, "agent-sessions: no usable cache; scanning…\n"))
        self.assertEqual([r["id"] for r in json.loads(out)], ["n"])

    def test_scan_says_how_many_are_offline(self):
        code, out, _err = run("scan")
        self.assertEqual(code, 0)
        self.assertIn("stores: 1 (1 more on drives not mounted)", out)
        self.assertRegex(out, r"fake\s+1 store\s+3 sessions\s+2 offline")
        self.assertIn(
            "sessions: 1 listed of 1 candidate (0 parsed, 1 from cache); "
            "2 more offline, on drives not mounted",
            out,
        )
        self.remount()
        out = run("scan")[1]
        self.assertRegex(out, r"fake\s+2 stores\s+3 sessions\n")
        self.assertIn("sessions: 3 listed of 3 candidates (0 parsed, 3 from cache)\n", out)

    def test_status_shows_every_remembered_volume_with_its_state(self):
        def mounts_part() -> list[str]:
            out = run("status")[1]
            return (
                out[out.index("\nmounts\n") + 8 : out.index("\nstores\n")].strip("\n").split("\n")
            )

        home, disk = str(self.box.root / "home"), str(self.disk)
        lines = mounts_part()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], rf"^\s+Linux\s+{home}\s+mounted\s+\d+ dirs?, ")
        self.assertRegex(
            lines[1], rf"^  C:\s+Win Disk\s+{disk}\s+ntfs3\s+{WIN}\s+attached, not mounted$"
        )
        self.disks.detach("nvme2n1p3")
        self.assertRegex(mounts_part()[1], rf"\s+ntfs3\s+{WIN}\s+not attached$")
        self.remount()
        self.assertRegex(
            mounts_part()[1], rf"^  C:\s+Win Disk\s+{disk}\s+ntfs3\s+{WIN}\s+mounted\s+\d+ dir"
        )
        self.box.unmount(self.disk)
        data = cache()
        data["volumes"][disk]["uuid"] = ""
        write_cache(data)
        self.assertRegex(mounts_part()[1], rf"^  C:\s+Win Disk\s+{disk}\s+ntfs3\s+not mounted$")
        out = run("status")[1]
        self.assertRegex(out, r"fake\s+1 store\s+3 sessions \(2 offline\)")
        self.assertRegex(
            out,
            rf"fake\s+foreign\s+{self.foreign}\s+2 listed / 2 candidates / 0 skipped\s+not mounted",
        )
        self.assertRegex(out, rf"C:\s+->\s+{disk}\s+as last resolved; the drive is not mounted")

    def test_mount(self):
        code, out, err = run("mount", "C:")
        self.assertEqual(code, 0)
        self.assertEqual(
            err, "agent-sessions: mounting Win Disk… (the desktop may ask for your password)\n"
        )
        lines = out.splitlines()
        self.assertEqual(lines[0], f"mounted at {self.disk}")
        self.assertRegex(lines[1], r"^scan: quick, 2 mounts, ")
        self.assertIn("sessions: 3 listed of 3 candidates (0 parsed, 3 from cache)", lines)
        self.assertEqual(len(self.disks.started), 1)
        self.assertNotIn("offline", run("list")[1])
        # Mounted already: said so by doing nothing but the scan.
        self.assertEqual(run("mount", "Win Disk")[2], "")
        self.assertEqual(len(self.disks.started), 1)

    def test_mount_on_a_terminal_shows_the_walk_and_clears_the_line(self):
        self.box.mount("New Stick")  # the set of mounts changed: the scan after it walks
        code, out, err = self.answer("", "mount", "C:")
        self.assertEqual((code, out.splitlines()[0]), (0, f"mounted at {self.disk}"))
        self.assertIn("scan: deep, 3 mounts", out)
        first, *lines = err.split("\r\x1b[K")
        self.assertEqual(
            first, "agent-sessions: mounting Win Disk… (the desktop may ask for your password)\n"
        )
        self.assertRegex(lines[0], r"^scanning .+… \d+ dirs$")
        self.assertEqual(lines[-1], "")  # the progress line is gone at the end

    def test_mount_failures_exit_one_with_the_reason(self):
        self.disks.on_start = lambda argv: FakeProcess(1, err=b"Error: \x1b[31mNot authorized\n")
        code, out, err = run("mount", WIN)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.splitlines()[1:], ["agent-sessions: Error:  [31mNot authorized"])
        self.assertEqual(
            run("mount", "nope"),
            (1, "", "agent-sessions: no remembered drive matches 'nope'; `status` lists them\n"),
        )
        self.assertEqual(run("mount")[0], 2)
        self.disks.has_tool = False
        self.assertEqual(
            run("mount", WIN)[2].splitlines()[1],
            "agent-sessions: udisksctl is not installed; mount the drive from your file manager",
        )

    def test_mount_help(self):
        code, out, _err = run("mount", "--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: agent-sessions mount [-h] drive", out)
        for needle in (
            "label",
            "drive letter (C:)",
            "mount path",
            "volume id",
            "udisksctl mount",
            "password",
        ):
            self.assertIn(needle, " ".join(out.split()))
        self.assertEqual(self.disks.started, [])

    def test_forget_with_yes(self):
        self.assertEqual(
            run("forget", "C:", "--yes"),
            (0, f"forgot Win Disk ({self.disk}): 1 store, 2 sessions\n", ""),
        )
        self.assertEqual(len(run("list")[1].splitlines()), 1)
        self.assertEqual(
            run("forget", "C:", "--yes"),
            (1, "", "agent-sessions: no remembered drive matches 'C:'; `status` lists them\n"),
        )

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

    def test_forget_asks_on_a_terminal(self):
        question = f"Forget Win Disk ({self.disk}): 1 store, 2 sessions? [y/N] "
        for text in ("n\n", "\n", "", "maybe\n", "yy\n"):
            with self.subTest(text=text):
                self.assertEqual(
                    self.answer(text, "forget", "C:"),
                    (1, "", question + "agent-sessions: nothing was forgotten\n"),
                )
        self.assertEqual(len(api.list_sessions()), 3)
        code, out, err = self.answer(" Y \n", "forget", "C:")
        self.assertEqual((code, err), (0, question))
        self.assertEqual(out, f"forgot Win Disk ({self.disk}): 1 store, 2 sessions\n")
        self.assertEqual(len(api.list_sessions()), 1)

    def test_forget_with_yes_does_not_ask(self):
        self.assertEqual(self.answer("", "forget", "C:", "--yes")[2], "")

    def test_forget_where_nobody_can_be_asked(self):
        code, out, err = run("forget", "C:")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            f"agent-sessions: forget Win Disk ({self.disk}): 1 store, 2 sessions: "
            "not a terminal to ask on; pass --yes\n",
        )
        self.assertEqual(len(api.list_sessions()), 3)

    def test_forget_a_mounted_drive_is_refused_without_a_question(self):
        self.remount()
        code, out, err = self.answer("y\n", "forget", "C:")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "agent-sessions: the drive Win Disk is mounted; a scan would find it again. "
            "Nothing was forgotten\n",
        )

    def test_the_question_is_cleaned(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("yes\n")), contextlib.redirect_stderr(err):
            self.assertTrue(cli._ask("Forget Evil\x1b[2J\u202e\nDisk?"))
        self.assertEqual(err.getvalue(), "Forget Evil [2J  Disk? [y/N] ")

    def test_help_names_the_new_commands(self):
        out = run("--help")[1]
        for command in ("mount", "forget", "map"):
            self.assertIn(command, out)
        self.assertIn("--yes", run("forget", "--help")[1])


if __name__ == "__main__":
    unittest.main()
