"""Build and run the resume command (platform layer).

The only module that starts a process. The Linux implementation opens a new
terminal window through ``omarchy-launch-tui``, which runs
``setsid uwsm-app -- xdg-terminal-exec --app-id=<id> -e <tool> <args>``; the
terminal inherits the working directory it was started in.

Two module-level hooks let a caller record instead of acting: ``starter``
(called as ``starter(command, cwd)`` to open the window) and ``notifier``
(called as ``notifier(reason)`` after a failed launch). Tests and the TUI
replace them, for example with ``mock.patch.object(launch, "starter", rec)``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import textsafe
from .errors import LaunchFailed
from .fsutil import home

if TYPE_CHECKING:
    from .model import Session
    from .providers.base import Provider

LAUNCHER = "omarchy-launch-tui"
NOTIFIER = "notify-send"
NOTIFY_ENV = "AGENT_SESSIONS_NOTIFY"  # "0" switches desktop notifications off
NOTIFY_TITLE = "Cannot resume session"


@dataclass
class LaunchPlan:
    cwd: str  # directory the tool starts in
    argv: list[str]  # the tool's resume command
    command: list[str]  # what is actually executed
    note: str | None = None


def _detached(command: list[str], cwd: str | None = None) -> None:
    """Start ``command`` in its own session with no terminal attached, and
    collect its exit status in the background so no zombie is left."""
    proc = subprocess.Popen(  # noqa: S603  (an argv list built by this module, no shell)
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    threading.Thread(target=proc.wait, daemon=True).start()


def _spawn(command: list[str], cwd: str | None) -> None:
    _detached(command, cwd)


def _notice(reason: object) -> str:
    """The text of a notification: one line with nothing a terminal or a
    notification daemon would act on (``textsafe.clean``), and never an
    option. The reason can quote a session's folder, which is data from
    another drive; ``notify-send`` reads an argument that starts with ``-``
    as an option wherever it stands (its ``--help`` names no ``--``
    terminator), so such a text gets a space in front."""
    text = textsafe.clean(reason).strip()
    return f" {text}" if text.startswith("-") else text


def _notify(reason: str) -> None:
    """A desktop notification, when ``notify-send`` exists."""
    if os.environ.get(NOTIFY_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return
    tool = shutil.which(NOTIFIER)
    if tool:
        _detached([tool, "--app-name=agent-sessions", NOTIFY_TITLE, _notice(reason)])


starter = _spawn  # starter(command, cwd): open the terminal window
notifier = _notify  # notifier(reason): tell the desktop about a failure


def _tell(reason: str) -> None:
    """Best effort: a notification that fails never hides the real failure.
    The notifier gets the reason with control characters taken out."""
    try:
        notifier(textsafe.clean(reason))
    except Exception:  # noqa: S110  (best effort, see the docstring)
        pass


def _directory(session: Session, provider: Provider) -> tuple[str, str | None]:
    """``(directory to start in, note)``. The recorded directory when it
    exists; else the home directory for a tool that finds a session by id
    from anywhere. A parent of the recorded directory is never used."""
    for path in (session.cwd_local, session.cwd):
        if path and os.path.isabs(path) and os.path.isdir(path):
            return path, None
    label = provider.label or provider.name
    state = "is gone" if session.cwd else "is unknown"
    if getattr(provider, "resume_needs_cwd", False):
        folder = f"the project folder {session.cwd}" if session.cwd else "the project folder"
        raise LaunchFailed(f"{folder} {state}; {label} can only resume from it")
    start = str(home())
    if not os.path.isdir(start):
        raise LaunchFailed(f"the home directory {start} does not exist")
    return start, f"project folder {state}; starting in the home directory"


def plan(
    session: Session, provider: Provider, where: str | None = None, here: bool = False
) -> LaunchPlan:
    """The resume command for ``session``; starts nothing.

    ``where`` names the place a foreign session is on, for the refusal.
    Raises ``LaunchFailed`` with the reason when it cannot be resumed.
    """
    label = provider.label or provider.name
    if session.origin != "native":
        place = where or session.mount_label or "another drive"
        raise LaunchFailed(f"this session is on {place}; import it first")
    if not provider.resumable:
        raise LaunchFailed(
            f"{label} sessions cannot be resumed here: the {label} CLI is not installed"
        )
    argv = [str(arg) for arg in provider.resume_argv(session)]
    if not argv or not shutil.which(argv[0]):
        tool = argv[0] if argv else provider.binary
        raise LaunchFailed(f"{tool} is not installed (not found on PATH)")
    cwd, note = _directory(session, provider)
    if here:
        return LaunchPlan(cwd, argv, list(argv), note)
    if not shutil.which(LAUNCHER):
        raise LaunchFailed(f"{LAUNCHER} was not found; cannot open a terminal window")
    return LaunchPlan(cwd, argv, [LAUNCHER, f"--app-id={provider.app_id}", *argv], note)


def launch(
    session: Session,
    provider: Provider,
    where: str | None = None,
    here: bool = False,
    dry_run: bool = False,
) -> LaunchPlan:
    """Resume ``session``: in a new terminal window, or with ``here`` in this
    process (``chdir`` then ``exec``; it does not return). ``dry_run`` only
    builds the plan. A real failure also sends a desktop notification."""
    try:
        made = plan(session, provider, where, here)
        if dry_run:
            return made
        if here:
            try:
                before = os.getcwd()
            except OSError:
                before = None
            os.chdir(made.cwd)
            try:
                os.execvp(made.argv[0], made.argv)  # noqa: S606  (the provider's resume argv, no shell)
            except OSError:
                if before is not None:
                    os.chdir(before)
                raise
            return made  # reached only when exec is replaced (tests)
        starter(made.command, made.cwd)
        return made
    except LaunchFailed as exc:
        if not dry_run:
            _tell(exc.reason)
        raise
    except OSError as exc:
        failure = LaunchFailed(
            f"could not start {exc.filename or provider.binary}: {exc.strerror or exc}"
        )
        _tell(failure.reason)
        raise failure from exc


__all__ = ["LAUNCHER", "LaunchFailed", "LaunchPlan", "launch", "plan"]
