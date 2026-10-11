"""Session and Store dataclasses, and the stable session key."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field


def make_key(provider: str, store_root, session_id: str) -> str:
    """Return ``<provider>:<store-hash8>:<id>``.

    hash8 is the first 8 hex chars of the sha1 of the store root path string.
    """
    digest = hashlib.sha1(str(store_root).encode("utf-8")).hexdigest()
    return f"{provider}:{digest[:8]}:{session_id}"


@dataclass
class Store:
    provider: str
    root: str
    origin: str  # "native" | "foreign"
    mount: str
    mount_label: str


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

    def to_dict(self) -> dict:
        return asdict(self)
