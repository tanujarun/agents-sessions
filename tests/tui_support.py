"""Scaffolding for the TUI tests: a fake engine that records every call and
never touches a store or starts a tool, a set of fake sessions, and a way to
read the screen as text. Imports no Textual at module level."""

import io
import sys
import threading
import time
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

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
    ``(name, argument)``; ``fail[name]`` is raised by that method."""

    def __init__(self, sessions=None, providers=PROVIDERS):
        self.sessions = list(default_sessions() if sessions is None else sessions)
        self.provider_list = list(providers)
        self.calls = []
        self.fail = {}
        self.verdicts = {}  # key -> (bool, reason); default (True, "")
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
        return index.where(session)

    def find_session(self, ref, index=None):
        self._record("find_session", ref)
        return self.by_key(ref)

    def can_import(self, session, index=None):
        self._record("can_import", session.key)
        if session.offline:
            return False, NOT_MOUNTED
        return self.verdicts.get(session.key, (True, ""))

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

    def import_session(self, ref, progress=None, index=None):
        self._record("import_session", ref)
        source = self.by_key(ref)
        if source.imported_as:  # a second import reports the existing copy
            return self.by_key(source.imported_as)
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
        return copy

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
