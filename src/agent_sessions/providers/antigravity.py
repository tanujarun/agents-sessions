"""Antigravity adapter. Formats: docs/FORMATS.md "Antigravity"."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import stat
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from .. import importer
from ..fsutil import home, listdir_ro
from ..model import Candidate, Session, Store
from . import register
from .base import (
    SQLITE_ERRORS,
    ExperimentalOnly,
    NotSupported,
    Provider,
    Turn,
    between,
    clean_title,
    iter_jsonl,
    open_sqlite_ro,
    parse_ts,
    read_small,
    unreadable,
    warn,
)

if TYPE_CHECKING:
    from ..fsutil import StrPath
    from ..pathmap import PathMap

IDE_NOTE = "Antigravity IDE conversation: list only"
IDE_STORE = "antigravity-ide"
# An import is a plain copy, and experimental (FORMATS section 6 and "Phase 2
# findings", Q5). What a front end shows before one:
IMPORT_WARNING = (
    "Experimental: no imported Antigravity conversation has been resumed by hand yet, "
    "and agy starts a new conversation without an error when it cannot read a copy. "
    "After the first resume, check that the old turns are on the screen."
)
# And what is answered when one was not asked for.
EXPERIMENTAL_ONLY = (
    "importing an Antigravity conversation is experimental and is done only when asked "
    "for: no imported copy has been resumed by hand yet, and agy starts a new conversation "
    "without an error when it cannot read one"
)

_SUMMARIES = "conversation_summaries.db"
_SIDECARS = ("-wal", "-shm", "-journal")
_SMALL_FILE = 16 * 1024 * 1024
_STEP_USER, _STEP_PLANNER = 14, 15
# ``title`` as the first thing on a line. The blanks before it are matched
# without line breaks: ``^\s*`` finds the same titles, but on a file of blank
# lines it reads the rest of the file again from every line.
_PBTXT_TITLE = re.compile(rb'^[ \t\r\f\v]*title\s*:\s*"((?:[^"\\]|\\.)*)"', re.M | re.S)
_PBTXT_ESCAPE = re.compile(rb"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", re.S)
_PBTXT_SIMPLE = {
    b"n": b"\n",
    b"t": b"\t",
    b"r": b"\r",
    b"a": b"\a",
    b"b": b"\b",
    b"f": b"\f",
    b"v": b"\v",
}


# --- protobuf without a schema ----------------------------------------------


def _varint(data: bytes, pos: int) -> tuple[int | None, int]:
    """``(value, next position)``; value is None when the bytes run out or
    the varint is longer than ten bytes."""
    value = shift = 0
    while pos < len(data) and shift < 70:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value & 0xFFFFFFFFFFFFFFFF, pos
        shift += 7
    return None, pos


def pb_fields(data: object) -> list[tuple[int, int, object]]:
    """Decode one protobuf message into ``(field number, wire type, value)``
    triples, in order.

    A varint (wire type 0) gives an unsigned int; fixed64 (1), a
    length-delimited value (2) and fixed32 (5) give bytes. Decoding stops at
    the first thing that cannot be read (a cut-off value, a group, field
    number 0) and returns the fields before it; anything that is not bytes
    gives an empty list.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return []
    data = bytes(data)
    out: list[tuple[int, int, object]] = []
    value: object
    pos, size = 0, len(data)
    while pos < size:
        key, pos = _varint(data, pos)
        if key is None:
            break
        number, wire = key >> 3, key & 7
        if number == 0:
            break
        if wire == 0:
            value, pos = _varint(data, pos)
            if value is None:
                break
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > size:
                break
            value = data[pos : pos + width]
            pos += width
        elif wire == 2:
            length, pos = _varint(data, pos)
            if length is None or pos + length > size:
                break
            value = data[pos : pos + length]
            pos += length
        else:
            break
        out.append((number, wire, value))
    return out


def pb_all(data: object, *path: int) -> list[object]:
    """Every value at a nested field path, e.g. ``pb_all(blob, 20, 7, 2)``;
    repeated fields are followed at each level."""
    values = [data]
    for number in path:
        values = [
            value
            for message in values
            for found, _wire, value in pb_fields(message)
            if found == number
        ]
    return values


def pb_first(data: object, *path: int) -> object:
    """The first value at a nested field path, or None."""
    values = pb_all(data, *path)
    return values[0] if values else None


