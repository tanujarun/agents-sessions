"""The TUI driven through Textual's headless pilot, against a fake engine.

Nothing here starts a tool, imports into a store or reads a real index: the
engine is ``tui_support.FakeEngine``, which only records what it was asked.
Needs Textual, so the whole module skips on a Python without it
(``make test-tui`` runs it under ``.venv``)."""

import asyncio
import sys
import threading
import time
import unittest
from datetime import UTC
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for _path in (str(REPO / "src"), str(REPO / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import tui_support as support  # noqa: E402
from tui_support import FakeEngine, FakeError, Turn, make_session, screen_text, settle  # noqa: E402

try:
    import textual
except ImportError:
    textual = None  # type: ignore[assignment]  # the suite then skips itself

if textual is not None:
    from rich.cells import cell_len

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

    def exited(self) -> bool:
        return self.app.return_code is not None

    async def select(self, pilot, key):
        """Put the selection on a session by key (All tab if need be)."""
        keys = [row.key for row in self.rows.rows]
        if key not in keys:
            await pilot.press("0")
            keys = [row.key for row in self.rows.rows]
        self.rows.select(keys.index(key))
        await settle(pilot)
        self.assertEqual(self.selected(), key)

    async def message(self, fragment, timeout=5.0):
        await until(lambda: fragment in self.status(), timeout, f"status line with {fragment!r}")


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
            self.assertEqual(self.opened() + self.imported(), [])
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
        self.engine.verdicts[FOREIGN] = (False, "this format cannot be imported safely")
        async with self.started() as pilot:
            await settle(pilot)
            await self.select(pilot, FOREIGN)
            await until(lambda: FOREIGN in self.main._verdicts, what="verdict")
            await pilot.pause()
            self.assertIn("not possible: this format cannot be imported safely", self.text())
            await pilot.press("up", "down", "up", "down")
            await asyncio.sleep(0.1)
            await settle(pilot)
            self.assertEqual(self.engine.called("can_import").count(FOREIGN), 1)
            # A native row and an already-imported row never ask.
            await self.select(pilot, NATIVE)
            await self.select(pilot, IMPORTED)
            await asyncio.sleep(0.1)
            await settle(pilot)
            self.assertIn("already imported", self.text())
            self.assertNotIn(NATIVE, self.engine.called("can_import"))
            self.assertNotIn(IMPORTED, self.engine.called("can_import"))

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
        self.assertEqual(self.main.mode, "foreign")
        self.assertIn(
            "On C: [i] import and resume  [I] import only  [v] view  [Esc] cancel", self.text()
        )
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
            await pilot.press("j", "q", "enter", "o", "r", "2", "y")
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
            self.assertEqual(self.main.mode, "confirm")
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

        def slow_import(ref, progress=None, index=None):
            progress(3_100_000, 12_000_000)
            gate.wait(5)
            return real_import(ref, progress=None, index=index)

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
        self.engine.verdicts[FOREIGN] = (False, "the mapped folder has characters Grok cannot use")
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
            await until(lambda: len(body._starts) == 200, what="all turns wrapped")
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
            await until(lambda: len(body._starts) == 120, what="wrapped")
            before = len(body.lines)
            body.scroll_to(y=body._starts[60], animate=False, force=True, immediate=True)
            await pilot.pause()
            await pilot.resize_terminal(40, 12)
            await until(
                lambda: len(body._starts) == 120 and body._anchor is None, what="re-wrapped"
            )
            await pilot.pause()
            self.assertGreater(len(body.lines), before)
            self.assertTrue(all(cell_len(text) <= body.size.width for _kind, text in body.lines))
            self.assertEqual(int(body.scroll_offset.y), body._starts[60])
            self.assertIn("turn 60", self.text())
            await pilot.resize_terminal(120, 40)
            await until(
                lambda: len(body._starts) == 120 and body._anchor is None, what="re-wrapped"
            )
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
            "can_import",
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
            self.assertIn("unknown: this build of the engine has no `can_import` yet", self.text())
            await pilot.press("i", "y")
            await self.message("this build of the engine has no `import_session` yet")
            await pilot.press("v")
            await settle(pilot)
            await until(lambda: "has no `transcript` yet" in self.text(), what="pager error")
            await pilot.press("q")
            await settle(pilot)
            self.assertFalse(self.exited())


class SizeTest(TuiCase):
    def check_frame(self, width, height):
        lines = self.text().splitlines()
        self.assertEqual(len(lines), height)
        for line in lines:
            self.assertLessEqual(cell_len(line), width)
        return lines

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
            self.assertIn("/ filter  r rescan  R full rescan  ? help  q quit", lines[-1])
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
            self.assertIn("[i] resume [I] import [v] view [Esc]", self.text())
            await pilot.resize_terminal(60, 10)
            self.assertIn("On C: [i] import+resume  [I] import  [v] view  [Esc]", self.text())
            await pilot.resize_terminal(40, 10)
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
            ):
                self.assertIn(fragment, text)
            await pilot.press("o", "i", "r", "1")  # no meaning here
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.assertEqual(self.engine.called("scan"), [None])
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
        for keys in ((), ("slash", "a"), ("question_mark",), ("v",), ("end", "enter")):
            async with self.started() as pilot:
                await settle(pilot)
                await pilot.press("0", *keys)
                await settle(pilot)
                await pilot.press("ctrl+c")
                await until(self.exited, what=f"exit after {keys}")
            self.assertEqual(self.app.return_code, 0)
            self.assertEqual(self.opened() + self.imported(), [])

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


if __name__ == "__main__":
    unittest.main()
