"""Copy a foreign session into the native store.

An adapter describes an import as an ``ImportPlan`` (``Provider.import_plan``:
which source files go where, what is rewritten on the way, what is appended
to a native file); it reads the source and writes nothing. ``check`` applies
the rules that hold for every provider, ``execute`` carries the plan out:

- the source is only read, through ``fsutil.open_ro`` and ``listdir_ro`` (no
  access time moves on the other drive);
- the session id must be in the form its provider's ids have, and every
  destination must name a place below the native store (``contained``): the
  names in a plan come from the source and are not trusted. Symbolic links
  inside the native store are the user's own and are followed;
- nothing that exists is overwritten: an id already in the native store, or a
  destination path that exists, raises ``Exists``;
- there must be room for the copy plus 10 %;
- every file is first written into a staging directory at the root of the
  native store (same filesystem, outside the directories the tool lists),
  then moved into place in plan order, so the piece that makes the session
  visible to the tool arrives last and complete. The move never replaces
  anything (``_place``), and the directories it changed are flushed to disk;
- once the files are in place the copy is read back the way any session of
  the native store is read, and must be the session that was planned (the
  ``verify`` step of ``execute``);
- on any failure, a failed read-back included, the staging directory, the
  pieces already in place and the directories made for them are removed (a
  line appended to a native file is the one thing that cannot be taken back,
  so appends come after everything that can fail but the one write that adds
  the import record, and the journal is opened before anything is put in
  place);
- a record of the import is added to the journal ``<state dir>/imports.jsonl``
  (``journal``): one line, with a manifest of every file and directory the
  import created, as they lie in the native store. ``unimport`` takes an
  import back from that record;
- all of it happens with the state directory's lock held (``fsutil.lock``),
  so one import runs at a time, whichever process starts it.

Nothing here imports the provider registry, so adapters can import this
module freely.
"""

from __future__ import annotations

import errno
import functools
import json
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, BinaryIO

from . import __version__, fsutil, journal
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
STAGING_PREFIX = ".agent-sessions-import-"
# What ``execute`` names its staging directory: prefix, pid, hex time.
_STAGING_NAME = re.compile(re.escape(STAGING_PREFIX) + r"([0-9]+)-[0-9a-f]+")
STAGING_MAX_AGE = 3600  # seconds; a younger staging directory is never swept
FILE_MODE = 0o600  # sessions are private; never wider than the tools write
DIR_MODE = 0o700
# How many directories deep a path below a unit may lie. The tools nest two
# or three; a directory copied whole from another drive can nest thousands,
# as far as a path of that drive goes.
DEPTH_MAX = 64
# What a path and one name in it may measure, in bytes, where the platform
# does not say (``os.pathconf``).
PATH_MAX = 4096
NAME_MAX = 255

_log = logging.getLogger(__name__)


class Exists(ImportFailed):
    """The session id, or a destination path, is already in the native
    store. ``paths`` are what was found. Nothing was written."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = sorted(set(paths))  # an id's own file can also be a destination
        more = f" (and {len(self.paths) - 1} more)" if len(self.paths) > 1 else ""
        super().__init__(
            f"already in the native store: {self.paths[0]}{more}; nothing was overwritten"
        )


class NoFolder(ImportFailed):
    """The folder the session ran in is not on this machine, or was never
    recorded. Nothing was written. The same import with a folder the user
    chooses (``target``) does not fail this way."""


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
    step (``_place``)."""

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


# What reads the copy a plan describes back from the native store and raises
# when it is not the planned session (``Provider.read_back``).
ReadBack = Callable[["ImportPlan"], object]


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
    # The folder the copy is made for: the chosen one, else where the
    # recorded one is on this machine. None only for an import that needs no
    # folder (``start_plan(needs_folder=False)``) and found none.
    cwd_local: str | None
    units: list[Unit] = field(default_factory=list)  # put in place in this order
    appends: list[Append] = field(default_factory=list)  # after the units
    existing: list[str] = field(default_factory=list)  # native paths that hold this id
    info: dict = field(default_factory=dict)  # extra provenance fields
    # The form ``session_id`` must have (``Provider.id_pattern``); ``check``
    # refuses any other. The default is the strict one, so a plan built
    # without the provider's own pattern is refused rather than let through.
    id_pattern: re.Pattern[str] = UUID_PATTERN
    # The folder the user chose for this import (``chosen_folder``), or None
    # when the recorded one was mapped.
    chosen_folder: str | None = None
    # How the provider reads a copy back from the native store
    # (``Provider.read_back``). ``execute`` needs it for an earlier import
    # into this store that died half way (``recover``): whether what that one
    # left is a whole session is the provider's to say. Not part of what the
    # plan says, so it does not count when two plans are compared.
    read_back: ReadBack | None = field(default=None, compare=False, repr=False)

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


