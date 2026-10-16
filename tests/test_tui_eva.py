"""The Evangelion presentation layer (``tui/eva.py``), and the app with its
transitions switched on."""

import asyncio
import importlib.util
import unittest
from typing import Any

from support import FIXTURES  # noqa: F401  (points state, cache and home at temp dirs)
from tui_support import FakeEngine, default_sessions, make_session, settle

from agent_sessions.tui import eva, view

HAS_TEXTUAL = importlib.util.find_spec("textual") is not None


def row(**fields: Any) -> view.Row:
    base: dict[str, Any] = {
        "key": "k",
        "provider": "claude",
        "title": "t",
        "project": "p",
        "stamp": None,
        "where": "Linux",
        "foreign": False,
        "imported": False,
        "haystack": "",
    }
    base.update(fields)
    return view.Row(**base)


class Session:
    def __init__(self, origin="native", launchable=True, imported_as=None, offline=False):
        self.origin = origin
        self.launchable = launchable
        self.imported_as = imported_as
        self.offline = offline


class HelpersTest(unittest.TestCase):
    def test_motion_is_on_unless_switched_off(self):
        self.assertTrue(eva.motion_enabled({}))
        for off in ("0", "false", "No", " off "):
            self.assertFalse(eva.motion_enabled({eva.MOTION_ENV: off}), off)
        self.assertTrue(eva.motion_enabled({eva.MOTION_ENV: "1"}))

    def test_rows_are_numbered_in_three_digits(self):
        self.assertEqual(eva.row_number(0), "001")
        self.assertEqual(eva.row_number(41), "042")
        self.assertEqual(len(eva.row_number(1500)), 3)
        self.assertEqual(eva.gutter_text(2, True), "003▶ ")
        self.assertEqual(eva.gutter_text(2, False), "003  ")
        self.assertEqual(len(eva.gutter_text(0, True)), eva.GUTTER)

    def test_signals(self):
        self.assertEqual(eva.signal(row()), "internal")
        self.assertEqual(eva.signal(row(foreign=True, where="C:")), "external")
        self.assertEqual(eva.signal(row(foreign=True, imported=True)), "imported")
        self.assertEqual(eva.signal(row(foreign=True, offline=True)), "lost")

    def test_a_lost_drive_reads_signal_lost_only_where_it_is_drawn(self):
        lost = row(foreign=True, offline=True, where="C: · offline")
        self.assertEqual(eva.shown_where(lost), "C: SIGNAL LOST")
        self.assertEqual(eva.drawn_row(lost).where, "C: SIGNAL LOST")
        self.assertEqual(lost.where, "C: · offline")  # the row keeps the engine's words
        plain = row()
        self.assertIs(eva.drawn_row(plain), plain)

    def test_a_wipe_uncovers_any_panel_in_a_few_frames(self):
        for height in (1, 9, 30, 200):
            step = eva.reveal_step(height)
            self.assertGreaterEqual(step, 1)
            self.assertLessEqual(-(-height // step), 6, height)
        self.assertEqual(eva.reveal_step(0), 1)

    def test_severity(self):
        for mode in ("warn", "undo", "undo_used", "start"):
            self.assertEqual(eva.severity(mode), "danger", mode)
        for mode in ("foreign", "confirm", "mount", "tool"):
            self.assertEqual(eva.severity(mode), "caution", mode)

    def test_a_hazard_band_fills_its_width_and_marches(self):
        for width in (0, 3, 20, 80, 121):
            for phase in range(6):
                line = eva.stripe_line(width, phase, eva.WARNING_LABEL)
                self.assertEqual(view.text_width(line), width, (width, phase))
        self.assertIn(eva.WARNING_LABEL, eva.stripe_line(80, 0, eva.WARNING_LABEL))
        self.assertNotIn(eva.WARNING_LABEL, eva.stripe_line(10, 0, eva.WARNING_LABEL))
        self.assertNotEqual(eva.stripe_line(40, 0), eva.stripe_line(40, 1))
        self.assertEqual(eva.stripe_line(40, 0), eva.stripe_line(40, len(eva.STRIPE)))

    def test_hint_spans_keep_the_text_and_name_the_keys(self):
        for hints in (view.HINTS_FULL, view.HINTS_MEDIUM, view.HINTS_SHORT):
            keys = []
            for line in hints:
                spans = eva.hint_spans(line)
                self.assertEqual("".join(text for _, text in spans), line)
                keys += [text for kind, text in spans if kind == "key"]
            self.assertIn("q", keys, hints)
        self.assertEqual(eva.hint_spans("↑↓ move")[0], ("key", "↑↓"))

    def test_flicker_ends_on(self):
        self.assertEqual(eva.flicker_frames()[-1], "on")


class MagiTest(unittest.TestCase):
    def verdicts(self, session, verdict=None):
        return [
            (vote.subject, vote.verdict, vote.note) for vote in eva.magi_votes(session, verdict)
        ]

    def test_no_session_is_no_vote(self):
        self.assertEqual(
            {vote.verdict for vote in eva.magi_votes(None, None)}, {eva.NOT_APPLICABLE}
        )

    def test_a_native_session(self):
        self.assertEqual(
            self.verdicts(Session()),
            [
                ("RESUME", eva.APPROVE, "READY"),
                ("IMPORT", eva.NOT_APPLICABLE, "INTERNAL"),
                ("DRIVE", eva.APPROVE, "INTERNAL"),
            ],
        )
        self.assertEqual(self.verdicts(Session(launchable=False))[0][1], eva.REJECT)

    def test_a_foreign_session_follows_the_import_verdict(self):
        foreign = Session(origin="foreign", launchable=False)
        self.assertEqual(self.verdicts(foreign)[1][1], eva.PENDING)  # still being asked
        cases = (
            (view.Verdict(possible=True), eva.APPROVE),
            (view.Verdict(needs_folder=True), eva.CONDITIONAL),
            (view.Verdict(experimental=True), eva.CONDITIONAL),
            (view.Verdict(reason="no"), eva.REJECT),
            (view.Verdict(known=False, reason="?"), eva.REJECT),
        )
        for verdict, expected in cases:
            self.assertEqual(self.verdicts(foreign, verdict)[1][1], expected, verdict)
        self.assertEqual(self.verdicts(foreign)[0][1], eva.REJECT)  # import first
        self.assertEqual(self.verdicts(foreign)[2], ("DRIVE", eva.APPROVE, "EXTERNAL"))

    def test_an_imported_session_resumes_its_copy(self):
        votes = self.verdicts(Session(origin="foreign", launchable=False, imported_as="x"))
        self.assertEqual(votes[0], ("RESUME", eva.APPROVE, "VIA COPY"))
        self.assertEqual(votes[1], ("IMPORT", eva.APPROVE, "DONE"))

    def test_signal_lost(self):
        votes = self.verdicts(Session(origin="foreign", launchable=False, offline=True))
        self.assertEqual(
            votes,
            [
                ("RESUME", eva.REJECT, "NO SIGNAL"),
                ("IMPORT", eva.REJECT, "NO SIGNAL"),
                ("DRIVE", eva.REJECT, "SIGNAL LOST"),
            ],
        )

    def test_a_line_gives_up_the_note_then_the_subject(self):
        vote = eva.magi_votes(Session(), None)[0]
        full = [part for part, _ in eva.magi_line(vote, 80)]
        self.assertEqual(full, ["unit", "subject", "verdict", "note"])
        self.assertNotIn("note", [part for part, _ in eva.magi_line(vote, 24)])
        self.assertEqual([part for part, _ in eva.magi_line(vote, 10)], ["unit", "verdict"])


@unittest.skipIf(not HAS_TEXTUAL, "Textual is not installed (run under .venv: make test-tui)")
class MotionTest(unittest.IsolatedAsyncioTestCase):
    """The app with every transition on: it settles to the same screens."""

    async def test_transitions_run_and_settle(self):
        from agent_sessions.tui.app import HelpScreen, MainScreen, SessionsApp, TranscriptScreen

        sessions = default_sessions()
        sessions.append(make_session("claude", 70, foreign=True, offline=True, title="Lost one"))
        engine = FakeEngine(sessions)
        app = SessionsApp(engine=engine)
        app.motion = True
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            main = app.screen
            self.assertIsInstance(main, MainScreen)
            await asyncio.sleep(0.5)  # the first wipe ends
            self.assertIsNone(main.session_list._reveal)
            await pilot.press("2")  # a tab switch: flash and wipe
            await pilot.pause(0.05)
            self.assertIsNotNone(main.session_list._reveal)
            await asyncio.sleep(0.5)
            self.assertIsNone(main.session_list._reveal)
            await pilot.press("1", "j", "j", "k")  # afterglow of the selection
            await asyncio.sleep(0.2)
            await pilot.press("question_mark")  # flicker in
            await asyncio.sleep(0.3)
            self.assertIsInstance(app.screen, HelpScreen)
            help_box = app.screen.query_one("#help")
            self.assertFalse(
                help_box.has_class("-flicker-off") or help_box.has_class("-flicker-dim")
            )
            await pilot.press("escape")
            await pilot.press("v")
            await asyncio.sleep(0.3)
            self.assertIsInstance(app.screen, TranscriptScreen)
            await pilot.press("q")
            await settle(pilot)
            self.assertIsInstance(app.screen, MainScreen)
            self.assertEqual(engine.called("open_session"), [])


if __name__ == "__main__":
    unittest.main()
