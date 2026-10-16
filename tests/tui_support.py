"""Scaffolding for the TUI tests: a fake engine that records every call and
never touches a store, mounts a drive or starts a tool, a set of fake
sessions, ways to read the screen (as text, and as the segments that would
go to the terminal), text no terminal may be sent, and a driver that runs a
program in a pseudo-terminal of its own. Imports no Textual at module
level."""

import fcntl
import io
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
import unicodedata
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Transitions are timed frames; the suites assert on still screens.
os.environ["AGENT_SESSIONS_MOTION"] = "0"

REPO = Path(__file__).resolve().parent.parent
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Imported for what it does when it is loaded: AGENT_SESSIONS_STATE points at
# a temp directory from then on, so no TUI test can write the user's own log.
import support  # noqa: E402, F401

from agent_sessions import textsafe  # noqa: E402
from agent_sessions.api import ContinueOptions, ImportOptions, UnimportReport  # noqa: E402
from agent_sessions.errors import UndoFailed  # noqa: E402
from agent_sessions.model import Session  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
PROVIDERS = [
    ("antigravity", "Antigravity"),
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("gemini", "Gemini"),
    ("grok", "Grok"),
]
WINDOWS = "/run/media/user/WINVOL"
NOT_MOUNTED = "the drive WINVOL is not mounted"  # the engine's refusal for an offline session
# The engine's refusals of an undo, and the tools it finds installed.
NO_RECORD = "no import of this session is recorded; nothing was removed"
USED_SINCE = (
    "the imported copy was used since the import: /home/user/.claude/x.jsonl was modified. "
    "Nothing was removed; a forced undo removes it all the same"
)
TOOLS = (("claude", "Claude"), ("codex", "Codex"), ("grok", "Grok"))
NATIVE_HASH = "aaaaaaaa"
FOREIGN_HASH = "bbbbbbbb"

Turn = namedtuple("Turn", "role text ts")
Plan = namedtuple("Plan", "cwd argv command note")