def chosen_folder(target: object) -> str:
    """The folder a user chose to import into, checked and in one spelling
    (``os.path.normpath``: no trailing separator, no doubled one).

    It is the user's own input, not a name from a store, and it must be an
    absolute path of a directory that exists. Raises ``ImportFailed`` for
    anything else: text with a NUL, a relative path (``~`` is the caller's
    to expand), a path that is not there, a file. Reads only. The check is
    ``fsutil.existing_folder``, which the folder a session is continued in
    goes through too."""
    try:
        return fsutil.existing_folder(target, "the folder to import into")
    except ValueError as exc:
        raise ImportFailed(str(exc)) from None


def start_plan(
    session: Session,
    native_root: StrPath,
    pathmap: PathMap,
    id_pattern: re.Pattern[str] = UUID_PATTERN,
    target: str | None = None,
    needs_folder: bool = True,
    read_back: ReadBack | None = None,
) -> ImportPlan:
    """A plan with everything but the copies filled in; ``id_pattern`` and
    ``read_back`` are the provider's (``Provider.id_pattern``,
    ``Provider.read_back``).

    ``target`` is a folder the user chose (``chosen_folder`` checks it); it
    takes the place of the folder the session recorded. Without one, raises
    ``NoFolder`` when the session's project folder is not on any mounted
    drive or was not recorded: an import without it would resume in the
    wrong place. An adapter whose import uses no folder at all passes
    ``needs_folder=False``, and then a folder that is not found is None."""
    chosen = chosen_folder(target) if target is not None else None
    local: str | None = chosen
    if local is None:
        found, exists = (
            pathmap.to_local(session.cwd, session.mount) if session.cwd else (None, False)
        )
        local = found if exists and found else None
    if local is None and needs_folder:
        if not session.cwd:
            raise NoFolder("the project folder of this session is not recorded")
        raise NoFolder(f"the project folder {session.cwd} was not found on any mounted drive")
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
        chosen_folder=chosen,
        read_back=read_back,
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
    Directories are listed without moving their access time.

    Raises ``ImportFailed`` as soon as a directory lies more than
    ``DEPTH_MAX`` levels below ``src_dir``: such a tree is not walked to its
    end (``execute`` refuses the same for a plan made any other way)."""
    src_dir = os.fspath(src_dir)
    dirs: list[str] = []
    files: list[FileCopy] = []
    pending = [""]
    while pending:
        rel_dir = pending.pop(0)
        if _depth(rel_dir) > DEPTH_MAX:
            raise _too_deep(src_dir)
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


def read_journal() -> list[dict]:
    """The import records, oldest first (``journal.read``); none when there
    is no journal. Raises ``ImportFailed`` when the journal is there but
    cannot be read: an import could not add its record to it either."""
    try:
        return journal.read()
    except OSError as exc:
        raise ImportFailed(
            f"the import journal {journal.path()} cannot be read: {exc.strerror or exc}"
        ) from exc


_SEPARATORS = re.compile("[" + re.escape(os.sep + (os.altsep or "")) + "]")


def _below(root: str) -> str:
    return root if root.endswith(os.sep) else root + os.sep


def _relative_ok(rel: str) -> bool:
    """True for a path below a unit that names one place inside it: no
    drive, not absolute, no empty, ``.`` or ``..`` component, no NUL."""
    if not rel or "\0" in rel or os.path.isabs(rel) or os.path.splitdrive(rel)[0]:
        return False
    return not any(part in ("", ".", "..") for part in _SEPARATORS.split(rel))


def _outside(root: str, path: object) -> bool:
    """True for what is not text, holds a NUL or does not start with the
    root and a separator."""
    return not isinstance(path, str) or "\0" in path or not path.startswith(_below(root))


def below(root: str, path: object) -> bool:
    """True when ``path`` names a place below ``root`` by the rule
    ``contained`` holds every destination to: the root plus components none
    of which is empty, ``.`` or ``..``, and no NUL. On the names alone;
    reads nothing. For a caller with one path to check (an undo checks what
    a journal record names before it removes anything)."""
    return (
        isinstance(path, str)
        and not _outside(root, path)
        and _relative_ok(path[len(_below(root)) :])
    )


def contained(plan: ImportPlan) -> None:
    """Refuse a plan that would write anywhere but below its native store.

    The names in a plan come from the source: ids, date directories and
    file names of a store on another drive. Whatever the adapter that built
    the plan checked, every destination is checked again here: each unit,
    each directory and file below a unit, each file appended to. A
    destination must be the native root plus components none of which is
    empty, ``.`` or ``..``, and must hold no NUL.

    The check is on the names, not on where symbolic links lead. The native
    store is the user's own and trusted (docs/SPEC.md "Threat model"): a
    ``~/.claude`` kept in a dotfiles repository, or a ``projects`` directory
    that is a link to another disk, is how that user set it up, and the
    tool itself writes through such a link. Nothing from another store can
    put a link there: an import creates regular files and directories only
    (``tree``, ``file_copy``, ``_copy``).

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
    for path in targets:
        if _outside(root, path):
            raise ImportFailed(f"{path!r} is outside the native store {root}; nothing was imported")
        if not below(root, path):
            raise ImportFailed(f"{path!r} is not a plain path below {root}; nothing was imported")
    for addition in plan.appends:  # a line is only ever added to a plain file
        try:
            mode = os.stat(addition.path).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ImportFailed(f"cannot read {addition.path}: {exc.strerror or exc}") from exc
        if not stat.S_ISREG(mode):
            raise ImportFailed(f"{addition.path} is not a regular file; nothing was imported")


