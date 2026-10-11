"""Gemini CLI adapter. Formats: docs/FORMATS.md "Gemini CLI"."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator

from ..fsutil import home, listdir_ro, subdirs
from ..model import Candidate, Session, Store
from . import register
from .base import (NotSupported, Provider, Turn, clean_title, iter_jsonl, open_ro,
                   parse_ts, unreadable, warn)

# The verdict of FORMATS section 6, shown to the user as it is.
IMPORT_UNSAFE = ("Gemini CLI is not installed on this machine, and no resumable Gemini "
                 "session exists to import")

_CHAT_NAME = re.compile(r"^session-.*\.jsonl?$")
# The tool names a chat file ``session-<time>-<first 8 of the session id>``.
_ID_PART = re.compile(r"-([^-]{1,8})\.jsonl?$")
_FIXED_PATHS = 3            # the chat file, .project_root, projects.json
_SMALL_FILE = 16 * 1024 * 1024
_LEGACY_MAX = 64 * 1024 * 1024
_IGNORED_STARTS = ("/", "?", "<session_context>", "<hook_context>")
# Parts that are not text; the tool renders each as a non-empty marker.
_PART_MARKERS = ("videoMetadata", "thought", "codeExecutionResult", "executableCode",
                 "fileData", "functionCall", "functionResponse", "inlineData")


def _id_group(name: str) -> str:
    """Chat files of one session share this part of their names."""
    found = _ID_PART.search(name)
    return found.group(1) if found else name


def _content_string(value) -> str:
    """A message ``content`` as the tool's own listing rule sees it: text,
    with a marker for every part that is not text."""
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_string(part) for part in value)
    if not isinstance(value, dict):
        return ""
    for key in _PART_MARKERS:
        if key in value:
            return f"[{key}]"
    return value["text"] if isinstance(value.get("text"), str) else ""


def _plain_text(value) -> str:
    """The text parts of a message ``content``, nothing else."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_plain_text(part) for part in value)
    if isinstance(value, dict) and not any(key in value for key in _PART_MARKERS):
        return value["text"] if isinstance(value.get("text"), str) else ""
    return ""


def _is_ignored(text: str) -> bool:
    """User content the tool does not count as a prompt: empty, a slash or
    ``?`` command, or injected context."""
    text = text.strip()
    return not text or text.startswith(_IGNORED_STARTS)


def _has_items(value) -> bool:
    return isinstance(value, (list, str)) and len(value) > 0


def _is_resumable(message: dict) -> bool:
    """The tool's "resumable content" test for one message."""
    content = _content_string(message.get("content"))
    if message.get("type") == "user":
        return not _is_ignored(content)
    if message.get("type") == "gemini":
        return (bool(content.strip()) or _has_items(message.get("toolCalls"))
                or _has_items(message.get("thoughts")))
    return False


# Metadata keys this reader uses. A value of another type than text (or one
# that is not a time, for the two times) is left out of a merge, so it cannot
# replace a good one.
_META_TEXT = ("sessionId", "projectHash", "kind", "summary")
_META_TIME = ("startTime", "lastUpdated")


def _usable_meta(patch: dict) -> dict:
    return {key: value for key, value in patch.items()
            if (key not in _META_TEXT or isinstance(value, str))
            and (key not in _META_TIME or parse_ts(value) is not None)}


def _is_message(record) -> bool:
    return isinstance(record, dict) and isinstance(record.get("id"), str)


def apply_records(records) -> tuple[dict, list[dict]]:
    """Replay a chat file's records in order; returns ``(metadata, messages)``.

    A ``$rewindTo`` drops that message and every later one (all of them when
    the id is unknown); a record with a string ``id`` is a message, and one
    whose id is already there replaces it in place; ``$set`` merges metadata
    keys, and a ``messages`` list in it replaces all messages; a record with
    ``sessionId`` and ``projectHash`` is metadata, and a ``messages`` list in
    it adds messages. Anything else is ignored.
    """
    meta: dict = {}
    messages: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("$rewindTo"), str):
            target = record["$rewindTo"]
            if target in messages:
                ids = list(messages)
                for message_id in ids[ids.index(target):]:
                    del messages[message_id]
            else:
                messages.clear()
        elif isinstance(record.get("id"), str):
            messages[record["id"]] = record
        elif isinstance(record.get("$set"), dict):
            patch = record["$set"]
            if isinstance(patch.get("messages"), list):
                messages.clear()
                messages.update((m["id"], m) for m in patch["messages"] if _is_message(m))
            meta.update(_usable_meta(patch))
        elif (isinstance(record.get("sessionId"), str)
              and isinstance(record.get("projectHash"), str)):
            meta.update(_usable_meta(record))
            if isinstance(record.get("messages"), list):
                messages.update((m["id"], m) for m in record["messages"] if _is_message(m))
    return meta, list(messages.values())


