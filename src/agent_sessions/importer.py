"""Copy a foreign session into the native store.

An adapter describes an import as an ``ImportPlan`` (``Provider.import_plan``:
which source files go where, what is rewritten on the way, what is appended
to a native file); it reads the source and writes nothing. ``check`` applies
the rules that hold for every provider, ``execute`` carries the plan out:

- the source is only read, through ``fsutil.open_ro`` and ``listdir_ro`` (no
  access time moves on the other drive);
- the session id must be in the form its provider's ids have, and every
  destination must lie below the native store (``contained``): the names in
  a plan come from the source and are not trusted;
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
import re
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, BinaryIO

from . import __version__
from .errors import ImportFailed
from .fsutil import listdir_ro, open_ro, state_dir
from .model import UUID_PATTERN, make_key

if TYPE_CHECKING:
    from .fsutil import StrPath
    from .model import Session
    from .pathmap import PathMap

Progress = Callable[[int, int], None]  # progress(done_bytes, total_bytes)

CHUNK = 1024 * 1024
HEADROOM = 10  # per cent of the copy size that must stay free on top
PROVENANCE = "imports.json"
PROVENANCE_VERSION = 1
STAGING_PREFIX = ".agent-sessions-import-"
# What ``execute`` names its staging directory: prefix, pid, hex time.
_STAGING_NAME = re.compile(re.escape(STAGING_PREFIX) + r"([0-9]+)-[0-9a-f]+")
STAGING_MAX_AGE = 3600  # seconds; a younger staging directory is never swept
FILE_MODE = 0o600  # sessions are private; never wider than the tools write
DIR_MODE = 0o700


class Exists(ImportFailed):
    """The session id, or a destination path, is already in the native
    store. ``paths`` are what was found. Nothing was written."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = sorted(paths)
        more = f" (and {len(self.paths) - 1} more)" if len(self.paths) > 1 else ""
        super().__init__(
            f"already in the native store: {self.paths[0]}{more}; nothing was overwritten"
        )


class Rewrite:
    """How one file is changed on its way into the native store.

    ``apply`` reads the source and yields the bytes to write; afterwards
    ``changes`` lists what it changed, in plain words. ``label`` says what
    kind of change this is.
    """

    label = ""

    def __init__(self) -> None:
        self.changes: list[str] = []

    def apply(self, reader: BinaryIO) -> Iterator[bytes]:
        raise NotImplementedError

    def preview(self, src: StrPath) -> list[str]:
        """What ``apply`` would change in ``src``; writes nothing."""
        try:
            with open_ro(src) as reader:
                for _chunk in self.apply(reader):
                    pass
        except (OSError, ValueError, RecursionError) as exc:
            raise ImportFailed(f"{src} cannot be rewritten: {exc}") from exc
        return list(self.changes)


@dataclass
class FileCopy:
    src: str
    rel: str = ""  # path below the unit; "" when the unit is this file
    size: int = 0
    rewrite: Rewrite | None = None  # None: byte for byte


@dataclass
class Unit:
    """One file or one directory that appears in the native store in a single
    rename."""

    dst: str
    src: str = ""  # the source file or directory, for reports
    files: list[FileCopy] = field(default_factory=list)
    is_dir: bool = False
    dirs: list[str] = field(default_factory=list)  # sub-directories, parents first

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
    cwd: str | None  # as recorded
    cwd_local: str  # where that folder is on this machine
    units: list[Unit] = field(default_factory=list)  # put in place in this order
    appends: list[Append] = field(default_factory=list)  # after the units
    existing: list[str] = field(default_factory=list)  # native paths that hold this id
    info: dict = field(default_factory=dict)  # extra provenance fields
    # The form ``session_id`` must have (``Provider.id_pattern``); ``check``
    # refuses any other. The default is the strict one, so a plan built
    # without the provider's own pattern is refused rather than let through.
    id_pattern: re.Pattern[str] = UUID_PATTERN

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


def start_plan(
    session: Session,
    native_root: StrPath,
    pathmap: PathMap,
    id_pattern: re.Pattern[str] = UUID_PATTERN,
) -> ImportPlan:
    """A plan with everything but the copies filled in; ``id_pattern`` is
    the provider's. Raises ``ImportFailed`` when the session's project folder
    is not on any mounted drive: an import without it would resume in the
    wrong place."""
    local, exists = pathmap.to_local(session.cwd, session.mount) if session.cwd else (None, False)
    if not exists or not local:
        if not session.cwd:
            raise ImportFailed("the project folder of this session is not recorded")
        raise ImportFailed(f"the project folder {session.cwd} was not found on any mounted drive")
    return ImportPlan(
        provider=session.provider,
        session_id=session.id,
        source_key=session.key,
        source_store=session.store,
        source_mount=session.mount,
        source_files=list(session.files),
        native_root=str(native_root),
        cwd=session.cwd,
        cwd_local=local,
        id_pattern=id_pattern,
    )


