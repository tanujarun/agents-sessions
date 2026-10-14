"""The facade the CLI and the TUI call: load, scan, list, status, verify,
find, open, import, unimport, transcript, export, continue, and the drives:
volumes, mount, forget, the drive letters.

What is worth a line in the log is written here, so both front ends log
alike (``logs``): the summary and the warnings of a scan, an import or an
undo and how it ended, a launch or a continue with the exact command, a
mount, a drive that is forgotten, and any exception that is not one of the
failures this module reports (``ApiError``), with its traceback.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from . import discover, fsutil, importer, launch, logs, pathmap
from . import export as exporting
from . import index as index_mod
from . import mounts as mounts_mod
from . import providers as registry
from . import unimport as undoing
from .errors import (
    REMEDY_EXPERIMENTAL,
    REMEDY_FOLDER,
    REMEDY_FORCE,
    REMEDY_KEYS,
    REMEDY_OVERWRITE,
    ApiError,
    ExportFailed,
    ImportFailed,
    LaunchFailed,
    MountFailed,
    NotFound,
    Offline,
    UndoFailed,
)
from .fsutil import cache_file, home, log_file
from .importer import NoFolder
from .index import Index
from .launch import ContinuePlan, LaunchPlan
from .model import Session, make_key
from .mounts import NATIVE_LABEL, MountsError, list_mounts
from .providers import all_providers
from .providers.base import ExperimentalOnly, NotSupported, Provider, Turn, parse_ts

if TYPE_CHECKING:
    import threading

# Agent CLIs that may be installed but have no adapter (SPEC non-goals).
NO_READER_TOOLS = ("copilot", "crush", "cursor-agent", "opencode")
EXPORT_FORMATS = exporting.FORMATS  # what ``export_transcript`` takes as ``fmt``
# Why an undo left a file (``UnimportReport.left``) that a forced one removes.
LEFT_FOR_A_FORCED_UNDO = undoing.EXTRA
HANDOFF_MAX_AGE = 30 * 24 * 3600  # seconds; an older handoff file goes with the next continue

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _logged(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Write an exception that nobody planned for to the log, with its
    traceback, and let it go on. The failures a caller is told in plain
    words (``ApiError``, ``MountsError``) are not such exceptions."""

    @functools.wraps(function)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return function(*args, **kwargs)
        except (ApiError, MountsError):
            raise
        except Exception as exc:
            logs.unexpected(exc, function.__name__)
            raise

    return wrapper


@_logged
def load_index(offline: bool = True) -> Index:
    """The cached index, finalised against the mounts present now. Reads no
    store; ``index.sessions`` is the listing. The sessions of a remembered
    store on a drive that is not mounted are in it, marked ``offline``;
    ``offline=False`` gives the listing of the mounted drives alone."""
    index = Index.load()
    index.finalise(all_providers(), list_mounts(), offline)
    return index