def pb_text(value: object) -> str | None:
    """A length-delimited value as text; None for anything else."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else None


def pb_timestamp(value: object) -> tuple[int, int] | None:
    """A ``{1: seconds, 2: nanos}`` message as ``(seconds, nanos)``; None
    when it is missing, cut off or not after the epoch (Go's zero time)."""
    seconds: int | None = None
    nanos: int | None = None
    for number, wire, found in pb_fields(value):
        if not isinstance(found, int):
            continue  # only wire type 0 gives an int
        if wire == 0 and number == 1 and seconds is None:
            seconds = found - (1 << 64) if found >= 1 << 63 else found
        elif wire == 0 and number == 2 and nanos is None:
            nanos = found
    if seconds is None or seconds <= 0:
        return None
    return seconds, nanos if nanos is not None and nanos < 1_000_000_000 else 0


# --- small values -----------------------------------------------------------


def uri_to_path(uri: object) -> str | None:
    """A ``file://`` URI as the path it names; a Windows drive keeps its
    form (``file:///F:/My%20Site`` gives ``F:/My Site``). Text without a
    scheme is taken as a path already; any other scheme gives None."""
    if not isinstance(uri, str) or not uri.strip():
        return None
    uri = uri.strip()
    if "://" not in uri:
        return uri
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme.lower() != "file":
        return None
    path = urllib.parse.unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = "//" + urllib.parse.unquote(parsed.netloc) + path
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path or None


def _pbtxt_title(path: StrPath) -> str:
    """The ``title:"…"`` value of an annotations text proto, or ''."""
    raw = read_small(path, _SMALL_FILE)
    found = _PBTXT_TITLE.search(raw) if raw else None
    if not found:
        return ""

    def unescape(match: re.Match[bytes]) -> bytes:
        code = match.group(1)
        if len(code) > 1 and code[:1] == b"x":
            return bytes([int(code[1:], 16)])
        if code[:1] in b"01234567":
            return bytes([int(code, 8) & 0xFF])
        return _PBTXT_SIMPLE.get(code, code)

    return _PBTXT_ESCAPE.sub(unescape, found.group(1)).decode("utf-8", "replace")


def _user_request(content: object) -> str:
    """What the user typed in a USER_INPUT ``content``: the inside of
    ``<USER_REQUEST>``, which is followed by injected metadata blocks."""
    if not isinstance(content, str):
        return ""
    wrapped = between(content, "<USER_REQUEST>", "</USER_REQUEST>")
    return (content if wrapped is None else wrapped).strip()


def _mtime(path: StrPath) -> str | None:
    try:
        return parse_ts(os.stat(path).st_mtime, unit="s")
    except OSError:
        return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _connect(path: StrPath) -> sqlite3.Connection:
    con = open_sqlite_ro(path)
    con.text_factory = lambda raw: raw.decode("utf-8", "replace")
    return con


def _optional(con: sqlite3.Connection, sql: str) -> tuple[Any, ...] | None:
    """First row of a query on a table that may not exist, or None."""
    try:
        row: tuple[Any, ...] | None = con.execute(sql).fetchone()
        return row
    except SQLITE_ERRORS:
        return None


@register
class Antigravity(Provider):
    name = "antigravity"
    label = "Antigravity"
    binary = "agy"
    app_id = "org.omarchy.agy"
    resumable = True
    dir_names: ClassVar[set[str]] = {"antigravity-cli", "antigravity-ide"}

    def native_store(self) -> Path:
        return home() / ".gemini" / "antigravity-cli"

    def is_store(self, path: Path) -> bool:
        return (path / "conversations").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        """One candidate per entry of ``conversations/``. A ``<id>.db`` is a
        conversation; anything else (dot files and SQLite sidecars aside) is
        a format this reader does not know, kept as a candidate so that
        ``parse`` can say so. Directory listing only."""
        root = Path(store.root)
        out = []
        try:
            names = sorted(listdir_ro(root / "conversations"))
        except OSError as exc:
            unreadable(root / "conversations", exc)
            names = []
        for name in names:
            if name.startswith(".") or name.endswith(_SIDECARS):
                continue
            entry = str(root / "conversations" / name)
            if not name.endswith(".db"):
                out.append(Candidate("unknown:" + name, [entry]))
                continue
            cid = name[:-3]
            out.append(
                Candidate(
                    cid,
                    [
                        entry,
                        str(root / _SUMMARIES),
                        str(root / "annotations" / f"{cid}.pbtxt"),
                        str(root / "cache" / "last_conversations.json"),
                        str(self._log(root, cid)),
                    ],
                )
            )
        return out

    @staticmethod
    def _log(root: Path, cid: str) -> Path:
        return root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"

    def parse(self, store: Store, c: Candidate) -> Session | None:
        fields = self._fields(store, c)
        if fields is None:
            return None
        fields.pop("sources")
        return Session.for_store(store, **fields)

    def _fields(self, store: Store, c: Candidate) -> dict | None:
        """The conversation fields of a listed conversation plus ``sources``
        (where title and cwd came from), or None."""
        if not c.paths:
            return None
        db = Path(c.paths[0])
        if db.suffix != ".db":
            warn("unknown conversation file format; not read", db)
            return None
        root, cid = db.parent.parent, db.stem
        conv = self._conversation(db)
        if conv is None:
            return None
        row = self._summary_row(root / _SUMMARIES, cid)

        # Sub-agent conversations.
        if _text(row.get("parent_conversation_id")) or _count(row.get("nesting_depth")) > 0:
            return None
        if conv["parents"] > 0:
            return None
        # Empty conversation. The index's step_count stands in when the
        # steps are not in the main database file yet (immutable reads skip
        # a hot -wal).
        step_count = _count(row.get("step_count"))
        if conv["steps"] == 0 and step_count <= 0:
            return None
        messages = conv["messages"] if conv["steps"] > 0 else step_count

        log = self._log(root, cid)
        annotation = root / "annotations" / f"{cid}.pbtxt"
        title, title_source = clean_title(_text(row.get("title"))), "conversation_summaries.title"
        if not title:
            title, title_source = clean_title(_pbtxt_title(annotation)), "annotations"
        if not title:
            title, title_source = clean_title(_text(row.get("preview"))), "preview"
        if not title:
            title, title_source = clean_title(self._first_request(log)), "first user input"
        if not title:
            title, title_source = clean_title(cid[:8]), "id"

        cwd, cwd_source = self._workspace(row.get("workspace_uris")), "workspace_uris"
        if not cwd:
            cwd = uri_to_path(pb_text(pb_first(conv["blob"], 7)))
            cwd_source = "metadata blob field 7"
        if not cwd:
            cwd = self._last_cwd(root / "cache" / "last_conversations.json", cid)
            cwd_source = "last_conversations.json"
        if not cwd:
            cwd, cwd_source = None, "none"

        started = (
            parse_ts(pb_timestamp(pb_first(conv["blob"], 2)))
            or parse_ts(pb_timestamp(pb_first(conv["first"], 1)))
            or _mtime(db)
        )
        modified = _text(row.get("last_modified_time"))
        updated = (
            (parse_ts(modified) if not modified.startswith("0001-") else None)
            or parse_ts(pb_timestamp(pb_first(conv["last"], 1)))
            or _mtime(db)
        )

        files = [str(db)]
        files += [str(p) for p in (root / "brain" / cid, annotation) if os.path.exists(p)]
        note = IDE_NOTE if root.name == IDE_STORE else None
        return {
            "id": cid,
            "title": title,
            "cwd": cwd,
            "started": started,
            "updated": updated,
            "messages": messages,
            "files": files,
            "note": note,
            "sources": {"title": title_source, "cwd": cwd_source},
        }

    @staticmethod
    def _conversation(db: Path) -> dict | None:
        """What one conversation database holds; None (with a warning) when
        it cannot be read as one."""
        try:
            with contextlib.closing(_connect(db)) as con:
                steps = con.execute("SELECT count(*) FROM steps").fetchone()[0]
                messages = con.execute(
                    "SELECT count(*) FROM steps WHERE step_type IN (?, ?)",
                    (_STEP_USER, _STEP_PLANNER),
                ).fetchone()[0]
                first = con.execute(
                    "SELECT metadata FROM steps ORDER BY idx ASC LIMIT 1"
                ).fetchone()
                last = con.execute(
                    "SELECT metadata FROM steps ORDER BY idx DESC LIMIT 1"
                ).fetchone()
                blob = _optional(con, "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
                parents = _optional(con, "SELECT count(*) FROM parent_references")
        except SQLITE_ERRORS as exc:
            warn(f"conversation database not readable: {exc}", db)
            return None
        return {
            "steps": _count(steps),
            "messages": _count(messages),
            "first": first[0] if first else None,
            "last": last[0] if last else None,
            "blob": blob[0] if blob else None,
            "parents": _count(parents[0]) if parents else 0,
        }

    @staticmethod
    def _summary_row(path: Path, cid: str) -> dict:
        """This conversation's row of the summaries index, by column name;
        empty when the index or the row is missing or unreadable."""
        if not os.path.exists(path):
            return {}
        try:
            with contextlib.closing(_connect(path)) as con:
                cursor = con.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ?", (cid,)
                )
                row = cursor.fetchone()
                if row is None:
                    return {}
                return dict(zip((column[0] for column in cursor.description), row, strict=False))
        except SQLITE_ERRORS as exc:
            warn(f"conversation summaries not readable: {exc}", path)
            return {}

    @staticmethod
    def _workspace(value: object) -> str | None:
        """cwd from ``workspace_uris``: a JSON list of file URIs, or ''."""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            uris = json.loads(value)
        except (ValueError, RecursionError):
            return None
        return uri_to_path(uris[0]) if isinstance(uris, list) and uris else None

    @staticmethod
    def _last_cwd(path: Path, cid: str) -> str | None:
        """The key of ``last_conversations.json`` whose value is the id."""
        raw = read_small(path, _SMALL_FILE)
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8-sig", "replace"))
        except (ValueError, RecursionError):
            warn("not valid JSON", path)
            return None
        if not isinstance(data, dict):
            return None
        keys = sorted(key for key, value in data.items() if value == cid and key)
        return keys[0] if keys else None

    @staticmethod
    def _first_request(log: Path) -> str:
        if not os.path.exists(log):
            return ""
        for record in iter_jsonl(log):
            if record.get("type") == "USER_INPUT":
                text = _user_request(record.get("content"))
                if text:
                    return text
        return ""

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "--conversation", s.id]

    def new_session_argv(self, prompt: str) -> list[str]:
        # "Run an initial prompt interactively and continue the session";
        # never ``-p``, ``--print`` or ``--prompt``.
        return [self.binary, "--prompt-interactive", prompt]

    def transcript(self, s: Session) -> Iterator[Turn]:
        """From the plain-text log in ``brain/`` when there is one, else from
        the ``steps`` table of the conversation database."""
        db = next((Path(f) for f in s.files if f.endswith(".db")), None)
        if db is None:
            return
        log = self._log(db.parent.parent, db.stem)
        if os.path.exists(log):
            yield from self._log_turns(log)
        else:
            yield from self._step_turns(db)

    @staticmethod
    def _log_turns(log: Path) -> Iterator[Turn]:
        for record in iter_jsonl(log):
            kind = record.get("type")
            ts = parse_ts(record.get("created_at"))
            if kind == "USER_INPUT":
                text = _user_request(record.get("content"))
                if text:
                    yield Turn("user", text, ts)
            elif kind == "PLANNER_RESPONSE":
                text = _text(record.get("content")).strip()
                if text:
                    yield Turn("assistant", text, ts)
                calls = record.get("tool_calls")
                for call in calls if isinstance(calls, list) else []:
                    name = call.get("name") if isinstance(call, dict) else None
                    if isinstance(name, str) and name:
                        yield Turn("tool", name, ts)

    @staticmethod
    def _step_turns(db: Path) -> Iterator[Turn]:
        """``step_payload`` fields, matched against the brain log on every
        step of the conversations that have both: 19.2 = the user's text,
        20.1 = the planner's reply, 20.7.2 = the name of each tool call."""
        try:
            with contextlib.closing(_connect(db)) as con:
                rows = con.execute(
                    "SELECT step_type, metadata, step_payload FROM steps ORDER BY idx"
                ).fetchall()
        except SQLITE_ERRORS:
            return
        for step_type, metadata, payload in rows:
            ts = parse_ts(pb_timestamp(pb_first(metadata, 1)))
            if step_type == _STEP_USER:
                text = _user_request(pb_text(pb_first(payload, 19, 2)))
                if text:
                    yield Turn("user", text, ts)
            elif step_type == _STEP_PLANNER:
                text = (pb_text(pb_first(payload, 20, 1)) or "").strip()
                if text:
                    yield Turn("assistant", text, ts)
                for name in pb_all(payload, 20, 7, 2):
                    name = pb_text(name)
                    if name:
                        yield Turn("tool", name, ts)

    def import_plan(
        self,
        s: Session,
        pathmap: PathMap,
        target: str | None = None,
        experimental: bool = False,
    ) -> importer.ImportPlan:
        """Copy only, and only as an experimental import (FORMATS "Phase 2
        findings", Q5; the table there is what this follows).

        ``brain/<id>/`` and ``annotations/<id>.pbtxt`` first, when the store
        has them, then ``conversations/<id>.db``, byte for byte: the tool
        finds a conversation by that file, so it arrives last. Nothing is
        rewritten and no index is written (the tool rebuilds both of its
        summary indexes from the conversation files). A write-ahead log is
        never copied; a conversation that has one with anything in it is
        refused, and so are a sub-agent conversation and one with no steps.

        No folder is needed: the workspace is inside the database and stays
        as it was recorded. A ``target`` is checked and noted in the plan,
        and changes nothing in the copy."""
        if not experimental:
            raise ExperimentalOnly(EXPERIMENTAL_ONLY, IMPORT_WARNING)
        source = next((f for f in s.files if f.endswith(".db")), None)
        if source is None:
            raise NotSupported("this conversation has no database file to copy")
        # The file's name becomes a name in the native store: it must be
        # exactly ``<this conversation's id>.db``.
        name = os.path.basename(source)
        if name != f"{s.id}.db" or not self.valid_id(s.id):
            raise NotSupported(
                f"the conversation file is not named after its conversation id: {source}"
            )
        db = Path(source)
        root = db.parent.parent
        if root.name == IDE_STORE:
            raise NotSupported(
                "this is an Antigravity IDE conversation; its file format is not known, "
                "and agy reads only its own conversations"
            )
        self._refuse_hot_wal(source + "-wal")
        conv = self._conversation(db)
        if conv is None:
            raise NotSupported(f"the conversation database cannot be read: {source}")
        row = self._summary_row(root / _SUMMARIES, s.id)
        if (
            conv["parents"] > 0
            or _text(row.get("parent_conversation_id"))
            or _count(row.get("nesting_depth")) > 0
        ):
            raise NotSupported("this is a sub-agent conversation; it is not resumed on its own")
        if conv["steps"] == 0:
            raise NotSupported("this conversation has no steps in its database file")

        plan = importer.start_plan(
            s,
            self.native_store(),
            pathmap,
            self.id_pattern,
            target,
            needs_folder=False,
            read_back=self.read_back,
        )
        native = plan.native_root
        # Any of these means the id is, or was, in use in this store; the
        # lock file is left behind by a conversation the tool has opened.
        plan.existing = [
            path
            for path in (
                os.path.join(native, "annotations", f"{s.id}.pbtxt"),
                os.path.join(native, "brain", s.id),
                os.path.join(native, "conversations", name),
                os.path.join(native, "conversations", name + "-wal"),
                os.path.join(native, "conversations", f"{s.id}.pb"),
                os.path.join(native, "presence", f"{s.id}.lock"),
            )
            if os.path.lexists(path)
        ]
        brain = str(root / "brain" / s.id)
        if os.path.isdir(brain) and not os.path.islink(brain):
            dirs, files = importer.tree(brain)
            plan.units.append(
                importer.Unit(os.path.join(native, "brain", s.id), brain, files, True, dirs)
            )
        annotation = str(root / "annotations" / f"{s.id}.pbtxt")
        if os.path.isfile(annotation) and not os.path.islink(annotation):
            plan.units.append(
                importer.Unit(
                    os.path.join(native, "annotations", f"{s.id}.pbtxt"),
                    annotation,
                    [importer.file_copy(annotation)],
                )
            )
        plan.units.append(
            importer.Unit(
                os.path.join(native, "conversations", name), source, [importer.file_copy(source)]
            )
        )
        plan.info["experimental"] = True
        return plan

    def undo_extras(self, session_id: str, native_root: StrPath) -> list[str]:
        """What the tool adds once it has opened a conversation (FORMATS
        "Phase 2 findings", Q5, "undo"): the write-ahead log and its index
        beside the database, and the lock file, which stays after the tool
        has exited."""
        root = os.fspath(native_root)
        database = os.path.join(root, "conversations", f"{session_id}.db")
        return [
            database + "-wal",
            database + "-shm",
            os.path.join(root, "presence", f"{session_id}.lock"),
        ]

    @staticmethod
    def _refuse_hot_wal(wal: str) -> None:
        """Raise ``NotSupported`` for a conversation whose write-ahead log
        holds anything: its last changes are not in the database file, and a
        copy of that file alone would be an older conversation. An empty
        log, or none, is the state at rest (FORMATS section 7)."""
        try:
            st = os.lstat(wal)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise importer.ImportFailed(f"cannot read {wal}: {exc.strerror or exc}") from exc
        if not stat.S_ISREG(st.st_mode) or st.st_size:
            raise NotSupported(
                "this conversation has a write-ahead log that is not empty: its last "
                f"changes are not in the database file, and a copy would not have them ({wal})"
            )
