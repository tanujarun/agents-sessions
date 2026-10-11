"""Gemini CLI adapter. Formats: docs/FORMATS.md "Gemini CLI"."""

from __future__ import annotations

from pathlib import Path

from ..fsutil import home
from ..model import Candidate, Session, Store
from . import register
from .base import Provider


@register
class Gemini(Provider):
    name = "gemini"
    label = "Gemini"
    binary = "gemini"
    app_id = "org.omarchy.gemini"
    # No usable gemini binary exists here (FORMATS "Gemini CLI" section 0).
    resumable = False
    dir_names = {".gemini"}

    def native_store(self) -> Path | None:
        return home() / ".gemini"

    def is_store(self, path: Path) -> bool:
        return (path / "tmp").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        raise NotImplementedError

    def parse(self, store: Store, c: Candidate) -> Session | None:
        raise NotImplementedError

    def resume_argv(self, s: Session) -> list[str]:
        raise NotImplementedError

    def import_session(self, s: Session, pathmap) -> Session:
        raise NotImplementedError