def file_copy(src: StrPath, rel: str = "", rewrite: Rewrite | None = None) -> FileCopy:
    """A copy of one regular file. Raises ``ImportFailed`` when it is not one."""
    src = os.fspath(src)
    try:
        st = os.lstat(src)
    except OSError as exc:
        raise ImportFailed(f"cannot read {src}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ImportFailed(f"{src} is not a regular file")
    return FileCopy(src, rel, st.st_size, rewrite)


def tree(
    src_dir: StrPath, skip: Callable[[str], bool] | None = None
) -> tuple[list[str], list[FileCopy]]:
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


def only_changed(before: object, after: object, allowed: Iterable[tuple[str, ...]]) -> bool:
    """True when two parsed JSON values are equal once the values at the
    ``allowed`` key paths are left out. A path is a tuple of object keys; a
    list on the way stands for each of its elements."""

    def blank(value: object, path: tuple[str, ...]) -> object:
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


def _text(value: object) -> str:
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
    except (OSError, ValueError, RecursionError) as exc:
        raise ImportFailed(f"the import record {path} cannot be read: {exc}") from exc
    records = data.get("imports") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ImportFailed(f"the import record {path} is not in the expected form")
    return records


_SEPARATORS = re.compile("[" + re.escape(os.sep + (os.altsep or "")) + "]")


def _below(root: str) -> str:
    return root if root.endswith(os.sep) else root + os.sep


def _relative_ok(rel: str) -> bool:
    """True for a path below a unit that names one place inside it: no
    drive, not absolute, no empty, ``.`` or ``..`` component, no NUL."""
    if not rel or "\0" in rel or os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
        return False
    return not any(part in ("", ".", "..") for part in _SEPARATORS.split(rel))


def contained(plan: ImportPlan) -> None:
    """Refuse a plan that would write anywhere but below its native store.

    The names in a plan come from the source: ids, date directories and
    file names of a store on another drive. Whatever the adapter that built
    the plan checked, every destination is checked again here: each unit,
    each directory and file below a unit, each file appended to. A
    destination must be the native root plus components none of which is
    empty, ``.`` or ``..``, must hold no NUL, and must still be below the
    native root once the symbolic links on the way are resolved (the links
    in the part of the path that exists; the native root itself may be a
    link, as a home directory kept in a dotfiles repository has it).

    Raises ``ImportFailed``; reads only.
    """
    root = plan.native_root
    if not root or "\0" in root or not os.path.isabs(root):
        raise ImportFailed(f"the native store {root!r} is not an absolute path")
    targets: list[str] = []
    for unit in plan.units:
        targets.append(unit.dst)
        if unit.is_dir:
            for rel in unit.dirs + [item.rel for item in unit.files]:
                if not isinstance(rel, str) or not _relative_ok(rel):
                    raise ImportFailed(
                        f"{rel!r} is not a path below {unit.dst}; nothing was imported"
                    )
                targets.append(os.path.join(unit.dst, rel))
        elif len(unit.files) != 1 or unit.files[0].rel or unit.dirs:
            raise ImportFailed(f"{unit.dst} is planned as one file but is not one")
    targets += [addition.path for addition in plan.appends]
    real_root = os.path.realpath(root)
    for path in targets:
        if not isinstance(path, str) or "\0" in path or not path.startswith(_below(root)):
            raise ImportFailed(f"{path!r} is outside the native store {root}; nothing was imported")
        if not _relative_ok(path[len(_below(root)) :]):
            raise ImportFailed(f"{path!r} is not a plain path below {root}; nothing was imported")
        real = os.path.realpath(path)
        if not real.startswith(_below(real_root)):
            raise ImportFailed(
                f"{path} leads out of the native store {root} through a symbolic link; "
                "nothing was imported"
            )
    for addition in plan.appends:  # a line is only ever added to a plain file
        try:
            mode = os.stat(addition.path).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ImportFailed(f"cannot read {addition.path}: {exc.strerror or exc}") from exc
        if not stat.S_ISREG(mode):
            raise ImportFailed(f"{addition.path} is not a regular file; nothing was imported")


def _sound(plan: ImportPlan) -> None:
    """The refusals that do not depend on what is in the native store: a
    session id that is not in the form its provider's ids have, and a
    destination outside the native store. ``execute`` makes them before it
    touches anything."""
    # The id becomes file and directory names in the native store and, once
    # the copy is listed, an argument of the tool that resumes it.
    if not isinstance(plan.session_id, str) or not plan.id_pattern.fullmatch(plan.session_id):
        raise ImportFailed(
            "the id of this session is not in the form its tool gives ids; nothing was imported"
        )
    contained(plan)


def check(plan: ImportPlan) -> None:
    """Everything ``execute`` would refuse, without writing: a session id
    that is not in the form its provider's ids have, a destination outside
    the native store (``contained``), an id or a destination that already
    exists (``Exists``), too little free space, an unusable import record."""
    _sound(plan)
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
            f"(the copy plus {HEADROOM} %), {human_size(free)} free"
        )
    read_provenance()


