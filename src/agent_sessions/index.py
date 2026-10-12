"""Cache of stores and parsed sessions, incremental by mtime/size.

The file holds what a scan found (remembered stores, the volumes they are
on, the result of the last walk) and, per candidate, what its adapter parsed
together with the ``(path, mtime_ns, size)`` signature of the files it was
parsed from. The session list itself is not stored: ``finalise`` derives it
from the cache and the mounts present right now, so the sessions of a drive
that was unmounted since the scan are listed as offline without a rescan,
from the cached parse alone.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import discover, journal, launch, textsafe
from .discover import Skipped
from .fmt import utc_now
from .fsutil import cache_file, make_private_dir, open_private
from .model import Candidate, Session, Store
from .mounts import NATIVE_LABEL, UUID_PATTERN, Mount, identity_from_mount_point
from .pathmap import HOST_ROOT, PathMap, normalise
from .providers import base

if TYPE_CHECKING:
    import threading

    from .fsutil import StrPath

CACHE_VERSION = 2
# Version 1 had no ``volumes`` table, no identity in ``mounts`` and no path
# evidence per store. It is taken over (``_adopt``), not thrown away: its
# remembered stores are what lists the sessions of a drive that is not
# mounted when the first version 2 program runs.
_OLD_VERSION = 1
DEEP_MAX_AGE = 24 * 3600  # seconds; an older deep pass is repeated by `scan`
OFFLINE_NOTE = "drive not mounted"  # the note of a session listed from the cache alone
_MOUNT_FIELDS = ("path", "label", "fstype", "device", "uuid", "fs_label")
_OLD_MOUNT_FIELDS = 4  # how many of them a version 1 cache has
_CONVERSATION = {  # field -> accepted types, as stored per candidate
    "id": (str,),
    "title": (str,),
    "cwd": (str, type(None)),
    "started": (str, type(None)),
    "updated": (str, type(None)),
    "messages": (int, type(None)),
    "files": (list,),
    "note": (str, type(None)),
}


def signature(paths: Iterable[str]) -> list:
    """``[path, mtime_ns, size]`` for every path, sorted; a missing path gets
    nulls. Only ``stat`` is called, which leaves access times alone."""
    rows = []
    for path in sorted(set(paths)):
        try:
            st = os.stat(path)
            rows.append([path, st.st_mtime_ns, st.st_size])
        except OSError:
            rows.append([path, None, None])
    return rows


def reader_id(provider: base.Provider) -> str:
    """Changes whenever the adapter's code does, so that parses cached by an
    older reader are not reused."""
    marks = []
    for name in (type(provider).__module__, base.__name__):
        try:
            # A module without a file gives None here: the TypeError is handled below.
            st = os.stat(sys.modules[name].__file__)  # type: ignore[arg-type]
            marks.append(f"{st.st_mtime_ns}.{st.st_size}")
        except (KeyError, AttributeError, TypeError, OSError):
            marks.append("?")
    return ":".join(marks)


def _store_key(store: Store) -> str:
    return f"{store.provider}:{store.root}"


def _conversation(session: object) -> dict | None:
    """The adapter-owned fields of a parsed session, or None when the adapter
    returned something that is not a usable session."""
    if not isinstance(session, Session):
        return None
    row = {name: getattr(session, name) for name in _CONVERSATION}
    row["files"] = list(row["files"] or [])
    row["title"] = row["title"] or ""
    return row if _is_conversation(row) and row["id"] else None


def _is_conversation(row: object) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == set(_CONVERSATION)
        and all(
            isinstance(row[k], t) and not isinstance(row[k], bool) for k, t in _CONVERSATION.items()
        )
        and all(isinstance(f, str) for f in row["files"])
    )


_TIMES = ("started", "updated")


def _sound_times(session: dict) -> tuple[dict, bool]:
    """A cached session with only such times as ``parse_ts`` writes them,
    and whether it had no other. The loader checks the type of a cached
    value; of a time it also checks the form, since a listing computes with
    it: text that is no time, a time without a zone or one of the year 9999
    would stop ``list`` or the TUI every time, and no scan would mend it
    while the session's file stays as it is. Such a time is dropped."""
    odd = [name for name in _TIMES if session[name] is not None]
    odd = [name for name in odd if base.parse_ts(session[name]) != session[name]]
    if not odd:
        return session, True
    return dict(session, **dict.fromkeys(odd)), False


ID_SHOWN = 40  # characters of a refused session id that a warning quotes
CWD_MAX = 32767  # characters; the longest path Windows takes, eight times Linux's
NOTE_MAX = 200  # characters of a session's note that are kept
EVIDENCE_MAX = 10_000  # recorded paths of one store that are kept as path-map evidence


