"""Cache of stores and parsed sessions, incremental by mtime/size.

The file holds what a scan found (remembered stores, the result of the last
walk) and, per candidate, what its adapter parsed together with the
``(path, mtime_ns, size)`` signature of the files it was parsed from. The
session list itself is not stored: ``finalise`` derives it from the cache and
the mounts present right now, so a drive that was unplugged since the scan
drops out of the listing without a rescan.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import discover
from .discover import Skipped
from .fsutil import cache_file
from .model import Candidate, Session, Store
from .mounts import NATIVE_LABEL
from .pathmap import PathMap
from .providers import base

CACHE_VERSION = 1
DEEP_MAX_AGE = 24 * 3600   # seconds; an older deep pass is repeated by `scan`
_CONVERSATION = {   # field -> accepted types, as stored per candidate
    "id": (str,), "title": (str,), "cwd": (str, type(None)),
    "started": (str, type(None)), "updated": (str, type(None)),
    "messages": (int, type(None)), "files": (list,), "note": (str, type(None)),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def signature(paths) -> list:
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


def reader_id(provider) -> str:
    """Changes whenever the adapter's code does, so that parses cached by an
    older reader are not reused."""
    marks = []
    for name in (type(provider).__module__, base.__name__):
        try:
            st = os.stat(sys.modules[name].__file__)
            marks.append(f"{st.st_mtime_ns}.{st.st_size}")
        except (KeyError, AttributeError, TypeError, OSError):
            marks.append("?")
    return ":".join(marks)


def _store_key(store: Store) -> str:
    return f"{store.provider}:{store.root}"


def _conversation(session) -> dict | None:
    """The adapter-owned fields of a parsed session, or None when the adapter
    returned something that is not a usable session."""
    if not isinstance(session, Session):
        return None
    row = {name: getattr(session, name) for name in _CONVERSATION}
    row["files"] = list(row["files"] or [])
    row["title"] = row["title"] or ""
    return row if _is_conversation(row) and row["id"] else None


def _is_conversation(row) -> bool:
    return (isinstance(row, dict) and set(row) == set(_CONVERSATION)
            and all(isinstance(row[k], t) and not isinstance(row[k], bool)
                    for k, t in _CONVERSATION.items())
            and all(isinstance(f, str) for f in row["files"]))


def _strings(items) -> list[str]:
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise ValueError("not a list of strings")
    return items


class Index:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        # persisted
        self.stores: list[Store] = []        # every store remembered
        self.skipped: list[Skipped] = []     # stores found but not listed
        self.mounts: list[dict] = []         # as seen by the last scan
        self.last_quick: str | None = None
        self.last_deep: str | None = None
        self.deep_mounts: list[str] = []     # mount paths of the last deep pass
        self.walk: dict[str, dict] = {}      # mount path -> stats of that pass
        self.parsed: dict[str, dict] = {}    # store key -> parse cache
        # derived by finalise()
        self.sessions: list[Session] = []
        self.store_rows: list[dict] = []
        self.warnings: list[str] = []
        self.pathmap: PathMap = PathMap([], {})
        self.current_mounts: list = []

    # --- file ---------------------------------------------------------------

    @classmethod
    def load(cls, path=None) -> "Index":
        """Read the cache; a missing, corrupt or old-version file gives an
        empty index."""
        index = cls(path if path is not None else cache_file())
        try:
            with open(index.path, encoding="utf-8") as fh:
                index._adopt(json.load(fh))
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                RecursionError):
            index.__init__(index.path)
        return index

    def _adopt(self, data) -> None:
        if data["version"] != CACHE_VERSION:
            raise ValueError("other cache version")
        self.stores = [Store(*_strings([d["provider"], d["root"], d["origin"],
                                        d["mount"], d["mount_label"]]))
                       for d in data["stores"]]
        self.skipped = [Skipped(*_strings([d["provider"], d["root"], d["reason"],
                                           d["mount"]]))
                        for d in data["skipped"]]
        self.mounts = [dict(zip(("path", "label", "fstype", "device"),
                                _strings([d["path"], d["label"], d["fstype"], d["device"]])))
                       for d in data["mounts"]]
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
                if not isinstance(cached["sig"], list):
                    raise ValueError("bad signature")
                candidates[hint] = {"sig": cached["sig"], "session": session,
                                    "warnings": _strings(cached["warnings"])}
            if not isinstance(entry["reader"], str):
                raise ValueError("bad reader id")
            self.parsed[key] = {"reader": entry["reader"],
                                "implemented": bool(entry["implemented"]),
                                "warnings": _strings(entry["warnings"]),
                                "candidates": candidates}

    def save(self) -> None:
        """Write the cache atomically: a temp file in the same directory, then
        ``os.replace``."""
        if self.path is None:
            return
        data = {
            "version": CACHE_VERSION,
            "stores": [vars(s) for s in self.stores],
            "skipped": [vars(s) for s in self.skipped],
            "mounts": self.mounts,
            "last_quick": self.last_quick,
            "last_deep": self.last_deep,
            "deep_mounts": self.deep_mounts,
            "walk": self.walk,
            "parsed": self.parsed,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True, separators=(",", ":"))
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # --- scan ---------------------------------------------------------------

    def needs_deep(self, mounts) -> bool:
        """Scan policy: a deep pass is due when none completed yet, when the
        set of mounts changed since the last one, or when it is over 24 h old."""
        if self.last_deep is None:
            return True
        if sorted(m.path for m in mounts) != sorted(self.deep_mounts):
            return True
        last = datetime.fromisoformat(base.parse_ts(self.last_deep).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age < 0 or age > DEEP_MAX_AGE

    def scan(self, providers, mounts, deep=None, progress=None, cancel=None) -> dict:
        """Refresh stores and the parse cache, save, finalise; return a
        summary. ``deep=None`` follows ``needs_deep``. A set ``cancel`` event
        stops the walk or the parsing; what was done until then is kept."""
        started = time.monotonic()
        providers = list(providers)
        mounts = sorted(mounts, key=lambda m: m.path)
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
        for old in self.skipped:
            key = (old.provider, old.root)
            if key not in skipped and old.mount not in present:
                skipped[key] = old
        self.stores = [stores[k] for k in sorted(stores)]
        self.skipped = [skipped[k] for k in sorted(skipped) if k not in stores]
        self.mounts = [{"path": m.path, "label": m.label, "fstype": m.fstype,
                        "device": m.device} for m in mounts]
        counts = {"parsed": 0, "cached": 0}
        kept = {_store_key(s): self.parsed[_store_key(s)] for s in self.stores
                if (s.provider, s.root) not in active and _store_key(s) in self.parsed}
        if not cancelled:
            cancelled = self._parse([s for s in self.stores if (s.provider, s.root) in active],
                                    providers, cancel, counts, kept)
        else:
            kept.update({k: v for k, v in self.parsed.items()
                         if k in {_store_key(s) for s in self.stores}})
        self.parsed = kept
        if not cancelled:
            self.last_quick = utc_now()
        save_error = None
        try:
            self.save()
        except OSError as exc:
            save_error = f"{self.path}: cache not saved: {exc.strerror or exc}"
        self.finalise(providers, mounts)
        if save_error:
            self.warnings = sorted(set(self.warnings) | {save_error})
        by_provider = {p.name: {"stores": 0, "sessions": 0} for p in providers}
        for row in self.store_rows:
            if row["present"] and row["provider"] in by_provider:
                by_provider[row["provider"]]["stores"] += 1
        for session in self.sessions:
            if session.provider in by_provider:
                by_provider[session.provider]["sessions"] += 1
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
            "parsed": counts["parsed"],
            "cached": counts["cached"],
            "by_provider": by_provider,
            "warnings": self.warnings,
        }

    def _parse(self, stores, providers, cancel, counts, out) -> bool:
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
            entry = {"reader": reader, "implemented": True, "warnings": [], "candidates": {}}
            out[key] = entry
            reusable = old["candidates"] if old and old["reader"] == reader else {}
            try:
                candidates = sorted(provider.candidates(store), key=lambda c: c.id_hint)
            except NotImplementedError:
                entry["implemented"] = False
                continue
            except Exception as exc:  # an adapter must not break the scan
                entry["warnings"].append(
                    f"{store.root}: cannot list sessions: {type(exc).__name__}: {exc}")
                continue
            for position, candidate in enumerate(candidates):
                hint = candidate.id_hint
                if hint in entry["candidates"]:
                    entry["warnings"].append(f"{store.root}: duplicate candidate {hint}")
                    continue
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    for later in candidates[position:]:
                        if later.id_hint in reusable:
                            entry["candidates"].setdefault(later.id_hint,
                                                           reusable[later.id_hint])
                    break
                sig = signature(candidate.paths)
                cached = reusable.get(hint)
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
                            f"{store.root}: {hint}: parse failed: {type(exc).__name__}: {exc}")
                        continue
                counts["parsed"] += 1
                conversation = _conversation(session)
                warnings = sorted(set(collected.items))
                if session is not None and conversation is None:
                    warnings.append(f"{store.root}: {hint}: adapter returned an unusable session")
                entry["candidates"][hint] = {"sig": sig, "session": conversation,
                                             "warnings": warnings}
            entry["warnings"].sort()
        return cancelled

    # --- derived view -------------------------------------------------------

    def finalise(self, providers, mounts) -> None:
        """Derive the session list, store rows, path map and warnings from the
        cache and the mounts present now. Parses nothing."""
        providers = list(providers)
        by_name = {p.name: p for p in providers}
        present = {m.path for m in mounts}
        self.current_mounts = sorted(mounts, key=lambda m: m.path)
        sessions: list[Session] = []
        rows = []
        warnings: set[str] = set()
        listed: set[tuple[str, str, str]] = set()   # (origin, provider, id)
        for store in sorted(self.stores, key=_store_key):
            entry = self.parsed.get(_store_key(store)) or {}
            cached = entry.get("candidates", {})
            row = {"provider": store.provider, "root": store.root, "origin": store.origin,
                   "mount": store.mount, "mount_label": store.mount_label,
                   "present": store.origin == "native" or store.mount in present,
                   "reader": entry.get("implemented", True) and store.provider in by_name,
                   "candidates": len(cached), "listed": 0, "skipped": 0, "duplicates": 0}
            rows.append(row)
            if not row["present"]:
                continue
            if not row["reader"]:
                warnings.add(f"{store.provider}: reader not implemented")
                continue
            warnings.update(entry.get("warnings", []))
            for hint in sorted(cached):
                warnings.update(cached[hint]["warnings"])
                conversation = cached[hint]["session"]
                if conversation is None:
                    row["skipped"] += 1
                    continue
                ident = (store.origin, store.provider, conversation["id"])
                if ident in listed:
                    # Hard-linked mirror of a store already listed.
                    row["duplicates"] += 1
                    continue
                listed.add(ident)
                row["listed"] += 1
                sessions.append(Session.for_store(store, **conversation))
        self.pathmap = PathMap.build(mounts, [(s.cwd, s.mount) for s in sessions if s.cwd])
        warnings.update(self.pathmap.warnings)
        native = {(s.provider, s.id): s.key for s in sessions if s.origin == "native"}
        usable = {p.name: bool(p.resumable and p.binary and shutil.which(p.binary))
                  for p in providers}
        for s in sessions:
            if s.cwd:
                s.cwd_local, s.cwd_exists = self.pathmap.to_local(s.cwd, s.mount)
            if s.origin == "native":
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
        `` · imported`` is added once a native copy exists."""
        if session.origin == "native":
            return NATIVE_LABEL
        place = self.pathmap.letter_for_mount(session.mount) or session.mount_label
        return f"{place} · imported" if session.imported_as else place


def fresh(providers, mounts, found: discover.Discovery) -> Index:
    """An index built from ``found`` with no cache behind it: every candidate
    is parsed. Nothing is saved. ``verify`` compares it with the real one."""
    index = Index()
    index.stores = list(found.stores)
    index.skipped = list(found.skipped)
    index._parse(index.stores, list(providers), None, {"parsed": 0, "cached": 0}, index.parsed)
    index.finalise(providers, mounts)
    return index


__all__ = ["CACHE_VERSION", "Candidate", "Index", "fresh", "reader_id", "signature", "utc_now"]
