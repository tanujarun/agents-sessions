"""Where our own files live, and read-only access that leaves no trace.

Everything here is portable: the flags that only Linux has are looked up with
``getattr`` and silently left out elsewhere.
"""

from __future__ import annotations

import errno
import io
import os
import stat
from collections.abc import Callable
from pathlib import Path

StrPath = str | os.PathLike[str]

_NOATIME = getattr(os, "O_NOATIME", 0)
# An open must never wait: without O_NONBLOCK, opening a named pipe blocks
# until something writes to it. O_NOCTTY: a terminal device that is opened
# must not become this process's controlling terminal.
_NO_WAIT = getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | _NO_WAIT
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOATIME | _NO_WAIT
_DIR_FD = bool(_NOATIME) and os.scandir in os.supports_fd


class NotRegularFile(OSError):
    """``open_ro`` was given something that is not a regular file: a named
    pipe, a device, a socket, a directory. An ``OSError``, so every caller
    that reports an unreadable file reports this one the same way."""

    def __init__(self, path: StrPath) -> None:
        super().__init__(errno.EINVAL, "not a regular file", os.fspath(path))


def home() -> Path:
    """The user's home; ``AGENT_SESSIONS_HOME`` replaces it (tests)."""
    value = os.environ.get("AGENT_SESSIONS_HOME")
    return Path(value) if value else Path.home()


def cache_file() -> Path:
    """The index file. ``AGENT_SESSIONS_CACHE`` names the cache directory, or
    the index file itself when it ends in ``.json``."""
    value = os.environ.get("AGENT_SESSIONS_CACHE")
    if not value:
        return home() / ".cache" / "agent-sessions" / "index.json"
    path = Path(value)
    return path if path.suffix == ".json" else path / "index.json"


def cache_dir() -> Path:
    return cache_file().parent


def state_dir() -> Path:
    value = os.environ.get("AGENT_SESSIONS_STATE")
    return Path(value) if value else home() / ".local" / "state" / "agent-sessions"


def config_dir() -> Path:
    return home() / ".config" / "agent-sessions"


def _open_fd(path: StrPath) -> int:
    """O_NOATIME is refused (EPERM) for files the caller does not own; the
    plain open that follows either works or raises the real error."""
    if _NOATIME:
        try:
            return os.open(path, _FILE_FLAGS | _NOATIME)
        except PermissionError:
            pass
    return os.open(path, _FILE_FLAGS)


def open_ro(path: StrPath) -> io.BufferedReader:
    """Open a regular file for binary reading without updating its access
    time. Never waits, and never reads from anything but a regular file:
    raises ``NotRegularFile`` for a named pipe, a device, a socket or a
    directory, whatever its name looks like (a link is followed first).

    The kind is looked at twice. Before the open, so that a device is not
    even opened (opening one can have effects of its own); and on the open
    descriptor, because the name can be given to something else in between.
    That open does not wait where the platform has O_NONBLOCK.
    """
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise NotRegularFile(path)
    fd = _open_fd(path)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise NotRegularFile(path)
    return os.fdopen(fd, "rb")


def _entries(path: StrPath, pick: Callable[[os.DirEntry[str]], bool]) -> list[str]:
    if _DIR_FD:
        try:
            fd = os.open(path, _DIR_FLAGS)
        except PermissionError:
            fd = -1
        if fd >= 0:
            try:
                # scandir duplicates the descriptor; entries are judged while
                # it is open because a later stat would go through it.
                with os.scandir(fd) as it:
                    return [e.name for e in it if pick(e)]
            finally:
                os.close(fd)
    with os.scandir(path) as it:
        return [e.name for e in it if pick(e)]


def _real_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def subdirs(path: StrPath) -> list[str]:
    """Names of the real sub-directories of ``path`` (symlinks left out),
    unsorted. Raises OSError when ``path`` cannot be listed."""
    return _entries(path, _real_dir)


def listdir_ro(path: StrPath) -> list[str]:
    """Like ``os.listdir`` without updating the directory's access time."""
    return _entries(path, lambda entry: True)
