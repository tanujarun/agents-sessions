"""mounts.py: mountinfo parsing, filtering, the env override."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: F401  (puts src on sys.path)

from agent_sessions import mounts
from agent_sessions.mounts import Mount, find_mount, parse_mountinfo

# Trimmed from a real /proc/self/mountinfo (user name changed).
MOUNTINFO = r"""
22 29 0:24 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw
23 29 0:25 / /sys rw,nosuid,nodev,noexec,relatime shared:2 - sysfs sys rw
24 29 0:7 / /dev rw,nosuid shared:8 - devtmpfs dev rw,size=4096k
25 29 0:26 / /run rw,nosuid,nodev shared:13 - tmpfs run rw,mode=755
29 1 0:29 /@ / rw,relatime shared:1 - btrfs /dev/mapper/root rw,ssd,subvol=/@
52 29 0:56 / /tmp rw,nosuid,nodev shared:27 - tmpfs tmpfs rw
90 29 0:29 /@pkg /var/cache/pacman/pkg rw,relatime shared:50 - btrfs /dev/mapper/root rw,subvol=/@pkg
91 29 0:29 /@home /home rw,relatime shared:52 - btrfs /dev/mapper/root rw,subvol=/@home
92 29 0:29 /@log /var/log rw,relatime shared:54 - btrfs /dev/mapper/root rw,subvol=/@log
95 29 259:5 / /boot rw,relatime shared:56 - vfat /dev/nvme1n1p5 rw,fmask=0077
96 95 259:6 / /boot/efi rw,relatime shared:57 - vfat /dev/nvme1n1p6 rw
180 25 0:79 / /run/user/1000 rw,nosuid,nodev,relatime shared:200 - tmpfs tmpfs rw
181 180 0:82 / /run/user/1000/gvfs rw,nosuid,nodev,relatime shared:210 - fuse.gvfsd-fuse gvfsd-fuse rw
182 180 0:83 / /run/user/1000/doc rw,nosuid,nodev,relatime shared:220 - fuse.portal portal rw
200 29 0:90 / /var/lib/docker/overlay2/abc/merged rw,relatime shared:230 - overlay overlay rw
201 29 7:0 / /var/lib/snapd/snap/core/1 ro,nodev,relatime shared:231 - squashfs /dev/loop0 ro
264 29 259:3 / /run/media/user/22EEA841EEA80F5B rw,nosuid,nodev,relatime shared:325 - ntfs3 /dev/nvme1n1p3 rw,uid=1000
364 29 259:11 / /run/media/user/No\040Other\040Way rw,nosuid,nodev,relatime shared:340 - ntfs3 /dev/nvme0n1p2 rw,uid=1000
414 29 259:12 / /run/media/user/Tab\011and\134slash rw,nosuid,nodev,relatime - fuseblk /dev/sdb1 rw
429 29 8:17 / /mnt/data rw,relatime shared:455 - ext4 /dev/sdb1 rw
430 29 8:17 /sub /mnt/data-bind rw,relatime shared:455 - ext4 /dev/sdb1 rw
431 29 0:99 / /mnt/nas rw,relatime shared:456 - nfs4 nas:/export rw
"""


class ParseMountinfoTest(unittest.TestCase):
    def setUp(self):
        self.mounts = parse_mountinfo(MOUNTINFO)
        self.by_path = {m.path: m for m in self.mounts}

    def test_real_filesystems_only_sorted(self):
        self.assertEqual(
            [m.path for m in self.mounts],
            [
                "/",
                "/home",
                "/mnt/data",
                "/run/media/user/22EEA841EEA80F5B",
                "/run/media/user/No Other Way",
                "/run/media/user/Tab\tand\\slash",
            ],
        )

    def test_escapes_are_decoded(self):
        self.assertIn("/run/media/user/No Other Way", self.by_path)
        self.assertEqual(self.by_path["/run/media/user/No Other Way"].label, "No Other Way")

    def test_labels(self):
        self.assertEqual(self.by_path["/"].label, "Linux")
        self.assertEqual(self.by_path["/home"].label, "Linux")
        self.assertEqual(self.by_path["/mnt/data"].label, "data")
        self.assertEqual(self.by_path["/run/media/user/22EEA841EEA80F5B"].label, "22EEA841EEA80F5B")

    def test_fstype_and_device(self):
        m = self.by_path["/run/media/user/22EEA841EEA80F5B"]
        self.assertEqual((m.fstype, m.device), ("ntfs3", "/dev/nvme1n1p3"))
        self.assertEqual(self.by_path["/home"].fstype, "btrfs")

    def test_filtered(self):
        for path in (
            "/proc",
            "/sys",
            "/dev",
            "/run",
            "/tmp",
            "/run/user/1000",
            "/run/user/1000/gvfs",
            "/run/user/1000/doc",  # pseudo, FUSE portals
            "/var/lib/docker/overlay2/abc/merged",  # overlay
            "/var/lib/snapd/snap/core/1",  # squashfs
            "/boot",
            "/boot/efi",  # boot partitions
            "/var/log",
            "/var/cache/pacman/pkg",  # other btrfs subvolumes
            "/mnt/data-bind",  # second mount of a device
            "/mnt/nas",
        ):  # network share
            with self.subTest(path=path):
                self.assertNotIn(path, self.by_path)

    def test_later_mount_on_the_same_point_wins(self):
        text = "1 0 0:1 / /mnt/x rw - tmpfs tmpfs rw\n2 0 8:1 / /mnt/x rw - ext4 /dev/sda1 rw\n"
        self.assertEqual([(m.path, m.fstype) for m in parse_mountinfo(text)], [("/mnt/x", "ext4")])
        self.assertEqual(parse_mountinfo("\n".join(reversed(text.splitlines()))), [])

    def test_garbage_lines_are_ignored(self):
        self.assertEqual(parse_mountinfo("nonsense\n\n1 2 3\n- - -\n"), [])
        self.assertEqual(parse_mountinfo(""), [])


class EnvOverrideTest(unittest.TestCase):
    def patch(self, value):
        env = mock.patch.dict(os.environ, {mounts.ENV: value})
        env.start()
        self.addCleanup(env.stop)

    def test_replaces_the_system_list(self):
        self.patch(json.dumps([{"path": "/t/b dir/", "label": "B"}, {"path": "/t/a"}]))
        self.assertEqual(mounts.list_mounts(), [Mount("/t/a", "a"), Mount("/t/b dir", "B")])
        self.assertEqual(mounts.mount_points(), {"/t/a", "/t/b dir"})

    def test_empty_list(self):
        self.patch("[]")
        self.assertEqual(mounts.list_mounts(), [])

    def test_bad_value_is_an_error(self):
        for value in ("{", '{"path": "/x"}', '[{"label": "no path"}]', "[3]"):
            with self.subTest(value=value):
                self.patch(value)
                with self.assertRaises(mounts.MountsError):
                    mounts.list_mounts()

    @unittest.skipUnless(os.path.exists(mounts.MOUNTINFO), "no mountinfo here")
    def test_system_list_has_the_root(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(mounts.ENV, None)
        self.assertIn("/", mounts.mount_points())
        found = mounts.list_mounts()
        self.assertEqual(found, sorted(found, key=lambda m: m.path))


class FindMountTest(unittest.TestCase):
    def test_longest_prefix_on_a_segment_boundary(self):
        ms = [Mount("/", "Linux"), Mount("/home", "Linux"), Mount("/run/media/u/Zeph", "Zeph")]
        self.assertEqual(find_mount("/home/u/.claude", ms).path, "/home")
        self.assertEqual(find_mount("/home", ms).path, "/home")
        self.assertEqual(find_mount("/homestead/x", ms).path, "/")
        self.assertEqual(find_mount("/run/media/u/Zeph/a b", ms).path, "/run/media/u/Zeph")
        self.assertEqual(find_mount(Path("/run/media/u/Zephyr"), ms).path, "/")
        self.assertIsNone(find_mount("/x", ms[1:]))


if __name__ == "__main__":
    unittest.main()