def describe(plan: ImportPlan) -> dict:
    """A plan as plain data, for ``import --dry-run``. Reads the files that
    would be rewritten to say what would change; writes nothing."""
    copies, rewrites = [], []
    for unit in plan.units:
        copies.append(
            {
                "from": unit.src,
                "to": unit.dst,
                "dir": unit.is_dir,
                "files": len(unit.files),
                "bytes": unit.size,
            }
        )
        for item in unit.files:
            if item.rewrite is not None:
                target = os.path.join(unit.dst, item.rel) if item.rel else unit.dst
                rewrites.append(
                    {
                        "file": target,
                        "what": item.rewrite.label,
                        "changes": item.rewrite.preview(item.src),
                    }
                )
    need, free, where = free_space(plan)
    return {
        "provider": plan.provider,
        "id": plan.session_id,
        "source_key": plan.source_key,
        "native_key": plan.native_key,
        "native_store": plan.native_root,
        "cwd": plan.cwd,
        "cwd_local": plan.cwd_local,
        "copies": copies,
        "rewrites": rewrites,
        "appends": [{"file": a.path, "what": a.label} for a in plan.appends],
        "bytes": plan.total_bytes,
        "needed": need,
        "free": free,
        "free_on": where,
    }


# --- execution --------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _copy(item: FileCopy, target: str, tick: Callable[[int], None]) -> None:
    """Write one staged file: the source's bytes, or what its rewrite yields;
    flushed to disk, with the source's modification time."""
    with open_ro(item.src) as reader:
        source = os.fstat(reader.fileno())
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), FILE_MODE
        )
        with os.fdopen(fd, "wb") as writer:
            if item.rewrite is not None:
                chunks = item.rewrite.apply(reader)
                while True:
                    try:
                        chunk = next(chunks, None)
                    except (ValueError, RecursionError) as exc:
                        # text the rewrite cannot carry over, or JSON nested too deeply
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
    fd = os.open(
        item.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o644
    )
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


def _pid_alive(pid: int) -> bool:
    """Whether a process with this id exists. Where that cannot be asked
    without side effects (signal 0 is not a probe on Windows) the answer is
    yes, so nothing is swept there."""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:  # not a process id on this system
        return False
    except OSError:  # it exists and belongs to someone else
        return True
    return True


def sweep_staging(native_root: StrPath) -> list[str]:
    """Remove the staging directories an import that died left at the root
    of a native store; returns the paths it removed.

    Only a real directory whose name is exactly what ``execute`` gives its
    staging directory, whose process is gone and which was last changed over
    an hour ago. Anything that cannot be listed or removed is left alone."""
    native_root = os.fspath(native_root)
    removed: list[str] = []
    try:
        names = sorted(os.listdir(native_root))
    except OSError:
        return removed
    now = time.time()
    for name in names:
        match = _STAGING_NAME.fullmatch(name)
        if not match:
            continue
        path = os.path.join(native_root, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime <= STAGING_MAX_AGE:
            continue
        if _pid_alive(int(match.group(1))):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            removed.append(path)
    return removed


def execute(plan: ImportPlan, progress: Progress | None = None) -> dict:
    """Carry out a plan and return its provenance record.

    ``progress(done_bytes, total_bytes)`` is called as the copy advances.
    Raises ``Exists`` or ``ImportFailed``; after a failure nothing of this
    import is left in the native store.
    """
    _sound(plan)
    sweep_staging(plan.native_root)
    check(plan)
    records = read_provenance()  # read again below the checks: this is what gets extended
    total = plan.total_bytes
    done = 0

    def tick(count: int) -> None:
        nonlocal done
        done += count
        if progress is not None:
            progress(min(done, total), total)

    made_dirs: list[str] = []  # created by this run, outermost first
    placed: list[str] = []  # units already renamed into the store
    staging = None
    record_tmp = None
    finished = False
    try:
        _make_dirs(plan.native_root, made_dirs)
        staging = os.path.join(
            plan.native_root, f"{STAGING_PREFIX}{os.getpid()}-{time.time_ns():x}"
        )
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
            json.dump(
                {"version": PROVENANCE_VERSION, "imports": records + [record]},
                fh,
                ensure_ascii=False,
                indent=1,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        for unit, target in zip(plan.units, staged, strict=False):
            _make_dirs(os.path.dirname(unit.dst), made_dirs)
            if os.path.lexists(unit.dst):
                raise Exists([unit.dst])
            os.replace(target, unit.dst)
            placed.append(unit.dst)
        for addition in plan.appends:
            _append(addition)
        os.replace(record_tmp, path)
        record_tmp = None
        finished = True
        return record
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        raise ImportFailed(
            f"import failed: {exc.strerror or exc}{where}; nothing was left in the native store"
        ) from exc
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
                    os.rmdir(directory)  # only when nothing else is in it
                except OSError:
                    pass


__all__ = [
    "Append",
    "Exists",
    "FileCopy",
    "ImportFailed",
    "ImportPlan",
    "Rewrite",
    "Unit",
    "check",
    "contained",
    "describe",
    "execute",
    "file_copy",
    "free_space",
    "human_size",
    "only_changed",
    "provenance_file",
    "read_provenance",
    "start_plan",
    "sweep_staging",
    "tree",
]
