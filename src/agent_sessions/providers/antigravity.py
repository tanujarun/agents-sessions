"""Antigravity adapter. Formats: docs/FORMATS.md "Antigravity"."""

from __future__ import annotations

from pathlib import Path

from ..fsutil import home
from ..model import Candidate, Session, Store
from . import register
from .base import Provider


@register
class Antigravity(Provider):
    name = "antigravity"
    label = "Antigravity"
    binary = "agy"
    app_id = "org.omarchy.agy"
    resumable = True
    dir_names = {"antigravity-cli", "antigravity-ide"}

    def native_store(self) -> Path | None:
        return home() / ".gemini" / "antigravity-cli"

    def is_store(self, path: Path) -> bool:
        return (path / "conversations").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        raise NotImplementedError

    def parse(self, store: Store, c: Candidate) -> Session | None:
        raise NotImplementedError

    def resume_argv(self, s: Session) -> list[str]:
        raise NotImplementedError

    def import_session(self, s: Session, pathmap) -> Session:
        raise NotImplementedError
