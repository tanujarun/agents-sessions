"""Provider adapter base class and the NotSupported error."""

from __future__ import annotations

from pathlib import Path

from ..model import Session, Store


class NotSupported(Exception):
    """Raised when a provider cannot do an operation; carries the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Provider:
    name: str = ""            # "claude"
    label: str = ""           # "Claude"
    binary: str = ""          # executable to resume with
    app_id: str = ""          # omarchy-launch-tui --app-id value
    dir_names: set[str] = set()  # directory names that may be a store root

    def native_store(self) -> Path | None:
        raise NotImplementedError

    def is_store(self, path: Path) -> bool:
        """Cheap signature check."""
        raise NotImplementedError

    def list_sessions(self, store: Store) -> list[Session]:
        raise NotImplementedError

    def resume_argv(self, s: Session) -> list[str]:
        """Resume command for a native session."""
        raise NotImplementedError

    def import_session(self, s: Session, pathmap) -> Session:
        """May raise NotSupported(reason)."""
        raise NotImplementedError
