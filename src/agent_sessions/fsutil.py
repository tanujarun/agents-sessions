"""Where our own files live, and read-only access that leaves no trace.

Everything here is portable: the flags that only Linux has are looked up with
``getattr`` and silently left out elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

_NOATIME = getattr(os, "O_NOATIME", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOATIME
_DIR_FD = bool(_NOATIME) and os.scandir in os.supports_fd


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


def open_ro(path):
    """Open ``path`` for binary reading without updating its access time.

    O_NOATIME is refused (EPERM) for files the caller does not own; the plain
    open that follows either works or raises the real error.
    """
    if _NOATIME:
        try:
            return os.fdopen(os.open(path, _FILE_FLAGS | _NOATIME), "rb")
        except PermissionError:
            pass
    return os.fdopen(os.open(path, _FILE_FLAGS), "rb")


def _entries(path, pick):
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


def _real_dir(entry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def subdirs(path) -> list[str]:
    """Names of the real sub-directories of ``path`` (symlinks left out),
    unsorted. Raises OSError when ``path`` cannot be listed."""
    return _entries(path, _real_dir)


def listdir_ro(path) -> list[str]:
    """Like ``os.listdir`` without updating the directory's access time."""
    return _entries(path, lambda entry: True)
