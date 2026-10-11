"""Copy a foreign session into the native store.

An adapter describes an import as an ``ImportPlan`` (``Provider.import_plan``:
which source files go where, what is rewritten on the way, what is appended
to a native file); it reads the source and writes nothing. ``check`` applies
the rules that hold for every provider, ``execute`` carries the plan out:

- the source is only read, through ``fsutil.open_ro`` and ``listdir_ro`` (no
  access time moves on the other drive);
- nothing that exists is overwritten: an id already in the native store, or a
  destination path that exists, raises ``Exists``;
- there must be room for the copy plus 10 %;
- every file is first written into a staging directory at the root of the
  native store (same filesystem, outside the directories the tool lists),
  then moved into place with ``os.replace``, in plan order, so the piece that
  makes the session visible to the tool arrives last and complete;
- on any failure the staging directory, the pieces already in place and the
  directories made for them are removed (a line appended to a native file is
  the one thing that cannot be taken back, so appends come after everything
  that can fail but the final rename of the import record);
- a record of the import is added to ``<state dir>/imports.json``, written to
  a temp file and renamed.

Nothing here imports the provider registry, so adapters can import this
module freely.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from . import __version__
from .errors import ImportFailed
from .fsutil import listdir_ro, open_ro, state_dir
from .model import make_key

CHUNK = 1024 * 1024
HEADROOM = 10            # per cent of the copy size that must stay free on top
PROVENANCE = "imports.json"
PROVENANCE_VERSION = 1
STAGING_PREFIX = ".agent-sessions-import-"
FILE_MODE = 0o600        # sessions are private; never wider than the tools write
DIR_MODE = 0o700


class Exists(ImportFailed):
    """The session id, or a destination path, is already in the native
    store. ``paths`` are what was found. Nothing was written."""

    def __init__(self, paths):
        self.paths = sorted(paths)
        more = f" (and {len(self.paths) - 1} more)" if len(self.paths) > 1 else ""
        super().__init__(
            f"already in the native store: {self.paths[0]}{more}; nothing was overwritten")


class Rewrite:
    """How one file is changed on its way into the native store.

    ``apply`` reads the source and yields the bytes to write; afterwards
    ``changes`` lists what it changed, in plain words. ``label`` says what
    kind of change this is.
    """

    label = ""

    def __init__(self):
        self.changes: list[str] = []

    def apply(self, reader) -> Iterator[bytes]:
        raise NotImplementedError

    def preview(self, src) -> list[str]:
        """What ``apply`` would change in ``src``; writes nothing."""
        try:
            with open_ro(src) as reader:
                for _chunk in self.apply(reader):
                    pass
        except (OSError, ValueError) as exc:
            raise ImportFailed(f"{src} cannot be rewritten: {exc}") from exc
        return list(self.changes)


@dataclass
class FileCopy:
    src: str
    rel: str = ""                    # path below the unit; "" when the unit is this file
    size: int = 0
    rewrite: Rewrite | None = None   # None: byte for byte


@dataclass
class Unit:
    """One file or one directory that appears in the native store in a single
    rename."""

    dst: str
    src: str = ""                    # the source file or directory, for reports
    files: list[FileCopy] = field(default_factory=list)
    is_dir: bool = False
    dirs: list[str] = field(default_factory=list)   # sub-directories, parents first

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class Append:
    """Bytes added to the end of a file the native tool owns."""

    path: str
    data: bytes
    label: str


@dataclass
class ImportPlan:
    provider: str
    session_id: str
    source_key: str
    source_store: str
    source_mount: str
    source_files: list[str]
    native_root: str
    cwd: str | None                  # as recorded
    cwd_local: str                   # where that folder is on this machine
    units: list[Unit] = field(default_factory=list)      # put in place in this order
    appends: list[Append] = field(default_factory=list)  # after the units
    existing: list[str] = field(default_factory=list)    # native paths that hold this id
    info: dict = field(default_factory=dict)             # extra provenance fields

    @property
    def total_bytes(self) -> int:
        return sum(unit.size for unit in self.units)

    @property
    def native_key(self) -> str:
        return make_key(self.provider, self.native_root, self.session_id)

    @property
    def destinations(self) -> list[str]:
        return [unit.dst for unit in self.units]


# --- helpers for adapters ---------------------------------------------------

def start_plan(session, native_root, pathmap) -> ImportPlan:
    """A plan with everything but the copies filled in. Raises ``ImportFailed``
    when the session's project folder is not on any mounted drive: an import
    without it would resume in the wrong place."""
    local, exists = (pathmap.to_local(session.cwd, session.mount)
                     if session.cwd else (None, False))
    if not exists or not local:
        if not session.cwd:
            raise ImportFailed("the project folder of this session is not recorded")
        raise ImportFailed(
            f"the project folder {session.cwd} was not found on any mounted drive")
    return ImportPlan(
        provider=session.provider, session_id=session.id, source_key=session.key,
        source_store=session.store, source_mount=session.mount,
        source_files=list(session.files), native_root=str(native_root),
        cwd=session.cwd, cwd_local=local)


def file_copy(src, rel: str = "", rewrite: Rewrite | None = None) -> FileCopy:
    """A copy of one regular file. Raises ``ImportFailed`` when it is not one."""
    src = os.fspath(src)
    try:
        st = os.lstat(src)
    except OSError as exc:
        raise ImportFailed(f"cannot read {src}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ImportFailed(f"{src} is not a regular file")
    return FileCopy(src, rel, st.st_size, rewrite)


def tree(src_dir, skip=None) -> tuple[list[str], list[FileCopy]]:
    """``(sub-directories, files)`` below ``src_dir``, as relative paths in
    sorted order, parents before children. Symbolic links and special files
    are left out, and so is every file whose name ``skip(name)`` accepts.
    Directories are listed without moving their access time."""
    src_dir = os.fspath(src_dir)
    dirs: list[str] = []
    files: list[FileCopy] = []
    pending = [""]
    while pending:
        rel_dir = pending.pop(0)
        here = os.path.join(src_dir, rel_dir) if rel_dir else src_dir
        try:
            names = sorted(listdir_ro(here))
        except OSError as exc:
            raise ImportFailed(f"cannot read {here}: {exc.strerror or exc}") from exc
        for name in names:
            path = os.path.join(here, name)
            rel = os.path.join(rel_dir, name) if rel_dir else name
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise ImportFailed(f"cannot read {path}: {exc.strerror or exc}") from exc
            if stat.S_ISDIR(st.st_mode):
                dirs.append(rel)
                pending.append(rel)
            elif stat.S_ISREG(st.st_mode) and not (skip and skip(name)):
                files.append(FileCopy(path, rel, st.st_size))
    dirs.sort()
    files.sort(key=lambda f: f.rel)
    return dirs, files


def only_changed(before, after, allowed) -> bool:
    """True when two parsed JSON values are equal once the values at the
    ``allowed`` key paths are left out. A path is a tuple of object keys; a
    list on the way stands for each of its elements."""
    def blank(value, path):
        if not path:
            return None
        if isinstance(value, dict):
            if path[0] not in value:
                return value
            return {k: (blank(v, path[1:]) if k == path[0] else v) for k, v in value.items()}
        if isinstance(value, list):
            return [blank(item, path) for item in value]
        return value

    for path in allowed:
        before, after = blank(before, path), blank(after, path)
    return _text(before) == _text(after)


def _text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --- checks -----------------------------------------------------------------

def human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{count} B"


def _anchor(path: str) -> str:
    """The nearest directory at or above ``path`` that exists."""
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return current


def free_space(plan: ImportPlan) -> tuple[int, int, str]:
    """``(bytes needed, bytes free, where)`` for a plan: the copy plus 10 %."""
    need = plan.total_bytes + (plan.total_bytes * HEADROOM + 99) // 100
    where = _anchor(plan.native_root)
    return need, shutil.disk_usage(where).free, where


def provenance_file() -> str:
    return str(state_dir() / PROVENANCE)


def read_provenance() -> list[dict]:
    """The import records, oldest first; a missing file gives none. Raises
    ``ImportFailed`` when the file is there but unusable: importing would
    lose its history."""
    path = provenance_file()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise ImportFailed(f"the import record {path} cannot be read: {exc}") from exc
    records = data.get("imports") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ImportFailed(f"the import record {path} is not in the expected form")
    return records


def check(plan: ImportPlan) -> None:
    """Everything ``execute`` would refuse, without writing: an id or a
    destination that already exists (``Exists``), too little free space, an
    unusable import record."""
    taken = list(plan.existing) + [dst for dst in plan.destinations if os.path.lexists(dst)]
    if taken:
        raise Exists(taken)
    if not plan.units:
        raise ImportFailed("the session has no files to copy")
    try:
        need, free, where = free_space(plan)
    except OSError as exc:
        raise ImportFailed(f"cannot check free space: {exc.strerror or exc}") from exc
    if free < need:
        raise ImportFailed(
            f"not enough free space on {where}: {human_size(need)} needed "
            f"(the copy plus {HEADROOM} %), {human_size(free)} free")
    read_provenance()


def describe(plan: ImportPlan) -> dict:
    """A plan as plain data, for ``import --dry-run``. Reads the files that
    would be rewritten to say what would change; writes nothing."""
    copies, rewrites = [], []
    for unit in plan.units:
        copies.append({"from": unit.src, "to": unit.dst, "dir": unit.is_dir,
                       "files": len(unit.files), "bytes": unit.size})
        for item in unit.files:
            if item.rewrite is not None:
                target = os.path.join(unit.dst, item.rel) if item.rel else unit.dst
                rewrites.append({"file": target, "what": item.rewrite.label,
                                 "changes": item.rewrite.preview(item.src)})
    need, free, where = free_space(plan)
    return {
        "provider": plan.provider, "id": plan.session_id,
        "source_key": plan.source_key, "native_key": plan.native_key,
        "native_store": plan.native_root, "cwd": plan.cwd, "cwd_local": plan.cwd_local,
        "copies": copies, "rewrites": rewrites,
        "appends": [{"file": a.path, "what": a.label} for a in plan.appends],
        "bytes": plan.total_bytes, "needed": need, "free": free, "free_on": where,
    }


# --- execution --------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_dirs(path: str, made: list[str]) -> None:
    """``mkdir -p``, remembering each directory this call created."""
    missing = []
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        os.mkdir(directory, DIR_MODE)
        made.append(directory)


def _copy(item: FileCopy, target: str, tick) -> None:
    """Write one staged file: the source's bytes, or what its rewrite yields;
    flushed to disk, with the source's modification time."""
    with open_ro(item.src) as reader:
        source = os.fstat(reader.fileno())
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                     FILE_MODE)
        with os.fdopen(fd, "wb") as writer:
            if item.rewrite is not None:
                chunks = item.rewrite.apply(reader)
                while True:
                    try:
                        chunk = next(chunks, None)
                    except ValueError as exc:   # text the rewrite cannot carry over
                        raise ImportFailed(f"{item.src} cannot be rewritten: {exc}") from exc
                    if chunk is None:
                        break
                    writer.write(chunk)
                tick(item.size)
            else:
                while True:
                    chunk = reader.read(CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    tick(len(chunk))
            writer.flush()
            os.fsync(writer.fileno())
    os.utime(target, ns=(time.time_ns(), source.st_mtime_ns))


def _remove(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def _append(item: Append) -> None:
    """Add the bytes in one write; start on a fresh line when the file does
    not end with one."""
    data = item.data
    try:
        with open(item.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    data = b"\n" + data
    except FileNotFoundError:
        pass
    fd = os.open(item.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
                 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _record(plan: ImportPlan) -> dict:
    rewritten = {}
    for unit in plan.units:
        for item in unit.files:
            if item.rewrite is not None:
                target = os.path.join(unit.dst, item.rel) if item.rel else unit.dst
                rewritten[target] = list(item.rewrite.changes)
    record = {
        "imported_at": _now(),
        "provider": plan.provider,
        "id": plan.session_id,
        "source_key": plan.source_key,
        "native_key": plan.native_key,
        "source_store": plan.source_store,
        "source_mount": plan.source_mount,
        "source_files": list(plan.source_files),
        "destination_store": plan.native_root,
        "destination_files": plan.destinations,
        "cwd": plan.cwd,
        "cwd_local": plan.cwd_local,
        "bytes": plan.total_bytes,
        "rewritten": rewritten,
        "appended": [item.path for item in plan.appends],
        "agent_sessions_version": __version__,
    }
    record.update({k: v for k, v in sorted(plan.info.items()) if k not in record})
    return record


def execute(plan: ImportPlan, progress=None) -> dict:
    """Carry out a plan and return its provenance record.

    ``progress(done_bytes, total_bytes)`` is called as the copy advances.
    Raises ``Exists`` or ``ImportFailed``; after a failure nothing of this
    import is left in the native store.
    """
    check(plan)
    records = read_provenance()    # read again below the checks: this is what gets extended
    total = plan.total_bytes
    done = 0

    def tick(count: int) -> None:
        nonlocal done
        done += count
        if progress is not None:
            progress(min(done, total), total)

    made_dirs: list[str] = []    # created by this run, outermost first
    placed: list[str] = []       # units already renamed into the store
    staging = None
    record_tmp = None
    finished = False
    try:
        _make_dirs(plan.native_root, made_dirs)
        staging = os.path.join(plan.native_root,
                               f"{STAGING_PREFIX}{os.getpid()}-{time.time_ns():x}")
        os.mkdir(staging, DIR_MODE)
        staged = []
        for number, unit in enumerate(plan.units):
            target = os.path.join(staging, str(number))
            if unit.is_dir:
                os.mkdir(target, DIR_MODE)
                for rel in unit.dirs:
                    os.makedirs(os.path.join(target, rel), DIR_MODE, exist_ok=True)
                for item in unit.files:
                    _copy(item, os.path.join(target, item.rel), tick)
            else:
                _copy(unit.files[0], target, tick)
            staged.append(target)

        # The record is prepared before anything becomes visible, so the
        # last step that can fail for lack of space is behind us.
        record = _record(plan)
        path = provenance_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record_tmp = f"{path}.{os.getpid()}.tmp"
        with open(record_tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": PROVENANCE_VERSION, "imports": records + [record]}, fh,
                      ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        for unit, target in zip(plan.units, staged):
            _make_dirs(os.path.dirname(unit.dst), made_dirs)
            if os.path.lexists(unit.dst):
                raise Exists([unit.dst])
            os.replace(target, unit.dst)
            placed.append(unit.dst)
        for item in plan.appends:
            _append(item)
        os.replace(record_tmp, path)
        record_tmp = None
        finished = True
        return record
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        raise ImportFailed(f"import failed: {exc.strerror or exc}{where}; "
                           "nothing was left in the native store") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if record_tmp is not None:
            _remove(record_tmp)
        if not finished:
            for path in reversed(placed):
                _remove(path)
            for directory in reversed(made_dirs):
                try:
                    os.rmdir(directory)     # only when nothing else is in it
                except OSError:
                    pass


__all__ = ["Append", "Exists", "FileCopy", "ImportFailed", "ImportPlan", "Rewrite", "Unit",
           "check", "describe", "execute", "file_copy", "free_space", "human_size",
           "only_changed", "provenance_file", "read_provenance", "start_plan", "tree"]
