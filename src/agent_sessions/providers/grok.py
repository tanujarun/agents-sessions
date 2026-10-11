"""Grok adapter. Formats: docs/FORMATS.md "Grok"."""

from __future__ import annotations

from pathlib import Path

from ..fsutil import home
from ..model import Candidate, Session, Store
from . import register
from .base import Provider


@register
class Grok(Provider):
    name = "grok"
    label = "Grok"
    binary = "grok"
    app_id = "org.omarchy.grok"
    resumable = True
    dir_names = {".grok"}

    def native_store(self) -> Path | None:
        return home() / ".grok"

    # Same signature as Codex; the directory name decides.
    def is_store(self, path: Path) -> bool:
        return (path / "sessions").is_dir()

    def candidates(self, store: Store) -> list[Candidate]:
        raise NotImplementedError

    def parse(self, store: Store, c: Candidate) -> Session | None:
        raise NotImplementedError

    def resume_argv(self, s: Session) -> list[str]:
        raise NotImplementedError

    def import_session(self, s: Session, pathmap) -> Session:
        raise NotImplementedError
