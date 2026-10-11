"""Claude Code adapter. Formats: docs/FORMATS.md "Claude Code"."""

from __future__ import annotations

from pathlib import Path

from ..fsutil import home
from ..model import Candidate, Session, Store
from . import register
from .base import Provider


@register
class Claude(Provider):
    name = "claude"
    label = "Claude"
    binary = "claude"
    app_id = "org.omarchy.claude"
    resumable = True
    dir_names = {".claude"}

    def native_store(self) -> Path | None:
        return home() / ".claude"

    def is_store(self, path: Path) -> bool:
        return (path / "projects").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        raise NotImplementedError

    def parse(self, store: Store, c: Candidate) -> Session | None:
        raise NotImplementedError

    def resume_argv(self, s: Session) -> list[str]:
        raise NotImplementedError

    def import_session(self, s: Session, pathmap) -> Session:
        raise NotImplementedError
