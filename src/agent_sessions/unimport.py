"""Take an import back: remove what one import put into the native store.

An import leaves a record in the journal with a manifest: every file it
created, with the size and modification time it had there, and every
directory it made (``importer``). ``undo`` removes exactly that, and nothing
it cannot account for:

- the record used is the last import of that provider and session id that
  no later ``undo`` record took back (``find_record``);
- an import into the same native store that began and never ended (its
  process was killed) is settled first, with the lock held
  (``importer.recover``): a copy that is whole gets its record and can then
  be undone like any other, anything less is taken out;
- with the state directory's lock held, every file of the manifest is
  compared with the disk: it must be there, a regular file, of the recorded
  size and modification time. Any difference means the session was used
  since the import, and the undo is refused, naming what differs, unless it
  is forced;
- the manifest's files are removed, then its directories, deepest first and
  only when nothing is left in them. No directory is ever removed with what
  it holds: a file this import did not create stays, and so does every
  directory above it; each is reported with the reason;
- a line the import added to a file the tool owns (Codex's
  ``session_index.jsonl``) is left in place and reported;
- what the tool creates beside an imported session once it has opened it
  (``Provider.undo_extras``; Antigravity's ``-wal``, ``-shm`` and lock file)
  is removed only when the undo is forced, and reported otherwise;
- the journal is the user's own file, trusted but possibly malformed. Every
  file to be removed must name a place below the native store by the rule an
  import's destinations are held to (``importer.below``), whatever the
  record says, and the record must be for this native store. Of the
  directories, the native store itself and the ones above it may be listed
  too (an import that had to make them recorded them): those are only ever
  removed empty, like every other;
- a record from before imports listed what they created has no manifest. It
  can only be undone when forced, and then only the files and directories it
  names that carry the session's id in their own name are removed;
- the undo is itself a record in the journal: what was removed, what was
  left and why, forced or not.

``plan`` reads only, and is what a dry run reports. Nothing here imports the
provider registry.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import __version__, fsutil, importer, journal
from .errors import REMEDY_FORCE, UndoFailed
from .journal import find_record, not_undone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .fsutil import StrPath

DIFFERENCES_SHOWN = 5  # how many differences a refusal names

# Why something is left where it is.
APPENDED = "a line was added to this file of the tool's; it is left as it is"
EXTRA = "made by the tool after the import; removed only by a forced undo"
NOT_EMPTY = "not empty: it holds something this import did not create"
NOT_NAMED = "not named after the session; a record without a manifest cannot say more"

NO_RECORD = "no import of this session is recorded; nothing was removed"
# An import of the session began and never ended (``journal.unsettled``).
UNFINISHED = (
    "an import of this session did not finish and is not settled yet: the next import or "
    "undo settles it. Nothing was removed"
)
TAKEN_OUT = (
    "an import of this session had not finished: what it left in the native store was "
    "removed, and no import of it is recorded"
)
NOT_SETTLED = (
    "an import of this session did not finish, and what it left could not be settled (the "
    "log says why); nothing was removed"
)
LEGACY = (
    "this import was recorded before imports listed what they created, so what was "
    "added since cannot be told; it can only be undone by force. Nothing was removed"
)


@dataclass
class UndoPlan:
    """What an undo would do; ``plan`` makes one and reads only."""

    provider: str
    session_id: str
    native_root: str
    record: dict  # the import record that is taken back
    forced: bool
    files: list[str] = field(default_factory=list)  # to remove, in ascending order
    dirs: list[str] = field(default_factory=list)  # to remove, deepest first
    left: list[tuple[str, str]] = field(default_factory=list)  # (path, why), by path
    differences: list[str] = field(default_factory=list)  # from the record; forced past
    bytes: int = 0  # of the files to remove


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_records() -> list[dict]:
    """The journal, oldest first. Raises ``UndoFailed`` when it is there and
    cannot be read."""
    try:
        return journal.read()
    except OSError as exc:
        raise UndoFailed(
            f"the import journal {journal.path()} cannot be read: {exc.strerror or exc}; "
            "nothing was removed"
        ) from exc


def _manifest(record: dict, root: str) -> tuple[list[dict], list[str]]:
    """``(files, directories)`` of a record's manifest, each checked: the
    right types, every file below the native root, every directory below it,
    the root itself or one above it. Raises ``UndoFailed`` otherwise; an
    undo does not act on part of a record it cannot read whole."""
    manifest = record.get("manifest")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    dirs = manifest.get("dirs") if isinstance(manifest, dict) else None
    damaged = UndoFailed("the import record is damaged (its manifest); nothing was removed")
    if not isinstance(files, list) or not isinstance(dirs, list):
        raise damaged
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise damaged
        for key in ("size", "mtime_ns"):
            if not isinstance(item.get(key), int) or isinstance(item.get(key), bool):
                raise damaged
        if not importer.below(root, item["path"]):
            raise _outside(item["path"], root)
    for path in dirs:
        if not isinstance(path, str):
            raise damaged
        if not importer.below(root, path) and not importer.at_or_above(root, path):
            raise _outside(path, root)
    return files, dirs


def _outside(path: object, root: str) -> UndoFailed:
    return UndoFailed(
        f"the import record names {path!r}, which is not a plain path below the native "
        f"store {root}; nothing was removed"
    )


def _difference(item: dict) -> str | None:
    """How a file differs from what the record says of it, or None."""
    path = item["path"]
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} is gone"
    except OSError as exc:
        return f"{path} cannot be read ({exc.strerror or exc})"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file any more"
    if st.st_size != item["size"]:
        return f"{path} was {item['size']:,} bytes and is {st.st_size:,}"
    if st.st_mtime_ns != item["mtime_ns"]:
        return f"{path} was modified"
    return None


def _removable(path: str, going: dict[str, int], left: list[tuple[str, str]]) -> None:
    """Sort one path into what is removed (``going``: path -> its size now)
    and what is left: a file or a link goes, a directory under a file's
    name stays, what is gone is neither."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        left.append((path, f"cannot be read: {exc.strerror or exc}"))
        return
    if stat.S_ISDIR(st.st_mode):
        left.append((path, "a directory now, where the import put a file"))
    else:
        going[path] = st.st_size


