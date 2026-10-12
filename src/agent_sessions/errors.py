"""The failures the facade reports. ``api`` re-exports them; they live here so
``launch``, ``importer`` and ``mounts`` can raise them without importing ``api``."""

from __future__ import annotations


class ApiError(Exception):
    """``reason`` is plain words, shown to the user as it is."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NotFound(ApiError):
    """No session, or more than one, matches a reference."""


class LaunchFailed(ApiError):
    """A session could not be resumed."""


class ImportFailed(ApiError):
    """A session could not be imported, or its provider does not allow it."""


class UndoFailed(ApiError):
    """An import could not be undone (``unimport``), or the undo was
    refused."""


class ExportFailed(ApiError):
    """A transcript could not be exported to a file (``export.write``)."""


class Offline(ApiError):
    """What was asked needs the files of a session whose drive is not
    mounted."""


class MountFailed(ApiError):
    """A drive could not be mounted (``mounts.mount_volume``)."""


__all__ = [
    "ApiError",
    "ExportFailed",
    "ImportFailed",
    "LaunchFailed",
    "MountFailed",
    "NotFound",
    "Offline",
    "UndoFailed",
]