@_logged
def scan(
    deep: bool | None = None,
    progress: discover.Progress | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Refresh the index and return a summary.

    ``deep=True`` walks every mount, ``False`` only checks the known store
    locations, ``None`` walks when the mounts changed since the last deep pass
    or that pass is older than 24 h. ``progress(mount_label, dirs_seen)`` is
    called during a walk; ``cancel`` is a ``threading.Event``.
    """
    log = logs.logger()
    summary = Index.load().scan(all_providers(), list_mounts(), deep, progress, cancel)
    log.info(
        "scan %s%s: %.2f s, %d mounts, %d stores, %d candidates (%d parsed, %d from cache), "
        "%d sessions (%d on drives not mounted), %d warnings",
        summary["mode"],
        " (cancelled)" if summary["cancelled"] else "",
        summary["seconds"],
        len(summary["mounts"]),
        summary["stores"],
        summary["candidates"],
        summary["parsed"],
        summary["cached"],
        summary["sessions"],
        summary["offline"],
        len(summary["warnings"]),
    )
    # Here and not in ``load_index``: once per scan, not once per listing.
    for warning in summary["warnings"]:
        log.warning("scan: %s", warning)
    return summary


def list_sessions(provider: str | None = None, index: Index | None = None) -> list[Session]:
    """Listed sessions, newest ``updated`` first, ties by key. ``provider``
    is a provider name; an unknown one raises ValueError."""
    if provider is not None and provider not in {p.name for p in all_providers()}:
        raise ValueError(f"unknown provider: {provider}")
    index = index if index is not None else load_index()
    return [s for s in index.sessions if provider is None or s.provider == provider]


@_logged
def status(index: Index | None = None) -> dict:
    """Mounts, remembered volumes, stores and counts, drive letters, skipped
    stores, the agent tools and whether each is installed, warnings, where
    the cache and the log are. Every list is sorted.

    "Installed" is ``launch.tool_state``: a command on PATH that would only
    install the tool does not count."""
    index = index if index is not None else load_index()
    present = {m.path for m in index.current_mounts}
    remembered = volumes(index)
    mounts = [
        {
            "path": m.path,
            "label": m.label,
            "fstype": m.fstype,
            "device": m.device,
            "uuid": m.uuid,
            "fs_label": m.fs_label,
            "mounted": True,
            "state": mounts_mod.MOUNTED,
            "letter": index.letter_for_mount(m.path),
            "walk": index.walk.get(m.path),
        }
        for m in index.current_mounts
    ]
    mounts += [
        {
            "path": volume["path"],
            "label": volume["label"],
            "fstype": volume["fstype"],
            "device": "",
            "uuid": volume["uuid"],
            "fs_label": volume["fs_label"],
            "mounted": False,
            "state": volume["state"],
            "letter": volume["letter"],
            "walk": None,
        }
        for volume in remembered
        if volume["path"] not in present
    ]
    providers = []
    readers = set()
    for p in all_providers():
        every = [r for r in index.store_rows if r["provider"] == p.name]
        rows = [r for r in every if r["present"]]
        state = launch.tool_state(p.binary)
        readers.add(p.binary)
        providers.append(
            {
                "name": p.name,
                "label": p.label,
                "binary": p.binary,
                "installed": state.path if state.installed else None,
                "tool": state.detail,
                "resumable": bool(p.resumable),
                "reader": all(r["reader"] for r in rows),
                "stores": len(rows),
                "sessions": sum(r["listed"] for r in every),
                "offline": sum(r["listed"] for r in every if not r["present"]),
            }
        )
    tools = []
    for name in sorted(readers | set(NO_READER_TOOLS)):
        state = launch.tool_state(name)
        tools.append(
            {
                "tool": name,
                "path": state.path,
                "installed": state.installed,
                "wrapper": state.wrapper,
                "detail": state.detail,
                "reader": name in readers,
            }
        )
    return {
        "cache": str(index.path),
        "log": str(log_file()),
        "last_quick": index.last_quick,
        "last_deep": index.last_deep,
        "mounts": mounts,
        "volumes": remembered,
        "providers": providers,
        "stores": index.store_rows,
        "skipped_stores": [
            {
                "provider": s.provider,
                "root": s.root,
                "reason": s.reason,
                "mounted": s.mount in present,
            }
            for s in index.skipped
        ],
        "letters": index.pathmap.letters(),
        "unresolved_letters": index.pathmap.unresolved(),
        "tools": tools,
        "no_reader": [
            {"tool": t["tool"], "path": t["path"]}
            for t in tools
            if t["installed"] and not t["reader"]
        ],
        "warnings": index.warnings,
    }


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _epoch_ns(moment: str | None) -> int | None:
    """An ISO time as nanoseconds since 1970, the unit of a file's
    ``st_mtime_ns``; None when ``moment`` is not a time."""
    stamp = parse_ts(moment)
    if stamp is None:
        return None
    then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (then - _EPOCH) // timedelta(microseconds=1) * 1000


def _newer_than(sig: list, moment: str | None) -> bool:
    """True when a file of the signature is missing or was modified at or
    after ``moment`` (an ISO time), and when ``moment`` is not a time at
    all: then nothing can be called older than it.

    The two are compared as numbers. A file's time is whatever the
    filesystem says, 1970 or the year 3000 included, and has no reading as a
    session time (``parse_ts`` gives none outside 1990 to 2100)."""
    limit = _epoch_ns(moment)
    if limit is None:
        return True
    return any(mtime_ns is None or mtime_ns >= limit for _path, mtime_ns, _size in sig)


def _moved_on(sig: list) -> bool:
    """True when the files of a recorded signature are different now."""
    return index_mod.signature([row[0] for row in sig]) != sig


@_logged
def verify(
    progress: discover.Progress | None = None, cancel: threading.Event | None = None
) -> dict:
    """Compare the index with a slow, independent look at the disks. Only
    the mounted volumes are compared.

    Every mount is walked with no prune list and no cache, every candidate of
    every store found is parsed afresh, and the result is compared with the
    index: stores, skipped stores, cached parses, listed sessions.
    ``differences`` are faults (``ok`` is False when there is one). ``stale``
    lists what changed on disk after the last scan, which is not a fault of
    the index. ``nested`` lists stores inside another store, which are not
    listed by design.
    """
    started = time.monotonic()
    providers = all_providers()
    mounts = list_mounts()
    # What is on the mounted volumes, and nothing else: a session listed
    # from the cache because its drive is away has no disk to be held against.
    index = load_index(offline=False)
    found = discover.brute_pass(mounts, providers, progress, cancel)
    result = {
        "ok": False,
        "cancelled": found.cancelled,
        "differences": [],
        "stale": [],
        "nested": [],
        "stores": 0,
        "sessions": 0,
        "walk": {path: stats.to_dict() for path, stats in found.stats.items()},
        "seconds": 0.0,
    }
    if found.cancelled:
        result["seconds"] = round(time.monotonic() - started, 3)
        return result
    nested = {(s.provider, s.root) for s in discover.nested_stores(found.stores)}
    found.stores = [s for s in found.stores if (s.provider, s.root) not in nested]
    truth = index_mod.fresh(providers, mounts, found)
    differences: list[str] = []
    stale: list[str] = []

    present = {m.path for m in mounts}
    mine = {(r["provider"], r["root"]) for r in index.store_rows if r["present"]}
    theirs = {(s.provider, s.root) for s in truth.stores}
    for provider, root in sorted(theirs - mine):
        differences.append(f"store on disk but not in the index: {provider} {root}")
    for provider, root in sorted(mine - theirs):
        differences.append(f"store in the index but not on disk: {provider} {root}")
    mine_skipped = {(s.provider, s.root) for s in index.skipped if s.mount in present}
    theirs_skipped = {(s.provider, s.root) for s in truth.skipped}
    for provider, root in sorted(theirs_skipped - mine_skipped):
        differences.append(f"skipped store on disk but not in the index: {provider} {root}")
    for provider, root in sorted(mine_skipped - theirs_skipped):
        differences.append(f"skipped store in the index but not on disk: {provider} {root}")

    # Cached parse against fresh parse, candidate by candidate.
    stale_keys: set[str] = set()
    for provider, root in sorted(mine & theirs):
        key = f"{provider}:{root}"
        cached = (index.parsed.get(key) or {}).get("candidates", {})
        current = (truth.parsed.get(key) or {}).get("candidates", {})
        for hint in sorted(set(cached) | set(current)):
            old, new = cached.get(hint), current.get(hint)
            if old is not None and new is not None and old["sig"] == new["sig"]:
                if old["session"] != new["session"]:
                    differences.append(f"{root}: {hint}: cached parse differs from a fresh parse")
                continue
            if old is not None and new is not None:
                stale.append(f"{root}: {hint}: changed since the last scan")
            elif old is None and _newer_than(new["sig"], index.last_quick):
                stale.append(f"{root}: {hint}: new since the last scan")
            elif new is None and _moved_on(old["sig"]):
                stale.append(f"{root}: {hint}: gone since the last scan")
            elif old is None:
                differences.append(f"{root}: {hint}: candidate on disk but not in the index")
                continue
            else:
                differences.append(f"{root}: {hint}: candidate in the index but not on disk")
                continue
            for entry in (old, new):
                if entry is not None and entry["session"] is not None:
                    stale_keys.add(make_key(provider, root, entry["session"]["id"]))

    # The derived listing.
    listed = {s.key: s.to_dict() for s in index.sessions}
    wanted = {s.key: s.to_dict() for s in truth.sessions}
    for key in sorted(set(listed) | set(wanted)):
        if key in stale_keys:
            continue
        if key not in listed:
            differences.append(f"session on disk but not listed: {key}")
        elif key not in wanted:
            differences.append(f"session listed but not on disk: {key}")
        else:
            for name in sorted(listed[key]):
                if listed[key][name] != wanted[key][name]:
                    differences.append(
                        f"session {key}: {name} is {listed[key][name]!r} in the index, "
                        f"{wanted[key][name]!r} on disk"
                    )

    result.update(
        ok=not differences,
        differences=differences,
        stale=stale,
        nested=sorted(f"{provider} {root}" for provider, root in nested),
        stores=len(theirs),
        sessions=len(truth.sessions),
        seconds=round(time.monotonic() - started, 3),
    )
    return result


# --- one session: find, show, open, import -----------------------------------

MATCHES_SHOWN = 8  # keys named in an "ambiguous" message


@_logged
def find_session(ref: Session | str, index: Index | None = None) -> Session:
    """The session ``ref`` names: a full key, a key prefix that only one
    session has, or a bare session id. A ``Session`` is returned as it is.

    A foreign session and its imported copy share an id; a bare id then means
    the native copy. Raises ``NotFound`` when nothing matches, and when
    several sessions do (the reason names them).
    """
    if isinstance(ref, Session):
        return ref
    text = ref.strip() if isinstance(ref, str) else ""
    if not text:
        raise NotFound("no session key given")
    index = index if index is not None else load_index()
    for session in index.sessions:
        if session.key == text:
            return session
    matches = [s for s in index.sessions if s.key.startswith(text) or s.id == text]
    if len(matches) > 1:
        native = [s for s in matches if s.origin == "native"]
        if len(native) == 1 and all(
            s is native[0] or s.imported_as == native[0].key for s in matches
        ):
            return native[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotFound(f"no session matches {text!r}", remedy=REMEDY_KEYS)
    keys = sorted(s.key for s in matches)
    shown = ", ".join(keys[:MATCHES_SHOWN])
    more = f", and {len(keys) - MATCHES_SHOWN} more" if len(keys) > MATCHES_SHOWN else ""
    raise NotFound(f"{text!r} matches {len(keys)} sessions: {shown}{more}")


def _provider(session: Session) -> Provider:
    try:
        return registry.get(session.provider)
    except KeyError:
        raise NotFound(f"no reader for provider {session.provider!r}") from None


def _need_files(session: Session) -> None:
    """Raise ``Offline`` for a session that is listed from the cache alone:
    what the caller is about to do reads its files."""
    if session.offline:
        raise Offline(f"the drive {session.mount_label} is not mounted")


def _native_copy(session: Session, index: Index) -> Session | None:
    """The native session with the same provider and id, as the index has it.

    For a foreign session that is its imported copy. A native session is its
    own copy only when a foreign session of that id is listed too: then it
    was imported (a bare id names the native side of such a pair)."""
    wanted: str | None = "native" if session.origin != "native" else "foreign"
    found = None
    for other in index.sessions:
        if other.provider == session.provider and other.id == session.id:
            if other.origin == "native":
                found = other
            if other.origin == wanted:
                wanted = None
    return found if wanted is None else None


@_logged
def transcript(ref: Session | str, index: Index | None = None) -> Iterator[Turn]:
    """The conversation as ``Turn(role, text, ts)``, oldest first: ``user``
    and ``assistant`` text, and one ``tool`` turn per tool call whose text is
    the tool's name. Read lazily. Raises ``NotFound``, and ``Offline`` at
    once for a session whose drive is not mounted."""
    session = find_session(ref, index)
    _need_files(session)
    return iter(_provider(session).transcript(session))


@_logged
def export_transcript(
    ref: Session | str, fmt: str = exporting.MARKDOWN, index: Index | None = None
) -> Iterator[str]:
    """The transcript of a session as a document, made lazily (it can be
    large): pieces of one or more whole lines, each without the line break
    that ends it.

    ``fmt="md"``: Markdown. A header (the title, then the tool, the session
    id, the folder as recorded, the times, the message count and what
    exported it), a ``## User`` or ``## Assistant`` section per turn and a
    line ``> tool: <name>`` per tool call; all of it text made safe to show
    (``textsafe.clean``). ``fmt="json"``: one JSON object per turn and line
    (``role``, ``text``, ``ts``), exact data (docs/JSON.md).

    Raises at once, before anything is read: ``NotFound``, ``Offline`` for a
    session whose drive is not mounted, ``ExportFailed`` for a format that
    is neither."""
    if fmt not in exporting.FORMATS:
        raise ExportFailed(
            f"unknown export format {fmt!r}; it is one of: {', '.join(exporting.FORMATS)}"
        )
    session = find_session(ref, index)
    _need_files(session)
    provider = _provider(session)
    turns = provider.transcript(session)
    if fmt == exporting.JSON_LINES:
        return exporting.json_lines(turns)
    return exporting.markdown(session, provider.label or provider.name, turns)


def _in_a_store(path: str, index: Index) -> str | None:
    """The root of the session store ``path`` lies in, or None. Whatever
    links lead there: both are compared as the places they really are."""
    real = os.path.realpath(path)
    roots = [store.root for store in index.stores] + [store.root for store in index.skipped]
    for root in sorted(roots):
        there = os.path.realpath(root)
        if real == there or real.startswith(there.rstrip(os.sep) + os.sep):
            return root
    return None


@_logged
def export_file(
    ref: Session | str,
    path: str,
    fmt: str = exporting.MARKDOWN,
    force: bool = False,
    index: Index | None = None,
) -> int:
    """Write ``export_transcript`` to the file ``path`` and return its size
    in bytes. The file is the user's own choice: an absolute path in a
    directory that exists. It is written whole or not at all (a temp file
    beside it, then moved into place), for the user alone (0600).

    Raises ``ExportFailed`` when the file exists and ``force`` is not set
    (with it, a regular file is replaced), for a path that will not do, and
    for one inside a session store, native or not, forced or not: nothing
    here writes into a store. And what ``export_transcript`` raises."""
    index = index if index is not None else load_index()
    pieces = export_transcript(ref, fmt, index)
    if isinstance(path, str) and path and "\0" not in path and os.path.isabs(path):
        store = _in_a_store(path, index)
        if store is not None:
            raise ExportFailed(
                f"{os.path.normpath(path)} is inside the session store {store}; nothing was written"
            )
    return exporting.write(path, pieces, overwrite=force)


@_logged
def open_session(
    ref: Session | str, here: bool = False, dry_run: bool = False, index: Index | None = None
) -> LaunchPlan:
    """Resume a session in a new terminal window, in its project directory.

    A foreign session that was imported resumes its native copy, whether
    its drive is mounted or not; any other foreign session is refused.
    ``here=True`` starts the tool in this process instead (``chdir`` then
    ``exec``: the call does not return). ``dry_run=True`` only returns the
    plan. Raises ``LaunchFailed`` with the reason (a real failure also sends
    a desktop notification), ``Offline`` for a session that was not imported
    and whose drive is not mounted, or ``NotFound``. The window is opened by
    ``launch.starter``, which a caller may replace.
    """
    session = find_session(ref, index)
    where = None
    if session.origin != "native":
        index = index if index is not None else load_index()
        copy = _native_copy(session, index)  # imported: resume that one
        if copy is not None:
            session = copy
        elif session.offline:
            if not dry_run:
                logs.logger().warning("launch %s refused: its drive is not mounted", session.key)
            _need_files(session)
        else:
            where = index.where(session)
            if where == NATIVE_LABEL:  # another store on this machine's own disks
                where = session.store
    provider = _provider(session)
    if dry_run:
        return launch.launch(session, provider, where=where, here=here, dry_run=True)
    log = logs.logger()
    try:
        # Planned once more, for the log: with ``here`` the launch below
        # replaces this process and nothing could be written after it.
        planned = launch.plan(session, provider, where, here)
    except LaunchFailed:
        pass  # the launch below fails the same way; it notifies, and is logged there
    else:
        log.info(
            "launch %s%s: cd %s && %s",
            session.key,
            " (in this terminal)" if here else "",
            shlex.quote(planned.cwd),
            shlex.join(planned.command),
        )
    try:
        return launch.launch(session, provider, where=where, here=here)
    except LaunchFailed as exc:
        log.warning("launch %s refused: %s", session.key, exc.reason)
        raise


# --- continue as a new session ------------------------------------------------

NO_TOOL = "no tool that can start a new session is installed"


@dataclass(frozen=True)
class ContinueOptions:
    """What continuing one session as a new one can be (``continue_options``):
    what a front end builds its question from."""

    # ``continue_session`` can start a tool for this session.
    possible: bool
    # Why not, in plain words; "" when ``possible``.
    reason: str = ""
    # The tools that can be started, as ``(name, label)``: the session's own
    # first, then by name. ``name`` is what ``continue_session`` takes.
    tools: tuple[tuple[str, str], ...] = ()
    # The tool ``continue_session`` starts when none is named: the session's
    # own. None when that one is not installed: one must be named then.
    default_tool: str | None = None
    # The folder it starts in when none is given: the session's own folder
    # on this machine. None when that is gone or was not recorded: one must
    # be given then.
    folder: str | None = None


def _new_session_tools(first: str | None = None) -> list[Provider]:
    """The providers whose tool can be started as a new session here: the
    adapter has a command for it (``Provider.new_session_argv``) and the
    tool is installed, not merely a name on PATH (``launch.tool_state``).
    The provider called ``first`` leads, the others follow by name."""
    found = []
    for provider in all_providers():
        try:
            argv = provider.new_session_argv("")
        except NotImplementedError:
            continue
        if argv and launch.tool_state(str(argv[0])).installed:
            found.append(provider)
    return sorted(found, key=lambda provider: provider.name != first)  # stable: by name within


def _own_folder(session: Session) -> str | None:
    """The session's folder on this machine when it is there now, else None."""
    folder = session.cwd_local
    if folder and os.path.isabs(folder) and os.path.isdir(folder):
        return folder
    return None


@_logged
def continue_options(ref: Session | str, index: Index | None = None) -> ContinueOptions:
    """What ``continue_session`` can do for this session: whether it is
    ``possible`` (and the ``reason`` when not), the ``tools`` that are
    installed, the ``default_tool`` and the ``folder`` it would use when
    none is given. Never raises for a session that cannot be continued or
    found: that is an answer. Reads only; writes nothing."""
    try:
        session = find_session(ref, index)
        _need_files(session)
        source = _provider(session)
    except ApiError as exc:
        return ContinueOptions(False, exc.reason)
    installed = _new_session_tools(source.name)
    tools = tuple((provider.name, provider.label or provider.name) for provider in installed)
    folder = _own_folder(session)
    if not tools:
        return ContinueOptions(False, NO_TOOL, folder=folder)
    default = source.name if installed[0].name == source.name else None
    return ContinueOptions(True, "", tools, default, folder)


def _continue_tool(source: Provider, tool: str | None) -> Provider:
    """The provider whose tool continues a session of ``source``: the one
    ``tool`` names (an adapter's name, its command or its label, in either
    case), else ``source`` itself. Raises ``LaunchFailed`` for a name that
    is none, and for a tool that is not installed; both name the ones that
    are."""
    installed = _new_session_tools(source.name)
    names = ", ".join(sorted(provider.name for provider in installed))
    others = f"the installed tools are: {names}" if names else NO_TOOL
    wanted = source
    if tool is not None:
        text = tool.strip().lower() if isinstance(tool, str) else ""
        known = [
            provider
            for provider in all_providers()
            if text and text in (provider.name, provider.binary, provider.label.lower())
        ]
        if len(known) != 1:
            raise LaunchFailed(f"unknown tool {tool!r}; {others}")
        wanted = known[0]
    if wanted.name not in {provider.name for provider in installed}:
        raise LaunchFailed(f"{wanted.label or wanted.name} is not installed; {others}")
    return wanted


def _sweep_handoff(now: float | None = None) -> list[str]:
    """Remove the handoff files that are older than ``HANDOFF_MAX_AGE``;
    returns the paths it removed, sorted.

    Only a regular file whose name is exactly what ``launch.handoff_file``
    gives a session of a provider that has a reader. Anything else in the
    directory is not this program's to judge and stays; what cannot be
    listed or removed is left alone."""
    directory = fsutil.handoff_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    providers = all_providers()
    limit = (time.time() if now is None else now) - HANDOFF_MAX_AGE
    removed = []
    for name in names:
        if not any(launch.handoff_session(provider, name) for provider in providers):
            continue
        path = os.path.join(directory, name)
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_mtime >= limit:
                continue
            os.unlink(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def _hand_over(session: Session, source: Provider, plan: ContinuePlan) -> int:
    """Write the transcript of ``session`` to the plan's handoff file, as
    Markdown, and return its size. The directory is made for the user alone
    (0700, the file 0600); the file replaces an older one of the same
    session in one step; files past their time are swept on the way."""
    directory = fsutil.handoff_dir()
    try:
        fsutil.make_private_dir(fsutil.state_dir())
        fsutil.make_private_dir(directory)
    except OSError as exc:
        raise ExportFailed(
            f"the directory {directory} cannot be made: {exc.strerror or exc}; nothing was started"
        ) from exc
    swept = _sweep_handoff()
    if swept:
        logs.logger().info(
            "continue: %d transcripts older than %d days removed from %s",
            len(swept),
            HANDOFF_MAX_AGE // 86400,
            directory,
        )
    pieces = exporting.markdown(session, source.label or source.name, source.transcript(session))
    return exporting.write(plan.handoff, pieces, overwrite=True)


@_logged
def continue_session(
    ref: Session | str,
    tool: str | None = None,
    folder: str | None = None,
    here: bool = False,
    dry_run: bool = False,
    index: Index | None = None,
) -> ContinuePlan:
    """Carry a session on as a **new** session of a tool: for one that
    cannot be resumed where it is wanted (another provider's tool, a
    provider that cannot be imported, a folder that is gone).

    The transcript is exported as Markdown to ``<state>/handoff/
    <provider>-<id>.md``, and ``tool`` (an adapter's name, its command or
    its label; default: the session's own) is started as a new interactive
    session in ``folder`` (default: the session's folder on this machine)
    with a fixed first prompt that names the file and says it is a record,
    not instructions. The prompt holds no text of the session
    (``launch.plan_new`` makes it). **The tool sends that first message to
    its model as soon as it starts**: a front end says so before it calls
    this.

    Returns the plan: the folder, the command, the prompt and the handoff
    file. ``dry_run=True`` only returns it: nothing is written, nothing is
    started. ``here=True`` starts the tool in this process instead of a new
    window (``chdir`` then ``exec``: the call does not return).

    Raises ``Offline`` for a session whose drive is not mounted,
    ``LaunchFailed`` for a tool that is unknown or not installed (the reason
    names the installed ones), for a session whose folder is gone when none
    is given, for a ``folder`` that is not an absolute path of a directory
    that exists, and for whatever stops the start itself; ``ExportFailed``
    when the transcript cannot be written; ``NotFound``. Nothing is started
    unless the transcript was written.
    """
    session = find_session(ref, index)
    log = logs.logger()
    try:
        _need_files(session)
        source = _provider(session)
        target = _continue_tool(source, tool)
        if folder is None:
            folder = _own_folder(session)
            if folder is None:
                state = "is gone" if session.cwd else "was not recorded"
                raise LaunchFailed(
                    f"the session's folder {state}; choose a folder to start in",
                    remedy=REMEDY_FOLDER,
                )
        planned = launch.plan_new(target, source, session.id, folder, here)
        if dry_run:
            return planned
        size = _hand_over(session, source, planned)
    except ApiError as exc:
        if not dry_run:
            log.warning("continue %s refused: %s", session.key, exc.reason)
        raise
    # Logged before the start: with ``here`` the launch below replaces this
    # process and nothing could be written after it.
    log.info(
        "continue %s with %s%s: transcript in %s (%d bytes); cd %s && %s",
        session.key,
        target.name,
        " (in this terminal)" if here else "",
        planned.handoff,
        size,
        shlex.quote(planned.cwd),
        shlex.join(planned.command),
    )
    try:
        return launch.launch_new(target, source, session.id, folder, here)
    except LaunchFailed as exc:
        log.warning("continue %s refused: %s", session.key, exc.reason)
        raise


@dataclass
class ImportReport:
    session: Session  # the native copy, as indexed
    created: bool  # False: it was already there, nothing was copied
    files: list[str] = field(default_factory=list)  # what this import put in place
    bytes: int = 0


def _plan(
    session: Session, index: Index, target: str | None = None, experimental: bool = False
) -> importer.ImportPlan:
    """The import plan for a foreign session, into the folder ``target``
    when one is given. Raises ``ImportFailed``, and ``Offline`` when its
    drive is not mounted."""
    if session.origin == "native":
        raise ImportFailed(f"this session is already on {NATIVE_LABEL}")
    _need_files(session)
    try:
        return _provider(session).import_plan(session, index.pathmap, target, experimental)
    except ExperimentalOnly as exc:
        raise ImportFailed(exc.reason, remedy=REMEDY_EXPERIMENTAL) from None
    except NotSupported as exc:
        raise ImportFailed(exc.reason) from None
    except NotFound as exc:
        raise ImportFailed(exc.reason) from None


@_logged
def can_import(
    session: Session | str,
    index: Index | None = None,
    target: str | None = None,
    experimental: bool = False,
) -> tuple[bool, str]:
    """``(True, "")`` when ``import_session`` would copy this session, else
    ``(False, reason)``: it is native, already imported, its provider cannot
    be imported, or only as an experimental import, its drive is not
    mounted, its project folder is not on any mounted drive, its id is
    already in the native store, or there is too little free space.
    ``target`` and ``experimental`` ask about an import with them. Reads
    only; writes nothing. ``import_options`` says more."""
    options = import_options(session, index, target)
    if options.possible or (experimental and options.experimental and not options.needs_folder):
        return True, ""
    return False, options.reason


@dataclass(frozen=True)
class ImportOptions:
    """What an import of one session can be (``import_options``): what a
    front end builds its prompt from."""

    # ``import_session`` would copy the session as it was asked about, with
    # no flag and no folder beyond the ``target`` that was given.
    possible: bool
    # Why not, in plain words; "" when ``possible``. With ``needs_folder``
    # or ``experimental`` it is what an import without them answers.
    reason: str = ""
    # It would work with a folder the user chooses (``target``): the
    # session's own folder is not on this machine, or was not recorded.
    needs_folder: bool = False
    # It works only as an experimental import (``experimental=True``).
    experimental: bool = False
    # What must be shown before an experimental import; "" otherwise.
    warning: str = ""


def _some_folder() -> str:
    """A directory that exists, to ask whether an import would work with a
    chosen folder before one is chosen: the home, else the root."""
    here = os.path.abspath(home())
    return here if os.path.isdir(here) else os.path.abspath(os.sep)


@_logged
def import_options(
    ref: Session | str, index: Index | None = None, target: str | None = None
) -> ImportOptions:
    """What an import of this session can be: possible as it is, possible
    with a chosen folder (``needs_folder``), possible only as an
    experimental import (``experimental``, with the ``warning`` to show
    first), or not at all, with the ``reason``. ``target`` asks about an
    import into that folder. Never raises for a session that cannot be
    imported or found: that is an answer. Reads only; writes nothing.

    ``needs_folder`` and ``experimental`` are set only when the import would
    then go through: the plan is made again with what it asked for (with a
    stand-in for the folder that is still to be chosen) and has to pass
    every check, free space included."""
    try:
        if target is not None:
            importer.chosen_folder(target)
        index = index if index is not None else load_index()
        session = find_session(ref, index)
        if _native_copy(session, index) is not None:
            return ImportOptions(False, "already imported")
        if session.origin == "native":
            raise ImportFailed(f"this session is already on {NATIVE_LABEL}")
        _need_files(session)
        provider = _provider(session)
    except ApiError as exc:
        return ImportOptions(False, exc.reason)
    reason = warning = ""  # of the import as it was asked about
    needs_folder = experimental = False
    folder = target
    while True:  # as it is; then with what it turned out to need
        try:
            importer.check(provider.import_plan(session, index.pathmap, folder, experimental))
        except ExperimentalOnly as exc:
            if experimental:  # asked for and still not enough: an adapter's fault
                return ImportOptions(False, exc.reason)
            experimental, warning = True, exc.warning
            reason = reason or exc.reason
        except importer.NoFolder as exc:
            if folder is not None:
                return ImportOptions(False, exc.reason)
            needs_folder, folder = True, _some_folder()
            reason = reason or exc.reason
        except (ApiError, NotSupported) as exc:
            return ImportOptions(False, exc.reason)
        else:
            possible = not (needs_folder or experimental)
            return ImportOptions(possible, reason, needs_folder, experimental, warning)


@_logged
def import_plan(
    ref: Session | str,
    index: Index | None = None,
    target: str | None = None,
    experimental: bool = False,
) -> dict:
    """What ``import_session`` would do, as plain data, for a dry run:
    ``already`` (the native key when it is imported already, else None),
    ``cwd`` (the folder as recorded), ``cwd_local`` (the folder the copy is
    made for), ``chosen_folder`` (``target`` when one was given, else None),
    ``copies``, ``rewrites`` (with the changes each would make), ``appends``,
    ``bytes``, ``needed``, ``free``. Writes nothing. Raises ``ImportFailed``
    for everything the import itself would refuse, a ``target`` that is not
    an absolute path of a directory included, ``Offline`` for a session
    whose drive is not mounted."""
    if target is not None:
        importer.chosen_folder(target)
    index = index if index is not None else load_index()
    session = find_session(ref, index)
    copy = _native_copy(session, index)
    if copy is not None:
        return {
            "provider": session.provider,
            "id": session.id,
            "source_key": session.key,
            "native_key": copy.key,
            "already": copy.key,
            "copies": [],
            "rewrites": [],
            "appends": [],
            "bytes": 0,
        }
    plan = _plan(session, index, target, experimental)
    importer.check(plan)
    return dict(importer.describe(plan), already=None)


@_logged
def import_report(
    ref: Session | str,
    progress: importer.Progress | None = None,
    index: Index | None = None,
    target: str | None = None,
    experimental: bool = False,
) -> ImportReport:
    """``import_session`` with the details: whether anything was copied (a
    session imported before is returned with ``created=False``) and which
    files were put in place."""
    index = index if index is not None else load_index()
    session = find_session(ref, index)
    log = logs.logger()
    log.info(
        "import %s (store %s)%s%s",
        session.key,
        session.store,
        f" into the chosen folder {target}" if target is not None else "",
        ", experimental imports allowed" if experimental else "",
    )
    try:
        if target is not None:
            importer.chosen_folder(target)
        report = _import(session, progress, index, target, experimental)
    except ApiError as exc:
        log.warning("import %s refused: %s", session.key, exc.reason)
        raise
    if report.created:
        log.info(
            "import %s: imported as %s, %d items, %d bytes: %s",
            session.key,
            report.session.key,
            len(report.files),
            report.bytes,
            ", ".join(report.files),
        )
    else:
        log.info("import %s: already imported as %s", session.key, report.session.key)
    return report


def _recover(provider: Provider) -> list[dict]:
    """Settle the imports into this provider's native store that began and
    never ended (``importer.recover``); returns the records written for
    them. When there are any, the index was scanned again: what is in the
    store changed.

    Before an import or an undo looks at the listing: a copy such an import
    left whole is listed as a native session without a record of an import,
    and one it left in part is in the way of the next import. Whoever holds
    the lock is not waited for here: the import or the undo that follows
    does the same as its first step, with the lock in its own hands. What
    was done is in the log (``importer`` writes it)."""
    native = provider.native_store()
    if native is None:
        return []
    logs.logger()  # set up, for what ``importer.recover`` has to say
    try:
        settled = importer.recover(native, provider.read_back, timeout=0)
    except OSError:  # the lock is held, or cannot be taken: said by what follows
        return []
    if settled:
        Index.load().scan(all_providers(), list_mounts(), deep=False)
    return settled


def _import(
    session: Session,
    progress: importer.Progress | None,
    index: Index,
    target: str | None = None,
    experimental: bool = False,
) -> ImportReport:
    """``import_report`` for a session that was found."""
    # First, so that the listing below is of a store with no import left
    # half done in it. Not for a session whose drive is away: that one is
    # answered from the listing alone, and no adapter is asked for anything.
    if not session.offline and _recover(_provider(session)):
        index = load_index()
    copy = _native_copy(session, index)
    if copy is not None:
        return ImportReport(copy, False)
    if session.origin == "native":
        raise ImportFailed(f"this session is already on {NATIVE_LABEL}")
    _need_files(session)
    provider = _provider(session)
    collision = None
    files: list[str] = []
    size = 0
    try:
        plan = provider.import_plan(session, index.pathmap, target, experimental)
        files, size = plan.destinations, plan.total_bytes
        provider.import_session(session, index.pathmap, progress, target, experimental)
    except importer.Exists as exc:
        collision = exc  # in the store but not in this index: look again below
    except ExperimentalOnly as exc:
        raise ImportFailed(exc.reason, remedy=REMEDY_EXPERIMENTAL) from None
    except NotSupported as exc:
        raise ImportFailed(exc.reason) from None
    fresh = Index.load()
    fresh.scan(all_providers(), list_mounts(), deep=False)
    copy = _native_copy(session, fresh)
    if collision is not None:
        if copy is None:
            raise collision
        return ImportReport(copy, False)
    if copy is None:
        raise ImportFailed(
            "the files were copied but the copy is not listed as a session: " + ", ".join(files)
        )
    return ImportReport(copy, True, files, size)


@_logged
def import_session(
    ref: Session | str,
    progress: importer.Progress | None = None,
    index: Index | None = None,
    target: str | None = None,
    experimental: bool = False,
) -> Session:
    """Copy a foreign session into the native store and return the native
    copy, indexed and launchable. A session that was imported before is
    returned as it is; nothing is copied twice and nothing is overwritten.

    ``target`` is a folder to import into in place of the one the session
    recorded: an absolute path of a directory that exists. It is the only
    way to import a session whose own folder is gone or was never recorded.
    What it changes in the copy depends on the provider (FORMATS "Phase 2
    findings", Q8); the import record keeps both folders.

    ``experimental=True`` allows an import that no resume has proven yet
    (Antigravity: a plain copy, FORMATS Q5). Without it such a session is
    refused, and the reason says so. ``import_options`` has the warning a
    front end shows first.

    ``progress(done_bytes, total_bytes)`` is called while copying. Raises
    ``ImportFailed`` with the reason, also when the provider cannot be
    imported. A quick scan runs afterwards; an ``index`` passed in is not
    refreshed, load it again.
    """
    return import_report(ref, progress, index, target, experimental).session


@dataclass
class UnimportReport:
    """What ``unimport`` did, or with ``dry_run`` would do."""

    provider: str
    id: str
    native_key: str | None  # of the copy, as its import record has it
    source_key: str | None  # of the session it was imported from
    removed: list[str] = field(default_factory=list)  # files, then directories deepest first
    files: int = 0  # how many of ``removed`` are files; the rest are directories
    left: list[tuple[str, str]] = field(default_factory=list)  # (path, why), by path
    differences: list[str] = field(default_factory=list)  # from the record: a forced undo
    forced: bool = False
    dry_run: bool = False
    bytes: int = 0  # of the files the undo set out to remove


def _undo_target(ref: Session | str, index: Index) -> tuple[str, str]:
    """The provider and session id ``ref`` names for an undo: those of a
    listed session (``find_session``: a key, a start of one, an id), else of
    the import record that still stands whose native key, source key or id
    ``ref`` is. The second way finds a copy whose session is not listed any
    more (its files were removed by hand, its drive forgotten)."""
    if isinstance(ref, Session):
        return ref.provider, ref.id
    try:
        session = find_session(ref, index)
    except NotFound as missing:
        text = ref.strip() if isinstance(ref, str) else ""
        named = sorted(
            {
                (record["provider"], record["id"])
                for record in undoing.not_undone(undoing.read_records())
                if text
                and text in (record.get("native_key"), record.get("source_key"), record.get("id"))
            }
        )
        if len(named) != 1:
            raise missing from None
        return named[0]
    return session.provider, session.id


@_logged
def unimport(
    ref: Session | str,
    force: bool = False,
    dry_run: bool = False,
    index: Index | None = None,
) -> UnimportReport:
    """Take an import back: remove from the native store what the last
    import of this session created, and nothing else (``unimport``, the
    module, has the rules). ``ref`` is the native copy, the foreign session
    it came from, or the id.

    Refused (``UndoFailed``, nothing removed) when no import of the session
    is recorded (the exception's ``no_record`` is set), and when a file of
    the copy differs from what the record says of it: the session was used
    since (its ``differences`` name the files). ``force=True`` removes the copy
    all the same, together with what the tool made beside it since
    (``Provider.undo_extras``), and is the only way to undo an import
    recorded without a manifest.

    Directories go only when they are empty, and a line the import added to
    a file of the tool's stays: ``left`` says what was left and why. With
    ``dry_run=True`` nothing is touched and the report is what would be
    done. Otherwise the undo is recorded in the journal and a quick scan
    runs afterwards (an ``index`` passed in is not refreshed). Raises
    ``NotFound`` for a reference that names nothing."""
    index = index if index is not None else load_index()
    name, session_id = _undo_target(ref, index)
    try:
        provider = registry.get(name)
    except KeyError:
        raise NotFound(f"no reader for provider {name!r}") from None
    native = provider.native_store()
    if native is None or not provider.valid_id(session_id):
        raise UndoFailed(undoing.NO_RECORD, no_record=True)
    log = logs.logger()
    settled: list[dict] = []
    if not dry_run:
        log.info("unimport %s %s from %s%s", name, session_id, native, " (forced)" if force else "")
        settled = _recover(provider)
    try:
        made, record = undoing.undo(
            name,
            session_id,
            native,
            provider.undo_extras(session_id, native),
            force,
            dry_run,
            provider.read_back,
        )
    except UndoFailed as exc:
        if exc.no_record and any(
            record.get("record") == "rollback"
            and (record.get("provider"), record.get("id")) == (name, session_id)
            for record in settled
        ):
            # What there was to take back was the start of an import, and
            # it went just now (``_recover``): said as the engine says it.
            exc = UndoFailed(undoing.TAKEN_OUT, no_record=True)
        if not dry_run:
            log.warning("unimport %s %s refused: %s", name, session_id, exc.reason)
        raise exc from None
    keys = [made.record.get(key) for key in ("native_key", "source_key")]
    native_key, source_key = (key if isinstance(key, str) else None for key in keys)
    report = UnimportReport(
        provider=name,
        id=session_id,
        native_key=native_key,
        source_key=source_key,
        removed=made.files + made.dirs,
        files=len(made.files),
        left=list(made.left),
        differences=list(made.differences),
        forced=made.forced,
        dry_run=dry_run,
        bytes=made.bytes,
    )
    if record is not None:
        log.info(
            "unimport %s %s: removed %d files and directories, left %d: %s",
            name,
            session_id,
            len(report.removed),
            len(report.left),
            ", ".join(f"{path} ({why})" for path, why in report.left) or "nothing",
        )
        Index.load().scan(all_providers(), list_mounts(), deep=False)
    return report


# --- drives: volumes, mount, forget, drive letters ----------------------------

_LETTER = re.compile(r"[A-Za-z]:")  # a drive letter as a reference, with ``fullmatch``


def volumes(index: Index | None = None) -> list[dict]:
    """The volumes that hold a remembered store, in ascending order of
    ``path``: ``label`` (what a listing calls the drive), ``fs_label`` and
    ``uuid`` of the filesystem, ``fstype``, ``letter`` (the Windows drive
    letter it stands for, or None), ``state``, ``last_seen`` (when a scan
    last saw it mounted), ``stores`` and ``sessions`` (what is remembered
    and listed of it).

    ``state`` is ``mounted``, ``attached`` (the device is there, its
    filesystem is not mounted) or ``absent``; read from files, nothing is
    run. A volume whose ``uuid`` is not known and that is not mounted at its
    path counts as ``absent``: there is nothing to look for."""
    index = index if index is not None else load_index()
    present = {m.path for m in index.current_mounts}
    rows = []
    for path in sorted(index.volumes):
        volume = index.volumes[path]
        if path in present:
            state = mounts_mod.MOUNTED
        else:
            state = mounts_mod.volume_state(volume["uuid"])
        rows.append(
            {
                "path": path,
                "label": volume["label"],
                "fs_label": volume["fs_label"],
                "uuid": volume["uuid"],
                "fstype": volume["fstype"],
                "letter": index.letter_for_mount(path),
                "state": state,
                "last_seen": volume["last_seen"],
                "stores": sum(1 for row in index.store_rows if row["mount"] == path),
                "sessions": sum(1 for session in index.sessions if session.mount == path),
            }
        )
    return rows


def find_volume(ref: str, index: Index | None = None) -> dict:
    """The row of ``volumes`` that ``ref`` names: a mount path, a filesystem
    UUID, a label (the drive's name in a listing, or the filesystem's own)
    or a drive letter (``C:``, either case). Raises ``NotFound`` when no
    remembered volume matches, and when several do (the reason names them)."""
    text = ref.strip() if isinstance(ref, str) else ""
    if not text:
        raise NotFound("no drive given")
    rows = volumes(index)
    path = text.rstrip("/") or "/"
    letter = text.upper() if _LETTER.fullmatch(text) else None
    matches = [row for row in rows if row["path"] == path]
    if not matches:
        matches = [
            row
            for row in rows
            if text in (row["uuid"], row["label"], row["fs_label"])
            or (letter is not None and row["letter"] == letter)
        ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotFound(f"no remembered drive matches {text!r}; `status` lists them")
    paths = ", ".join(row["path"] for row in matches)
    raise NotFound(f"{text!r} matches {len(matches)} drives: {paths}; name one by its mount path")


@_logged
def mount_drive(
    ref: str,
    progress: discover.Progress | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Mount the remembered volume ``ref`` names (see ``find_volume``), then
    scan with the engine's own choice between a quick and a deep pass.
    Returns ``{"mount_point", "scan"}``: where the volume is mounted, and
    the summary of that scan.

    The mount is ``mounts.mount_volume``: it may wait for the password the
    desktop asks for. One that is mounted already is only scanned. Raises
    ``NotFound``, or ``MountFailed`` with the reason. ``progress`` and
    ``cancel`` are the scan's."""
    index = load_index()
    volume = find_volume(ref, index)
    log = logs.logger()
    if volume["path"] in {m.path for m in index.current_mounts}:
        where = volume["path"]
        log.info("mount %s: already mounted at %s", volume["label"], where)
    elif not volume["uuid"]:
        raise MountFailed(
            f"the volume id of {volume['label']} is not known; "
            "mount the drive from your file manager"
        )
    else:
        log.info(
            "mount %s (volume id %s, last at %s)", volume["label"], volume["uuid"], volume["path"]
        )
        try:
            where = mounts_mod.mount_volume(volume["uuid"])
        except MountFailed as exc:
            log.warning("mount %s failed: %s", volume["label"], exc.reason)
            raise
        log.info("mount %s: mounted at %s", volume["label"], where)
    return {"mount_point": where, "scan": scan(None, progress, cancel)}


@_logged
def forget_drive(ref: str) -> dict:
    """Take the remembered volume ``ref`` names (see ``find_volume``) out of
    the cache: its stores, their cached parses, its row. Its sessions leave
    the listing. Returns ``{"path", "label", "stores", "sessions"}``.

    Refused (``ApiError``) for a volume that is mounted: the next scan would
    find it again. Raises ``NotFound``. No store is touched."""
    index = load_index()
    volume = find_volume(ref, index)
    if volume["state"] == mounts_mod.MOUNTED:
        raise ApiError(
            f"the drive {volume['label']} is mounted; a scan would find it again. "
            "Nothing was forgotten"
        )
    stores = index.forget(volume["path"])
    try:
        index.save()
    except OSError as exc:
        raise ApiError(f"{index.path}: cache not saved: {exc.strerror or exc}") from exc
    logs.logger().info(
        "forget %s (%s): %d stores and %d sessions dropped from the cache",
        volume["label"],
        volume["path"],
        stores,
        volume["sessions"],
    )
    return {
        "path": volume["path"],
        "label": volume["label"],
        "stores": stores,
        "sessions": volume["sessions"],
    }


@_logged
def set_letter(letter: str, directory: str) -> tuple[str, str]:
    """Say by hand which directory a drive letter stands for: written to
    the override file ``pathmap.json`` of the configuration directory, where
    it wins over evidence. Returns ``(letter, directory)`` as written.
    Raises ``ApiError`` for something that is not a drive letter, a
    directory that is not absolute or does not exist, and an override file
    that is there and cannot be read (it is not overwritten)."""
    return pathmap.set_override(letter, directory)


@_logged
def remove_letter(letter: str) -> str:
    """Take a drive letter out of the override file; returns the letter.
    Raises ``ApiError`` when the file has no entry for it."""
    return pathmap.remove_override(letter)


__all__ = [
    "EXPORT_FORMATS",
    "HANDOFF_MAX_AGE",
    "LEFT_FOR_A_FORCED_UNDO",
    "NO_READER_TOOLS",
    "REMEDY_EXPERIMENTAL",
    "REMEDY_FOLDER",
    "REMEDY_FORCE",
    "REMEDY_KEYS",
    "REMEDY_OVERWRITE",
    "ApiError",
    "ContinueOptions",
    "ContinuePlan",
    "ExportFailed",
    "ImportFailed",
    "ImportOptions",
    "ImportReport",
    "LaunchFailed",
    "LaunchPlan",
    "MountFailed",
    "NoFolder",
    "NotFound",
    "Offline",
    "Turn",
    "UndoFailed",
    "UnimportReport",
    "cache_file",
    "can_import",
    "continue_options",
    "continue_session",
    "export_file",
    "export_transcript",
    "find_session",
    "find_volume",
    "forget_drive",
    "import_options",
    "import_plan",
    "import_report",
    "import_session",
    "list_sessions",
    "load_index",
    "mount_drive",
    "open_session",
    "remove_letter",
    "scan",
    "set_letter",
    "status",
    "transcript",
    "unimport",
    "verify",
    "volumes",
]