def _depth(rel: str) -> int:
    """How many names a path below a unit has; none for the unit itself."""
    return len(_SEPARATORS.split(rel)) if rel else 0


def _too_deep(what: str) -> ImportFailed:
    return ImportFailed(
        f"{what} holds directories nested more than {DEPTH_MAX} levels deep, and a session "
        "like that is not imported; nothing was imported"
    )


def _limit(where: str, name: str, default: int) -> int:
    """A limit of the filesystem that holds ``where`` (``os.pathconf``);
    ``default`` where the platform has no such call, does not know the name,
    or sets no limit."""
    try:
        value = os.pathconf(where, name)
    except (AttributeError, OSError, ValueError):
        return default
    return value if value > 0 else default


def _bytes(path: str) -> int:
    """The length of a path as the filesystem counts it."""
    try:
        return len(os.fsencode(path))
    except UnicodeEncodeError:  # text no file name decodes to; the copy itself will say so
        return len(path.encode("utf-8", "surrogatepass"))


def _staging_name() -> str:
    """What this process names a staging directory: prefix, pid, hex time."""
    return f"{STAGING_PREFIX}{os.getpid()}-{time.time_ns():x}"


def fits(plan: ImportPlan) -> None:
    """Refuse a plan whose copy nests deeper than ``DEPTH_MAX`` directories
    below a unit, or needs a path or a name longer than the filesystem of
    the native store takes (``os.pathconf``; ``PATH_MAX`` and ``NAME_MAX``
    where it does not say).

    The names and the nesting of a directory that is copied whole come from
    the source. A path that is within the limit on the other drive can pass
    it here, where it lies below another directory; found only when the
    files are written, that fails an import half way, and a tree nested
    thousands deep is more than a roll back can be trusted to remove
    (``shutil.rmtree`` recurses before Python 3.12). Every path is measured
    where it will lie and where it is staged.

    Raises ``ImportFailed``; reads only. ``contained`` comes first: this
    takes the names for text."""
    where = _anchor(plan.native_root)
    path_max = _limit(where, "PC_PATH_MAX", PATH_MAX)
    name_max = _limit(where, "PC_NAME_MAX", NAME_MAX)
    staging = os.path.join(plan.native_root, _staging_name())
    for number, unit in enumerate(plan.units):
        what = unit.src or unit.dst
        inside = [(rel, _depth(rel)) for rel in unit.dirs]
        inside += [(item.rel, _depth(item.rel) - 1) for item in unit.files if item.rel]
        if any(depth > DEPTH_MAX for _rel, depth in inside):
            raise _too_deep(what)
        own = unit.dst[len(_below(plan.native_root)) :]
        rels = ["", *(rel for rel, _levels in inside)]
        longest = max(
            _bytes(os.path.join(base, rel) if rel else base)
            for base in (unit.dst, os.path.join(staging, str(number)))
            for rel in rels
        )
        if longest >= path_max:  # the limit counts the byte that ends the path
            raise ImportFailed(
                f"the copy of {what} would need a path of {longest:,} bytes below "
                f"{plan.native_root}, and at most {path_max - 1:,} fit there; "
                "nothing was imported"
            )
        names = {name for rel in rels for name in _SEPARATORS.split(rel) if name}
        widest = max(map(_bytes, names | set(_SEPARATORS.split(own))))
        if widest > name_max:
            raise ImportFailed(
                f"the copy of {what} would need a file or directory name of {widest:,} "
                f"bytes below {plan.native_root}, and at most {name_max:,} fit there; "
                "nothing was imported"
            )


def _sound(plan: ImportPlan) -> None:
    """The refusals that do not depend on what is in the native store: a
    session id that is not in the form its provider's ids have, a
    destination outside the native store, and a copy that nests too deep or
    needs a path longer than the filesystem takes. ``execute`` makes them
    before it touches anything."""
    # The id becomes file and directory names in the native store and, once
    # the copy is listed, an argument of the tool that resumes it.
    if not isinstance(plan.session_id, str) or not plan.id_pattern.fullmatch(plan.session_id):
        raise ImportFailed(
            "the id of this session is not in the form its tool gives ids; nothing was imported"
        )
    contained(plan)
    fits(plan)


