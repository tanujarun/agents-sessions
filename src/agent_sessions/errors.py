"""The failures the facade reports. ``api`` re-exports them; they live here so
``launch`` and ``importer`` can raise them without importing ``api``."""

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


__all__ = ["ApiError", "ImportFailed", "LaunchFailed", "NotFound"]
