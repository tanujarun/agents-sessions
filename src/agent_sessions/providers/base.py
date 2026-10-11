"""Provider adapter base class, NotSupported, and the helpers adapters share.

The helpers implement docs/FORMATS.md "Rules common to all providers" once:
JSONL reading, read-only SQLite, timestamp and title normalisation, and the
warnings an adapter records instead of raising.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, NamedTuple

from ..fsutil import open_ro
from ..model import Candidate, Session, Store

__all__ = [
    "Candidate", "NotSupported", "Provider", "Session", "Store", "Warnings",
    "clean_title", "collect_warnings", "iter_jsonl", "open_ro",
    "open_sqlite_ro", "parse_ts", "warn",
]

TITLE_MAX = 80


class NotSupported(Exception):
    """Raised when a provider cannot do an operation; carries the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Turn(NamedTuple):
    role: str          # "user" | "assistant" | "tool"
    text: str
    ts: str | None     # ISO 8601 UTC, as parse_ts returns it


class Provider:
    name: str = ""            # "claude"
    label: str = ""           # "Claude"
    binary: str = ""          # executable to resume with
    app_id: str = ""          # omarchy-launch-tui --app-id value
    resumable: bool = True    # False when no usable binary exists
    dir_names: set[str] = set()  # directory names that may be a store root

    def native_store(self) -> Path | None:
        raise NotImplementedError

    def is_store(self, path: Path) -> bool:
        """Cheap signature check."""
        raise NotImplementedError

    def candidates(self, store: Store) -> list[Candidate]:
        """Everything that may be a session: names and stats only, no reads."""
        raise NotImplementedError

    def parse(self, store: Store, c: Candidate) -> Session | None:
        """Read one candidate; None when it is not a listed session.

        Build the result with ``Session.for_store``. Never raise on a
        malformed file: call ``warn`` and return None.
        """
        raise NotImplementedError

    def list_sessions(self, store: Store) -> list[Session]:
        """Uncached convenience: parse every candidate, in id order."""
        found = []
        for c in sorted(self.candidates(store), key=lambda c: c.id_hint):
            session = self.parse(store, c)
            if session is not None:
                found.append(session)
        return found

    def resume_argv(self, s: Session) -> list[str]:
        """Resume command for a native session."""
        raise NotImplementedError

    def transcript(self, s: Session) -> Iterator[Turn]:
        """The conversation as plain turns, oldest first, read from ``s.files``.

        User prompts and assistant text only; a tool call becomes one
        ``Turn("tool", "<tool name>", ts)``; tool output, reasoning and
        injected context are left out. Works for foreign sessions too.
        """
        raise NotImplementedError

    def import_session(self, s: Session, pathmap) -> Session:
        """May raise NotSupported(reason)."""
        raise NotImplementedError


# --- warnings ---------------------------------------------------------------

class Warnings:
    """Messages recorded while one candidate (or one scan step) is read."""

    def __init__(self):
        self.items: list[str] = []

    def add(self, message: str, path=None) -> None:
        self.items.append(f"{path}: {message}" if path is not None else str(message))


_active = threading.local()


def warn(message: str, path=None) -> None:
    """Record a warning for the scan in progress; ``scan`` reports it.

    The index caches it with the candidate it was raised for. Outside a
    ``collect_warnings`` block the call does nothing.
    """
    stack = getattr(_active, "stack", None)
    if stack:
        stack[-1].add(message, path)


@contextlib.contextmanager
def collect_warnings():
    """Collect the ``warn`` calls made on this thread inside the block."""
    collector = Warnings()
    stack = getattr(_active, "stack", None)
    if stack is None:
        stack = _active.stack = []
    stack.append(collector)
    try:
        yield collector
    finally:
        stack.pop()


# --- files ------------------------------------------------------------------

_BOM = b"\xef\xbb\xbf"


