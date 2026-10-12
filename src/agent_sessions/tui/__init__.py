"""The Textual front end. The only package that imports ``textual``.

Importing this package does not import Textual: ``view`` and ``engine`` are
plain Python, and ``app`` (the part that needs Textual) is loaded by ``run``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine

INSTALL_HINT = "agent-sessions: the TUI needs Textual; run ./install.sh in {repo}"


def run(engine: Engine | None = None) -> int:
    """Run the app until it exits; the process exit code."""
    from .__main__ import main

    return main([], engine=engine)


__all__ = ["INSTALL_HINT", "run"]