class FakeError(Exception):
    """Shaped like the engine's ApiError: ``reason`` is what to show."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def stamp(**ago) -> str:
    return (NOW - timedelta(**ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_session(
    provider,
    n,
    *,
    foreign=False,
    title=None,
    age=None,
    imported_as=None,
    cwd=None,
    note=None,
    messages=12,
    offline=False,
) -> Session:
    """A fake session; ``n`` makes it distinct. Newer for smaller ``age``
    (minutes ago). ``offline``: a foreign one whose drive is not mounted, as
    the engine lists it from its cache."""
    sid = f"{provider[:2]}{'f' if foreign else 'n'}-{n:04d}"
    age = age if age is not None else 10 + n * 7
    if foreign:
        store = f"{WINDOWS}/Users/someone/.{provider}"
        recorded = cwd or f"C:\\Work\\project-{n}"
        local = f"{WINDOWS}/Work/project-{n}"
        key = f"{provider}:{FOREIGN_HASH}:{sid}"
    else:
        store = f"/home/user/.{provider}"
        recorded = cwd or f"/home/user/code/project-{n}"
        local = recorded
        key = f"{provider}:{NATIVE_HASH}:{sid}"
    return Session(
        provider=provider,
        id=sid,
        key=key,
        title=title if title is not None else f"{provider} session {n}",
        cwd=recorded,
        cwd_local=None if offline else local,
        cwd_exists=not offline,
        started=stamp(minutes=age + 30),
        updated=stamp(minutes=age),
        messages=messages,
        store=store,
        origin="foreign" if foreign else "native",
        mount=WINDOWS if foreign else "/",
        mount_label="WINVOL" if foreign else "Linux",
        files=[],
        launchable=not foreign,
        imported_as=imported_as,
        note="drive not mounted" if offline else note,
        offline=offline,
    )


def default_sessions() -> list:
    """Thirty-odd sessions across four providers and both origins; Gemini
    has none, so its tab is empty."""
    sessions = []
    for n in range(12):
        sessions.append(make_session("claude", n))
    for n in range(12, 18):
        sessions.append(make_session("claude", n, foreign=True))
    for n in range(4):
        sessions.append(make_session("codex", n, age=5 + n * 11))
    for n in range(4, 7):
        sessions.append(make_session("codex", n, foreign=True, age=200 + n))
    for n in range(5):
        sessions.append(make_session("grok", n, age=3000 + n * 500))
    sessions.append(make_session("antigravity", 0, foreign=True, age=90000))
    sessions.append(make_session("antigravity", 1, age=40000))
    # A foreign session that already has a native copy.
    copy = make_session("claude", 90, title="Already imported", age=1)
    sessions.append(copy)
    sessions.append(
        make_session(
            "claude", 90, foreign=True, title="Already imported", age=2, imported_as=copy.key
        )
    )
    return sessions


class FakeIndex:
    def where(self, session) -> str:
        if session.origin == "native":
            return "Linux"
        if session.imported_as:
            return "C: · imported"
        return "C: · offline" if session.offline else "C:"


class FakeEngine:
    """The methods of ``tui.engine.Engine``. ``calls`` records every call as
    ``(name, argument)``; ``fail[name]`` is raised by that method. Nothing is
    mounted, started, imported or removed: the sessions it lists change, and
    that is all."""

    def __init__(self, sessions=None, providers=PROVIDERS):
        self.sessions = list(default_sessions() if sessions is None else sessions)
        self.provider_list = list(providers)
        self.calls = []
        self.fail = {}
        self.options = {}  # key -> ImportOptions; default: possible as it is
        self.import_args = []  # (key, target, experimental) of every import
        self.continue_answers = {}  # key -> ContinueOptions; default: every tool of TOOLS
        self.continue_folder = None  # the folder the default answer suggests
        self.used_since = {}  # native key -> what differs in a copy used since its import
        self.undo_left = []  # what an undo leaves in place: [(path, why)]
        self.volume_state = None  # of the drive in `volumes`; default: by what is listed
        self.mount_gate = None  # an Event: mount_drive waits for it
        self.listing_threads = []  # the thread of every list_folder call
        # method name -> what its call waits for first: an Event (30 s at
        # most) or a number of seconds. A call that does not come back.
        self.block = {}
        self.threads = {}  # method name -> the thread of its last call
        self.import_mark = None  # a file every finished import writes
        self.where_text = None  # what ``where`` says of every session, when set
        self.home_dir = None  # what ``home`` answers; None: this user's own
        self.turns = {}  # key -> list of Turn
        self.default_turns = [
            Turn("user", "hello there", stamp(minutes=5)),
            Turn("tool", "Read", None),
            Turn("assistant", "general reply", stamp(minutes=4)),
        ]
        self.last_quick = stamp(minutes=2)
        self.scan_progress = [("WINVOL", 41000)]
        self.scan_gate = None  # an Event: scan waits for it (or for cancel)
        self.on_scan = None  # called with the engine during a scan
        self.plan_note = None
        self.import_bytes = 12_000_000
        self.lock = threading.Lock()

    # --- helpers for the tests ---

    def _record(self, name, arg=None):
        with self.lock:
            self.calls.append((name, arg))
            self.threads[name] = threading.current_thread()
        wait = self.block.get(name)
        if isinstance(wait, threading.Event):
            wait.wait(30)
        elif wait:
            time.sleep(wait)
        if name in self.fail:
            raise self.fail[name]

    def called(self, name) -> list:
        with self.lock:
            return [arg for call, arg in self.calls if call == name]

    def by_key(self, key):
        for session in self.sessions:
            if session.key == key:
                return session
        raise FakeError(f"no session {key}")

    def listed(self, provider=None) -> list:
        """What the list shows, newest first: fresh objects every time, as a
        reloaded index gives."""
        rows = sorted(self.sessions, key=lambda s: s.key)
        rows.sort(key=lambda s: s.updated or "", reverse=True)
        return [
            Session.from_dict(s.to_dict())
            for s in rows
            if provider is None or s.provider == provider
        ]

    # --- the engine interface ---

    def providers(self):
        return list(self.provider_list)

    def load_index(self):
        self._record("load_index")
        return FakeIndex()

    def list_sessions(self, provider=None, index=None):
        self._record("list_sessions", provider)
        return self.listed(provider)

    def status(self, index=None):
        self._record("status")
        return {
            "last_quick": self.last_quick,
            "mounts": [
                {"path": "/", "label": "Linux", "mounted": True},
                {"path": "/home", "label": "Linux", "mounted": True},
                {"path": WINDOWS, "label": "WINVOL", "mounted": True},
                {"path": "/run/media/user/GONE", "label": "GONE", "mounted": False},
            ],
        }

    def where(self, session, index):
        if self.where_text is not None:
            return self.where_text + (" · offline" if session.offline else "")
        return index.where(session)

    def find_session(self, ref, index=None):
        self._record("find_session", ref)
        return self.by_key(ref)

    def import_options(self, ref, index=None, target=None):
        # A session that is handed over is judged as it is, as the engine does.
        session = ref if isinstance(ref, Session) else self.by_key(ref)
        self._record("import_options", session.key)
        if session.origin == "native":
            return ImportOptions(False, "this session is already on Linux")
        if session.imported_as:
            return ImportOptions(False, "already imported")
        if session.offline:
            return ImportOptions(False, NOT_MOUNTED)
        return self.options.get(session.key, ImportOptions(True))

    def continue_options(self, ref, index=None):
        self._record("continue_options", ref)
        session = self.by_key(ref)
        if ref in self.continue_answers:
            return self.continue_answers[ref]
        if session.offline:
            return ContinueOptions(False, NOT_MOUNTED)
        tools = tuple(sorted(TOOLS, key=lambda tool: tool[0] != session.provider))
        default = session.provider if tools[0][0] == session.provider else None
        return ContinueOptions(True, "", tools, default, self.continue_folder)

    def volumes(self, index=None):
        self._record("volumes")
        offline = any(session.offline for session in self.sessions)
        return [
            {
                "path": WINDOWS,
                "label": "WINVOL",
                "fs_label": "Windows",
                "uuid": "0123-ABCD",
                "fstype": "ntfs3",
                "letter": "C:",
                "state": self.volume_state or ("attached" if offline else "mounted"),
                "last_seen": self.last_quick,
                "stores": 1,
                "sessions": sum(1 for session in self.sessions if session.mount == WINDOWS),
            }
        ]

    def home(self):
        self._record("home")
        return self.home_dir or os.path.expanduser("~")

    def list_folder(self, directory):
        """A real listing (the tests complete in temp directories); which
        thread asked is kept, since it must never be the UI's."""
        self._record("list_folder", directory)
        self.listing_threads.append(threading.current_thread())
        with os.scandir(directory) as entries:
            return [(entry.name, entry.is_dir()) for entry in entries]

    def transcript(self, ref, index=None):
        self._record("transcript", ref)
        if self.by_key(ref).offline:  # at the call, as the engine does
            raise FakeError(NOT_MOUNTED)
        return iter(self.turns.get(ref, self.default_turns))

    def scan(self, deep=None, progress=None, cancel=None):
        self._record("scan", deep)
        for label, dirs in self.scan_progress:
            if progress is not None:
                progress(label, dirs)
        cancelled = False
        if self.scan_gate is not None:
            deadline = time.monotonic() + 10
            while not self.scan_gate.is_set() and time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                time.sleep(0.005)
        if self.on_scan is not None and not cancelled:
            self.on_scan(self)
        self.last_quick = stamp(seconds=1)
        return {
            "mode": "deep" if deep else "quick",
            "cancelled": cancelled,
            "seconds": 0.25,
            "sessions": len(self.sessions),
            "warnings": [],
        }

    def import_session(self, ref, progress=None, index=None, target=None, experimental=False):
        self._record("import_session", ref)
        self.import_args.append((ref, target, experimental))
        source = self.by_key(ref)
        if source.imported_as:  # a second import reports the existing copy
            return self.by_key(source.imported_as)
        options = self.options.get(ref, ImportOptions(True))
        if not options.possible and not (
            (options.needs_folder or options.experimental)
            and (target is not None or not options.needs_folder)
            and (experimental or not options.experimental)
        ):
            raise FakeError(options.reason)  # without what it needs, as the engine refuses
        if progress is not None:
            progress(0, self.import_bytes)
            progress(self.import_bytes, self.import_bytes)
        copy = Session.from_dict(source.to_dict())
        copy.key = f"{source.provider}:{NATIVE_HASH}:{source.id}"
        copy.origin, copy.mount, copy.mount_label = "native", "/", "Linux"
        copy.store = f"/home/user/.{source.provider}"
        copy.launchable, copy.imported_as = True, None
        source.imported_as = copy.key
        self.sessions.append(copy)
        if self.import_mark:
            Path(self.import_mark).write_text("imported", encoding="utf-8")
        return copy

    def unimport(self, ref, force=False, dry_run=False, index=None):
        """An import is "recorded" for every foreign session that has a
        native copy; taking it back unlists the copy."""
        self._record("unimport", (ref, force, dry_run))
        session = self.by_key(ref)
        if session.origin == "native":
            copy = session
            source = next((s for s in self.sessions if s.imported_as == copy.key), None)
        else:
            source = session
            copy = next((s for s in self.sessions if s.key == source.imported_as), None)
        if source is None or copy is None:
            raise UndoFailed(NO_RECORD, no_record=True)
        differences = list(self.used_since.get(copy.key, ()))
        if differences and not force:
            raise UndoFailed(USED_SINCE, differences=differences)
        files = [f"{copy.store}/sessions/{copy.id}.jsonl", f"{copy.store}/sessions/{copy.id}/a.txt"]
        report = UnimportReport(
            provider=copy.provider,
            id=copy.id,
            native_key=copy.key,
            source_key=source.key,
            removed=files + [f"{copy.store}/sessions/{copy.id}"],
            files=len(files),
            left=list(self.undo_left),
            differences=differences,
            forced=force,
            dry_run=dry_run,
            bytes=self.import_bytes,
        )
        if not dry_run:
            self.sessions = [s for s in self.sessions if s is not copy]
            source.imported_as = None
        return report

    def mount_drive(self, ref, progress=None, cancel=None):
        """Nothing is mounted: the sessions listed at ``ref`` stop being
        offline, as a scan after a mount would find them."""
        self._record("mount_drive", ref)
        if self.mount_gate is not None:
            deadline = time.monotonic() + 10
            while not self.mount_gate.is_set() and time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():  # the app is quitting
                    break
                time.sleep(0.005)
        for label, dirs in self.scan_progress:
            if progress is not None:
                progress(label, dirs)
        for session in self.sessions:
            if session.mount == ref and session.offline:
                session.offline, session.note = False, None
                session.cwd_exists, session.cwd_local = True, f"{ref}/Work/{session.id}"
        self.last_quick = stamp(seconds=1)
        return {"mount_point": ref, "scan": {"mode": "quick", "cancelled": False}}

    def continue_session(self, ref, tool=None, folder=None, index=None):
        self._record("continue_session", (ref, tool, folder))
        if self.by_key(ref).offline:
            raise FakeError(NOT_MOUNTED)
        command = [tool, "Continue an earlier session."]
        return Plan(folder, command, ["omarchy-launch-tui", *command], None)

    def open_session(self, ref, here=False, dry_run=False, index=None):
        # A dry run opens nothing: it is recorded under a name of its own.
        self._record("open_dry_run" if dry_run else "open_session", ref)
        session = self.by_key(ref)
        if session.offline:
            raise FakeError(NOT_MOUNTED)
        if session.origin != "native":
            raise FakeError("import first")
        return Plan(
            session.cwd,
            ["tool", "--resume", session.id],
            ["omarchy-launch-tui", "tool", "--resume", session.id],
            self.plan_note,
        )