def check(plan: ImportPlan) -> None:
    """Everything ``execute`` would refuse, without writing: a session id
    that is not in the form its provider's ids have, a destination outside
    the native store (``contained``), a copy nested too deep or with a path
    too long for the native store (``fits``), an id or a destination that
    already exists (``Exists``), too little free space, an unreadable
    import journal."""
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
    read_journal()


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
        "chosen_folder": plan.chosen_folder,
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


def _missing(path: str) -> list[str]:
    """The directories at and above ``path`` that are not there, outermost
    first."""
    missing = []
    current = os.path.abspath(path)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return missing[::-1]


def _make_dirs(path: str, made: list[str], mode: int = DIR_MODE) -> None:
    """``mkdir -p``, remembering each directory this call created."""
    for directory in _missing(path):
        # Remembered first: an interrupt between the two steps then costs a
        # failed rmdir in the roll back, not a directory left behind. One
        # that could not be made is not ours and is forgotten again.
        made.append(directory)
        try:
            os.mkdir(directory, mode)
        except OSError:
            made.pop()
            raise


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


# What ``os.link`` fails with where a hard link cannot be made at all, as
# opposed to "the name is taken" (EEXIST).
_NO_LINK = frozenset(
    getattr(errno, name)
    for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EMLINK", "EXDEV")
    if hasattr(errno, name)
)


def _place(target: str, dst: str, is_dir: bool) -> None:
    """Move one staged unit to its place in the native store without
    replacing anything. Raises ``Exists`` when the name is taken.

    A file is hard-linked under its final name and the staged name removed.
    ``link`` fails when the name exists, whatever it is, and the kernel
    decides that in the same step that creates the name: a file that appears
    at the last moment is never overwritten. Only where the filesystem has
    no hard links (FAT, some network mounts) is the name looked at first and
    the file then renamed, which leaves a moment between the two.

    A directory cannot be linked; it is renamed. What ``os.rename`` of a
    directory guarantees: on POSIX it fails when the name is a file, a link
    or a directory with anything in it, and replaces only an *empty*
    directory; on Windows it fails whenever the name exists. So nothing that
    holds data is ever replaced, and the name is looked for just before
    (the standard library has no rename that refuses an empty directory
    too).
    """
    if os.path.lexists(dst):
        raise Exists([dst])
    if is_dir:
        os.rename(target, dst)
        return
    try:
        os.link(target, dst)
    except FileExistsError:
        raise Exists([dst]) from None
    except (OSError, NotImplementedError, AttributeError) as exc:
        if isinstance(exc, OSError) and os.name == "posix" and exc.errno not in _NO_LINK:
            raise
        if os.path.lexists(dst):
            raise Exists([dst]) from None
        os.replace(target, dst)
        return
    os.unlink(target)


def _arrived(target: str, dst: str) -> bool:
    """Whether a move that was interrupted had put the unit in place: the
    name is there, and the staged one is gone or is the same file (linked,
    not yet unlinked). A name that someone else holds leaves the staged unit
    where it was."""
    try:
        there = os.lstat(dst)
    except OSError:
        return False
    try:
        return os.path.samestat(os.lstat(target), there)
    except OSError:
        return True


def _flush_tree(root: str, dirs: Iterable[str]) -> None:
    """Flush a staged directory and the directories below it, deepest first:
    the names of the files in them are part of the copy."""
    for rel in sorted(dirs, reverse=True):
        fsutil.fsync_dir(os.path.join(root, rel))
    fsutil.fsync_dir(root)


def _flush_placed(placed: Iterable[str], made_dirs: Iterable[str]) -> None:
    """Flush every directory in which this import created a name, so that a
    crash right afterwards does not lose a rename or a new directory. Best
    effort (``fsutil.fsync_dir``): not every platform can."""
    made = list(made_dirs)
    changed = {os.path.dirname(path) for path in [*placed, *made]} | set(made)
    for directory in sorted(changed):
        fsutil.fsync_dir(directory)


