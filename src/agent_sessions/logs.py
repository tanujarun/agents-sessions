"""The log: ``<state dir>/agent-sessions.log`` (docs/SPEC.md "Logs").

A rotating file (1 MB, three older files kept), UTF-8, created 0600 in a
state directory created 0700; a log that is there with a wider mode is
tightened when it is opened. What goes into it is decided where it happens
(``api`` for scans, imports and launches, so the CLI and the TUI log alike);
this module is the plumbing:

- nothing is set up, and no file or directory made, until the first message
  is written (importing the package creates nothing);
- a log that cannot be written never breaks a command and never prints: with
  a read-only or missing state directory the messages are dropped. Only a
  regular file is written to, and opening it never waits: a named pipe or a
  device under the log's name is left alone and the messages are dropped;
- a message is never lost to its own text: a character the file cannot
  hold is written as a backslash escape;
- every message is made safe on its way out (``textsafe.clean``): a log is
  read in a terminal, and titles, folders and ids come from stores on other
  drives. A traceback goes below its message, every line of it indented, so
  nothing in it can pass for a line of the log;
- ``debug(True)`` (the CLI's ``--debug``) lowers the level to DEBUG and
  copies the log to stderr.

Use: ``logs.logger().info("scan: %s", ...)``, or a module logger below
``agent_sessions`` once ``logger()`` has been called.

Standard library only.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import stat
import sys
import threading
import time
from pathlib import Path

from . import fsutil, textsafe

NAME = "agent_sessions"  # the logger everything in the package logs below
MAX_BYTES = 1024 * 1024  # the file is rotated before it grows past this
BACKUPS = 3  # agent-sessions.log.1 to .3
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # UTC
_INDENT = "  "  # in front of every line of a traceback
_LOGGED = "_agent_sessions_logged"  # set on an exception ``unexpected`` wrote


class SafeFormatter(logging.Formatter):
    """``<UTC time> <process id> <level> <message>``, one line, with nothing
    in it a terminal would act on; a traceback follows, indented."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime(TIME_FORMAT, time.gmtime(record.created))
        message = textsafe.clean(record.getMessage())
        lines = [f"{stamp} {record.process} {record.levelname} {message}"]
        if record.exc_info:
            trace = self.formatException(record.exc_info)
            lines += [_INDENT + textsafe.clean(row) for row in trace.splitlines()]
        return "\n".join(lines)


# An open must never wait: a named pipe under the log's name would hold
# every command that logs until something read from it. O_NOCTTY: a terminal
# device under that name must not become this process's controlling terminal.
_NO_WAIT = getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)


class PrivateRotatingFile(logging.handlers.RotatingFileHandler):
    """The log file: opened only when there is something to write, created
    0600 in a directory created 0700, and silent when that cannot be done
    (the next message tries again).

    Rotation is per process: two processes that log at the same time (the
    TUI and a command beside it) each rename the file when they find it
    full, so a few lines can end up in an older file than their neighbours.
    Nothing is lost by it, and a log is not worth a lock."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUPS,
            encoding="utf-8",
            # A message that names a path which is not UTF-8 is written with
            # the odd bytes spelled out, not dropped.
            errors="backslashreplace",
            delay=True,
        )

    def _open(self) -> io.TextIOWrapper:
        """Open the log for appending. Raises ``OSError`` for anything under
        its name that is not a regular file (``emit`` then drops the message,
        ``handleError``): looked at before the open, so that a device is not
        even opened, and again on the descriptor, since the name can be
        given to something else in between."""
        fsutil.make_private_dir(os.path.dirname(self.baseFilename))
        try:
            regular = stat.S_ISREG(os.stat(self.baseFilename).st_mode)
        except FileNotFoundError:
            regular = True  # about to be created
        if not regular:
            raise fsutil.NotRegularFile(self.baseFilename)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NO_WAIT
        fd = os.open(self.baseFilename, flags, fsutil.PRIVATE_FILE)
        try:
            found = os.fstat(fd)
            if not stat.S_ISREG(found.st_mode):
                raise fsutil.NotRegularFile(self.baseFilename)
            if stat.S_IMODE(found.st_mode) & ~fsutil.PRIVATE_FILE and hasattr(os, "fchmod"):
                os.fchmod(fd, fsutil.PRIVATE_FILE)  # made by an older version, or by hand
            return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(fd)
            raise

    def handleError(self, record: logging.LogRecord) -> None:
        """A message that cannot be written is dropped: no traceback on
        stderr (what ``logging`` does by default), no exception."""


class QuietStream(logging.StreamHandler):
    """The copy on stderr; like the file, it never reports a failure of
    its own."""

    def handleError(self, record: logging.LogRecord) -> None:
        pass


_guard = threading.RLock()
_target: Path | None = None  # the log file the logger is set up for
_file: PrivateRotatingFile | None = None
_stderr: QuietStream | None = None
_debug = False


def _set_up(log: logging.Logger, target: Path) -> None:
    """Point the logger at ``target``. Makes no file: the handler opens it
    with the first message."""
    global _target, _file
    if _file is not None:
        log.removeHandler(_file)
        _file.close()
    _file = PrivateRotatingFile(target)
    _file.setFormatter(SafeFormatter())
    log.addHandler(_file)
    log.propagate = False  # never through the root logger to a stream of its own
    log.setLevel(logging.DEBUG if _debug else logging.INFO)
    _target = target


def logger() -> logging.Logger:
    """The package's logger, with the log file behind it. Set up on the
    first call, and again when the state directory is another one."""
    log = logging.getLogger(NAME)
    target = fsutil.log_file()
    with _guard:
        if target != _target:
            _set_up(log, target)
    return log


def debug(on: bool = True) -> None:
    """``--debug``: log at DEBUG, and to stderr as well as to the file."""
    global _debug, _stderr
    log = logging.getLogger(NAME)
    with _guard:
        _debug = bool(on)
        if _stderr is not None:
            log.removeHandler(_stderr)
            _stderr = None
        if _debug:
            _stderr = QuietStream(sys.stderr)
            _stderr.setFormatter(SafeFormatter())
            log.addHandler(_stderr)
        log.setLevel(logging.DEBUG if _debug else logging.INFO)
    logger()


def close() -> None:
    """Let go of the log file and of stderr; the next ``logger()`` sets up
    again. For a process that changes its state directory (the tests)."""
    global _target, _file, _stderr, _debug
    log = logging.getLogger(NAME)
    with _guard:
        for handler in (_file, _stderr):
            if handler is not None:
                log.removeHandler(handler)
                handler.close()
        _target = _file = _stderr = None
        _debug = False


def written() -> Path | None:
    """The log file when there is one to point a person at, else None."""
    path = fsutil.log_file()
    return path if path.is_file() else None


def unexpected(exc: BaseException, where: str = "") -> None:
    """Log an exception nobody planned for, with its traceback. Once per
    exception, however many layers it passes on its way up."""
    if getattr(exc, _LOGGED, False):
        return
    try:
        setattr(exc, _LOGGED, True)
    except (AttributeError, TypeError):
        pass  # an exception that takes no attributes: logged again higher up, no harm
    place = f" in {where}" if where else ""
    logger().error(
        "unexpected error%s: %s: %s",
        place,
        type(exc).__name__,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


__all__ = [
    "BACKUPS",
    "MAX_BYTES",
    "NAME",
    "PrivateRotatingFile",
    "SafeFormatter",
    "close",
    "debug",
    "logger",
    "unexpected",
    "written",
]
