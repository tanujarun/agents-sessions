"""Session, Store and Candidate dataclasses, and the stable session key."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields


def make_key(provider: str, store_root, session_id: str) -> str:
    """Return ``<provider>:<store-hash8>:<id>``.

    hash8 is the first 8 hex chars of the sha1 of the store root path string.
    """
    digest = hashlib.sha1(str(store_root).encode("utf-8", "surrogateescape")).hexdigest()
    return f"{provider}:{digest[:8]}:{session_id}"


@dataclass
class Store:
    provider: str
    root: str
    origin: str = "foreign"  # "native" | "foreign"
    mount: str = ""
    mount_label: str = ""


@dataclass
class Candidate:
    """Something in a store that may be a session.

    ``paths`` are the files or directories whose ``(path, mtime_ns, size)``
    decide whether a cached parse is still valid. List every file that
    matters: a directory's own stat does not change when a file in it grows.
    """

    id_hint: str
    paths: list[str] = field(default_factory=list)


@dataclass
class Session:
    provider: str
    id: str
    key: str
    title: str
    cwd: str | None
    cwd_local: str | None
    cwd_exists: bool
    started: str | None  # ISO 8601 UTC
    updated: str | None  # ISO 8601 UTC
    messages: int | None
    store: str
    origin: str  # "native" | "foreign"
    mount: str
    mount_label: str
    files: list[str] = field(default_factory=list)
    launchable: bool = False
    imported_as: str | None = None
    note: str | None = None

    @classmethod
    def for_store(cls, store: Store, *, id: str, title: str = "",
                  cwd: str | None = None, started: str | None = None,
                  updated: str | None = None, messages: int | None = None,
                  files: list[str] | None = None,
                  note: str | None = None) -> "Session":
        """Build a session from the conversation fields an adapter knows.

        The fields the index owns (``cwd_local``, ``cwd_exists``,
        ``launchable``, ``imported_as``) start at their defaults.
        """
        return cls(
            provider=store.provider, id=id,
            key=make_key(store.provider, store.root, id),
            title=title, cwd=cwd, cwd_local=None, cwd_exists=False,
            started=started, updated=updated, messages=messages,
            store=store.root, origin=store.origin, mount=store.mount,
            mount_label=store.mount_label,
            files=list(files) if files else [], note=note,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        """Inverse of ``to_dict``; unknown keys are ignored."""
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values["files"] = list(values.get("files") or [])
        return cls(**values)

    def to_dict(self) -> dict:
        return asdict(self)
