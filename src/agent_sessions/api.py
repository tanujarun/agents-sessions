"""The facade the CLI and the TUI call: load, scan, list, status, verify,
find, open, import, transcript.

What is worth a line in the log is written here, so both front ends log
alike (``logs``): the summary and the warnings of a scan, an import and how
it ended, a launch with the exact command, and any exception that is not one
of the failures this module reports (``ApiError``), with its traceback.
"""

from __future__ import annotations

import functools
import shlex
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from . import discover, importer, launch, logs
from . import index as index_mod
from . import providers as registry
from .errors import ApiError, ImportFailed, LaunchFailed, NotFound
from .fsutil import cache_file, log_file
from .index import Index
from .launch import LaunchPlan
from .model import Session, make_key
from .mounts import NATIVE_LABEL, MountsError, list_mounts
from .providers import all_providers
from .providers.base import NotSupported, Provider, Turn, parse_ts

if TYPE_CHECKING:
    import threading

# Agent CLIs that may be installed but have no adapter (SPEC non-goals).
NO_READER_TOOLS = ("copilot", "crush", "cursor-agent", "opencode")

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
def load_index() -> Index:
    """The cached index, finalised against the mounts present now. Reads no
    store; ``index.sessions`` is the listing."""
    index = Index.load()
    index.finalise(all_providers(), list_mounts())
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
        "%d sessions, %d warnings",
        summary["mode"],
        " (cancelled)" if summary["cancelled"] else "",
        summary["seconds"],
        len(summary["mounts"]),
        summary["stores"],
        summary["candidates"],
        summary["parsed"],
        summary["cached"],
        summary["sessions"],
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
    """Mounts, stores and counts, drive letters, skipped stores, the agent
    tools and whether each is installed, warnings, where the cache and the
    log are. Every list is sorted.

    "Installed" is ``launch.tool_state``: a command on PATH that would only
    install the tool does not count."""
    index = index if index is not None else load_index()
    present = {m.path for m in index.current_mounts}
    mounts = [
        {
            "path": m.path,
            "label": m.label,
            "fstype": m.fstype,
            "device": m.device,
            "mounted": True,
            "letter": index.pathmap.letter_for_mount(m.path),
            "walk": index.walk.get(m.path),
        }
        for m in index.current_mounts
    ]
    gone = {(row["mount"], row["mount_label"]) for row in index.store_rows if not row["present"]}
    mounts += [
        {
            "path": path,
            "label": label,
            "fstype": "",
            "device": "",
            "mounted": False,
            "letter": None,
            "walk": None,
        }
        for path, label in sorted(gone)
        if path not in present
    ]
    providers = []
    readers = set()
    for p in all_providers():
        rows = [r for r in index.store_rows if r["provider"] == p.name and r["present"]]
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
                "sessions": sum(r["listed"] for r in rows),
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
    """Compare the index with a slow, independent look at the disks.

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
    index = load_index()
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
        raise NotFound(f"no session matches {text!r}; `list --keys` shows the keys")
    keys = sorted(s.key for s in matches)
    shown = ", ".join(keys[:MATCHES_SHOWN])
    more = f", and {len(keys) - MATCHES_SHOWN} more" if len(keys) > MATCHES_SHOWN else ""
    raise NotFound(f"{text!r} matches {len(keys)} sessions: {shown}{more}")


def _provider(session: Session) -> Provider:
    try:
        return registry.get(session.provider)
    except KeyError:
        raise NotFound(f"no reader for provider {session.provider!r}") from None


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
    the tool's name. Read lazily. Raises ``NotFound``."""
    session = find_session(ref, index)
    return iter(_provider(session).transcript(session))


@_logged
def open_session(
    ref: Session | str, here: bool = False, dry_run: bool = False, index: Index | None = None
) -> LaunchPlan:
    """Resume a session in a new terminal window, in its project directory.

    A foreign session that was imported resumes its native copy; any other
    foreign session is refused. ``here=True`` starts the tool in this process
    instead (``chdir`` then ``exec``: the call does not return).
    ``dry_run=True`` only returns the plan. Raises ``LaunchFailed`` with the
    reason (a real failure also sends a desktop notification) or ``NotFound``.
    The window is opened by ``launch.starter``, which a caller may replace.
    """
    session = find_session(ref, index)
    where = None
    if session.origin != "native":
        index = index if index is not None else load_index()
        copy = _native_copy(session, index)  # imported: resume that one
        if copy is not None:
            session = copy
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


