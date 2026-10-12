"""Map a recorded (foreign) path to a local path.

A recorded path is first reduced to a root token and segments
(``F:`` + ``AI Projects/Site``, or ``/`` + ``home/u/proj``). A root in the
host's own flavour maps to itself. Any other root is resolved per source
mount (the mount the store that recorded it is on) by evidence: which mount
holds the recorded paths. Nothing below is tied to "drive letters on Linux";
on a Windows host the same code would resolve the ``/`` root of a Linux store.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .fsutil import config_dir, listdir_ro

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .mounts import Mount

OVERRIDE_FILE = "pathmap.json"
HOST_ROOT = "/" if os.sep == "/" else None  # a Windows host has drive roots

# (segments recorded, segments on disk). A trailing "*" matches any suffix.
# Claude Desktop is an MSIX app: Windows shows it AppData\Roaming\Claude but
# the files are in the package's private copy.
REDIRECTS = (
    (
        ("AppData", "Roaming", "Claude"),
        ("AppData", "Local", "Packages", "Claude_*", "LocalCache", "Roaming", "Claude"),
    ),
)
# Directories that mark the Windows system volume.
SYSTEM_VOLUME_DIRS = ("users", "windows")
SYSTEM_LETTER = "C:"  # what Windows calls the volume it runs from

_DRIVE = re.compile(r"([A-Za-z]):(?:[\\/]+(.*))?", re.S)  # the whole text, with ``fullmatch``
_SPLIT_ANY = re.compile(r"[\\/]+")


@dataclass(frozen=True)
class Recorded:
    root: str  # "F:" (always upper case) or "/"
    parts: tuple  # the segments below the root


def normalise(recorded: object) -> Recorded | None:
    """Reduce a recorded path to root and segments; None when it has no root
    that can be mapped (relative, UNC, empty, not a string).

    Handles a ``\\\\?\\`` prefix, ``/`` and ``\\`` separators in a drive path,
    and ``file://`` URIs (percent-decoded, the slash before a drive letter
    dropped).
    """
    if not isinstance(recorded, str):
        return None
    text = recorded.strip("\r\n")
    if text[:7].lower() == "file://":
        host, slash, tail = text[7:].partition("/")
        if not slash or host.lower() not in ("", "localhost"):
            return None
        text = "/" + urllib.parse.unquote(tail)
        if re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
    if text.startswith(("\\\\?\\", "//?/")):
        text = text[4:]
        if text[:4].upper() in ("UNC\\", "UNC/"):
            return None
    m = _DRIVE.fullmatch(text)
    if m:
        parts = (p for p in _SPLIT_ANY.split(m.group(2) or "") if p and p != ".")
        return Recorded(m.group(1).upper() + ":", tuple(parts))
    if text.startswith("/") and not text.startswith("//"):
        return Recorded("/", tuple(p for p in text.split("/") if p and p != "."))
    return None


def _mount_path(mount: Mount | str) -> str:
    return mount if isinstance(mount, str) else mount.path


def _join(base: str, name: str) -> str:
    return base + name if base.endswith(os.sep) else base + os.sep + name


class PathMap:
    """Use ``PathMap.build(mounts, evidence)``."""

    def __init__(
        self, mounts: Iterable[Mount | str], overrides: Mapping[str, object] | None = None
    ) -> None:
        self.mounts = sorted({_mount_path(m) for m in mounts})
        self.warnings: list[str] = []
        self._root_overrides: dict[str, str] = {}
        self._prefix_overrides: list[tuple[tuple, str]] = []
        # (source mount, root) -> (local base, how, number of paths behind it)
        self._roots: dict[tuple[str, str], tuple[str, str, int]] = {}
        self._open: dict[tuple[str, str], dict] = {}
        self._listings: dict[str, dict | None] = {}
        self._load_overrides(overrides)

    @classmethod
    def build(
        cls,
        mounts: Iterable[Mount | str],
        evidence: Iterable[tuple[object, Mount | str | None]],
        overrides: Mapping[str, object] | None = None,
    ) -> PathMap:
        """``evidence``: every ``(recorded cwd, source mount)`` pair known.
        ``overrides`` replaces the override file (a dict like its content)."""
        pathmap = cls(mounts, overrides)
        seen: dict[tuple[str, str], set] = {}
        for recorded, source in evidence:
            rec = normalise(recorded)
            if rec is None or rec.root == HOST_ROOT or source is None:
                continue
            # Spellings that differ only in case are one path.
            seen.setdefault((_mount_path(source), rec.root), set()).add(
                tuple(part.lower() for part in rec.parts)
            )
        for key in sorted(seen):
            pathmap._settle(key, sorted(seen[key]))
        return pathmap

    # --- public -------------------------------------------------------------

    def to_local(
        self, recorded_cwd: object, source_mount: Mount | str | None = None
    ) -> tuple[str | None, bool]:
        """``(local path, True)`` when the recorded directory exists here, in
        its on-disk spelling; ``(None, False)`` otherwise. A parent that
        exists is never substituted."""
        rec = normalise(recorded_cwd)
        if rec is None:
            return None, False
        if rec.root == HOST_ROOT:
            path = self._host_path(rec)
            return (path, True) if os.path.isdir(path) else (None, False)
        source = _mount_path(source_mount) if source_mount is not None else None
        base = self._base(rec.root, source)
        hit = self._find(base, rec.parts) if base is not None else None
        return (hit, True) if hit else (None, False)

    def letter_for_mount(
        self, mount: Mount | str, source_mount: Mount | str | None = None
    ) -> str | None:
        """The drive letter (``"C:"``) of a mount, or None when unknown.

        Without ``source_mount`` the mount's own stores decide, then any other
        source; of several letters the one with the most evidence wins.
        """
        mount = _mount_path(mount)

        def letters(source: str | None) -> dict[str, int]:
            found = {
                root: count
                for (src, root), (base, _how, count) in self._roots.items()
                if base == mount and (source is None or src == source)
            }
            for root, base in self._root_overrides.items():
                if base == mount:
                    found.setdefault(root, 0)
            return found

        if source_mount is not None:
            found = letters(_mount_path(source_mount))
        else:
            found = letters(mount) or letters(None)
        found = {root: n for root, n in found.items() if root != "/"}
        if not found:
            return None
        return min(found, key=lambda root: (-found[root], root))

    def letters(self) -> list[dict]:
        """Every resolved root, sorted: ``{letter, source_mount, mount, how,
        paths}`` with ``how`` in ``override``, ``evidence``, ``system volume``."""
        rows = [
            {"letter": root, "source_mount": source, "mount": base, "how": how, "paths": count}
            for (source, root), (base, how, count) in self._roots.items()
        ]
        known = {(row["letter"], row["mount"]) for row in rows}
        for root, base in self._root_overrides.items():
            if (root, base) not in known:
                rows.append(
                    {
                        "letter": root,
                        "source_mount": None,
                        "mount": base,
                        "how": "override",
                        "paths": 0,
                    }
                )
        return sorted(rows, key=lambda r: (r["source_mount"] or "", r["letter"]))

    def unresolved(self) -> list[dict]:
        """Roots that stay unmapped, sorted: ``{letter, source_mount, reason,
        candidates, paths}`` with ``reason`` ``no evidence`` or ``ambiguous``."""
        return [
            dict(self._open[key], letter=key[1], source_mount=key[0]) for key in sorted(self._open)
        ]

    # --- overrides ----------------------------------------------------------

    def _load_overrides(self, overrides: Mapping[str, object] | None) -> None:
        if overrides is None:
            path = config_dir() / OVERRIDE_FILE
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    overrides = json.load(fh)
            except FileNotFoundError:
                return
            except (OSError, ValueError, RecursionError) as exc:
                self.warnings.append(f"{path}: ignored: {exc}")
                return
            if not isinstance(overrides, dict):
                self.warnings.append(f"{path}: ignored: not a JSON object")
                return
        for key in sorted(overrides):
            value = overrides[key]
            rec = normalise(key)
            if not isinstance(value, str) or not os.path.isabs(value):
                self.warnings.append(f"path map override {key!r}: not an absolute path")
            elif rec is None or (rec.root != "/" and rec.parts):
                self.warnings.append(
                    f"path map override {key!r}: not a drive letter or an absolute path"
                )
            else:
                base = value.rstrip(os.sep) or os.sep
                if rec.root == "/":
                    self._prefix_overrides.append((rec.parts, base))
                else:
                    self._root_overrides[rec.root] = base
        self._prefix_overrides.sort(key=lambda item: (-len(item[0]), item[0]))

    def _host_path(self, rec: Recorded) -> str:
        for prefix, base in self._prefix_overrides:
            if rec.parts[: len(prefix)] == prefix:
                return os.sep.join((base,) + rec.parts[len(prefix) :])
        return os.sep + os.sep.join(rec.parts)

    # --- roots --------------------------------------------------------------

    def _settle(self, key: tuple[str, str], paths: list[tuple[str, ...]]) -> None:
        source, root = key
        if root in self._root_overrides:
            self._roots[key] = (self._root_overrides[root], "override", len(paths))
            return
        alone: dict[str, int] = {}  # mounts that are the only one holding a path
        shared: set[str] = set()  # mounts holding a path other mounts hold too
        for parts in paths:
            if not parts:
                continue  # the bare root is on every mount
            hits = [m for m in self.mounts if self._find(m, parts)]
            if len(hits) == 1:
                alone[hits[0]] = alone.get(hits[0], 0) + 1
            else:
                shared.update(hits)
        if len(alone) == 1:
            ((mount, count),) = alone.items()
            self._roots[key] = (mount, "evidence", count)
        elif len(alone) > 1:
            self._unsettled(key, "ambiguous", alone, paths)
        elif source in shared and self._is_system_volume(source):
            # Two Windows installs with the same folders: a store on a system
            # volume means that volume when it says its own letter.
            self._roots[key] = (source, "system volume", len(paths))
        elif shared:
            self._unsettled(key, "ambiguous", shared, paths)
        elif root == SYSTEM_LETTER and self._is_system_volume(source):
            # None of the recorded folders exists any more, on any mount: a
            # Windows install still calls its own system volume C:.
            self._roots[key] = (source, "system volume", len(paths))
        else:
            self._unsettled(key, "no evidence", (), paths)

    def _unsettled(
        self,
        key: tuple[str, str],
        reason: str,
        candidates: Iterable[str],
        paths: list[tuple[str, ...]],
    ) -> None:
        self._open[key] = {"reason": reason, "candidates": sorted(candidates), "paths": len(paths)}

    def _base(self, root: str, source: str | None) -> str | None:
        if root in self._root_overrides:
            return self._root_overrides[root]
        if source is not None and (source, root) in self._roots:
            return self._roots[(source, root)][0]
        if (source, root) in self._open:
            return None
        # This source recorded nothing under the root: use the other sources
        # when they agree.
        bases = {base for (_src, r), (base, _how, _n) in self._roots.items() if r == root}
        return bases.pop() if len(bases) == 1 else None

    def _is_system_volume(self, mount: str) -> bool:
        table = self._names(mount)
        if not table:
            return False
        return all(
            any(os.path.isdir(_join(mount, name)) for name in table.get(wanted, ()))
            for wanted in SYSTEM_VOLUME_DIRS
        )

    # --- resolution ---------------------------------------------------------

    def _names(self, directory: str) -> dict | None:
        """lower-cased name -> on-disk names, or None when unreadable."""
        if directory not in self._listings:
            try:
                names = listdir_ro(directory)
            except OSError:
                self._listings[directory] = None
            else:
                table: dict[str, list[str]] = {}
                for name in sorted(names):
                    table.setdefault(name.lower(), []).append(name)
                self._listings[directory] = table
        return self._listings[directory]

    def _find(self, base: str, parts: tuple) -> str | None:
        hit = self._resolve(base, parts)
        if hit is not None:
            return hit
        lowered = tuple(p.lower() for p in parts)
        for recorded, actual in REDIRECTS:
            want = tuple(p.lower() for p in recorded)
            for i in range(len(parts) - len(want) + 1):
                if lowered[i : i + len(want)] == want:
                    hit = self._resolve(
                        base,
                        parts[:i] + actual + parts[i + len(want) :],
                        wild=range(i, i + len(actual)),
                    )
                    if hit is not None:
                        return hit
                    break
        return None

    def _resolve(self, base: str, parts: tuple, wild: range = range(0)) -> str | None:
        """Follow ``parts`` below ``base`` comparing names case-insensitively;
        the result is spelled as on disk. Indexes in ``wild`` may end in "*"."""
        current = base
        for i, segment in enumerate(parts):
            table = self._names(current)
            if table is None:
                return None
            if i in wild and segment.endswith("*"):
                stem = segment[:-1].lower()
                rest = range(max(wild.start - i - 1, 0), max(wild.stop - i - 1, 0))
                for key in sorted(table):
                    if key.startswith(stem):
                        for name in table[key]:
                            hit = self._resolve(_join(current, name), parts[i + 1 :], rest)
                            if hit is not None:
                                return hit
                return None
            group = table.get(segment.lower())
            if not group:
                return None
            if segment in group:
                name = segment
            elif len(group) == 1:
                name = group[0]
            else:
                return None  # several spellings differ only by case
            current = _join(current, name)
        return current if os.path.isdir(current) else None
