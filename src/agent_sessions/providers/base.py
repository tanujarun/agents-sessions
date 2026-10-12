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
import stat
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar, NamedTuple

from .. import importer, textsafe
from ..fsutil import NotRegularFile, open_ro
from ..model import UUID_PATTERN, Candidate, Session, Store

if TYPE_CHECKING:
    from ..fsutil import StrPath
    from ..pathmap import PathMap

__all__ = [
    "SQLITE_ERRORS",
    "Candidate",
    "NotSupported",
    "Provider",
    "Session",
    "Store",
    "Warnings",
    "clean_title",
    "collect_warnings",
    "iter_jsonl",
    "open_ro",
    "open_sqlite_ro",
    "parse_ts",
    "read_lines",
    "read_small",
    "unreadable",
    "warn",
]

TITLE_MAX = 80
TITLE_SOURCE_MAX = 64 * 1024  # characters of a text that a title is made from


class NotSupported(Exception):
    """Raised when a provider cannot do an operation; carries the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Turn(NamedTuple):
    role: str  # "user" | "assistant" | "tool"
    text: str
    ts: str | None  # ISO 8601 UTC, as parse_ts returns it


class Provider:
    name: str = ""  # "claude"
    label: str = ""  # "Claude"
    binary: str = ""  # executable to resume with
    app_id: str = ""  # omarchy-launch-tui --app-id value
    resumable: bool = True  # False when no usable binary exists
    # True when the tool finds a session only from its project directory
    resume_needs_cwd: bool = False
    dir_names: ClassVar[set[str]] = set()  # directory names that may be a store root
    # The form of the tool's session ids, matched whole. The index lists no
    # session whose id fails it, ``launch`` resumes none and ``importer``
    # copies none, so an adapter cannot let another kind of id through.
    id_pattern: ClassVar[re.Pattern[str]] = UUID_PATTERN

    def valid_id(self, session_id: object) -> bool:
        """True when ``session_id`` is text in the form ``id_pattern``."""
        return isinstance(session_id, str) and self.id_pattern.fullmatch(session_id) is not None

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

    def import_plan(self, s: Session, pathmap: PathMap) -> importer.ImportPlan:
        """What importing foreign session ``s`` into the native store would
        copy, rewrite and append, as an ``importer.ImportPlan``. Reads the
        source only, writes nothing. Raises ``NotSupported(reason)`` when the
        format cannot be imported safely, ``ImportFailed`` when this session
        cannot be (its project folder is not on any mounted drive)."""
        raise NotSupported(f"{self.label or self.name} sessions cannot be imported")

    def import_session(
        self, s: Session, pathmap: PathMap, progress: importer.Progress | None = None
    ) -> Session:
        """Copy foreign session ``s`` into the native store and return the
        native copy as ``parse`` reads it (the index fills the other fields).

        ``progress(done_bytes, total_bytes)`` is called while copying. May
        raise ``NotSupported(reason)`` or ``ImportFailed`` (``importer.Exists``
        when the id is already in the native store). The copy is read back
        (``read_back``) while the import can still be undone: when it is not
        the session that was planned, nothing of it stays in the native
        store.
        """
        plan = self.import_plan(s, pathmap)
        verified: list[Session] = []
        importer.execute(plan, progress, lambda: verified.append(self.read_back(plan)))
        try:
            return self.read_back(plan)  # again: an appended line can change what is read
        except importer.ImportFailed:
            return verified[0]

    def read_back(self, plan: importer.ImportPlan) -> Session:
        """The session an import has put into the native store, read the way
        ``parse`` reads any other.

        Every candidate of the native store that is made of files the plan
        put in place is parsed. The result must have the plan's session id
        (the id that is parsed, not the candidate's name for it) and must be
        made of everything that was put in place. Raises ``ImportFailed``
        otherwise.
        """
        store = Store(self.name, plan.native_root, "native")
        placed = plan.destinations
        for candidate in sorted(self.candidates(store), key=lambda c: c.id_hint):
            if not any(
                path == dst or path.startswith(dst + os.sep)
                for path in candidate.paths
                for dst in placed
            ):
                continue
            copy = self.parse(store, candidate)
            if copy is not None and copy.id == plan.session_id and set(placed) <= set(copy.files):
                return copy
        raise importer.ImportFailed(
            f"the copy does not read as session {plan.session_id}; it was removed again and "
            "nothing was left in the native store"
        )


# --- warnings ---------------------------------------------------------------


class Warnings:
    """Messages recorded while one candidate (or one scan step) is read."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, message: str, path: StrPath | None = None) -> None:
        self.items.append(f"{path}: {message}" if path is not None else str(message))


_active = threading.local()


def warn(message: str, path: StrPath | None = None) -> None:
    """Record a warning for the scan in progress; ``scan`` reports it.

    The index caches it with the candidate it was raised for. Outside a
    ``collect_warnings`` block the call does nothing.
    """
    stack = getattr(_active, "stack", None)
    if stack:
        stack[-1].add(message, path)


def unreadable(path: StrPath, exc: BaseException | None = None) -> None:
    """Report a directory ``candidates`` could not list. One that is not
    there (any more) is not reported: nothing in it can be missed."""
    if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        warn("unreadable; its sessions are not listed", path)


@contextlib.contextmanager
def collect_warnings() -> Iterator[Warnings]:
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
# The longest line of a JSONL file that is read, in bytes with its line
# break. The longest in any real store is 2.74 MB (docs/FORMATS.md); a file
# on another drive can hold one of any length, and a line is parsed whole.
JSONL_LINE_MAX = 32 * 1024 * 1024
_PIECE = 1024 * 1024  # how much of a line is read at a time


def read_lines(fh: BinaryIO, limit: int | None = None) -> Iterator[bytes | None]:
    """The lines of a binary file, each with its line break. A line of more
    than ``limit`` bytes (default ``JSONL_LINE_MAX``) comes out as None: it
    is read and dropped a piece at a time, so no more than ``limit`` bytes
    plus one piece are ever held, however long the line is."""
    limit = JSONL_LINE_MAX if limit is None else limit
    pieces: list[bytes] = []
    size = 0
    while True:
        piece = fh.readline(_PIECE)
        if piece:
            size += len(piece)
            if size > limit:
                pieces.clear()
            else:
                pieces.append(piece)
            if not piece.endswith(b"\n"):
                continue  # the line goes on; at the end of the file the next read is empty
        if size:
            yield None if size > limit else b"".join(pieces)
        if not piece:
            return
        pieces.clear()
        size = 0


def read_small(path: StrPath, limit: int) -> bytes | None:
    """The bytes of a file that is expected to be small, or None when it
    cannot be read. Something that is not a regular file, and a file of more
    than ``limit`` bytes, are not read and get a warning."""
    try:
        with open_ro(path) as fh:
            data = fh.read(limit + 1)
    except NotRegularFile:
        warn("not a regular file; not read", path)
        return None
    except OSError:
        return None
    if len(data) > limit:
        warn(f"larger than {importer.human_size(limit)}; not read", path)
        return None
    return data


def iter_jsonl(path: StrPath) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects of a JSONL file, one line at a time.

    Blank lines, lines that do not parse (a half-written last line of a live
    session, a line nested too deeply) and lines that are not objects are
    skipped silently. A line longer than ``JSONL_LINE_MAX`` is skipped
    without being held in memory, with a warning. A file that cannot be read
    (one that is not a regular file included) yields what was read so far
    and records a warning.
    """
    try:
        fh = open_ro(path)
    except OSError as exc:
        warn(f"cannot read: {exc.strerror or exc}", path)
        return
    with fh:
        first = True
        lines = read_lines(fh)
        while True:
            try:
                raw = next(lines, b"")
            except OSError as exc:
                warn(f"read error: {exc.strerror or exc}", path)
                return
            if raw is None:
                first = False
                size = importer.human_size(JSONL_LINE_MAX)
                warn(f"a line longer than {size} was skipped", path)
                continue
            if not raw:
                return
            if first:
                first = False
                if raw.startswith(_BOM):
                    raw = raw[len(_BOM) :]
            line = raw.rstrip(b"\n").rstrip(b"\r")
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield record


SQLITE_MEMORY_MAX = 64 * 1024 * 1024  # larger files are opened in place
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_HEADER = 100


# What reading a damaged database can raise: sqlite3's own errors, and
# UnicodeDecodeError when sqlite3 cannot build the message of one (it names
# something in the schema, and the name is not UTF-8).
SQLITE_ERRORS = (sqlite3.Error, UnicodeDecodeError)


def _harden(con: sqlite3.Connection) -> None:
    """Settings for a connection that is about to read a database file from
    anywhere, made before any statement looks at that file's schema or pages.

    ``query_only`` first: nothing can be written through the connection.
    ``trusted_schema`` off: views, triggers and expressions stored in the
    file cannot call functions or virtual tables that are not safe for
    untrusted input. ``cell_size_check`` on: cell sizes on a b-tree page are
    checked when it is loaded, not assumed. Defensive mode (where this
    Python can set it, 3.12 and later): no statement can change the schema
    table or the pages directly.
    """
    con.execute("pragma query_only = 1")
    con.execute("pragma trusted_schema = off")
    con.execute("pragma cell_size_check = on")
    defensive = getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", None)
    setconfig = getattr(con, "setconfig", None)
    if defensive is not None and setconfig is not None:
        setconfig(defensive, True)


def _plain_schema(con: sqlite3.Connection) -> None:
    """Raise ``sqlite3.DatabaseError`` unless the database holds ordinary
    tables and their indexes, nothing else.

    A session database of the tools read here holds no view, no trigger and
    no virtual table. In a file from another drive, a view under the name of
    a table would run whatever query it was given the moment that "table" is
    read. What a schema row is called in its ``type`` column proves nothing
    (SQLite goes by the SQL text beside it), so the schema as SQLite parsed
    it is asked too (``pragma_table_list``, SQLite 3.37 and later).
    """
    try:
        odd = con.execute(
            "select count(*) from sqlite_master where type not in ('table', 'index')"
        ).fetchone()[0]
        try:
            odd += con.execute(
                "select count(*) from pragma_table_list where schema = 'main' and type <> 'table'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass  # an older SQLite: the type column is all there is to ask
    except UnicodeDecodeError as exc:
        raise sqlite3.DatabaseError("malformed database schema (a name is not UTF-8)") from exc
    if odd:
        raise sqlite3.DatabaseError(
            "the schema holds a view, a trigger or a virtual table; not read"
        )


def _sqlite_in_memory(path: StrPath) -> sqlite3.Connection | None:
    """A read-only in-memory copy of a SQLite file, or None when the file is
    missing, too large, not a database, or this Python cannot load one.

    The bytes are read through ``open_ro``, so SQLite itself never opens the
    file and its access time stays where it was. Header bytes 18 and 19 (file
    format write and read version) say 2 for a WAL database; the copy gets 1,
    because a WAL database in memory would look for a journal it cannot have.
    """
    if not hasattr(sqlite3.Connection, "deserialize"):
        return None
    try:
        if os.stat(path).st_size > SQLITE_MEMORY_MAX:
            return None
        with open_ro(path) as fh:
            data = fh.read(SQLITE_MEMORY_MAX + 1)
    except OSError:
        return None
    if (
        len(data) > SQLITE_MEMORY_MAX
        or len(data) < _SQLITE_HEADER
        or not data.startswith(_SQLITE_MAGIC)
    ):
        return None
    if data[18] == 2 or data[19] == 2:
        patched = bytearray(data)
        for offset in (18, 19):
            if patched[offset] == 2:
                patched[offset] = 1
        data = bytes(patched)
    con = None
    try:
        con = sqlite3.connect(":memory:")
        _harden(con)  # before the file's bytes are loaded: no statement runs on them unprotected
        con.deserialize(data)
        con.execute("select count(*) from sqlite_master").fetchone()
        return con
    except (sqlite3.Error, OverflowError, ValueError):
        if con is not None:
            con.close()
        return None


def open_sqlite_ro(path: StrPath, plain_schema: bool = True) -> sqlite3.Connection:
    """Open a SQLite file for reading without leaving a trace.

    Files up to 64 MB are read through ``open_ro`` and loaded into an
    in-memory connection (see ``_sqlite_in_memory``). Anything else, and any
    file that does not load that way, is opened in place with
    ``mode=ro&immutable=1``: plain ``mode=ro`` creates or rewrites
    ``-shm``/``-wal`` next to a WAL database, ``immutable=1`` does not.

    Either way the connection refuses writes, and rows still in a hot
    ``-wal`` are not visible. The file is not trusted: the connection is
    hardened before any statement reads it (``_harden``), and a database
    whose schema holds anything but ordinary tables and indexes is refused
    (``_plain_schema``; ``plain_schema=False`` leaves that check out, for a
    database that is known to hold more).

    Raises ``sqlite3.Error`` when the file cannot be opened or its schema
    cannot be read, and at once for something that is not a regular file:
    SQLite itself would open a named pipe and wait, or read a device. A
    damaged database can also raise ``UnicodeDecodeError`` from a later
    query; catch ``SQLITE_ERRORS``.
    """
    try:
        regular = stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        regular = True  # missing or unreachable: reported by the first query, as before
    if not regular:
        raise sqlite3.OperationalError("not a regular file")
    con = _sqlite_in_memory(path)
    if con is None:
        uri = "file:" + urllib.parse.quote(os.fspath(path)) + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
    try:
        _harden(con)
        if plain_schema:
            _plain_schema(con)
    except BaseException:
        con.close()
        raise
    return con


# --- values -----------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
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


def _from_number(value: float) -> str | None:
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


def parse_ts(value: object) -> str | None:
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
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or isinstance(nanos, bool)
            or not isinstance(nanos, int)
        ):
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
        moment = datetime(year, month, day, hour, minute, second, micros, tzinfo=UTC)
        if m.group(9):
            offset = timedelta(hours=int(m.group(10)), minutes=int(m.group(11)))
            moment = moment - offset if m.group(9) == "+" else moment + offset
    except (OverflowError, ValueError):
        return None
    return _iso(moment)


def clean_title(text: object) -> str:
    """One line, whitespace collapsed, control characters and the characters
    that reorder text taken out (``textsafe.clean``), at most 80 characters.
    None gives an empty string."""
    if text is None:
        return ""
    # A title is built from the start of its source only: the text of a
    # prompt can be as long as a line of a transcript.
    collapsed = " ".join(textsafe.clean(str(text)[:TITLE_SOURCE_MAX]).split())
    return collapsed[:TITLE_MAX].rstrip()
