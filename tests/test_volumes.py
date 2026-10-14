"""mounts.py: which volume a mount is (UUID and label from ``/dev/disk``),
whether a volume is mounted, attached or absent, and mounting one.

Everything runs on a fake ``/dev/disk`` tree and a fake mount table in a temp
directory (``support.FakeDisks``), with the program start replaced by a
recorder. No test here starts ``udisksctl`` or mounts anything."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeDisks, FakeProcess, finish, python_child

from agent_sessions import mounts
from agent_sessions.errors import ApiError, MountFailed
from agent_sessions.mounts import Mount

WIN = "22EEA841EEA80F5B"  # an NTFS volume without a label
NOW = "583E82883E825EBE"  # one labelled "No Other Way"
ROOT = "1d0c9da6-409d-477a-8970-1d40b6a5269e"
EFI = "325C-D35D"


class DisksCase(unittest.TestCase):
    """A machine with a btrfs root (``/`` and ``/home`` of one device, named
    through ``/dev/mapper``), two NTFS volumes and a boot partition. Which of
    them is mounted is up to the test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(mounts.ENV, None)  # the mount table is the fake file
        self.disks = FakeDisks(self, self.root)
        self.dm = self.disks.device("dm-0", ROOT, "OMARCHY")
        (self.disks.dev / "mapper").mkdir()
        os.symlink("../dm-0", self.disks.dev / "mapper" / "root")
        self.mapper = str(self.disks.dev / "mapper" / "root")
        self.win = self.disks.device("nvme2n1p3", WIN)
        self.now = self.disks.device("nvme0n1p2", NOW, "No Other Way")
        self.efi = self.disks.device("nvme2n1p5", EFI, "OMARCHY_EFI")
        self.win_at = str(self.disks.media / "user" / WIN)
        self.now_at = str(self.disks.media / "user" / "No Other Way")
        self.disks.mount(self.mapper, "/", "btrfs", "/@")
        self.disks.mount(self.mapper, "/home", "btrfs", "/@home")

    def by_path(self) -> dict:
        return {m.path: m for m in mounts.list_mounts()}


