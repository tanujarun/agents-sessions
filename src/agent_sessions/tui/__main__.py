"""``python -m agent_sessions.tui``: run the TUI."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .engine import Engine


def main(argv: Sequence[str] | None = None, engine: Engine | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv:
        print("usage: agent-sessions [tui]", file=sys.stderr)
        return 0 if argv[0] in ("-h", "--help") else 2
    try:
        from .app import SessionsApp
    except ImportError as exc:
        if (exc.name or "").split(".")[0] not in ("textual", "rich"):
            raise
        from . import install_hint

        print(install_hint(), file=sys.stderr)
        return 1
    app = SessionsApp(engine=engine)
    note = app.run()
    if isinstance(note, str) and note:
        # A launch that had something to say (the app has already quit).
        print(f"agent-sessions: {note}", file=sys.stderr)
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
