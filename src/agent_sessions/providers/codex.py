"""Codex adapter. Formats: docs/FORMATS.md "Codex"."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

from ..fsutil import home, listdir_ro, subdirs
from ..model import Candidate, Session, Store
from . import register
from .base import Provider, Turn, clean_title, iter_jsonl, parse_ts, warn

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Groups: local time, thread id, segment id (or None), ".zst" (or None).
_ROLLOUT = re.compile(
    rf"^rollout-(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}})-({_UUID})(?:_({_UUID}))?"
    r"\.jsonl(\.zst)?$")
_INDEX = "session_index.jsonl"
_ROLES = {"UserMessage": "user", "AgentMessage": "assistant"}


def _subdirs(path) -> list[str]:
    try:
        return sorted(subdirs(path))
    except OSError:
        return []


def _item_text(item: dict, joiner: str) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    texts = [block["text"] for block in content
             if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return joiner.join(texts)


class _Rollout:
    """What one pass over a rollout file collects."""

    def __init__(self, path: str):
        self.path = path
        self.records = 0
        self.meta: dict | None = None        # session_meta payload of the first line
        self.first_ordinal: int | None = None
        self.first_ts: str | None = None     # timestamp of the first line
        self.last_ts: str | None = None      # last timestamp in the file
        self.turn_cwd: str | None = None     # first turn_context cwd
        self.turns: list[tuple[int | None, Turn]] = []   # (ordinal, turn)

    @property
    def thread_id(self) -> str | None:
        ident = self.meta.get("id") if self.meta else None
        return ident if isinstance(ident, str) and ident else None

    @property
    def base_end(self) -> int | None:
        """``history_base.end_ordinal_exclusive`` of a continuation segment."""
        base = self.meta.get("history_base") if self.meta else None
        end = base.get("end_ordinal_exclusive") if isinstance(base, dict) else None
        return end if isinstance(end, int) and not isinstance(end, bool) else None

    @property
    def is_segment(self) -> bool:
        if self.meta is not None and "history_base" in self.meta:
            return True
        match = _ROLLOUT.match(os.path.basename(self.path))
        return bool(match and match.group(3))


def _read(path: str, tools: bool = False) -> _Rollout:
    """Read a rollout once. ``turns`` gets the user and agent messages, and
    with ``tools`` one turn per tool call."""
    rollout = _Rollout(path)
    for record in iter_jsonl(path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        kind = record.get("type")
        ordinal = record.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            ordinal = None
        moment = parse_ts(record.get("timestamp"))
        rollout.records += 1
        if rollout.records == 1:
            rollout.first_ordinal = ordinal
            rollout.first_ts = moment
            if kind == "session_meta":
                # base_instructions is some 20 kB of prompt text; not needed.
                rollout.meta = {k: v for k, v in payload.items() if k != "base_instructions"}
        if moment is not None:
            rollout.last_ts = moment
        if kind == "event_msg":
            if payload.get("type") != "item_completed":
                continue
            item = payload.get("item")
            role = _ROLES.get(item.get("type")) if isinstance(item, dict) else None
            if role == "user":
                rollout.turns.append((ordinal, Turn(role, _item_text(item, " "), moment)))
            elif role == "assistant":
                rollout.turns.append((ordinal, Turn(role, _item_text(item, "\n"), moment)))
        elif kind == "turn_context":
            if rollout.turn_cwd is None:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    rollout.turn_cwd = cwd
        elif kind == "response_item" and tools:
            call, tool = payload.get("type"), payload.get("name")
            if (isinstance(call, str) and call.endswith("_call")
                    and isinstance(tool, str) and tool):
                rollout.turns.append((ordinal, Turn("tool", tool, moment)))
    return rollout


def _ordered(rollouts: list[_Rollout]) -> list[_Rollout]:
    """Thread order: by first ordinal (the base file starts at 0)."""
    return sorted(rollouts, key=lambda r: (r.first_ordinal or 0, r.path))


def _surviving(ordered: list[_Rollout]) -> Iterator[Turn]:
    """The thread's turns, oldest first, without the records a later segment
    superseded: those with ``ordinal >= history_base.end_ordinal_exclusive``
    of any segment that follows their file."""
    for position, rollout in enumerate(ordered):
        ends = [later.base_end for later in ordered[position + 1:]
                if later.base_end is not None]
        cutoff = min(ends) if ends else None
        for ordinal, turn in rollout.turns:
            if cutoff is not None and ordinal is not None and ordinal >= cutoff:
                continue
            yield turn


def _index_title(path: str, thread_id: str) -> str:
    """``thread_name`` of the last index line for the thread; renames append."""
    if not os.path.exists(path):   # the file is optional
        return ""
    name = None
    for record in iter_jsonl(path):
        if record.get("id") == thread_id and isinstance(record.get("thread_name"), str):
            name = record["thread_name"]
    return clean_title(name)


def _is_subagent(meta: dict) -> bool:
    if "source" in meta and not isinstance(meta["source"], str):
        return True
    if "thread_source" in meta and meta["thread_source"] != "user":
        return True
    return "agent_nickname" in meta or "agent_role" in meta


def _mtime(path) -> str | None:
    try:
        return parse_ts(os.stat(path).st_mtime)
    except OSError:
        return None


@register
class Codex(Provider):
    name = "codex"
    label = "Codex"
    binary = "codex"
    app_id = "org.omarchy.codex"
    resumable = True
    dir_names = {".codex"}

    def native_store(self) -> Path | None:
        return home() / ".codex"

    # The directory name tells a Codex store from a Grok one: both hold
    # sessions/ (Codex's has YYYY/ directories, Grok's percent-encoded names).
    def is_store(self, path: Path) -> bool:
        return (path / "sessions").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        """One candidate per thread: the rollout files under
        ``sessions/YYYY/MM/DD/`` grouped by the first uuid in their name,
        plus the thread-name index (listed even when absent)."""
        sessions = os.path.join(store.root, "sessions")
        threads: dict[str, list[str]] = {}
        for year in _subdirs(sessions):
            for month in _subdirs(os.path.join(sessions, year)):
                for day in _subdirs(os.path.join(sessions, year, month)):
                    folder = os.path.join(sessions, year, month, day)
                    try:
                        names = listdir_ro(folder)
                    except OSError:
                        continue
                    for name in names:
                        match = _ROLLOUT.match(name)
                        if match:
                            threads.setdefault(match.group(2), []).append(
                                os.path.join(folder, name))
        index = os.path.join(store.root, _INDEX)
        return [Candidate(thread, sorted(paths) + [index])
                for thread, paths in sorted(threads.items())]

    def parse(self, store: Store, c: Candidate) -> Session | None:
        rollouts = []
        for path in sorted(set(c.paths)):
            match = _ROLLOUT.match(os.path.basename(path))
            if not match:
                continue   # the index
            if match.group(4):
                warn("compressed rollout (.jsonl.zst) is not supported; skipped", path)
                continue
            rollout = _read(path)
            if rollout.records:
                rollouts.append(rollout)
        if not rollouts:
            return None
        ordered = _ordered(rollouts)
        base = next((r for r in ordered if not r.is_segment), ordered[0])
        thread_id = base.thread_id
        if thread_id is None:
            match = _ROLLOUT.match(os.path.basename(base.path))
            thread_id = match.group(2) if match else c.id_hint
        kept = []
        for rollout in ordered:
            if rollout.thread_id not in (None, thread_id):
                warn(f"belongs to thread {rollout.thread_id}, not {thread_id}; skipped",
                     rollout.path)
            else:
                kept.append(rollout)
        ordered = kept
        meta = base.meta or {}
        if meta.get("source") == "exec" or _is_subagent(meta):
            return None   # non-interactive run, or a sub-agent thread
        turns = list(_surviving(ordered))
        if not turns:
            return None   # empty thread
        title = _index_title(os.path.join(store.root, _INDEX), thread_id)
        if not title:
            first = next((t.text for t in turns if t.role == "user"), None)
            title = clean_title(first) or thread_id[:8]
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = base.turn_cwd or next((r.turn_cwd for r in ordered if r.turn_cwd), None)
        last = ordered[-1]
        return Session.for_store(
            store, id=thread_id, title=title, cwd=cwd,
            started=parse_ts(meta.get("timestamp")) or base.first_ts,
            updated=last.last_ts or _mtime(last.path),
            messages=len(turns), files=[r.path for r in ordered])

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "resume", s.id]

    def transcript(self, s: Session) -> Iterator[Turn]:
        rollouts = []
        for path in s.files:
            match = _ROLLOUT.match(os.path.basename(path))
            if match and not match.group(4):
                rollouts.append(_read(path, tools=True))
        yield from _surviving(_ordered(rollouts))

    def import_session(self, s: Session, pathmap) -> Session:
        raise NotImplementedError
