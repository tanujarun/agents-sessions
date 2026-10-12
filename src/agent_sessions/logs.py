"""The log: ``<state dir>/agent-sessions.log`` (docs/SPEC.md "Logs").

A rotating file (1 MB, three older files kept), UTF-8, created 0600 in a
state directory created 0700. What goes into it is decided where it happens
(``api`` for scans, imports and launches, so the CLI and the TUI log alike);
this module is the plumbing:

- nothing is set up, and no file or directory made, until the first message
  is written (importing the package creates nothing);
- a log that cannot be written never breaks a command and never prints: with
  a read-only or missing state directory the messages are dropped;
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


class PrivateRotatingFile(logging.handlers.RotatingFileHandler):
    """The log file: opened only when there is something to write, created
    0600 in a directory created 0700, and silent when that cannot be done
    (the next message tries again)."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True
        )

    def _open(self) -> io.TextIOWrapper:
        fsutil.make_private_dir(os.path.dirname(self.baseFilename))
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(self.baseFilename, flags, fsutil.PRIVATE_FILE)
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)

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