def _import_folders() -> dict[tuple[str, str], tuple[object, str]]:
    """``(provider, session id)`` -> ``(native store, folder)`` for every
    import that stands (``journal.find_record``) and whose folder is there.

    The folder is the one the copy was made for: the one the user chose for
    the import, else the one the recorded folder was mapped to then (the
    record's ``chosen_folder``, ``cwd_local``). A copy can keep the folder
    as the other machine recorded it (Claude, Antigravity); the path map
    then leads to where that folder is found today, which is not the chosen
    one, and nowhere when the drive that resolved its letter is away.

    The journal is the user's own file, trusted but possibly malformed. One
    that is missing, cannot be read or holds something else gives nothing,
    and no warning: this is read with every listing. A folder counts only
    when it is text without a NUL, an absolute path, and a directory that
    exists."""
    try:
        records = journal.read(quiet=True)
    except OSError:
        return {}
    last: dict[tuple[str, str], dict] = {}
    for record in journal.not_undone(records):
        ident = journal.ident(record)
        if ident is not None:
            last[ident] = record
    folders: dict[tuple[str, str], tuple[object, str]] = {}
    for ident, record in last.items():
        for key in ("chosen_folder", "cwd_local"):
            folder = record.get(key)
            if (
                isinstance(folder, str)
                and "\0" not in folder
                and os.path.isabs(folder)
                and os.path.isdir(folder)
            ):
                folders[ident] = (record.get("destination_store"), folder)
                break
    return folders


def _id_refusal(provider: base.Provider, store: Store, hint: str, conversation: dict) -> str | None:
    """None when the session's id is in the form the provider's ids have,
    else the warning that says why it is not listed.

    The check is here, not in the adapters, and it is made twice: on what
    ``parse`` returns and on every cached entry when it is used. An id is
    text from a file on another drive, and a listed session's id goes into a
    command line (``launch``) and into file names (``importer``).
    """
    if provider.valid_id(conversation["id"]):
        return None
    files = conversation["files"]
    where = files[0] if files else hint  # the candidate's file, below the store
    if where.startswith(store.root + os.sep):
        where = where[len(store.root) + 1 :]
    shown = textsafe.clean(conversation["id"])
    if len(shown) > ID_SHOWN:
        shown = shown[:ID_SHOWN] + "…"
    label = provider.label or provider.name
    return (
        f"{store.root}: {where}: session id '{shown}' is not in the form "
        f"{label} ids have; not listed"
    )


def _bound(store: Store, hint: str, conversation: dict) -> list[str]:
    """Hold a parsed session's free text to sizes that make sense, in place;
    returns the warnings. Adapters take a folder and a note over as the
    store has them, and a store on another drive can hold megabytes there;
    every listed session is kept in memory and written to the cache."""
    warnings = []
    conversation["title"] = base.clean_title(conversation["title"])
    cwd = conversation["cwd"]
    if cwd is not None and len(cwd) > CWD_MAX:
        conversation["cwd"] = None
        warnings.append(
            f"{store.root}: {hint}: the recorded folder is {len(cwd):,} characters long, "
            "which no path is; left out"
        )
    note = conversation["note"]
    if note is not None and len(note) > NOTE_MAX:
        conversation["note"] = note[:NOTE_MAX]
    return warnings


def _evidence(provider: base.Provider, store: Store, old: dict | None) -> dict[str, list]:
    """The recorded paths a store's adapter offers as evidence for the path
    map (``Provider.path_evidence``), as cached with the store: ``sig`` is
    the signature of the files they were read from, and while it stands the
    files are not read again (``old`` is the store's cache entry, when the
    same reader wrote it). The native store is not asked: its paths are
    this machine's own and need no mapping. An adapter that fails costs the
    evidence, never the scan, and is asked again next time."""
    if store.origin == "native":
        return _no_evidence()
    with base.collect_warnings() as collected:
        try:
            files = [str(path) for path in provider.evidence_files(store)]
            if not files:
                return _no_evidence()
            sig = signature(files)
            cached = (old or {}).get("evidence")
            if cached is not None and cached["sig"] == sig:
                return dict(cached)
            paths = provider.path_evidence(store)
            kept = sorted({p for p in paths if isinstance(p, str) and 0 < len(p) <= CWD_MAX})
        except Exception as exc:  # an adapter must not break the scan
            return dict(
                _no_evidence(),
                warnings=[f"{store.root}: path evidence not read: {type(exc).__name__}: {exc}"],
            )
    warnings = sorted(set(collected.items))
    if len(kept) > EVIDENCE_MAX:
        warnings.append(
            f"{store.root}: {len(kept):,} recorded paths; only {EVIDENCE_MAX:,} are used "
            "to work out drive letters"
        )
        kept = kept[:EVIDENCE_MAX]
    return {"sig": sig, "paths": kept, "warnings": warnings}


