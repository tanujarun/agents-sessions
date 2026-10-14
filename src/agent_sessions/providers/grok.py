"""Grok adapter. Formats: docs/FORMATS.md "Grok"."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar

from .. import blake3, importer
from ..fsutil import home, listdir_ro, subdirs
from ..model import Candidate, Session, Store
from . import register
from .base import (
    NotSupported,
    Provider,
    Turn,
    between,
    clean_title,
    iter_jsonl,
    load_json,
    mtime,
    open_ro,
    parse_ts,
    read_small,
    unreadable,
    warn,
)

if TYPE_CHECKING:
    from ..fsutil import StrPath
    from ..pathmap import PathMap

_SUMMARY = "summary.json"
_CHAT = "chat_history.jsonl"
_CWD = ".cwd"
_SUBAGENTS = "subagents"
_META = "meta.json"
_FIXED_PATHS = 3  # summary.json, chat_history.jsonl, <group>/.cwd
_SMALL_FILE = 16 * 1024 * 1024
# How deep a ``meta.json`` value is searched. The tool writes a flat object;
# a file from elsewhere can nest values without end.
_NEST_MAX = 32
_INJECTED = ("<user_info>", "<system-reminder>")
# The group directory of a working directory (FORMATS "Phase 2 findings", Q1).
GROUP_NAME_MAX = 255  # bytes of the percent-encoded name; a longer one is a slug and a hash
_SLUG_MAX = 40  # characters of the slug that are kept
_SLUG_OTHER = re.compile(r"[^a-z0-9]+")  # every run of these is one "-" in a slug
_SLUG_EMPTY = "workspace"  # the slug of a name that leaves nothing
_HASH_DIGITS = 16  # hex digits of the BLAKE3 that follow the slug


def _load_json(path: StrPath) -> tuple[Any, str | None]:
    """``(value, None)``, or ``(None, reason)`` when the file is missing
    (reason ``"missing"``), empty, too large or not JSON."""
    try:
        with open_ro(path) as fh:
            raw = fh.read(_SMALL_FILE + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"cannot read: {exc.strerror or exc}"
    if not raw.strip():
        return None, "empty file"
    if len(raw) > _SMALL_FILE:
        return None, "file too large"
    try:
        return load_json(raw), None
    except (ValueError, RecursionError):
        return None, "not valid JSON"


def group_slug(cwd: str) -> str:
    """The slug that starts the group name of a long working directory: its
    last segment in lower case, every run of characters that are not
    ``a-z0-9`` as one ``-``, the ``-`` at both ends taken off, and then the
    first 40 characters (the cut is not trimmed again); ``workspace`` when
    nothing is left."""
    slug = _SLUG_OTHER.sub("-", os.path.basename(cwd).lower()).strip("-")
    return slug[:_SLUG_MAX] or _SLUG_EMPTY


def group_name(cwd: str) -> str:
    """The name of the directory under ``sessions/`` in which the tool keeps
    the sessions of the working directory ``cwd``, and the only one it lists
    them from (FORMATS "Phase 2 findings", Q1; seen in a sandbox).

    ``urllib.parse.quote(cwd, safe="")`` when that is at most 255 bytes:
    letters, digits and ``- . _ ~`` as they are, every other byte of the
    UTF-8 path as ``%XX`` in upper-case hex. Otherwise ``<slug>-<first 16
    hex digits of BLAKE3(cwd as UTF-8)>`` (``group_slug``).

    Raises ``NotSupported`` for what was never seen: an empty path, a path
    that is not valid UTF-8 text, and a path of more than 1,024 bytes (the
    BLAKE3 here hashes one chunk)."""
    try:
        raw = cwd.encode("utf-8")
    except UnicodeEncodeError:
        raise NotSupported(
            "the project folder's name is not valid UTF-8, and how Grok names its "
            f"directory for such a folder is not known (project folder {cwd})"
        ) from None
    if not raw:
        raise NotSupported("the project folder has no name to make Grok's directory name from")
    encoded = urllib.parse.quote(raw, safe="")
    if len(encoded) <= GROUP_NAME_MAX:  # ASCII: characters are bytes
        return encoded
    if len(raw) > blake3.MAX_INPUT:
        raise NotSupported(
            f"the project folder's path is {len(raw):,} bytes long; Grok's directory name "
            f"for it can be worked out for paths up to {blake3.MAX_INPUT:,} bytes only "
            f"(project folder {cwd})"
        )
    return f"{group_slug(cwd)}-{blake3.hexdigest(raw)[:_HASH_DIGITS]}"


class _SummaryRewrite(importer.Rewrite):
    """Sets ``info.cwd`` and ``grok_home`` in ``summary.json``; every other
    key keeps its value. The file is written in the source's own layout when
    that is one the tool is known to write (two-space indent, as every real
    summary is), else as compact JSON."""

    label = "info.cwd and grok_home in summary.json"

    def __init__(self, cwd: str, grok_home: str) -> None:
        super().__init__()
        self._cwd = cwd
        self._home = grok_home

    @staticmethod
    def _dump(value: object, pretty: bool) -> bytes:
        if pretty:
            return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def apply(self, reader: BinaryIO) -> Iterator[bytes]:
        self.changes = []
        raw = reader.read(_SMALL_FILE + 1)
        try:
            if len(raw) > _SMALL_FILE:
                raise ValueError("too large")
            text = raw.decode("utf-8")
            data, original = json.loads(text), json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise importer.ImportFailed(f"summary.json cannot be rewritten: {exc}") from exc
        if not isinstance(data, dict):
            raise importer.ImportFailed("summary.json cannot be rewritten: not a JSON object")
        pretty = self._dump(original, True) == raw
        info = data.get("info")
        if isinstance(info, dict) and "cwd" in info and info["cwd"] != self._cwd:
            self.changes.append(f"info.cwd: {info['cwd']} -> {self._cwd}")
            info["cwd"] = self._cwd
        if "grok_home" in data and data["grok_home"] != self._home:
            self.changes.append(f"grok_home: {data['grok_home']} -> {self._home}")
            data["grok_home"] = self._home
        out = self._dump(data, pretty)
        if not importer.only_changed(
            original, json.loads(out.decode("utf-8")), [("info", "cwd"), ("grok_home",)]
        ):
            raise importer.ImportFailed(
                "summary.json did not survive rewriting unchanged; nothing was imported"
            )
        yield out


def _text(content: object) -> str:
    """The text of a ``content`` value: a string, or the ``text`` parts of a
    list (image parts are left out)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def _is_prompt(record: dict[str, Any]) -> bool:
    """A real user prompt: a user record that has ``prompt_index`` and no
    ``synthetic_reason`` (FORMATS section 4)."""
    return (
        record.get("type") == "user"
        and record.get("prompt_index") is not None
        and not record.get("synthetic_reason")
    )


