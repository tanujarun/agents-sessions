"""``python -m agent_sessions.tui``: run the TUI. The one way in: the
checkout's launcher and ``cli.main`` (through ``tui.run``) start it here."""

from __future__ import annotations

import io
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .app import SessionsApp
    from .engine import Engine

DEBUG = "--debug"
USAGE = f"usage: python -m agent_sessions.tui [{DEBUG}]"


def main(argv: Sequence[str] | None = None, engine: Engine | None = None) -> int:
    """Run the app until it exits; the exit status. ``argv`` holds nothing,
    or ``--debug``. One line and status 2 when there is no terminal to run
    on, one line and status 1 when Textual is missing."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if any(arg != DEBUG for arg in argv):
        print(USAGE, file=sys.stderr)
        return 0 if all(arg in (DEBUG, "-h", "--help") for arg in argv) else 2
    from . import NO_TERMINAL, install_hint, on_a_terminal

    if not on_a_terminal():
        print(NO_TERMINAL, file=sys.stderr)
        return 2
    try:
        from .app import SessionsApp
    except ImportError as exc:
        if (exc.name or "").split(".")[0] not in ("textual", "rich"):
            raise
        print(install_hint(), file=sys.stderr)
        return 1
    app = SessionsApp(engine=engine)
    note = _run_debugging(app) if argv else app.run()
    if isinstance(note, str) and note:
        # A launch that had something to say (the app has already quit).
        from .view import clean

        print(f"agent-sessions: {clean(note)}", file=sys.stderr)
    return app.return_code or 0


def _run_debugging(app: SessionsApp) -> object:
    """Run the app with ``--debug``: the log at DEBUG level. Its copy on
    stderr is kept back until the app has left the screen and printed then:
    stderr is the terminal the interface is drawn on."""
    from .. import logs

    kept, stderr = io.StringIO(), sys.stderr
    sys.stderr = kept
    try:
        logs.debug()  # takes the stream that is ``sys.stderr`` now
    finally:
        sys.stderr = stderr
    try:
        return app.run()
    finally:
        logs.debug()  # and from here on the real one again
        stderr.write(kept.getvalue())


if __name__ == "__main__":
    sys.exit(main())
