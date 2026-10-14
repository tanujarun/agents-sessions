"""The TUI driven through Textual's headless pilot, against a fake engine.

Nothing here starts a tool, imports into a store or reads a real index: the
engine is ``tui_support.FakeEngine``, which only records what it was asked.
Needs Textual, so the whole module skips on a Python without it
(``make test-tui`` runs it under ``.venv``)."""

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _path in (str(REPO / "src"), str(REPO / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import tui_support as support  # noqa: E402
from tui_support import (  # noqa: E402
    ContinueOptions,
    FakeEngine,
    FakeError,
    ImportOptions,
    Turn,
    make_session,
    screen_text,
    settle,
)

from agent_sessions.errors import MountFailed, UndoFailed  # noqa: E402

try:
    import textual
except ImportError:
    textual = None  # type: ignore[assignment]  # the suite then skips itself

if textual is not None:
    from rich.cells import cell_len
    from textual import events

    from agent_sessions.tui import app as app_mod
    from agent_sessions.tui import view
    from agent_sessions.tui.app import (
        HelpScreen,
        MainScreen,
        SessionList,
        SessionsApp,
        TranscriptScreen,
    )

NATIVE = "claude:aaaaaaaa:cln-0000"  # "claude session 0", third row of Claude
FOREIGN = "claude:bbbbbbbb:clf-0012"  # "claude session 12", on C:
FOREIGN_COPY = "claude:aaaaaaaa:clf-0012"  # what importing FOREIGN makes
IMPORTED = "claude:bbbbbbbb:clf-0090"  # foreign, already imported
IMPORTED_COPY = "claude:aaaaaaaa:cln-0090"
OFFLINE = "claude:bbbbbbbb:clf-0070"  # foreign, its drive is not mounted
OFFLINE_IMPORTED = "claude:bbbbbbbb:clf-0091"  # the same, and imported before
OFFLINE_COPY = "claude:aaaaaaaa:cln-0091"
AGY = "antigravity:bbbbbbbb:anf-0000"  # foreign, on C:; no tool of its own is installed
POSSIBLE_PROMPT = (
    "On C: [i] import and resume  [I] import only  [t] into another folder  "
    "[c] continue as new  [v] view  [Esc] cancel"
)
NEEDS_FOLDER = ImportOptions(
    False, "the project folder C:\\Work\\project-12 was not found on any mounted drive", True
)
WARNING = (
    "Experimental: no imported Antigravity conversation has been resumed by hand yet, "
    "and agy starts a new conversation without an error when it cannot read a copy. "
    "After the first resume, check that the old turns are on the screen."
)
EXPERIMENTAL = ImportOptions(False, "experimental, done only when asked for", False, True, WARNING)
IMPOSSIBLE = ImportOptions(False, "Gemini chats cannot be imported")


async def until(condition, timeout: float = 5.0, what: str = "condition") -> None:
    """Wait for something a worker or a timer will make true."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


@unittest.skipIf(textual is None, "Textual is not installed (run under .venv: make test-tui)")
class TuiCase(unittest.IsolatedAsyncioTestCase):
    size = (120, 40)

    def setUp(self):
        patcher = mock.patch.object(app_mod, "VERDICT_DELAY", 0.01) if textual else None
        if patcher:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.engine = FakeEngine()

    def make_app(self, engine=None):
        if engine is not None:
            self.engine = engine
        self.app = SessionsApp(engine=self.engine, clock=lambda: support.NOW, tz=UTC)
        return self.app

    def started(self, size=None, engine=None):
        return self.make_app(engine).run_test(size=size or self.size)

    # --- reading the app ---

    @property
    def main(self) -> "MainScreen":
        return self.app.screen_stack[0]

    @property
    def rows(self) -> "SessionList":
        return self.main.session_list

    def selected(self):
        row = self.rows.current
        return row.key if row else None

    def text(self) -> str:
        return screen_text(self.app)

    def status(self) -> str:
        return self.text().splitlines()[0]

    def titles(self) -> list:
        return [row.title for row in self.rows.rows]

    def opened(self) -> list:
        return self.engine.called("open_session")

    def imported(self) -> list:
        return self.engine.called("import_session")

    def mounted(self) -> list:
        return self.engine.called("mount_drive")

    def continued(self) -> list:
        return self.engine.called("continue_session")

    def undone(self) -> list:
        return self.engine.called("unimport")

    def acted(self) -> list:
        """Everything the engine was asked to do to the machine."""
        undone = [call for call in self.undone() if not call[2]]  # a dry run removes nothing
        return self.opened() + self.imported() + self.mounted() + self.continued() + undone

    def exited(self) -> bool:
        return self.app.return_code is not None

    def prompt(self) -> str:
        """The prompt's text, or "" when none is shown."""
        widget = self.main.query_one("#prompt")
        return str(widget.render()) if widget.display else ""

    @property
    def folder(self):
        return self.main.folder_input

    async def select(self, pilot, key):
        """Put the selection on a session by key (All tab if need be). For a
        row that can be asked about, the detail pane has its answer on
        return, as it has for a user who looks before pressing a key."""
        keys = [row.key for row in self.rows.rows]
        if key not in keys:
            await pilot.press("0")
            keys = [row.key for row in self.rows.rows]
        self.rows.select(keys.index(key))
        await settle(pilot)
        self.assertEqual(self.selected(), key)
        session = self.main.snapshot.sessions[key]
        if session.origin != "native" and not session.imported_as and app_mod.VERDICT_DELAY < 1:
            await until(lambda: key in self.main._verdicts, what="the import verdict")
            await pilot.pause()

    async def mode_is(self, pilot, mode, timeout=5.0):
        """Wait for a prompt (or the list) that a worker's answer brings."""
        deadline = time.monotonic() + timeout
        while self.main.mode != mode:
            if time.monotonic() > deadline:
                raise AssertionError(f"mode is {self.main.mode!r}, not {mode!r}: {self.status()}")
            await asyncio.sleep(0.01)
        await pilot.pause()

    async def type_folder(self, pilot, text):
        """Replace what the folder input holds, as typing over it would."""
        self.assertEqual(self.main.mode, "folder")
        self.folder.value = text
        self.folder.cursor_position = len(text)
        await pilot.pause()

    async def message(self, fragment, timeout=5.0):
        await until(lambda: fragment in self.status(), timeout, f"status line with {fragment!r}")

    def check_frame(self, width, height):
        """The screen is exactly the terminal: its lines, and none wider."""
        lines = self.text().splitlines()
        self.assertEqual(len(lines), height)
        for line in lines:
            self.assertLessEqual(cell_len(line), width)
        return lines


class StartTest(TuiCase):
    async def test_cached_sessions_show_before_the_scan_finishes(self):
        self.engine.scan_gate = threading.Event()  # the startup scan hangs
        async with self.started() as pilot:
            await until(lambda: self.main.loaded and self.rows.rows, what="cached rows")
            await pilot.pause()
            text = self.text()
            self.assertIn("Already imported", text)
            self.assertIn("claude session 0", text)
            self.assertIn("scanning", text.splitlines()[0])
            self.assertEqual(
                self.engine.called("scan"), [None]
            )  # the engine picks the depth; running
            self.assertIsNotNone(self.main._scan)
            self.assertEqual(len(self.rows.rows), 20)
            # Now the scan ends, having found a new session.
            self.engine.on_scan = lambda e: e.sessions.append(
                make_session("claude", 500, title="Brand new session", age=0)
            )
            self.engine.scan_gate.set()
            await until(lambda: self.main._scan is None, what="scan end")
            await settle(pilot)
            self.assertEqual(self.titles()[0], "Brand new session")
            self.assertEqual(len(self.rows.rows), 21)
            self.assertIn("scanned just now", self.status())
            self.assertNotIn("scanning", self.status())
            self.assertNotIn("rescan", self.status())  # the startup scan is silent
            self.assertEqual(self.opened(), [])

    async def test_layout_regions_top_to_bottom(self):
        async with self.started() as pilot:
            await settle(pilot)
            lines = self.text().splitlines()
            self.assertEqual(len(lines), 40)
            self.assertTrue(lines[0].strip().startswith("agent-sessions"))
            self.assertIn("2 drives · 34 sessions · scanned", lines[0])
            self.assertEqual(
                lines[1].split(),
                [
                    "1",
                    "Claude",
                    "20",
                    "2",
                    "Codex",
                    "7",
                    "3",
                    "Grok",
                    "5",
                    "4",
                    "Antigravity",
                    "2",
                    "5",
                    "Gemini",
                    "0",
                    "0",
                    "All",
                    "34",
                ],
            )
            self.assertEqual(lines[3].split(), ["TITLE", "PROJECT", "AGE", "WHERE"])
            self.assertEqual(lines[4].split(), ["Already", "imported", "project-90", "1m", "Linux"])
            self.assertEqual(lines[5].split()[-4:], ["2m", "C:", "·", "imported"])
            text = "\n".join(lines)
            for label in ("id", "messages", "updated", "cwd", "local", "store", "note", "import"):
                self.assertRegex(text, rf"(?m)^\s*{label}\s|\s{label}\s")
            self.assertIn("↑↓ move  ←→ provider  Enter resume", lines[-2])
            self.assertIn("/ filter  r rescan  R full rescan  ? help  q quit", lines[-1])
            # First tab, first row, list focused.
            self.assertEqual(self.main.tab_name, "claude")
            self.assertEqual(self.rows.cursor, 0)
            self.assertIs(self.app.focused, self.rows)

    async def test_rows_are_newest_first_native_and_foreign_mixed(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("0")
            keys = [row.key for row in self.rows.rows]
            self.assertEqual(keys, [s.key for s in self.engine.listed()])
            flags = [row.foreign for row in self.rows.rows]
            self.assertIn(True, flags)
            self.assertIn(False, flags)
            self.assertNotEqual(flags, sorted(flags))  # really interleaved

    async def test_list_is_one_widget_however_many_rows(self):
        sessions = [make_session("claude", n, foreign=n % 3 == 0, age=n) for n in range(5000)]
        async with self.started(engine=FakeEngine(sessions)) as pilot:
            await settle(pilot)
            self.assertEqual(len(self.rows.rows), 5000)
            self.assertEqual(len(self.rows.children), 0)
            self.assertLess(len(list(self.main.walk_children())), 60)
            started = time.monotonic()
            await pilot.press("G")
            await pilot.press("pageup", "pageup", "k", "k")
            await pilot.press("slash", "4", "9", "9", "9")
            await settle(pilot)
            elapsed = time.monotonic() - started
            self.assertEqual(self.titles(), ["claude session 4999"])
            self.assertLess(elapsed, 5.0)
            await pilot.press("escape", "G")
            self.assertEqual(self.rows.cursor, 4999)
            self.assertIn("claude session 4999", self.text())

    async def test_empty_index(self):
        async with self.started(engine=FakeEngine([])) as pilot:
            await settle(pilot)
            text = self.text()
            self.assertIn("No sessions found", text)
            self.assertIn("0 sessions", text.splitlines()[0])
            self.assertIn("1 Claude 0", text)
            line = next(line for line in text.splitlines() if "No sessions found" in line)
            left = len(line) - len(line.lstrip())
            right = len(line) - len(line.rstrip())
            self.assertLessEqual(abs(left - right), 1)  # centred
            for key in (
                "down",
                "up",
                "G",
                "g",
                "pagedown",
                "enter",
                "o",
                "i",
                "t",
                "c",
                "u",
                "m",
                "v",
                "0",
                "3",
                "left",
                "right",
                "tab",
                "escape",
                "slash",
                "x",
                "escape",
            ):
                await pilot.press(key)
            await settle(pilot)
            self.assertIn("No sessions found", self.text())
            self.assertIsNone(self.selected())
            self.assertEqual(self.acted() + self.undone(), [])
            self.assertEqual(self.engine.called("transcript"), [])
            self.assertFalse(self.exited())
            self.assertIs(self.app.screen, self.main)

    async def test_no_providers_at_all(self):
        async with self.started(engine=FakeEngine([], providers=[])) as pilot:
            await settle(pilot)
            await pilot.press("right", "left", "1", "0", "down", "enter")
            self.assertIn("0 All 0", self.text())
            self.assertFalse(self.exited())


class TabTest(TuiCase):
    async def test_arrows_tab_and_letters_cycle(self):
        async with self.started() as pilot:
            await settle(pilot)
            order = ["claude", "codex", "grok", "antigravity", "gemini", "all"]
            for key in ("right", "l", "tab", "right", "right", "right"):
                await pilot.press(key)
                order.append(order.pop(0))
                self.assertEqual(self.main.tab_name, order[0], key)
            self.assertEqual(self.main.tab_name, "claude")  # wrapped round
            for key, expected in (
                ("left", "all"),
                ("h", "gemini"),
                ("shift+tab", "antigravity"),
                ("left", "grok"),
            ):
                await pilot.press(key)
                self.assertEqual(self.main.tab_name, expected, key)
            self.assertIs(self.app.focused, self.rows)  # Tab did not move the focus

    async def test_number_keys(self):
        async with self.started() as pilot:
            await settle(pilot)
            counts = {
                "1": ("claude", 20),
                "2": ("codex", 7),
                "3": ("grok", 5),
                "4": ("antigravity", 2),
                "5": ("gemini", 0),
                "0": ("all", 34),
            }
            for key in ("3", "5", "0", "2", "4", "1"):
                await pilot.press(key)
                name, count = counts[key]
                self.assertEqual(self.main.tab_name, name)
                self.assertEqual(len(self.rows.rows), count)
                if name != "all":
                    self.assertTrue(all(row.provider == name for row in self.rows.rows))
            await pilot.press("7")  # no such tab
            self.assertEqual(self.main.tab_name, "claude")

    async def test_click_a_tab(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.click("#tab-codex")
            self.assertEqual(self.main.tab_name, "codex")
            self.assertEqual(len(self.rows.rows), 7)
            await pilot.click("#tab-all")
            self.assertEqual(self.main.tab_name, "all")
            await pilot.click("#tab-gemini")
            self.assertEqual(self.main.tab_name, "gemini")
            self.assertIn("No sessions found", self.text())
            active = [tab.id for tab in self.main.query(".tab.-active")]
            self.assertEqual(active, ["tab-gemini"])
            self.assertEqual(self.opened(), [])

    async def test_all_tab_adds_a_provider_column(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.assertEqual(
                self.text().splitlines()[3].split(), ["TITLE", "PROJECT", "AGE", "WHERE"]
            )
            await pilot.press("0")
            lines = self.text().splitlines()
            self.assertEqual(lines[3].split(), ["TITLE", "PROVIDER", "PROJECT", "AGE", "WHERE"])
            self.assertIn("Codex", "\n".join(lines[4:12]))
            self.assertIn("Claude", lines[4])

    async def test_each_tab_remembers_its_selection(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("down", "down", "down")
            claude_key = self.selected()
            await pilot.press("2", "down")
            codex_key = self.selected()
            self.assertEqual(self.rows.cursor, 1)
            await pilot.press("3", "1")
            self.assertEqual(self.selected(), claude_key)
            await pilot.press("2")
            self.assertEqual(self.selected(), codex_key)
            await pilot.press("0")  # All shows the same session selected
            self.assertEqual(self.selected(), codex_key)


class MoveTest(TuiCase):
    size = (100, 24)

    async def test_movement_keys(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("0")
            count = len(self.rows.rows)
            page = self.rows.size.height - 1
            self.assertGreater(count, self.rows.size.height)  # the list scrolls
            steps = [
                ("down", 1),
                ("j", 2),
                ("j", 3),
                ("up", 2),
                ("k", 1),
                ("k", 0),
                ("k", 0),
                ("pagedown", page),
                ("pagedown", 2 * page),
                ("pageup", page),
                ("end", count - 1),
                ("down", count - 1),
                ("home", 0),
                ("G", count - 1),
                ("g", 0),
                ("pageup", 0),
                ("G", count - 1),
                ("pagedown", count - 1),
            ]
            for key, expected in steps:
                await pilot.press(key)
                self.assertEqual(self.rows.cursor, expected, key)
                top = self.rows.top
                self.assertTrue(top <= self.rows.cursor < top + self.rows.size.height, key)

    async def test_detail_pane_follows_the_selection(self):
        gone = self.engine.by_key(FOREIGN)
        gone.cwd_exists = False
        gone.note = "recorded folder is gone"
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            session = self.engine.by_key(NATIVE)
            text = self.text()
            self.assertIn(session.id, text)
            self.assertIn("/home/user/code/project-0   (exists)", text)
            self.assertIn("/home/user/.claude", text)
            self.assertIn(view.local_time(session.updated, UTC), text)
            self.assertEqual(str(self.main.query_one("#detail-import").render()), "-")
            await self.select(pilot, FOREIGN)
            text = self.text()
            self.assertIn("C:\\Work\\project-12", text)
            self.assertIn("(not found)", text)
            self.assertIn("/run/media/user/WINVOL/Users/someone/.claude", text)
            self.assertIn("recorded folder is gone", text)

    async def test_import_verdict_is_fetched_off_thread_and_cached(self):
        self.engine.options[FOREIGN] = ImportOptions(False, "this format cannot be imported safely")
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await until(lambda: FOREIGN in self.main._verdicts, what="verdict")
            await pilot.pause()
            self.assertIn("not possible: this format cannot be imported safely", self.text())
            await pilot.press("up", "down", "up", "down")
            await asyncio.sleep(0.1)
            await settle(pilot)
            self.assertEqual(self.engine.called("import_options").count(FOREIGN), 1)
            # A native row and an already-imported row never ask.
            await self.select(pilot, NATIVE)
            await self.select(pilot, IMPORTED)
            await asyncio.sleep(0.1)
            await settle(pilot)
            self.assertIn("already imported", self.text())
            self.assertNotIn(NATIVE, self.engine.called("import_options"))
            self.assertNotIn(IMPORTED, self.engine.called("import_options"))

    async def test_the_detail_pane_says_what_the_import_can_be(self):
        self.engine.options[FOREIGN] = NEEDS_FOLDER
        self.engine.options[AGY] = EXPERIMENTAL

        def detail() -> str:
            return str(self.main.query_one("#detail-import").render())

        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            self.assertEqual(detail(), "possible into a folder you choose")
            await self.select(pilot, AGY)
            self.assertEqual(detail(), "experimental only")
            await self.select(pilot, "codex:bbbbbbbb:cof-0004")
            self.assertEqual(detail(), "possible")
            self.assertEqual(self.acted(), [])

    async def test_click_selects_and_the_wheel_scrolls(self):
        from textual.events import MouseScrollDown, MouseScrollUp

        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("0")
            await pilot.click("#list", offset=(10, 3))
            self.assertEqual(self.rows.cursor, 3)
            # The wheel scrolls the list without moving the selection.
            for _ in range(3):
                await pilot._post_mouse_events([MouseScrollDown], "#list", offset=(10, 2))
            await pilot.pause()
            top = self.rows.top
            self.assertGreater(top, 0)
            self.assertEqual(self.rows.cursor, 3)
            self.assertNotIn("Already imported", self.text())  # the first rows are gone
            await pilot.click("#list", offset=(10, 2))  # rows under the pointer
            self.assertEqual(self.rows.cursor, top + 2)
            for _ in range(10):
                await pilot._post_mouse_events([MouseScrollUp], "#list", offset=(10, 2))
            await pilot.pause()
            self.assertEqual(self.rows.top, 0)
            self.assertEqual(self.rows.cursor, top + 2)
            await pilot.press("k")  # keys bring it back in view
            visible = range(self.rows.top, self.rows.top + self.rows.size.height)
            self.assertIn(self.rows.cursor, visible)
            self.assertEqual(self.opened(), [])
            self.assertFalse(self.exited())

    async def test_empty_filter_box_goes_when_clicked_away(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("slash")
            self.assertTrue(self.main.query_one("#filterbar").display)
            await pilot.click("#list", offset=(10, 2))
            await pilot.pause()
            self.assertFalse(self.main.query_one("#filterbar").display)
            self.assertEqual(self.rows.cursor, 2)
            await pilot.press("j")
            self.assertEqual(self.rows.cursor, 3)


class FilterTest(TuiCase):
    async def test_filter_narrows_and_escape_clears(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("slash")
            self.assertIs(self.app.focused, self.main.filter_input)
            self.assertTrue(self.main.query_one("#filterbar").display)
            await pilot.press(*"session 1")
            await pilot.pause()
            self.assertEqual(
                self.titles(), ["claude session 1"] + [f"claude session {n}" for n in range(10, 18)]
            )
            # Letters that are commands elsewhere are just text here.
            await pilot.press("backspace", "backspace", "q", "o", "i", "r")
            await pilot.pause()
            self.assertEqual(self.main.needle, "sessionqoir")
            self.assertEqual(self.titles(), [])
            self.assertIn('No sessions match "sessionqoir"', self.text())
            self.assertEqual(self.opened() + self.imported(), [])
            self.assertFalse(self.exited())
            await pilot.press("escape")
            self.assertEqual(self.main.needle, "")
            self.assertEqual(len(self.rows.rows), 20)
            self.assertFalse(self.main.query_one("#filterbar").display)
            self.assertIs(self.app.focused, self.rows)

    async def test_filter_matches_project_and_cwd_and_keeps_selection(self):
        self.engine.sessions.append(
            make_session(
                "claude", 77, title="Hero section", cwd="/srv/Website Works/landing", age=400
            )
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("1", "slash", *"LANDING")
            await pilot.pause()
            self.assertEqual(self.titles(), ["Hero section"])  # project, any case
            await pilot.press("escape", "slash", *"website w")
            await pilot.pause()
            self.assertEqual(self.titles(), ["Hero section"])  # cwd
            await pilot.press("escape")
            self.assertEqual(len(self.rows.rows), 21)
            await pilot.press("slash", *"session")
            await pilot.pause()
            await pilot.press("down", "down")  # arrows still move
            key = self.selected()
            await pilot.press(*" 1")
            await pilot.pause()
            self.assertIn(key, [row.key for row in self.rows.rows])
            self.assertEqual(self.selected(), key)  # same session, new place

    async def test_enter_keeps_the_filter_and_returns_to_the_list(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("slash", *"session 3", "enter")
            await pilot.pause()
            self.assertIs(self.app.focused, self.rows)
            self.assertEqual(self.titles(), ["claude session 3"])
            self.assertTrue(self.main.query_one("#filterbar").display)
            self.assertEqual(self.opened(), [])  # that Enter was not "resume"
            await pilot.press("0")  # the filter applies to the new tab too
            self.assertEqual(
                sorted(self.titles()), ["claude session 3", "codex session 3", "grok session 3"]
            )
            await pilot.press("j", "k", "escape")
            self.assertEqual(self.main.needle, "")
            self.assertEqual(len(self.rows.rows), 34)
            self.assertFalse(self.exited())

    async def test_empty_filter_closes_on_enter(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("slash", "enter")
            self.assertFalse(self.main.query_one("#filterbar").display)
            self.assertIs(self.app.focused, self.rows)


class ResumeTest(TuiCase):
    async def test_enter_on_native_opens_once_and_exits_zero(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            self.assertEqual(self.opened(), [])
            await pilot.press("enter")
            await until(self.exited, what="exit")
        self.assertEqual(self.opened(), [NATIVE])
        self.assertEqual(self.app.return_code, 0)
        self.assertEqual(self.imported(), [])

    async def test_enter_twice_still_opens_once(self):
        gate = threading.Event()
        real_open = self.engine.open_session

        def slow_open(ref, **kwargs):
            gate.wait(5)
            return real_open(ref, **kwargs)

        self.engine.open_session = slow_open
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("enter", "enter", "o", "enter")
            gate.set()
            await until(self.exited, what="exit")
        self.assertEqual(self.opened(), [NATIVE])

    async def test_o_opens_and_stays(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("o")
            await self.message("opened claude session 0")
            self.assertEqual(self.opened(), [NATIVE])
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), NATIVE)
            await pilot.press("down")  # a key clears the message
            self.assertNotIn("opened", self.status())
            self.assertIn("agent-sessions", self.status())
            await pilot.press("o")
            await until(lambda: len(self.opened()) == 2, what="second open")
            self.assertFalse(self.exited())

    async def test_launch_note_is_shown(self):
        self.engine.plan_note = "the recorded folder is gone; opened in your home folder"
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("o")
            await self.message("opened claude session 0 · the recorded folder is gone")
        # With Enter the app quits; the note is handed to the caller to print.
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("enter")
            await until(self.exited, what="exit")
        self.assertEqual(self.app.return_value, self.engine.plan_note)

    async def test_double_click_is_enter(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.click("#list", offset=(10, 2))  # single: select only
            await asyncio.sleep(0.7)  # not a double-click pair
            self.assertEqual(self.selected(), NATIVE)
            self.assertEqual(self.opened(), [])
            await pilot.double_click("#list", offset=(10, 2))
            await until(self.exited, what="exit")
        self.assertEqual(self.opened(), [NATIVE])

    async def test_launch_failure_is_shown_and_the_app_lives(self):
        self.engine.fail["open_session"] = FakeError(
            "the folder /home/user/code/project-0 no longer exists"
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("enter")
            await self.message("the folder /home/user/code/project-0 no longer exists")
            self.assertFalse(self.exited())
            self.assertIn("-error", self.main.query_one("#status-message").classes)
            del self.engine.fail["open_session"]
            await pilot.press("o")
            await self.message("opened claude session 0")
            self.assertNotIn("-error", self.main.query_one("#status-message").classes)
            self.assertFalse(self.exited())


class ForeignTest(TuiCase):
    async def prompt_on(self, pilot, key="enter"):
        await settle(pilot)
        await self.select(pilot, FOREIGN)
        await pilot.press(key)
        await self.mode_is(pilot, "foreign")
        self.assertEqual(self.prompt(), POSSIBLE_PROMPT)
        self.assertIn(POSSIBLE_PROMPT, self.text())
        self.assertEqual(self.opened() + self.imported(), [])

    async def test_enter_shows_the_prompt_over_the_hints(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            lines = self.text().splitlines()
            at = next(n for n, line in enumerate(lines) if "[I] import only" in line)
            hints = next(n for n, line in enumerate(lines) if "↑↓ move" in line)
            self.assertLess(at, hints)
            self.assertLessEqual(hints - at, 2)
            # Other keys mean nothing while it is up.
            await pilot.press("j", "q", "enter", "o", "r", "2", "y", "m", "u", "slash")
            self.assertEqual(self.main.mode, "foreign")
            self.assertEqual(self.selected(), FOREIGN)
            self.assertEqual(self.engine.called("scan"), [None])
            self.assertFalse(self.exited())

    async def test_escape_cancels(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertNotIn("[I] import only", self.text())
            self.assertFalse(self.main.query_one("#prompt").display)
            self.assertEqual(self.opened() + self.imported(), [])
            await pilot.press("j")  # the list has its keys back
            self.assertNotEqual(self.selected(), FOREIGN)

    async def test_i_imports_then_resumes_and_quits(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.press("i")
            await until(self.exited, what="exit")
        self.assertEqual(self.imported(), [FOREIGN])
        self.assertEqual(self.opened(), [FOREIGN_COPY])
        self.assertEqual(self.app.return_code, 0)
        calls = [
            name for name, _ in self.engine.calls if name in ("import_session", "open_session")
        ]
        self.assertEqual(calls, ["import_session", "open_session"])

    async def test_capital_i_imports_only_and_selects_the_copy(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.press("I")
            await self.message("imported claude session 12")
            self.assertEqual(self.imported(), [FOREIGN])
            self.assertEqual(self.opened(), [])
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), FOREIGN_COPY)
            self.assertFalse(self.rows.current.foreign)
            row = next(row for row in self.rows.rows if row.key == FOREIGN)
            self.assertEqual(row.where, "C: · imported")
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.engine.called("scan"), [None])  # the engine indexes the copy
            self.assertGreaterEqual(len(self.engine.called("load_index")), 3)  # and is re-read

    async def test_v_views(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.press("v")
            await settle(pilot)
            self.assertIsInstance(self.app.screen, TranscriptScreen)
            self.assertEqual(self.engine.called("transcript"), [FOREIGN])
            self.assertEqual(self.opened() + self.imported(), [])
            await pilot.press("q")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)
            self.assertEqual(self.main.mode, "list")

    async def test_o_asks_too_and_then_stays(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot, key="o")
            await pilot.press("i")
            await self.message("imported and opened claude session 12")
            self.assertEqual(self.imported(), [FOREIGN])
            self.assertEqual(self.opened(), [FOREIGN_COPY])
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), FOREIGN_COPY)

    async def test_already_imported_resumes_the_native_copy(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, IMPORTED)
            await pilot.press("o")
            await self.message("opened Already imported")
            self.assertEqual(self.opened(), [IMPORTED_COPY])
            self.assertEqual(self.main.mode, "list")  # no prompt
            await pilot.press("enter")
            await until(self.exited, what="exit")
        self.assertEqual(self.opened(), [IMPORTED_COPY, IMPORTED_COPY])
        self.assertEqual(self.imported(), [])

    async def test_import_refused_shows_the_reason(self):
        self.engine.fail["import_session"] = FakeError(
            "Antigravity conversations cannot be imported"
        )
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.press("i")
            await self.message("Antigravity conversations cannot be imported")
            self.assertEqual(self.opened(), [])
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), FOREIGN)
            self.assertFalse(self.main._importing)
            self.assertIn("-error", self.main.query_one("#status-message").classes)

    async def test_import_works_but_launch_fails(self):
        self.engine.fail["open_session"] = FakeError("claude is not installed")
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.press("i")
            await self.message("imported claude session 12, but: claude is not installed")
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), FOREIGN_COPY)

    async def test_click_closes_the_prompt(self):
        async with self.started() as pilot:
            await self.prompt_on(pilot)
            await pilot.click("#tab-codex")
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.main.tab_name, "codex")
            self.assertEqual(self.opened() + self.imported(), [])


class ImportKeyTest(TuiCase):
    async def test_i_asks_and_yes_imports(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("i")
            await self.mode_is(pilot, "confirm")
            self.assertIn(
                'Import "claude session 12" from C: to Linux?  [y] yes  [n] no', self.text()
            )
            self.assertEqual(self.imported(), [])
            await pilot.press("enter", "i", "j")  # not answers
            self.assertEqual(self.main.mode, "confirm")
            self.assertEqual(self.imported(), [])
            await pilot.press("y")
            await self.message("imported claude session 12")
            self.assertEqual(self.imported(), [FOREIGN])
            self.assertEqual(self.opened(), [])  # `i` never resumes
            self.assertEqual(self.selected(), FOREIGN_COPY)
            self.assertFalse(self.exited())
            self.assertNotIn("to Linux?", self.text())

    async def test_no_and_escape_do_nothing(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            for answer in ("n", "escape", "N"):
                await pilot.press("i")
                self.assertEqual(self.main.mode, "confirm")
                await pilot.press(answer)
                self.assertEqual(self.main.mode, "list")
                self.assertNotIn("to Linux?", self.text())
            await settle(pilot)
            self.assertEqual(self.imported(), [])
            self.assertEqual(self.selected(), FOREIGN)

    async def test_progress_in_bytes(self):
        gate = threading.Event()
        real_import = self.engine.import_session

        def slow_import(ref, progress=None, index=None, **how):
            progress(3_100_000, 12_000_000)
            gate.wait(5)
            return real_import(ref, progress=None, index=index, **how)

        self.engine.import_session = slow_import
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("i", "y")
            await self.message("importing… 3.1 MB of 12.0 MB")
            # Meanwhile the app answers keys, and refuses a second job.
            await pilot.press("k")
            self.assertNotEqual(self.selected(), FOREIGN)
            await pilot.press("r")
            await self.message("an import is running")
            self.assertEqual(self.engine.called("scan"), [None])
            gate.set()
            await self.message("imported claude session 12")
            self.assertNotIn("importing", self.status())
            self.assertEqual(self.selected(), FOREIGN_COPY)

    async def test_failure_reason_is_shown_as_is(self):
        self.engine.fail["import_session"] = FakeError(
            "a session with this id already exists on Linux"
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("i", "y")
            await self.message("a session with this id already exists on Linux")
            self.assertNotIn("importing", self.status())
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), FOREIGN)
            del self.engine.fail["import_session"]
            await pilot.press("i", "y")  # and it can be tried again
            await self.message("imported claude session 12")

    async def test_known_verdict_refuses_without_asking(self):
        self.engine.options[FOREIGN] = ImportOptions(
            False, "the mapped folder has characters Grok cannot use"
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await until(lambda: FOREIGN in self.main._verdicts, what="verdict")
            await pilot.press("i")
            await self.message("the mapped folder has characters Grok cannot use")
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.imported(), [])

    async def test_native_and_imported_rows_have_nothing_to_import(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("i")
            await self.message("already on Linux")
            self.assertEqual(self.main.mode, "list")
            await self.select(pilot, IMPORTED)
            await pilot.press("i")
            await self.message("already imported")
            self.assertEqual(self.main.mode, "list")
            await pilot.press("y")
            await settle(pilot)
            self.assertEqual(self.imported(), [])


class PagerTest(TuiCase):
    size = (80, 24)

    async def test_v_opens_and_q_returns(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            await settle(pilot)
            self.assertIsInstance(self.app.screen, TranscriptScreen)
            text = self.text()
            lines = text.splitlines()
            self.assertIn("claude session 0", lines[0])
            self.assertIn("User", text)
            self.assertIn("hello there", text)
            self.assertIn("Tool  Read", text)
            self.assertIn("Assistant", text)
            self.assertIn("general reply", text)
            self.assertIn("3 turns", lines[-1])
            self.assertIn("q back", lines[-1])
            self.assertEqual(self.engine.called("transcript"), [NATIVE])
            await pilot.press("q")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)
            self.assertEqual(self.selected(), NATIVE)
            self.assertFalse(self.exited())  # q closed the pager, not the app
            await pilot.press("v")
            await settle(pilot)
            await pilot.press("escape")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)
            self.assertEqual(self.opened() + self.imported(), [])

    async def test_scrolling_and_wrapping(self):
        long = "wrap me " * 40  # 320 characters: several lines at 80 columns
        self.engine.turns[NATIVE] = [
            Turn("user" if n % 2 == 0 else "assistant", f"turn {n} {long}", None)
            for n in range(200)
        ]
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            body = None
            await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="pager")
            body = self.app.screen.body
            await until(lambda: len(body.turns) == 200 and body.settled, what="all turns wrapped")
            await pilot.pause()
            self.assertIn("200 turns", self.text().splitlines()[-1])
            width = body.size.width
            self.assertTrue(all(cell_len(text) <= width for _kind, text in body.lines))
            self.assertGreater(len(body.lines), 200 * 5)
            for line in self.text().splitlines():
                self.assertLessEqual(cell_len(line.rstrip()), 80)

            def top():
                return int(body.scroll_offset.y)

            page = body.size.height
            for key, expected in (
                ("j", 1),
                ("down", 2),
                ("k", 1),
                ("up", 0),
                ("up", 0),
                ("pagedown", page),
                ("space", 2 * page),
                ("pageup", page),
                ("end", len(body.lines) - page),
                ("G", len(body.lines) - page),
                ("home", 0),
                ("G", len(body.lines) - page),
                ("g", 0),
            ):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(top(), expected, key)
            await pilot.press("end")
            await pilot.pause()
            self.assertIn("turn 199", self.text())

    async def test_resize_rewraps_and_keeps_the_place(self):
        self.engine.turns[NATIVE] = [
            Turn("assistant", f"turn {n} " + "word " * 60, None) for n in range(120)
        ]
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="pager")
            body = self.app.screen.body
            await until(lambda: len(body.turns) == 120 and body.settled, what="wrapped")
            before = len(body.lines)
            body.scroll_to(y=body._starts[60], animate=False, force=True, immediate=True)
            await pilot.pause()
            await pilot.resize_terminal(40, 12)
            await until(lambda: body.settled, what="re-wrapped")
            await pilot.pause()
            self.assertGreater(len(body.lines), before)
            self.assertTrue(all(cell_len(text) <= body.size.width for _kind, text in body.lines))
            self.assertEqual(int(body.scroll_offset.y), body._starts[60])
            self.assertIn("turn 60", self.text())
            await pilot.resize_terminal(120, 40)
            await until(lambda: body.settled, what="re-wrapped")
            await pilot.press("q")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)

    async def test_long_transcript_is_capped_and_says_so(self):
        produced = []

        def endless(ref, index=None):
            n = 0
            while True:  # a 100 MB session, as far as the pager knows
                produced.append(n)
                yield Turn("assistant", f"turn {n}", None)
                n += 1

        self.engine.transcript = endless
        with mock.patch.object(view, "PAGER_MAX_TURNS", 120):
            async with self.started() as pilot:
                await settle(pilot)
                await self.select(pilot, NATIVE)
                await pilot.press("v")
                await settle(pilot)
                await until(lambda: "showing first 120 turns" in self.text(), what="cap notice")
                self.assertEqual(self.app.screen.turn_count, 120)
                self.assertLessEqual(len(produced), 121)  # read lazily, then stopped
                self.assertGreater(app_mod.PAGER_BATCH, 0)
                self.assertLess(app_mod.PAGER_BATCH, 120)  # so it arrived in batches
                await pilot.press("q")

    async def test_closing_stops_the_reader(self):
        release = threading.Event()
        produced = []

        def slow(ref, index=None):
            for n in range(1000):
                produced.append(n)
                yield Turn("assistant", f"turn {n}", None)
                if n == 5:
                    release.wait(5)

        self.engine.transcript = slow
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            await until(lambda: len(produced) >= 6, what="reader started")
            await pilot.press("j", "k")  # the pager answers while it reads
            await pilot.press("q")
            await pilot.pause()
            self.assertIs(self.app.screen, self.main)
            release.set()
            await settle(pilot)
            self.assertLess(len(produced), 20)

    async def test_transcript_error_is_shown_in_the_pager(self):
        self.engine.fail["transcript"] = FakeError("the session file is gone")
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            await settle(pilot)
            await until(
                lambda: "stopped: the session file is gone" in self.text(), what="pager error"
            )
            self.assertFalse(self.exited())
            await pilot.press("q")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)

    async def test_markup_in_text_is_shown_literally(self):
        self.engine.turns[NATIVE] = [Turn("user", "[bold]not markup[/bold] [/oops] 日本語", None)]
        self.engine.by_key(NATIVE).title = "[red]title[/red] [/]"
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            self.assertIn("[red]title[/red] [/]", self.text())
            await pilot.press("o")
            await self.message("opened [red]title[/red] [/]")
            await pilot.press("v")
            await settle(pilot)
            text = self.text()
            self.assertIn("[bold]not markup[/bold] [/oops] 日本語", text)
            self.assertIn("[red]title[/red] [/]", text.splitlines()[0])


class ScanTest(TuiCase):
    async def test_r_is_quick_and_capital_r_is_deep(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.assertEqual(self.engine.called("scan"), [None])
            await pilot.press("r")
            await self.message("rescan: 34 sessions, 0.2 s")
            self.assertEqual(self.engine.called("scan"), [None, False])
            await pilot.press("R")
            await self.message("full rescan: 34 sessions")
            self.assertEqual(self.engine.called("scan"), [None, False, True])
            self.assertFalse(self.exited())

    async def test_progress_then_escape_cancels(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.engine.scan_gate = threading.Event()
            self.engine.scan_progress = [("WINVOL", 41234)]
            await pilot.press("slash", *"session", "enter")  # a filter is active too
            await pilot.press("R")
            await self.message("scanning WINVOL… 41k dirs")
            self.assertTrue(self.main._scan["deep"])
            cancel = self.main._scan["cancel"]
            self.assertFalse(cancel.is_set())
            await pilot.press("j")  # still responsive
            self.assertEqual(self.rows.cursor, 1)
            await pilot.press("escape")
            self.assertTrue(cancel.is_set())  # the scan, not the filter
            self.assertEqual(self.main.needle, "session")
            await self.message("full rescan cancelled")
            self.assertIsNone(self.main._scan)
            self.assertNotIn("scanning", self.status())
            await pilot.press("escape")  # now the filter goes
            self.assertEqual(self.main.needle, "")
            self.assertFalse(self.exited())

    async def test_second_scan_request_is_ignored(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            self.engine.scan_gate = threading.Event()
            await pilot.press("r")
            await until(lambda: len(self.engine.called("scan")) == 2, what="scan start")
            await pilot.press("R")
            await self.message("a scan is already running")
            await pilot.press("r")
            await self.message("a scan is already running")
            self.assertEqual(self.engine.called("scan"), [None, False])
            # An import waits for the scan as well.
            await pilot.press("i", "y")
            await self.message("a scan is running")
            self.assertEqual(self.imported(), [])
            self.engine.scan_gate.set()
            await until(lambda: self.main._scan is None, what="scan end")
            await pilot.press("r")
            await until(lambda: len(self.engine.called("scan")) == 3, what="third scan")

    async def test_selection_survives_a_refresh_that_reorders(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("down", "down", "down", "down")
            key = self.selected()
            self.assertEqual(self.rows.cursor, 4)

            def reorder(engine):
                engine.by_key(key).updated = support.stamp(days=400)  # now the oldest
                engine.sessions.append(make_session("claude", 300, title="Newer", age=0))
                engine.sessions.append(make_session("claude", 301, title="Newest", age=0))

            self.engine.on_scan = reorder
            await pilot.press("r")
            await self.message("rescan: 36 sessions")
            self.assertEqual(len(self.rows.rows), 22)
            self.assertEqual(self.selected(), key)
            self.assertEqual(self.rows.cursor, 21)
            top = self.rows.top
            self.assertTrue(top <= self.rows.cursor < top + self.rows.size.height)
            self.assertIn(self.engine.by_key(key).id, self.text())  # detail pane too

    async def test_selection_stays_in_place_when_its_session_goes(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("down", "down", "down")
            key = self.selected()
            below = self.rows.rows[4].key
            self.engine.on_scan = lambda engine: engine.sessions.remove(engine.by_key(key))
            await pilot.press("r")
            await self.message("rescan: 33 sessions")
            self.assertEqual(self.rows.cursor, 3)
            self.assertEqual(self.selected(), below)
            # And when everything goes (a drive unmounted, say):
            self.engine.on_scan = lambda engine: engine.sessions.clear()
            await pilot.press("r")
            await self.message("rescan: 0 sessions")
            self.assertIsNone(self.selected())
            self.assertIn("No sessions found", self.text())
            await pilot.press("down", "enter", "v", "i")
            self.assertFalse(self.exited())

    async def test_quitting_stops_a_running_scan(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.engine.scan_gate = threading.Event()
            await pilot.press("R")
            await until(lambda: self.main._scan is not None, what="scan start")
            cancel = self.main._scan["cancel"]
            await pilot.press("q")
            await until(self.exited, what="exit")
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.app.return_code, 0)


class ErrorTest(TuiCase):
    async def test_index_cannot_be_read_at_start(self):
        self.engine.fail["load_index"] = FakeError("the index file is damaged")
        async with self.started() as pilot:
            await settle(pilot)
            await self.message("the index file is damaged")
            self.assertFalse(self.exited())
            self.assertIn("No sessions found", self.text())
            await pilot.press("down", "enter", "2", "v")
            self.assertFalse(self.exited())
            del self.engine.fail["load_index"]
            await pilot.press("r")
            await self.message("rescan: 34 sessions")
            self.assertEqual(self.main.tab_name, "claude")
            self.assertEqual(len(self.rows.rows), 20)
            await pilot.press("2")
            self.assertEqual(len(self.rows.rows), 7)

    async def test_scan_failure_keeps_the_list(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.engine.fail["scan"] = OSError("mount table could not be read")
            await pilot.press("R")
            await self.message("mount table could not be read")
            self.assertIsNone(self.main._scan)
            self.assertEqual(len(self.rows.rows), 20)
            self.assertNotIn("scanning", self.status())
            self.assertFalse(self.exited())

    async def test_every_engine_call_can_fail_without_killing_the_app(self):
        for name in (
            "status",
            "list_sessions",
            "scan",
            "import_options",
            "open_session",
            "import_session",
            "transcript",
        ):
            engine = FakeEngine()
            async with self.started(engine=engine) as pilot:
                await settle(pilot)
                engine.fail[name] = RuntimeError(f"{name} blew up")
                await self.select(pilot, FOREIGN)
                # (No Enter here: where the launch works, Enter rightly quits.)
                for key in ("r", "o", "escape", "i", "y", "v", "q", "R", "o", "I"):
                    await pilot.press(key)
                    await settle(pilot)
                    self.assertFalse(self.exited(), (name, key))
                await self.select(pilot, NATIVE)
                await pilot.press("o")
                await settle(pilot)
                await pilot.press("v")
                await settle(pilot)
                await pilot.press("q")
                await settle(pilot)
                self.assertFalse(self.exited(), name)
                self.assertTrue(self.app.is_running, name)
                self.assertIs(self.app.screen, self.main, name)

    async def test_the_calls_of_the_new_keys_can_fail_too_and_say_so(self):
        """An exception nobody planned for, in each call the mount, folder,
        continue and undo keys make: its words in the status line, the app
        alive, the list back."""
        with tempfile.TemporaryDirectory() as tmp:
            flows = (  # (the call that fails, the row, the keys that lead to it)
                ("import_options", FOREIGN, ("enter",)),
                ("import_options", FOREIGN, ("t",)),
                ("import_session", FOREIGN, ("t", "enter")),
                ("list_folder", FOREIGN, ("t", "tab")),
                ("continue_options", NATIVE, ("c",)),
                ("continue_session", NATIVE, ("c", "enter", "enter", "y")),
                ("unimport", IMPORTED_COPY, ("u",)),
                ("volumes", OFFLINE, ("enter",)),
                ("mount_drive", OFFLINE, ("m",)),
                ("mount_drive", OFFLINE, ("enter", "m")),
            )
            for name, key, keys in flows:
                engine = FakeEngine()
                engine.continue_folder = tmp
                engine.sessions.append(make_session("claude", 70, foreign=True, offline=True))
                with mock.patch.dict(os.environ, {"HOME": tmp}):
                    async with self.started(engine=engine) as pilot:
                        await settle(pilot)
                        await self.select(pilot, key)
                        engine.fail[name] = RuntimeError(f"{name} blew up")
                        self.main._verdicts.clear()  # so that the engine is asked again
                        for press in keys:
                            await pilot.press(press)
                            await settle(pilot)
                        await self.message(f"{name} blew up")
                        error = self.main.query_one("#status-message").has_class("-error")
                        self.assertTrue(error, (name, keys))
                        await pilot.press("escape", "escape")
                        await settle(pilot)
                        self.assertEqual(self.main.mode, "list", (name, keys))
                        self.assertFalse(self.exited(), (name, keys))
                        self.assertTrue(self.app.is_running, (name, keys))
                        self.assertIs(self.app.screen, self.main, (name, keys))
                        self.assertEqual(self.selected(), key)
                        self.assertIs(self.app.focused, self.rows)

    async def test_engine_without_the_new_functions(self):
        """The default engine against a module that lacks open/import/…:
        each key gives a message, none a crash."""
        from agent_sessions.tui.engine import Engine

        fake = self.engine

        class Partial(Engine):
            module = "agent_sessions.model"  # has none of the api functions

            def providers(self):
                return fake.providers()

            def load_index(self):
                return fake.load_index()

            def list_sessions(self, provider=None, index=None):
                return fake.list_sessions(provider, index)

            def status(self, index=None):
                return fake.status(index)

            def scan(self, deep=None, progress=None, cancel=None):
                return fake.scan(deep, progress, cancel)

        async with self.started(engine=Partial()) as pilot:
            self.engine = fake
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("enter")
            await self.message("this build of the engine has no `open_session` yet")
            await self.select(pilot, FOREIGN)
            await until(lambda: FOREIGN in self.main._verdicts, what="verdict")
            await pilot.pause()
            self.assertIn(
                "unknown: this build of the engine has no `import_options` yet", self.text()
            )
            for key, function in (
                ("i", "import_options"),
                ("enter", "import_options"),
                ("t", "import_options"),
                ("c", "continue_options"),
                ("u", "unimport"),
            ):
                await pilot.press(key)
                await self.message(f"this build of the engine has no `{function}` yet")
                self.assertEqual(self.main.mode, "list")
                await pilot.press("escape")
            await pilot.press("v")
            await settle(pilot)
            await until(lambda: "has no `transcript` yet" in self.text(), what="pager error")
            await pilot.press("q")
            await settle(pilot)
            self.assertFalse(self.exited())


class SizeTest(TuiCase):
    async def test_120x40(self):
        async with self.started(size=(120, 40)) as pilot:
            await settle(pilot)
            lines = self.check_frame(120, 40)
            self.assertIn("PROJECT", lines[3])
            self.assertTrue(self.main.query_one("#detail").display)
            self.assertIn("store", "\n".join(lines))
            self.assertEqual(len([line for line in lines if "claude session" in line]), 18)
            self.assertFalse(self.main.has_class("-narrow"))
            self.assertFalse(self.main.has_class("-compact"))

    async def test_80x24(self):
        async with self.started(size=(80, 24)) as pilot:
            await settle(pilot)
            lines = self.check_frame(80, 24)
            self.assertIn("PROJECT", lines[3])
            self.assertTrue(self.main.query_one("#detail").display)
            self.assertIn("1 Claude 20", lines[1])
            self.assertIn("0 All 34", lines[1])
            self.assertEqual(lines[-2].strip(), view.HINTS_MEDIUM[0])  # every key still
            self.assertEqual(lines[-1].strip(), view.HINTS_MEDIUM[1])
            self.assertIn("c continue  u undo  m mount", lines[-1])
            self.assertGreaterEqual(self.rows.size.height, 8)
            await pilot.press("0")
            self.assertEqual(
                self.check_frame(80, 24)[3].split(),
                ["TITLE", "PROVIDER", "PROJECT", "AGE", "WHERE"],
            )

    async def test_79_columns_drops_project_only(self):
        async with self.started(size=(79, 24)) as pilot:
            await settle(pilot)
            lines = self.check_frame(79, 24)
            self.assertEqual(lines[3].split(), ["TITLE", "AGE", "WHERE"])
            self.assertTrue(self.main.query_one("#detail").display)
            self.assertTrue(self.main.has_class("-narrow"))

    async def test_40x10(self):
        async with self.started(size=(40, 10)) as pilot:
            await settle(pilot)
            lines = self.check_frame(40, 10)
            self.assertNotIn("PROJECT", "\n".join(lines))
            self.assertFalse(self.main.query_one("#detail").display)
            self.assertTrue(self.main.has_class("-compact"))
            self.assertNotIn("store", "\n".join(lines))
            self.assertEqual(lines[1].split(), ["1", "Claude", "20", "2", "3", "4", "5", "0"])
            self.assertIn("TITLE", lines[2])
            self.assertIn("q quit", lines[-1])
            self.assertGreaterEqual(self.rows.size.height, 5)
            self.assertIn("Already imported", lines[3])
            # Everything still works at this size.
            await pilot.press("j", "j", "0", "G", "slash", "x", "escape", "question_mark")
            await settle(pilot)
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.check_frame(40, 10)
            await pilot.press("escape")
            await self.select(pilot, FOREIGN)
            await pilot.press("enter")
            await self.mode_is(pilot, "foreign")
            self.assertIn("[i] [I] [t] [c] [v] [Esc]", self.text())
            await pilot.resize_terminal(60, 10)
            await pilot.pause()
            self.assertIn("[i] resume [I] import [t] folder [c] new [v] view [Esc]", self.text())
            await pilot.resize_terminal(40, 10)
            await pilot.pause()
            await pilot.press("v")
            await settle(pilot)
            self.check_frame(40, 10)
            self.assertIn("hello there", self.text())
            await pilot.press("q")
            await settle(pilot)
            self.assertEqual(self.opened() + self.imported(), [])
            self.assertFalse(self.exited())

    async def test_resize_between_sizes(self):
        async with self.started(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("0")
            await self.select(pilot, FOREIGN)
            sizes = [
                (80, 24),
                (40, 10),
                (120, 40),
                (20, 5),
                (10, 3),
                (1, 1),
                (200, 60),
                (79, 19),
                (60, 20),
                (59, 20),
                (80, 24),
            ]
            for width, height in sizes:
                await pilot.resize_terminal(width, height)
                await settle(pilot)
                self.check_frame(width, height)
                self.assertEqual(self.selected(), FOREIGN, (width, height))
                if self.rows.size.height > 0:
                    top = self.rows.top
                    self.assertTrue(
                        top <= self.rows.cursor < top + self.rows.size.height, (width, height)
                    )
                layout = view.layout_for(width, height)
                self.assertEqual(self.main.query_one("#detail").display, layout.show_detail)
                names = [column.name for column in self.rows.columns]
                self.assertEqual("project" in names, width >= 80, (width, height))
                await pilot.press("j", "k")
            self.assertEqual(
                self.text().splitlines()[3].split(),
                ["TITLE", "PROVIDER", "PROJECT", "AGE", "WHERE"],
            )
            self.assertFalse(self.exited())
            self.assertEqual(self.opened() + self.imported(), [])

    async def test_resize_with_overlays_open(self):
        async with self.started(size=(100, 30)) as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            for width, height in ((40, 10), (12, 4), (100, 30)):
                await pilot.resize_terminal(width, height)
                await settle(pilot)
                self.check_frame(width, height)
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.assertIn("move the selection", self.text())
            await pilot.press("j", "pagedown", "k", "question_mark")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)
            await self.select(pilot, FOREIGN)
            await pilot.press("i")
            await self.mode_is(pilot, "confirm")
            await pilot.resize_terminal(40, 10)
            await settle(pilot)
            self.assertEqual(self.main.mode, "confirm")
            await pilot.press("n")
            await pilot.press("slash", "a", "b")
            await pilot.resize_terminal(90, 20)
            await settle(pilot)
            self.assertEqual(self.main.needle, "ab")
            self.assertFalse(self.exited())

    async def test_wide_characters_keep_the_columns_aligned(self):
        sessions = [
            make_session("claude", 1, title="日本語のタイトルがとても長い場合のテスト" * 4, age=1),
            make_session("claude", 2, title="🚀 ship it 🚀 emoji title", age=2),
            make_session("claude", 3, title="plain ascii", age=3),
            make_session(
                "claude", 4, title="é combining e\u0301 marks", age=4, cwd="/home/user/プロジェクト"
            ),
        ]
        for size in ((120, 40), (80, 24), (40, 10)):
            async with self.started(size=size, engine=FakeEngine(sessions)) as pilot:
                await settle(pilot)
                lines = self.check_frame(*size)
                rows = [line for line in lines if "Linux" in line]
                self.assertEqual(len(rows), 4, size)
                where = {cell_len(line[: line.rindex("Linux")]) for line in rows}
                self.assertEqual(len(where), 1, (size, where))  # WHERE starts in one column
                self.assertIn("…", rows[0])


class HelpAndQuitTest(TuiCase):
    async def test_help_overlay(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await settle(pilot)
            self.assertIsInstance(self.app.screen, HelpScreen)
            text = self.text()
            for fragment in (
                "move the selection",
                "switch provider",
                "view the transcript",
                "rescan every drive",
                "quit",
                "double-click resumes",
                "import it into a folder you choose",
                "continue as a new session",
                "undo an import",
                "mount the drive of an offline session",
            ):
                self.assertIn(fragment, text)
            for key in ("t", "c", "u", "m"):
                self.assertRegex(text, rf"(?m)^\W*{key}  +\w")
            await pilot.press("o", "i", "r", "1", "t", "c", "u", "m")  # no meaning here
            await settle(pilot)
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.assertEqual(self.engine.called("scan"), [None])
            self.assertEqual(self.acted() + self.undone(), [])
            await pilot.press("escape")
            await settle(pilot)
            self.assertIs(self.app.screen, self.main)
            for closer in ("question_mark", "q", "enter"):
                await pilot.press("question_mark")
                await settle(pilot)
                self.assertIsInstance(self.app.screen, HelpScreen)
                await pilot.press(closer)
                await settle(pilot)
                self.assertIs(self.app.screen, self.main)
            self.assertEqual(self.opened(), [])
            self.assertFalse(self.exited())

    async def test_q_quits(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("q")
            await until(self.exited, what="exit")
        self.assertEqual(self.app.return_code, 0)
        self.assertEqual(self.opened(), [])

    async def test_ctrl_c_quits_from_anywhere(self):
        for keys in (
            (),
            ("slash", "a"),
            ("question_mark",),
            ("v",),
            ("end", "enter"),
            ("end", "t"),  # the folder input
            ("c",),  # the choice of a tool
            ("u",),
        ):
            async with self.started() as pilot:
                await settle(pilot)
                await pilot.press("0", *keys)
                await settle(pilot)
                await pilot.press("ctrl+c")
                await until(self.exited, what=f"exit after {keys}")
            self.assertEqual(self.app.return_code, 0)
            self.assertEqual(self.acted(), [])

    async def test_nothing_launches_without_a_key_press(self):
        async with self.started() as pilot:
            await settle(pilot)
            await asyncio.sleep(0.3)
            for width, height in ((80, 24), (40, 10), (120, 40)):
                await pilot.resize_terminal(width, height)
            for tab in ("#tab-codex", "#tab-all"):
                await pilot.click(tab)
                await pilot.click("#list", offset=(8, 1))
                await asyncio.sleep(0.6)  # so no two clicks pair up
                await pilot.click("#list", offset=(8, 3))
                await asyncio.sleep(0.6)
            await pilot.click("#header")
            await pilot.click("#detail")
            await pilot.click("#hints")
            for key in (
                "down",
                "up",
                "j",
                "k",
                "G",
                "g",
                "pagedown",
                "left",
                "right",
                "tab",
                "1",
                "0",
                "slash",
                "escape",
                "question_mark",
                "escape",
                "r",
            ):
                await pilot.press(key)
            await settle(pilot)
            self.assertEqual(self.opened(), [])
            self.assertEqual(self.imported(), [])
            self.assertFalse(self.exited())

    async def test_ansi_theme_and_no_inline_styles(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.assertTrue(self.app.current_theme.name.startswith("ansi"))
            self.assertTrue(self.app.native_ansi_color)
            background = self.main.styles.background
            self.assertTrue(background.ansi is not None or background.a == 0, background)
            for widget in self.main.walk_children():
                inline = widget.styles.inline
                rules = set(inline.get_rules()) - {"display"}
                self.assertEqual(rules, set(), widget)


class OfflineCase(TuiCase):
    """The usual sessions, and four on a drive that is not mounted."""

    def setUp(self):
        super().setUp()
        sessions = support.default_sessions()
        sessions.append(
            make_session("claude", 70, foreign=True, offline=True, title="Offline one", age=3)
        )
        sessions.append(make_session("codex", 71, foreign=True, offline=True, age=4))
        copy = make_session("claude", 91, title="Offline imported", age=5)
        sessions.append(copy)
        sessions.append(
            make_session(
                "claude",
                91,
                foreign=True,
                offline=True,
                title="Offline imported",
                age=6,
                imported_as=copy.key,
            )
        )
        self.engine = FakeEngine(sessions)
        self.total = len(sessions)


class OfflineTest(OfflineCase):
    """Sessions whose drive is not mounted: listed from the engine's cache.
    The rows are dimmed like foreign rows and say where they are, the status
    line counts them, ``v`` and ``i`` show the engine's refusal, and Enter
    asks whether to mount the drive (``MountTest`` has the mounting)."""

    def row(self, key) -> "view.Row":
        return next(row for row in self.rows.rows if row.key == key)

    def line_style(self, key):
        """The style the list draws a row in."""
        index = [row.key for row in self.rows.rows].index(key)
        strip = self.rows.render_line(index - self.rows.top)
        return next(iter(strip)).style

    def nothing_was_started(self) -> None:
        self.assertEqual(self.acted(), [])
        self.assertEqual(self.main.mode, "list")
        self.assertFalse(self.main.query_one("#prompt").display)
        self.assertIsInstance(self.app.screen, MainScreen)
        self.assertFalse(self.exited())

    async def test_rows_say_where_they_are_and_are_dimmed_like_foreign_rows(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)  # the cursor is on none of the rows compared
            offline, foreign = self.row(OFFLINE), self.row(FOREIGN)
            self.assertEqual((offline.where, offline.foreign), ("C: · offline", True))
            self.assertEqual(self.row(OFFLINE_IMPORTED).where, "C: · imported")
            line = next(line for line in self.text().splitlines() if "Offline one" in line)
            self.assertIn("C: · offline", line)
            self.assertFalse(foreign.imported)
            self.assertEqual(self.line_style(OFFLINE), self.line_style(FOREIGN))
            self.assertNotEqual(self.line_style(OFFLINE), self.line_style(IMPORTED_COPY))

    async def test_the_status_line_counts_them(self):
        async with self.started() as pilot:
            await settle(pilot)
            self.assertIn(
                f"2 drives · {self.total} sessions · 3 offline · scanned just now", self.status()
            )
            self.assertEqual(self.main.snapshot.offline, 3)
            await pilot.press("r")  # still there after a rescan
            await settle(pilot)
            await pilot.press("down")
            self.assertIn(f"{self.total} sessions · 3 offline", self.status())

    async def test_no_count_when_none_is_offline(self):
        async with self.started(engine=FakeEngine()) as pilot:
            await settle(pilot)
            self.assertNotIn("offline", self.text())
            self.assertRegex(self.status(), r"2 drives · \d+ sessions · scanned")

    async def test_a_narrow_terminal_still_fits_the_status_line(self):
        async with self.started(size=(50, 24)) as pilot:
            await settle(pilot)
            self.assertIn(f"{self.total} sessions · 3 offline", self.status())
            self.assertNotIn("drives", self.status())
        async with self.started(size=(30, 24)) as pilot:  # no room beside the name: the total
            await settle(pilot)
            self.assertIn(f"{self.total} sessions", self.status())
            self.assertNotIn("offline", self.status())

    async def test_enter_asks_about_the_drive_not_about_an_import(self):
        async with self.started() as pilot:
            await settle(pilot)
            for key in ("enter", "o"):
                await self.select(pilot, OFFLINE)
                await pilot.press(key)
                await self.mode_is(pilot, "mount")
                self.assertEqual(self.prompt(), "C: is not mounted: [m] mount  [Esc] cancel")
                self.assertNotIn("[i] import and resume", self.text())
                self.assertEqual(self.acted(), [])
                await pilot.press("escape")
                self.nothing_was_started()
                self.assertEqual(self.selected(), OFFLINE)
            # No window could have opened: the engine was not asked to open at all.
            self.assertEqual(self.engine.called("open_dry_run"), [])
            self.assertEqual(self.engine.called("volumes"), [None, None])

    async def test_v_shows_the_refusal_and_opens_no_pager(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("v")
            await self.message(support.NOT_MOUNTED)
            self.nothing_was_started()
            self.assertEqual(self.engine.called("transcript"), [OFFLINE])

    async def test_i_shows_the_refusal_and_asks_no_question(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            for key in ("i", "t"):  # into a folder or not: its files are not there
                await pilot.press(key)
                await self.message(support.NOT_MOUNTED)
                self.nothing_was_started()
                self.assertNotIn("Import", self.text())
                await pilot.press("y")  # there is no question to answer
                await settle(pilot)
                await pilot.press("escape")
            self.assertEqual(self.imported(), [])

    async def test_i_before_the_detail_pane_has_its_answer(self):
        """The refusal does not wait for, or depend on, the verdict the
        detail pane asks for."""
        with mock.patch.object(app_mod, "VERDICT_DELAY", 60):
            async with self.started() as pilot:
                await settle(pilot)
                await self.select(pilot, OFFLINE)
                self.assertEqual(self.engine.called("can_import"), [])
                await pilot.press("i")
                await self.message(support.NOT_MOUNTED)
                self.nothing_was_started()

    async def test_a_double_click_is_enter(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            y = [row.key for row in self.rows.rows].index(OFFLINE) - self.rows.top
            await pilot.click("#list", offset=(3, y), times=2)
            await self.mode_is(pilot, "mount")
            self.assertEqual(self.acted(), [])

    async def test_one_that_was_imported_resumes_its_linux_copy(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE_IMPORTED)
            await pilot.press("i")
            await self.message("already imported; Enter resumes the Linux copy")
            await pilot.press("enter")
            await until(self.exited, what="exit")
        self.assertEqual(self.opened(), [OFFLINE_COPY])
        self.assertEqual(self.imported(), [])

    async def test_the_detail_pane_says_why(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)

            def detail(name) -> str:
                return str(self.main.query_one(f"#detail-{name}").render())

            await until(lambda: "not possible" in detail("import"), what="the verdict")
            self.assertEqual(detail("import"), f"not possible: {support.NOT_MOUNTED}")
            self.assertEqual(detail("note"), "drive not mounted")
            self.assertEqual(detail("local"), "(not found)")
            self.assertEqual(detail("cwd"), "C:\\Work\\project-70")

    async def test_an_engine_that_does_not_refuse_still_starts_nothing(self):
        """A listing that is behind the engine, say. The TUI itself never
        opens, views, imports or continues a row it shows as offline: it
        passes on what the engine answers, and what the row says when that
        is nothing."""
        self.engine.by_key(OFFLINE).offline = False  # to the engine it is an ordinary foreign one
        async with self.started() as pilot:
            await settle(pilot)
            self.main.snapshot.sessions[OFFLINE].offline = True  # the list still has it offline
            self.main.snapshot.sessions[OFFLINE].note = "drive not mounted"
            await self.select(pilot, OFFLINE)
            for key in ("v", "c"):  # no refusal at all: the row's own note
                await pilot.press(key)
                await self.message("drive not mounted")
                self.nothing_was_started()
                await pilot.press("escape")
            await pilot.press("enter")  # the drive is asked about, nothing else
            await self.mode_is(pilot, "mount")
            self.assertEqual(self.acted(), [])

    async def test_the_filter_and_the_tabs_treat_them_like_any_row(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("0")
            self.assertIn(OFFLINE, [row.key for row in self.rows.rows])
            await pilot.press("slash", *"offline one", "enter")
            await settle(pilot)
            self.assertEqual([row.key for row in self.rows.rows], [OFFLINE])


class MountTest(OfflineCase):
    """``m``: mount the drive of a session that is offline. Nothing is
    mounted here: the fake engine's ``mount_drive`` only changes what it
    lists, as a scan after a mount would."""

    def gated(self) -> threading.Event:
        self.engine.mount_gate = threading.Event()
        return self.engine.mount_gate

    async def test_m_in_the_prompt_mounts_and_the_list_refreshes(self):
        gate = self.gated()
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("enter")
            await self.mode_is(pilot, "mount")
            await pilot.press("j", "q", "enter", "i", "y", "r", "u", "c")  # not answers
            self.assertEqual(self.main.mode, "mount")
            self.assertEqual(self.selected(), OFFLINE)
            self.assertEqual(self.mounted(), [])
            await pilot.press("m")
            await until(lambda: self.mounted() == [support.WINDOWS], what="the mount")
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.prompt(), "")
            await self.message("mounting C: answer the password prompt…")
            self.assertTrue(self.main.query_one("#status-summary").has_class("-busy"))
            # The rest stays usable while the desktop asks for the password.
            await pilot.press("k")
            self.assertNotEqual(self.selected(), OFFLINE)
            await pilot.press("j", "question_mark")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")  # no scan yet: Esc has nothing to stop
            self.assertFalse(self.main._mount["cancel"].is_set())
            self.assertIn("mounting C:", self.status())
            gate.set()
            await self.message("C: mounted")
            self.assertIsNone(self.main._mount)
            self.assertNotIn("mounting", self.status())
            self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
            # The same session is selected, and it is not offline any more.
            self.assertEqual(self.selected(), OFFLINE)
            self.assertEqual(self.rows.current.where, "C:")
            self.assertEqual(self.main.snapshot.offline, 0)
            await pilot.press("escape")
            self.assertNotIn("offline", self.status())
            self.assertEqual(self.mounted(), [support.WINDOWS])
            self.assertEqual(self.opened() + self.imported(), [])
            self.assertFalse(self.exited())

    async def test_m_on_the_row_mounts_without_asking(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("m")
            await self.message("C: mounted")
            self.assertEqual(self.mounted(), [support.WINDOWS])
            self.assertEqual(self.engine.called("volumes"), [None])  # is the drive there at all
            self.assertEqual(self.prompt(), "")
            self.assertEqual(self.selected(), OFFLINE)
            # Mounted now: `m` has nothing to do, and Enter asks about the import.
            await pilot.press("m")
            await self.message("this session's drive is mounted")
            await pilot.press("enter")
            await self.mode_is(pilot, "foreign")
            self.assertEqual(self.mounted(), [support.WINDOWS])

    async def test_m_on_an_offline_row_that_was_imported(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE_IMPORTED)
            await pilot.press("m")
            await self.message("C: mounted")
            self.assertEqual(self.mounted(), [support.WINDOWS])
            self.assertEqual(self.opened(), [])

    async def test_m_on_a_row_whose_drive_is_mounted(self):
        async with self.started() as pilot:
            await settle(pilot)
            for key in (NATIVE, FOREIGN, IMPORTED):
                await self.select(pilot, key)
                await pilot.press("m")
                await self.message("this session's drive is mounted")
                self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(self.mounted(), [])
            self.assertEqual(self.engine.called("volumes"), [])

    async def test_the_selection_stays_where_the_user_went(self):
        gate = self.gated()
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("m")
            await until(lambda: self.mounted() == [support.WINDOWS], what="the mount")
            keys = [row.key for row in self.rows.rows]
            self.rows.select(keys.index(NATIVE))
            await pilot.pause()
            gate.set()
            await self.message("C: mounted")
            self.assertEqual(self.selected(), NATIVE)

    async def test_a_drive_that_is_not_attached_gets_no_prompt(self):
        self.engine.volume_state = "absent"
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            for key in ("enter", "o"):
                await pilot.press(key)
                await self.message("C: is not attached")
                self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                self.assertEqual(self.prompt(), "")
                await pilot.press("escape")
            self.assertEqual(self.mounted(), [])
            # `m` says the same at once (N-37): nothing is started, and
            # nothing asks for the password of a drive that is not there.
            activity = []
            real = self.main._set_activity
            self.main._set_activity = lambda text: (activity.append(text), real(text))[1]
            await pilot.press("m")
            await self.message("C: is not attached")
            await settle(pilot)
            self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
            self.assertEqual(self.mounted(), [])
            self.assertEqual([text for text in activity if "password" in text], [])
            self.assertEqual(self.main.mode, "list")
            # When the drive is there, the engine's own refusal is shown.
            self.engine.volume_state = "attached"
            self.engine.fail["mount_drive"] = MountFailed(
                "no drive with this volume id is attached"
            )
            await pilot.press("m")
            await self.message("no drive with this volume id is attached")

    async def test_a_drive_the_engine_does_not_list_can_still_be_asked_about(self):
        self.engine.volumes = lambda index=None: []
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("enter")
            await self.mode_is(pilot, "mount")

    async def test_the_drives_cannot_be_asked_about(self):
        self.engine.fail["volumes"] = FakeError("the index file is damaged")
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("enter")
            await self.message("the index file is damaged")
            self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
            self.assertEqual(self.main.mode, "list")
            self.assertFalse(self.exited())
            del self.engine.fail["volumes"]
            await pilot.press("enter")
            await self.mode_is(pilot, "mount")
            self.assertEqual(self.mounted(), [])

    async def test_a_mount_that_fails_says_why(self):
        reason = "no answer after 120 s (was the password prompt answered?)"
        self.engine.fail["mount_drive"] = MountFailed(reason)
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("m")
            await self.message(reason)
            self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
            self.assertIsNone(self.main._mount)
            self.assertNotIn("mounting", self.status())
            self.assertFalse(self.main.query_one("#status-summary").has_class("-busy"))
            self.assertEqual(self.rows.current.where, "C: · offline")
            self.assertEqual(self.selected(), OFFLINE)
            self.assertFalse(self.exited())
            # Through the prompt it is the same, and it can be tried again.
            await pilot.press("enter")
            await self.mode_is(pilot, "mount")
            await pilot.press("m")
            await self.message(reason)
            del self.engine.fail["mount_drive"]
            await pilot.press("m")
            await self.message("C: mounted")
            self.assertEqual(self.mounted(), [support.WINDOWS] * 3)

    async def test_only_one_mount_at_a_time(self):
        gate = self.gated()
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, IMPORTED_COPY)
            await self.select(pilot, FOREIGN)
            await self.select(pilot, OFFLINE)
            await pilot.press("m")
            await until(lambda: self.mounted() == [support.WINDOWS], what="the mount")
            await pilot.press("m")
            await self.message("already mounting C:")
            self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
            await pilot.press("enter")  # and through the prompt
            await self.mode_is(pilot, "mount")
            await pilot.press("m")
            await self.message("already mounting C:")
            self.assertEqual(self.mounted(), [support.WINDOWS])
            # What else ends in a scan waits for the mount.
            for key in ("r", "R"):
                await pilot.press(key)
                await self.message("a mount is running; rescan when it has finished")
            self.assertEqual(self.engine.called("scan"), [None])
            keys = [row.key for row in self.rows.rows]
            self.rows.select(keys.index(FOREIGN))
            await pilot.pause()
            await pilot.press("i")
            await self.mode_is(pilot, "confirm")
            await pilot.press("y")
            await self.message("a mount is running; import when it has finished")
            self.assertEqual(self.imported(), [])
            self.rows.select(keys.index(IMPORTED_COPY))
            await pilot.pause()
            await pilot.press("u")
            await self.mode_is(pilot, "undo")
            await pilot.press("y")
            await self.message("a mount is running; undo when it has finished")
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)])
            self.assertIn("mounting C:", self.status())
            gate.set()
            await self.message("C: mounted")
            await pilot.press("r")
            await until(lambda: len(self.engine.called("scan")) == 2, what="a scan again")

    async def test_a_mount_waits_for_a_scan(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            self.engine.scan_gate = threading.Event()
            await pilot.press("r")
            await until(lambda: len(self.engine.called("scan")) == 2, what="scan start")
            await pilot.press("m")
            await self.message("a scan is running; mount when it has finished")
            self.assertEqual(self.mounted(), [])
            self.assertIsNone(self.main._mount)
            self.engine.scan_gate.set()
            await until(lambda: self.main._scan is None, what="scan end")
            await pilot.press("m")
            await self.message("C: mounted")

    async def test_the_scan_after_the_mount_shows_and_esc_stops_it(self):
        seen = {}

        def slow_mount(ref, progress=None, cancel=None):
            self.engine.calls.append(("mount_drive", ref))
            seen["cancel"] = cancel
            progress("WINVOL", 41234)
            cancel.wait(5)
            return {"mount_point": ref, "scan": {"mode": "deep", "cancelled": cancel.is_set()}}

        self.engine.mount_drive = slow_mount
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("m")
            await self.message("scanning WINVOL… 41k dirs")
            self.assertTrue(self.main._mount["scanning"])
            self.assertFalse(seen["cancel"].is_set())
            await pilot.press("j")  # still responsive
            await pilot.press("escape")
            self.assertTrue(seen["cancel"].is_set())
            await self.message("C: mounted")
            self.assertIsNone(self.main._mount)
            self.assertNotIn("scanning", self.status())

    async def test_quitting_does_not_wait_for_the_scan_after_a_mount(self):
        gate = self.gated()
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("m")
            await until(lambda: self.main._mount is not None, what="the mount")
            cancel = self.main._mount["cancel"]
            await pilot.press("q")
            await until(self.exited, what="exit")
        # The mount was still waiting when the screen went: it was told to stop.
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.app.return_code, 0)
        gate.set()

    async def test_the_prompt_does_not_come_up_on_a_row_that_was_left(self):
        gate = threading.Event()
        real = self.engine.volumes

        def slow(index=None):
            gate.wait(5)
            return real(index)

        self.engine.volumes = slow
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, OFFLINE)
            await pilot.press("enter")
            await pilot.press("k")  # on to another row before the engine has answered
            gate.set()
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.prompt(), "")
            await pilot.press("j", "enter")  # asked again, it is answered
            await self.mode_is(pilot, "mount")
            self.assertEqual(self.selected(), OFFLINE)


OTHER = "claude:bbbbbbbb:clf-0013"  # another foreign session on C:
AGY_COPY = "antigravity:aaaaaaaa:anf-0000"
EXPERIMENTAL_CHOICES = (
    "[i] import and resume (experimental)  [I] import only (experimental)  "
    "[c] continue as new  [v] view  [Esc] cancel"
)


class ImportOptionsTest(TuiCase):
    """Enter, ``i`` and ``t`` on a foreign row, for each answer the engine's
    ``import_options`` can give."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = os.path.join(os.path.realpath(tmp.name), "chosen folder")
        os.mkdir(self.target)

    async def ask(self, pilot, key, mode, session=FOREIGN):
        await self.select(pilot, session)
        await pilot.press(key)
        await self.mode_is(pilot, mode)

    async def give_folder(self, pilot, text=None):
        """The folder input is open: type the folder and press Enter."""
        self.assertEqual(self.main.mode, "folder")
        self.assertIs(self.app.focused, self.folder)
        line = next(line for line in self.text().splitlines() if "Import into:" in line)
        self.assertEqual(line.split(), ["Import", "into:", "~/"])
        await self.type_folder(pilot, self.target if text is None else text)
        await pilot.press("enter")

    async def test_possible_t_asks_for_a_folder_and_imports_into_it(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "enter", "foreign")
            self.assertEqual(self.prompt(), POSSIBLE_PROMPT)  # one line: nothing to explain
            await pilot.press("t")
            self.assertEqual(self.prompt(), "")  # the input is in the prompt's place
            await self.give_folder(pilot, self.target + "/")
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.target, False)])
            self.assertEqual(self.opened(), [])  # into a folder: imported, not resumed
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), FOREIGN_COPY)
            self.assertEqual(self.main.mode, "list")
            self.assertFalse(self.main.query_one("#folderbar").display)
            self.assertIs(self.app.focused, self.rows)

    async def test_possible_t_in_the_list_asks_for_a_folder_too(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "t", "folder")
            await self.give_folder(pilot)
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.target, False)])
            self.assertEqual(self.opened(), [])

    async def test_needs_a_folder(self):
        self.engine.options[FOREIGN] = NEEDS_FOLDER
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "enter", "foreign_folder")
            self.assertEqual(
                self.prompt().splitlines(),
                [
                    NEEDS_FOLDER.reason,
                    "[t] import into another folder  [c] continue as new  [v] view  [Esc] cancel",
                ],
            )
            lines = self.text().splitlines()
            at = next(n for n, line in enumerate(lines) if NEEDS_FOLDER.reason in line and n > 30)
            self.assertIn("[t] import into another folder", lines[at + 1])  # reason, then choices
            self.assertIn("↑↓ move", lines[at + 3])  # over the key hints
            await pilot.press("i", "I", "y", "enter", "j", "m")  # not offered: nothing
            self.assertEqual(self.main.mode, "foreign_folder")
            self.assertEqual(self.imported(), [])
            await pilot.press("t")
            await self.give_folder(pilot)
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.target, False)])
            self.assertEqual(self.selected(), FOREIGN_COPY)

    async def test_needs_a_folder_i_and_t_in_the_list_open_the_input(self):
        self.engine.options[FOREIGN] = NEEDS_FOLDER
        async with self.started() as pilot:
            await settle(pilot)
            for key in ("i", "t"):
                await self.ask(pilot, key, "folder")
                self.assertIn("Import into: ~/", self.text())
                await pilot.press("escape")
                self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.imported(), [])
            await self.ask(pilot, "i", "folder")
            await self.give_folder(pilot)
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.target, False)])

    async def test_experimental_asks_again_with_the_warning(self):
        self.engine.options[AGY] = EXPERIMENTAL
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "enter", "foreign_experimental", AGY)
            self.assertEqual(
                self.prompt().splitlines(), [view.EXPERIMENTAL_LINE, EXPERIMENTAL_CHOICES]
            )
            await pilot.press("t", "y", "enter")  # not offered: nothing
            self.assertEqual(self.main.mode, "foreign_experimental")
            await pilot.press("I")
            self.assertEqual(self.main.mode, "warn")
            lines = self.prompt().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(" ".join(lines[:-1]), WARNING)  # whole at this width
            self.assertEqual(lines[-1], "Import anyway? y/n")
            await pilot.press("enter", "i", "I", "j")  # not answers
            self.assertEqual(self.main.mode, "warn")
            self.assertEqual(self.imported(), [])
            await pilot.press("n")
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.prompt(), "")
            await settle(pilot)
            self.assertEqual(self.imported(), [])
            # Again, and yes this time: import only.
            await pilot.press("enter")
            await self.mode_is(pilot, "foreign_experimental")
            await pilot.press("I", "y")
            await self.message("imported antigravity session 0")
            self.assertEqual(self.engine.import_args, [(AGY, None, True)])
            self.assertEqual(self.opened(), [])
            self.assertFalse(self.exited())
            self.assertEqual(self.selected(), AGY_COPY)

    async def test_experimental_i_resumes_after_the_warning(self):
        self.engine.options[AGY] = EXPERIMENTAL
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "enter", "foreign_experimental", AGY)
            await pilot.press("i")
            self.assertEqual(self.main.mode, "warn")
            self.assertEqual(self.imported(), [])
            await pilot.press("y")
            await until(self.exited, what="exit")
        self.assertEqual(self.engine.import_args, [(AGY, None, True)])
        self.assertEqual(self.opened(), [AGY_COPY])

    async def test_experimental_with_o_resumes_and_stays(self):
        self.engine.options[AGY] = EXPERIMENTAL
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "o", "foreign_experimental", AGY)
            await pilot.press("i", "y")
            await self.message("imported and opened antigravity session 0")
            self.assertEqual(self.opened(), [AGY_COPY])
            self.assertFalse(self.exited())

    async def test_experimental_i_in_the_list_warns_and_never_resumes(self):
        self.engine.options[AGY] = EXPERIMENTAL
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "i", "warn", AGY)
            self.assertIn("Import anyway? y/n", self.text())
            self.assertNotIn("to Linux?", self.text())  # the warning, not the plain y/n
            await pilot.press("escape")
            self.assertEqual(self.main.mode, "list")
            await self.ask(pilot, "i", "warn", AGY)
            await pilot.press("y")
            await self.message("imported antigravity session 0")
            self.assertEqual(self.engine.import_args, [(AGY, None, True)])
            self.assertEqual(self.opened(), [])

    async def test_experimental_t_in_the_list_warns_after_the_folder(self):
        self.engine.options[AGY] = EXPERIMENTAL
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "t", "folder", AGY)
            await self.give_folder(pilot)
            await self.mode_is(pilot, "warn")
            self.assertFalse(self.main.query_one("#folderbar").display)
            self.assertEqual(self.imported(), [])
            await pilot.press("y")
            await self.message("imported antigravity session 0")
            self.assertEqual(self.engine.import_args, [(AGY, self.target, True)])

    async def test_a_folder_and_experimental_both(self):
        self.engine.options[FOREIGN] = ImportOptions(False, "no folder", True, True, WARNING)
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "enter", "foreign_folder")
            self.assertIn("experimental only, into a folder you choose", self.text())
            await pilot.press("t")
            await self.give_folder(pilot)
            await self.mode_is(pilot, "warn")
            await pilot.press("y")
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.target, True)])

    async def test_not_possible(self):
        self.engine.options[FOREIGN] = IMPOSSIBLE
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "enter", "foreign_none")
            self.assertEqual(
                self.prompt().splitlines(),
                [IMPOSSIBLE.reason, "[c] continue as new  [v] view  [Esc] cancel"],
            )
            await pilot.press("i", "I", "t", "y", "enter")  # nothing to import with
            self.assertEqual(self.main.mode, "foreign_none")
            await pilot.press("v")
            await settle(pilot)
            self.assertIsInstance(self.app.screen, TranscriptScreen)
            await pilot.press("q")
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            # In the list, `i` and `t` say why and ask nothing.
            for key in ("i", "t"):
                await pilot.press(key)
                await self.message(IMPOSSIBLE.reason)
                self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                await pilot.press("escape")
            self.assertEqual(self.imported(), [])

    async def test_c_in_any_foreign_prompt_starts_the_continue(self):
        for options, mode in (
            (None, "foreign"),
            (NEEDS_FOLDER, "foreign_folder"),
            (EXPERIMENTAL, "foreign_experimental"),
            (IMPOSSIBLE, "foreign_none"),
        ):
            engine = FakeEngine()
            if options is not None:
                engine.options[FOREIGN] = options
            async with self.started(engine=engine) as pilot:
                await settle(pilot)
                await self.ask(pilot, "enter", mode)
                await pilot.press("c")
                await self.mode_is(pilot, "tool")
                self.assertEqual(engine.called("continue_options"), [FOREIGN])
                self.assertEqual(self.imported(), [])  # a continue needs no import

    async def test_t_has_nothing_to_do_on_a_native_or_an_imported_row(self):
        async with self.started() as pilot:
            await settle(pilot)
            for key, said in ((NATIVE, "already on Linux"), (IMPORTED, "already imported")):
                await self.select(pilot, key)
                await pilot.press("t")
                await self.message(said)
                self.assertEqual(self.main.mode, "list")
                await pilot.press("escape")
            self.assertEqual(self.engine.called("import_options"), [])

    async def test_escape_at_every_step(self):
        self.engine.options[AGY] = EXPERIMENTAL
        self.engine.options[OTHER] = NEEDS_FOLDER
        steps = (
            (FOREIGN, ("enter",), "foreign"),
            (FOREIGN, ("enter", "t"), "folder"),
            (FOREIGN, ("i",), "confirm"),
            (FOREIGN, ("t",), "folder"),
            (OTHER, ("enter",), "foreign_folder"),
            (OTHER, ("enter", "t"), "folder"),
            (OTHER, ("i",), "folder"),
            (AGY, ("enter",), "foreign_experimental"),
            (AGY, ("enter", "i"), "warn"),
            (AGY, ("enter", "I"), "warn"),
            (AGY, ("i",), "warn"),
            (AGY, ("t",), "folder"),
        )
        async with self.started() as pilot:
            await settle(pilot)
            for key, keys, mode in steps:
                await self.select(pilot, key)
                await pilot.press(keys[0])
                await until(lambda: self.main.mode != "list", what=f"a prompt for {keys}")
                await pilot.press(*keys[1:])
                await self.mode_is(pilot, mode)
                await pilot.press("escape")
                await settle(pilot)
                self.assertEqual(self.main.mode, "list", (key, keys))
                self.assertEqual(self.prompt(), "")
                self.assertFalse(self.main.query_one("#folderbar").display)
                self.assertIs(self.app.focused, self.rows)
                await pilot.press("k", "j")  # the list has its keys back
                self.assertEqual(self.selected(), key)
            self.assertEqual(self.acted(), [])
            self.assertFalse(self.exited())

    async def test_asking_the_engine_fails_and_is_tried_again(self):
        self.engine.fail["import_options"] = FakeError("the store cannot be read")
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            self.assertIn("unknown: the store cannot be read", self.text())
            for key in ("enter", "i", "t"):
                await pilot.press(key)
                await self.message("the store cannot be read")
                self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                self.assertEqual(self.prompt(), "")
                await pilot.press("escape")
            self.assertEqual(self.engine.called("import_options").count(FOREIGN), 4)  # asked anew
            del self.engine.fail["import_options"]
            await pilot.press("enter")
            await self.mode_is(pilot, "foreign")
            self.assertEqual(self.imported(), [])
            self.assertFalse(self.exited())

    async def test_enter_before_the_detail_pane_has_its_answer(self):
        with mock.patch.object(app_mod, "VERDICT_DELAY", 60):
            async with self.started() as pilot:
                await settle(pilot)
                await self.select(pilot, FOREIGN)
                self.assertEqual(self.engine.called("import_options"), [])
                await pilot.press("enter")
                await self.mode_is(pilot, "foreign")  # asked at once, not after the delay
                self.assertEqual(self.engine.called("import_options"), [FOREIGN])
                self.assertEqual(self.prompt(), POSSIBLE_PROMPT)

    async def test_the_answer_for_a_row_that_was_left_opens_nothing(self):
        gate = threading.Event()
        real = self.engine.import_options

        def slow(ref, index=None, target=None):
            gate.wait(5)
            return real(ref, index=index, target=target)

        self.engine.import_options = slow
        with mock.patch.object(app_mod, "VERDICT_DELAY", 60):
            async with self.started() as pilot:
                await settle(pilot)
                await self.select(pilot, FOREIGN)
                for key in ("enter", "i", "t"):
                    # On to another row before the engine has answered, or Esc on this one.
                    for then, back in (("j", "k"), ("escape", "escape")):
                        gate.clear()
                        self.main._verdicts.clear()  # not known yet, each time
                        await pilot.press(key)
                        await pilot.press(then)
                        gate.set()
                        await settle(pilot)
                        self.assertEqual(self.main.mode, "list", (key, then))
                        self.assertEqual(self.prompt(), "")
                        await pilot.press(back)
                self.assertEqual(self.selected(), FOREIGN)
                self.assertEqual(self.acted(), [])

    async def test_an_import_that_the_engine_refuses_after_all(self):
        self.engine.options[FOREIGN] = NEEDS_FOLDER
        self.engine.fail["import_session"] = FakeError(
            "the folder to import into is not a directory"
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, "t", "folder")
            await self.give_folder(pilot)
            await self.message("the folder to import into is not a directory")
            self.assertEqual(self.main.mode, "list")
            self.assertFalse(self.main._importing)
            self.assertEqual(self.selected(), FOREIGN)
            self.assertFalse(self.exited())


class FolderInputTest(TuiCase):
    """The one-line folder input: completion in real temp directories, what
    Enter takes, and that no key reaches the list while it is open."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        for name in ("projects-site", "projects-tools", "pictures", ".hidden", ".hoard", "work"):
            os.mkdir(os.path.join(self.tmp, name))
        os.mkdir(os.path.join(self.tmp, "work", "inner"))
        for name in ("plain.txt", "wrong"):
            Path(self.tmp, name).write_text("a file", encoding="utf-8")
        home = mock.patch.dict(os.environ, {"HOME": self.tmp})  # what ``~`` means here
        home.start()
        self.addCleanup(home.stop)

    def below(self, *parts) -> str:
        return os.path.join(self.tmp, *parts)

    async def opened_input(self, pilot):
        await settle(pilot)
        await self.select(pilot, FOREIGN)
        await pilot.press("t")
        await self.mode_is(pilot, "folder")

    async def tab(self, pilot, text) -> str:
        """Type ``text``, press Tab and wait for the answer; what the input
        holds then."""
        await self.type_folder(pilot, text)
        await pilot.press("tab")
        await settle(pilot)
        self.assertEqual(self.main.mode, "folder")
        self.assertIs(self.app.focused, self.folder)  # Tab did not move the focus
        return self.folder.value

    async def test_one_match_is_completed_with_a_slash(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            self.assertEqual(await self.tab(pilot, self.below("pi")), self.below("pictures") + "/")
            self.assertEqual(self.folder.cursor_position, len(self.folder.value))
            self.assertIn("agent-sessions", self.status())  # nothing to say
            self.assertEqual(await self.tab(pilot, self.below("wo")), self.below("work") + "/")
            self.assertEqual(await self.tab(pilot, self.folder.value), self.below("work", "inner/"))
            self.assertEqual(self.imported(), [])

    async def test_several_matches_go_as_far_as_they_agree_and_are_named(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            self.assertEqual(await self.tab(pilot, self.below("pro")), self.below("projects-"))
            self.assertIn("projects-site  projects-tools", self.status())
            self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
            self.assertEqual(self.folder.cursor_position, len(self.folder.value))
            await pilot.press("s")  # typing on takes the names away
            await pilot.pause()
            self.assertEqual(self.folder.value, self.below("projects-s"))
            self.assertNotIn("projects-tools", self.status())
            await pilot.press("tab")
            await settle(pilot)
            self.assertEqual(self.folder.value, self.below("projects-site") + "/")
            # Nothing more to add: the names again, the text as it was.
            self.assertEqual(await self.tab(pilot, self.below("p")), self.below("p"))
            self.assertIn("pictures  projects-site  projects-tools", self.status())

    async def test_hidden_folders_only_after_a_dot_and_files_never(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            self.assertEqual(await self.tab(pilot, self.tmp + "/"), self.tmp + "/")
            self.assertIn("pictures  projects-site  projects-tools  work", self.status())
            self.assertNotIn(".hidden", self.status())
            self.assertNotIn("plain.txt", self.status())
            self.assertEqual(await self.tab(pilot, self.below(".")), self.below(".h"))
            self.assertIn(".hidden  .hoard", self.status())
            self.assertEqual(await self.tab(pilot, self.below(".hi")), self.below(".hidden") + "/")

    async def test_no_match_and_a_file_change_nothing(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            for text in (
                self.below("zzz"),
                self.below("pla"),
                self.below("wrong"),
                "relative/x",
                "",
            ):
                self.assertEqual(await self.tab(pilot, text), text)
                self.assertIn("agent-sessions", self.status())  # no message, no error

    async def test_tilde_is_the_home_folder(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            self.assertEqual(self.folder.value, "~/")
            self.assertEqual(await self.tab(pilot, "~"), "~/")
            self.assertEqual(await self.tab(pilot, "~/"), "~/")
            self.assertIn("pictures  projects-site  projects-tools  work", self.status())
            self.assertEqual(await self.tab(pilot, "~/pi"), "~/pictures/")
            self.assertEqual(self.engine.called("list_folder"), [self.tmp + "/"] * 2)
            await pilot.press("enter")
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.below("pictures"), False)])

    async def test_tilde_alone_is_accepted_as_the_home_folder(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.type_folder(pilot, "~")
            await pilot.press("enter")
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.tmp, False)])

    async def test_a_folder_that_cannot_be_listed_is_said(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            text = self.below("nowhere", "x")
            self.assertEqual(await self.tab(pilot, text), text)
            self.assertIn(
                f"cannot list {self.below('nowhere')}/: No such file or directory", self.status()
            )
            self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
            # The engine's listing failing in any other way is a message too.
            self.engine.fail["list_folder"] = RuntimeError("the listing blew up")
            self.assertEqual(await self.tab(pilot, self.below("pi")), self.below("pi"))
            self.assertIn("the listing blew up", self.status())
            self.assertFalse(self.exited())

    async def test_the_listing_never_runs_on_the_ui_thread(self):
        ui = threading.current_thread()
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.tab(pilot, self.below("pi"))
            await self.tab(pilot, self.tmp + "/")
            await self.tab(pilot, "~/")
        self.assertEqual(len(self.engine.listing_threads), 3)
        for thread in self.engine.listing_threads:
            self.assertIsNot(thread, ui)

    async def test_an_answer_to_a_text_that_was_typed_over_is_dropped(self):
        gate = threading.Event()
        real = self.engine.list_folder

        def slow(directory):
            gate.wait(5)
            return real(directory)

        self.engine.list_folder = slow
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.type_folder(pilot, self.below("pi"))
            await pilot.press("tab")
            await pilot.press("x")  # typed on while the folder is listed
            gate.set()
            await settle(pilot)
            self.assertEqual(self.folder.value, self.below("pix"))
            # Or the input is gone by then.
            gate.clear()
            await self.type_folder(pilot, self.below("pi"))
            await pilot.press("tab", "escape")
            gate.set()
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertFalse(self.main.query_one("#folderbar").display)

    async def test_enter_takes_only_a_folder_that_exists(self):
        refused = (
            (self.below("nope"), f"not an existing folder: {self.below('nope')}"),
            (self.below("plain.txt"), f"not an existing folder: {self.below('plain.txt')}"),
            ("relative/path", "not an existing folder: relative/path"),
            ("~/nope", f"not an existing folder: {self.below('nope')}"),
            ("", "type a folder"),
            ("   ", "type a folder"),
        )
        async with self.started() as pilot:
            await self.opened_input(pilot)
            for text, said in refused:
                await self.type_folder(pilot, text)
                await pilot.press("enter")
                await self.message(said)
                self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "folder")  # and the input stays open
                self.assertTrue(self.main.query_one("#folderbar").display)
                self.assertIs(self.app.focused, self.folder)
                self.assertEqual(self.folder.value, text)
            self.assertEqual(self.imported(), [])
            await self.type_folder(pilot, self.below("work") + "//")
            await pilot.press("enter")
            await self.message("imported claude session 12")
            self.assertEqual(self.engine.import_args, [(FOREIGN, self.below("work"), False)])

    async def test_escape_cancels_and_returns_to_the_list(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.type_folder(pilot, self.below("work"))
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertFalse(self.main.query_one("#folderbar").display)
            self.assertIs(self.app.focused, self.rows)
            self.assertEqual(self.imported(), [])
            await pilot.press("j")
            self.assertNotEqual(self.selected(), FOREIGN)
            # Opened again it starts afresh.
            await pilot.press("k", "t")
            await self.mode_is(pilot, "folder")
            self.assertEqual(self.folder.value, "~/")

    async def test_no_key_reaches_the_list_while_it_is_open(self):
        typed = "jkqoimcuvrRtGgIhl?/0123 y"
        async with self.started() as pilot:
            await self.opened_input(pilot)
            tab = self.main.tab_name
            await pilot.press(*typed)
            await pilot.pause()
            self.assertEqual(self.folder.value, "~/" + typed)  # all of it is text
            await pilot.press("up", "down", "pageup", "pagedown", "shift+tab", "home", "left")
            await pilot.press("right", "end")
            await settle(pilot)
            self.assertEqual(self.folder.value, "~/" + typed)
            self.assertEqual(self.main.mode, "folder")
            self.assertIs(self.app.focused, self.folder)
            self.assertIs(self.app.screen, self.main)  # no help, no pager
            self.assertEqual(self.selected(), FOREIGN)
            self.assertEqual(self.main.tab_name, tab)
            self.assertEqual(self.main.needle, "")
            self.assertFalse(self.main.query_one("#filterbar").display)
            self.assertEqual(self.engine.called("scan"), [None])
            self.assertEqual(self.acted() + self.undone(), [])
            self.assertEqual(self.engine.called("continue_options"), [])
            self.assertFalse(self.exited())
            # The keys that edit still do.
            await pilot.press("backspace", "backspace", "home", "delete")
            await pilot.pause()
            self.assertEqual(self.folder.value, ("~/" + typed)[1:-2])

    async def test_a_click_inside_keeps_it_and_one_elsewhere_cancels(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.type_folder(pilot, self.below("work"))
            await pilot.click("#folder", offset=(3, 0))
            await pilot.pause()
            self.assertEqual(self.main.mode, "folder")
            self.assertIs(self.app.focused, self.folder)
            self.assertEqual(self.folder.value, self.below("work"))
            await pilot.click("#list", offset=(10, 2))
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertFalse(self.main.query_one("#folderbar").display)
            self.assertEqual(self.rows.cursor, 2)
            self.assertEqual(self.imported(), [])
            # A tab clicked does the same.
            await self.select(pilot, FOREIGN)
            await pilot.press("t")
            await self.mode_is(pilot, "folder")
            await pilot.click("#tab-codex")
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.main.tab_name, "codex")

    async def test_the_terminal_losing_the_focus_does_not_cancel(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.type_folder(pilot, self.below("wo"))
            self.app.app_focus = False  # another window has the keyboard
            await pilot.pause()
            self.assertEqual(self.main.mode, "folder")
            self.assertTrue(self.main.query_one("#folderbar").display)
            self.app.app_focus = True
            await pilot.pause()
            self.assertIs(self.app.focused, self.folder)
            self.assertEqual(await self.tab(pilot, self.folder.value), self.below("work") + "/")

    async def test_resize_while_it_is_open(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            await self.type_folder(pilot, self.below("projects-site"))
            for width, height in ((80, 24), (40, 10), (20, 5), (1, 1), (120, 40)):
                await pilot.resize_terminal(width, height)
                await settle(pilot)
                self.check_frame(width, height)
                self.assertEqual(self.main.mode, "folder", (width, height))
                self.assertEqual(self.folder.value, self.below("projects-site"))
                self.assertIs(self.app.focused, self.folder)
            self.assertIn(f"Import into: {self.below('projects-site')}", self.text())
            await pilot.press("enter")
            await self.message("imported claude session 12")

    async def test_the_session_going_away_closes_it(self):
        async with self.started() as pilot:
            await self.opened_input(pilot)
            self.engine.on_scan = lambda engine: engine.sessions.remove(engine.by_key(FOREIGN))
            self.main.start_scan(False)
            await until(lambda: self.main.mode == "list", what="the input to close")
            await settle(pilot)
            self.assertFalse(self.main.query_one("#folderbar").display)
            self.assertIs(self.app.focused, self.rows)
            await pilot.press("j", "k")
            self.assertFalse(self.exited())


class ContinueTest(TuiCase):
    """``c``: continue a session as a new session of a tool, in three steps.
    The fake engine starts nothing; it records what it was asked to start."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.engine.continue_folder = self.tmp
        self.marks = view.TOOL_MARKS if textual else ("", "")

    def chosen(self, label) -> str:
        return f"{self.marks[0]} {label} {self.marks[1]}"

    async def to(self, pilot, step, key=NATIVE):
        """Go to a step of the continue: ``tool``, ``folder`` or ``start``."""
        await self.select(pilot, key)
        await pilot.press("c")
        await self.mode_is(pilot, "tool")
        if step != "tool":
            await pilot.press("enter")
            self.assertEqual(self.main.mode, "folder")
        if step == "start":
            await pilot.press("enter")
            await self.mode_is(pilot, "start")

    async def test_three_steps_then_the_tool_is_started(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "tool")
            self.assertEqual(self.engine.called("continue_options"), [NATIVE])
            self.assertEqual(
                self.prompt(),
                f"Continue with: {self.chosen('Claude')}   ←→ change  Enter next  Esc cancel",
            )
            turns = (("right", "Codex"), ("right", "Grok"), ("right", "Claude"), ("left", "Grok"))
            for key, label in turns + (("left", "Codex"),):
                await pilot.press(key)
                self.assertIn(self.chosen(label), self.prompt())
            await pilot.press("j", "y", "c", "q", "tab", "2")  # nothing here
            self.assertEqual(self.main.mode, "tool")
            self.assertEqual(self.selected(), NATIVE)
            self.assertEqual(self.main.tab_name, "claude")
            await pilot.press("enter")
            self.assertEqual(self.main.mode, "folder")
            self.assertIs(self.app.focused, self.folder)
            self.assertEqual(self.folder.value, self.tmp)  # the session's own folder
            self.assertIn(f"Start in: {self.tmp}", self.text())
            self.assertEqual(self.prompt(), "")
            await pilot.press("enter")
            await self.mode_is(pilot, "start")
            self.assertEqual(
                self.prompt(),
                f"Start Codex in {self.tmp} and send a first message to the model? y/n",
            )
            self.assertFalse(self.main.query_one("#folderbar").display)
            await pilot.press("enter", "j", "c", "right")  # not answers
            self.assertEqual(self.main.mode, "start")
            self.assertEqual(self.continued(), [])  # nothing before the yes
            await pilot.press("y")
            await self.message("started Codex in a new window")
            self.assertEqual(self.continued(), [(NATIVE, "codex", self.tmp)])
            self.assertFalse(self.exited())  # the TUI stays open
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.selected(), NATIVE)
            self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
            self.assertEqual(self.opened() + self.imported(), [])

    async def test_no_default_tool_starts_at_the_first(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "tool", AGY)  # its own tool is not installed
            self.assertIn(self.chosen("Claude"), self.prompt())
            await pilot.press("left")  # round the other way
            self.assertIn(self.chosen("Grok"), self.prompt())

    async def test_a_single_tool_has_nothing_to_change_to(self):
        self.engine.continue_answers[NATIVE] = ContinueOptions(
            True, "", (("grok", "Grok"),), "grok", self.tmp
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "tool")
            await pilot.press("right", "left", "left")
            self.assertIn(self.chosen("Grok"), self.prompt())
            await pilot.press("enter", "enter")
            await self.mode_is(pilot, "start")
            await pilot.press("y")
            await self.message("started Grok in a new window")
            self.assertEqual(self.continued(), [(NATIVE, "grok", self.tmp)])

    async def test_without_a_folder_of_its_own_the_input_starts_at_home(self):
        self.engine.continue_folder = None
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            async with self.started() as pilot:
                await settle(pilot)
                await self.to(pilot, "folder")
                self.assertEqual(self.folder.value, "~/")
                await pilot.press("enter")
                await self.mode_is(pilot, "start")
                self.assertIn(f"Start Claude in {self.tmp} and send", self.prompt())
                await pilot.press("y")
                await self.message("started Claude in a new window")
                self.assertEqual(self.continued(), [(NATIVE, "claude", self.tmp)])

    async def test_another_folder_can_be_typed(self):
        other = os.path.join(self.tmp, "another")
        os.mkdir(other)
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "folder")
            await self.type_folder(pilot, os.path.join(self.tmp, "nope"))
            await pilot.press("enter")
            await self.message("not an existing folder")
            self.assertEqual(self.main.mode, "folder")  # and the input stays open
            self.assertEqual(self.continued(), [])
            await self.type_folder(pilot, os.path.join(self.tmp, "an"))
            await pilot.press("tab")
            await settle(pilot)
            self.assertEqual(self.folder.value, other + "/")
            await pilot.press("enter")
            await self.mode_is(pilot, "start")
            await pilot.press("y")
            await self.message("started Claude in a new window")
            self.assertEqual(self.continued(), [(NATIVE, "claude", other)])

    async def test_a_foreign_session_is_continued_without_an_import(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "start", FOREIGN)
            await pilot.press("y")
            await self.message("started Claude in a new window")
            self.assertEqual(self.continued(), [(FOREIGN, "claude", self.tmp)])
            self.assertEqual(self.imported(), [])
            self.assertEqual(self.selected(), FOREIGN)

    async def test_escape_at_every_step_starts_nothing(self):
        async with self.started() as pilot:
            await settle(pilot)
            for step, answer in (("tool", "escape"), ("folder", "escape"), ("start", "escape")) + (
                ("start", "n"),
                ("start", "N"),
            ):
                await self.to(pilot, step)
                await pilot.press(answer)
                await settle(pilot)
                self.assertEqual(self.main.mode, "list", (step, answer))
                self.assertEqual(self.prompt(), "")
                self.assertFalse(self.main.query_one("#folderbar").display)
                self.assertIs(self.app.focused, self.rows)
                await pilot.press("j", "k")  # the list has its keys back
                self.assertEqual(self.selected(), NATIVE)
            self.assertEqual(self.continued(), [])
            self.assertEqual(self.acted(), [])
            self.assertFalse(self.exited())

    async def test_what_cannot_be_continued_says_why(self):
        no_tool = "no tool that can start a new session is installed"
        self.engine.continue_answers[NATIVE] = ContinueOptions(False, no_tool)
        self.engine.continue_answers[FOREIGN] = ContinueOptions(True, "")  # and no tools at all
        self.engine.sessions.append(make_session("claude", 70, foreign=True, offline=True, age=3))
        async with self.started() as pilot:
            await settle(pilot)
            for key, said in (
                (NATIVE, no_tool),
                (FOREIGN, "cannot be continued"),
                (OFFLINE, support.NOT_MOUNTED),
            ):
                await self.select(pilot, key)
                await pilot.press("c")
                await self.message(said)
                self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                self.assertEqual(self.prompt(), "")
                await pilot.press("escape")
            self.assertEqual(self.continued(), [])

    async def test_an_engine_error_at_every_step(self):
        self.engine.fail["continue_options"] = FakeError("the index file is damaged")
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("c")
            await self.message("the index file is damaged")
            self.assertEqual(self.main.mode, "list")
            del self.engine.fail["continue_options"]
            refusal = "codex is not installed; the installed tools are: claude"
            self.engine.fail["continue_session"] = FakeError(refusal)
            await self.to(pilot, "start")
            await pilot.press("y")
            await self.message(refusal)
            self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
            self.assertFalse(self.main._continuing)
            self.assertFalse(self.exited())
            del self.engine.fail["continue_session"]
            await self.to(pilot, "start")  # and it can be tried again
            await pilot.press("y")
            await self.message("started Claude in a new window")
            self.assertEqual(len(self.continued()), 2)

    async def test_the_status_line_says_starting_until_the_engine_answers(self):
        """The engine exports the transcript before it starts the tool: for
        that time the status line shows that something is under way."""
        gate = threading.Event()
        real = self.engine.continue_session

        def slow(ref, tool=None, folder=None, index=None):
            gate.wait(5)
            return real(ref, tool=tool, folder=folder, index=index)

        self.engine.continue_session = slow
        starting = f"starting Claude{view.ELLIPSIS}"
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "start")
            summary = self.main.query_one("#status-summary")
            self.assertNotIn("starting", self.status())
            self.assertFalse(summary.has_class("-busy"))
            await pilot.press("y")
            await self.message(starting)
            self.assertTrue(summary.has_class("-busy"))
            self.assertEqual(self.continued(), [])  # the engine has not answered
            await pilot.press("j")  # the list stays usable, and the text stays
            await pilot.pause()
            self.assertIn(starting, self.status())
            gate.set()
            await self.message("started Claude in a new window")
            self.assertNotIn("starting", self.status())
            self.assertFalse(summary.has_class("-busy"))
            self.assertEqual(self.continued(), [(NATIVE, "claude", self.tmp)])

    async def test_starting_goes_when_the_engine_refuses(self):
        gate = threading.Event()

        def refuse(ref, tool=None, folder=None, index=None):
            gate.wait(5)
            raise FakeError("codex is not installed")

        self.engine.continue_session = refuse
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "start")
            await pilot.press("y")
            await self.message(f"starting Claude{view.ELLIPSIS}")
            gate.set()
            await self.message("codex is not installed")
            self.assertNotIn("starting", self.status())
            self.assertFalse(self.main.query_one("#status-summary").has_class("-busy"))

    async def test_a_scan_that_runs_meanwhile_keeps_its_own_progress(self):
        """The text of a continue is shown only while no other job has one,
        and ending the continue does not wipe that job's."""
        gate = threading.Event()
        real = self.engine.continue_session

        def slow(ref, tool=None, folder=None, index=None):
            gate.wait(5)
            return real(ref, tool=tool, folder=folder, index=index)

        self.engine.continue_session = slow
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "start")
            await pilot.press("y")
            await self.message(f"starting Claude{view.ELLIPSIS}")
            self.main._set_activity("scanning Zephyro")  # as a scan's progress does
            await pilot.pause()
            self.assertIn("scanning Zephyro", self.status())
            self.assertNotIn("starting", self.status())
            gate.set()
            await self.message("started Claude in a new window")
            self.assertIn("scanning Zephyro", self.status())
            self.main._set_activity("")
            await pilot.pause()
            self.assertNotIn("scanning", self.status())

    async def test_a_second_start_while_the_first_is_under_way_is_ignored(self):
        gate = threading.Event()
        real = self.engine.continue_session

        def slow(ref, tool=None, folder=None, index=None):
            gate.wait(5)
            return real(ref, tool=tool, folder=folder, index=index)

        self.engine.continue_session = slow
        async with self.started() as pilot:
            await settle(pilot)
            await self.to(pilot, "start")
            await pilot.press("y")
            await pilot.press("c")  # once more, while the first is still starting
            await self.mode_is(pilot, "tool")
            await pilot.press("enter", "enter")
            await self.mode_is(pilot, "start")
            await pilot.press("y")
            self.assertEqual(self.main.mode, "list")
            gate.set()
            await self.message("started Claude in a new window")
            await settle(pilot)
            self.assertEqual(self.continued(), [(NATIVE, "claude", self.tmp)])

    async def test_the_answer_for_a_row_that_was_left_opens_nothing(self):
        gate = threading.Event()
        real = self.engine.continue_options

        def slow(ref, index=None):
            gate.wait(5)
            return real(ref, index=index)

        self.engine.continue_options = slow
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("c")
            await pilot.press("j")
            gate.set()
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.prompt(), "")
            # Esc while the engine is asked takes the key back, on the same row too.
            gate.clear()
            await pilot.press("k", "c")
            await pilot.press("escape")
            gate.set()
            await settle(pilot)
            self.assertEqual((self.main.mode, self.selected()), ("list", NATIVE))
            self.assertEqual(self.continued(), [])


class UndoTest(OfflineCase):
    """``u``: take an import back. The fake engine removes nothing: it stops
    listing the copy. Its refusals are the engine's own ``UndoFailed``."""

    def row(self, key) -> "view.Row":
        return next(row for row in self.rows.rows if row.key == key)

    async def ask(self, pilot, key, mode):
        await self.select(pilot, key)
        await pilot.press("u")
        await self.mode_is(pilot, mode)

    async def test_u_on_the_copy_asks_then_removes_it(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, IMPORTED_COPY, "undo")
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)])  # a dry run
            self.assertEqual(self.prompt(), "Remove the imported copy (2 files, 12.0 MB)? y/n")
            await pilot.press("enter", "u", "j", "i")  # not answers
            self.assertEqual(self.main.mode, "undo")
            await pilot.press("n")
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)])
            self.assertTrue(self.row(IMPORTED).imported)
            await self.ask(pilot, IMPORTED_COPY, "undo")
            await pilot.press("y")
            await self.message("removed 2 files")
            self.assertNotIn("left in place", self.status())
            self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
            self.assertEqual(self.undone()[-1], (IMPORTED_COPY, False, False))
            # The list was read again: the copy is gone, its source is plain foreign.
            self.assertNotIn(IMPORTED_COPY, [row.key for row in self.rows.rows])
            self.assertEqual((self.row(IMPORTED).where, self.row(IMPORTED).imported), ("C:", False))
            self.assertFalse(self.main._undoing)
            self.assertFalse(self.exited())
            self.assertEqual(self.opened() + self.imported() + self.mounted(), [])

    async def test_u_on_the_foreign_row_it_was_imported_from(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, IMPORTED, "undo")
            await pilot.press("Y")
            await self.message("removed 2 files")
            self.assertEqual(self.undone(), [(IMPORTED, False, True), (IMPORTED, False, False)])
            self.assertEqual(self.selected(), IMPORTED)  # still listed, and selected
            self.assertFalse(self.rows.current.imported)

    async def test_u_on_a_row_whose_drive_is_away(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, OFFLINE_IMPORTED, "undo")
            await pilot.press("y")
            await self.message("removed 2 files")
            self.assertEqual(self.row(OFFLINE_IMPORTED).where, "C: · offline")
            self.assertNotIn(OFFLINE_COPY, [row.key for row in self.rows.rows])

    async def test_what_is_left_in_place_is_counted(self):
        self.engine.undo_left = [("/home/user/.codex/session_index.jsonl", "a line was added")]
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, IMPORTED_COPY, "undo")
            await pilot.press("y")
            await self.message("removed 2 files, 1 left in place")

    async def test_no_import_recorded_is_an_answer_not_a_question(self):
        async with self.started() as pilot:
            await settle(pilot)
            for key in (NATIVE, FOREIGN, OFFLINE):
                await self.select(pilot, key)
                await pilot.press("u")
                await self.message(support.NO_RECORD)
                self.assertFalse(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                self.assertEqual(self.prompt(), "")
                await pilot.press("y")  # there is nothing to say yes to
                await settle(pilot)
            self.assertEqual(
                self.undone(), [(key, False, True) for key in (NATIVE, FOREIGN, OFFLINE)]
            )

    async def test_a_copy_that_was_used_since_asks_whether_to_lose_it(self):
        self.engine.used_since[IMPORTED_COPY] = ["a.jsonl was modified", "b.json is gone"]
        used = (
            "The copy was used since the import (2 files differ). "
            "Remove it and lose its new turns? y/n"
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, IMPORTED_COPY, "undo_used")
            self.assertEqual(self.prompt(), used)
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)])
            for answer in ("n", "escape"):
                await pilot.press(answer)
                await settle(pilot)
                self.assertEqual(self.main.mode, "list")
                self.assertTrue(self.row(IMPORTED).imported)
                await self.ask(pilot, IMPORTED_COPY, "undo_used")
            self.assertEqual([call for call in self.undone() if not call[2]], [])
            await pilot.press("y")
            await self.message("removed 2 files")
            self.assertEqual(self.undone()[-1], (IMPORTED_COPY, True, False))  # forced
            self.assertNotIn(IMPORTED_COPY, [row.key for row in self.rows.rows])

    async def test_any_other_refusal_is_shown_and_asks_nothing(self):
        refusals = (
            UndoFailed("the import record is damaged (its manifest); nothing was removed"),
            # The words of a copy that was used, without the engine's mark: not a question.
            UndoFailed(support.USED_SINCE),
            FakeError("the import journal cannot be read: Permission denied"),
            RuntimeError("boom"),
        )
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, IMPORTED_COPY)
            for refusal in refusals:
                self.engine.fail["unimport"] = refusal
                await pilot.press("u")
                await self.message(str(refusal)[:60])
                self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
                self.assertEqual(self.main.mode, "list")
                self.assertEqual(self.prompt(), "")
                await pilot.press("escape")
            self.assertTrue(self.row(IMPORTED).imported)
            self.assertFalse(self.exited())

    async def test_the_removal_itself_fails(self):
        real = self.engine.unimport
        busy = "an import or another undo is still running (process 7); nothing was removed"

        def refused(ref, force=False, dry_run=False, index=None):
            if dry_run:
                return real(ref, force=force, dry_run=True, index=index)
            self.engine.calls.append(("unimport", (ref, force, dry_run)))
            raise UndoFailed(busy)

        self.engine.unimport = refused
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, IMPORTED_COPY, "undo")
            await pilot.press("y")
            await self.message(busy)
            self.assertTrue(self.main.query_one("#status-message").has_class("-error"))
            self.assertFalse(self.main._undoing)
            self.assertTrue(self.row(IMPORTED).imported)
            self.assertEqual(self.selected(), IMPORTED_COPY)
            self.engine.unimport = real
            await self.ask(pilot, IMPORTED_COPY, "undo")  # and it can be tried again
            await pilot.press("y")
            await self.message("removed 2 files")

    async def test_an_undo_waits_for_a_scan(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.ask(pilot, IMPORTED_COPY, "undo")
            self.engine.scan_gate = threading.Event()
            self.main.start_scan(False)
            await until(lambda: len(self.engine.called("scan")) == 2, what="scan start")
            await pilot.press("y")
            await self.message("a scan is running; undo when it has finished")
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)])
            self.engine.scan_gate.set()
            await until(lambda: self.main._scan is None, what="scan end")

    async def test_the_answer_for_a_row_that_was_left_opens_nothing(self):
        gate = threading.Event()
        real = self.engine.unimport

        def slow(ref, force=False, dry_run=False, index=None):
            gate.wait(5)
            return real(ref, force=force, dry_run=dry_run, index=index)

        self.engine.unimport = slow
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, IMPORTED_COPY)
            await pilot.press("u")
            await pilot.press("j")
            gate.set()
            await settle(pilot)
            self.assertEqual(self.main.mode, "list")
            self.assertEqual(self.prompt(), "")
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)])
            # Esc while the engine is asked takes the key back, on the same row too.
            gate.clear()
            await pilot.press("k", "u")
            await pilot.press("escape")
            gate.set()
            await settle(pilot)
            self.assertEqual((self.main.mode, self.selected()), ("list", IMPORTED_COPY))
            self.assertEqual(self.undone(), [(IMPORTED_COPY, False, True)] * 2)  # dry runs


