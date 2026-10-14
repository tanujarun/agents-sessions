"""The Textual front end. The only package that imports ``textual``.

Importing this package does not import Textual: ``view`` and ``engine`` are
plain Python, and ``app`` (the part that needs Textual) is loaded by ``run``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import Engine

# What is said when Textual is missing. Which one depends on how the program
# got here: from a checkout (``install.sh`` puts Textual into its ``.venv``)
# or as an installed package (Textual is the extra ``tui``).
INSTALL_HINT = "agent-sessions: the TUI needs Textual; run ./install.sh in {repo}"
PIP_HINT = "agent-sessions: the TUI needs Textual; run: pip install 'agent-sessions[tui]'"

# What is said, with exit status 2, when the TUI is asked for and there is
# no terminal to run it on.
NO_TERMINAL = "agent-sessions: the TUI needs a terminal (stdin, stdout and stderr)"

# The files that make a directory a checkout of this project, beside the
# package at ``src/agent_sessions``.
CHECKOUT_FILES = ("install.sh", "pyproject.toml", "requirements-tui.txt")


def checkout(package: Path | None = None) -> Path | None:
    """The checkout this package runs from, or None for an installed one.

    A checkout is a directory that holds the package as ``src/agent_sessions``
    and, beside ``src``, the files ``install.sh`` works with. A package in a
    ``site-packages`` directory has neither, whatever lies three levels up.
    ``package`` is the package directory to ask about (this one by default).
    """
    here = Path(__file__).resolve().parent.parent if package is None else Path(package)
    if here.name != "agent_sessions" or here.parent.name != "src":
        return None
    root = here.parent.parent
    if all((root / name).is_file() for name in CHECKOUT_FILES):
        return root
    return None


def install_hint(package: Path | None = None) -> str:
    """The one line that says how to get Textual, for where this runs from."""
    root = checkout(package)
    return PIP_HINT if root is None else INSTALL_HINT.format(repo=root)


def on_a_terminal() -> bool:
    """Whether a person is at every end: the TUI reads keys from stdin and
    draws on the terminal behind stdout and stderr (Textual writes to
    stderr). With one of them a file, a pipe or closed it would wait for
    keys nobody can see it wait for."""
    try:
        return all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))
    except (AttributeError, ValueError):  # a stream that was closed, or replaced
        return False


def run(engine: Engine | None = None, debug: bool = False) -> int:
    """Run the app until it exits; the process exit code. ``debug``: as
    started with ``--debug``."""
    from .__main__ import DEBUG, main

    return main([DEBUG] if debug else [], engine=engine)


__all__ = [
    "CHECKOUT_FILES",
    "INSTALL_HINT",
    "NO_TERMINAL",
    "PIP_HINT",
    "checkout",
    "install_hint",
    "on_a_terminal",
    "run",
]