def _prompt_text(record: dict[str, Any]) -> str | None:
    """What the user typed in a user record, or None for injected context.

    The text sits inside ``<user_query>…</user_query>``, which can follow an
    ``<image_files>`` block or an interrupt notice. A prompt without the
    wrapper gives its whole text. A record that survived compaction keeps
    the wrapper but loses ``prompt_index``; it is still the user's text.
    """
    if record.get("type") != "user" or record.get("synthetic_reason"):
        return None
    text = _text(record.get("content"))
    wrapped = between(text, "<user_query>", "</user_query>")
    if wrapped is not None:
        return wrapped.strip()
    if not _is_prompt(record):
        return None
    text = text.strip()
    return None if text.startswith(_INJECTED) else text


def _names_session(value: object, session_id: str, key: str = "", depth: int = 0) -> bool:
    """True when a ``meta.json`` value holds ``session_id`` as a string,
    anywhere but under a key that names a parent, and no more than
    ``_NEST_MAX`` levels down."""
    if "parent" in key.lower():
        return False
    if isinstance(value, str):
        return value == session_id
    if depth >= _NEST_MAX:
        return False
    if isinstance(value, dict):
        return any(_names_session(v, session_id, str(k), depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return any(_names_session(v, session_id, key, depth + 1) for v in value)
    return False


def _is_child(session_id: str, session_dir: Path, meta_paths: Iterable[str]) -> bool:
    """True when another session's ``subagents/<child>/meta.json`` names
    ``session_id``, by its directory name or by a value inside the file."""
    for path in meta_paths:
        meta = Path(path)
        if meta.parent.parent.parent == session_dir:
            continue  # a session is not its own sub-agent
        if meta.parent.name == session_id:
            return True
        data, error = _load_json(meta)
        if error is None:
            if _names_session(data, session_id):
                return True
        elif error != "missing":
            warn(f"sub-agent meta.json skipped: {error}", meta)
    return False


@register
class Grok(Provider):
    name = "grok"
    label = "Grok"
    binary = "grok"
    app_id = "org.omarchy.grok"
    resumable = True
    dir_names: ClassVar[set[str]] = {".grok"}

    def native_store(self) -> Path:
        return home() / ".grok"

    # Same signature as Codex; the directory name decides.
    def is_store(self, path: Path) -> bool:
        return (path / "sessions").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        """One candidate per ``sessions/<group>/<name>/`` that holds a
        ``summary.json``. Directory listings only.

        ``paths`` = the session's ``summary.json`` and ``chat_history.jsonl``,
        the group's ``.cwd``, and every other session's
        ``subagents/*/meta.json`` in the store: whether a session is
        somebody's sub-agent is decided in its own ``parse`` from those
        files, so the answer does not depend on the order candidates are
        parsed in, and a new or changed ``meta.json`` invalidates the cache.
        """
        sessions = Path(store.root) / "sessions"
        found: list[tuple[str, str, Path]] = []  # (group, name, dir)
        metas: list[tuple[Path, str]] = []  # (owning session dir, meta path)
        try:
            groups = sorted(subdirs(sessions))
        except OSError as exc:
            unreadable(sessions, exc)
            groups = []
        for group in groups:
            try:
                names = sorted(subdirs(sessions / group))
            except OSError as exc:
                unreadable(sessions / group, exc)
                continue
            for name in names:
                session_dir = sessions / group / name
                try:
                    entries = set(listdir_ro(session_dir))
                except OSError as exc:
                    unreadable(session_dir, exc)
                    continue
                if _SUMMARY not in entries:
                    continue
                found.append((group, name, session_dir))
                if _SUBAGENTS not in entries:
                    continue
                try:
                    children = sorted(subdirs(session_dir / _SUBAGENTS))
                except OSError as exc:
                    unreadable(session_dir / _SUBAGENTS, exc)
                    continue
                metas += [
                    (session_dir, str(session_dir / _SUBAGENTS / child / _META))
                    for child in children
                ]
        seen: dict[str, int] = {}
        for _group, name, _dir in found:
            seen[name] = seen.get(name, 0) + 1
        out = []
        for group, name, session_dir in found:
            # The directory name is the session id; the same id under two
            # groups needs the group to stay unique.
            hint = name if seen[name] == 1 else f"{group}/{name}"
            paths = [
                str(session_dir / _SUMMARY),
                str(session_dir / _CHAT),
                str(session_dir.parent / _CWD),
            ]
            paths += [meta for owner, meta in metas if owner != session_dir]
            out.append(Candidate(hint, paths))
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
        summary_path = Path(c.paths[0])
        session_dir = summary_path.parent
        summary, error = _load_json(summary_path)
        if error is not None:
            warn(f"summary.json skipped: {error}", summary_path)
            return None
        if not isinstance(summary, dict):
            warn("summary.json skipped: not a JSON object", summary_path)
            return None
        info = summary.get("info")
        if not isinstance(info, dict):
            info = {}
        ident = info.get("id")
        session_id = ident if isinstance(ident, str) and ident else session_dir.name
        if _is_child(session_id, session_dir, c.paths[_FIXED_PATHS:]):
            return None

        own_title = ""
        title_source = ""
        for key in ("session_summary", "generated_title"):
            own_title = clean_title(summary.get(key)) if isinstance(summary.get(key), str) else ""
            if own_title:
                title_source = key
                break

        chat_path = session_dir / _CHAT
        prompts = replies = 0
        first_prompt = ""
        messages: int | None
        if os.path.exists(chat_path):
            for record in iter_jsonl(chat_path):
                kind = record.get("type")
                if kind == "assistant":
                    replies += 1
                elif kind == "user":
                    if _is_prompt(record):
                        prompts += 1
                    if not first_prompt:
                        first_prompt = clean_title(_prompt_text(record))
            if prompts + replies == 0:
                return None  # empty session
            messages = prompts + replies
        else:
            # No chat file: an untitled session is an empty one; a titled one
            # was a conversation whose history is gone.
            if not own_title:
                return None
            warn("chat_history.jsonl is missing; message count taken from summary.json", chat_path)
            count = summary.get("num_chat_messages")
            messages = count if isinstance(count, int) and not isinstance(count, bool) else None

        if own_title:
            title = own_title
        elif first_prompt:
            title, title_source = first_prompt, "first prompt"
        else:
            title, title_source = clean_title(session_id[:8]), "id"

        cwd, cwd_source = info.get("cwd"), "info.cwd"
        if not isinstance(cwd, str) or not cwd:
            cwd, cwd_source = self._group_cwd(session_dir.parent)

        started = parse_ts(summary.get("created_at")) or mtime(session_dir)
        updated = (
            parse_ts(summary.get("updated_at"))
            or parse_ts(summary.get("last_active_at"))
            or mtime(summary_path)
        )
        return {
            "id": session_id,
            "title": title,
            "cwd": cwd,
            "started": started,
            "updated": updated,
            "messages": messages,
            "files": [str(session_dir)],
            "note": None,
            "sources": {"title": title_source, "cwd": cwd_source},
        }

    @staticmethod
    def _group_cwd(group_dir: Path) -> tuple[str | None, str]:
        """cwd fallbacks: the group's ``.cwd`` file, then the decoded group
        directory name."""
        raw = read_small(group_dir / _CWD, _SMALL_FILE)
        recorded = raw.decode("utf-8", "replace").strip() if raw else ""
        if recorded:
            return recorded, ".cwd"
        decoded = urllib.parse.unquote(group_dir.name)
        return (decoded or None), "group directory"

    def resume_argv(self, s: Session) -> list[str]:
        return [self.binary, "--resume", s.id]

    def new_session_argv(self, prompt: str) -> list[str]:
        return [self.binary, prompt]  # ``grok [PROMPT]``; never ``-p``

    def transcript(self, s: Session) -> Iterator[Turn]:
        """From ``chat_history.jsonl``, which records no times: ``ts`` is None."""
        if not s.files:
            return
        for record in iter_jsonl(Path(s.files[0]) / _CHAT):
            kind = record.get("type")
            if kind == "user":
                text = _prompt_text(record)
                if text:
                    yield Turn("user", text, None)
            elif kind == "assistant":
                text = _text(record.get("content")).strip()
                if text:
                    yield Turn("assistant", text, None)
                calls = record.get("tool_calls")
                for call in calls if isinstance(calls, list) else []:
                    name = call.get("name") if isinstance(call, dict) else None
                    if isinstance(name, str) and name:
                        yield Turn("tool", name, None)
            elif kind == "backend_tool_call":
                detail = record.get("kind")
                name = detail.get("tool_type") if isinstance(detail, dict) else None
                if isinstance(name, str) and name:
                    yield Turn("tool", name, None)

    def import_plan(
        self,
        s: Session,
        pathmap: PathMap,
        target: str | None = None,
        experimental: bool = False,
    ) -> importer.ImportPlan:
        """The session directory without ``*.lock``, into the group directory
        of the mapped cwd, with ``summary.json`` rewritten (FORMATS section
        6). The group directory is the one the tool itself would use
        (``group_name``); when the native store has none of that name but
        has one that decodes to the same folder, that one is used.

        With a chosen folder (``target``) both are made for its real path
        (``os.path.realpath``): the tool goes by the physical working
        directory, so a session filed under the name of a symbolic link
        would not be listed (FORMATS Q1, Q8)."""
        if not s.files:
            raise NotSupported("this session has no directory to copy")
        source = s.files[0]
        # The directory's name becomes a name in the native store. The tool
        # names a session directory after the session's id; one that is named
        # otherwise is not copied under either name.
        name = os.path.basename(os.path.normpath(source))
        if name != s.id or not self.valid_id(name):
            raise NotSupported(f"the session directory is not named after its session id: {source}")
        plan = importer.start_plan(s, self.native_store(), pathmap, self.id_pattern, target)
        if plan.chosen_folder is not None:
            plan.cwd_local = os.path.realpath(plan.chosen_folder)
        cwd = plan.cwd_local or ""
        sessions = os.path.join(plan.native_root, "sessions")
        try:
            groups = sorted(subdirs(sessions))
        except OSError:
            groups = []
        group = group_name(cwd)
        if group not in groups:
            group = next((g for g in groups if urllib.parse.unquote(g) == cwd), group)
        plan.existing = sorted(
            {
                path
                for path in (os.path.join(sessions, g, n) for g in groups for n in {name, s.id})
                if os.path.lexists(path)
            }
        )
        dirs, files = importer.tree(source, skip=lambda entry: entry.endswith(".lock"))
        for item in files:
            if item.rel == _SUMMARY:
                item.rewrite = _SummaryRewrite(cwd, plan.native_root)
        files.sort(key=lambda item: (item.rel == _SUMMARY, item.rel))  # summary.json last
        plan.units.append(
            importer.Unit(os.path.join(sessions, group, name), source, files, True, dirs)
        )
        return plan
