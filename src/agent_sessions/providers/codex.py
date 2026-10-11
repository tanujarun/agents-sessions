"""Codex adapter. Formats: docs/FORMATS.md "Codex"."""

from __future__ import annotations

from pathlib import Path

from ..fsutil import home
from ..model import Candidate, Session, Store
from . import register
from .base import Provider


@register
class Codex(Provider):
    name = "codex"
    label = "Codex"
    binary = "codex"
    app_id = "org.omarchy.codex"
    resumable = True
    dir_names = {".codex"}

    def native_store(self) -> Path | None:
        return home() / ".codex"

    # The directory name tells a Codex store from a Grok one: both hold
    # sessions/ (Codex's has YYYY/ directories, Grok's percent-encoded names).
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