def _strings(items: object) -> list[str]:
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ValueError("not a list of strings")
    return items


def _no_evidence() -> dict[str, list]:
    """The path evidence of a store that offers none, as cached per store."""
    return {"sig": [], "paths": [], "warnings": []}


def _volume(data: Any) -> dict[str, Any]:  # one row of the cache's ``volumes``: any JSON value
    """A row of the volumes table as read from the cache file; ``ValueError``
    and friends when it is not one."""
    label, fs_label, uuid, fstype = _strings(
        [data["label"], data["fs_label"], data["uuid"], data["fstype"]]
    )
    if uuid and not UUID_PATTERN.fullmatch(uuid):
        raise ValueError("bad volume id")
    letters = data["letters"]
    if not isinstance(letters, dict):
        raise ValueError("bad drive letters")
    _strings(list(letters.values()))
    seen = data["last_seen"]
    if seen is not None and base.parse_ts(seen) is None:
        raise ValueError("bad timestamp")
    return {
        "label": label,
        "fs_label": fs_label,
        "uuid": uuid,
        "fstype": fstype,
        "letters": dict(letters),
        "last_seen": seen,
    }


class Index:
    def __init__(self, path: StrPath | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._reset()

    def _reset(self) -> None:
        """Empty the index: everything but where its file is. ``load`` comes
        back here when a cache turns out to be unusable half way through."""
        # persisted
        self.stores: list[Store] = []  # every store remembered
        self.skipped: list[Skipped] = []  # stores found but not listed
        self.mounts: list[dict] = []  # as seen by the last scan
        # mount path -> {label, fs_label, uuid, fstype, letters, last_seen}:
        # the volumes that hold a remembered store. ``letters`` is what the
        # stores on the volume call the drives (letter -> mount path), as
        # the last scan that saw the volume resolved them.
        self.volumes: dict[str, dict[str, Any]] = {}
        self.last_quick: str | None = None
        self.last_deep: str | None = None
        self.deep_mounts: list[str] = []  # mount paths of the last deep pass
        self.walk: dict[str, dict] = {}  # mount path -> stats of that pass
        self.parsed: dict[str, dict] = {}  # store key -> parse cache
        # set by load(), not persisted: why the cache gave nothing
        # ("missing", "unreadable: <reason>"), None when it was read
        self.load_error: str | None = None
        # derived by finalise()
        self.sessions: list[Session] = []
        self.store_rows: list[dict] = []
        self.warnings: list[str] = []
        self.pathmap: PathMap = PathMap([], {})
        self.current_mounts: list[Mount] = []

    # --- file ---------------------------------------------------------------

    @classmethod
    def load(cls, path: StrPath | None = None) -> Index:
        """Read the cache; a missing, corrupt or old-version file gives an
        empty index, and ``load_error`` says which it was."""
        where = Path(path) if path is not None else cache_file()
        index = cls(where)
        try:
            with open(where, encoding="utf-8") as fh:
                index._adopt(json.load(fh))
        except FileNotFoundError:
            index._reset()
            index.load_error = "missing"
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            index._reset()  # whatever _adopt had taken over before it failed
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            index.load_error = f"unreadable: {reason or type(exc).__name__}"
        return index

    def _adopt(self, data: Any) -> None:  # the parsed cache file: any JSON value
        if data["version"] not in (_OLD_VERSION, CACHE_VERSION):
            raise ValueError("other cache version")
        old = data["version"] == _OLD_VERSION
        self.stores = [
            Store(*_strings([d["provider"], d["root"], d["origin"], d["mount"], d["mount_label"]]))
            for d in data["stores"]
        ]
        self.skipped = [
            Skipped(*_strings([d["provider"], d["root"], d["reason"], d["mount"]]))
            for d in data["skipped"]
        ]
        self.mounts = []
        for d in data["mounts"]:
            row = dict.fromkeys(_MOUNT_FIELDS, "")
            for name in _MOUNT_FIELDS[: _OLD_MOUNT_FIELDS if old else None]:
                row[name] = d[name]
            _strings(list(row.values()))
            self.mounts.append(row)
        for name in ("last_quick", "last_deep"):
            value = data[name]
            if value is not None and base.parse_ts(value) is None:
                raise ValueError("bad timestamp")
            setattr(self, name, value)
        self.deep_mounts = _strings(data["deep_mounts"])
        self.walk = {}
        for mount, stats in data["walk"].items():
            row = discover.WalkStats(**stats).to_dict()
            _strings(row["error_paths"])
            if not all(isinstance(row[k], (int, float)) for k in row if k != "error_paths"):
                raise ValueError("bad walk stats")
            self.walk[mount] = row
        self.parsed = {}
        for key, entry in data["parsed"].items():
            candidates = {}
            for hint, cached in entry["candidates"].items():
                session = cached["session"]
                if session is not None and not _is_conversation(session):
                    raise ValueError("bad cached session")
                sig = cached["sig"]
                if not isinstance(sig, list):
                    raise ValueError("bad signature")
                if session is not None:
                    session, sound = _sound_times(session)
                    if not sound:
                        sig = []  # no file has this signature: the next scan parses it again
                candidates[hint] = {
                    "sig": sig,
                    "session": session,
                    "warnings": _strings(cached["warnings"]),
                }
            if not isinstance(entry["reader"], str):
                raise ValueError("bad reader id")
            evidence = _no_evidence() if old else entry["evidence"]
            if not isinstance(evidence["sig"], list):
                raise ValueError("bad signature")
            self.parsed[key] = {
                "reader": entry["reader"],
                "implemented": bool(entry["implemented"]),
                "warnings": _strings(entry["warnings"]),
                "candidates": candidates,
                "evidence": {
                    "sig": evidence["sig"],
                    "paths": _strings(evidence["paths"]),
                    "warnings": _strings(evidence["warnings"]),
                },
            }
        if old:
            self._volumes_of_old_cache()
        else:
            self.volumes = {path: _volume(row) for path, row in data["volumes"].items()}
            # A store on a drive that is not mounted is listed by its
            # volume's row: none may be without one.
            for path in sorted(self._foreign_mounts() - set(self.volumes)):
                self.volumes[path] = self._blank_volume(path)

    def save(self) -> None:
        """Write the cache atomically: a temp file in the same directory, then
        ``os.replace``. The file is private (0600), whatever mode the one it
        replaces had, and so is a cache directory this makes (0700): the
        cache holds the title and the folder of every session."""
        if self.path is None:
            return
        data = {
            "version": CACHE_VERSION,
            "stores": [vars(s) for s in self.stores],
            "skipped": [vars(s) for s in self.skipped],
            "mounts": self.mounts,
            "volumes": self.volumes,
            "last_quick": self.last_quick,
            "last_deep": self.last_deep,
            "deep_mounts": self.deep_mounts,
            "walk": self.walk,
            "parsed": self.parsed,
        }
        make_private_dir(self.path.parent)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            with open_private(tmp) as fh:
                json.dump(data, fh, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # --- volumes ------------------------------------------------------------

    def _foreign_mounts(self) -> set[str]:
        """Mount paths that hold something which stays remembered while its
        drive is away: a store that is not the native one, a skipped store."""
        return {s.mount for s in self.stores if s.origin != "native"} | {
            s.mount for s in self.skipped
        }

    def _blank_volume(self, path: str) -> dict[str, Any]:
        """The row of a volume nothing is known about but its mount path."""
        label = next((s.mount_label for s in self.stores if s.mount == path), "")
        return {
            "label": label or os.path.basename(path) or path,
            "fs_label": "",
            "uuid": "",
            "fstype": "",
            "letters": {},
            "last_seen": None,
        }

    def _volumes_of_old_cache(self) -> None:
        """The volumes table for a version 1 cache, which had none, from what
        that cache does hold: the mounts its last scan saw, the mount path
        itself (udisks names it after the volume's label or UUID, so the
        identity of a volume that is attached can be read back from the
        name) and the folders its sessions recorded."""
        seen = {mount["path"]: mount for mount in self.mounts}
        holding = {s.mount for s in self.stores} & set(seen)
        for path in sorted(self._foreign_mounts() | holding):
            row = self._blank_volume(path)
            if path in seen:
                row.update(
                    label=seen[path]["label"],
                    fstype=seen[path]["fstype"],
                    last_seen=self.last_quick,
                )
            row["uuid"], row["fs_label"] = identity_from_mount_point(path)
            row["letters"] = self._letters_from_cache(path)
            self.volumes[path] = row

    def _letters_from_cache(self, mount: str) -> dict[str, str]:
        """The drive letter of a volume, told from the cache alone (a version
        1 cache kept no letters, and evidence cannot be gathered while the
        volume is away): the one letter under which a session of a store on
        the volume recorded the folder its own store is in, or a folder
        above it. ``C:\\Users\\me`` recorded in ``<mount>/Users/me/.claude``
        says the volume is ``C:``. Nothing when no letter or several fit."""
        base_path = mount.rstrip(os.sep)
        letters: set[str] = set()
        for store in self.stores:
            if (
                store.mount != mount
                or store.origin == "native"
                or not store.root.startswith(base_path + os.sep)
            ):
                continue
            below = tuple(part.lower() for part in store.root[len(base_path) + 1 :].split(os.sep))
            entry = self.parsed.get(_store_key(store)) or {}
            for cached in entry.get("candidates", {}).values():
                session = cached["session"]
                recorded = normalise(session["cwd"]) if session else None
                if recorded is None or recorded.root == HOST_ROOT or not recorded.parts:
                    continue
                parts = tuple(part.lower() for part in recorded.parts)
                if below[: len(parts)] == parts:
                    letters.add(recorded.root)
        return {letters.pop(): mount} if len(letters) == 1 else {}

    def _drop_moved(self, mounts: Iterable[Mount]) -> None:
        """Forget what is remembered at the old mount path of a volume that
        is mounted somewhere else now (the same filesystem UUID at another
        path): its stores are not there any more. The walk that a changed
        set of mounts brings finds them where they are. A volume whose UUID
        is not known is never matched."""
        mounts = list(mounts)
        present = {m.path for m in mounts}
        here = {m.uuid for m in mounts if m.uuid}
        moved = {
            path
            for path, row in self.volumes.items()
            if path not in present and row["uuid"] and row["uuid"] in here
        }
        for path in sorted(moved):
            self.forget(path)

    def forget(self, path: str) -> int:
        """Take a volume out of the index: its row, the stores remembered on
        it (the native one is never one of them), their cached parses, the
        skipped stores and the walk of it. Returns how many stores went.
        Nothing is saved here."""
        gone = [s for s in self.stores if s.mount == path and s.origin != "native"]
        for store in gone:
            self.parsed.pop(_store_key(store), None)
        self.stores = [s for s in self.stores if s not in gone]
        self.skipped = [s for s in self.skipped if s.mount != path]
        self.volumes.pop(path, None)
        self.walk.pop(path, None)
        # Not part of the last walk any more: when the drive is back, the
        # set of mounts differs from it and a walk is due.
        self.deep_mounts = [p for p in self.deep_mounts if p != path]
        return len(gone)

    def _note_volumes(self, mounts: Iterable[Mount]) -> None:
        """Bring the volumes table up to date after a scan found the stores:
        a row with the identity of every mounted volume that holds one, and
        the rows of the volumes that are away and still remembered."""
        now = utc_now()
        mounts = list(mounts)
        present = {m.path for m in mounts}
        holding = {s.mount for s in self.stores} | {s.mount for s in self.skipped}
        table: dict[str, dict[str, Any]] = {}
        for mount in mounts:
            if mount.path in holding:
                table[mount.path] = {
                    "label": mount.label,
                    "fs_label": mount.fs_label,
                    "uuid": mount.uuid,
                    "fstype": mount.fstype,
                    "letters": (self.volumes.get(mount.path) or {}).get("letters", {}),
                    "last_seen": now,
                }
        for path in sorted(self._foreign_mounts() - present):
            table[path] = self.volumes.get(path) or self._blank_volume(path)
        self.volumes = table

    def _note_letters(self) -> None:
        """Keep, per mounted volume, the drive letters its stores use as the
        path map resolved them just now: what ``where`` shows of the volume
        once it is away."""
        resolved = self.pathmap.letters()
        for mount in self.current_mounts:
            row = self.volumes.get(mount.path)
            if row is not None:
                row["letters"] = {
                    r["letter"]: r["mount"] for r in resolved if r["source_mount"] == mount.path
                }

    def letter_for_mount(self, path: str) -> str | None:
        """The drive letter a mount stands for, or None: what the path map
        says now and, for a volume that is not mounted, the letter the last
        scan that saw it resolved for it."""
        letter = self.pathmap.letter_for_mount(path)
        if letter is None and path not in {m.path for m in self.current_mounts}:
            letters = (self.volumes.get(path) or {}).get("letters", {})
            own = sorted(name for name, target in letters.items() if target == path and name != "/")
            letter = own[0] if own else None
        return letter

    # --- scan ---------------------------------------------------------------

    def needs_deep(self, mounts: Iterable[Mount]) -> bool:
        """Scan policy: a deep pass is due when none completed yet, when the
        set of mounts changed since the last one, or when it is over 24 h old."""
        if self.last_deep is None:
            return True
        if sorted(m.path for m in mounts) != sorted(self.deep_mounts):
            return True
        stamp = base.parse_ts(self.last_deep)
        if stamp is None:
            # Not a time (a caller set it, or a clock far off wrote it): a
            # pass whose age cannot be told is due.
            return True
        last = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - last).total_seconds()
        return age < 0 or age > DEEP_MAX_AGE

    def scan(
        self,
        providers: Iterable[base.Provider],
        mounts: Iterable[Mount],
        deep: bool | None = None,
        progress: discover.Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> dict:
        """Refresh stores and the parse cache, save, finalise; return a
        summary. ``deep=None`` follows ``needs_deep``. A set ``cancel`` event
        stops the walk or the parsing; what was done until then is kept."""
        started = time.monotonic()
        launch.forget_tools()  # a tool installed since the last scan is seen by this one
        providers = list(providers)
        mounts = sorted(mounts, key=lambda m: m.path)
        self._drop_moved(mounts)
        do_deep = self.needs_deep(mounts) if deep is None else bool(deep)
        remembered = [(s.provider, s.root) for s in self.stores]
        remembered += [(s.provider, s.root) for s in self.skipped]
        found = discover.fast_pass(mounts, providers, remembered)
        stores = {(s.provider, s.root): s for s in found.stores}
        skipped = {(s.provider, s.root): s for s in found.skipped}
        cancelled = False
        walk = None
        if do_deep:
            deep_found = discover.deep_pass(mounts, providers, progress, cancel)
            stores.update({(s.provider, s.root): s for s in deep_found.stores})
            skipped.update({(s.provider, s.root): s for s in deep_found.skipped})
            cancelled = deep_found.cancelled
            walk = {path: stats.to_dict() for path, stats in deep_found.stats.items()}
            if not cancelled:
                self.last_deep = utc_now()
                self.deep_mounts = [m.path for m in mounts]
                self.walk = walk
        # A store on a mount that is gone was not looked at: it stays
        # remembered, with its cached parses. One on a present mount that is
        # no longer a store is forgotten.
        present = {m.path for m in mounts}
        active = set(stores)
        for old in self.stores:
            key = (old.provider, old.root)
            if key not in stores and old.origin != "native" and old.mount not in present:
                stores[key] = old
        for old_skip in self.skipped:
            key = (old_skip.provider, old_skip.root)
            if key not in skipped and old_skip.mount not in present:
                skipped[key] = old_skip
        self.stores = [stores[k] for k in sorted(stores)]
        self.skipped = [skipped[k] for k in sorted(skipped) if k not in stores]
        self.mounts = [{name: getattr(m, name) for name in _MOUNT_FIELDS} for m in mounts]
        counts = {"parsed": 0, "cached": 0}
        kept = {
            _store_key(s): self.parsed[_store_key(s)]
            for s in self.stores
            if (s.provider, s.root) not in active and _store_key(s) in self.parsed
        }
        if not cancelled:
            cancelled = self._parse(
                [s for s in self.stores if (s.provider, s.root) in active],
                providers,
                cancel,
                counts,
                kept,
            )
        else:
            kept.update(
                {k: v for k, v in self.parsed.items() if k in {_store_key(s) for s in self.stores}}
            )
        self.parsed = kept
        if not cancelled:
            self.last_quick = utc_now()
        # The listing is derived before the cache is written: the drive
        # letters the path map resolves in it are kept with the volumes.
        self._note_volumes(mounts)
        self.finalise(providers, mounts)
        self._note_letters()
        try:
            self.save()
        except OSError as exc:
            save_error = f"{self.path}: cache not saved: {exc.strerror or exc}"
            self.warnings = sorted(set(self.warnings) | {save_error})
        by_provider = {p.name: {"stores": 0, "sessions": 0, "offline": 0} for p in providers}
        for row in self.store_rows:
            if row["present"] and row["provider"] in by_provider:
                by_provider[row["provider"]]["stores"] += 1
        for session in self.sessions:
            if session.provider in by_provider:
                by_provider[session.provider]["sessions"] += 1
                by_provider[session.provider]["offline"] += session.offline
        return {
            "mode": "deep" if do_deep else "quick",
            "cancelled": cancelled,
            "seconds": round(time.monotonic() - started, 3),
            "mounts": self.mounts,
            "walk": walk,
            "stores": sum(1 for row in self.store_rows if row["present"]),
            "stores_not_mounted": sum(1 for row in self.store_rows if not row["present"]),
            "skipped_stores": len(self.skipped),
            "candidates": sum(row["candidates"] for row in self.store_rows if row["present"]),
            "sessions": len(self.sessions),
            "offline": sum(1 for session in self.sessions if session.offline),
            "parsed": counts["parsed"],
            "cached": counts["cached"],
            "by_provider": by_provider,
            "warnings": self.warnings,
        }

    def _parse(
        self,
        stores: Iterable[Store],
        providers: Iterable[base.Provider],
        cancel: threading.Event | None,
        counts: dict[str, int],
        out: dict[str, dict],
    ) -> bool:
        """Fill ``out`` (store key -> cache entry) for ``stores``, parsing
        only candidates whose signature changed. Returns True when cancelled."""
        by_name = {p.name: p for p in providers}
        cancelled = False
        for store in sorted(stores, key=_store_key):
            key = _store_key(store)
            old = self.parsed.get(key)
            provider = by_name.get(store.provider)
            if provider is None or cancelled:
                if old is not None:
                    out[key] = old
                continue
            reader = reader_id(provider)
            same_reader = old is not None and old["reader"] == reader
            entry: dict[str, Any] = {
                "reader": reader,
                "implemented": True,
                "warnings": [],
                "candidates": {},
                "evidence": _evidence(provider, store, old if same_reader else None),
            }
            out[key] = entry
            reusable = old["candidates"] if old and same_reader else {}
            with base.collect_warnings() as listing:
                try:
                    candidates = sorted(provider.candidates(store), key=lambda c: c.id_hint)
                except NotImplementedError:
                    entry["implemented"] = False
                    continue
                except Exception as exc:  # an adapter must not break the scan
                    entry["warnings"].append(
                        f"{store.root}: cannot list sessions: {type(exc).__name__}: {exc}"
                    )
                    continue
            # Directories the adapter could not list: kept with the store,
            # and gone again on the scan after they become readable.
            entry["warnings"] += sorted(set(listing.items))
            for position, candidate in enumerate(candidates):
                hint = candidate.id_hint
                if hint in entry["candidates"]:
                    entry["warnings"].append(f"{store.root}: duplicate candidate {hint}")
                    continue
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    for later in candidates[position:]:
                        if later.id_hint in reusable:
                            entry["candidates"].setdefault(later.id_hint, reusable[later.id_hint])
                    break
                sig = signature(candidate.paths)
                cached = reusable.get(hint)
                if (
                    cached is not None
                    and cached["session"] is not None
                    and _id_refusal(provider, store, hint, cached["session"]) is not None
                ):
                    cached = None  # not taken over: parsed again, and refused below
                if cached is not None and cached["sig"] == sig:
                    entry["candidates"][hint] = cached
                    counts["cached"] += 1
                    continue
                with base.collect_warnings() as collected:
                    try:
                        session = provider.parse(store, candidate)
                    except NotImplementedError:
                        entry["implemented"] = False
                        entry["candidates"] = {}
                        break
                    except Exception as exc:  # not cached: retried next scan
                        entry["warnings"].append(
                            f"{store.root}: {hint}: parse failed: {type(exc).__name__}: {exc}"
                        )
                        continue
                counts["parsed"] += 1
                conversation = _conversation(session)
                warnings = sorted(set(collected.items))
                if session is not None and conversation is None:
                    warnings.append(f"{store.root}: {hint}: adapter returned an unusable session")
                if conversation is not None:
                    refusal = _id_refusal(provider, store, hint, conversation)
                    if refusal is not None:
                        warnings.append(refusal)
                        conversation = None
                    else:
                        warnings += _bound(store, hint, conversation)
                entry["candidates"][hint] = {
                    "sig": sig,
                    "session": conversation,
                    "warnings": warnings,
                }
            entry["warnings"].sort()
        return cancelled

    # --- derived view -------------------------------------------------------

    def finalise(
        self, providers: Iterable[base.Provider], mounts: Collection[Mount], offline: bool = True
    ) -> None:
        """Derive the session list, store rows, path map and warnings from the
        cache and the mounts present now. Parses nothing and touches no file
        of a store.

        The sessions of a remembered store whose volume is not mounted are
        listed from the cached parse, marked ``offline``: no local folder,
        not launchable, the note ``drive not mounted``. They take part in
        the order, in the choice between copies of one session and in
        ``imported_as`` like any other. ``offline=False`` leaves them out
        altogether, which gives the listing of the mounted volumes alone
        (``verify`` compares that one with the disks).

        A native session's local folder is where the path map finds the
        recorded one, except for a copy an import made: that one has the
        folder of its import while the import stands and the folder is
        there (``_import_folders``)."""
        providers = list(providers)
        by_name = {p.name: p for p in providers}
        present = {m.path for m in mounts}
        self.current_mounts = sorted(mounts, key=lambda m: m.path)
        sessions: list[Session] = []
        rows = []
        warnings: set[str] = set()
        # (row, store, conversation), in order
        found: list[tuple[dict[str, Any], Store, dict[str, Any]]] = []
        best: dict[tuple[str, str, str], tuple[str, int]] = {}  # (origin, provider, id)
        evidence: list[tuple[object, Mount | str | None]] = []  # (recorded path, source mount)
        for store in sorted(self.stores, key=_store_key):
            entry = self.parsed.get(_store_key(store)) or {}
            cached = entry.get("candidates", {})
            row: dict[str, Any] = {
                "provider": store.provider,
                "root": store.root,
                "origin": store.origin,
                "mount": store.mount,
                "mount_label": store.mount_label,
                "present": store.origin == "native" or store.mount in present,
                "reader": entry.get("implemented", True) and store.provider in by_name,
                "candidates": len(cached),
                "listed": 0,
                "skipped": 0,
                "duplicates": 0,
            }
            rows.append(row)
            here = row["present"]
            if not here and not offline:
                continue
            if not row["reader"]:
                if here:
                    warnings.add(f"{store.provider}: reader not implemented")
                continue
            if here:
                # What a scan could not read is said for the volumes that
                # are there; nothing of a volume that is away was looked at.
                warnings.update(entry.get("warnings", []))
                gathered = entry.get("evidence") or _no_evidence()
                warnings.update(gathered["warnings"])
                evidence += [(path, store.mount) for path in gathered["paths"]]
            for hint in sorted(cached):
                if here:
                    warnings.update(cached[hint]["warnings"])
                conversation = cached[hint]["session"]
                if conversation is None:
                    row["skipped"] += 1
                    continue
                # Every listed session passes here, whatever wrote the cache.
                refusal = _id_refusal(by_name[store.provider], store, hint, conversation)
                if refusal is not None:
                    warnings.add(refusal)
                    row["skipped"] += 1
                    continue
                # Of several copies of one session (a hard-linked mirror, a
                # copied profile) the newest is listed; on a tie the one
                # met first, which is the smaller store key.
                ident = (store.origin, store.provider, conversation["id"])
                updated = conversation["updated"] or ""
                if ident not in best or updated > best[ident][0]:
                    best[ident] = (updated, len(found))
                found.append((row, store, conversation))
        for position, (row, store, conversation) in enumerate(found):
            ident = (store.origin, store.provider, conversation["id"])
            if best[ident][1] != position:
                row["duplicates"] += 1
                continue
            row["listed"] += 1
            session = Session.for_store(store, **conversation)
            session.offline = not row["present"]
            sessions.append(session)
        # Evidence for drive letters: the folders the sessions recorded and
        # what the adapters add (``Provider.path_evidence``), of the mounted
        # volumes only. Whether a folder is on a drive cannot be seen while
        # the store that names it is away; ``letter_for_mount`` remembers.
        evidence += [(s.cwd, s.mount) for s in sessions if s.cwd and not s.offline]
        self.pathmap = PathMap.build(mounts, evidence)
        warnings.update(self.pathmap.warnings)
        native = {(s.provider, s.id): s.key for s in sessions if s.origin == "native"}
        # Installed, not merely on PATH (``launch.tool_state``): asked once
        # per provider, and remembered there for the sessions that follow.
        usable = {
            p.name: bool(p.resumable and p.binary and launch.tool_state(p.binary).installed)
            for p in providers
        }
        imported = _import_folders()  # the journal, read once
        for s in sessions:
            if s.offline:
                s.note = OFFLINE_NOTE
                s.imported_as = native.get((s.provider, s.id))
                continue
            if s.cwd:
                s.cwd_local, s.cwd_exists = self.pathmap.to_local(s.cwd, s.mount)
            if s.origin == "native":
                # An imported copy is resumed in the folder of its import.
                into, folder = imported.get((s.provider, s.id), (None, None))
                if folder is not None and into == s.store:
                    s.cwd_local, s.cwd_exists = folder, True
                s.launchable = usable.get(s.provider, False)
                if not s.launchable and not s.note and s.provider in by_name:
                    s.note = f"{by_name[s.provider].label} CLI is not installed"
            else:
                s.imported_as = native.get((s.provider, s.id))
        sessions.sort(key=lambda s: s.key)
        sessions.sort(key=lambda s: s.updated or "", reverse=True)
        self.sessions = sessions
        self.store_rows = rows
        self.warnings = sorted(warnings)

    def where(self, session: Session) -> str:
        """Where a session lives, for a listing: ``Linux`` for a native one,
        else the drive letter of its mount when known, else the mount label;
        `` · imported`` is added once a native copy exists, `` · offline``
        when there is none and the drive is not mounted."""
        if session.origin == "native":
            return NATIVE_LABEL
        place = self.letter_for_mount(session.mount) or session.mount_label
        if session.imported_as:
            return f"{place} · imported"
        return f"{place} · offline" if session.offline else place


def fresh(
    providers: Collection[base.Provider], mounts: Collection[Mount], found: discover.Discovery
) -> Index:
    """An index built from ``found`` with no cache behind it: every candidate
    is parsed. Nothing is saved. ``verify`` compares it with the real one."""
    index = Index()
    index.stores = list(found.stores)
    index.skipped = list(found.skipped)
    index._parse(index.stores, list(providers), None, {"parsed": 0, "cached": 0}, index.parsed)
    index.finalise(providers, mounts)
    return index


__all__ = [
    "CACHE_VERSION",
    "OFFLINE_NOTE",
    "Candidate",
    "Index",
    "fresh",
    "reader_id",
    "signature",
    "utc_now",
]