class IdentityTest(DisksCase):
    def test_uuid_and_label_of_every_mount(self):
        self.disks.mount(self.win, self.win_at)
        self.disks.mount(self.now, self.now_at)
        found = self.by_path()
        self.assertEqual(sorted(found), sorted(["/", "/home", self.win_at, self.now_at]))
        self.assertEqual(
            {path: (m.uuid, m.fs_label) for path, m in found.items()},
            {
                # btrfs: the source is a link to the device; both subvolumes are that volume
                "/": (ROOT, "OMARCHY"),
                "/home": (ROOT, "OMARCHY"),
                self.win_at: (WIN, ""),  # no label, and no entry under by-label
                self.now_at: (NOW, "No Other Way"),
            },
        )
        # What a listing calls the drive is not the filesystem's label.
        self.assertEqual(found[self.win_at].label, WIN)
        self.assertEqual(found["/"].label, "Linux")
        self.assertEqual(
            found[self.now_at],
            Mount(self.now_at, "No Other Way", "ntfs3", self.now, NOW, "No Other Way"),
        )

    def test_udev_escapes_in_a_label_are_decoded(self):
        self.assertTrue((self.disks.by_label / "No\\x20Other\\x20Way").is_symlink())
        for number, label in enumerate(("Daten ä", "back\\slash", "tab\there", "x\\x41y", "é")):
            with self.subTest(label=label):
                device = self.disks.device(f"sdz{number}", f"AAAA-000{number}", label)
                where = str(self.disks.media / "user" / f"m{number}")
                self.disks.mount(device, where, "vfat")
                self.assertEqual(self.by_path()[where].fs_label, label)
        self.assertEqual(mounts._udev_name("plain"), "plain")
        self.assertEqual(mounts._udev_name("a\\x2fb\\x5cx20"), "a/b\\x20")  # decoded once
        self.assertEqual(os.fsencode(mounts._udev_name("\\xff\\xfe")), b"\xff\xfe")  # not UTF-8

    def test_a_source_that_is_no_device_path_has_no_identity(self):
        self.disks.mount("tank/data", "/mnt/tank", "zfs")
        self.disks.mount("/dev/does-not-exist", "/mnt/gone", "ext4")
        found = self.by_path()
        for path in ("/mnt/tank", "/mnt/gone"):
            self.assertEqual((found[path].uuid, found[path].fs_label), ("", ""))

    def test_a_name_that_is_no_uuid_is_not_taken_for_one(self):
        device = self.disks.device("sdy1", label="Stick")
        for name in ("has space", "semi;colon", "x" * 65, "dot.dot"):
            os.symlink("../../sdy1", self.disks.by_uuid / name)
        self.disks.mount(device, "/mnt/stick", "vfat")
        self.assertEqual(self.by_path()["/mnt/stick"].uuid, "")
        self.assertEqual(self.by_path()["/mnt/stick"].fs_label, "Stick")

    def test_a_link_to_nothing_is_skipped(self):
        os.symlink("../../no-such-device", self.disks.by_uuid / "DEAD-BEEF")
        self.assertEqual(self.by_path()["/"].uuid, ROOT)

    def test_silent_where_the_directories_do_not_exist(self):
        """A container, another system: every mount without identity, no error."""
        self.disks.mount(self.win, self.win_at)
        plain = mounts.parse_mountinfo(self.disks.mountinfo.read_text(encoding="utf-8"))
        for missing in (str(self.root / "nowhere"), str(self.disks.mountinfo)):  # absent; a file
            with (
                self.subTest(missing=missing),
                mock.patch.object(mounts, "DEV_BY_UUID", missing),
                mock.patch.object(mounts, "DEV_BY_LABEL", missing),
            ):
                self.assertEqual(mounts.list_mounts(), plain)
                self.assertEqual({m.uuid for m in mounts.list_mounts()}, {""})
                self.assertEqual(mounts.volume_state(WIN), "absent")
                self.assertEqual(mounts.identity_from_mount_point(self.win_at), ("", ""))

    def test_parse_mountinfo_alone_adds_nothing(self):
        text = self.disks.mountinfo.read_text(encoding="utf-8")
        self.assertEqual({m.uuid for m in mounts.parse_mountinfo(text)}, {""})

    def test_the_override_takes_uuid_and_fs_label(self):
        os.environ[mounts.ENV] = json.dumps(
            [
                {
                    "path": "/t/a",
                    "label": "A",
                    "uuid": WIN,
                    "fs_label": "Windows",
                    "fstype": "ntfs3",
                },
                {"path": "/t/b", "uuid": "not a uuid!", "fs_label": None},
                {"path": "/t/c", "uuid": None},
            ]
        )
        self.assertEqual(
            mounts.list_mounts(),
            [
                Mount("/t/a", "A", "ntfs3", "", WIN, "Windows"),
                Mount("/t/b", "b"),  # what is not in the form of a UUID is none
                Mount("/t/c", "c"),
            ],
        )

    @unittest.skipUnless(os.path.exists("/proc/self/mountinfo"), "no mountinfo here")
    def test_the_real_system_answers_without_an_error(self):
        """Read-only, on whatever this machine has; a UUID is always in form."""
        with (
            mock.patch.object(mounts, "DEV_BY_UUID", "/dev/disk/by-uuid"),
            mock.patch.object(mounts, "DEV_BY_LABEL", "/dev/disk/by-label"),
            mock.patch.object(mounts, "MOUNTINFO", "/proc/self/mountinfo"),
        ):
            for mount in mounts.list_mounts():
                self.assertTrue(mount.uuid == "" or mounts.UUID_PATTERN.fullmatch(mount.uuid))
                self.assertIsInstance(mount.fs_label, str)


