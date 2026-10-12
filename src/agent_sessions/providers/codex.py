"""Codex adapter. Formats: docs/FORMATS.md "Codex"."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar

from .. import importer
from ..fsutil import home, listdir_ro, open_ro, subdirs
from ..model import Candidate, Session, Store
from . import register
from .base import NotSupported, Provider, Turn, clean_title, iter_jsonl, parse_ts, unreadable, warn

if TYPE_CHECKING:
    from ..fsutil import StrPath
    from ..pathmap import PathMap

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Groups: local time, thread id, segment id (or None), ".zst" (or None).
_ROLLOUT = re.compile(
    rf"^rollout-(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}})-({_UUID})(?:_({_UUID}))?"
    r"\.jsonl(\.zst)?$"
)
_INDEX = "session_index.jsonl"
_ROLES = {"UserMessage": "user", "AgentMessage": "assistant"}
# Where the Orca app keeps its own CODEX_HOME, relative to the profile that
# holds ``.codex`` (FORMATS section 1, "Mirror stores"). Its thread-name index
# can exist when the store's own does not.
_MIRROR_HOMES = (
    ("AppData", "Roaming", "orca", "codex-runtime-home", "home"),
    (".config", "orca", "codex-runtime-home", "home"),
)
# record type -> the payload keys an import maps to local paths (section 6).
_CWD_KEYS = {
    "session_meta": ("cwd", "runtime_workspace_roots"),
    "turn_context": ("cwd", "workspace_roots"),
}


def _subdirs(path: StrPath) -> list[str]:
    try:
        return sorted(subdirs(path))
    except OSError as exc:
        unreadable(path, exc)
        return []


def _item_text(item: dict, joiner: str) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return joiner.join(texts)


class _Rollout:
    """What one pass over a rollout file collects."""

    def __init__(self, path: str):
        self.path = path
        self.records = 0
        self.meta: dict | None = None  # session_meta payload of the first line
        self.first_ordinal: int | None = None
        self.first_ts: str | None = None  # timestamp of the first line
        self.last_ts: str | None = None  # last timestamp in the file
        self.turn_cwd: str | None = None  # first turn_context cwd
        self.turns: list[tuple[int | None, Turn]] = []  # (ordinal, turn)

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
            if not isinstance(item, dict):
                continue
            role = item.get("type")
            role = _ROLES.get(role) if isinstance(role, str) else None
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
            if isinstance(call, str) and call.endswith("_call") and isinstance(tool, str) and tool:
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
        ends = [later.base_end for later in ordered[position + 1 :] if later.base_end is not None]
        cutoff = min(ends) if ends else None
        for ordinal, turn in rollout.turns:
            if cutoff is not None and ordinal is not None and ordinal >= cutoff:
                continue
            yield turn


def _index_title(path: str, thread_id: str) -> str:
    """``thread_name`` of the last index line for the thread; renames append."""
    if not os.path.exists(path):  # the file is optional
        return ""
    name = None
    for record in iter_jsonl(path):
        if record.get("id") == thread_id and isinstance(record.get("thread_name"), str):
            name = record["thread_name"]
    return clean_title(name)


def _index_line(path: str, thread_id: str) -> bytes | None:
    """The last index line that names the thread, as it is in the file."""
    if not os.path.exists(path):
        return None
    found = None
    try:
        with open_ro(path) as fh:
            for raw in fh:
                line = raw.rstrip(b"\r\n")
                try:
                    record = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("id") == thread_id
                    and isinstance(record.get("thread_name"), str)
                ):
                    found = line
    except OSError:
        return None
    return found


def _source_name_line(store_root: str, thread_id: str) -> bytes | None:
    """The thread's name line from the store's index, else from the Orca
    mirror's beside it."""
    homes = [store_root] + [
        os.path.join(os.path.dirname(store_root), *parts) for parts in _MIRROR_HOMES
    ]
    for directory in homes:
        line = _index_line(os.path.join(directory, _INDEX), thread_id)
        if line is not None:
            return line
    return None


class _CwdRewrite(importer.Rewrite):
    """Maps the recorded working directories of a rollout to local paths.

    Only ``session_meta`` and ``turn_context`` lines are touched, and of
    those only ``payload.cwd`` and the workspace-root lists; a path that does
    not map to an existing folder is left as it is. A changed line is
    serialised again the way the tool writes it (compact, non-ASCII as it
    is) and parsed back to confirm that nothing else moved. Every other line
    goes through byte for byte.
    """

    label = "working directories in session_meta and turn_context records"

    def __init__(self, pathmap: PathMap, mount: str) -> None:
        super().__init__()
        self._pathmap = pathmap
        self._mount = mount
        self._seen: dict[tuple[str, str, str, str], int] = {}

    def _local(self, value: Any) -> Any:  # a JSON value: itself, or the mapped path
        if not isinstance(value, str) or not value:
            return value
        local, exists = self._pathmap.to_local(value, self._mount)
        return local if exists and local else value

    def _line(self, raw: bytes) -> bytes:
        body = raw.rstrip(b"\r\n")
        if b'"session_meta"' not in body and b'"turn_context"' not in body:
            return raw
        try:
            text = body.decode("utf-8")
            record, original = json.loads(text), json.loads(text)
        except (ValueError, RecursionError):
            return raw
        kind = record.get("type") if isinstance(record, dict) else None
        payload = record.get("payload") if isinstance(record, dict) else None
        if kind not in _CWD_KEYS or not isinstance(payload, dict):
            return raw
        changed = []
        for key in _CWD_KEYS[kind]:
            value = payload.get(key)
            if isinstance(value, str):
                mapped = self._local(value)
                if mapped != value:
                    payload[key] = mapped
                    changed.append((kind, f"payload.{key}", value, mapped))
            elif isinstance(value, list):
                mapped = [self._local(item) for item in value]
                for old, new in zip(value, mapped, strict=False):
                    if old != new:
                        changed.append((kind, f"payload.{key}[]", old, new))
                payload[key] = mapped
        if not changed:
            return raw
        out = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        allowed = [("payload", key) for key in _CWD_KEYS[kind]]
        if not importer.only_changed(original, json.loads(out), allowed):
            raise importer.ImportFailed(
                "a rollout line did not survive rewriting unchanged; nothing was imported"
            )
        for change in changed:
            self._seen[change] = self._seen.get(change, 0) + 1
        return out.encode("utf-8") + raw[len(body) :]

    def apply(self, reader: BinaryIO) -> Iterator[bytes]:
        self._seen = {}
        self.changes = []
        for raw in reader:
            yield self._line(raw)
        self.changes = [
            f"{kind} {field}: {old} -> {new}" + (f" ({count} records)" if count > 1 else "")
            for (kind, field, old, new), count in sorted(self._seen.items())
        ]