class PromptSizeTest(OfflineCase):
    """Every prompt and the folder input at every size and across a resize:
    no crash, no line wider than the terminal, the question still there."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine.continue_folder = os.path.realpath(tmp.name)
        self.engine.options[OTHER] = NEEDS_FOLDER
        self.engine.options[AGY] = EXPERIMENTAL
        self.engine.options["codex:bbbbbbbb:cof-0004"] = IMPOSSIBLE
        self.engine.used_since[OFFLINE_COPY] = ["a was modified"]

    # (the mode it ends in, the row, the keys that lead there)
    prompts = (
        ("mount", OFFLINE, ("enter",)),
        ("foreign", FOREIGN, ("enter",)),
        ("foreign_folder", OTHER, ("enter",)),
        ("foreign_experimental", AGY, ("enter",)),
        ("foreign_none", "codex:bbbbbbbb:cof-0004", ("enter",)),
        ("confirm", FOREIGN, ("i",)),
        ("warn", AGY, ("enter", "i")),
        ("folder", FOREIGN, ("t",)),
        ("tool", NATIVE, ("c",)),
        ("folder", NATIVE, ("c", "enter")),
        ("start", NATIVE, ("c", "enter", "enter")),
        ("undo", IMPORTED_COPY, ("u",)),
        ("undo_used", OFFLINE_COPY, ("u",)),
    )

    async def open_prompt(self, pilot, mode, key, keys):
        await self.select(pilot, key)
        for press in keys:
            before = self.main.mode
            await pilot.press(press)
            await until(
                lambda before=before: self.main.mode != before,
                what=f"{press!r} on the way to {mode}",
            )
            await pilot.pause()
        self.assertEqual(self.main.mode, mode)

    def test_every_prompt_is_in_the_table(self):
        self.assertEqual(
            {mode for mode, _key, _keys in self.prompts}, {*view.MODAL_MODES, "folder"}
        )

    async def at_size(self, width, height):
        async with self.started(size=(width, height)) as pilot:
            await settle(pilot)
            for mode, key, keys in self.prompts:
                await self.open_prompt(pilot, mode, key, keys)
                lines = self.check_frame(width, height)
                if mode != "folder":
                    shown = self.prompt().splitlines()
                    self.assertTrue(shown, mode)
                    for line in shown:  # the prompt is on screen, whole
                        self.assertIn(line.strip(), "\n".join(lines), (mode, width))
                await pilot.press("escape")
                await settle(pilot)
                self.assertEqual(self.main.mode, "list", mode)
            await pilot.press("j", "k", "question_mark", "escape")
            self.assertEqual(self.acted(), [])
            self.assertFalse(self.exited())

    async def test_120x40(self):
        await self.at_size(120, 40)

    async def test_80x24(self):
        await self.at_size(80, 24)

    async def test_40x10(self):
        await self.at_size(40, 10)

    async def test_resize_with_each_open(self):
        sizes = ((80, 24), (40, 10), (10, 3), (120, 40))
        async with self.started() as pilot:
            await settle(pilot)
            for mode, key, keys in self.prompts:
                await self.open_prompt(pilot, mode, key, keys)
                for width, height in sizes:
                    await pilot.resize_terminal(width, height)
                    await settle(pilot)
                    self.check_frame(width, height)
                    self.assertEqual(self.main.mode, mode, (mode, width, height))
                    self.assertEqual(self.selected(), key)
                await pilot.press("escape")
                await settle(pilot)
                self.assertEqual(self.main.mode, "list", mode)
            self.assertEqual(self.acted(), [])
            self.assertFalse(self.exited())

    async def test_the_prompt_is_worded_again_for_the_new_width(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.open_prompt(pilot, "foreign_experimental", AGY, ("enter",))
            self.assertEqual(self.prompt().splitlines()[1], EXPERIMENTAL_CHOICES)
            await pilot.resize_terminal(80, 24)
            await settle(pilot)
            self.assertEqual(
                self.prompt().splitlines()[1],
                "[i] import+resume (exp.)  [I] import (exp.)  [c] continue  [v] view  [Esc]",
            )
            await pilot.resize_terminal(60, 24)
            await settle(pilot)
            self.assertEqual(
                self.prompt().splitlines()[1], "[i] resume [I] import [c] new [v] view [Esc]"
            )
            await pilot.press("i")  # the warning takes the lines it needs, three at most
            await pilot.pause()
            lines = self.prompt().splitlines()
            self.assertEqual((len(lines), lines[-1]), (4, "Import anyway? y/n"))
            self.assertTrue(lines[-2].endswith("…"))
            await pilot.resize_terminal(40, 10)
            await settle(pilot)
            lines = self.prompt().splitlines()
            self.assertEqual((len(lines), lines[-1]), (4, "Import anyway? y/n"))
            self.check_frame(40, 10)
            self.assertIn("Import anyway? y/n", self.text())


# --- quitting (AUDIT A-02) -------------------------------------------------------------


class LeaveTest(TuiCase):
    """``q`` and Ctrl-C leave at once: nothing that only reads is waited
    for. An import or an undo under way is finished first, and says so."""

    async def import_under_way(self, pilot, keys=("i", "y")) -> threading.Event:
        await settle(pilot)
        await self.select(pilot, FOREIGN)
        gate = self.engine.block["import_session"] = threading.Event()
        await pilot.press(*keys)
        await until(lambda: self.imported() == [FOREIGN], what="the import")
        await pilot.pause()
        return gate

    async def test_q_during_an_import_waits_for_it_and_says_so(self):
        async with self.started() as pilot:
            gate = await self.import_under_way(pilot)
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(self.exited())
            self.assertIn("finishing the import…", self.status())
            # Meanwhile no key does anything, a second q or Ctrl-C included.
            await pilot.press("q", "j", "enter", "v", "question_mark", "r", "ctrl+c")
            await pilot.pause()
            self.assertFalse(self.exited())
            self.assertIs(self.app.screen, self.main)
            self.assertEqual(self.selected(), FOREIGN)
            self.assertEqual(self.engine.called("scan"), [None])
            self.assertIn("finishing the import…", self.status())
            gate.set()
            await until(self.exited, what="the exit after the import")
        self.assertEqual(self.app.return_code, 0)
        self.assertIn(FOREIGN_COPY, [session.key for session in self.engine.sessions])
        self.assertEqual(self.opened(), [])

    async def test_an_import_and_resume_resumes_nothing_once_asked_to_quit(self):
        async with self.started() as pilot:
            gate = await self.import_under_way(pilot, ("enter", "i"))
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertIn("finishing the import…", self.status())
            gate.set()
            await until(self.exited, what="the exit after the import")
        self.assertEqual((self.app.return_code, self.opened()), (0, []))

    async def test_ctrl_c_in_the_pager_during_an_import_shows_why_it_waits(self):
        async with self.started() as pilot:
            gate = await self.import_under_way(pilot)
            await pilot.press("v")
            await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="the pager")
            await pilot.press("ctrl+c")
            await until(lambda: self.app.screen is self.main, what="the pager closed")
            self.assertIn("finishing the import…", self.status())
            gate.set()
            await until(self.exited, what="the exit")
        self.assertEqual(self.app.return_code, 0)

    async def test_a_signal_during_an_import_waits_for_it_too(self):
        async with self.started() as pilot:
            gate = await self.import_under_way(pilot)
            self.app.leave(143)  # what SIGTERM does
            await pilot.pause()
            self.assertFalse(self.exited())
            gate.set()
            await until(self.exited, what="the exit")
        self.assertEqual(self.app.return_code, 143)

    async def test_q_during_an_undo_waits_for_it(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, IMPORTED_COPY)
            await pilot.press("u")
            await self.mode_is(pilot, "undo")
            gate = self.engine.block["unimport"] = threading.Event()
            await pilot.press("y")
            await until(lambda: len(self.undone()) == 2, what="the undo")
            await pilot.press("q")
            await pilot.pause()
            self.assertFalse(self.exited())
            self.assertIn("finishing the undo…", self.status())
            gate.set()
            await until(self.exited, what="the exit")
        self.assertEqual(self.app.return_code, 0)
        self.assertNotIn(IMPORTED_COPY, [session.key for session in self.engine.sessions])

    async def test_a_read_that_does_not_come_back_does_not_hold_q(self):
        """Every engine call that only reads runs in a daemon thread, which
        the interpreter does not wait for at its end; an import and an undo
        run in threads it waits for."""
        flows = (  # (the call that does not come back, the row, the keys that make it)
            ("scan", None, ()),
            ("list_folder", FOREIGN, ("t", "tab")),
            ("transcript", NATIVE, ("v",)),
            ("import_options", FOREIGN, ("enter",)),
            ("continue_options", NATIVE, ("c",)),
            ("open_session", NATIVE, ("o",)),
            ("unimport", IMPORTED_COPY, ("u",)),  # the dry run
            ("volumes", OFFLINE, ("enter",)),
            ("mount_drive", OFFLINE, ("m",)),
        )
        for name, key, keys in flows:
            engine = FakeEngine()
            engine.sessions.append(make_session("claude", 70, foreign=True, offline=True))
            gate = threading.Event()
            if key is None:
                engine.block[name] = gate
            async with self.started(engine=engine) as pilot:
                if key is None:
                    await until(lambda e=engine: e.called("scan"), what="the scan")
                else:
                    await settle(pilot)
                    await self.select(pilot, key)
                    self.main._verdicts.clear()  # so that the engine is asked again
                    engine.block[name] = gate
                    for press in keys:
                        await pilot.press(press)
                        await pilot.pause()
                await until(lambda n=name, e=engine: n in e.threads, what=f"the call {name}")
                await pilot.pause()
                started = time.monotonic()
                await pilot.press("ctrl+c")
                await until(self.exited, what=f"the exit while {name} hangs")
                self.assertLess(time.monotonic() - started, 0.5, name)
            self.assertEqual(self.app.return_code, 0, name)
            self.assertTrue(engine.threads[name].daemon, name)
            gate.set()
        engine = FakeEngine()
        async with self.started(engine=engine) as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("i", "y")
            await until(lambda: self.imported() == [FOREIGN], what="the import")
            await settle(pilot)
            await self.select(pilot, IMPORTED_COPY)
            await pilot.press("u")
            await self.mode_is(pilot, "undo")
            await pilot.press("y")
            await settle(pilot)
        self.assertFalse(engine.threads["import_session"].daemon)
        self.assertFalse(engine.threads["unimport"].daemon)

    async def test_every_cancel_event_is_set(self):
        self.engine.scan_gate = threading.Event()
        async with self.started() as pilot:
            await until(lambda: self.main._scan is not None, what="the scan")
            cancel = self.main._scan["cancel"]
            await pilot.press("q")
            await until(self.exited, what="the exit")
        self.assertTrue(cancel.is_set())
        self.engine.scan_gate.set()


@unittest.skipIf(textual is None, "Textual is not installed (run under .venv: make test-tui)")
class PtyCase(unittest.TestCase):
    """The app over the fake engine as a program of its own, in a
    pseudo-terminal made for it alone (``tui_support.pty_run``)."""

    def play(self, setup, steps, size=(100, 30), **env):
        with tempfile.TemporaryDirectory() as tmp:
            return support.pty_run(
                [sys.executable, "-P", "-c", support.PTY_APP, setup],
                steps,
                support.pty_env(tmp, **env),
                tmp,
                size=size,
            )

    def assert_left_cleanly(self, run, what):
        screen = support.plain(run.output)
        self.assertEqual(run.status, 0, (what, screen[-1500:]))
        self.assertNotIn("Traceback", screen, what)
        after = support.plain(run.output[run.output.rfind(b"\x1b[?1049l") :])
        self.assertNotIn("Error", after, what)


def find(title) -> list:
    """Steps that put the selection on the session with ``title``: the All
    tab, the filter, the title, Enter. A key apart from the next, as typed:
    in one write the title would reach the list before the filter box has
    the focus."""
    return [("q quit", b"0"), (0.2, b"/"), (0.3, title.encode()), (0.3, b"\r"), (0.3, b"")]


FILTER_12 = "claude session 12"  # the foreign session that can be imported
FILTER_70 = "claude session 70"  # the one whose drive is not mounted


class LeaveOnATerminalTest(PtyCase):
    """AUDIT A-02 end to end: the time from the key to the end of the
    process. The audit measured 6 s for a call slowed to 8 s; here a call
    does not come back for 30 s."""

    def quickest(self, setup, steps):
        """The fastest of up to three runs: a loaded machine can slow one,
        never all three by 30 s."""
        runs = []
        for _ in range(3):
            run = self.play(setup, steps)
            self.assert_left_cleanly(run, setup)
            runs.append(run.seconds)
            if run.seconds < 0.5:
                break
        return min(runs)

    def test_a_read_that_does_not_come_back_does_not_hold_the_exit(self):
        for setup, steps in (
            ("block:scan", [("scanning", b"q")]),
            ("block:transcript", [("q quit", b"v"), ("loading", b"\x03")]),
            (
                "block:list_folder",
                [*find(FILTER_12), (0, b"t"), ("Import into", b"\t"), (0.3, b"\x03")],
            ),
            ("block:mount_drive", [*find(FILTER_70), (0, b"m"), ("mounting", b"q")]),
            ("block:continue_options", [("q quit", b"c"), (0.3, b"q")]),
        ):
            self.assertLess(self.quickest(setup, steps), 0.5, setup)

    def test_an_import_under_way_is_finished_first(self):
        with tempfile.TemporaryDirectory() as marks:
            mark = os.path.join(marks, "imported")
            run = self.play(
                "import:1.5",
                [*find(FILTER_12), (0, b"i"), ("[y] yes", b"y"), ("importing", b"q")],
                PTY_MARK=mark,
            )
            self.assert_left_cleanly(run, "import")
            self.assertTrue(os.path.exists(mark))  # it was finished, not cut off
        self.assertIn("finishing the import…", support.plain(run.output))
        self.assertGreater(run.seconds, 0.8)  # it was waited for


# --- a key that arrives twice (AUDIT A-13) ---------------------------------------------


class TwiceTest(TuiCase):
    """Two presses that are both on their way before the first is handled:
    a key that repeats, or a second press because the first seemed lost."""

    async def twice(self, pilot, key, character, times=2):
        for _ in range(times):
            self.app.post_message(events.Key(key, character))
        await asyncio.sleep(0.3)
        await settle(pilot)

    def alive(self, screen_type):
        self.assertTrue(self.app.is_running)
        self.assertIsNone(self.app.return_code)
        self.assertIsInstance(self.app.screen, screen_type)

    async def test_closing_the_pager_twice(self):
        for key, character in (("q", "q"), ("escape", None)):
            async with self.started() as pilot:
                await settle(pilot)
                await self.select(pilot, NATIVE)
                await pilot.press("v")
                await settle(pilot)
                await self.twice(pilot, key, character)
                self.alive(MainScreen)
                self.assertEqual(len(self.app.screen_stack), 1)
                await pilot.press("v")  # and it still opens
                await settle(pilot)
                self.alive(TranscriptScreen)

    async def test_closing_the_help_twice(self):
        for key, character in (("q", "q"), ("question_mark", "?"), ("enter", "\r")):
            async with self.started() as pilot:
                await settle(pilot)
                await pilot.press("question_mark")
                await settle(pilot)
                await self.twice(pilot, key, character)
                self.alive(MainScreen)
                self.assertEqual(len(self.app.screen_stack), 1)

    async def test_clicking_the_help_away_twice(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("question_mark")
            await settle(pilot)
            help_screen = self.app.screen
            help_screen.on_click(None)
            help_screen.on_click(None)
            await settle(pilot)
            self.alive(MainScreen)
            self.assertEqual(len(self.app.screen_stack), 1)

    async def test_opening_twice_opens_once(self):
        for key, character, screen_type in (
            ("v", "v", TranscriptScreen),
            ("question_mark", "?", HelpScreen),
        ):
            for times in (2, 8):
                read = len(self.engine.called("transcript"))
                async with self.started() as pilot:
                    await settle(pilot)
                    await self.select(pilot, NATIVE)
                    await self.twice(pilot, key, character, times)
                    self.alive(screen_type)
                    self.assertEqual(len(self.app.screen_stack), 2, (key, times))
                    self.assertLessEqual(len(self.engine.called("transcript")) - read, 1)

    async def test_view_from_the_foreign_prompt_while_one_is_open(self):
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("enter")
            await self.mode_is(pilot, "foreign")
            await self.twice(pilot, "v", "v")
            self.alive(TranscriptScreen)
            self.assertEqual(len(self.app.screen_stack), 2)


class TwiceOnATerminalTest(PtyCase):
    """The audit's reproduction: several keys in one write to a terminal."""

    def test_keys_written_together(self):
        for what, steps in (
            ("qq", [("q quit", b"qq")]),
            ("qqqqqqqq", [("q quit", b"qqqqqqqq")]),
            ("??", [("q quit", b"??"), (0.5, b"\x1b"), (0.3, b"q")]),
            ("vv", [("q quit", b"vv"), ("3 turns", b"q"), (0.3, b"q")]),
            ("v, then qq", [("q quit", b"v"), ("3 turns", b"qq"), (0.5, b"q")]),
            ("v, then qqqqqqqq", [("q quit", b"v"), ("3 turns", b"qqqqqqqq"), (0.5, b"q")]),
        ):
            self.assert_left_cleanly(self.play("plain", steps), what)