class MountPointNameTest(DisksCase):
    """``identity_from_mount_point``: udisks names the mount point after the
    label, else the UUID."""

    def test_by_label_then_by_uuid(self):
        self.assertEqual(mounts.identity_from_mount_point(self.now_at), (NOW, "No Other Way"))
        self.assertEqual(mounts.identity_from_mount_point(self.win_at), (WIN, ""))
        self.assertEqual(mounts.identity_from_mount_point(Path(self.win_at)), (WIN, ""))

    def test_a_label_wins_over_a_uuid_of_the_same_name(self):
        self.disks.device("sdx1", "AAAA-BBBB", WIN)  # a stick someone labelled like that UUID
        self.assertEqual(mounts.identity_from_mount_point(self.win_at), ("AAAA-BBBB", WIN))

    def test_a_label_without_a_uuid(self):
        self.disks.device("sdx2", label="Bare")
        self.assertEqual(
            mounts.identity_from_mount_point(self.disks.media / "user" / "Bare"), ("", "Bare")
        )

    def test_what_is_not_a_udisks_mount_point(self):
        media = self.disks.media
        os.symlink("../../nvme2n1p3", self.disks.by_uuid / "has space")
        for path in (
            "/",
            "/home",
            "/mnt/" + WIN,
            str(media),
            str(media / WIN),  # no user directory
            str(media / "user" / WIN / "deeper"),
            str(media / "user" / "Unknown Name"),
            str(media / "user" / ""),
            str(media) + "x/user/" + WIN,
            str(media / "user" / "has space"),  # a by-uuid name that is no UUID
        ):
            with self.subTest(path=path):
                self.assertEqual(mounts.identity_from_mount_point(path), ("", ""))

    def test_a_device_that_went_away(self):
        self.disks.detach("nvme2n1p3")
        self.assertEqual(mounts.identity_from_mount_point(self.win_at), ("", ""))


class VolumeStateTest(DisksCase):
    def test_mounted_attached_absent(self):
        self.assertEqual(mounts.volume_state(WIN), "attached")
        self.disks.mount(self.win, self.win_at)
        self.assertEqual(mounts.volume_state(WIN), "mounted")
        self.assertEqual(mounts.volume_state(NOW), "attached")
        self.assertEqual(mounts.volume_state(ROOT), "mounted")  # through /dev/mapper
        self.assertEqual(mounts.volume_state("0123456789ABCDEF"), "absent")
        self.assertEqual(mounts.volume_state(EFI), "attached")
        self.disks.detach("nvme0n1p2")
        self.assertEqual(mounts.volume_state(NOW), "absent")
        self.assertEqual(
            (mounts.MOUNTED, mounts.ATTACHED, mounts.ABSENT), ("mounted", "attached", "absent")
        )

    def test_mounted_under_any_filesystem_type_counts(self):
        """Not only the types worth searching: /boot is mounted too."""
        self.disks.mount(self.efi, "/boot", "vfat")
        self.assertNotIn("/boot", self.by_path())
        self.assertEqual(mounts.volume_state(EFI), "mounted")

    def test_a_link_to_something_that_is_no_block_device(self):
        self.disks.device("plain-file", "AAAA-0001", block=False)
        self.assertEqual(mounts.volume_state("AAAA-0001"), "absent")
        os.symlink(str(self.root), self.disks.by_uuid / "AAAA-0002")  # a directory
        self.assertEqual(mounts.volume_state("AAAA-0002"), "absent")

    def test_what_is_not_a_uuid_is_absent_and_never_looked_up(self):
        (self.root / "outside").write_bytes(b"")
        self.disks.block.add(str(self.root / "outside"))
        with mock.patch.object(mounts.os.path, "realpath", side_effect=AssertionError("looked")):
            for uuid in ("", " ", "../../../outside", "a/b", "a b", "x" * 65, "ä", None, 7, b"x"):
                with self.subTest(uuid=uuid):
                    self.assertEqual(mounts.volume_state(uuid), "absent")
                    # The lookup itself refuses too, whoever calls it.
                    self.assertIsNone(mounts._device_of(uuid))

    def test_with_the_override_the_mount_table_is_the_override(self):
        os.environ[mounts.ENV] = json.dumps([{"path": "/t/win", "uuid": WIN}, {"path": "/t/x"}])
        self.assertEqual(mounts.volume_state(WIN), "mounted")
        self.assertEqual(mounts.volume_state(NOW), "attached")  # the device tree still counts
        self.assertEqual(mounts.volume_state("0123456789ABCDEF"), "absent")
        self.disks.mount(self.now, self.now_at)  # the fake file is not read now
        self.assertEqual(mounts.volume_state(NOW), "attached")

    def test_is_block_device(self):
        # FakeDisks stands in for it everywhere else in this suite.
        with mock.patch.object(mounts, "is_block_device", self.disks.real_is_block_device):
            self.assertFalse(mounts.is_block_device(self.win))  # a plain file
            self.assertFalse(mounts.is_block_device(self.root))
            self.assertFalse(mounts.is_block_device(self.root / "missing"))
            if os.path.exists("/dev/null"):
                self.assertFalse(mounts.is_block_device("/dev/null"))  # a character device
            block = os.stat_result((stat.S_IFBLK | 0o660,) + (0,) * 9)
            with mock.patch.object(mounts.os, "stat", return_value=block):
                self.assertTrue(mounts.is_block_device("/dev/anything"))