def _load(path: Path) -> tuple[dict, list[dict]] | None:
    """Metadata and messages of one chat file, or None when it holds no
    session.

    Read as JSONL first. A file that gives no ``sessionId`` + ``projectHash``
    that way is tried as one JSON object with ``sessionId`` and ``messages``,
    which is what older versions wrote to ``.json`` files (the tool does the
    same: ``parseLegacyRecordFallback``).
    """
    meta, messages = apply_records(iter_jsonl(path))
    if meta.get("sessionId") and meta.get("projectHash"):
        return meta, messages
    try:
        with open_ro(path) as fh:
            raw = fh.read(_LEGACY_MAX + 1)
    except OSError:
        return None             # iter_jsonl has recorded why
    if not raw.strip():
        return None             # created, nothing written yet
    if len(raw) > _LEGACY_MAX:
        warn("no session metadata record; too large to try as one JSON object", path)
        return None
    try:
        legacy = json.loads(raw.decode("utf-8-sig", "replace"))
    except (ValueError, RecursionError):
        legacy = None
    if not isinstance(legacy, dict) or "sessionId" not in legacy:
        warn("no session metadata (sessionId and projectHash)", path)
        return None
    found = legacy.get("messages")
    return legacy, [m for m in found if isinstance(m, dict)] if isinstance(found, list) else []


def _mtime(path) -> str | None:
    try:
        return parse_ts(os.stat(path).st_mtime)
    except OSError:
        return None


def _read_text(path) -> str:
    try:
        with open_ro(path) as fh:
            return fh.read(_SMALL_FILE).decode("utf-8-sig", "replace").strip()
    except OSError:
        return ""


