"""The engine as the TUI sees it: one object whose methods forward to
``agent_sessions.api``. The app takes it as a parameter, so a test (or
another back end) can hand in a replacement with the same methods.

Each function is looked up when it is called, not when this module is
imported: an engine that lacks one gives a status-line message, not a crash.
"""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator

    from ..api import ContinueOptions, ImportOptions, UnimportReport
    from ..index import Index
    from ..launch import ContinuePlan, LaunchPlan
    from ..model import Session
    from ..providers.base import Turn
    from ..search import Corpus


class EngineError(Exception):
    """An engine function is missing or could not be loaded. ``reason`` is
    plain words for the status line."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Engine:
    """Forwards to ``agent_sessions.api``. Arguments and results are the
    engine's own; see that module."""

    module = "agent_sessions.api"

    def _call(self, name: str, *args: object, **kwargs: object) -> Any:
        try:
            function = getattr(importlib.import_module(self.module), name)
        except ImportError as exc:
            raise EngineError(f"the engine could not be loaded: {exc}") from exc
        except AttributeError:
            raise EngineError(f"this build of the engine has no `{name}` yet") from None
        return function(*args, **kwargs)

    # --- reading ---------------------------------------------------------

    def providers(self) -> list[tuple[str, str]]:
        """``[(name, label)]`` of every registered provider."""
        try:
            registry = importlib.import_module("agent_sessions.providers")
        except ImportError as exc:
            raise EngineError(f"the engine could not be loaded: {exc}") from exc
        return [(provider.name, provider.label) for provider in registry.all_providers()]

    def load_index(self) -> Index:
        return self._call("load_index")

    def list_sessions(
        self, provider: str | None = None, index: Index | None = None
    ) -> list[Session]:
        return self._call("list_sessions", provider, index=index)

    def status(self, index: Index | None = None) -> dict:
        return self._call("status", index=index)

    def where(self, session: Session, index: Index) -> str:
        """The WHERE text of a session: ``Linux``, ``C:``, a mount label."""
        return index.where(session)

    def find_session(self, ref: Session | str, index: Index | None = None) -> Session:
        return self._call("find_session", ref, index=index)

    def import_options(
        self, ref: Session | str, index: Index | None = None, target: str | None = None
    ) -> ImportOptions:
        return self._call("import_options", ref, index=index, target=target)

    def continue_options(self, ref: Session | str, index: Index | None = None) -> ContinueOptions:
        return self._call("continue_options", ref, index=index)

    def volumes(self, index: Index | None = None) -> list[dict]:
        return self._call("volumes", index=index)

    def transcript(self, ref: Session | str, index: Index | None = None) -> Iterator[Turn]:
        return self._call("transcript", ref, index=index)

    def corpus(self) -> Corpus:
        """An empty ``search.Corpus``: the sessions' text, kept to find a
        session by what was said in it."""
        try:
            search = importlib.import_module("agent_sessions.search")
        except ImportError as exc:
            raise EngineError(f"the engine could not be loaded: {exc}") from exc
        return search.Corpus()

    def _fsutil(self) -> Any:
        try:
            return importlib.import_module("agent_sessions.fsutil")
        except ImportError as exc:
            raise EngineError(f"the engine could not be loaded: {exc}") from exc

    def home(self) -> str:
        """The home directory the engine works with (``fsutil.home``): what
        ``~`` means in a folder the user types."""
        return str(self._fsutil().home())

    def list_folder(self, directory: str) -> list[tuple[str, bool]]:
        """The entries of a directory as ``(name, is a directory)``, for the
        completion of a folder the user types. Listed the engine's way,
        which leaves the directory's access time alone (it may be on
        another drive); raises ``OSError`` when it cannot be listed."""
        fsutil = self._fsutil()
        return [
            (name, os.path.isdir(os.path.join(directory, name)))
            for name in fsutil.listdir_ro(directory)
        ]

    # --- acting ----------------------------------------------------------

    def scan(
        self,
        deep: bool | None = None,
        progress: Callable[[str, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict:
        return self._call("scan", deep=deep, progress=progress, cancel=cancel)

    def import_session(
        self,
        ref: Session | str,
        progress: Callable[[int, int], None] | None = None,
        index: Index | None = None,
        target: str | None = None,
        experimental: bool = False,
    ) -> Session:
        return self._call(
            "import_session",
            ref,
            progress=progress,
            index=index,
            target=target,
            experimental=experimental,
        )

    def unimport(
        self,
        ref: Session | str,
        force: bool = False,
        dry_run: bool = False,
        index: Index | None = None,
    ) -> UnimportReport:
        return self._call("unimport", ref, force=force, dry_run=dry_run, index=index)

    def mount_drive(
        self,
        ref: str,
        progress: Callable[[str, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> dict:
        return self._call("mount_drive", ref, progress=progress, cancel=cancel)

    def continue_session(
        self,
        ref: Session | str,
        tool: str | None = None,
        folder: str | None = None,
        index: Index | None = None,
    ) -> ContinuePlan:
        """Always in a new window: the engine's ``here`` would replace this
        process, so it is not forwarded."""
        return self._call("continue_session", ref, tool=tool, folder=folder, index=index)

    def open_session(
        self,
        ref: Session | str,
        here: bool = False,
        dry_run: bool = False,
        index: Index | None = None,
    ) -> LaunchPlan:
        return self._call("open_session", ref, here=here, dry_run=dry_run, index=index)


__all__ = ["Engine", "EngineError"]
