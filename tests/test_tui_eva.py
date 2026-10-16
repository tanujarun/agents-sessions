"""The Evangelion presentation layer (``tui/eva.py``), and the app with its
transitions switched on."""

import asyncio
import importlib.util
import random
import time
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest import mock

from support import FIXTURES  # noqa: F401  (points state, cache and home at temp dirs)
from tui_support import FakeEngine, default_sessions, make_session, screen_text, settle

from agent_sessions.tui import eva, view

if importlib.util.find_spec("textual") is not None:
    from agent_sessions.tui import crt

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
        # The list draws the signal mark into the gutter after the selection mark.
        self.assertEqual(len(eva.gutter_text(0, True)) + 1, eva.GUTTER)

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

    def test_signal_marks_and_static(self):
        self.assertEqual(eva.signal_mark("internal"), "▌")
        self.assertEqual(eva.signal_mark("lost"), "░")
        rng = random.Random(1)  # noqa: S311
        self.assertTrue(all(eva.signal_mark("lost", rng) in eva.STATIC for _ in range(20)))
        self.assertEqual(eva.signal_mark("external", rng), "▌")  # only static moves

    def test_seven_segment_numerals(self):
        lines = eva.segments("012:34")
        self.assertEqual(len({len(line) for line in lines}), 1)  # one width
        self.assertEqual(lines[0][:3], "┏━┓")
        self.assertIn("▪", lines[1])
        self.assertEqual(eva.clock_text((3, 4, 5)), "003:04:05")
        self.assertEqual(eva.clock_text(None).replace(":", "").strip(), "")
        rolled = eva.rolled("003:04:05", random.Random(0))  # noqa: S311
        self.assertEqual([i for i, c in enumerate(rolled) if c == ":"], [3, 6])

    def test_elapsed(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        self.assertEqual(eva.elapsed("2026-10-10T09:30:00Z", now), (2, 2, 30))
        self.assertEqual(eva.elapsed("2026-10-13T00:00:00Z", now), (0, 0, 0))  # future
        self.assertIsNone(eva.elapsed("not a time", now))
        self.assertEqual(eva.elapsed("1990-01-01T00:00:00Z", now)[0], 999)

    def test_activity_and_trace(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        stamps = ["2026-10-12T01:00:00Z", "2026-10-12T02:00:00Z", "2026-10-10T01:00:00Z", None]
        counts = eva.activity(stamps, now, days=4)
        self.assertEqual(counts, [0, 1, 0, 2])  # oldest first, today last
        self.assertEqual(eva.trace(counts), "·▄·█")
        self.assertEqual(eva.trace([0, 0]), "··")

    def test_leader_runs_out_along_and_in(self):
        lines = [eva.leader_cell(y, 4, 1, 3) for y in range(6)]
        self.assertEqual(lines, ["   ", " ┏━", " ┃ ", " ┃ ", "━┛ ", "   "])
        self.assertEqual(eva.leader_cell(2, 2, 2, 3), "━━━")
        self.assertEqual(eva.leader_cell(0, 0, 2, 3), "━┓ ")

    def test_subject_column(self):
        tabs = [
            view.Tab("claude", "Claude", 114, "1"),
            view.Tab("antigravity", "Antigravity", 4, "2"),
            view.Tab(view.ALL, "All", 118, "0"),
        ]
        lines = eva.subject_lines(tabs, 0, {"claude": "▁▂▃"}, eva.SUBJECT_WIDTH)
        texts = ["".join(text for _, text in line.parts) for line in lines]
        self.assertTrue(texts[0].startswith("┏") and texts[0].endswith("┓"))
        frame = [t for t in texts if t and t[0] in "┏┃┗"]
        self.assertEqual({len(t) for t in frame}, {eva.SUBJECT_WIDTH})  # a closed plate
        self.assertIn("CLAUDE", texts[1])
        self.assertTrue(any("02 ANTIGRAVITY" in t and t.rstrip().endswith("004") for t in texts))
        self.assertTrue(any("00 ALL" in t for t in texts))
        self.assertEqual({line.tab for line in lines if "CLAUDE" in str(line.parts)}, {0})
        self.assertIn("SIGNAL LOST", texts[-1])

    def test_state_plate_fills_its_line(self):
        kanji, english, slashes = eva.plate_parts("lost", 40)
        self.assertEqual((kanji.strip(), english.strip()), ("信号途絶", "SIGNAL LOST"))
        self.assertEqual(view.text_width(kanji + english + slashes), 40)
        self.assertEqual(eva.plate_parts("nonsense", 20)[1].strip(), "STANDBY")

    def test_magi_shape_and_resolution(self):
        votes = eva.magi_votes(Session(origin="foreign"), None)
        shape = eva.magi_shape(votes, 44)
        texts = ["".join(text for _, text in line) for line in shape]
        self.assertEqual(len(shape), eva.MAGI_SHAPE_HEIGHT)
        self.assertLessEqual(max(view.text_width(t) for t in texts), 44)
        self.assertIn("BALTHASAR·2", texts[1])
        self.assertIn("CASPER·3", texts[7])
        self.assertIn("MELCHIOR·1", texts[7])
        self.assertIn("MAGI", texts[7])
        self.assertEqual(eva.resolution(votes), (eva.PENDING, "pending"))  # import pending
        approve = eva.Vote("A", "X", eva.APPROVE, "approve")
        reject = eva.Vote("B", "Y", eva.REJECT, "reject")
        none = eva.Vote("C", "Z", eva.NOT_APPLICABLE, "none")
        self.assertEqual(eva.resolution([approve, approve, reject])[0], eva.PASSED)
        self.assertEqual(eva.resolution([approve, reject, none])[0], eva.REJECTED)
        self.assertEqual(eva.deliberating(approve).verdict, eva.PENDING)
        self.assertEqual(eva.deliberating(none), none)

    def test_boot_log_waits_for_the_index_then_reports_it(self):
        waiting = eva.boot_lines("1.0", None)
        self.assertEqual(waiting[-1][0], "wait")
        done = eva.boot_lines("1.0", eva.BootFacts(3, 2, 1, (("Claude", 3),)))
        text = "\n".join(line for _, line in done)
        self.assertIn("Ver.1.0", text)
        self.assertIn("SUBJECT 01 CLAUDE", text)
        self.assertEqual(done[-1][0], "done")
        self.assertIn(("lost", next(t for s, t in done if "SIGNAL LOST" in t)), done)

    def test_the_dark_beat_of_a_hazard_band(self):
        left, label, right = eva.ticks_parts(40, eva.EMERGENCY_LABEL)
        self.assertEqual(view.text_width(left + label + right), 40)
        self.assertIn("▮", left)
        self.assertIn("EMERGENCY", label)


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


class PanelsTest(unittest.TestCase):
    """The panels round the log: track, meter, cells, drives, related,
    sync graph, communication log."""

    NOW = datetime(2026, 10, 16, 22, 0, tzinfo=UTC)

    def lifespan(self, started, updated, messages=5):
        return row(started=started, stamp=updated, messages=messages)

    def test_the_track_is_a_log_scale_of_age(self):
        rows = [
            self.lifespan("2026-08-14T10:00:00Z", "2026-08-20T10:00:00Z"),
            self.lifespan("2026-10-16T08:00:00Z", "2026-10-16T21:00:00Z"),
        ]
        span = eva.track_span(rows, self.NOW)
        ruler = eva.track_ruler(span, 70)
        self.assertTrue(ruler.endswith("NOW"))
        # Marks run oldest to newest, and the last hours get room of their own.
        found = [label for label, _ in eva.TRACK_MARKS if label in ruler.split()]
        self.assertEqual(found, ["1H", "6H", "1D", "3D", "1W", "2W", "1MO", "2MO"])
        self.assertGreater(
            ruler.index("1H") - ruler.index("6H"), ruler.index("1MO") - ruler.index("2MO")
        )
        recent = "".join(text for _, text in eva.track_cells(rows[1], span, 70))
        self.assertEqual(len(recent), 70)
        self.assertTrue(recent.rstrip().endswith("▮"))
        self.assertIn("━", recent)
        floor = eva.track_span(
            [self.lifespan("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z")], self.NOW
        )
        self.assertEqual((self.NOW - floor.start).days, eva.TRACK_MAX_DAYS)
        old = eva.track_cells(
            self.lifespan("2020-01-01T00:00:00Z", "2026-10-16T00:00:00Z"), floor, 30
        )
        self.assertEqual(old[0][1][0], "◂")  # it began before the track does

    def test_the_meter_counts_in_units(self):
        self.assertEqual(eva.meter(None, 100), ("", "▯" * eva.METER_UNITS))
        lit, unlit = eva.meter(100, 100)
        self.assertEqual((len(lit), unlit), (eva.METER_UNITS, ""))
        self.assertLess(len(eva.meter(3, 100)[0]), len(eva.meter(40, 100)[0]))
        self.assertEqual(eva.meter_count(None), "  --")
        self.assertEqual(eva.meter_count(12345), " 12k")

    def test_activity_cells(self):
        stamps = ["2026-10-16T01:00:00Z", "2026-10-16T02:00:00Z", "2026-10-12T05:00:00Z", None]
        grid = eva.activity_grid(stamps, self.NOW, weeks=3)  # 2026-10-16 is a Friday
        self.assertEqual(grid[4][2], 2)
        self.assertEqual(grid[0][2], 1)  # Monday the 12th, this week
        self.assertEqual(grid[5][2], -1)  # Saturday has not come
        lines = eva.activity_lines(grid, 24, "CLAUDE")
        texts = ["".join(text for _, text in line) for line in lines]
        self.assertEqual(len(lines), 9)
        self.assertIn("CLAUDE · 3W · 003", texts[1])
        self.assertIn("cell-4", [kind for kind, _ in lines[2 + 4]])

    def test_drive_board(self):
        drives = [
            eva.Drive("Linux", "btrfs", "/dev/x", "", "mounted", 145),
            eva.Drive("No Other Way", "ntfs3", "", "D:", "absent", 29),
        ]
        lines = eva.drive_lines(drives, 24)
        texts = ["".join(text for _, text in line) for line in lines]
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(view.text_width(text) <= 24 for text in texts))
        self.assertIn("D-01 LINUX", texts[1])
        self.assertIn("ONLINE", texts[1])
        self.assertIn("信号途絶", texts[3])
        self.assertIn("ntfs3 · D:", texts[4])
        self.assertTrue(texts[4].endswith("029 SES"))

    def test_related_sessions(self):
        related = [eva.Related("02", "Codex", f"title {n}", "3d") for n in range(7)]
        lines = eva.related_lines(related, "/home/u", 44, 5)
        texts = ["".join(text for _, text in line) for line in lines]
        self.assertIn("· 07", texts[0])
        self.assertEqual(len(lines), 7)  # heading, five, "+2 MORE"
        self.assertIn("+2 MORE", texts[-1])
        self.assertTrue(all(view.text_width(text) <= 44 for text in texts))
        self.assertIn(
            "NONE IN /home/u", "".join(t for _, t in eva.related_lines([], "/home/u", 44, 5)[1])
        )

    def test_sync_graph_and_communication_log(self):
        beats = tuple(
            ("user" if n % 4 == 0 else "assistant", f"2026-10-16T20:{n:02d}:00Z") for n in range(40)
        )
        preview = eva.Preview(
            turns=(
                ("user", "fix the bug", "2026-10-16T20:00:00Z"),
                ("tool", "Bash", "2026-10-16T20:01:00Z"),
                ("assistant", "Done", "2026-10-16T20:02:00Z"),
            ),
            beats=beats,
            total=40,
        )
        graph = eva.sync_lines(preview, 44)
        self.assertEqual(len(graph), eva.SYNC_ROWS + 2)
        self.assertIn("40 TURNS · 39M", "".join(t for _, t in graph[0]))
        self.assertIn("▲", "".join(t for _, t in graph[-1]))
        log = ["".join(t for _, t in line) for line in eva.comm_lines(preview, 40, 10, UTC)]
        self.assertEqual(len(log), 3)  # heading and the two things said: no tool turns
        self.assertIn("▐USER▌ 20:00 fix the bug", log[1])
        self.assertIn("▐AI  ▌", log[2])
        self.assertIn("RECEIVING", "".join(t for _, t in eva.comm_lines(None, 40, 10)[1]))
        failed = eva.Preview((), (), 0, error="the drive is gone")
        self.assertIn("信号途絶", "".join(t for _, t in eva.comm_lines(failed, 40, 10)[1]))
        short = eva.comm_lines(preview, 40, 2)
        self.assertEqual(len(short), 2)  # the newest that fit
        self.assertIn(
            "NO TIMING", "".join(t for _, t in eva.sync_lines(eva.Preview((), (), 0), 30)[1])
        )


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
        async with app.run_test(size=(160, 45)) as pilot:
            main = app.screen
            self.assertIsInstance(main, MainScreen)
            self.assertTrue(main.query("#boot"))  # the boot log prints first
            await until(lambda: not main.query("#boot"), 3.0)
            await until(lambda: main.raster.kind is None, 2.0)  # then the screen sweeps in
            self.assertIn("SESSION LOG", screen_text(app))
            # A tab switch sweeps a raster band down the screen. (The pilot's
            # press returns once the screen is idle, so the band is caught by
            # the strips drawn while it ran.)
            painted: list[str] = []
            original = crt._painted

            def spy(raster, strip, old, row, width):
                result = original(raster, strip, old, row, width)
                painted.append(result.text)
                return result

            with mock.patch.object(crt, "_painted", spy):
                before = time.monotonic()
                await pilot.press("2")
                await until(lambda: main.raster.kind is None, 2.0)
            self.assertGreater(main.raster.started, before)
            self.assertTrue(any(char in line for line in painted for char in "▓▚▞"))
            self.assertFalse(any(char in screen_text(app) for char in "▓▚▞"))
            with mock.patch.object(main.raster, "type_in", wraps=main.raster.type_in) as typed:
                await pilot.press("1", "j", "j", "k")  # a new target types itself in
            self.assertGreaterEqual(typed.call_count, 3)
            await until(lambda: not main.raster.typing, 2.0)
            self.assertIn("SESSION", screen_text(app))
            await pilot.press("question_mark")  # interlaced in
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpScreen)
            await until(lambda: app.screen.raster.kind is None, 2.0)
            await pilot.press("escape")
            await pilot.press("v")
            await asyncio.sleep(0.3)
            self.assertIsInstance(app.screen, TranscriptScreen)
            await pilot.press("q")
            await settle(pilot)
            self.assertIsInstance(app.screen, MainScreen)
            self.assertEqual(engine.called("open_session"), [])

    async def test_a_key_during_the_boot_log_is_not_lost(self):
        from agent_sessions.tui.app import SessionsApp

        app = SessionsApp(engine=FakeEngine())
        app.motion = True
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await settle(pilot)
            self.assertFalse(app.screen.query("#boot"))  # a key takes it away
            self.assertEqual(app.screen.tab_name, "codex")


async def until(condition, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


if __name__ == "__main__":
    unittest.main()
