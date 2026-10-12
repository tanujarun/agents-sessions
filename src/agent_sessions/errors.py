"""The failures the facade reports. ``api`` re-exports them; they live here so
``launch``, ``importer`` and ``mounts`` can raise them without importing ``api``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


# What would get past a refusal (``ApiError.remedy``). The reason itself never
# names a command-line flag, a key or a button: it says what is the case, and
# each front end turns the word into its own hint (``cli._REMEDIES``).
REMEDY_KEYS = "keys"  # no session matches: a listing of the keys helps
REMEDY_FOLDER = "folder"  # a folder has to be chosen
REMEDY_FORCE = "force"  # a forced undo does it
REMEDY_OVERWRITE = "overwrite"  # the file is replaced when that is asked for
REMEDY_EXPERIMENTAL = "experimental"  # done only as an experimental import


class ApiError(Exception):
    """``reason`` is plain words, shown to the user as it is. It names no
    flag of the command line: ``remedy`` is one of the ``REMEDY_`` words when
    asking in another way would get past the refusal, else ""."""

    def __init__(self, reason: str, *, remedy: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.remedy = remedy


class NotFound(ApiError):
    """No session, or more than one, matches a reference."""


class LaunchFailed(ApiError):
    """A session could not be resumed."""


class ImportFailed(ApiError):
    """A session could not be imported, or its provider does not allow it."""


class UndoFailed(ApiError):
    """An import could not be undone (``unimport``), or the undo was
    refused.

    Two refusals a front end answers in its own way are marked, so that it
    need not read the reason: ``no_record`` when no import of the session is
    recorded, and ``differences`` (what differs, one text per file) when the
    copy was used since the import, which a forced undo gets past. Any other
    refusal has neither."""

    def __init__(
        self,
        reason: str,
        *,
        no_record: bool = False,
        differences: Iterable[str] = (),
        remedy: str = "",
    ) -> None:
        super().__init__(reason, remedy=remedy)
        self.no_record = no_record
        self.differences = list(differences)


class ExportFailed(ApiError):
    """A transcript could not be exported to a file (``export.write``)."""


class Offline(ApiError):
    """What was asked needs the files of a session whose drive is not
    mounted."""


class MountFailed(ApiError):
    """A drive could not be mounted (``mounts.mount_volume``)."""


__all__ = [
    "REMEDY_EXPERIMENTAL",
    "REMEDY_FOLDER",
    "REMEDY_FORCE",
    "REMEDY_KEYS",
    "REMEDY_OVERWRITE",
    "ApiError",
    "ExportFailed",
    "ImportFailed",
    "LaunchFailed",
    "MountFailed",
    "NotFound",
    "Offline",
    "UndoFailed",
]