def _tree(top: str, going: dict[str, int], left: list[tuple[str, str]], dirs: list[str]) -> None:
    """Everything below a directory, links not followed: what is not a
    directory is sorted by ``_removable``, the directories (``top``
    included) go into ``dirs``. One that cannot be listed is reported when
    the directories are looked at (``plan``)."""
    dirs.append(top)
    try:
        entries = sorted(os.scandir(top), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            _tree(entry.path, going, left, dirs)
        else:
            _removable(entry.path, going, left)


def _legacy(
    record: dict, root: str, session_id: str, going: dict[str, int], left: list[tuple[str, str]]
) -> list[str]:
    """What a record without a manifest lets an undo remove, sorted into
    ``going`` and ``left``; returns the directories. It names its units
    (``destination_files``), each a file or a directory the import put in
    place whole. Only one whose own name carries the session's id is
    touched: nothing else ties such a record to what it names."""
    units = record.get("destination_files")
    if not isinstance(units, list) or not all(isinstance(unit, str) for unit in units):
        raise UndoFailed("the import record is damaged (its files); nothing was removed")
    dirs: list[str] = []
    for unit in units:
        if not importer.below(root, unit):
            raise _outside(unit, root)
        if session_id not in os.path.basename(unit):
            left.append((unit, NOT_NAMED))
        elif os.path.isdir(unit) and not os.path.islink(unit):
            _tree(unit, going, left, dirs)
        else:
            _removable(unit, going, left)
    return dirs


def _deepest_first(paths: Iterable[str]) -> list[str]:
    return sorted(set(paths), key=lambda path: (-path.count(os.sep), path))


def plan(
    record: dict,
    native_root: StrPath,
    extras: Iterable[str] = (),
    force: bool = False,
) -> UndoPlan:
    """What undoing ``record`` would remove and leave, with the native store
    as it is now. ``extras`` are the provider's (``Provider.undo_extras``).
    Reads only.

    Raises ``UndoFailed`` for everything an undo refuses: a record for
    another native store, a damaged one, a path outside the native store, a
    record without a manifest (unless ``force``), and files that differ from
    the record (unless ``force``; the exception's ``differences`` lists them
    all, the reason names the first few)."""
    root = os.fspath(native_root)
    ident = journal.ident(record)
    if ident is None:
        raise UndoFailed("the import record is damaged (no provider or id); nothing was removed")
    provider, session_id = ident
    if not root or "\0" in root or not os.path.isabs(root):
        raise UndoFailed(f"the native store {root!r} is not an absolute path")
    if record.get("destination_store") != root:
        raise UndoFailed(
            f"this import was made into another native store "
            f"({record.get('destination_store')!r}), not {root}; nothing was removed"
        )
    made = UndoPlan(provider, session_id, root, record, bool(force))
    going: dict[str, int] = {}  # what is removed: path -> size
    if "manifest" not in record:
        if not force:
            raise UndoFailed(LEGACY, remedy=REMEDY_FORCE)
        dirs = _legacy(record, root, session_id, going, made.left)
    else:
        items, dirs = _manifest(record, root)
        made.differences = [text for text in map(_difference, items) if text is not None]
        if made.differences and not force:
            shown = "; ".join(made.differences[:DIFFERENCES_SHOWN])
            more = len(made.differences) - DIFFERENCES_SHOWN
            raise UndoFailed(
                "the imported copy was used since the import: "
                + shown
                + (f"; and {more} more" if more > 0 else "")
                + ". Nothing was removed; a forced undo removes it all the same",
                differences=made.differences,
                remedy=REMEDY_FORCE,
            )
        for item in items:
            _removable(item["path"], going, made.left)

    appended = record.get("appended")
    for path in appended if isinstance(appended, list) else []:
        if isinstance(path, str):
            made.left.append((path, APPENDED))
    for path in extras:
        if not importer.below(root, path):
            raise _outside(path, root)
        if force:
            _removable(path, going, made.left)
        elif os.path.lexists(path):
            made.left.append((path, EXTRA))

    # A directory goes when everything in it goes; worked out here, on the
    # names, so that a dry run says what the real run does.
    made.files = sorted(going)
    made.bytes = sum(going.values())
    gone = set(going)
    for path in _deepest_first(dirs):
        if os.path.islink(path):
            made.left.append((path, "a link now, where the import made a directory"))
            continue
        try:
            names = os.listdir(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            made.left.append((path, f"cannot be read: {exc.strerror or exc}"))
            continue
        if all(os.path.join(path, name) in gone for name in names):
            made.dirs.append(path)
            gone.add(path)
        else:
            made.left.append((path, NOT_EMPTY))
    made.left = sorted(set(made.left))
    return made


def _carry_out(made: UndoPlan) -> tuple[list[str], list[tuple[str, str]]]:
    """Remove what a plan lists; returns ``(removed, left)``. What cannot be
    removed is left and reported, never fatal: the rest still goes."""
    removed: list[str] = []
    left = list(made.left)
    for path in made.files:
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue  # gone since the plan was made: nothing to remove
        except OSError as exc:
            left.append((path, f"could not be removed: {exc.strerror or exc}"))
            continue
        removed.append(path)
    for path in made.dirs:
        try:
            os.rmdir(path)  # only when nothing is in it
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                left.append((path, NOT_EMPTY))
            else:
                left.append((path, f"could not be removed: {exc.strerror or exc}"))
            continue
        removed.append(path)
    for directory in sorted({os.path.dirname(path) for path in removed}):
        fsutil.fsync_dir(directory)  # best effort; one that went with the rest cannot be opened
    return removed, sorted(set(left))


def undo_record(made: UndoPlan, removed: list[str], left: list[tuple[str, str]]) -> dict:
    """The journal record of an undo."""
    record = made.record
    return {
        "record": journal.UNDO,
        "version": journal.VERSION,
        "undone_at": _now(),
        "provider": made.provider,
        "id": made.session_id,
        "source_key": record.get("source_key"),
        "native_key": record.get("native_key"),
        "destination_store": made.native_root,
        "imported_at": record.get("imported_at"),
        "forced": made.forced,
        "differences": list(made.differences),
        "removed": sorted(removed),
        "left": [{"path": path, "why": why} for path, why in left],
        "agent_sessions_version": __version__,
    }


def _unfinished(records: Iterable[dict], provider: str, session_id: str, root: str) -> bool:
    """Whether an import of this session into this native store began and
    never ended."""
    return any(
        journal.ident(begin) == (provider, session_id) and begin.get("destination_store") == root
        for begin in journal.unsettled(records)
    )


def undo(
    provider: str,
    session_id: str,
    native_root: StrPath,
    extras: Iterable[str] = (),
    force: bool = False,
    dry_run: bool = False,
    read_back: importer.ReadBack | None = None,
) -> tuple[UndoPlan, dict | None]:
    """Undo the last import of ``session_id`` that still stands: returns
    the plan that was carried out and the record added to the journal.

    With ``dry_run`` nothing is touched, not even the lock: the plan is what
    would be done, and the record is None. Otherwise the plan is made and
    carried out with the state directory's lock held, so no import runs
    meanwhile, and the plan's ``files``, ``dirs`` and ``left`` are what was
    really removed and left.

    The first thing done with the lock held is to settle the imports into
    this native store that began and never ended (``importer.recover``;
    ``read_back`` is the provider's ``read_back``, which says whether such a
    copy is whole). One of this session that is whole is recorded then and
    undone like any other; one that is not is taken out, and the undo ends
    with ``TAKEN_OUT``: there is no import left to undo.

    Raises ``UndoFailed``, and then nothing was removed. Two exceptions say
    so in their reason: ``TAKEN_OUT``, and the one for a copy that went
    while the journal could not take the record of it (the import then
    still stands in the journal, with its files gone, and a forced undo
    settles it)."""
    extras = list(extras)
    root = os.fspath(native_root)
    records = read_records()
    record = find_record(records, provider, session_id)
    unfinished = _unfinished(records, provider, session_id, root)
    if record is None and not unfinished:
        raise UndoFailed(NO_RECORD, no_record=True)
    if dry_run:
        if record is None:
            raise UndoFailed(UNFINISHED)
        return plan(record, root, extras, force), None
    try:
        # The state directory is there: the record was read from it.
        with fsutil.lock():
            settled = importer.recover(root, read_back)
            records = read_records()
            record = find_record(records, provider, session_id)
            if record is None:
                # Undone by someone else while this one waited, or never
                # more than the beginning of an import.
                if any(
                    done.get("record") == journal.ROLLBACK
                    and journal.ident(done) == (provider, session_id)
                    for done in settled
                ):
                    raise UndoFailed(TAKEN_OUT, no_record=True)
                if _unfinished(records, provider, session_id, root):
                    raise UndoFailed(NOT_SETTLED)
                raise UndoFailed(NO_RECORD, no_record=True)
            made = plan(record, root, extras, force)
            # Opened before anything goes: what can still fail for the
            # journal afterwards is one small write.
            fd, created = journal.open_for_append()
            try:
                removed, left = _carry_out(made)
                written = undo_record(made, removed, left)
                try:
                    journal.write(fd, written)
                except OSError as exc:
                    raise UndoFailed(
                        f"{len(removed)} files and directories of the imported copy were "
                        f"removed, but the journal could not take the record of it "
                        f"({exc.strerror or exc}); a forced undo records it once it can",
                        remedy=REMEDY_FORCE,
                    ) from exc
            finally:
                os.close(fd)
            if created:  # the import was recorded in the old ``imports.json``
                fsutil.fsync_dir(os.path.dirname(journal.path()))
    except fsutil.LockTimeout as exc:
        who = f"process {exc.pid}" if exc.pid is not None else "another process"
        raise UndoFailed(
            f"an import or another undo is still running ({who}); nothing was removed"
        ) from exc
    except OSError as exc:  # the lock or the journal, before anything was removed
        where = f" ({exc.filename})" if exc.filename else ""
        raise UndoFailed(
            f"the undo could not start: {exc.strerror or exc}{where}; nothing was removed"
        ) from exc
    gone = set(removed)
    made.files = [path for path in made.files if path in gone]
    made.dirs = [path for path in made.dirs if path in gone]
    made.left = left
    return made, written


__all__ = [
    "APPENDED",
    "DIFFERENCES_SHOWN",
    "EXTRA",
    "LEGACY",
    "NOT_EMPTY",
    "NOT_NAMED",
    "NOT_SETTLED",
    "NO_RECORD",
    "TAKEN_OUT",
    "UNFINISHED",
    "UndoFailed",
    "UndoPlan",
    "find_record",
    "not_undone",
    "plan",
    "read_records",
    "undo",
    "undo_record",
]