class MountVolumeTest(DisksCase):
    def argv(self, uuid) -> list[str]:
        return ["udisksctl", "mount", "--block-device", str(self.disks.by_uuid / uuid)]

    def failure(self, *args, **kwargs) -> str:
        with self.assertRaises(MountFailed) as caught:
            mounts.mount_volume(*args, **kwargs)
        self.assertIsInstance(caught.exception, ApiError)
        return caught.exception.reason

    def test_runs_exactly_one_command_and_reads_the_mount_point_back(self):
        process = FakeProcess(out=b"Mounted /dev/nvme2n1p3 at /somewhere/else\n")

        def start(argv):
            self.disks.mount(self.win, self.win_at)
            return process

        self.disks.on_start = start
        # From the mount table, by device: not from what the tool printed.
        self.assertEqual(mounts.mount_volume(WIN), self.win_at)
        self.assertEqual(self.disks.started, [self.argv(WIN)])
        self.assertTrue(all(type(arg) is str for arg in self.disks.started[0]))
        self.assertNotIn("--no-user-interaction", self.disks.started[0])
        self.assertEqual(process.calls, [("communicate", 120.0)])
        self.assertEqual(mounts.MOUNT_TIMEOUT, 120.0)

    def test_with_the_real_names_the_command_is_the_documented_one(self):
        with (
            mock.patch.object(mounts, "DEV_BY_UUID", "/dev/disk/by-uuid"),
            # No lookup under the real /dev here: the device is handed in.
            mock.patch.object(mounts, "_device_of", return_value=self.win),
        ):
            self.disks.mounting = (self.win, self.win_at)
            mounts.mount_volume(WIN)
        self.assertEqual(
            self.disks.started,
            [["udisksctl", "mount", "--block-device", f"/dev/disk/by-uuid/{WIN}"]],
        )

    def test_the_timeout_is_handed_on(self):
        self.disks.mounting = (self.win, self.win_at)
        process = FakeProcess()
        self.disks.on_start = lambda argv: (self.disks.mount(*self.disks.mounting), process)[1]
        mounts.mount_volume(WIN, timeout=7.5)
        self.assertEqual(process.calls, [("communicate", 7.5)])

    def test_already_mounted_runs_nothing(self):
        self.disks.mount(self.win, self.win_at)
        self.assertEqual(mounts.mount_volume(WIN), self.win_at)
        self.assertEqual(mounts.mount_volume(ROOT), "/")  # of / and /home, the first
        self.assertEqual(self.disks.started, [])

    def test_of_two_mounts_of_a_device_the_one_showing_its_root(self):
        self.disks.mount(self.win, "/mnt/a-bind", root="/sub")
        self.disks.mount(self.win, "/mnt/whole")
        self.assertEqual(mounts.mount_volume(WIN), "/mnt/whole")

    def test_what_is_not_a_uuid_is_refused_before_anything(self):
        (self.root / "outside").write_bytes(b"")
        self.disks.block.add(str(self.root / "outside"))
        for uuid in ("", "../../../outside", "a b", "x;reboot", "$(reboot)", "x" * 65, None, 5):
            with self.subTest(uuid=uuid):
                self.assertEqual(
                    self.failure(uuid), "that is not the id of a volume; nothing was mounted"
                )
        self.assertEqual(self.disks.started, [])

    def test_a_uuid_that_looks_like_an_option_never_reaches_the_command_as_one(self):
        self.assertEqual(self.failure("-o"), "no drive with the volume id -o is attached")
        self.disks.device("sdo1", "--options")
        self.disks.mounting = (str(self.disks.dev / "sdo1"), "/mnt/o")
        mounts.mount_volume("--options")
        self.assertEqual(self.disks.started, [self.argv("--options")])
        self.assertTrue(os.path.isabs(self.disks.started[0][3]))  # a path, whatever the name

    def test_a_volume_that_is_not_attached_is_refused_before_anything(self):
        self.assertEqual(
            self.failure("0123456789ABCDEF"),
            "no drive with the volume id 0123456789ABCDEF is attached",
        )
        self.disks.device("plain-file", "AAAA-0001", block=False)
        self.assertEqual(
            self.failure("AAAA-0001"), "no drive with the volume id AAAA-0001 is attached"
        )
        self.assertEqual(self.disks.started, [])

    def test_no_udisksctl(self):
        self.disks.has_tool = False
        wanted = "udisksctl is not installed; mount the drive from your file manager"
        self.assertEqual(self.failure(WIN), wanted)
        self.assertEqual(self.disks.started, [])
        self.disks.has_tool = True  # on PATH a moment ago, gone when it is started

        def gone(argv):
            raise FileNotFoundError(2, "No such file or directory", "udisksctl")

        self.disks.on_start = gone
        self.assertEqual(self.failure(WIN), wanted)

        def denied(argv):
            raise PermissionError(13, "Permission denied", "udisksctl")

        self.disks.on_start = denied
        self.assertEqual(self.failure(WIN), "udisksctl could not be started: Permission denied")

    def test_a_failure_gives_the_tools_own_last_line(self):
        cases = (
            (
                dict(
                    code=1,
                    out=b"some chatter\n",
                    err=b"Error mounting /dev/nvme2n1p3: GDBus.Error:org.freedesktop.UDisks2.Error."
                    b"NotAuthorized: Not authorized to perform operation\n\n",
                ),
                "Error mounting /dev/nvme2n1p3: GDBus.Error:org.freedesktop.UDisks2.Error."
                "NotAuthorized: Not authorized to perform operation",
            ),
            (dict(code=1, err=b"first\nlast line\n  \n"), "last line"),
            (dict(code=2, out=b"only stdout says why\n"), "only stdout says why"),
            (dict(code=3), "udisksctl failed (exit status 3)"),
            (dict(code=1, err=b"\n \n"), "udisksctl failed (exit status 1)"),
            # Through textsafe: nothing a terminal would act on, one line.
            (dict(code=1, err=b"bad \x1b[2J\x07label\xff\n"), "bad  [2J label�"),
            (dict(code=1, err=b"x" * 5000), "x" * 300),
        )
        for options, wanted in cases:
            with self.subTest(wanted=wanted[:30]):
                self.disks.on_start = lambda argv, options=options: FakeProcess(**options)
                self.assertEqual(self.failure(WIN), wanted)

    def test_no_answer_in_time_ends_the_child(self):
        process = FakeProcess(hangs=True)
        self.disks.on_start = lambda argv: process
        self.assertEqual(
            self.failure(WIN), "no answer after 120 s (was the password prompt answered?)"
        )
        self.assertEqual(process.calls, [("communicate", 120.0), "terminate", ("communicate", 5.0)])
        self.disks.on_start = lambda argv: FakeProcess(hangs=True)
        self.assertEqual(self.failure(WIN, timeout=0.5)[:21], "no answer after 0.5 s")

    def test_a_child_that_ignores_the_request_is_killed(self):
        process = FakeProcess(stubborn=True)
        self.disks.on_start = lambda argv: process
        self.failure(WIN)
        self.assertEqual(
            process.calls,
            [
                ("communicate", 120.0),
                "terminate",
                ("communicate", 5.0),
                "kill",
                ("communicate", None),
            ],
        )

    def test_a_child_that_is_gone_when_it_is_stopped(self):
        process = FakeProcess(hangs=True)
        process.terminate = mock.Mock(side_effect=ProcessLookupError(3, "No such process"))
        self.disks.on_start = lambda argv: process
        self.assertIn("no answer after", self.failure(WIN))

    def test_an_interrupt_while_waiting_ends_the_child_too(self):
        process = FakeProcess()
        process.communicate = mock.Mock(side_effect=[KeyboardInterrupt(), (b"", b"")])
        process.terminate = mock.Mock()
        self.disks.on_start = lambda argv: process
        with self.assertRaises(KeyboardInterrupt):
            mounts.mount_volume(WIN)
        process.terminate.assert_called_once_with()

    def test_a_cancel_ends_the_wait_and_the_child(self):
        """The TUI sets the event when it quits during a mount: the wait
        looks at it between short waits, and the child is terminated."""
        cancel = threading.Event()
        process = FakeProcess(hangs=True)
        answer = process.communicate

        def communicate(timeout=None):
            if len(process.calls) == 2:  # two waits have run out
                cancel.set()
            return answer(timeout)

        process.communicate = communicate
        self.disks.on_start = lambda argv: process
        self.assertEqual(self.failure(WIN, cancel=cancel), mounts.CANCELLED)
        self.assertEqual(
            process.calls,
            [
                ("communicate", mounts._POLL),
                ("communicate", mounts._POLL),
                ("communicate", mounts._POLL),
                "terminate",
                ("communicate", 5.0),
            ],
        )
        self.assertEqual(mounts._waiting, set())

    def test_a_cancel_set_before_the_start_starts_nothing(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.failure(WIN, cancel=cancel), mounts.CANCELLED)
        self.assertEqual(self.disks.started, [])

    def test_with_a_cancel_the_short_waits_add_up_to_the_timeout(self):
        process = FakeProcess(hangs=True)
        self.disks.on_start = lambda argv: process
        reason = self.failure(WIN, timeout=0.625, cancel=threading.Event())
        self.assertEqual(reason, "no answer after 0.625 s (was the password prompt answered?)")
        self.assertEqual(mounts._POLL, 0.25)
        self.assertEqual(
            process.calls,
            [("communicate", 0.25), ("communicate", 0.25), ("communicate", 0.125)]
            + ["terminate", ("communicate", 5.0)],
        )

    def test_with_a_cancel_a_child_that_answers_is_read_as_before(self):
        self.disks.mounting = (self.win, self.win_at)
        process = FakeProcess()
        self.disks.on_start = lambda argv: (self.disks.mount(*self.disks.mounting), process)[1]
        self.assertEqual(mounts.mount_volume(WIN, cancel=threading.Event()), self.win_at)
        self.assertEqual(process.calls, [("communicate", mounts._POLL)])
        self.assertEqual(mounts._waiting, set())

    def test_the_child_is_known_while_it_is_waited_for(self):
        seen = []
        process = FakeProcess()
        answer = process.communicate

        def communicate(timeout=None):
            seen.append(process in mounts._waiting)
            return answer(timeout)

        process.communicate = communicate
        self.disks.on_start = lambda argv: process
        self.failure(WIN)  # success the mount table does not show; the wait is what counts
        self.assertEqual(seen, [True])
        self.assertEqual(mounts._waiting, set())

    def test_at_exit_every_child_still_waited_for_is_terminated(self):
        gone = FakeProcess()
        gone.terminate = mock.Mock(side_effect=ProcessLookupError(3, "No such process"))
        running = FakeProcess()
        with mock.patch.object(mounts, "_waiting", {gone, running}):
            mounts._stop_waiting()
            self.assertEqual(mounts._waiting, set())
        self.assertEqual(running.calls, ["terminate"])
        gone.terminate.assert_called_once_with()

    def test_success_that_the_mount_table_does_not_show(self):
        self.assertEqual(
            self.failure(WIN),
            "udisksctl reported success, but the drive is not in the mount table",
        )
        self.assertEqual(self.disks.started, [self.argv(WIN)])

    def test_with_the_override_the_mount_point_is_the_overrides(self):
        def start(argv):
            os.environ[mounts.ENV] = json.dumps([{"path": "/t/win", "uuid": WIN}])
            return FakeProcess()

        os.environ[mounts.ENV] = "[]"
        self.disks.on_start = start
        self.assertEqual(mounts.mount_volume(WIN), "/t/win")
        self.assertEqual(mounts.mount_volume(WIN), "/t/win")
        self.assertEqual(len(self.disks.started), 1)