def _is_subagent(meta: dict) -> bool:
    if "source" in meta and not isinstance(meta["source"], str):
        return True
    if "thread_source" in meta and meta["thread_source"] != "user":
        return True
    return "agent_nickname" in meta or "agent_role" in meta


def _mtime(path: StrPath) -> str | None:
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
    dir_names: ClassVar[set[str]] = {".codex"}

    def native_store(self) -> Path:
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
                    except OSError as exc:
                        unreadable(folder, exc)
                        continue
                    for name in names:
                        match = _ROLLOUT.match(name)
                        if match:
                            threads.setdefault(match.group(2), []).append(
                                os.path.join(folder, name)
                            )
        index = os.path.join(store.root, _INDEX)
        return [
            Candidate(thread, sorted(paths) + [index]) for thread, paths in sorted(threads.items())
        ]

    def parse(self, store: Store, c: Candidate) -> Session | None:
        rollouts = []
        for path in sorted(set(c.paths)):
            match = _ROLLOUT.match(os.path.basename(path))
            if not match:
                continue  # the index
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
                warn(
                    f"belongs to thread {rollout.thread_id}, not {thread_id}; skipped", rollout.path
                )
            else:
                kept.append(rollout)
        ordered = kept
        meta = base.meta or {}
        if meta.get("source") == "exec" or _is_subagent(meta):
            return None  # non-interactive run, or a sub-agent thread
        turns = list(_surviving(ordered))
        if not turns:
            return None  # empty thread
        title = _index_title(os.path.join(store.root, _INDEX), thread_id)
        if not title:
            first = next((t.text for t in turns if t.role == "user"), None)
            title = clean_title(first) or clean_title(thread_id[:8])
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = base.turn_cwd or next((r.turn_cwd for r in ordered if r.turn_cwd), None)
        last = ordered[-1]
        return Session.for_store(
            store,
            id=thread_id,
            title=title,
            cwd=cwd,
            started=parse_ts(meta.get("timestamp")) or base.first_ts,
            updated=last.last_ts or _mtime(last.path),
            messages=len(turns),
            files=[r.path for r in ordered],
        )

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "resume", s.id]

    def transcript(self, s: Session) -> Iterator[Turn]:
        rollouts = []
        for path in s.files:
            match = _ROLLOUT.match(os.path.basename(path))
            if match and not match.group(4):
                rollouts.append(_read(path, tools=True))
        yield from _surviving(_ordered(rollouts))

    def import_plan(self, s: Session, pathmap: PathMap) -> importer.ImportPlan:
        """Every rollout of the thread to the same ``YYYY/MM/DD/<name>``
        (FORMATS section 6). A thread in one file has its working directories
        rewritten; a thread with segments keeps the base file (and any middle
        segment) byte for byte, because a segment's ``history_base`` holds a
        byte offset into the base, and only its last segment is rewritten.
        The thread's name line, when the source has one, is appended to the
        native ``session_index.jsonl`` after the files are in place."""
        files = [f for f in s.files if _ROLLOUT.match(os.path.basename(f))]
        if not files:
            raise NotSupported("this thread has no rollout file to copy")
        plan = importer.start_plan(s, self.native_store(), pathmap, self.id_pattern)
        native = Store(self.name, plan.native_root, "native")
        for candidate in self.candidates(native):
            if candidate.id_hint == s.id:
                plan.existing = [p for p in candidate.paths if _ROLLOUT.match(os.path.basename(p))]
        source_sessions = os.path.join(s.store, "sessions")
        for position, path in enumerate(files):
            parts = os.path.relpath(path, source_sessions).split(os.sep)
            if len(parts) != 4 or parts[0] == os.pardir:
                raise NotSupported(f"a rollout is not under sessions/YYYY/MM/DD: {path}")
            rewrite = None
            if position == len(files) - 1:
                rewrite = _CwdRewrite(pathmap, s.mount)
            plan.units.append(
                importer.Unit(
                    os.path.join(plan.native_root, "sessions", *parts),
                    path,
                    [importer.file_copy(path, rewrite=rewrite)],
                )
            )
        index = os.path.join(plan.native_root, _INDEX)
        line = _source_name_line(s.store, s.id)
        if line is not None and _index_line(index, s.id) is None:
            plan.appends.append(importer.Append(index, line + b"\n", "the thread's name"))
        meta = _read_meta(files[0])
        if isinstance(meta.get("cli_version"), str):
            plan.info["source_tool_version"] = meta["cli_version"]
        return plan


def _read_meta(path: str) -> dict:
    """The ``session_meta`` payload of a rollout's first line, or ``{}``."""
    for record in iter_jsonl(path):
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            return payload
        break
    return {}