# --- the pager stays responsive (AUDIT A-14) -------------------------------------------


class PagerResponsiveTest(TuiCase):
    """2,000 turns of 20,000 characters: the pager answers keys while it
    wraps them, and stops when it is closed."""

    async def open_long(self, pilot, text):
        self.engine.turns[NATIVE] = [Turn("assistant", text, None)] * view.PAGER_MAX_TURNS
        await settle(pilot)
        await self.select(pilot, NATIVE)
        await pilot.press("v")
        await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="the pager")
        body = self.app.screen.body
        await until(lambda: len(body.turns) >= 500, what="the turns")
        return body

    async def press_now(self, *keys):
        """Keys as a terminal hands them over: queued, with nothing waited
        for (the pilot's ``press`` waits until the app is idle, and a pager
        that wraps is not)."""
        for key in keys:
            self.app.post_message(events.Key(key, key if len(key) == 1 else None))

    async def test_q_is_answered_within_a_third_of_a_second(self):
        for text in ("word " * 4000, "中" * 20000):
            async with self.started() as pilot:
                body = await self.open_long(pilot, text)
                await self.press_now("j", "j", "pagedown")
                started = time.monotonic()
                await self.press_now("q")
                await until(lambda: self.app.screen is self.main, what="the pager closed")
                took = time.monotonic() - started
                self.assertLess(took, 0.3, text[:3])
                self.assertGreater(body.pending, 0)  # answered while there was work left
                # Closed: no more wrapping.
                left = body.pending
                await asyncio.sleep(0.2)
                self.assertEqual(body.pending, left)
                self.assertFalse(self.exited())

    async def test_a_resize_wraps_what_is_on_screen_first(self):
        self.engine.turns[NATIVE] = [
            Turn("assistant", f"turn {n} " + "word " * 600, None) for n in range(400)
        ]
        order = []
        wrap = view.transcript_lines

        def recorded(role, text, ts, width, tz=None, **options):
            order.append((width, int(text.split()[1])))
            return wrap(role, text, ts, width, tz, **options)

        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="the pager")
            body = self.app.screen.body
            await until(lambda: len(body.turns) == 400 and body.settled, what="wrapped", timeout=20)
            body.scroll_to(y=body._starts[300], animate=False, force=True, immediate=True)
            await pilot.pause()
            with mock.patch.object(view, "transcript_lines", recorded):
                await pilot.resize_terminal(50, 20)
                await until(lambda: body.settled, what="re-wrapped", timeout=20)
            width = body.size.width
            again = [turn for wrapped_for, turn in order if wrapped_for == width]
            self.assertEqual(sorted(again), list(range(400)))
            # The turn on screen first, then the ones below it that fill the
            # screen, then outwards, up and down by turns.
            self.assertEqual(again[0], 300)
            self.assertLess(again.index(301), again.index(310))
            self.assertLess(again.index(299), again.index(320))
            self.assertLess(again.index(250), again.index(0))
            self.assertEqual(int(body.scroll_offset.y), body._starts[300])
            self.assertIn("turn 300", self.text())
            self.assertTrue(all(cell_len(text) <= width for _kind, text in body.lines))

    async def test_turns_not_yet_wrapped_again_are_cut_to_the_width(self):
        self.engine.turns[NATIVE] = [Turn("assistant", "word " * 600, None)] * 300
        async with self.started(size=(120, 40)) as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("v")
            await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="the pager")
            body = self.app.screen.body
            await until(lambda: len(body.turns) == 300 and body.settled, what="wrapped", timeout=20)
            await pilot.resize_terminal(60, 20)
            await pilot.pause()
            self.check_frame(60, 20)


