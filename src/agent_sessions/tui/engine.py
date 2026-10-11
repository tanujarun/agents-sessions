"""The engine as the TUI sees it: one object whose methods forward to
``agent_sessions.api``. The app takes it as a parameter, so a test (or
another back end) can hand in a replacement with the same methods.

Each function is looked up when it is called, not when this module is
imported: an engine that lacks one gives a status-line message, not a crash.
"""

from __future__ import annotations

import importlib


class EngineError(Exception):
    """An engine function is missing or could not be loaded. ``reason`` is
    plain words for the status line."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Engine:
    """Forwards to ``agent_sessions.api``. Arguments and results are the
    engine's own; see that module."""

    module = "agent_sessions.api"

    def _call(self, name: str, *args, **kwargs):
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

    def load_index(self):
        return self._call("load_index")

    def list_sessions(self, provider=None, index=None):
        return self._call("list_sessions", provider, index=index)

    def status(self, index=None) -> dict:
        return self._call("status", index=index)

    def where(self, session, index) -> str:
        """The WHERE text of a session: ``Linux``, ``C:``, a mount label."""
        return index.where(session)

    def find_session(self, ref, index=None):
        return self._call("find_session", ref, index=index)

    def can_import(self, session, index=None) -> tuple[bool, str]:
        return self._call("can_import", session, index=index)

    def transcript(self, ref, index=None):
        return self._call("transcript", ref, index=index)

    # --- acting ----------------------------------------------------------

    def scan(self, deep=None, progress=None, cancel=None) -> dict:
        return self._call("scan", deep=deep, progress=progress, cancel=cancel)

    def import_session(self, ref, progress=None, index=None):
        return self._call("import_session", ref, progress=progress, index=index)

    def open_session(self, ref, here=False, dry_run=False, index=None):
        return self._call("open_session", ref, here=here, dry_run=dry_run, index=index)


__all__ = ["Engine", "EngineError"]
