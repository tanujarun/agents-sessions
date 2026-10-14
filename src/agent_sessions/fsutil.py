"""Where our own files live, and read-only access that leaves no trace.

Everything here is portable: the flags that only Linux has are looked up with
``getattr`` and silently left out elsewhere.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
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


def own_cache_dir() -> Path | None:
    """The cache directory when it is this program's own: the default one,
    or the directory ``AGENT_SESSIONS_CACHE`` names. None when the variable
    names the index file itself: the directory around it is one the user
    chose, and is not ours to tighten or to fill."""
    value = os.environ.get("AGENT_SESSIONS_CACHE")
    if not value:
        return cache_file().parent
    path = Path(value)
    return None if path.suffix == ".json" else path


def index_lock_file() -> Path:
    """The lock a scan holds from loading the cache to saving it
    (``index.Index.scan``): ``index.lock`` beside the index file."""
    return cache_file().with_suffix(".lock")


def state_dir() -> Path:
    value = os.environ.get("AGENT_SESSIONS_STATE")
    return Path(value) if value else home() / ".local" / "state" / "agent-sessions"


def config_dir() -> Path:
    return home() / ".config" / "agent-sessions"


def log_file() -> Path:
    """The log (``logs``): in the state directory, rotated there."""
    return state_dir() / "agent-sessions.log"


def handoff_dir() -> Path:
    """Where the transcript of a session is exported to for the tool that
    continues it as a new session (``api.continue_session``): one file per
    session, in the state directory."""
    return state_dir() / "handoff"


def lock_file() -> Path:
    """The file ``lock`` locks: one for everything that changes the state
    directory or puts a session into a native store."""
    return state_dir() / "agent-sessions.lock"


def existing_folder(target: object, what: str) -> str:
    """A folder the user named, checked and in one spelling
    (``os.path.normpath``: no trailing separator, no doubled one). ``what``
    says which folder it is, for the reason: ``the folder to import into``.

    It is the user's own input, not a name from a store, and it must be an
    absolute path of a directory that exists. Raises ``ValueError`` with the
    reason in plain words for anything else: text with a NUL, a relative
    path (``~`` is the caller's to expand), a path that is not there, a
    file. Reads only."""
    if not isinstance(target, str) or not target:
        raise ValueError(f"{what} is not a path")
    if "\0" in target:
        raise ValueError(f"{what} has a NUL character in its path")
    if not os.path.isabs(target):
        raise ValueError(f"{what} must be an absolute path: {target}")
    folder = os.path.normpath(target)
    if not os.path.lexists(folder):
        raise ValueError(f"{what} does not exist: {folder}")
    if not os.path.isdir(folder):
        raise ValueError(f"{what} is not a directory: {folder}")
    return folder


# --- our own directories ----------------------------------------------------

PRIVATE_FILE = 0o600  # the cache and the state hold titles, prompts and paths
PRIVATE_DIR = 0o700


def make_private_dir(path: StrPath, made: list[str] | None = None) -> list[str]:
    """``mkdir -p`` for one of our own directories. Each directory this call
    creates is added to ``made`` (outermost first) as it is created, and
    ``made`` is returned. The directory itself is created 0700, parents that
    are missing with the default mode (they are not ours: ``~/.cache``,
    ``~/.local/state``). One that exists is left as it is."""
    target = os.path.abspath(os.fspath(path))
    missing = []
    current = target
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    made = [] if made is None else made
    for directory in reversed(missing):
        try:
            os.mkdir(directory, PRIVATE_DIR if directory == target else 0o777)
        except FileExistsError:
            if not os.path.isdir(directory):
                raise
            continue  # another process made it meanwhile
        made.append(directory)
    return made


def keep_private(path: StrPath) -> bool:
    """Make one of our own directories that exists the owner's alone (0700)
    when its mode is wider: one an older version made with the default mode
    (0.1.0 made the cache directory 0755). Only a real directory, not a
    link, and only one this user owns; True when the mode was changed. Never
    raises: a directory that cannot be changed stays as it is."""
    try:
        found = os.lstat(path)
        mine = not hasattr(os, "geteuid") or found.st_uid == os.geteuid()
        if not stat.S_ISDIR(found.st_mode) or not mine:
            return False
        if not stat.S_IMODE(found.st_mode) & ~PRIVATE_DIR:
            return False
        os.chmod(path, PRIVATE_DIR)
    except OSError:
        return False
    return True


def process_alive(pid: int) -> bool:
    """Whether a process with this id exists. Where that cannot be asked
    without side effects (signal 0 is not a probe on Windows) the answer is
    yes, so that nothing of a live process is ever taken for left over."""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:  # not a process id on this system
        return False
    except OSError:  # it exists and belongs to someone else
        return True
    return True


def open_private(path: StrPath) -> io.TextIOWrapper:
    """Open one of our own files for writing as UTF-8 text, empty, and for
    its owner alone (0600): created so, and made so when a file of that name
    was there with a wider mode. The cache and the state directory hold
    titles, prompts and paths."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, PRIVATE_FILE)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, PRIVATE_FILE)
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def open_regular(path: StrPath, flags: int, mode: int = PRIVATE_FILE) -> int:
    """``os.open`` for one of our own files that is written to (the lock
    file, the journal): the descriptor of a regular file, or an error. The
    state directory is the user's own, trusted but possibly malformed: a
    named pipe under one of our names must not make a command wait for a
    reader (the open does not wait where the platform has O_NONBLOCK), and
    what is written into anything but a regular file is lost. Raises
    ``NotRegularFile`` for a pipe, a device or a socket; a directory fails
    in the open itself. ``flags`` are ``os.open``'s."""
    fd = os.open(path, flags | _NO_WAIT, mode)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        raise NotRegularFile(path)
    return fd


def fsync_dir(path: StrPath) -> bool:
    """Flush a directory to disk, so that a name just created, renamed or
    removed in it survives a crash. Best effort: False where a directory
    cannot be opened (Windows) or the filesystem refuses, never an error."""
    flag = getattr(os, "O_DIRECTORY", None)
    if flag is None:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | flag)
    except OSError:
        return False
    try:
        os.fsync(fd)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