class StarterTest(unittest.TestCase):
    """How the program is started (``mounts._start``), tried on a Python
    child: never on ``udisksctl``."""

    def test_the_suite_never_reaches_the_real_starter(self):
        with self.assertRaises(AssertionError):
            mounts.starter(["udisksctl", "mount", "--block-device", "/dev/disk/by-uuid/X"])

    def test_no_shell_no_terminal_own_session_output_captured(self):
        with mock.patch.object(mounts.subprocess, "Popen") as popen:
            self.assertIs(mounts._start(["tool", "a b", "$(c)"]), popen.return_value)
        popen.assert_called_once_with(
            ["tool", "a b", "$(c)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    @unittest.skipUnless(hasattr(os, "getsid"), "no sessions on this platform")
    def test_on_a_real_child(self):
        code = (
            "import os, sys\n"
            "null = os.stat(os.devnull).st_rdev\n"
            "print(os.getsid(0) == os.getpid(), os.fstat(0).st_rdev == null)\n"
            "print('to stderr', file=sys.stderr)\n"
        )
        child = mounts._start([sys.executable, "-c", code])
        self.assertIsInstance(child, subprocess.Popen)
        ((status, out, err),) = finish(self, [child])
        self.assertEqual((status, out, err), (0, b"True True\n", b"to stderr\n"))
        self.assertNotEqual(os.getsid(0), child.pid)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc to look at a process")
    def test_a_program_that_ends_during_a_mount_leaves_no_child(self):
        """The TUI's quit: the mount waits in a daemon thread, the cancel is
        set and the program ends at once, before the thread looks again.
        The child (a sleeping Python here, never udisksctl) is terminated
        on the way out all the same."""
        code = (
            "import os, subprocess, sys, threading, time, types\n"
            "from agent_sessions import mounts\n"
            "def start(argv):\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            "        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,\n"
            "        start_new_session=True)\n"
            "    print(child.pid, flush=True)\n"
            "    return child\n"
            "mounts.starter = start\n"
            "mounts._device_of = lambda uuid: '/dev/x'\n"
            "mounts._mounted_at = lambda uuid, device: None\n"
            "mounts.shutil = types.SimpleNamespace(which=lambda name: '/usr/bin/' + name)\n"
            "cancel = threading.Event()\n"
            "threading.Thread(target=mounts.mount_volume, args=('ABCD', 120.0, cancel),\n"
            "    daemon=True).start()\n"
            "while not mounts._waiting:\n"
            "    time.sleep(0.01)\n"
            "cancel.set()\n"
        )
        ((status, out, err),) = finish(self, [python_child(code)])
        self.assertEqual((status, err), (0, ""))
        pid = int(out)

        def running() -> bool:
            try:
                with open(f"/proc/{pid}/stat", encoding="ascii") as stat_file:
                    return stat_file.read().rsplit(")", 1)[1].split()[0] != "Z"
            except OSError:  # gone, or going
                return False

        deadline = time.monotonic() + 10
        while running() and time.monotonic() < deadline:
            time.sleep(0.05)
        if running():
            os.kill(pid, 9)
            self.fail("the mount's child outlived the program")

    def test_outside_the_suite_the_starter_is_the_real_one(self):
        """In a fresh interpreter, where ``support`` has replaced nothing.
        The child only looks at the module; it starts no program."""
        code = "from agent_sessions import mounts\nprint(mounts.starter is mounts._start)\n"
        ((status, out, _err),) = finish(self, [python_child(code)])
        self.assertEqual((status, out), (0, "True\n"))


if __name__ == "__main__":
    unittest.main()
