"""The import journal: ``<state dir>/imports.jsonl``.

One JSON object per line, oldest first. A line is only ever added, in a
single write on a descriptor opened for appending, flushed to disk, with the
state directory's lock held (``fsutil.lock``): two processes that import at
the same moment both leave their record, and a crash can at worst cut the
last line short. Nothing in the file is rewritten.

A record says what it is in ``record`` (``"import"``, or ``"undo"`` for an
import that was taken back, ``unimport``) and which form it has in
``version``. Before version 2 the records were a JSON array in
``imports.json``, rewritten on every import; such a file is still read, its
records first, and is never written or removed.

Standard library only, and nothing here knows a provider.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from . import fsutil

NAME = "imports.jsonl"
LEGACY_NAME = "imports.json"
VERSION = 2  # of a record written now
LEGACY_VERSION = 1  # what a record of ``imports.json`` is given
IMPORT = "import"  # ``record`` of an import
UNDO = "undo"  # ``record`` of an import that was taken back

_log = logging.getLogger(__name__)


def path() -> Path:
    return fsutil.state_dir() / NAME


def legacy_path() -> Path:
    return fsutil.state_dir() / LEGACY_NAME


def _legacy(quiet: bool = False) -> list[dict]:
    """The records of an ``imports.json`` from before the journal, each
    marked as an import of version 1. A file that cannot be used gives none
    and a warning (none with ``quiet``): it is not ours to repair, and
    nothing is lost by leaving it alone."""
    where = legacy_path()
    try:
        with fsutil.open_ro(where) as fh:  # a regular file, and no waiting
            data = json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError) as exc:
        if not quiet:
            _log.warning("the old import record %s cannot be read (%s); left as it is", where, exc)
        return []
    items = data.get("imports") if isinstance(data, dict) else None
    if not isinstance(items, list):
        if not quiet:
            _log.warning(
                "the old import record %s is not in the expected form; left as it is", where
            )
        return []
    return [
        {"record": IMPORT, "version": LEGACY_VERSION, **item}
        for item in items
        if isinstance(item, dict)
    ]


def read(quiet: bool = False) -> list[dict]:
    """Every record, oldest first: those of an old ``imports.json``, then
    the lines of the journal. Reads only.

    A line that is not a JSON object is skipped with a warning in the log.
    That is expected of the last line alone, when the write that made it was
    cut short. Raises ``OSError`` when the journal is there and cannot be
    read at all; that includes a journal that is not a regular file
    (``fsutil.NotRegularFile``): a named pipe under its name is not opened,
    so no command waits on it.

    ``quiet`` leaves the warnings out: for a caller that reads the journal
    with every listing (``Index.finalise``), where the same line would be
    reported each time.
    """
    records = _legacy(quiet)
    where = path()
    try:
        with fsutil.open_ro(where) as fh:
            data = fh.read()
    except FileNotFoundError:
        return records
    lines = data.split(b"\n")
    complete = len(lines) - 1  # what follows the last line break is not a whole line
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            record = None
        if isinstance(record, dict):
            records.append(record)
        elif quiet:
            continue
        elif number > complete:
            _log.warning("%s: the last line is incomplete (a write was cut short); skipped", where)
        else:
            _log.warning("%s: line %d is not a record; skipped", where, number)
    return records


def _text(record: dict, key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def ident(record: dict) -> tuple[str, str] | None:
    """``(provider, session id)`` of a record, or None when it has not both."""
    provider, session_id = _text(record, "provider"), _text(record, "id")
    return None if provider is None or session_id is None else (provider, session_id)


def not_undone(records: Iterable[dict]) -> list[dict]:
    """The import records no later undo took back, oldest first.

    An undo always takes back the last import of its provider and session id
    that is still standing, so the records are settled in order: an ``undo``
    cancels the nearest import before it of the same provider and id."""
    standing: list[dict] = []
    for record in records:
        kind = record.get("record")
        whose = ident(record)
        if whose is None:
            continue
        if kind == IMPORT:
            standing.append(record)
        elif kind == UNDO:
            for position in range(len(standing) - 1, -1, -1):
                if ident(standing[position]) == whose:
                    del standing[position]
                    break
    return standing


def find_record(records: Iterable[dict], provider: str, session_id: str) -> dict | None:
    """The record that stands for this session: its last import that was not
    undone; None when there is none. It is what an undo takes back
    (``unimport``) and what says which folder the copy was made for
    (``Index.finalise``)."""
    found = None
    for record in not_undone(records):
        if ident(record) == (provider, session_id):
            found = record
    return found


def _line(record: dict) -> bytes:
    """One record as one line. Text is written as it is; a path with bytes
    that are not UTF-8 makes the whole line ASCII with escapes instead."""
    for ascii_only in (False, True):
        text = json.dumps(record, ensure_ascii=ascii_only, separators=(",", ":"))
        try:
            return (text + "\n").encode("utf-8")
        except UnicodeEncodeError:
            continue
    raise ValueError("the record cannot be written as JSON text")  # not reached


def open_for_append() -> tuple[int, bool]:
    """``(descriptor, created)`` of the journal, opened for appending; the
    state directory must exist. For a caller that has to know the journal
    can be written before it does something that cannot be undone
    (``write`` follows, under the same lock). Raises ``OSError``; a journal
    that is not a regular file is one (``fsutil.open_regular``: a record
    written into a named pipe would be lost)."""
    where = os.fspath(path())
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_BINARY", 0)
    try:
        return fsutil.open_regular(where, flags), False
    except FileNotFoundError:
        pass
    try:
        return fsutil.open_regular(where, flags | os.O_CREAT | os.O_EXCL), True
    except FileExistsError:  # made between the two opens
        return fsutil.open_regular(where, flags), False


def write(fd: int, record: dict) -> None:
    """Add one record through a descriptor of ``open_for_append``: a single
    write, then a flush to disk. The caller holds the lock.

    After a line that a crash cut short the record starts on a line of its
    own. A write that fails, is interrupted, or that the disk takes only in
    part, is taken back (the file is cut to the length it had); the failure
    is an ``OSError``.
    """
    data = _line(record)
    size = os.fstat(fd).st_size
    if size:
        os.lseek(fd, size - 1, os.SEEK_SET)
        if os.read(fd, 1) != b"\n":
            data = b"\n" + data
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(errno.ENOSPC, "the record was written only in part", os.fspath(path()))
        os.fsync(fd)
    except BaseException:  # an interrupt too: no half-written line stays
        try:
            os.ftruncate(fd, size)
        except OSError:
            pass
        raise


def append(record: dict) -> None:
    """Add one record to the journal: lock, one write, flush. The state
    directory and the journal are created private (0700, 0600) when they are
    missing. Raises ``OSError`` (``fsutil.LockTimeout`` when another process
    keeps the lock)."""
    fsutil.make_private_dir(fsutil.state_dir())
    with fsutil.lock():
        fd, created = open_for_append()
        try:
            write(fd, record)
        finally:
            os.close(fd)
        if created:
            fsutil.fsync_dir(fsutil.state_dir())


__all__ = [
    "IMPORT",
    "LEGACY_NAME",
    "LEGACY_VERSION",
    "NAME",
    "UNDO",
    "VERSION",
    "append",
    "find_record",
    "ident",
    "legacy_path",
    "not_undone",
    "open_for_append",
    "path",
    "read",
    "write",
]