@dataclass
class ImportReport:
    session: Session  # the native copy, as indexed
    created: bool  # False: it was already there, nothing was copied
    files: list[str] = field(default_factory=list)  # what this import put in place
    bytes: int = 0


def _plan(session: Session, index: Index) -> importer.ImportPlan:
    """The import plan for a foreign session. Raises ``ImportFailed``."""
    if session.origin == "native":
        raise ImportFailed(f"this session is already on {NATIVE_LABEL}")
    try:
        return _provider(session).import_plan(session, index.pathmap)
    except NotSupported as exc:
        raise ImportFailed(exc.reason) from None
    except NotFound as exc:
        raise ImportFailed(exc.reason) from None


@_logged
def can_import(session: Session | str, index: Index | None = None) -> tuple[bool, str]:
    """``(True, "")`` when ``import_session`` would copy this session, else
    ``(False, reason)``: it is native, already imported, its provider cannot
    be imported, its project folder is not on any mounted drive, its id is
    already in the native store, or there is too little free space. Reads
    only; writes nothing."""
    try:
        index = index if index is not None else load_index()
        session = find_session(session, index)
        if _native_copy(session, index) is not None:
            return False, "already imported"
        importer.check(_plan(session, index))
    except ApiError as exc:
        return False, exc.reason
    return True, ""


@_logged
def import_plan(ref: Session | str, index: Index | None = None) -> dict:
    """What ``import_session`` would do, as plain data, for a dry run:
    ``already`` (the native key when it is imported already, else None),
    ``copies``, ``rewrites`` (with the changes each would make), ``appends``,
    ``bytes``, ``needed``, ``free``. Writes nothing. Raises ``ImportFailed``
    for everything the import itself would refuse."""
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
    plan = _plan(session, index)
    importer.check(plan)
    return dict(importer.describe(plan), already=None)


@_logged
def import_report(
    ref: Session | str, progress: importer.Progress | None = None, index: Index | None = None
) -> ImportReport:
    """``import_session`` with the details: whether anything was copied (a
    session imported before is returned with ``created=False``) and which
    files were put in place."""
    index = index if index is not None else load_index()
    session = find_session(ref, index)
    log = logs.logger()
    log.info("import %s (store %s)", session.key, session.store)
    try:
        report = _import(session, progress, index)
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


def _import(session: Session, progress: importer.Progress | None, index: Index) -> ImportReport:
    """``import_report`` for a session that was found."""
    copy = _native_copy(session, index)
    if copy is not None:
        return ImportReport(copy, False)
    if session.origin == "native":
        raise ImportFailed(f"this session is already on {NATIVE_LABEL}")
    provider = _provider(session)
    collision = None
    files: list[str] = []
    size = 0
    try:
        plan = provider.import_plan(session, index.pathmap)
        files, size = plan.destinations, plan.total_bytes
        provider.import_session(session, index.pathmap, progress)
    except importer.Exists as exc:
        collision = exc  # in the store but not in this index: look again below
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
    ref: Session | str, progress: importer.Progress | None = None, index: Index | None = None
) -> Session:
    """Copy a foreign session into the native store and return the native
    copy, indexed and launchable. A session that was imported before is
    returned as it is; nothing is copied twice and nothing is overwritten.

    ``progress(done_bytes, total_bytes)`` is called while copying. Raises
    ``ImportFailed`` with the reason, also when the provider cannot be
    imported. A quick scan runs afterwards; an ``index`` passed in is not
    refreshed, load it again.
    """
    return import_report(ref, progress, index).session


__all__ = [
    "NO_READER_TOOLS",
    "ApiError",
    "ImportFailed",
    "ImportReport",
    "LaunchFailed",
    "LaunchPlan",
    "NotFound",
    "Turn",
    "cache_file",
    "can_import",
    "find_session",
    "import_plan",
    "import_report",
    "import_session",
    "list_sessions",
    "load_index",
    "open_session",
    "scan",
    "status",
    "transcript",
    "verify",
]
