"""The facade the CLI and the TUI call: load, scan, list, status, verify."""

from __future__ import annotations

import shutil
import time

from . import discover
from . import index as index_mod
from .fsutil import cache_file
from .index import Index
from .model import Session, make_key
from .mounts import list_mounts
from .providers import all_providers
from .providers.base import parse_ts

# Agent CLIs that may be installed but have no adapter (SPEC non-goals).
NO_READER_TOOLS = ("copilot", "crush", "cursor-agent", "opencode")


def load_index() -> Index:
    """The cached index, finalised against the mounts present now. Reads no
    store; ``index.sessions`` is the listing."""
    index = Index.load()
    index.finalise(all_providers(), list_mounts())
    return index


def scan(deep=None, progress=None, cancel=None) -> dict:
    """Refresh the index and return a summary.

    ``deep=True`` walks every mount, ``False`` only checks the known store
    locations, ``None`` walks when the mounts changed since the last deep pass
    or that pass is older than 24 h. ``progress(mount_label, dirs_seen)`` is
    called during a walk; ``cancel`` is a ``threading.Event``.
    """
    return Index.load().scan(all_providers(), list_mounts(), deep, progress, cancel)


def list_sessions(provider=None, index=None) -> list[Session]:
    """Listed sessions, newest ``updated`` first, ties by key. ``provider``
    is a provider name; an unknown one raises ValueError."""
    if provider is not None and provider not in {p.name for p in all_providers()}:
        raise ValueError(f"unknown provider: {provider}")
    index = index if index is not None else load_index()
    return [s for s in index.sessions if provider is None or s.provider == provider]


def status(index=None) -> dict:
    """Mounts, stores and counts, drive letters, skipped stores, tools with
    no reader, warnings. Every list is sorted."""
    index = index if index is not None else load_index()
    present = {m.path for m in index.current_mounts}
    mounts = [{"path": m.path, "label": m.label, "fstype": m.fstype, "device": m.device,
               "mounted": True, "letter": index.pathmap.letter_for_mount(m.path),
               "walk": index.walk.get(m.path)}
              for m in index.current_mounts]
    gone = {(row["mount"], row["mount_label"]) for row in index.store_rows
            if not row["present"]}
    mounts += [{"path": path, "label": label, "fstype": "", "device": "",
                "mounted": False, "letter": None, "walk": None}
               for path, label in sorted(gone) if path not in present]
    providers = []
    for p in all_providers():
        rows = [r for r in index.store_rows if r["provider"] == p.name and r["present"]]
        providers.append({
            "name": p.name, "label": p.label, "binary": p.binary,
            "installed": shutil.which(p.binary) if p.binary else None,
            "resumable": bool(p.resumable),
            "reader": all(r["reader"] for r in rows),
            "stores": len(rows),
            "sessions": sum(r["listed"] for r in rows),
        })
    tools = [{"tool": name, "path": shutil.which(name)} for name in NO_READER_TOOLS]
    return {
        "cache": str(index.path),
        "last_quick": index.last_quick,
        "last_deep": index.last_deep,
        "mounts": mounts,
        "providers": providers,
        "stores": index.store_rows,
        "skipped_stores": [{"provider": s.provider, "root": s.root, "reason": s.reason,
                            "mounted": s.mount in present}
                           for s in index.skipped],
        "letters": index.pathmap.letters(),
        "unresolved_letters": index.pathmap.unresolved(),
        "no_reader": [t for t in tools if t["path"]],
        "warnings": index.warnings,
    }


def _newer_than(sig, moment: str | None) -> bool:
    """True when a file of the signature is missing or was modified at or
    after ``moment`` (an ISO time)."""
    if moment is None:
        return True
    limit = parse_ts(moment)
    return any(mtime_ns is None or parse_ts(mtime_ns // 1_000_000) >= limit
               for _path, mtime_ns, _size in sig)


def _moved_on(sig) -> bool:
    """True when the files of a recorded signature are different now."""
    return index_mod.signature([row[0] for row in sig]) != sig


def verify(progress=None, cancel=None) -> dict:
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
        "ok": False, "cancelled": found.cancelled, "differences": [], "stale": [],
        "nested": [], "stores": 0, "sessions": 0,
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
                    differences.append(
                        f"{root}: {hint}: cached parse differs from a fresh parse")
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
                        f"{wanted[key][name]!r} on disk")

    result.update(
        ok=not differences, differences=differences, stale=stale,
        nested=sorted(f"{provider} {root}" for provider, root in nested),
        stores=len(theirs), sessions=len(truth.sessions),
        seconds=round(time.monotonic() - started, 3))
    return result


__all__ = ["NO_READER_TOOLS", "cache_file", "list_sessions", "load_index", "scan",
           "status", "verify"]