@register
class Gemini(Provider):
    name = "gemini"
    label = "Gemini"
    binary = "gemini"
    app_id = "org.omarchy.gemini"
    # No usable gemini binary exists here (FORMATS "Gemini CLI" section 0).
    resumable = False
    # Resume searches only the chats of the process cwd's project (section 5).
    resume_needs_cwd = True
    dir_names = {".gemini"}

    def native_store(self) -> Path | None:
        return home() / ".gemini"

    def is_store(self, path: Path) -> bool:
        return (path / "tmp").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        """One candidate per ``tmp/<slug>/chats/session-*.json[l]``; the id
        hint is the file's path under the store, since the session id is
        inside the file. Directory listings only.

        ``paths`` = the chat file, the project's ``.project_root``,
        ``projects.json``, and the other chat files of the same directory
        whose names end in the same session-id part: when one session is in
        several files only the newest is listed, and each file's ``parse``
        decides that from the files it is given.
        """
        root = Path(store.root)
        out = []
        try:
            slugs = sorted(subdirs(root / "tmp"))
        except OSError as exc:
            unreadable(root / "tmp", exc)
            slugs = []
        for slug in slugs:
            chats = root / "tmp" / slug / "chats"
            try:
                names = sorted(n for n in listdir_ro(chats) if _CHAT_NAME.match(n))
            except OSError as exc:
                unreadable(chats, exc)
                continue
            groups: dict[str, list[str]] = {}
            for name in names:
                groups.setdefault(_id_group(name), []).append(name)
            for name in names:
                paths = [str(chats / name), str(chats.parent / ".project_root"),
                         str(root / "projects.json")]
                paths += [str(chats / other) for other in groups[_id_group(name)]
                          if other != name]
                out.append(Candidate(f"tmp/{slug}/chats/{name}", paths))
        return out

    def parse(self, store: Store, c: Candidate) -> Session | None:
        fields = self._fields(store, c)
        if fields is None:
            return None
        fields.pop("sources")
        return Session.for_store(store, **fields)

    def _fields(self, store: Store, c: Candidate) -> dict | None:
        """The conversation fields of a listed session plus ``sources``
        (where title and cwd came from), or None."""
        if len(c.paths) < _FIXED_PATHS:
            return None
        path = Path(c.paths[0])
        own = self._read(path)
        if own is None:
            return None
        for other_path in c.paths[_FIXED_PATHS:]:
            other = self._read(Path(other_path))
            if other is None or other["id"] != own["id"]:
                continue
            # The same session in two files: the newest lastUpdated wins,
            # and on a tie the first file name does, as in the tool.
            newer = (other["updated"] or "") > (own["updated"] or "")
            same = (other["updated"] or "") == (own["updated"] or "")
            if newer or (same and Path(other_path).name < path.name):
                return None
        slug_dir = path.parent.parent
        cwd, cwd_source = _read_text(slug_dir / ".project_root"), ".project_root"
        if not cwd:
            cwd, cwd_source = self._registered(Path(c.paths[2]), slug_dir.name), "projects.json"
        if not cwd:
            cwd, cwd_source = None, "none"
        return {"id": own["id"], "title": own["title"], "cwd": cwd, "started": own["started"],
                "updated": own["updated"], "messages": len(own["messages"]),
                "files": [str(path)], "note": None,
                "sources": {"title": own["title_source"], "cwd": cwd_source}}

    @staticmethod
    def _read(path: Path) -> dict | None:
        """One chat file as a listed session, or None: no ``sessionId``, a
        sub-agent's file, or no resumable content."""
        loaded = _load(path)
        if loaded is None:
            return None
        meta, messages = loaded
        session_id = meta.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            warn("no sessionId", path)
            return None
        if meta.get("kind") == "subagent":
            return None
        if not any(_is_resumable(message) for message in messages):
            return None
        title = clean_title(meta.get("summary")) if isinstance(meta.get("summary"), str) else ""
        title_source = "summary"
        if not title:
            title_source = "first user message"
            for message in messages:
                if message.get("type") == "user" and _is_resumable(message):
                    title = clean_title(_plain_text(message.get("content")))
                    if title:
                        break
        if not title:
            title, title_source = session_id[:8], "id"
        start, last = parse_ts(meta.get("startTime")), parse_ts(meta.get("lastUpdated"))
        return {"id": session_id, "title": title, "title_source": title_source,
                "started": start or last or _mtime(path),
                "updated": last or start or _mtime(path), "messages": messages}

    @staticmethod
    def _registered(projects_json: Path, slug: str) -> str | None:
        """The ``projects.json`` key whose value is the slug."""
        text = _read_text(projects_json)
        if not text:
            return None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            warn("not valid JSON", projects_json)
            return None
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            return None
        keys = sorted(key for key, value in projects.items() if value == slug and key)
        return keys[0] if keys else None

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "--resume", s.id]

    def transcript(self, s: Session) -> Iterator[Turn]:
        """From the chat file, after ``$set`` and ``$rewindTo`` are applied."""
        if not s.files:
            return
        loaded = _load(Path(s.files[0]))
        if loaded is None:
            return
        for message in loaded[1]:
            kind = message.get("type")
            ts = parse_ts(message.get("timestamp"))
            if kind == "user":
                if _is_resumable(message):
                    text = _plain_text(message.get("content")).strip()
                    if text:
                        yield Turn("user", text, ts)
            elif kind == "gemini":
                text = _plain_text(message.get("content")).strip()
                if text:
                    yield Turn("assistant", text, ts)
                calls = message.get("toolCalls")
                for call in calls if isinstance(calls, list) else []:
                    name = call.get("name") if isinstance(call, dict) else None
                    if isinstance(name, str) and name:
                        yield Turn("tool", name, parse_ts(call.get("timestamp")) or ts)

    def import_plan(self, s: Session, pathmap):
        raise NotSupported(IMPORT_UNSAFE)