# --- nothing unsafe reaches the terminal (AUDIT A-12, A-15) ------------------------------


H = support.hostile
HN = support.hostile_name


class HostileTest(TuiCase):
    """SECURITY 6 for the TUI: every value a session or an engine answer
    puts on the screen carries escape sequences, C1 controls, bidirectional
    overrides, markup and lone surrogates. After every step the segments
    the compositor would send are searched: none may hold a character a
    terminal acts on. A tag at the start of each value shows that it was
    on the screen at all."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.tmp, HN("DIRQ"))  # a folder with a hostile name
        os.mkdir(self.dir)
        for tag in ("SUBQ1", "SUBQ2"):
            os.mkdir(os.path.join(self.tmp, HN(tag)))
        os.mkdir(os.path.join(self.tmp, "plain"))

    def hostile_engine(self) -> FakeEngine:
        native = make_session("claude", 1, title=H("NTITLE"), cwd="/home/user/" + HN("PJN"))
        native.id, native.store = H("NID"), H("NSTORE")
        native.cwd_local, native.note = H("NLOCAL"), H("NNOTE")
        refused = make_session("claude", 2, foreign=True, title=H("RTITLE"), age=1)
        experimental = make_session("codex", 3, foreign=True, title=H("XTITLE"), age=2)
        possible = make_session("claude", 4, foreign=True, title=H("PTITLE"), age=3)
        untitled = make_session("claude", 5, foreign=True, title="", age=4)
        untitled.id = H("UID")
        offline = make_session("claude", 6, foreign=True, offline=True, title=H("OTITLE"), age=5)
        offline.note = H("ONOTE")
        stranger = make_session("grok", 7, title=H("STITLE"), age=6)
        stranger.provider = H("PROVQ")
        engine = FakeEngine([native, refused, experimental, possible, untitled, offline, stranger])
        engine.provider_list = [("claude", H("LABQ")), ("codex", "Codex"), ("grok", "Grok")]
        engine.where_text = H("WHQ")
        engine.options[refused.key] = ImportOptions(False, H("REASONQ"))
        engine.options[experimental.key] = ImportOptions(
            False, "experimental", False, True, H("WARNQ")
        )
        engine.continue_answers[possible.key] = ContinueOptions(
            True, "", (("claude", H("TOOLQ")),), "claude", self.dir
        )
        engine.scan_progress = [(H("DRIVEQ"), 41000)]
        engine.turns[native.key] = [
            Turn("user", H("TURNQ"), H("TSQ")),
            Turn("tool", H("TOOLNAMEQ"), None),
            Turn(H("ROLEQ"), H("REPLYQ"), None),
        ]
        engine.plan_note = H("PLANQ")
        self.keys = {
            "native": native.key,
            "refused": refused.key,
            "experimental": experimental.key,
            "possible": possible.key,
            "untitled": untitled.key,
            "offline": offline.key,
            "stranger": stranger.key,
        }
        return engine

    async def said(self, fragment):
        """Wait for the status line to have ``fragment`` in what it was
        given (on a narrow screen the long values cut it short)."""
        main = self.main
        await until(
            lambda: fragment in main._message or fragment in main._activity,
            what=f"the status line given {fragment!r}",
        )
        await asyncio.sleep(0.05)

    def check(self, step, *tags):
        found = support.unsafe_on_screen(self.app)
        self.assertEqual(found, [], f"{step} at {self.app.size}: {found[:3]!r}")
        text = self.text()
        for tag in tags:
            self.assertIn(tag, text, f"{step} at {self.app.size}")

    async def test_nothing_unsafe_reaches_the_screen(self):
        # 200 columns as well: only there does the import question have room
        # for the title beside a drive name this long.
        for size in ((120, 40), (80, 24), (200, 50)):
            engine = self.hostile_engine()
            engine.scan_gate = threading.Event()
            async with self.started(size=size, engine=engine) as pilot:
                await self.walk(pilot, engine)

    async def walk(self, pilot, engine):
        keys = self.keys
        await until(lambda: self.main.loaded and "DRIVEQ" in self.status(), what="the scan")
        self.check("scan progress", "DRIVEQ")
        engine.scan_gate.set()
        await settle(pilot)
        await pilot.press("0")
        await settle(pilot)
        wide = self.app.size.width >= 120  # PROJECT beside PROVIDER
        self.check("the list", "NTITLE", "WHQ", "PROVQ", *(("PJN",) if wide else ()))
        await pilot.press("1")
        await settle(pilot)
        self.check("the tabs", "LABQ")
        await self.select(pilot, keys["native"])
        self.check("the detail pane", "NID", "NSTORE", "NLOCAL", "NNOTE")
        await pilot.press("o")
        await self.said("opened NTITLE")
        self.check("a launch", "opened NTITLE")
        await pilot.press("v")
        await until(lambda: isinstance(self.app.screen, TranscriptScreen), what="the pager")
        await settle(pilot)
        self.check("the pager", "NTITLE", "TURNQ", "TOOLNAMEQ", "REPLYQ")
        await pilot.press("q")
        await settle(pilot)

        await self.select(pilot, keys["refused"])
        self.check("a refused import", "RTITLE", "REASONQ")
        await pilot.press("enter")
        await self.mode_is(pilot, "foreign_none")
        self.check("the foreign prompt", "REASONQ")
        await pilot.press("escape", "i")
        await self.said("REASONQ")
        self.check("the refusal")

        await self.select(pilot, keys["experimental"])
        await pilot.press("i")
        await self.mode_is(pilot, "warn")
        self.check("the warning", "WARNQ")
        await pilot.press("n")

        await self.select(pilot, keys["possible"])
        await pilot.press("i")
        await self.mode_is(pilot, "confirm")
        self.check("the import question", "PTITLE", "WHQ")
        if self.app.size.width >= 200:
            self.assertIn("PTITLE", self.prompt())
        await pilot.press("n", "enter")
        await self.mode_is(pilot, "foreign")
        self.check("the import prompt", "WHQ")
        await pilot.press("escape")

        await self.select(pilot, keys["untitled"])
        await pilot.press("i")
        await self.mode_is(pilot, "confirm")
        self.check("the question about a session without a title", "UID")
        if self.app.size.width >= 200:
            self.assertIn("UID", self.prompt())
        await pilot.press("n")

        # Continue: the tool, then a folder the input must not take as it is.
        await self.select(pilot, keys["possible"])
        await pilot.press("c")
        await self.mode_is(pilot, "tool")
        self.check("the tool", "TOOLQ")
        await pilot.press("enter")
        await self.mode_is(pilot, "folder")
        await settle(pilot)
        self.assertEqual(self.folder.value, view.FOLDER_START)
        self.assertIn("folder name has characters that cannot be shown", self.status())
        self.check("the folder input")
        self.folder.insert_text_at_cursor(support.HOSTILE)  # a paste
        await pilot.pause()
        self.check("a paste into the folder input")
        await self.type_folder(pilot, self.tmp + "/")
        await pilot.press("tab")
        await settle(pilot)
        self.assertEqual(self.folder.value, self.tmp + "/plain/")
        self.assertIn("3 names left out", self.status())
        self.check("the names Tab found")
        await self.type_folder(pilot, self.dir)
        await pilot.press("enter")
        await self.mode_is(pilot, "start")
        self.check("the start question", *(("TOOLQ",) if wide else ()))
        await pilot.press("n")

        await pilot.press("slash")
        self.main.filter_input.insert_text_at_cursor(H("NEEDLEQ"))
        await settle(pilot)
        self.check("a filter that matches nothing", "NEEDLEQ")
        await pilot.press("escape")
        await settle(pilot)

        await self.select(pilot, keys["offline"])
        self.check("an offline row", "ONOTE")
        engine.volume_state = "absent"
        await pilot.press("enter")
        await self.said("is not attached")
        self.check("a drive that is not attached", "WHQ")
        engine.volume_state = "attached"
        engine.mount_gate = threading.Event()
        await pilot.press("m")
        await self.said("mounting WHQ")
        self.check("the mount", "WHQ")
        engine.mount_gate.set()
        await self.said("mounted")
        self.check("a mount that is done", "WHQ")

        engine.fail["continue_options"] = FakeError(H("FAILQ"))
        await self.select(pilot, keys["native"])
        await pilot.press("c")
        await self.said("FAILQ")
        self.check("an engine failure", "FAILQ")
        await pilot.press("question_mark")
        await settle(pilot)
        self.check("the help")
        await pilot.press("escape")

    async def test_markup_in_every_value_is_shown_as_text(self):
        """A-15: ``markup=False`` on the detail values was held by no test."""
        markup = "[red]K[/red] [@click=app.quit]L[/] [bold]M"
        session = make_session("claude", 1, title="[b]T[/b]", cwd="/x/[b]P[red]")
        session.id, session.store, session.cwd_local, session.note = (
            f"I{markup}",
            f"S{markup}",
            f"L{markup}",
            f"N{markup}",
        )
        async with self.started(size=(160, 40), engine=FakeEngine([session])) as pilot:
            await settle(pilot)
            await self.select(pilot, session.key)
            text = self.text()
            for value in (session.id, session.store, session.note):
                self.assertIn(value, text)
            self.assertIn(f"L{markup}", text)
            self.assertIn("[b]T[/b]", text)
            self.assertIn("/x/[b]P[red]", text)
            self.assertFalse(self.exited())

    async def test_the_inputs_draw_what_they_hold_cleaned(self):
        async with self.started() as pilot:
            await settle(pilot)
            await pilot.press("slash")
            await pilot.pause()
            self.main.filter_input.value = H("FILTERQ")
            await settle(pilot)
            self.check("the filter input", "FILTERQ")
            self.assertEqual(self.main.filter_input.value, H("FILTERQ"))  # held as it is
            await pilot.press("escape")
            await self.select(pilot, FOREIGN)
            await pilot.press("t")
            await self.mode_is(pilot, "folder")
            self.folder.value = "/mnt/" + H("FOLDERQ")
            self.folder.cursor_position = 5
            await settle(pilot)
            self.check("the folder input", "FOLDERQ")
            # The cursor stands where the cleaned text puts it.
            self.assertEqual(self.folder._cursor_offset, 5)


# --- times that cannot be placed (AUDIT A-26) --------------------------------------------


class UnplacedTimeTest(TuiCase):
    async def test_a_time_out_of_range_or_without_a_zone_is_a_dash(self):
        india = timezone(timedelta(hours=5, minutes=30))
        sessions = [make_session("claude", n) for n in range(3)]
        sessions[0].updated = "9999-12-31T23:59:59.000000Z"
        sessions[1].updated = "2026-10-05T10:00:00"
        sessions[2].started, sessions[2].updated = "0001-01-01T00:00:00Z", None
        engine = FakeEngine(sessions)
        self.app = SessionsApp(engine=engine, clock=lambda: support.NOW, tz=india)
        async with self.app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            for session in sessions:
                await self.select(pilot, session.key)
                text = self.text()
                self.assertRegex(text, r"updated +-")
            self.assertTrue(self.app.is_running)
            self.assertFalse(self.exited())


# --- the folder input: what it shows and what it checks (N-32, N-33, N-34, N-37) -----------


class FolderNitsTest(TuiCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

    async def test_a_long_suggested_folder_shows_its_end(self):
        """N-32: the input opened at the beginning of a 212-character
        folder, and its end could not be seen until a key was typed."""
        deep = os.path.join(self.tmp, *[f"segment-{n:02d}" for n in range(18)], "THE-END")
        os.makedirs(deep)
        self.assertGreater(len(deep), 200)
        self.engine.continue_folder = deep
        async with self.started(size=(80, 24)) as pilot:
            await settle(pilot)
            await self.select(pilot, NATIVE)
            await pilot.press("c")
            await self.mode_is(pilot, "tool")
            await pilot.press("enter")
            await self.mode_is(pilot, "folder")
            await settle(pilot)
            await pilot.pause()
            line = next(line for line in self.text().splitlines() if "Start in:" in line)
            self.assertIn("THE-END", line)
            self.assertEqual(self.folder.cursor_position, len(deep))

    async def test_a_folder_is_checked_as_it_is_used(self):
        """N-33: ``<root>/a/link/../y`` was looked at as ``<root>/b/y`` (the
        link followed) and imported into ``<root>/a/y``."""
        for name in ("a", "b/c", "b/y"):
            os.makedirs(os.path.join(self.tmp, name))
        os.symlink(os.path.join(self.tmp, "b", "c"), os.path.join(self.tmp, "a", "link"))
        typed = os.path.join(self.tmp, "a", "link", "..", "y")
        self.assertTrue(os.path.isdir(typed))  # what the old check looked at
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("t")
            await self.mode_is(pilot, "folder")
            await self.type_folder(pilot, typed)
            await pilot.press("enter")
            await self.message("not an existing folder")
            self.assertEqual(self.imported(), [])
            os.mkdir(os.path.join(self.tmp, "a", "y"))
            await pilot.press("enter")
            await self.message("imported claude session 12")
        self.assertEqual(
            self.engine.import_args, [(FOREIGN, os.path.join(self.tmp, "a", "y"), False)]
        )

    async def test_tilde_is_the_engines_home(self):
        """N-34: not ``$HOME``, which ``AGENT_SESSIONS_HOME`` leaves alone."""
        self.engine.home_dir = self.tmp
        with mock.patch.dict(os.environ, {"HOME": "/nonexistent-home"}):
            async with self.started() as pilot:
                await settle(pilot)
                await self.select(pilot, FOREIGN)
                await pilot.press("t")
                await self.mode_is(pilot, "folder")
                await pilot.press("enter")  # "~/" as it opens
                await self.message("imported claude session 12")
        self.assertEqual(self.engine.import_args, [(FOREIGN, self.tmp, False)])

    async def test_a_nul_is_refused_in_plain_words(self):
        """N-37: Tab said ``open: embedded null character in path``."""
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await pilot.press("t")
            await self.mode_is(pilot, "folder")
            await self.type_folder(pilot, self.tmp + "/a\0b")
            for key in ("tab", "enter"):
                await pilot.press(key)
                await self.message(view.FOLDER_NUL)
                self.assertEqual(self.main.mode, "folder")
            self.assertEqual(self.engine.called("list_folder"), [])
            self.assertEqual(self.imported(), [])


if __name__ == "__main__":
    unittest.main()
