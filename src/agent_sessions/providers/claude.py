"""Claude Code adapter. Formats: docs/FORMATS.md "Claude Code"."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .. import importer
from ..fsutil import home, listdir_ro, open_ro, subdirs
from ..model import Candidate, Session, Store
from . import register
from .base import (
    TITLE_SOURCE_MAX,
    NotSupported,
    Provider,
    Turn,
    clean_title,
    iter_jsonl,
    parse_ts,
    unreadable,
    warn,
)

if TYPE_CHECKING:
    from ..fsutil import StrPath
    from ..pathmap import PathMap

_TRANSCRIPT = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")

# The tool's own first-prompt rule (FORMATS "Real prompt").
_COMMAND = re.compile(r"<command-name>(.*?)</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>")
_BASH = re.compile(r"<bash-input>([\s\S]*?)</bash-input>")
_NOT_A_PROMPT = re.compile(
    r"^(?:\s*<[a-z][A-Za-z0-9_-]*[\s>]|\[Request interrupted by user[^\]]*\])"
)

# Title records, in the tool's order of preference: record type -> field.
_TITLE_FIELDS = (
    ("agent-name", "agentName"),
    ("custom-title", "customTitle"),
    ("ai-title", "aiTitle"),
    ("summary", "summary"),
)
_TITLE_FIELD = dict(_TITLE_FIELDS)

ENCODED_MAX = 200  # longer directory names are cut and get a hash
_ENCODED = re.compile(r"[A-Za-z0-9-]+")  # all ``encode_cwd`` can give
_HEAD = 65536  # the tool itself reads this much for cheap fields
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def encode_cwd(cwd: str) -> str:
    """The project directory name the tool gives a working directory
    (FORMATS "Claude Code" section 2; the tool's functions ``OO``, ``Mse``).

    Every UTF-16 code unit that is not an ASCII letter or digit becomes
    ``-``. A result over 200 characters is cut to 200 and gets ``-`` plus the
    base-36 absolute value of the 32-bit string hash ``h = h * 31 + unit``
    over the original text.
    """
    raw = cwd.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    encoded = "".join(chr(unit) if unit < 128 and chr(unit).isalnum() else "-" for unit in units)
    if len(encoded) <= ENCODED_MAX:
        return encoded
    total = 0
    for unit in units:
        total = (total * 31 + unit) & 0xFFFFFFFF
    if total >= 0x80000000:
        total -= 0x100000000
    total = abs(total)
    digits = ""
    while True:
        total, digit = divmod(total, 36)
        digits = _BASE36[digit] + digits
        if not total:
            break
    return f"{encoded[:ENCODED_MAX]}-{digits}"


def _recorded_version(path: StrPath) -> str | None:
    """The tool version of the first record that names one, looking only at
    the head of the transcript."""
    try:
        with open_ro(path) as fh:
            head = fh.read(_HEAD)
    except OSError:
        return None
    for line in head.split(b"\n")[:-1]:
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            continue
        version = record.get("version") if isinstance(record, dict) else None
        if isinstance(version, str):
            return version
    return None


def _prompts(record: dict) -> tuple[list[str], str | None]:
    """``(real prompt texts, command fallback)`` of one record.

    A record is a real prompt when the list is not empty. The texts keep
    their line breaks; a ``<bash-input>`` becomes ``"! <command>"``. The
    command fallback is the first ``<command-name>`` of the record, with its
    ``<command-args>`` when they are not empty.
    """
    if (
        record.get("type") != "user"
        or record.get("isMeta") is True
        or record.get("isCompactSummary") is True
    ):
        return [], None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                return [], None
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
    real, command = [], None
    for raw in texts:
        flat = raw.replace("\n", " ").strip()
        if not flat:
            continue
        found = _COMMAND.search(flat)
        if found:
            if command is None:
                found_args = _COMMAND_ARGS.search(flat)
                args = found_args.group(1).strip() if found_args else ""
                command = f"{found.group(1)} {args}" if args else found.group(1)
            continue
        found = _BASH.search(raw)
        if found:
            real.append("! " + found.group(1).strip())
        elif not _NOT_A_PROMPT.match(flat):
            real.append(raw.strip())
    return real, command


class _Scan:
    """What one pass over a transcript collects."""

    def __init__(self) -> None:
        self.has_message = False  # a record with a parentUuid key
        self.sidechain = False  # the first such record is a sidechain one
        self.prompts = 0  # real prompt records
        self.assistants = 0  # assistant records
        self.assistant_ids: set[str] = set()
        self.assistant_anonymous = 0  # assistant records without message.id
        self.titles: dict[str, str] = {}  # record type -> last value
        self.first_prompt: str | None = None
        self.command: str | None = None
        self.cwd: str | None = None  # first recorded
        self.relocated: str | None = None  # last recorded
        self.started: str | None = None
        self.updated: str | None = None
        self.continued_in: str | None = None

    @property
    def messages(self) -> int:
        return self.prompts + len(self.assistant_ids) + self.assistant_anonymous


def _scan(path: StrPath) -> _Scan:
    """Read a transcript once, line by line."""
    scan = _Scan()
    for record in iter_jsonl(path):
        if not scan.has_message and "parentUuid" in record:
            scan.has_message = True
            if record.get("isSidechain") is True:
                scan.sidechain = True
                return scan
        moment = record.get("timestamp")
        if moment is not None:
            moment = parse_ts(moment)
            if moment is not None:
                if scan.started is None or moment < scan.started:
                    scan.started = moment
                if scan.updated is None or moment > scan.updated:
                    scan.updated = moment
        if scan.cwd is None:
            cwd = record.get("cwd")
            if isinstance(cwd, str) and cwd:
                scan.cwd = cwd
        kind = record.get("type")
        if kind == "user":
            scan.continued_in = None
            real, command = _prompts(record)
            # Kept for the title only: the start is all a title is made from.
            if command is not None and scan.command is None:
                scan.command = command[:TITLE_SOURCE_MAX]
            if real:
                scan.prompts += 1
                if scan.first_prompt is None:
                    scan.first_prompt = real[0][:TITLE_SOURCE_MAX]
        elif kind == "assistant":
            scan.continued_in = None
            scan.assistants += 1
            message = record.get("message")
            ident = message.get("id") if isinstance(message, dict) else None
            if isinstance(ident, str) and ident:
                scan.assistant_ids.add(ident)
            else:
                scan.assistant_anonymous += 1
        elif isinstance(kind, str) and kind in _TITLE_FIELD:
            value = record.get(_TITLE_FIELD[kind])
            if isinstance(value, str):
                scan.titles[kind] = value[:TITLE_SOURCE_MAX]
        elif kind == "continued-in":
            child = record.get("continuedInSessionId")
            if isinstance(child, str) and child:
                scan.continued_in = child
        elif kind == "relocated":
            cwd = record.get("relocatedCwd")
            if isinstance(cwd, str) and cwd:
                scan.relocated = cwd
    return scan


def _title(scan: _Scan, session_id: str) -> tuple[str, str]:
    """``(title, source)``; source is a title record type, ``first-prompt``,
    ``command`` or ``id``."""
    for kind, _field in _TITLE_FIELDS:
        title = clean_title(scan.titles.get(kind))
        if title:
            return title, kind
    title = clean_title(scan.first_prompt)
    if title:
        return title, "first-prompt"
    title = clean_title(scan.command)
    if title:
        return title, "command"
    return clean_title(session_id[:8]), "id"


def _mtime(path: StrPath) -> str | None:
    try:
        return parse_ts(os.stat(path).st_mtime)
    except OSError:
        return None


@register
class Claude(Provider):
    name = "claude"
    label = "Claude"
    binary = "claude"
    app_id = "org.omarchy.claude"
    resumable = True
    dir_names: ClassVar[set[str]] = {".claude"}

    def native_store(self) -> Path:
        return home() / ".claude"

    def is_store(self, path: Path) -> bool:
        return (path / "projects").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        """``projects/<dir>/<uuid>.jsonl``, at that depth only.

        An id found in two project directories is one candidate holding both
        paths; ``parse`` reads the first and warns about the rest.
        """
        projects = os.path.join(store.root, "projects")
        try:
            dirs = subdirs(projects)
        except OSError as exc:
            unreadable(projects, exc)
            return []
        found: dict[str, list[str]] = {}
        for name in dirs:
            try:
                entries = listdir_ro(os.path.join(projects, name))
            except OSError as exc:
                unreadable(os.path.join(projects, name), exc)
                continue
            for entry in entries:
                match = _TRANSCRIPT.match(entry)
                if match:
                    found.setdefault(match.group(1), []).append(os.path.join(projects, name, entry))
        return [Candidate(ident, sorted(paths)) for ident, paths in sorted(found.items())]

    def parse(self, store: Store, c: Candidate) -> Session | None:
        paths = sorted(set(c.paths))
        if not paths:
            return None
        path = paths[0]
        for other in paths[1:]:
            warn(f"session id also exists at {path}; this copy is not listed", other)
        match = _TRANSCRIPT.match(os.path.basename(path))
        session_id = match.group(1) if match else c.id_hint
        scan = _scan(path)
        if not scan.has_message or scan.sidechain:
            return None  # bookkeeping-only, or a sidechain at top level
        if not scan.prompts and not scan.assistants:
            return None  # empty session
        title, _source = _title(scan, session_id)
        files = [path]
        side = path[: -len(".jsonl")] if path.endswith(".jsonl") else ""
        if side and os.path.isdir(side):
            files.append(side)
        fallback = None
        if scan.started is None or scan.updated is None:
            fallback = _mtime(path)
        return Session.for_store(
            store,
            id=session_id,
            title=title,
            cwd=scan.relocated or scan.cwd,
            started=scan.started or fallback,
            updated=scan.updated or fallback,
            messages=scan.messages,
            files=files,
            note=f"continued in {scan.continued_in}" if scan.continued_in else None,
        )

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "--resume", s.id]

    def transcript(self, s: Session) -> Iterator[Turn]:
        path = next((f for f in s.files if f.endswith(".jsonl")), None)
        if path is None:
            return
        for record in iter_jsonl(path):
            kind = record.get("type")
            if kind == "user":
                real, _command = _prompts(record)
                if real:
                    yield Turn("user", "\n".join(real), parse_ts(record.get("timestamp")))
            elif kind == "assistant":
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                moment = parse_ts(record.get("timestamp"))
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            yield Turn("assistant", text.strip(), moment)
                    elif block.get("type") == "tool_use":
                        tool = block.get("name")
                        if isinstance(tool, str) and tool:
                            yield Turn("tool", tool, moment)

    def import_plan(self, s: Session, pathmap: PathMap) -> importer.ImportPlan:
        """Copy only (FORMATS section 6): the ``<id>/`` side directory first,
        the transcript last, into ``projects/<encoded mapped cwd>/``. Nothing
        is rewritten and nothing else is copied."""
        transcript = next((f for f in s.files if _TRANSCRIPT.match(os.path.basename(f))), None)
        if transcript is None:
            raise NotSupported("this session has no transcript file to copy")
        # The file's name becomes a name in the native store: it must be
        # exactly ``<this session's id>.jsonl`` (``match`` and ``$`` alone
        # would let a name with a trailing line break through).
        name = os.path.basename(transcript)
        named = _TRANSCRIPT.fullmatch(name)
        if named is None or named.group(1) != s.id:
            raise NotSupported(f"the transcript is not named after its session id: {transcript}")
        plan = importer.start_plan(s, self.native_store(), pathmap, self.id_pattern)
        folder = encode_cwd(plan.cwd_local)
        if not _ENCODED.fullmatch(folder):
            raise NotSupported(f"the project folder {plan.cwd_local} has no directory name")
        projects = os.path.join(plan.native_root, "projects")
        try:
            dirs = sorted(subdirs(projects))
        except OSError:
            dirs = []
        plan.existing = [
            path
            for path in (os.path.join(projects, d, name) for d in dirs)
            if os.path.lexists(path)
        ]
        target = os.path.join(projects, folder)
        side = transcript[: -len(".jsonl")]
        if os.path.isdir(side) and not os.path.islink(side):
            sub, files = importer.tree(side)
            plan.units.append(
                importer.Unit(os.path.join(target, os.path.basename(side)), side, files, True, sub)
            )
        plan.units.append(
            importer.Unit(os.path.join(target, name), transcript, [importer.file_copy(transcript)])
        )
        version = _recorded_version(transcript)
        if version:
            plan.info["source_tool_version"] = version
        return plan