# --- lock -------------------------------------------------------------------

LOCK_TIMEOUT = 30.0  # seconds ``lock`` waits for another holder by default
_LOCK_POLL = 0.05  # seconds between two attempts
# Windows locks a range of bytes, and a locked byte cannot be read by anyone
# else: the byte that is locked lies far behind the holder's process id.
_LOCK_BYTE = 1 << 30
_PID_TEXT_MAX = 32


class LockTimeout(TimeoutError):
    """``lock`` gave up: someone else held the lock for the whole wait.
    ``pid`` is the holder's process id when the lock file names one."""

    def __init__(self, path: StrPath, waited: float, pid: int | None = None) -> None:
        self.path = os.fspath(path)
        self.waited = waited
        self.pid = pid
        who = f"process {pid}" if pid is not None else "another process"
        super().__init__(f"{self.path} is locked by {who}; gave up after {waited:g} s")


def _try_lock(fd: int) -> bool:
    """One attempt at an exclusive lock on an open lock file; never waits.
    ``flock`` on POSIX, a byte-range lock on Windows; on a platform with
    neither there is no lock and the answer is yes."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        import fcntl
    except ImportError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return False
        raise
    return True


def _holder(path: str) -> int | None:
    """The process id the holder wrote into the lock file, when it is one.
    Read without waiting, and from a regular file only (``open_ro``)."""
    try:
        with open_ro(path) as fh:
            text = fh.read(_PID_TEXT_MAX).strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _still_named(fd: int, path: str) -> bool:
    """Whether the lock file that is open is still the one at ``path``; a
    link under that name is not it, wherever it leads."""
    if sys.platform == "win32":
        return True  # the file is never removed there, see _release
    try:
        return os.path.samestat(os.fstat(fd), os.lstat(path))
    except OSError:
        return False


def _note_holder(fd: int) -> None:
    """Write this process's id into the lock file, for whoever waits. The
    lock is the descriptor, not this number: a disk too full to take it does
    not cost the lock."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, b"%d\n" % os.getpid())
    except OSError:
        pass


def _open_lock(path: str) -> int:
    """The lock file, opened or created: a regular file under that very
    name. The holder's process id is written into it in place of what it
    held (``_note_holder``), so it is never opened through a symbolic link:
    a link under the lock's name is refused (``NotRegularFile``) whatever
    it points at, and so is a named pipe (``open_regular``)."""
    if os.path.islink(path):
        raise NotRegularFile(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return open_regular(path, flags)
    except OSError:
        if os.path.islink(path):  # put there after the look; O_NOFOLLOW refused it
            raise NotRegularFile(path) from None
        raise


def _acquire(path: str, deadline: float, timeout: float) -> int:
    """Open the lock file and lock it, by ``deadline`` (``timeout`` is what
    the caller asked for, for the error); the descriptor is the lock."""
    while True:
        fd = _open_lock(path)
        mine = False
        try:
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise LockTimeout(path, timeout, _holder(path))
                time.sleep(_LOCK_POLL)
            mine = _still_named(fd, path)
            if mine:
                _note_holder(fd)
                return fd
        except BaseException:
            if mine:
                _release(fd, path)  # interrupted with the lock in hand: no file is left
            else:
                os.close(fd)
            raise
        # The holder before us removed the file on its way out, and what was
        # locked just now is a file nobody can find: take the one that is
        # there now.
        os.close(fd)


def _release(fd: int, path: str) -> None:
    """Give the lock up and leave no lock file. The name goes while the lock
    is still held, so whoever waits on this file finds it gone and opens a
    new one (``_acquire``). Windows cannot remove an open file: there the
    lock file stays, empty of meaning once the lock is released."""
    try:
        if sys.platform != "win32":
            try:
                os.unlink(path)
            except OSError:
                pass
    finally:
        os.close(fd)  # closing releases the lock on every platform


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_HELD: dict[str, list[int]] = {}  # lock path -> [descriptor, depth], for this process


@contextlib.contextmanager
def lock(path: StrPath | None = None, timeout: float | None = None) -> Iterator[None]:
    """Hold the exclusive lock of the state directory (``lock_file``, or
    ``path``) for the block: between processes through the lock file,
    between the threads of this process through a lock of their own.

    Waits up to ``timeout`` seconds (``LOCK_TIMEOUT``) and then raises
    ``LockTimeout``, which names the process that holds it when the lock
    file says so. The thread that holds the lock may take it again. The
    directory of the lock file must exist.
    """
    name = os.path.abspath(os.fspath(path if path is not None else lock_file()))
    wait = LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + wait
    with _LOCKS_GUARD:
        mine = _THREAD_LOCKS.setdefault(name, threading.RLock())
    if not mine.acquire(timeout=wait):
        raise LockTimeout(name, wait, os.getpid())
    try:
        held = _HELD.get(name)
        if held is None:
            held = _HELD[name] = [_acquire(name, deadline, wait), 0]
        held[1] += 1
        try:
            yield
        finally:
            held[1] -= 1
            if not held[1]:
                del _HELD[name]
                _release(held[0], name)
    finally:
        mine.release()


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