def screen_text(app) -> str:
    """The current screen as plain text, one line per terminal row."""
    from rich.console import Console

    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    console.print(
        app.screen._compositor.render_update(
            full=True, screen_stack=app._background_screens, simplify=True
        )
    )
    return console.export_text()


async def settle(pilot, rounds: int = 2) -> None:
    """Let workers finish and their results reach the screen."""
    for _ in range(rounds):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


# --- text no terminal may be sent ---------------------------------------------------

# An escape sequence that sets the window title, one that clears the screen,
# a blink, the 8-bit CSI and OSC (C1), a bidirectional override and an
# isolate, a line separator, a byte order mark, NUL, a lone surrogate (what a
# byte that is not UTF-8 is read as), and text that looks like Rich markup.
HOSTILE = (
    "\x1b]0;PWNED\x07\x1b[2J\x1b[5mA\x9b31mB\x9d0;x\x9cC"
    "\u202eD\u2066E\u2028F\ufeffG\x00H\ud800I\udc9bJ"
    "[red]K[/red] [@click=app.quit]L[/] [/oops]"
)
# What is left of it on a screen that shows it whole: the markup, as text.
HOSTILE_MARK = "[red]K[/red] [@click=app.quit]L[/] [/oops]"


def hostile(tag) -> str:
    """Hostile text that can be told from every other one by ``tag``."""
    return f"{tag}{HOSTILE}"