def iter_jsonl(path):
    """Yield the JSON objects of a JSONL file, one line at a time.

    Blank lines, lines that do not parse (a half-written last line of a live
    session) and lines that are not objects are skipped silently. A file that
    cannot be read yields what was read so far and records a warning.
    """
    try:
        fh = open_ro(path)
    except OSError as exc:
        warn(f"cannot read: {exc.strerror or exc}", path)
        return
    with fh:
        first = True
        while True:
            try:
                raw = fh.readline()
            except OSError as exc:
                warn(f"read error: {exc.strerror or exc}", path)
                return
            if not raw:
                return
            if first:
                first = False
                if raw.startswith(_BOM):
                    raw = raw[len(_BOM):]
            line = raw.rstrip(b"\n").rstrip(b"\r")
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield record


def open_sqlite_ro(path) -> sqlite3.Connection:
    """Open a SQLite file read-only and immutable.

    Plain ``mode=ro`` creates or rewrites ``-shm``/``-wal`` next to a WAL
    database; ``immutable=1`` does not. Rows still in a hot ``-wal`` are not
    visible. Raises ``sqlite3.Error`` when the file cannot be opened.
    """
    uri = "file:" + urllib.parse.quote(os.fspath(path)) + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


# --- values -----------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TS = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?"
    r"\s*(?:([Zz])|([+-])(\d{2}):?(\d{2}))?\s*$"
)


def _iso(moment: datetime) -> str:
    # isoformat pads the year; strftime("%Y") does not on every platform.
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _from_epoch(seconds: int, micros: int = 0) -> str | None:
    try:
        moment = _EPOCH + timedelta(seconds=seconds, microseconds=micros)
    except (OverflowError, ValueError):
        return None
    return _iso(moment)


def _from_number(value) -> str | None:
    if value != value or value in (float("inf"), float("-inf")):
        return None
    # Magnitude tells the unit: 1e11 s is the year 5138, 1e11 ms is 1973.
    size = abs(value)
    if size < 1e11:
        scale = 1_000_000
    elif size < 1e14:
        scale = 1_000
    elif size < 1e17:
        scale = 1
    else:
        return _from_epoch(0, int(value) // 1000)
    return _from_epoch(0, int(value * scale))


def parse_ts(value) -> str | None:
    """Normalise a recorded timestamp to ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC).

    Accepts ISO text with ``Z`` or an offset and any number of fraction
    digits (cut to six), SQLite text with a space, text without a zone (taken
    as UTC), epoch seconds or milliseconds as int, float or digit string, and
    a protobuf timestamp as ``{1: seconds, 2: nanos}`` or ``(seconds,
    nanos)``. Anything else gives None. The fixed width makes the strings
    sort in time order.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _from_number(value)
    if isinstance(value, dict):
        value = (value.get(1, value.get("1")), value.get(2, value.get("2", 0)))
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            return None
        seconds, nanos = value[0], value[1] or 0
        if (isinstance(seconds, bool) or not isinstance(seconds, int)
                or isinstance(nanos, bool) or not isinstance(nanos, int)):
            return None
        return _from_epoch(seconds, nanos // 1000)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return _from_number(float(text) if "." in text else int(text))
    m = _TS.match(text)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    micros = int((m.group(7) or "0")[:6].ljust(6, "0"))
    try:
        moment = datetime(year, month, day, hour, minute, second, micros,
                          tzinfo=timezone.utc)
        if m.group(9):
            offset = timedelta(hours=int(m.group(10)), minutes=int(m.group(11)))
            moment = moment - offset if m.group(9) == "+" else moment + offset
    except (OverflowError, ValueError):
        return None
    return _iso(moment)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_title(text) -> str:
    """One line, whitespace collapsed, control characters removed, at most
    80 characters. None gives an empty string."""
    if text is None:
        return ""
    collapsed = " ".join(_CONTROL.sub(" ", str(text)).split())
    return collapsed[:TITLE_MAX].rstrip()
