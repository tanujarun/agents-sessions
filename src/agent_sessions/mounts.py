"""Enumerate real mounted filesystems, and mount one (platform layer).

Interface: ``list_mounts()`` (the filesystems worth searching, each with the
UUID and the label of its filesystem when the system tells them),
``mount_points()`` (every mountpoint, so a walk can stop at them),
``find_mount()``, ``volume_state()`` (is a volume mounted, attached or
absent), ``mount_volume()`` and ``identity_from_mount_point()``. The
implementation below is the Linux one. ``AGENT_SESSIONS_MOUNTS`` (a JSON list
of ``{"path", "label"}``, optionally with ``fstype``, ``device``, ``uuid``
and ``fs_label``) replaces the system's answer in tests.

Identity comes from files only (docs/FORMATS.md "Q4"): the links in
``/dev/disk/by-uuid`` and ``/dev/disk/by-label`` that resolve to the device a
mount is on. Where those directories do not exist (a container, another
system) every mount simply has no UUID and no filesystem label.

``mount_volume`` is the one place outside ``launch`` that starts a program:
``udisksctl mount``, through the module-level hook ``starter`` (called as
``starter(argv)``, returns the started process), which tests replace.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import textsafe
from .errors import MountFailed

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .fsutil import StrPath

ENV = "AGENT_SESSIONS_MOUNTS"
MOUNTINFO = "/proc/self/mountinfo"
NATIVE_LABEL = "Linux"
# udev's links to every block device that holds a filesystem, by the
# filesystem's UUID and by its label.
DEV_BY_UUID = "/dev/disk/by-uuid"
DEV_BY_LABEL = "/dev/disk/by-label"
# udisks mounts a volume at <MEDIA_ROOT>/<user>/<label, or UUID without one>.
MEDIA_ROOT = "/run/media"
# The form of a filesystem UUID, used with ``fullmatch`` (NTFS: 16 hex
# digits; FAT: ``325C-D35D``; the others a UUID with hyphens). A UUID is read
# from a disk anyone may have plugged in and ends up in a command line and in
# a path below ``DEV_BY_UUID``: nothing that does not match is ever one.
UUID_PATTERN = re.compile(r"[0-9A-Za-z-]{1,64}")

MOUNTED = "mounted"
ATTACHED = "attached"  # the device is there, its filesystem is not mounted
ABSENT = "absent"

UDISKSCTL = "udisksctl"
MOUNT_TIMEOUT = 120.0  # seconds ``mount_volume`` waits: a password may have to be typed
NOT_INSTALLED = f"{UDISKSCTL} is not installed; mount the drive from your file manager"
_STOP_WAIT = 5.0  # seconds a terminated child gets before it is killed
_REASON_MAX = 300  # characters of the tool's own message that are passed on

# Filesystems that hold a user's files on a disk. Everything else (proc,
# sysfs, tmpfs, overlay, squashfs, FUSE portals, network shares) is skipped.
REAL_FSTYPES = frozenset(
    {
        "apfs",
        "bcachefs",
        "btrfs",
        "exfat",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "fuseblk",
        "hfs",
        "hfsplus",
        "jfs",
        "msdos",
        "nilfs2",
        "ntfs",
        "ntfs3",
        "reiserfs",
        "udf",
        "ufs",
        "vfat",
        "xfs",
        "zfs",
    }
)
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
    label: str  # what the drive is called in a listing: ``Linux``, or the mount point's name
    fstype: str = ""
    device: str = ""
    uuid: str = ""  # of the filesystem; empty when the system does not tell
    fs_label: str = ""  # the filesystem's own label; empty when it has none


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
        found[_unescape(head[4])] = (head[2], _unescape(head[3]), tail[0], _unescape(tail[1]))
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
            (
                Mount(
                    str(i["path"]).rstrip("/") or "/",
                    str(i.get("label") or _label(str(i["path"]).rstrip("/") or "/")),
                    str(i.get("fstype", "")),
                    str(i.get("device", "")),
                    _uuid(str(i.get("uuid") or "")),
                    str(i.get("fs_label") or ""),
                )
                for i in items
            ),
            key=lambda m: m.path,
        )
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
    """The mounted filesystems to search, sorted by path, each with its
    filesystem's UUID and label where the system tells them."""
    override = _override()
    if override is not None:
        return override
    return _identified(parse_mountinfo(_read_mountinfo()))


