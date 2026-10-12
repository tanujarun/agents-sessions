"""Build and run the resume command (platform layer).

The only module that starts a process. The Linux implementation opens a new
terminal window through ``omarchy-launch-tui`` where that exists (it runs
``setsid uwsm-app -- xdg-terminal-exec --app-id=<id> -e <tool> <args>``),
else through the first terminal it finds (``terminal_command``); the
terminal inherits the working directory it was started in.

It also answers whether a tool is installed at all (``tool_state``). A name
on PATH proves nothing here: most agent commands are small scripts that
install the tool when it is missing, and starting one of those would install
software. The answer is read from files; no program is run for it.

Two module-level hooks let a caller record instead of acting: ``starter``
(called as ``starter(command, cwd)`` to open the window) and ``notifier``
(called as ``notifier(reason)`` after a failed launch). Tests and the TUI
replace them, for example with ``mock.patch.object(launch, "starter", rec)``.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import textsafe
from .errors import LaunchFailed
from .fsutil import home, open_ro

if TYPE_CHECKING:
    from .model import Session
    from .providers.base import Provider

LAUNCHER = "omarchy-launch-tui"
XDG_TERMINAL = "xdg-terminal-exec"
# A command line that opens a terminal window; the tool's command is added at
# its end. It wins over everything ``terminal_command`` would find.
TERMINAL_ENV = "AGENT_SESSIONS_TERMINAL"
# The terminals that are looked for when nothing else names one, in this
# order: the program, and what stands between it and the command it is to
# run. Each has its own form, and the table is the one place that knows them.
#
# Where a form comes from: ``alacritty`` and ``kitty`` are installed on the
# machine this was written on, and their ``--help`` says it (alacritty: "-e,
# --command <COMMAND>...  Command and args to execute (must be last
# argument)"; kitty: "Usage: kitty [options] [program-to-run ...]"). The
# other six are not installed there and are taken from each terminal's own
# documentation, not run: foot(1) "foot [OPTIONS] [command [command_arguments]]",
# ghostty "-e <command>", "wezterm start [OPTIONS] [-- PROG...]",
# gnome-terminal(1) "gnome-terminal [OPTION...] [-- COMMAND ...]", konsole
# "-e <command>" (takes every argument after it), xterm(1) "-e program
# [arguments ...]" (must be last). None was started to try it.
TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("alacritty", ("-e",)),
    ("kitty", ()),
    ("foot", ()),
    ("ghostty", ("-e",)),
    ("wezterm", ("start", "--")),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xterm", ("-e",)),
)
_RUN = ("-e",)  # what a terminal the table does not know is given (``$TERMINAL -e``)
NOTIFIER = "notify-send"
NOTIFY_ENV = "AGENT_SESSIONS_NOTIFY"  # "0" switches desktop notifications off
NOTIFY_TITLE = "Cannot resume session"
# ``_from_session``: what stands in for every text of the second session, and
# the fields that are ours, not a store's.
_PROBE = "00000000-0000-4000-8000-000000000000"
_NOT_DATA = ("provider", "origin")


# --- is the tool installed? -------------------------------------------------

INSTALLED = "installed"
NOT_INSTALLED = "not installed (the command on PATH would install it)"
NOT_FOUND = "not found"

WRAPPER_MAX = 4096  # bytes; the wrappers seen are 111 to 419 bytes long
WRAPPER_COMMANDS = 8  # "a script of a few lines": more commands than this is a program
SEARCH_DEPTH = 3  # directories below a version in which the tool's executable may lie
SEARCH_ENTRIES = 4000  # directory entries looked at for one tool, at most


@dataclass(frozen=True)
class ToolState:
    """What is behind a command name. ``path`` is what PATH gives for it
    (None: nothing); ``wrapper`` says that this is a script which installs
    the tool when it is missing; ``installed`` that the tool itself is there
    to be started; ``detail`` is one of ``INSTALLED``, ``NOT_INSTALLED``,
    ``NOT_FOUND``, for a person."""

    path: str | None
    installed: bool
    wrapper: bool
    detail: str


def _wrapper(path: str) -> tuple[str, str] | None:
    """``(mise spec, tool)`` when the file is an installing wrapper, else
    None. Recognised by its text (docs/FORMATS.md "What is on PATH"):

        #!/bin/bash
        export MISE_MINIMUM_RELEASE_AGE=0
        mise use -g --quiet "<spec>" || exit 1
        exec mise x "<spec>" -- "<tool>" "$@"

    A short script whose last command is that ``exec``; the quoting may
    differ and ``mise exec`` is the same as ``mise x``. Anything else, a
    real program included, is not a wrapper. The file is read, never run.
    """
    try:
        with open_ro(path) as fh:  # regular files only, and it never waits
            head = fh.read(WRAPPER_MAX + 1)
    except OSError:
        return None
    if len(head) > WRAPPER_MAX or not head.startswith(b"#!") or b"\0" in head:
        return None
    try:
        lines = head.decode("utf-8").splitlines()[1:]
    except UnicodeDecodeError:
        return None
    commands = [
        line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if not commands or len(commands) > WRAPPER_COMMANDS:
        return None
    try:
        words = shlex.split(commands[-1])
    except ValueError:  # quotes that do not close
        return None
    if (
        len(words) != 7
        or words[0] != "exec"
        or os.path.basename(words[1]) != "mise"
        or words[2] not in ("x", "exec")
        or words[4] != "--"
        or words[6] != "$@"
    ):
        return None
    spec, tool = words[3], words[5]
    if not spec or spec.startswith("-") or not tool or tool != os.path.basename(tool):
        return None
    return spec, tool


def _install_name(spec: str) -> str:
    """The name of the directory mise keeps a tool in, for the spec a
    wrapper passes to ``mise x``.

    The rule is read off this machine's ``~/.local/share/mise/installs``
    (2026-10-12), where the specs ``claude``, ``codex``, ``opencode``,
    ``npm:playwright`` and ``npm:@xai-official/grok`` have the directories
    ``claude``, ``codex``, ``opencode``, ``npm-playwright`` and
    ``npm-xai-official-grok``: lower case, every run of characters that are
    not letters or digits becomes one ``-``, none at either end. It was not
    checked against mise's code. Where it is wrong for some other spec, the
    directory is not found and the tool counts as not installed: a launch is
    refused, nothing is installed. A version (``tool@1.2``) is not part of
    the name.
    """
    cut = spec.rfind("@")
    if cut > 0 and spec[cut - 1] not in ":/":  # not the scope of ``npm:@scope/tool``
        spec = spec[:cut]
    return re.sub(r"[^a-z0-9]+", "-", spec.lower()).strip("-")


def _mise_installs() -> str:
    """Where mise keeps what it installed: ``installs`` below its data
    directory, which is ``$MISE_DATA_DIR``, else ``$XDG_DATA_HOME/mise``,
    else ``~/.local/share/mise``. A value that is not an absolute path is
    passed over, as the XDG rules have it."""
    data = os.environ.get("MISE_DATA_DIR", "")
    if not os.path.isabs(data):
        share = os.environ.get("XDG_DATA_HOME", "")
        if not os.path.isabs(share):
            share = os.path.join(os.path.expanduser("~"), ".local", "share")
        data = os.path.join(share, "mise")
    return os.path.join(data, "installs")


def _executable_below(root: str, tool: str) -> bool:
    """Whether some version directory in ``root`` holds an executable file
    named ``tool``, or a link to one, at most ``SEARCH_DEPTH`` directories
    down: ``claude/<version>/claude``, ``codex/<version>/bin/codex``,
    ``npm-…/<version>/node_modules/.bin/grok``. A version directory with
    nothing but a ``package.json`` (an install that did not finish) has
    none. Listings and stats only."""
    try:
        versions = sorted(os.listdir(root), reverse=True)
    except OSError:
        return False
    budget = SEARCH_ENTRIES
    seen = set()
    for version in versions:
        start = os.path.join(root, version)
        real = os.path.realpath(start)  # ``latest`` and ``1.0`` are links to a version
        if real in seen or not os.path.isdir(start):
            continue
        seen.add(real)
        level = [start]
        for depth in range(SEARCH_DEPTH):
            below = []
            for directory in level:
                candidate = os.path.join(directory, tool)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    return True
                if depth + 1 == SEARCH_DEPTH:
                    continue
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            budget -= 1
                            if budget < 0:
                                return False
                            if entry.is_dir(follow_symlinks=False):
                                below.append(entry.path)
                except OSError:
                    continue
            level = sorted(below)
    return False


def _inspect(path: str) -> ToolState:
    found = _wrapper(path)
    if found is None:
        return ToolState(path, True, False, INSTALLED)  # a program, or a script of another kind
    spec, tool = found
    if _executable_below(os.path.join(_mise_installs(), _install_name(spec)), tool):
        return ToolState(path, True, True, INSTALLED)
    return ToolState(path, False, True, NOT_INSTALLED)


_inspected: dict[tuple[str, str], ToolState] = {}


def tool_state(binary: str) -> ToolState:
    """Whether the tool behind the command ``binary`` is installed: ``not
    found`` when PATH has no such command, ``not installed`` when the command
    is a wrapper whose tool is missing from mise's installs, ``installed``
    otherwise (a wrapper whose tool is there, a real program, a script that
    is not such a wrapper).

    Files only; no program is started. What a file says is remembered for
    the life of the process (``forget_tools`` drops it), so asking for every
    session of a listing reads a wrapper once.
    """
    path = shutil.which(binary) if binary else None
    if path is None:
        return ToolState(None, False, False, NOT_FOUND)
    key = (path, _mise_installs())
    state = _inspected.get(key)
    if state is None:
        state = _inspected[key] = _inspect(path)
    return state


def forget_tools() -> None:
    """Look again next time: a scan calls this, so a tool installed while
    the program runs is seen by the next scan."""
    _inspected.clear()


# --- launch -----------------------------------------------------------------


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


def terminal_command(app_id: str, argv: list[str]) -> list[str]:
    """The command that opens a terminal window and runs ``argv`` in it.

    The first of these that is there:

    1. ``AGENT_SESSIONS_TERMINAL``, a command line of the user's own (split
       the way a shell splits words, ``shlex.split``; no shell runs it);
       ``argv`` is added at its end. It is a decision, not a hint: when it
       cannot be split or its program is not found, that is the failure;
    2. ``omarchy-launch-tui --app-id=<app_id> ...``, this desktop's launcher;
    3. ``xdg-terminal-exec --app-id=<app_id> -- ...``, the desktop's default
       terminal. The form is the usage line of its ``--help`` (0.14.3:
       "xdg-terminal-exec [options] [--] [command [arguments ...]]", with
       "--app-id=app-id  set app-id (Wayland) or window class (X11)");
    4. ``$TERMINAL``, with the form the table has for that program, else
       ``-e``. One that is not on PATH is passed over;
    5. the first terminal of ``TERMINALS`` that is installed.

    Only the first three give the window the tool's ``app_id``. Raises
    ``LaunchFailed`` when there is none, naming the variable that settles it.
    Files only (``shutil.which``); nothing is started.
    """
    chosen = os.environ.get(TERMINAL_ENV, "").strip()
    if chosen:
        try:
            words = shlex.split(chosen)
        except ValueError as exc:  # quotes that do not close
            raise LaunchFailed(f"{TERMINAL_ENV} is not a command line ({exc})") from None
        if not shutil.which(words[0]):
            raise LaunchFailed(f"{TERMINAL_ENV} names a terminal that was not found: {words[0]}")
        return [*words, *argv]
    if shutil.which(LAUNCHER):
        return [LAUNCHER, f"--app-id={app_id}", *argv]
    if shutil.which(XDG_TERMINAL):
        return [XDG_TERMINAL, f"--app-id={app_id}", "--", *argv]
    forms = dict(TERMINALS)
    try:
        words = shlex.split(os.environ.get("TERMINAL", ""))
    except ValueError:
        words = []  # not ours to complain about: the table is tried instead
    if words and shutil.which(words[0]):
        return [*words, *forms.get(os.path.basename(words[0]), _RUN), *argv]
    for program, form in TERMINALS:
        if shutil.which(program):
            return [program, *form, *argv]
    raise LaunchFailed(
        f"no terminal was found to open a window with; set {TERMINAL_ENV} to the command "
        "that starts yours (the tool's command is added at its end)"
    )


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


def _from_session(session: Session, provider: Provider, argv: list[str]) -> list[str]:
    """The elements of a resume command that came from session data.

    The command is built a second time for a session that has ``_PROBE`` in
    place of every text it holds; an element that differs between the two
    depends on the session. An adapter does not have to declare anything for
    this, so a new one cannot forget to. When the two commands cannot be
    compared (the second build fails, or has another length), every element
    after the program's name counts as session data.
    """
    changes: dict[str, Any] = {}
    for field in dataclasses.fields(session):
        value = getattr(session, field.name)
        if field.name in _NOT_DATA:
            continue
        if isinstance(value, list):
            changes[field.name] = [_PROBE for _item in value]
        elif isinstance(value, str) and value:
            changes[field.name] = _PROBE
    try:
        other = [str(arg) for arg in provider.resume_argv(dataclasses.replace(session, **changes))]
    except Exception:
        return argv[1:]
    if len(other) != len(argv):
        return argv[1:]
    return [arg for arg, probe in zip(argv[1:], other[1:], strict=True) if arg != probe]


def plan(
    session: Session, provider: Provider, where: str | None = None, here: bool = False
) -> LaunchPlan:
    """The resume command for ``session``; starts nothing.

    ``where`` names the place a foreign session is on, for the refusal.
    Raises ``LaunchFailed`` with the reason when it cannot be resumed.

    A session's fields are data from a file, possibly one that came from
    another drive. Two rules keep them from being read as options of the
    tool, each on its own: the id must be in the form the provider's ids
    have, and no element of the command that came from the session may start
    with ``-`` or hold a NUL. (``--`` in front of the id is not the answer:
    for ``--resume <id>`` and ``--conversation <id>`` the id is an option's
    value, and ``--`` there would change what the command means.)
    """
    label = provider.label or provider.name
    if session.origin != "native":
        place = where or session.mount_label or "another drive"
        raise LaunchFailed(f"this session is on {place}; import it first")
    if not provider.resumable:
        raise LaunchFailed(
            f"{label} sessions cannot be resumed here: the {label} CLI is not installed"
        )
    if not provider.valid_id(session.id):
        raise LaunchFailed(
            f"the id of this session is not in the form {label} ids have; it was not resumed"
        )
    argv = [str(arg) for arg in provider.resume_argv(session)]
    tool = argv[0] if argv else provider.binary
    state = tool_state(tool) if argv else ToolState(None, False, False, NOT_FOUND)
    if state.path is None:
        raise LaunchFailed(f"{tool} is not installed (not found on PATH)")
    if not state.installed:
        # Starting the wrapper would install the tool: never from here.
        raise LaunchFailed(f"{tool} is {state.detail}")
    if any(arg.startswith("-") for arg in _from_session(session, provider, argv)):
        raise LaunchFailed(
            f"the data of this session would be read as an option of {argv[0]}; it was not resumed"
        )
    if any("\0" in arg for arg in argv):
        raise LaunchFailed("the data of this session holds a NUL character; it was not resumed")
    cwd, note = _directory(session, provider)
    if here:
        return LaunchPlan(cwd, argv, list(argv), note)
    return LaunchPlan(cwd, argv, terminal_command(provider.app_id, argv), note)


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


__all__ = [
    "INSTALLED",
    "LAUNCHER",
    "NOT_FOUND",
    "NOT_INSTALLED",
    "TERMINALS",
    "TERMINAL_ENV",
    "XDG_TERMINAL",
    "LaunchFailed",
    "LaunchPlan",
    "ToolState",
    "forget_tools",
    "launch",
    "plan",
    "terminal_command",
    "tool_state",
]
