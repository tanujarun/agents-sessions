"""Antigravity adapter. Formats: docs/FORMATS.md "Antigravity"."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Iterator

from ..fsutil import home, listdir_ro
from ..model import Candidate, Session, Store
from . import register
from .base import (NotSupported, Provider, Turn, clean_title, iter_jsonl, open_ro,
                   open_sqlite_ro, parse_ts, unreadable, warn)

IDE_NOTE = "Antigravity IDE conversation: list only"
# The verdict of FORMATS section 6, shown to the user as it is.
IMPORT_UNSAFE = ("Antigravity keeps conversations in SQLite files with protobuf columns; "
                 "registering one needs a row in conversation_summaries.db and the "
                 "workspace path is inside protobuf blobs")

_SUMMARIES = "conversation_summaries.db"
_SIDECARS = ("-wal", "-shm", "-journal")
_SMALL_FILE = 16 * 1024 * 1024
_STEP_USER, _STEP_PLANNER = 14, 15
_USER_REQUEST = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_PBTXT_TITLE = re.compile(rb'^\s*title\s*:\s*"((?:[^"\\]|\\.)*)"', re.M | re.S)
_PBTXT_ESCAPE = re.compile(rb"\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", re.S)
_PBTXT_SIMPLE = {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"a": b"\a", b"b": b"\b",
                 b"f": b"\f", b"v": b"\v"}


# --- protobuf without a schema ----------------------------------------------

def _varint(data: bytes, pos: int):
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


def pb_fields(data) -> list[tuple[int, int, object]]:
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
            value = data[pos:pos + width]
            pos += width
        elif wire == 2:
            length, pos = _varint(data, pos)
            if length is None or pos + length > size:
                break
            value = data[pos:pos + length]
            pos += length
        else:
            break
        out.append((number, wire, value))
    return out


def pb_all(data, *path) -> list:
    """Every value at a nested field path, e.g. ``pb_all(blob, 20, 7, 2)``;
    repeated fields are followed at each level."""
    values = [data]
    for number in path:
        values = [value for message in values
                  for found, _wire, value in pb_fields(message) if found == number]
    return values


def pb_first(data, *path):
    """The first value at a nested field path, or None."""
    values = pb_all(data, *path)
    return values[0] if values else None


def pb_text(value) -> str | None:
    """A length-delimited value as text; None for anything else."""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else None


def pb_timestamp(value) -> tuple[int, int] | None:
    """A ``{1: seconds, 2: nanos}`` message as ``(seconds, nanos)``; None
    when it is missing, cut off or not after the epoch (Go's zero time)."""
    seconds = nanos = None
    for number, wire, found in pb_fields(value):
        if wire == 0 and number == 1 and seconds is None:
            seconds = found - (1 << 64) if found >= 1 << 63 else found
        elif wire == 0 and number == 2 and nanos is None:
            nanos = found
    if seconds is None or seconds <= 0:
        return None
    return seconds, nanos if nanos is not None and nanos < 1_000_000_000 else 0


# --- small values -----------------------------------------------------------

def uri_to_path(uri) -> str | None:
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


def _read_small(path) -> bytes | None:
    try:
        with open_ro(path) as fh:
            return fh.read(_SMALL_FILE)
    except OSError:
        return None


def _pbtxt_title(path) -> str:
    """The ``title:"…"`` value of an annotations text proto, or ''."""
    raw = _read_small(path)
    found = _PBTXT_TITLE.search(raw) if raw else None
    if not found:
        return ""

    def unescape(match):
        code = match.group(1)
        if len(code) > 1 and code[:1] == b"x":
            return bytes([int(code[1:], 16)])
        if code[:1] in b"01234567":
            return bytes([int(code, 8) & 0xFF])
        return _PBTXT_SIMPLE.get(code, code)

    return _PBTXT_ESCAPE.sub(unescape, found.group(1)).decode("utf-8", "replace")


def _user_request(content) -> str:
    """What the user typed in a USER_INPUT ``content``: the inside of
    ``<USER_REQUEST>``, which is followed by injected metadata blocks."""
    if not isinstance(content, str):
        return ""
    wrapped = _USER_REQUEST.search(content)
    return (wrapped.group(1) if wrapped else content).strip()


def _mtime(path) -> str | None:
    try:
        return parse_ts(os.stat(path).st_mtime)
    except OSError:
        return None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _connect(path) -> sqlite3.Connection:
    con = open_sqlite_ro(path)
    con.text_factory = lambda raw: raw.decode("utf-8", "replace")
    return con


def _optional(con, sql):
    """First row of a query on a table that may not exist, or None."""
    try:
        return con.execute(sql).fetchone()
    except sqlite3.Error:
        return None


@register
class Antigravity(Provider):
    name = "antigravity"
    label = "Antigravity"
    binary = "agy"
    app_id = "org.omarchy.agy"
    resumable = True
    dir_names = {"antigravity-cli", "antigravity-ide"}

    def native_store(self) -> Path | None:
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
            out.append(Candidate(cid, [
                entry,
                str(root / _SUMMARIES),
                str(root / "annotations" / f"{cid}.pbtxt"),
                str(root / "cache" / "last_conversations.json"),
                str(self._log(root, cid)),
            ]))
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
            title, title_source = cid[:8], "id"

        cwd, cwd_source = self._workspace(row.get("workspace_uris")), "workspace_uris"
        if not cwd:
            cwd = uri_to_path(pb_text(pb_first(conv["blob"], 7)))
            cwd_source = "metadata blob field 7"
        if not cwd:
            cwd = self._last_cwd(root / "cache" / "last_conversations.json", cid)
            cwd_source = "last_conversations.json"
        if not cwd:
            cwd, cwd_source = None, "none"

        started = (parse_ts(pb_timestamp(pb_first(conv["blob"], 2)))
                   or parse_ts(pb_timestamp(pb_first(conv["first"], 1))) or _mtime(db))
        modified = _text(row.get("last_modified_time"))
        updated = ((parse_ts(modified) if not modified.startswith("0001-") else None)
                   or parse_ts(pb_timestamp(pb_first(conv["last"], 1))) or _mtime(db))

        files = [str(db)]
        files += [str(p) for p in (root / "brain" / cid, annotation) if os.path.exists(p)]
        note = IDE_NOTE if root.name == "antigravity-ide" else None
        return {"id": cid, "title": title, "cwd": cwd, "started": started, "updated": updated,
                "messages": messages, "files": files, "note": note,
                "sources": {"title": title_source, "cwd": cwd_source}}

    @staticmethod
    def _conversation(db: Path) -> dict | None:
        """What one conversation database holds; None (with a warning) when
        it cannot be read as one."""
        try:
            with contextlib.closing(_connect(db)) as con:
                steps = con.execute("SELECT count(*) FROM steps").fetchone()[0]
                messages = con.execute(
                    "SELECT count(*) FROM steps WHERE step_type IN (?, ?)",
                    (_STEP_USER, _STEP_PLANNER)).fetchone()[0]
                first = con.execute(
                    "SELECT metadata FROM steps ORDER BY idx ASC LIMIT 1").fetchone()
                last = con.execute(
                    "SELECT metadata FROM steps ORDER BY idx DESC LIMIT 1").fetchone()
                blob = _optional(
                    con, "SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
                parents = _optional(con, "SELECT count(*) FROM parent_references")
        except sqlite3.Error as exc:
            warn(f"conversation database not readable: {exc}", db)
            return None
        return {"steps": _count(steps), "messages": _count(messages),
                "first": first[0] if first else None, "last": last[0] if last else None,
                "blob": blob[0] if blob else None,
                "parents": _count(parents[0]) if parents else 0}

    @staticmethod
    def _summary_row(path: Path, cid: str) -> dict:
        """This conversation's row of the summaries index, by column name;
        empty when the index or the row is missing or unreadable."""
        if not os.path.exists(path):
            return {}
        try:
            with contextlib.closing(_connect(path)) as con:
                cursor = con.execute(
                    "SELECT * FROM conversation_summaries WHERE conversation_id = ?", (cid,))
                row = cursor.fetchone()
                if row is None:
                    return {}
                return dict(zip((column[0] for column in cursor.description), row))
        except sqlite3.Error as exc:
            warn(f"conversation summaries not readable: {exc}", path)
            return {}

    @staticmethod
    def _workspace(value) -> str | None:
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
        raw = _read_small(path)
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
                    "SELECT step_type, metadata, step_payload FROM steps ORDER BY idx").fetchall()
        except sqlite3.Error:
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

    def import_plan(self, s: Session, pathmap):
        raise NotSupported(IMPORT_UNSAFE)