def hostile_name(tag) -> str:
    """The same as one name of a path: no separator and no NUL, and without
    the surrogate no file name can hold."""
    return hostile(tag).replace("/", "|").replace("\x00", "").replace("\ud800", "")


def unsafe_chars(text) -> list:
    """The characters of ``text`` that must not reach a terminal: what
    ``textsafe`` replaces, every other control character, and surrogates."""
    return [ch for ch in text if ch in textsafe.UNSAFE or unicodedata.category(ch) in ("Cc", "Cs")]


def screen_segments(app) -> list:
    """Every piece of text the compositor would send to the terminal for a
    full repaint of the current screen (Rich's control segments, which are
    Textual's own cursor movements, left out)."""
    from rich.console import Console

    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    update = app.screen._compositor.render_update(
        full=True, screen_stack=app._background_screens, simplify=False
    )
    return [segment.text for segment in console.render(update) if not segment.control]


def unsafe_on_screen(app) -> list:
    """``(characters, the text they are in)`` for every piece of the screen
    that holds a character no terminal may be sent; empty for a clean one.
    The line feeds that end the rows are the compositor's own."""
    found = []
    for text in screen_segments(app):
        bad = unsafe_chars(text.replace("\n", ""))
        if bad:
            found.append((bad, text))
    return found