def mount_points() -> set[str]:
    """Every mountpoint of the system, pseudo-filesystems included."""
    override = _override()
    if override is not None:
        return {m.path for m in override}
    return set(_entries(_read_mountinfo()))


def find_mount(path: StrPath, mounts: Iterable[Mount]) -> Mount | None:
    """The mount that holds ``path``: the longest mountpoint that is a prefix."""
    text = os.fspath(path)
    best = None
    for mount in mounts:
        base = mount.path.rstrip("/")
        if text == mount.path or text.startswith(base + "/"):  # noqa: SIM102  (two questions)
            if best is None or len(mount.path) > len(best.path):
                best = mount
    return best


# --- volume identity ----------------------------------------------------------


def _uuid(text: str) -> str:
    """``text`` when it has the form of a filesystem UUID, else nothing."""
    return text if UUID_PATTERN.fullmatch(text) else ""


def _udev_name(name: str) -> str:
    """A name under ``/dev/disk/by-*`` as the filesystem has it: udev writes
    every byte it does not trust as ``\\xNN`` (``No\\x20Other\\x20Way``)."""
    raw = re.sub(rb"\\x([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), os.fsencode(name))
    return os.fsdecode(raw)


def _links(directory: str) -> dict[str, str]:
    """name (decoded) -> the device its link resolves to, for one of the
    ``/dev/disk/by-*`` directories; nothing when it cannot be listed."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return {}
    found: dict[str, str] = {}
    for name in names:
        try:
            device = os.path.realpath(os.path.join(directory, name), strict=True)
        except OSError:
            continue  # a link to a device that just went away
        found.setdefault(_udev_name(name), device)
    return found


def _name_of(device: str, links: dict[str, str]) -> str:
    """The first name, in sorted order, whose link resolves to ``device``."""
    return next((name for name in sorted(links) if links[name] == device), "")


def _resolved(source: str) -> str:
    """The device behind a mount's source field. The source is followed as a
    path (``/dev/mapper/root`` is a link to ``/dev/dm-0``); the major:minor
    pair of the mount is not used, since btrfs shows one that belongs to no
    device. Nothing for a source that is not a path (``tmpfs``, ``nas:/x``)."""
    return os.path.realpath(source) if source.startswith("/") else ""


def _identified(found: list[Mount]) -> list[Mount]:
    """``found`` with ``uuid`` and ``fs_label`` filled in, best effort."""
    uuids, labels = _links(DEV_BY_UUID), _links(DEV_BY_LABEL)
    if not uuids and not labels:
        return found
    identified = []
    for mount in found:
        device = _resolved(mount.device)
        identified.append(
            dataclasses.replace(
                mount,
                uuid=_uuid(_name_of(device, uuids)) if device else "",
                fs_label=_name_of(device, labels) if device else "",
            )
        )
    return identified


def identity_from_mount_point(path: StrPath) -> tuple[str, str]:
    """``(uuid, filesystem label)`` of the volume udisks would mount at
    ``path``, told from the path's name alone; two empty strings when the
    path is not ``<MEDIA_ROOT>/<user>/<name>`` or no attached device goes by
    that name.

    udisks names a mount point after the filesystem's label, or after its
    UUID when it has none (docs/FORMATS.md "Q4"). This is how a volume that
    was remembered by its mount path alone gets its identity back while it
    is not mounted."""
    text = os.fspath(path)
    base = MEDIA_ROOT.rstrip("/") + "/"
    if not text.startswith(base):
        return "", ""
    parts = text[len(base) :].split("/")
    if len(parts) != 2 or not all(parts):
        return "", ""
    name = parts[1]
    uuids, labels = _links(DEV_BY_UUID), _links(DEV_BY_LABEL)
    if name in labels:
        return _uuid(_name_of(labels[name], uuids)), name
    if name in uuids and _uuid(name):
        return name, _name_of(uuids[name], labels)
    return "", ""


def is_block_device(path: StrPath) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def _device_of(uuid: str) -> str | None:
    """The block device whose filesystem has this UUID, or None when no such
    device is attached (or ``uuid`` is not in the form of one)."""
    if not isinstance(uuid, str) or not _uuid(uuid):
        return None
    try:
        device = os.path.realpath(os.path.join(DEV_BY_UUID, uuid), strict=True)
    except OSError:
        return None
    return device if is_block_device(device) else None


def _mounted_at(uuid: str, device: str | None) -> str | None:
    """Where a volume is mounted now, read from the mount table: the mount
    whose source is ``device``. Of several (bind mounts, subvolumes) the one
    that shows the filesystem's root, then the first by path. With the
    ``AGENT_SESSIONS_MOUNTS`` override, the mount it lists with ``uuid``."""
    override = _override()
    if override is not None:
        return next((m.path for m in override if m.uuid == uuid), None)
    if device is None:
        return None
    entries = _entries(_read_mountinfo())
    hits = sorted(
        (root != "/", path)
        for path, (_devid, root, _fstype, source) in entries.items()
        if _resolved(source) == device
    )
    return hits[0][1] if hits else None


def volume_state(uuid: str) -> str:
    """``mounted``, ``attached`` (the device is there and its filesystem is
    not mounted) or ``absent``, for the volume with this filesystem UUID.
    Read from files only: ``DEV_BY_UUID`` and the mount table."""
    if not isinstance(uuid, str) or not _uuid(uuid):
        return ABSENT
    device = _device_of(uuid)
    if _mounted_at(uuid, device) is not None:
        return MOUNTED
    return ATTACHED if device is not None else ABSENT


# --- mounting -----------------------------------------------------------------


def _start(argv: list[str]) -> subprocess.Popen[bytes]:
    """Start ``argv`` with no terminal attached and its output captured.

    A session of its own: when the mount needs a password, polkit's text
    agent would otherwise ask for it on the terminal this program runs in,
    under a full-screen interface that cannot show the question. With no
    terminal to ask on, the desktop's graphical agent asks."""
    return subprocess.Popen(  # noqa: S603  (an argv list built by this module, no shell)
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


starter = _start  # starter(argv): start the mount program, return the process


def _stop(proc: Any) -> None:  # whatever ``starter`` returned
    """End a child that is not waited for any longer, and collect it."""
    try:
        proc.terminate()
        try:
            proc.communicate(timeout=_STOP_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
    except OSError:
        pass  # it ended by itself in the meantime


def _last_line(*outputs: object) -> str:
    """The last line that says something, of the first output that has one,
    as a reason to show: one clean line of bounded length."""
    for output in outputs:
        text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output or "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return textsafe.clean(lines[-1])[:_REASON_MAX]
    return ""


def mount_volume(uuid: str, timeout: float = MOUNT_TIMEOUT) -> str:
    """Mount the volume with this filesystem UUID and return its mount
    point. One that is mounted already is returned as it is, and nothing is
    run.

    Runs ``udisksctl mount --block-device <DEV_BY_UUID>/<uuid>`` and waits up
    to ``timeout`` seconds for it: an internal volume needs the user's
    password, which the desktop's polkit agent asks for. The mount point is
    read back from the mount table, not from what the tool printed. Raises
    ``MountFailed`` with the reason: the UUID is not one or no such device is
    attached (nothing is run then), the tool is missing, its own last line
    when it fails, or no answer in time (the child is terminated)."""
    if not isinstance(uuid, str) or not _uuid(uuid):
        raise MountFailed("that is not the id of a volume; nothing was mounted")
    device = _device_of(uuid)
    if device is None:
        raise MountFailed(f"no drive with the volume id {uuid} is attached")
    where = _mounted_at(uuid, device)
    if where is not None:
        return where
    if shutil.which(UDISKSCTL) is None:
        raise MountFailed(NOT_INSTALLED)
    argv = [UDISKSCTL, "mount", "--block-device", os.path.join(DEV_BY_UUID, uuid)]
    try:
        proc = starter(argv)
    except FileNotFoundError:
        raise MountFailed(NOT_INSTALLED) from None
    except OSError as exc:
        raise MountFailed(f"{UDISKSCTL} could not be started: {exc.strerror or exc}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(proc)
        raise MountFailed(
            f"no answer after {timeout:g} s (was the password prompt answered?)"
        ) from None
    except BaseException:
        _stop(proc)  # interrupted while waiting: the child is not left behind
        raise
    if proc.returncode != 0:
        reason = _last_line(err, out)
        raise MountFailed(reason or f"{UDISKSCTL} failed (exit status {proc.returncode})")
    where = _mounted_at(uuid, device)
    if where is None:
        raise MountFailed(f"{UDISKSCTL} reported success, but the drive is not in the mount table")
    return where
