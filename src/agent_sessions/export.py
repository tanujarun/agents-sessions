"""A session's transcript as a document (docs/SPEC.md "Continue as a new
session"): Markdown for a person or for another agent, JSON lines for a
program.

Both are made lazily, piece by piece, from the turns an adapter yields
(``Provider.transcript``); a transcript can be hundreds of megabytes. A piece
is one or more whole lines without the line break that ends it: whoever
writes the document adds one after every piece.

Everything in a transcript is text from a store, possibly one on another
drive. The Markdown is made of ``textsafe.clean`` text: a title, a folder or
a tool's name is one line whatever it holds, and the text of a turn keeps
its line breaks and tabs and nothing else a terminal would act on. The JSON
lines are exact data with those characters as ``\\uXXXX`` escapes
(``textsafe.json_escape``; docs/JSON.md has the form).

``write`` puts a document into a file, whole or not at all.

Standard library only, and nothing here knows a provider.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import TYPE_CHECKING

from . import __version__, fsutil, textsafe
from .errors import ExportFailed

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from .fsutil import StrPath
    from .model import Session
    from .providers.base import Turn

MARKDOWN = "md"
JSON_LINES = "json"
FORMATS = (MARKDOWN, JSON_LINES)

TOOL_ROLE = "tool"  # the role of a turn that is a tool call; its text is the tool's name
_HEADINGS = {"user": "User", "assistant": "Assistant"}
_UNKNOWN = "unknown"
_NO_FOLDER = "not recorded"


def _heading(role: str) -> str:
    """What a turn's section is called: ``User``, ``Assistant``, and for a
    role no adapter gives today, that role as one clean line."""
    return _HEADINGS.get(role) or textsafe.clean(role).strip() or _UNKNOWN


def markdown(session: Session, label: str, turns: Iterable[Turn]) -> Iterator[str]:
    """The transcript as Markdown, piece by piece.

    First the header: the title as ``#`` (the id when there is none), then a
    list: the tool (``label``), the session id, the folder as recorded, when
    it started and was last updated, how many messages it has, and what
    exported it. Then one ``## User`` or ``## Assistant`` section per turn,
    with the turn's time in the heading when it is known, and one line
    ``> tool: <name>`` per tool call, the calls that follow one another in
    one block.
    """
    count = _UNKNOWN if session.messages is None else str(session.messages)
    header = [
        f"# {textsafe.clean(session.title or session.id)}",
        "",
        f"- tool: {textsafe.clean(label)}",
        f"- session: {textsafe.clean(session.id)}",
        f"- folder: {textsafe.clean(session.cwd or _NO_FOLDER)}",
        f"- started: {textsafe.clean(session.started or _UNKNOWN)}",
        f"- updated: {textsafe.clean(session.updated or _UNKNOWN)}",
        f"- messages: {count}",
        f"- exported by agent-sessions {__version__}",
    ]
    yield "\n".join(header)
    last = None
    for turn in turns:
        if turn.role == TOOL_ROLE:
            line = f"> tool: {textsafe.clean(turn.text)}"
            yield line if last == TOOL_ROLE else "\n" + line
        else:
            when = f" ({textsafe.clean(turn.ts)})" if turn.ts else ""
            text = textsafe.clean(turn.text, multiline=True)
            yield f"\n## {_heading(turn.role)}{when}\n\n{text}"
        last = turn.role


def json_lines(turns: Iterable[Turn]) -> Iterator[str]:
    """The transcript as JSON lines, one object per turn (``role``, ``text``,
    ``ts``; docs/JSON.md "ExportLine"). Exact data: the text is what the
    adapter read, and a character a terminal would act on is written as a
    ``\\uXXXX`` escape, so a line holds no line break of its own."""
    for turn in turns:
        row = {"role": turn.role, "text": turn.text, "ts": turn.ts}
        yield textsafe.json_escape(json.dumps(row, ensure_ascii=False, sort_keys=True))


# --- a document in a file -----------------------------------------------------


def _bytes(piece: str) -> bytes:
    """One piece and its line break as UTF-8. Text that is not Unicode (half
    of a surrogate pair, out of a store's JSON) is written as its ``\\udXXX``
    escape instead of failing: in a JSON line that is the same character
    again, in Markdown it is readable."""
    return (piece + "\n").encode("utf-8", "backslashreplace")


def write(path: StrPath, pieces: Iterable[str], overwrite: bool = False) -> int:
    """Write a document to the file ``path``, whole or not at all; returns
    its size in bytes. ``pieces`` are what ``markdown`` or ``json_lines``
    yield.

    The document is first written to a temp file in the same directory, for
    the user alone (0600), and flushed to disk; then that file is given the
    name. The temp file is always a new one (``O_EXCL``, under a name with
    the process id and the time in it): the directory is any the user
    named, a shared one included, and nothing that lies there is written
    through. With ``overwrite`` the document replaces a file that is there
    (``os.replace``). Without, the name is made with ``os.link``, which the
    kernel refuses in the same step when the name exists: a file that
    appears at the last moment is not replaced either. Only where the
    filesystem has no hard links is the name looked at first and the file
    then renamed (as ``importer._place`` does for a session file).

    Raises ``ExportFailed``: the path is not absolute, its directory is not
    there, the name is a directory or something else that is no regular
    file, it exists and ``overwrite`` was not asked for, or the write itself
    fails. A failure leaves no temp file and the old file as it was.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise ExportFailed("the file to write is not a path")
    target = os.fspath(path)
    if not target or "\0" in target or not os.path.isabs(target):
        raise ExportFailed(f"the file to write must be an absolute path: {textsafe.clean(target)}")
    target = os.path.normpath(target)
    folder = os.path.dirname(target)
    if not os.path.isdir(folder):
        raise ExportFailed(f"the directory {folder} does not exist; nothing was written")
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        mode = None
    except OSError as exc:
        raise ExportFailed(f"cannot write {target}: {exc.strerror or exc}") from exc
    if mode is not None:
        if stat.S_ISDIR(mode):
            raise ExportFailed(f"{target} is a directory; nothing was written")
        if not stat.S_ISREG(mode):
            raise ExportFailed(f"{target} is not a regular file; nothing was written")
        if not overwrite:
            raise _exists(target)
    tmp = f"{target}.{os.getpid()}-{time.time_ns():x}.tmp"
    size = 0
    made = False  # the temp file is ours to remove
    try:
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                fsutil.PRIVATE_FILE,
            )
            made = True
            with os.fdopen(fd, "wb") as fh:
                if hasattr(os, "fchmod"):
                    os.fchmod(fh.fileno(), fsutil.PRIVATE_FILE)
                for piece in pieces:
                    size += fh.write(_bytes(piece))
                fh.flush()
                os.fsync(fh.fileno())
            if overwrite:
                os.replace(tmp, target)
            else:
                _link(tmp, target)
            fsutil.fsync_dir(folder)
        finally:
            if made:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as exc:
        raise ExportFailed(
            f"{target} was not written: {exc.strerror or exc}; nothing was left behind"
        ) from exc
    return size


def _exists(target: str) -> ExportFailed:
    return ExportFailed(f"{target} exists; nothing was written (--force replaces it)")


def _link(tmp: str, target: str) -> None:
    """Give the finished temp file its name without replacing anything."""
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise _exists(target) from None
    except (OSError, NotImplementedError, AttributeError):
        # No hard links here (FAT, some network mounts, an old Windows).
        if os.path.lexists(target):
            raise _exists(target) from None
        os.replace(tmp, target)


__all__ = [
    "FORMATS",
    "JSON_LINES",
    "MARKDOWN",
    "ExportFailed",
    "json_lines",
    "markdown",
    "write",
]
