"""Enumerate real mounted filesystems (platform layer).

Interface: ``list_mounts()`` (the filesystems worth searching),
``mount_points()`` (every mountpoint, so a walk can stop at them) and
``find_mount()``. The implementation below is the Linux one.
``AGENT_SESSIONS_MOUNTS`` (a JSON list of ``{"path", "label"}``) replaces the
system's answer in tests.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

ENV = "AGENT_SESSIONS_MOUNTS"
MOUNTINFO = "/proc/self/mountinfo"
NATIVE_LABEL = "Linux"

# Filesystems that hold a user's files on a disk. Everything else (proc,
# sysfs, tmpfs, overlay, squashfs, FUSE portals, network shares) is skipped.
REAL_FSTYPES = frozenset({
    "apfs", "bcachefs", "btrfs", "exfat", "ext2", "ext3", "ext4", "f2fs",
    "fuseblk", "hfs", "hfsplus", "jfs", "msdos", "nilfs2", "ntfs", "ntfs3",
    "reiserfs", "udf", "ufs", "vfat", "xfs", "zfs",
})
# Boot and EFI partitions never hold sessions.
SKIP_TREES = ("/boot", "/efi")
# The only mountpoints kept when a device is mounted more than once
# (btrfs subvolumes, bind mounts).
KEEP_ALWAYS = ("/", "/home")


class MountsError(ValueError):
    """``AGENT_SESSIONS_MOUNTS`` does not hold what it should."""


@dataclass(frozen=True)
class Mount:
    path: str
    label: str
    fstype: str = ""
    device: str = ""


def _unescape(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _entries(text: str) -> dict[str, tuple[str, str, str, str]]:
    """mountpoint -> (major:minor, root, fstype, source); a later line for the
    same mountpoint hides the earlier one."""
    found: dict[str, tuple[str, str, str, str]] = {}
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        head, tail = left.split(" "), right.split(" ")
        if len(head) < 5 or len(tail) < 2:
            continue
        found[_unescape(head[4])] = (head[2], _unescape(head[3]), tail[0],
                                     _unescape(tail[1]))
    return found


def _label(path: str) -> str:
    if path in KEEP_ALWAYS:
        return NATIVE_LABEL
    return os.path.basename(path) or path


def _under(path: str, tree: str) -> bool:
    return path == tree or path.startswith(tree + "/")


def parse_mountinfo(text: str) -> list[Mount]:
    """The real filesystems in a ``/proc/self/mountinfo`` text, sorted by path."""
    entries = _entries(text)
    kept: dict[str, Mount] = {}
    devices: set[str] = set()

    def consider(path: str) -> None:
        devid, _root, fstype, source = entries[path]
        if fstype not in REAL_FSTYPES or any(_under(path, t) for t in SKIP_TREES):
            return
        if devid in devices and path not in KEEP_ALWAYS:
            return
        devices.add(devid)
        kept[path] = Mount(path, _label(path), fstype, source)

    for path in KEEP_ALWAYS:
        if path in entries:
            consider(path)
    # Of several mounts of one device, prefer the one showing its root.
    for path in sorted(entries, key=lambda p: (entries[p][1] != "/", p)):
        if path not in kept:
            consider(path)
    return [kept[path] for path in sorted(kept)]


def _override() -> list[Mount] | None:
    raw = os.environ.get(ENV)
    if raw is None:
        return None
    try:
        items = json.loads(raw)
        return sorted(
            (Mount(str(i["path"]).rstrip("/") or "/", str(i.get("label") or
                   _label(str(i["path"]).rstrip("/") or "/")),
                   str(i.get("fstype", "")), str(i.get("device", "")))
             for i in items),
            key=lambda m: m.path)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise MountsError(f"{ENV} is not a JSON list of {{path, label}}: {exc}") from exc


def _read_mountinfo() -> str:
    if not sys.platform.startswith("linux"):
        return ""
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except OSError:
        return ""


def list_mounts() -> list[Mount]:
    """The mounted filesystems to search, sorted by path."""
    override = _override()
    if override is not None:
        return override
    return parse_mountinfo(_read_mountinfo())


def mount_points() -> set[str]:
    """Every mountpoint of the system, pseudo-filesystems included."""
    override = _override()
    if override is not None:
        return {m.path for m in override}
    return set(_entries(_read_mountinfo()))


def find_mount(path, mounts) -> Mount | None:
    """The mount that holds ``path``: the longest mountpoint that is a prefix."""
    text = os.fspath(path)
    best = None
    for mount in mounts:
        base = mount.path.rstrip("/")
        if text == mount.path or text.startswith(base + "/"):
            if best is None or len(mount.path) > len(best.path):
                best = mount
    return best