def _remove(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def _each(*steps: Callable[[], object]) -> None:
    """Run every step, whatever the ones before it did, and then raise the
    first failure, if there was one.

    For a roll back: what one step could not do must not keep the others
    from being tried. ``BaseException``: what stops a step can be an
    interrupt, or a ``RecursionError`` out of ``shutil.rmtree``, as well as
    an error of the filesystem."""
    first: BaseException | None = None
    for step in steps:
        try:
            step()
        except BaseException as exc:  # raised again below, once every step was tried
            if first is None:
                first = exc
    if first is not None:
        raise first


def _append(item: Append) -> None:
    """Add the bytes in one write; start on a fresh line when the file does
    not end with one. A file that is not there yet is created private, like
    everything else an import puts into the store; one that exists keeps its
    mode."""
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
        item.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), FILE_MODE
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _manifest(placed: list[str], made_dirs: list[str]) -> dict:
    """What an import created, read from the native store once its units are
    in place, not taken from the plan: ``dirs`` (every directory, those made
    on the way to a destination included) and ``files`` (each with the size
    and modification time it has there). An undo compares against this."""
    dirs = list(made_dirs)
    files = []
    pending = []
    for dst in placed:
        st = os.lstat(dst)
        if stat.S_ISDIR(st.st_mode):
            dirs.append(dst)
            pending.append(dst)
        else:
            files.append({"path": dst, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    while pending:
        current = pending.pop()
        for name in os.listdir(current):
            path = os.path.join(current, name)
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                dirs.append(path)
                pending.append(path)
            else:
                files.append({"path": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return {"dirs": sorted(dirs), "files": sorted(files, key=lambda item: item["path"])}


def _record(plan: ImportPlan, manifest: dict) -> dict:
    rewritten = {}
    for unit in plan.units:
        for item in unit.files:
            if item.rewrite is not None:
                target = os.path.join(unit.dst, item.rel) if item.rel else unit.dst
                rewritten[target] = list(item.rewrite.changes)
    record = {
        "record": journal.IMPORT,
        "version": journal.VERSION,
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
        "chosen_folder": plan.chosen_folder,
        "bytes": plan.total_bytes,
        "rewritten": rewritten,
        "appended": [item.path for item in plan.appends],
        "manifest": manifest,
        "agent_sessions_version": __version__,
    }
    record.update({k: v for k, v in sorted(plan.info.items()) if k not in record})
    return record


def _begin(plan: ImportPlan, staging: str, made_dirs: Iterable[str]) -> dict:
    """The journal record of an import whose files are staged and whose
    first unit is about to be put in place. It says what is going to appear
    in the native store, for whoever has to settle the import should this
    process die before its last line (``recover``):

    - ``destinations``: every unit, in the order they are placed;
    - ``dirs``: the directories this import made so far, and the ones on
      the way to a destination that are not there yet and that it is going
      to make;
    - ``staging``: its staging directory;
    - ``import``: the import record as it will be, without the manifest:
      everything of it that cannot be worked out from the native store
      later (where the session came from, what was rewritten on the way)."""
    to_make = [
        directory for unit in plan.units for directory in _missing(os.path.dirname(unit.dst))
    ]
    planned = _record(plan, {})
    del planned["manifest"]
    return {
        "record": journal.BEGIN,
        "version": journal.VERSION,
        "begun_at": planned["imported_at"],
        "provider": plan.provider,
        "id": plan.session_id,
        "destination_store": plan.native_root,
        "destinations": plan.destinations,
        "dirs": sorted({*made_dirs, *to_make}),
        "staging": staging,
        "import": planned,
        "agent_sessions_version": __version__,
    }


def _rolled_back(begin: dict, removed: Iterable[str], recovered: bool) -> dict:
    """The journal record of an import that began and was taken out again:
    by the import itself (its own roll back), or, ``recovered``, by a later
    run that found its ``begin`` open."""
    return {
        "record": journal.ROLLBACK,
        "version": journal.VERSION,
        "rolled_back_at": _now(),
        "provider": begin.get("provider"),
        "id": begin.get("id"),
        "destination_store": begin.get("destination_store"),
        "begun_at": begin.get("begun_at"),
        "removed": sorted(removed),
        "recovered": recovered,
        "agent_sessions_version": __version__,
    }


def at_or_above(root: str, path: str) -> bool:
    """True for ``root`` itself and for a directory on the way to it: what
    an import that had to create its native store made besides what lies
    below it."""
    if "\0" in path or not os.path.isabs(path):
        return False
    return path == root or root.startswith(path if path.endswith(os.sep) else path + os.sep)


def _begin_paths(begin: dict, root: str) -> tuple[list[str], list[str], str | None] | None:
    """``(destinations, directories, staging directory)`` of a ``begin``
    record, each checked, or None for a record that cannot be acted on.

    The journal is the user's own file, trusted but possibly malformed.
    Whatever it says, a destination must be a path below the native store by
    the rule of an import's destinations (``below``); a directory must be
    one too, or the store itself or a directory above it (those only ever
    go empty); and the staging directory must lie in the root of the store
    under a name ``execute`` gives one."""
    destinations, dirs, staging = (begin.get(key) for key in ("destinations", "dirs", "staging"))
    if not isinstance(destinations, list) or not destinations or not isinstance(dirs, list):
        return None
    if not all(below(root, path) for path in destinations):
        return None
    if not all(isinstance(d, str) and (below(root, d) or at_or_above(root, d)) for d in dirs):
        return None
    if staging is not None and not (
        isinstance(staging, str)
        and _STAGING_NAME.fullmatch(os.path.basename(staging))
        and staging == os.path.join(root, os.path.basename(staging))
    ):
        return None
    return destinations, dirs, staging


def _reads_back(begin: dict, root: str, destinations: list[str], read_back: ReadBack) -> bool:
    """Whether what lies at the destinations of a ``begin`` reads as the
    session that was to be imported, by the provider's own reading."""
    copy = ImportPlan(
        provider=begin["provider"],
        session_id=begin["id"],
        source_key="",
        source_store="",
        source_mount="",
        source_files=[],
        native_root=root,
        cwd=None,
        cwd_local=None,
        units=[Unit(dst) for dst in destinations],
    )
    try:
        read_back(copy)
    except Exception:  # whatever keeps it from being read: then it is not a whole copy
        return False
    return True


def _take_out(destinations: list[str], dirs: list[str], staging: str | None) -> list[str] | None:
    """Remove what an import that died left: the destinations that are
    there, its staging directory, then the directories it made, deepest
    first and only when empty. Returns what was removed, or None when a
    destination or the staging directory is still there afterwards."""
    removed = []
    for path in [*reversed(destinations), *([staging] if staging else [])]:
        if not os.path.lexists(path):
            continue
        try:
            _remove(path)
        except RecursionError:  # ``shutil.rmtree`` before Python 3.12, a tree nested too deep
            pass
        if os.path.lexists(path):
            return None
        removed.append(path)
    for directory in sorted(set(dirs), key=lambda path: (-path.count(os.sep), path)):
        try:
            os.rmdir(directory)  # only when nothing else is in it
        except OSError:
            continue
        removed.append(directory)
    return removed


def _settle(begin: dict, root: str, read_back: ReadBack | None) -> dict | None:
    """Settle one import that began and never ended (``recover``); returns
    the record written for it, or None when it could not be settled now."""
    whose = f"{begin.get('provider')} {begin.get('id')} into {root}"
    paths = _begin_paths(begin, root)
    if paths is None:
        # Nothing it names can be trusted, so nothing is removed for it; the
        # line that closes it keeps it from being looked at again.
        _log.warning(
            "the journal's line for an unfinished import of %s cannot be used (it does not "
            "name plain paths below that store); nothing was removed for it",
            whose,
        )
        record = dict(_rolled_back(begin, [], recovered=True), damaged=True)
    else:
        destinations, dirs, staging = paths
        whole = all(os.path.lexists(path) for path in destinations)
        if whole and read_back is None:
            _log.warning(
                "an import of %s did not finish, and whether its copy is whole cannot be "
                "told here; it is left as it is",
                whose,
            )
            return None
        if whole and read_back is not None and _reads_back(begin, root, destinations, read_back):
            if staging is not None and os.path.lexists(staging):
                _remove(staging)  # what is left of it is the import's own, and empty
            planned = begin.get("import")
            try:
                made = [path for path in dirs if os.path.isdir(path) and not os.path.islink(path)]
                manifest = _manifest(destinations, made)
            except OSError as exc:
                _log.warning(
                    "an import of %s did not finish; its copy is whole but could not be "
                    "listed (%s), so it is not recorded yet",
                    whose,
                    exc,
                )
                return None
            record = dict(planned if isinstance(planned, dict) else {})
            record.setdefault("imported_at", begin.get("begun_at"))
            record.update(
                {
                    "record": journal.IMPORT,
                    "version": journal.VERSION,
                    "provider": begin["provider"],
                    "id": begin["id"],
                    "destination_store": root,
                    "destination_files": destinations,
                    "manifest": manifest,
                    "recovered": True,
                    "recovered_at": _now(),
                }
            )
        else:
            removed = _take_out(destinations, dirs, staging)
            if removed is None:
                _log.warning(
                    "an import of %s did not finish, and what it left could not be removed; "
                    "it will be tried again",
                    whose,
                )
                return None
            record = _rolled_back(begin, removed, recovered=True)
    try:
        journal.append(record)
    except OSError as exc:
        _log.warning(
            "an import of %s did not finish and was settled, but the journal could not "
            "take the line that says so (%s); it will be settled again",
            whose,
            exc,
        )
        return None
    if record["record"] == journal.IMPORT:
        _log.info("an import of %s had not finished: its copy is whole and is recorded now", whose)
    else:
        _log.info(
            "an import of %s had not finished: what it left was removed (%d files and directories)",
            whose,
            len(record["removed"]),
        )
    return record


def _open_begins(root: str) -> list[dict]:
    """The imports into this native store that began and never ended."""
    try:
        records = journal.read(quiet=True)
    except OSError:  # an import or an undo that follows reports a journal it cannot read
        return []
    return [begin for begin in journal.unsettled(records) if begin.get("destination_store") == root]


def recover(
    native_root: StrPath, read_back: ReadBack | None = None, timeout: float | None = None
) -> list[dict]:
    """Settle the imports into this native store that began and never ended
    (``journal.unsettled``); returns the records written for them.

    Such an import was killed, or the machine went down, between its
    ``begin`` line and its last one; or its own roll back could not finish.
    What it had put in place lies in the store with no record of an import:
    it could not be imported again (the names are taken) and not be undone
    (nothing says it was imported). Each one is settled in one of two ways:

    - forward, when every destination its ``begin`` names is there and
      ``read_back`` reads the copy as the session that was planned: the
      ``import`` record is written now, marked ``recovered``, with a
      manifest of the copy as it lies in the store at this moment. A copy
      that reads back whole is never removed: it was listed as a session of
      the tool all along, and its user may have worked in it;
    - back, in every other case: the destinations that are there and the
      staging directory are removed, then the directories the import made,
      when empty, and a ``rollback`` record is written.

    Without a ``read_back`` an import whose destinations are all there is
    left as it is: whether it is whole cannot be told.

    Runs with the state directory's lock held: taken here, and a caller that
    holds it already may call this (``execute`` and ``unimport.undo`` do, as
    their first step). An import holds that lock from its first check to its
    last line, so whoever has it knows that no ``begin`` of this state
    directory is still being worked on. ``timeout`` is ``fsutil.lock``'s.

    Raises ``OSError`` when the lock cannot be taken (``fsutil.LockTimeout``
    when someone keeps it), and nothing else. What cannot be settled now (a
    journal that cannot be written, a copy that will not go) stays open, is
    said in the log, and is tried again by the next call."""
    root = os.fspath(native_root)
    if not _open_begins(root):
        return []
    settled = []
    with fsutil.lock(timeout=timeout):
        for begin in _open_begins(root):  # again: read with the lock held
            record = _settle(begin, root, read_back)
            if record is not None:
                settled.append(record)
    return settled


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


def _drop_if_empty(path: StrPath) -> None:
    """Remove a journal this import created and wrote nothing to."""
    try:
        if os.path.getsize(path) == 0:
            os.unlink(path)
    except OSError:
        pass


def execute(
    plan: ImportPlan,
    progress: Progress | None = None,
    verify: Callable[[], object] | None = None,
) -> dict:
    """Carry out a plan and return its record in the journal.

    The order is: settle what an earlier import into this store left
    unfinished (``recover``), check, stage every file, write the ``begin``
    record, move the units into place, ``verify``, append, write the
    ``import`` record. ``verify()`` is the caller's check that what now lies
    in the native store reads back as the session that was planned
    (``Provider.import_session`` passes one); it raises when it does not.
    Then, as after any other failure, everything this import put in place is
    taken out again, the directories it made included: no line is appended,
    and the journal gets a ``rollback`` record in place of the ``import``.

    The ``begin`` record is written, and flushed to disk, before the first
    unit appears in the store. A process that is killed after that leaves a
    line that says what it was doing, and the next import or undo of this
    store settles it.

    All of it runs with the state directory's lock held, so two imports
    never run at once: the second waits (``fsutil.LOCK_TIMEOUT``), and when
    it is its turn it sees what the first one left.

    ``progress(done_bytes, total_bytes)`` is called as the copy advances.
    Raises ``Exists`` or ``ImportFailed``; after a failure nothing of this
    import is left in the native store. An import that failed before it put
    anything in place leaves nothing in the state directory either; one
    that failed later leaves its two lines in the journal.
    """
    _sound(plan)
    state_made: list[str] = []  # the state directory, when this run created it
    finished = False
    try:
        fsutil.make_private_dir(state_dir(), state_made)
        with fsutil.lock():
            record = _carry_out(plan, progress, verify)
        finished = True
        return record
    except fsutil.LockTimeout as exc:
        who = f"process {exc.pid}" if exc.pid is not None else "another process"
        raise ImportFailed(
            f"another import is still running ({who}); nothing was imported"
        ) from exc
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        raise ImportFailed(
            f"import failed: {exc.strerror or exc}{where}; nothing was left in the native store"
        ) from exc
    finally:
        if not finished:
            for directory in reversed(state_made):
                try:
                    os.rmdir(directory)  # only when nothing else is in it
                except OSError:
                    pass


@dataclass
class _Run:
    """What one import has done so far: what its end has to tidy up, and a
    roll back has to take out again (``_wind_up``)."""

    made_dirs: list[str] = field(default_factory=list)  # created by this run, outermost first
    placed: list[str] = field(default_factory=list)  # units already moved into the store
    moving: tuple[str, str] | None = None  # (staged, destination) of the move under way
    staging: str | None = None
    record_fd: int | None = None
    record_new: bool = False  # this run created the journal
    begin: dict | None = None  # its ``begin`` record, once the journal has it


def _wind_up(run: _Run, finished: bool) -> None:
    """The end of an import, however it ended. One that finished gives up
    its staging directory and its descriptor of the journal. One that did
    not is rolled back: the staging directory, the units already in place
    and the directories made for them are removed, and when the journal has
    its ``begin``, a ``rollback`` record closes it.

    The steps do not depend on each other (``_each``): one that fails does
    not keep the rest from being tried, and the first failure is raised when
    all of them are done."""

    def arrived() -> None:
        # An interrupt between a move and the line that notes it: asked
        # while the staged unit can still tell whose the name is.
        if run.moving is not None and run.moving[1] not in run.placed and _arrived(*run.moving):
            run.placed.append(run.moving[1])

    def staging() -> None:
        if run.staging is not None:
            shutil.rmtree(run.staging, ignore_errors=True)

    def units() -> None:
        _each(*(functools.partial(_remove, path) for path in reversed(run.placed)))

    def directories() -> None:
        for directory in reversed(run.made_dirs):
            try:
                os.rmdir(directory)  # only when nothing else is in it
            except OSError:
                pass

    def rolled_back() -> None:
        # Said only when it is true. While anything of this import is still
        # there, its ``begin`` stays open, and the next import or undo of
        # this store finishes the job (``recover``).
        if run.begin is None or run.record_fd is None:
            return
        if any(os.path.lexists(path) for path in [*run.placed, run.staging] if path):
            return
        gone = [path for path in [*run.placed, *run.made_dirs] if not os.path.lexists(path)]
        try:
            journal.write(run.record_fd, _rolled_back(run.begin, gone, recovered=False))
        except OSError:
            pass  # the same: left open, and closed by ``recover`` once the journal takes a line

    def journal_closed() -> None:
        if run.record_fd is not None:
            os.close(run.record_fd)
        if run.record_new and not finished:
            _drop_if_empty(journal.path())

    if finished:
        _each(staging, journal_closed)
    else:
        _each(arrived, staging, units, directories, rolled_back, journal_closed)


def _carry_out(
    plan: ImportPlan, progress: Progress | None, verify: Callable[[], object] | None
) -> dict:
    """``execute`` below the lock: every step, and the roll back of a failed
    one, before anyone else can look."""
    if recover(plan.native_root, plan.read_back):
        # What it took out may be what the plan found in the store when it was made.
        plan.existing = [path for path in plan.existing if os.path.lexists(path)]
    sweep_staging(plan.native_root)
    check(plan)
    total = plan.total_bytes
    done = 0

    def tick(count: int) -> None:
        nonlocal done
        done += count
        if progress is not None:
            progress(min(done, total), total)

    run = _Run()
    made_dirs, placed = run.made_dirs, run.placed
    finished = False
    try:
        _make_dirs(plan.native_root, made_dirs)
        staging = run.staging = os.path.join(plan.native_root, _staging_name())
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
                _flush_tree(target, unit.dirs)
            else:
                _copy(unit.files[0], target, tick)
            staged.append(target)

        # The journal is opened before anything becomes visible: what can
        # still fail for it after the appends is one small write. And it is
        # told what is about to appear in the store, on disk before the
        # first unit is: from here on a process that dies has left a line to
        # go by (``recover``).
        run.record_fd, run.record_new = journal.open_for_append()
        begin = _begin(plan, staging, made_dirs)
        journal.write(run.record_fd, begin)
        run.begin = begin
        if run.record_new:
            fsutil.fsync_dir(os.path.dirname(journal.path()))

        for unit, target in zip(plan.units, staged, strict=False):
            _make_dirs(os.path.dirname(unit.dst), made_dirs)
            run.moving = (target, unit.dst)
            try:
                _place(target, unit.dst, unit.is_dir)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # A link inside the native store that leads to another disk:
                # the copy was staged on this one and cannot be moved over
                # in one step.
                raise ImportFailed(
                    f"{os.path.dirname(unit.dst)} is on another filesystem than the native "
                    f"store {plan.native_root} (a symbolic link or a mount leads there), and "
                    "a copy cannot be moved across in one step; nothing was left in the "
                    "native store"
                ) from exc
            placed.append(unit.dst)
        run.moving = None
        _flush_placed(placed, made_dirs)
        manifest = _manifest(placed, made_dirs)
        if verify is not None:
            try:
                verify()
            except ImportFailed:
                raise
            except Exception as exc:  # whatever stopped the copy from being read back
                raise ImportFailed(
                    f"the copy could not be read back ({type(exc).__name__}: {exc}); it was "
                    "removed again and nothing was left in the native store"
                ) from exc
        for addition in plan.appends:
            _append(addition)
        record = _record(plan, manifest)
        journal.write(run.record_fd, record)
        finished = True
        return record
    finally:
        _wind_up(run, finished)


__all__ = [
    "Append",
    "Exists",
    "FileCopy",
    "ImportFailed",
    "ImportPlan",
    "NoFolder",
    "ReadBack",
    "Rewrite",
    "Unit",
    "at_or_above",
    "below",
    "check",
    "chosen_folder",
    "contained",
    "describe",
    "execute",
    "file_copy",
    "fits",
    "free_space",
    "human_size",
    "only_changed",
    "read_journal",
    "recover",
    "start_plan",
    "sweep_staging",
    "tree",
]
