"""Find provider stores on the mounts; prune list; signatures.

``fast_pass`` checks the places a store normally is, ``deep_pass`` walks every
mount, ``brute_pass`` is the unpruned walk ``verify`` compares against. All
three only list directories and call ``Provider.is_store``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import mounts as mounts_mod
from .fsutil import home, subdirs
from .model import Store

if TYPE_CHECKING:
    import threading

    from .mounts import Mount
    from .providers.base import Provider

Progress = Callable[[str, int], None]  # progress(mount_label, dirs_seen)


@dataclass(frozen=True)
class PruneList:
    """Directories a deep pass never descends."""

    names: frozenset  # a directory with this name, anywhere
    prefixes: tuple  # a directory whose name starts with this
    path_suffixes: tuple  # a directory whose path ends with these segments
    root_only: frozenset  # direct children of the mount at "/"

    def matches(self, name: str, parent: str, parent_is_root: bool) -> bool:
        if name in self.names or name.startswith(self.prefixes):
            return True
        if parent_is_root and name in self.root_only:
            return True
        for suffix in self.path_suffixes:
            if name == suffix[-1]:
                lead = os.sep + os.sep.join(suffix[:-1])
                if parent.endswith(lead):
                    return True
        return False


PRUNE = PruneList(
    names=frozenset(
        {
            # Windows system trees (the recycle bin is spelled both ways on NTFS)
            "Windows",
            "ProgramData",
            "$RECYCLE.BIN",
            "$Recycle.Bin",
            "System Volume Information",
            # package trees, game libraries, tool caches
            "node_modules",
            ".git",
            "site-packages",
            "SteamLibrary",
            "XboxGames",
            "_cacache",
            "npm-cache",
            ".cargo",
            ".rustup",
            ".pnpm-store",
            ".npm",
            "__pycache__",
            ".venv",
            "miniforge",
            ".cache",
        }
    ),
    prefixes=("Program Files",),
    # A bare "Temp" may be a user folder; "AppData" and "Packages" hold stores.
    path_suffixes=(("AppData", "Local", "Temp"),),
    root_only=frozenset({"proc", "sys", "dev", "run", "tmp", "var", "usr", "nix"}),
)

# Descended even when it is itself a store: it holds the Antigravity stores.
ALWAYS_DESCEND = frozenset({".gemini"})

# Stores that are found and counted but not listed: (provider name, or None
# for every provider; consecutive path segments; reason shown by `status`).
SKIP_RULES = (
    ("claude", ("local-agent-mode-sessions",), "Claude Desktop history"),
    # Sample stores kept by a project for its tests, this project's included.
    (None, ("tests", "fixtures"), "test fixtures"),
)

IN_PROJECT = "inside the agent-sessions project"


def _project_root() -> str | None:
    """The agent-sessions source tree this module runs from: the directory
    that holds ``src/agent_sessions``, when it also holds ``tests/fixtures``.
    None for an installed copy, which then skips nothing by this rule."""
    package = Path(__file__).resolve().parent
    if package.parent.name != "src":
        return None
    root = package.parent.parent
    return str(root) if (root / "tests" / "fixtures").is_dir() else None


# Stores below this directory are found and counted but not listed: fake
# homes and copied fixtures left by the project's own tests and experiments.
PROJECT_ROOT = _project_root()

# Directories whose children are home directories, relative to a mount.
HOME_PARENTS = ("Users", "home")

PROGRESS_EVERY = 0.1  # seconds between progress calls
ERROR_SAMPLES = 20


@dataclass
class WalkStats:
    dirs: int = 0  # directories listed
    pruned: int = 0  # directories skipped by the prune list
    errors: int = 0  # directories that could not be listed
    mount_skips: int = 0  # other mountpoints not crossed
    seconds: float = 0.0
    error_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Skipped:
    provider: str
    root: str
    reason: str
    mount: str = ""


@dataclass
class Discovery:
    stores: list[Store] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    stats: dict[str, WalkStats] = field(default_factory=dict)  # by mount path
    cancelled: bool = False


class _Found:
    """Collects stores, deduplicated by real path, sorted on the way out."""

    def __init__(self, mounts: Iterable[Mount], providers: Iterable[Provider]) -> None:
        self.mounts = list(mounts)
        self.native: dict[str, str] = {}
        for p in providers:
            try:
                root = p.native_store()
            except NotImplementedError:
                root = None
            if root is not None:
                self.native[p.name] = os.path.realpath(root)
        self.stores: dict[tuple[str, str], Store] = {}
        self.skipped: dict[tuple[str, str], Skipped] = {}

    def check(self, provider: Provider, path: str) -> bool:
        """Record ``path`` if it is a store of ``provider``; say whether it is."""
        try:
            if not provider.is_store(Path(path)):
                return False
        except OSError:
            return False
        root = os.path.realpath(path)
        key = (provider.name, root)
        if key in self.stores or key in self.skipped:
            return True
        native = self.native.get(provider.name) == root
        mount = mounts_mod.find_mount(root, self.mounts)
        if mount is None:
            if not native:
                return True  # on no listed mount: not ours to report
            # A native store is listed even when no listed mount holds it.
            mount = mounts_mod.Mount(Path(root).anchor or os.sep, mounts_mod.NATIVE_LABEL)
        reason = _skip_reason(provider.name, root)
        if reason is not None:
            self.skipped[key] = Skipped(provider.name, root, reason, mount.path)
            return True
        self.stores[key] = Store(
            provider.name, root, "native" if native else "foreign", mount.path, mount.label
        )
        return True

    def result(
        self, stats: dict[str, WalkStats] | None = None, cancelled: bool = False
    ) -> Discovery:
        return Discovery(
            stores=[self.stores[k] for k in sorted(self.stores)],
            skipped=[self.skipped[k] for k in sorted(self.skipped)],
            stats=stats or {},
            cancelled=cancelled,
        )


def _skip_reason(provider_name: str, root: str) -> str | None:
    segments = root.split(os.sep)
    for name, run, reason in SKIP_RULES:
        if name in (None, provider_name) and any(
            tuple(segments[i : i + len(run)]) == run for i in range(len(segments))
        ):
            return reason
    if PROJECT_ROOT and (root == PROJECT_ROOT or root.startswith(PROJECT_ROOT + os.sep)):
        return IN_PROJECT
    return None


def _by_dir_name(providers: Iterable[Provider]) -> dict[str, list[Provider]]:
    table: dict[str, list[Provider]] = {}
    for p in sorted(providers, key=lambda p: p.name):
        for name in p.dir_names:
            table.setdefault(name, []).append(p)
    return table


def _join(parent: str, name: str) -> str:
    return parent + name if parent.endswith(os.sep) else parent + os.sep + name


def _homes(mounts: Iterable[Mount]) -> list[str]:
    found = {str(home())}
    for mount in mounts:
        parents = [_join(mount.path, name) for name in HOME_PARENTS]
        if os.path.basename(mount.path) == "home":
            parents.append(mount.path)
        for parent in parents:
            try:
                found.update(_join(parent, name) for name in subdirs(parent))
            except OSError:
                pass
    return sorted(found)


def fast_pass(
    mounts: Iterable[Mount],
    providers: Iterable[Provider],
    remembered: Iterable[tuple[str, str]] = (),
) -> Discovery:
    """Check the known store locations without walking anything.

    Looked at: every native store, every provider directory name directly
    under each home directory (``<mount>/Users/*``, ``<mount>/home/*``, the
    children of a mount called ``home``, the current home) and under its
    ``.gemini``, and ``remembered`` ``(provider name, root)`` pairs.
    """
    providers = list(providers)
    found = _Found(mounts, providers)
    by_name = {p.name: p for p in providers}
    table = _by_dir_name(providers)
    for p in providers:
        if p.name in found.native:
            found.check(p, found.native[p.name])
    for base in _homes(mounts):
        for parent in [base] + [_join(base, d) for d in sorted(ALWAYS_DESCEND)]:
            for name in sorted(table):
                for p in table[name]:
                    found.check(p, _join(parent, name))
    for provider_name, root in sorted(set(remembered)):
        if provider_name in by_name:
            found.check(by_name[provider_name], root)
    return found.result()


def _walk(
    mount: Mount,
    table: dict[str, list[Provider]],
    found: _Found,
    stops: set[str],
    prune: PruneList | None,
    progress: Progress | None,
    cancel: threading.Event | None,
) -> tuple[WalkStats, bool]:
    stats = WalkStats()
    started = last = time.monotonic()
    root_fs = mount.path == os.sep
    stack = [mount.path]
    cancelled = False
    while stack:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        path = stack.pop()
        try:
            names = subdirs(path)
        except OSError:
            stats.errors += 1
            if len(stats.error_paths) < ERROR_SAMPLES:
                stats.error_paths.append(path)
            continue
        stats.dirs += 1
        at_root = root_fs and path == mount.path
        for name in names:
            child = _join(path, name)
            if child in stops:
                stats.mount_skips += 1
                continue
            if prune is not None and prune.matches(name, path, at_root):
                stats.pruned += 1
                continue
            if name in table:
                confirmed = False
                for provider in table[name]:
                    confirmed = found.check(provider, child) or confirmed
                if confirmed and name not in ALWAYS_DESCEND:
                    continue
            stack.append(child)
        if progress is not None and not stats.dirs & 0xFF:
            now = time.monotonic()
            if now - last >= PROGRESS_EVERY:
                last = now
                progress(mount.label, stats.dirs)
    stats.error_paths.sort()
    stats.seconds = round(time.monotonic() - started, 3)
    if progress is not None:
        progress(mount.label, stats.dirs)
    return stats, cancelled


def deep_pass(
    mounts: Iterable[Mount],
    providers: Iterable[Provider],
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> Discovery:
    """Walk every mount completely and return the stores on them.

    Symlinks are not followed, other mountpoints are not crossed, ``PRUNE`` is
    not descended, and neither is a confirmed store unless its name is in
    ``ALWAYS_DESCEND``. ``progress(mount_label, dirs_seen)`` is called a few
    times a second; setting the ``cancel`` event stops the walk and marks the
    result ``cancelled``.
    """
    providers = list(providers)
    mounts = sorted(mounts, key=lambda m: m.path)
    found = _Found(mounts, providers)
    table = _by_dir_name(providers)
    stops = mounts_mod.mount_points() | {m.path for m in mounts}
    stats: dict[str, WalkStats] = {}
    cancelled = False
    for mount in mounts:
        stats[mount.path], cancelled = _walk(
            mount, table, found, stops - {mount.path}, PRUNE, progress, cancel
        )
        if cancelled:
            break
    return found.result(stats, cancelled)


def brute_pass(
    mounts: Iterable[Mount],
    providers: Iterable[Provider],
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> Discovery:
    """The slow walk ``verify`` compares the index with.

    Written apart from ``deep_pass`` on purpose: no prune list, and nothing is
    left undescended, stores included. Only other mountpoints stop it, which
    covers the pseudo-filesystems. Stores inside another store are in the
    result; ``nested_stores`` tells them apart.
    """
    providers = list(providers)
    mounts = sorted(mounts, key=lambda m: m.path)
    found = _Found(mounts, providers)
    names = {name for p in providers for name in p.dir_names}
    every_stop = mounts_mod.mount_points() | {m.path for m in mounts}
    stats: dict[str, WalkStats] = {}
    cancelled = False
    for mount in mounts:
        one = stats[mount.path] = WalkStats()
        started = last = time.monotonic()
        level = [mount.path]
        while level and not cancelled:
            below = []
            for path in level:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                try:
                    children = subdirs(path)
                except OSError:
                    one.errors += 1
                    if len(one.error_paths) < ERROR_SAMPLES:
                        one.error_paths.append(path)
                    continue
                one.dirs += 1
                for name in children:
                    child = _join(path, name)
                    if child != mount.path and child in every_stop:
                        one.mount_skips += 1
                        continue
                    if name in names:
                        for provider in providers:
                            if name in provider.dir_names:
                                found.check(provider, child)
                    below.append(child)
                if progress is not None and not one.dirs & 0xFF:
                    now = time.monotonic()
                    if now - last >= PROGRESS_EVERY:
                        last = now
                        progress(mount.label, one.dirs)
            level = below
        one.error_paths.sort()
        one.seconds = round(time.monotonic() - started, 3)
        if progress is not None:
            progress(mount.label, one.dirs)
        if cancelled:
            break
    return found.result(stats, cancelled)


def nested_stores(stores: list[Store]) -> list[Store]:
    """The stores that lie inside another store of the list. A store under a
    directory named in ``ALWAYS_DESCEND`` is not nested: that one is descended
    by design."""
    outer = [s.root for s in stores if os.path.basename(s.root) not in ALWAYS_DESCEND]
    return [
        s
        for s in stores
        if any(s.root != root and s.root.startswith(root + os.sep) for root in outer)
    ]