# --- a program in a pseudo-terminal of its own ---------------------------------------

PtyRun = namedtuple("PtyRun", "status output seconds")
"""What ``pty_run`` gives: the exit status (None: it had to be killed), all
it wrote, and the seconds from the last keys sent to its exit."""

_ESCAPES = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    rb"|\x1b[\]P_^][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC, DCS, APC, PM
    rb"|\x1b[()][0-9A-Za-z]|\x1b[=>78cDEHM]"
)


def plain(output) -> str:
    """Terminal output as text: the escape sequences taken out."""
    return _ESCAPES.sub(b"", bytes(output)).decode("utf-8", "replace")


def pty_run(argv, steps, env, cwd, size=(100, 30), timeout=30.0) -> PtyRun:
    """Run ``argv`` with a pseudo-terminal made here for its stdin, stdout
    and stderr (no terminal window: nothing is shown anywhere) and play
    ``steps`` to it. A step is ``(wait, keys)``: wait until the text
    ``wait`` has appeared in what the program wrote since the step before
    (a number: that many seconds), then write ``keys``. Raises
    ``AssertionError`` with what was on the terminal when a text does not
    come within ``timeout`` seconds."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
    try:
        child = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd, start_new_session=True
        )
    finally:
        os.close(slave)
    output = bytearray()

    def read(seconds) -> bool:
        """Take what the program writes for ``seconds``; False at its end."""
        until = time.monotonic() + seconds
        while True:
            ready, _, _ = select.select([master], [], [], max(until - time.monotonic(), 0))
            if not ready:
                return True
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO: the other end is closed
                return False
            if not data:
                return False
            output.extend(data)

    sent = time.monotonic()
    try:
        for wait, keys in steps:
            seen = len(output)
            deadline = time.monotonic() + (wait if isinstance(wait, (int, float)) else timeout)
            while isinstance(wait, (int, float)) or wait not in plain(output[seen:]):
                if time.monotonic() >= deadline or child.poll() is not None:
                    if isinstance(wait, (int, float)):
                        break
                    read(0.05)
                    raise AssertionError(
                        f"{wait!r} did not appear (exit status {child.poll()}); "
                        f"the terminal had: {plain(output)[-2000:]!r}"
                    )
                read(0.005)
            os.write(master, keys)
            sent = time.monotonic()
        deadline = sent + timeout
        while child.poll() is None and time.monotonic() < deadline:
            read(0.005)
        seconds = time.monotonic() - sent
        status = child.poll()
        while status is not None and read(0.05) and select.select([master], [], [], 0)[0]:
            pass  # what it wrote last
        return PtyRun(status, bytes(output), seconds)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)


def pty_env(root, **more) -> dict:
    """The environment for a program run by ``pty_run``: a terminal type, a
    UTF-8 locale, and our four overrides pointing into ``root``, so that the
    real engine finds no store and walks no drive."""
    root = Path(root)
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root / "home"),
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "AGENT_SESSIONS_HOME": str(root / "home"),
        "AGENT_SESSIONS_CACHE": str(root / "cache"),
        "AGENT_SESSIONS_STATE": str(root / "state"),
        "AGENT_SESSIONS_MOUNTS": "[]",
        "AGENT_SESSIONS_NOTIFY": "0",
        **more,
    }


# The app over the fake engine as a program of its own, for ``pty_run``:
# ``python -P -c PTY_APP <setup>``. ``-P`` keeps the directory it is started
# in off ``sys.path``; nothing here reads a store or starts anything.
PTY_APP = f"""
import sys
sys.path[:0] = [{_SRC!r}, {str(REPO / "tests")!r}]
import tui_support
from agent_sessions.tui.app import SessionsApp
app = SessionsApp(engine=tui_support.pty_engine(sys.argv[1]))
app.run()
sys.exit(app.return_code or 0)
"""

LONG_TURNS = 2000  # the pager's own limit
BLOCKED = 30.0  # seconds a call of a ``block:`` setup does not come back for


def pty_engine(setup) -> "FakeEngine":
    """The fake engine for ``PTY_APP``. ``setup`` is ``plain``;
    ``block:<method>``, a call of that method does not come back for
    ``BLOCKED`` seconds; ``import:<seconds>``, an import takes that long; or
    ``turns:<kind>``, the first session listed has ``LONG_TURNS`` turns of
    20,000 characters (``words``, or ``cjk`` without a single space). It
    lists one session more than the default ones: ``claude session 70``,
    whose drive is not mounted. When the environment names ``PTY_MARK``, a
    finished import writes that file."""
    engine = FakeEngine()
    engine.sessions.append(make_session("claude", 70, foreign=True, offline=True))
    kind, _, value = setup.partition(":")
    if kind == "block":
        engine.block[value] = BLOCKED
    elif kind == "import":
        engine.block["import_session"] = float(value)
    elif kind == "turns":
        text = {"words": "word " * 4000, "cjk": "\u4e2d" * 20000}[value]
        engine.turns[engine.listed()[0].key] = [Turn("assistant", text, None)] * LONG_TURNS
    engine.import_mark = os.environ.get("PTY_MARK")
    return engine
