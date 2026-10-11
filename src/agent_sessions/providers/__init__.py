"""Provider adapters and their registry."""

from __future__ import annotations

from .base import NotSupported, Provider

__all__ = ["NotSupported", "Provider", "register", "all_providers", "get"]

_REGISTRY: dict[str, type[Provider]] = {}


def register(provider_cls: type[Provider]) -> type[Provider]:
    """Register a Provider subclass under its ``name``; usable as a decorator."""
    name = getattr(provider_cls, "name", "")
    if not name:
        raise ValueError("provider class has no name")
    _REGISTRY[name] = provider_cls
    return provider_cls


def all_providers() -> list[Provider]:
    """One instance of every registered provider, sorted by name."""
    return [_REGISTRY[name]() for name in sorted(_REGISTRY)]


def get(name: str) -> Provider:
    """Instance of the provider called ``name``; KeyError if unknown."""
    return _REGISTRY[name]()


# Importing an adapter module registers it.
from . import antigravity, claude, codex, gemini, grok  # noqa: E402,F401
