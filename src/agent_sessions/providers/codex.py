"""Codex adapter. Formats: docs/FORMATS.md "Codex"."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar

from .. import fmt, importer, textsafe
from ..fsutil import home, listdir_ro, open_ro, subdirs
from ..model import Candidate, Session, Store
from ..pathmap import normalise
from . import register
from .base import (
    JSONL_LINE_MAX,
    NotSupported,
    Provider,
    Turn,
    clean_title,
    iter_jsonl,
    mtime,
    parse_ts,
    read_lines,
    too_dense,
    unreadable,
    warn,
)

if TYPE_CHECKING:
    from ..fsutil import StrPath
    from ..pathmap import PathMap

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Groups: local time, thread id, segment id (or None), ".zst" (or None).
# A rollout's file name, matched whole (``fullmatch``): with ``match`` and a
# ``$``, a name that ends in a line break would pass too.
_ROLLOUT = re.compile(
    rf"rollout-(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}})-({_UUID})(?:_({_UUID}))?"
    r"\.jsonl(\.zst)?"
)
_DATE_DIRS = re.compile(r"[0-9]{4}/[0-9]{2}/[0-9]{2}")  # YYYY/MM/DD below sessions/
_INDEX = "session_index.jsonl"
# The index is read once for every thread of its store: one of more than
# this many bytes is not read. A line of it holds an id and a name.
_INDEX_MAX = 64 * 1024 * 1024
_INDEX_LINE_MAX = 64 * 1024
_NAME_MAX = 200  # characters of a thread's name that an import writes to the native index
_VERSION_MAX = 64  # characters; a longer "cli_version" is not one
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
        match = _ROLLOUT.fullmatch(os.path.basename(self.path))
        return bool(match and match.group(3))


def _read(path: str, tools: bool = False, titles: bool = False) -> _Rollout:
    """Read a rollout once. ``turns`` gets the user and agent messages, and
    with ``tools`` one turn per tool call. With ``titles`` a turn holds no
    more of its text than a title is made of (``clean_title``): listing a
    thread needs its turns counted and one title, not every message kept."""
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
            if role in ("user", "assistant"):
                text = _item_text(item, " " if role == "user" else "\n")
                rollout.turns.append(
                    (ordinal, Turn(role, clean_title(text) if titles else text, moment))
                )
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


def _index_too_large(path: str) -> bool:
    try:
        large = os.stat(path).st_size > _INDEX_MAX
    except OSError:
        return False  # whoever reads it reports that
    if large:
        warn(f"larger than {fmt.size(_INDEX_MAX)}; thread names not read", path)
    return large


def _index_title(path: str, thread_id: str) -> str:
    """``thread_name`` of the last index line for the thread; renames append."""
    if not os.path.exists(path) or _index_too_large(path):  # the file is optional
        return ""
    name = None
    for record in iter_jsonl(path):
        if record.get("id") == thread_id and isinstance(record.get("thread_name"), str):
            name = record["thread_name"]
    return clean_title(name)


def _index_line(path: str, thread_id: str) -> bytes | None:
    """The last index line that names the thread, as it is in the file."""
    if not os.path.exists(path) or _index_too_large(path):
        return None
    found = None
    try:
        with open_ro(path) as fh:
            for raw in read_lines(fh, _INDEX_LINE_MAX):
                if raw is None:
                    continue  # too long to be a thread's name line
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


def _once(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object from its pairs; one that has a key twice is refused
    (``ValueError``): which of the two values counts is the reader's choice."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key is there twice")
    return found


def _name_line(source: bytes, thread_id: str) -> bytes | None:
    """The line an import adds to the native index for a thread whose name
    line in the source is ``source``: a new line, made here of three values
    and nothing else. No byte of the source line is copied: it is the one
    thing an import writes that an undo does not take back.

    ``id`` is the thread's own id (the import has checked its form);
    ``thread_name`` is the source's as one line of clean text
    (``textsafe.clean``), at most ``_NAME_MAX`` characters; ``updated_at``
    is the source's time in our own form (``parse_ts``). None, and then no
    line is added, when the source line is not strict UTF-8, is not a JSON
    object with every key once, names another thread, leaves nothing of the
    name that can be written, or has no time: every line the tool writes
    has all three fields (FORMATS "Codex"), and one without is not written
    into its file."""
    if too_dense(source):
        return None
    try:
        record = json.loads(source.decode("utf-8"), object_pairs_hook=_once)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(record, dict) or record.get("id") != thread_id:
        return None
    name = record.get("thread_name")
    name = textsafe.clean(name)[:_NAME_MAX].strip() if isinstance(name, str) else ""
    if not name:
        return None
    moment = parse_ts(record.get("updated_at"))
    if moment is None:
        return None
    entry = {"id": thread_id, "thread_name": name, "updated_at": moment}
    try:
        # The tool's own form: compact, text as it is.
        text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        return text.encode("utf-8") + b"\n"
    except UnicodeEncodeError:  # a lone surrogate in the name: no text a tool reads back
        return None


def _folded(path: object) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """A recorded path as ``(root, segments, segments in lower case)``, for
    comparing two spellings of one path: either slash, any case, with or
    without the ``\\\\?\\`` prefix (``pathmap.normalise``). None for what
    has no root, and for a path with a ``..`` segment, which names nothing
    until it is resolved."""
    recorded = normalise(path)
    if recorded is None or ".." in recorded.parts:
        return None
    return recorded.root, recorded.parts, tuple(part.lower() for part in recorded.parts)


class _CwdRewrite(importer.Rewrite):
    """Maps the recorded working directories of a rollout to local paths.

    Only ``session_meta`` and ``turn_context`` lines are touched, and of
    those only ``payload.cwd`` and the workspace-root lists; a path that does
    not map to an existing folder is left as it is. A changed line is
    serialised again the way the tool writes it (compact, non-ASCII as it
    is) and parsed back to confirm that nothing else moved. Every other line
    goes through byte for byte.

    With a chosen folder (``target``, and ``recorded``: the working
    directory the thread recorded) the same values are re-pointed first
    (FORMATS "Phase 2 findings", Q8): a value that is the recorded folder
    becomes the chosen one, a value below it the same path below the chosen
    one, and only a value that is neither goes through the path map.
    """

    label = "working directories in session_meta and turn_context records"

    def __init__(
        self,
        pathmap: PathMap,
        mount: str,
        recorded: str | None = None,
        target: str | None = None,
    ) -> None:
        super().__init__()
        self._pathmap = pathmap
        self._mount = mount
        self._target = target
        self._recorded = _folded(recorded) if target is not None else None
        self._seen: dict[tuple[str, str, str, str], int] = {}

    def _moved(self, value: str) -> str | None:
        """``value`` in the chosen folder, when it is the recorded folder or
        a path below it; None when it is neither, or no folder was chosen."""
        if self._target is None or self._recorded is None:
            return None
        path = _folded(value)
        if path is None:
            return None
        root, parts, lowered = path
        base_root, base_parts, base_lowered = self._recorded
        depth = len(base_parts)
        if root != base_root or lowered[:depth] != base_lowered:
            return None
        return os.path.join(self._target, *parts[depth:])

    def _local(self, value: Any) -> Any:  # a JSON value: itself, or the mapped path
        if not isinstance(value, str) or not value:
            return value
        moved = self._moved(value)
        if moved is not None:
            return moved
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
        while True:
            raw = reader.readline(JSONL_LINE_MAX + 1)
            if not raw:
                break
            if len(raw) <= JSONL_LINE_MAX:
                yield self._line(raw)
                continue
            # Longer than any line that is read as a record: copied as it
            # is, a piece at a time.
            while raw:
                yield raw
                if raw.endswith(b"\n"):
                    break
                raw = reader.readline(JSONL_LINE_MAX)
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
                        match = _ROLLOUT.fullmatch(name)
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
            match = _ROLLOUT.fullmatch(os.path.basename(path))
            if not match:
                continue  # the index
            if match.group(4):
                warn("compressed rollout (.jsonl.zst) is not supported; skipped", path)
                continue
            rollout = _read(path, titles=True)
            if rollout.records:
                rollouts.append(rollout)
        if not rollouts:
            return None
        ordered = _ordered(rollouts)
        base = next((r for r in ordered if not r.is_segment), ordered[0])
        thread_id = base.thread_id
        if thread_id is None:
            match = _ROLLOUT.fullmatch(os.path.basename(base.path))
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
            updated=last.last_ts or mtime(last.path),
            messages=len(turns),
            files=[r.path for r in ordered],
        )

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "resume", s.id]

    def new_session_argv(self, prompt: str) -> list[str]:
        return [self.binary, prompt]  # ``codex [PROMPT]``; never ``codex exec``

    def transcript(self, s: Session) -> Iterator[Turn]:
        rollouts = []
        for path in s.files:
            match = _ROLLOUT.fullmatch(os.path.basename(path))
            if match and not match.group(4):
                rollouts.append(_read(path, tools=True))
        yield from _surviving(_ordered(rollouts))

    def import_plan(
        self,
        s: Session,
        pathmap: PathMap,
        target: str | None = None,
        experimental: bool = False,
    ) -> importer.ImportPlan:
        """Every rollout of the thread to the same ``YYYY/MM/DD/<name>``
        (FORMATS section 6). A thread in one file has its working directories
        rewritten; a thread with segments keeps the base file (and any middle
        segment) byte for byte, because a segment's ``history_base`` holds a
        byte offset into the base, and only its last segment is rewritten.
        When the source's index names the thread, a name line is added to
        the native ``session_index.jsonl`` after the files are in place: a
        line written here from the source's values (``_name_line``), never
        the source's own bytes.

        With a chosen folder (``target``) the destination is the same; the
        working directories are re-pointed at it (``_CwdRewrite``)."""
        files = [f for f in s.files if _ROLLOUT.fullmatch(os.path.basename(f))]
        if not files:
            raise NotSupported("this thread has no rollout file to copy")
        plan = importer.start_plan(
            s, self.native_store(), pathmap, self.id_pattern, target, read_back=self.read_back
        )
        native = Store(self.name, plan.native_root, "native")
        for candidate in self.candidates(native):
            if candidate.id_hint == s.id:
                plan.existing = [
                    p for p in candidate.paths if _ROLLOUT.fullmatch(os.path.basename(p))
                ]
        source_sessions = os.path.join(s.store, "sessions")
        for position, path in enumerate(files):
            # These four names become names in the native store: the date
            # directories must be digits and the file must be a rollout of
            # this thread, matched whole.
            parts = os.path.relpath(path, source_sessions).split(os.sep)
            if len(parts) != 4 or not _DATE_DIRS.fullmatch("/".join(parts[:3])):
                raise NotSupported(f"a rollout is not under sessions/YYYY/MM/DD: {path}")
            named = _ROLLOUT.fullmatch(parts[3])
            if named is None or named.group(2) != s.id:
                raise NotSupported(f"a rollout is not named after its thread id: {path}")
            rewrite = None
            if position == len(files) - 1:
                rewrite = _CwdRewrite(pathmap, s.mount, s.cwd, plan.chosen_folder)
            plan.units.append(
                importer.Unit(
                    os.path.join(plan.native_root, "sessions", *parts),
                    path,
                    [importer.file_copy(path, rewrite=rewrite)],
                )
            )
        index = os.path.join(plan.native_root, _INDEX)
        source = _source_name_line(s.store, s.id)
        line = _name_line(source, s.id) if source is not None else None
        if line is not None and _index_line(index, s.id) is None:
            plan.appends.append(importer.Append(index, line, "the thread's name"))
        meta = _read_meta(files[0])
        version = meta.get("cli_version")
        if isinstance(version, str) and len(version) <= _VERSION_MAX:
            plan.info["source_tool_version"] = version
        return plan


def _read_meta(path: str) -> dict:
    """The ``session_meta`` payload of a rollout's first line, or ``{}``."""
    for record in iter_jsonl(path):
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            return payload
        break
    return {}
