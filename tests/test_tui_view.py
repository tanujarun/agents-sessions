"""The TUI's pure view logic, its engine wrapper and its entry point. Runs on
the system Python: nothing here needs Textual."""

import subprocess
import sys
import unittest
from datetime import UTC, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

import tui_support as support  # noqa: E402

from agent_sessions.tui import engine as engine_mod  # noqa: E402
from agent_sessions.tui import view  # noqa: E402

NOW = support.NOW
PROVIDERS = support.PROVIDERS


def ago(**delta) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_of(sessions) -> list:
    index = support.FakeIndex()
    return [view.make_row(s, index.where(s)) for s in sessions]


class WidthTest(unittest.TestCase):
    def test_char_width(self):
        self.assertEqual(view.char_width("a"), 1)
        self.assertEqual(view.char_width("é"), 1)
        self.assertEqual(view.char_width("漢"), 2)
        self.assertEqual(view.char_width("😀"), 2)
        self.assertEqual(view.char_width("\u0301"), 0)  # combining acute
        self.assertEqual(view.char_width("\u200d"), 0)  # zero-width joiner
        self.assertEqual(view.char_width("\ufe0f"), 0)  # variation selector
        self.assertEqual(view.char_width("\n"), 0)
        self.assertEqual(view.char_width("\x7f"), 0)

    def test_text_width(self):
        self.assertEqual(view.text_width(""), 0)
        self.assertEqual(view.text_width("hello"), 5)
        self.assertEqual(view.text_width("日本語"), 6)
        self.assertEqual(view.text_width("e\u0301"), 1)
        self.assertEqual(view.text_width("🚀 launch"), 9)

    def test_clip_ascii(self):
        self.assertEqual(view.clip("abc", 3), "abc")
        self.assertEqual(view.clip("hello world", 8), "hello w…")
        self.assertEqual(view.clip("abcd", 1), "…")
        self.assertEqual(view.clip("abc", 0), "")
        self.assertEqual(view.clip("abc", -2), "")

    def test_clip_wide(self):
        self.assertEqual(view.clip("日本語テキスト", 14), "日本語テキスト")
        self.assertEqual(view.clip("日本語テキスト", 7), "日本語…")
        # A wide character that would straddle the limit is dropped whole.
        self.assertEqual(view.clip("日本語テキスト", 6), "日本…")
        self.assertEqual(view.clip("🚀🚀🚀🚀", 5), "🚀🚀…")
        for width in range(1, 16):
            for text in (
                "日本語テキスト",
                "a漢b字c",
                "🚀 ship it 🚀",
                "e\u0301e\u0301e\u0301e\u0301",
            ):
                self.assertLessEqual(view.text_width(view.clip(text, width)), width)

    def test_pad_is_exact(self):
        self.assertEqual(view.pad("ab", 5), "ab   ")
        self.assertEqual(view.pad("ab", 5, "right"), "   ab")
        self.assertEqual(view.pad("日本", 6), "日本  ")
        self.assertEqual(view.pad("日本語テキスト", 6), "日本… ")
        for width in range(0, 12):
            for text in ("", "plain", "日本語テキスト", "🚀🚀🚀", "mixé 漢 🚀"):
                self.assertEqual(view.text_width(view.pad(text, width)), width)

    def test_clean(self):
        self.assertEqual(view.clean("a\nb\tc\x1b[0m"), "a b c [0m")
        self.assertEqual(view.clean(None), "")
        self.assertEqual(view.clean("日本 ok"), "日本 ok")
        self.assertEqual(view.clean(12), "12")


class AgeTest(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (dict(seconds=0), "now"),
            (dict(seconds=59), "now"),
            (dict(seconds=60), "1m"),
            (dict(minutes=5), "5m"),
            (dict(seconds=3599), "59m"),
            (dict(seconds=3600), "1h"),
            (dict(hours=3), "3h"),
            (dict(seconds=86399), "23h"),
            (dict(days=1), "1d"),
            (dict(days=2), "2d"),
            (dict(days=13), "13d"),
            (dict(days=14), "2w"),
            (dict(days=21), "3w"),
            (dict(days=59), "8w"),
            (dict(days=60), "2mo"),
            (dict(days=364), "12mo"),
            (dict(days=365), "1y"),
            (dict(days=800), "2y"),
        ]
        for delta, expected in cases:
            self.assertEqual(view.age_text(ago(**delta), NOW), expected, delta)

    def test_odd_stamps(self):
        self.assertEqual(view.age_text(None, NOW), "-")
        self.assertEqual(view.age_text("", NOW), "-")
        self.assertEqual(view.age_text("yesterday", NOW), "-")
        self.assertEqual(view.age_text(12345, NOW), "-")
        self.assertEqual(view.age_text(ago(hours=-5), NOW), "now")  # clock skew: the future
        self.assertEqual(view.age_text("2026-10-12T11:00:00", NOW), "1h")  # naive = UTC
        self.assertEqual(view.age_text("2026-10-12T13:00:00+02:00", NOW), "1h")

    def test_ages_fit_the_column(self):
        for days in (0, 1, 13, 14, 59, 60, 300, 364, 365, 20000):
            self.assertLessEqual(len(view.age_text(ago(days=days), NOW)), view.AGE_WIDTH + 1)
        self.assertLessEqual(len(view.age_text(ago(days=364), NOW)), view.AGE_WIDTH)

    def test_local_time(self):
        tz = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(view.local_time("2026-10-10T12:28:00Z", tz), "2026-10-10 17:58")
        self.assertEqual(view.local_time("2026-10-10T12:28:00Z", UTC), "2026-10-10 12:28")
        self.assertEqual(view.local_time(None), "-")
        self.assertEqual(view.local_time("nope"), "-")


class RowTest(unittest.TestCase):
    def test_project_name(self):
        self.assertEqual(view.project_name("/home/user/code/thing"), "thing")
        self.assertEqual(view.project_name("/home/user/code/thing/"), "thing")
        self.assertEqual(view.project_name("F:\\AI Projects\\Website Works"), "Website Works")
        self.assertEqual(view.project_name("C:/mixed\\sep/dir"), "dir")
        self.assertEqual(view.project_name(None), "-")
        self.assertEqual(view.project_name(""), "-")
        self.assertEqual(view.project_name("/"), "-")

    def test_make_row_native(self):
        session = support.make_session("claude", 3, title="Fix  the\nlag")
        row = view.make_row(session, "Linux")
        self.assertEqual(row.key, session.key)
        self.assertEqual(row.title, "Fix  the lag")
        self.assertEqual(row.project, "project-3")
        self.assertEqual(row.where, "Linux")
        self.assertFalse(row.foreign)
        self.assertFalse(row.imported)
        self.assertEqual(row.stamp, session.updated)

    def test_make_row_foreign_and_fallbacks(self):
        session = support.make_session(
            "codex", 1, foreign=True, title="", imported_as="codex:aaaaaaaa:x"
        )
        session.updated = None
        row = view.make_row(session, "C: · imported")
        self.assertEqual(row.title, "-")
        self.assertEqual(row.project, "project-1")
        self.assertTrue(row.foreign)
        self.assertTrue(row.imported)
        self.assertEqual(row.stamp, session.started)
        session.cwd = None
        self.assertEqual(view.make_row(session, "C:").project, "-")

    def test_filter_matches_title_project_and_cwd(self):
        session = support.make_session(
            "claude", 1, title="Portfolio Hero", cwd="/home/user/Website Works/landing"
        )
        row = view.make_row(session, "Linux")
        self.assertTrue(view.matches(row, ""))
        self.assertTrue(view.matches(row, "   "))
        self.assertTrue(view.matches(row, "portfolio"))
        self.assertTrue(view.matches(row, "HERO"))
        self.assertTrue(view.matches(row, "landing"))  # project
        self.assertTrue(view.matches(row, "website works"))  # cwd
        self.assertTrue(view.matches(row, " hero "))
        self.assertFalse(view.matches(row, "codex"))
        self.assertFalse(view.matches(row, session.id))  # id is not searched

    def test_filter_rows(self):
        rows = rows_of(support.default_sessions())
        self.assertEqual(len(view.filter_rows(rows)), len(rows))
        self.assertEqual(len(view.filter_rows(rows, view.ALL)), len(rows))
        claude = view.filter_rows(rows, "claude")
        self.assertEqual(len(claude), 20)
        self.assertTrue(all(row.provider == "claude" for row in claude))
        self.assertEqual(view.filter_rows(rows, "gemini"), [])
        hits = view.filter_rows(rows, "claude", "session 1")
        self.assertEqual(
            [row.title for row in hits],
            ["claude session 1"] + [f"claude session {n}" for n in range(10, 18)],
        )
        # Order is the order given.
        self.assertEqual(
            [row.key for row in claude], [row.key for row in rows if row.provider == "claude"]
        )


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.rows = rows_of(support.make_session("claude", n) for n in range(6))

    def test_same_key_after_reorder(self):
        key = self.rows[2].key
        shuffled = [self.rows[i] for i in (5, 4, 2, 0, 1, 3)]
        self.assertEqual(view.select_index(shuffled, key, 2), 2)
        shuffled = [self.rows[i] for i in (2, 5, 4, 0, 1, 3)]
        self.assertEqual(view.select_index(shuffled, key, 2), 0)
        self.assertEqual(view.select_index(list(reversed(self.rows)), key, 2), 3)

    def test_removed_key_keeps_the_position(self):
        key = self.rows[2].key
        without = [row for row in self.rows if row.key != key]
        self.assertEqual(view.select_index(without, key, 2), 2)
        self.assertEqual(view.select_index(without[:2], key, 2), 1)  # clamped
        last = self.rows[5].key
        self.assertEqual(view.select_index(self.rows[:5], last, 5), 4)

    def test_edges(self):
        self.assertEqual(view.select_index([], "x", 3), -1)
        self.assertEqual(view.select_index(self.rows, None, 0), 0)
        self.assertEqual(view.select_index(self.rows, None, -1), 0)
        self.assertEqual(view.select_index(self.rows, "nope", 99), 5)

    def test_move_index(self):
        self.assertEqual(view.move_index(0, 10, "down"), 1)
        self.assertEqual(view.move_index(9, 10, "down"), 9)
        self.assertEqual(view.move_index(0, 10, "up"), 0)
        self.assertEqual(view.move_index(5, 10, "up"), 4)
        self.assertEqual(view.move_index(2, 100, "pagedown", 20), 22)
        self.assertEqual(view.move_index(95, 100, "pagedown", 20), 99)
        self.assertEqual(view.move_index(5, 100, "pageup", 20), 0)
        self.assertEqual(view.move_index(50, 100, "home"), 0)
        self.assertEqual(view.move_index(50, 100, "end"), 99)
        self.assertEqual(view.move_index(3, 10, "pagedown", 0), 4)  # page of at least 1
        self.assertEqual(view.move_index(3, 0, "down"), -1)
        self.assertEqual(view.move_index(-1, 4, "down"), 0)
        self.assertEqual(view.move_index(3, 10, "sideways"), 3)

    def test_scroll_for(self):
        self.assertEqual(view.scroll_for(0, 0, 10, 100), 0)
        self.assertEqual(view.scroll_for(9, 0, 10, 100), 0)
        self.assertEqual(view.scroll_for(10, 0, 10, 100), 1)
        self.assertEqual(view.scroll_for(99, 0, 10, 100), 90)
        self.assertEqual(view.scroll_for(3, 20, 10, 100), 3)
        self.assertEqual(view.scroll_for(25, 20, 10, 100), 20)
        self.assertEqual(view.scroll_for(2, 50, 10, 5), 0)  # list shrank under the view
        self.assertEqual(view.scroll_for(4, 0, 0, 5), 0)
        self.assertEqual(view.scroll_for(-1, 3, 10, 0), 0)


class TabTest(unittest.TestCase):
    def setUp(self):
        self.rows = rows_of(support.default_sessions())

    def test_order_and_counts(self):
        tabs = view.build_tabs(PROVIDERS, self.rows)  # the registry gives them by name
        self.assertEqual(
            [tab.name for tab in tabs], ["claude", "codex", "grok", "antigravity", "gemini", "all"]
        )
        self.assertEqual(
            [tab.label for tab in tabs], ["Claude", "Codex", "Grok", "Antigravity", "Gemini", "All"]
        )
        self.assertEqual([tab.count for tab in tabs], [20, 7, 5, 2, 0, 34])
        self.assertEqual([tab.digit for tab in tabs], ["1", "2", "3", "4", "5", "0"])

    def test_unknown_providers_follow(self):
        providers = [("zed", "Zed"), ("codex", "Codex"), ("aider", "Aider"), ("claude", "Claude")]
        tabs = view.build_tabs(providers, [])
        self.assertEqual([tab.name for tab in tabs], ["claude", "codex", "zed", "aider", "all"])
        self.assertEqual([tab.digit for tab in tabs], ["1", "2", "3", "4", "0"])
        self.assertEqual([tab.count for tab in tabs], [0, 0, 0, 0, 0])

    def test_many_providers_run_out_of_digits(self):
        providers = [(f"p{n:02d}", f"P{n}") for n in range(11)]
        tabs = view.build_tabs(providers, [])
        self.assertEqual(
            [tab.digit for tab in tabs], [str(n) for n in range(1, 10)] + ["", "", "0"]
        )

    def test_no_providers(self):
        tabs = view.build_tabs([], self.rows)
        self.assertEqual([(tab.name, tab.count) for tab in tabs], [("all", 34)])

    def test_digits_and_cycling(self):
        tabs = view.build_tabs(PROVIDERS, self.rows)
        self.assertEqual(view.tab_for_digit(tabs, "1"), 0)
        self.assertEqual(view.tab_for_digit(tabs, "5"), 4)
        self.assertEqual(view.tab_for_digit(tabs, "0"), 5)
        self.assertIsNone(view.tab_for_digit(tabs, "6"))
        self.assertIsNone(view.tab_for_digit(tabs, ""))
        self.assertEqual(view.cycle_tab(0, 1, 6), 1)
        self.assertEqual(view.cycle_tab(5, 1, 6), 0)
        self.assertEqual(view.cycle_tab(0, -1, 6), 5)
        self.assertEqual(view.cycle_tab(0, 1, 0), 0)

    def test_labels_shrink_to_fit(self):
        tabs = view.build_tabs(PROVIDERS, self.rows)
        full = view.fit_tab_labels(tabs, 0, 120, extra=2)
        self.assertEqual(
            full,
            ["1 Claude 20", "2 Codex 7", "3 Grok 5", "4 Antigravity 2", "5 Gemini 0", "0 All 34"],
        )
        fits = sum(len(label) + 2 for label in full)
        self.assertEqual(view.fit_tab_labels(tabs, 0, fits, extra=2), full)
        no_counts = view.fit_tab_labels(tabs, 3, fits - 1, extra=2)
        self.assertEqual(
            no_counts, ["1 Claude", "2 Codex", "3 Grok", "4 Antigravity 2", "5 Gemini", "0 All"]
        )
        short = view.fit_tab_labels(tabs, 3, 52, extra=2)
        self.assertEqual(short, ["1 Cla", "2 Cod", "3 Gro", "4 Antigravity 2", "5 Gem", "0 All"])
        digits = view.fit_tab_labels(tabs, 3, 40, extra=2)
        self.assertEqual(digits, ["1", "2", "3", "4 Antigravity 2", "5", "0"])
        self.assertLessEqual(sum(len(label) + 2 for label in digits), 40)
        bare = view.fit_tab_labels(tabs, 3, 31, extra=2)
        self.assertEqual(bare, ["1", "2", "3", "4 Antigravity", "5", "0"])
        # Nothing smaller exists: the last level comes back even when it overflows.
        self.assertEqual(view.fit_tab_labels(tabs, 3, 5, extra=2), bare)
        self.assertEqual(view.fit_tab_labels([], 0, 80), [])


class ColumnTest(unittest.TestCase):
    def setUp(self):
        self.rows = rows_of(support.default_sessions())
        self.widths = view.column_widths(self.rows, dict(PROVIDERS))

    def names(self, columns):
        return [column.name for column in columns]

    def total(self, columns, cell_pad):
        return sum(column.width + cell_pad for column in columns)

    def test_natural_widths(self):
        self.assertEqual(self.widths, {"project": 10, "where": 13, "provider": 11})
        self.assertEqual(view.column_widths([]), {"project": 7, "where": 5, "provider": 8})
        long = support.make_session("claude", 1, cwd="/x/" + "p" * 60)
        self.assertEqual(view.column_widths(rows_of([long]))["project"], view.MAX_SIDE_WIDTH)

    def test_wide_terminal(self):
        columns = view.fit_columns(120, self.widths, cell_pad=2)
        self.assertEqual(self.names(columns), ["title", "project", "age", "where"])
        self.assertEqual(self.total(columns, 2), 120)
        by_name = {column.name: column.width for column in columns}
        self.assertEqual((by_name["project"], by_name["age"], by_name["where"]), (10, 4, 13))
        self.assertEqual(by_name["title"], 120 - 10 - 4 - 13 - 8)

    def test_all_tab_adds_provider(self):
        columns = view.fit_columns(120, self.widths, show_provider=True, cell_pad=2)
        self.assertEqual(self.names(columns), ["title", "provider", "project", "age", "where"])
        self.assertEqual(self.total(columns, 2), 120)

    def test_project_goes_first(self):
        columns = view.fit_columns(79, self.widths, show_project=False, cell_pad=2)
        self.assertEqual(self.names(columns), ["title", "age", "where"])
        self.assertEqual(self.total(columns, 2), 79)

    def test_project_is_capped_at_a_quarter(self):
        wide = dict(self.widths, project=24)
        columns = view.fit_columns(80, wide, cell_pad=2)
        self.assertEqual({c.name: c.width for c in columns}["project"], 20)

    def test_drop_order_when_title_starves(self):
        kwargs = dict(show_provider=True, show_project=True, cell_pad=2)
        self.assertEqual(
            self.names(view.fit_columns(70, self.widths, **kwargs)),
            ["title", "provider", "project", "age", "where"],
        )
        self.assertEqual(
            self.names(view.fit_columns(60, self.widths, **kwargs)),
            ["title", "provider", "age", "where"],
        )
        self.assertEqual(
            self.names(view.fit_columns(40, self.widths, **kwargs)), ["title", "provider", "age"]
        )
        self.assertEqual(self.names(view.fit_columns(30, self.widths, **kwargs)), ["title", "age"])
        self.assertEqual(self.names(view.fit_columns(20, self.widths, **kwargs)), ["title"])

    def test_title_is_never_dropped(self):
        for width in range(0, 30):
            columns = view.fit_columns(width, self.widths, show_provider=True, cell_pad=2)
            self.assertEqual(columns[0].name, "title")
            self.assertGreaterEqual(columns[0].width, 1)
        for width in range(20, 200):
            for provider in (False, True):
                columns = view.fit_columns(
                    width, self.widths, show_provider=provider, show_project=width >= 80, cell_pad=2
                )
                self.assertEqual(self.total(columns, 2), width, (width, provider))

    def test_rows_and_header_are_exactly_as_wide_as_the_list(self):
        sessions = [
            support.make_session("claude", 1, title="日本語のタイトルがとても長い場合のテスト" * 3),
            support.make_session("claude", 2, title="🚀 ship it 🚀 " * 12),
            support.make_session("claude", 3, title="short"),
            support.make_session(
                "claude",
                4,
                title="e\u0301" * 90,
                cwd="/x/プロジェクト名プロジェクト名プロジェクト名",
            ),
            support.make_session("codex", 5, foreign=True, title="x" * 300, imported_as="k"),
        ]
        rows = rows_of(sessions)
        widths = view.column_widths(rows, dict(PROVIDERS))
        for width in (120, 100, 80, 79, 60, 41, 40, 25):
            for provider in (False, True):
                columns = view.fit_columns(
                    width, widths, show_provider=provider, show_project=width >= 80, cell_pad=2
                )
                header = view.format_header(columns, 1, 1)
                self.assertEqual(view.text_width(header), width)
                for row in rows:
                    line = view.format_row(row, columns, NOW, dict(PROVIDERS), 1, 1)
                    self.assertEqual(view.text_width(line), width, (width, row.title[:8]))

    def test_row_text(self):
        session = support.make_session("claude", 7, title="Fix the first-open lag", age=2 * 1440)
        row = view.make_row(session, "Linux")
        columns = [
            view.Column("title", "TITLE", 12),
            view.Column("provider", "PROVIDER", 8),
            view.Column("project", "PROJECT", 9),
            view.Column("age", "AGE", 4),
            view.Column("where", "WHERE", 5),
        ]
        self.assertEqual(
            view.format_header(columns, 1, 1), " TITLE         PROVIDER  PROJECT    AGE   WHERE "
        )
        self.assertEqual(
            view.format_row(row, columns, NOW, {"claude": "Claude"}, 1, 1),
            " Fix the fir…  Claude    project-7  2d    Linux ",
        )
        self.assertEqual(
            view.format_row(row, columns, NOW), "Fix the fir…claude  project-72d  Linux"
        )


class LayoutTest(unittest.TestCase):
    def test_sizes(self):
        big = view.layout_for(120, 40)
        self.assertTrue(big.show_project and big.show_detail)
        self.assertEqual(big.hints, view.HINTS_FULL)
        standard = view.layout_for(80, 24, hint_gutter=2)
        self.assertTrue(standard.show_project and standard.show_detail)
        self.assertEqual(standard.hints, view.HINTS_FULL)
        narrow = view.layout_for(79, 24, hint_gutter=2)
        self.assertFalse(narrow.show_project)  # PROJECT goes first
        self.assertTrue(narrow.show_detail)
        tiny = view.layout_for(40, 10, hint_gutter=2)
        self.assertFalse(tiny.show_project or tiny.show_detail)  # then the detail pane
        self.assertEqual(tiny.hints, ("Enter resume  / filter  ? help  q quit",))

    def test_detail_pane_thresholds(self):
        self.assertTrue(view.layout_for(60, 20).show_detail)
        self.assertFalse(view.layout_for(59, 20).show_detail)
        self.assertFalse(view.layout_for(60, 19).show_detail)
        self.assertFalse(view.layout_for(200, 12).show_detail)
        self.assertTrue(view.layout_for(200, 12).show_project)

    def test_hints_always_fit_or_are_the_shortest(self):
        for width in range(0, 130):
            for roomy in (True, False):
                lines = view.hint_lines(width, roomy)
                if lines != (view.HINTS_SHORT[-1],):
                    self.assertTrue(all(view.text_width(line) <= width for line in lines))
                if not roomy:
                    self.assertEqual(len(lines), 1)
        self.assertEqual(view.hint_lines(200, roomy=True), view.HINTS_FULL)
        self.assertEqual(view.hint_lines(60), (view.HINTS_SHORT[0],))
        self.assertEqual(view.hint_lines(3), ("? help  q quit",))


class StatusTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(view.short_count(0), "0")
        self.assertEqual(view.short_count(999), "999")
        self.assertEqual(view.short_count(1000), "1k")
        self.assertEqual(view.short_count(41234), "41k")
        self.assertEqual(view.short_count(1_250_000), "1.2M")
        self.assertEqual(view.plural(1, "drive"), "1 drive")
        self.assertEqual(view.plural(5, "drive"), "5 drives")
        self.assertEqual(view.plural(1500, "session"), "1,500 sessions")

    def test_bytes(self):
        self.assertEqual(view.bytes_text(0), "0 B")
        self.assertEqual(view.bytes_text(812), "812 B")
        self.assertEqual(view.bytes_text(3100), "3.1 kB")
        self.assertEqual(view.bytes_text(12_000_000), "12.0 MB")
        self.assertEqual(view.bytes_text(1_400_000_000), "1.4 GB")
        self.assertEqual(view.bytes_text(5_000_000_000_000), "5000.0 GB")
        self.assertEqual(view.bytes_text(None), "0 B")

    def test_drive_count(self):
        mounts = [
            {"path": "/", "label": "Linux", "mounted": True},
            {"path": "/home", "label": "Linux", "mounted": True},
            {"path": "/m/a", "label": "A", "mounted": True},
            {"path": "/m/b", "label": "", "mounted": True},
            {"path": "/m/c", "label": "C", "mounted": False},
        ]
        self.assertEqual(view.drive_count(mounts), 3)
        self.assertEqual(view.drive_count([]), 0)
        self.assertEqual(view.drive_count(None), 0)

    def test_summary(self):
        self.assertEqual(
            view.summary_variants(5, 63, ago(minutes=2), NOW),
            [
                "5 drives · 63 sessions · scanned 2m ago",
                "63 sessions · scanned 2m ago",
                "63 sessions",
            ],
        )
        self.assertEqual(
            view.summary_variants(1, 1, ago(seconds=3), NOW)[0],
            "1 drive · 1 session · scanned just now",
        )
        self.assertEqual(
            view.summary_variants(0, 0, None, NOW)[0], "0 drives · 0 sessions · never scanned"
        )

    def test_status_parts(self):
        variants = view.summary_variants(5, 63, ago(minutes=2), NOW)
        self.assertEqual(
            view.status_parts("agent-sessions", variants, 120), ("agent-sessions", variants[0])
        )
        self.assertEqual(
            view.status_parts("agent-sessions", variants, 14 + 2 + len(variants[0])),
            ("agent-sessions", variants[0]),
        )
        self.assertEqual(
            view.status_parts("agent-sessions", variants, 14 + 1 + len(variants[0])),
            ("agent-sessions", variants[1]),
        )
        self.assertEqual(
            view.status_parts("agent-sessions", variants, 30), ("agent-sessions", "63 sessions")
        )
        # Nothing fits beside the name: the summary wins over the name...
        self.assertEqual(view.status_parts("agent-sessions", variants, 12), ("", "63 sessions"))
        self.assertEqual(view.status_parts("agent-sessions", variants, 6), ("", "63 se…"))
        # ...but a message wins over the summary.
        message = "the folder /home/user/code/gone no longer exists"
        self.assertEqual(view.status_parts(message, variants, 50, keep_left=True), (message, ""))
        self.assertEqual(
            view.status_parts(message, variants, 20, keep_left=True),
            ("the folder /home/use…"[:19] + "…", ""),
        )
        self.assertEqual(
            view.status_parts(message, variants, 70, keep_left=True), (message, "63 sessions")
        )
        self.assertEqual(view.status_parts("a\nb", [], 10), ("a b", ""))
        for width in range(0, 100):
            left, right = view.status_parts(message, variants, width, gap=0, keep_left=True)
            self.assertLessEqual(view.text_width(left) + view.text_width(right), max(width, 0))

    def test_progress_texts(self):
        self.assertEqual(view.scan_progress_text("Zephyro", 41234), "scanning Zephyro… 41k dirs")
        self.assertEqual(
            view.scan_progress_text("No Other Way", 12), "scanning No Other Way… 12 dirs"
        )
        self.assertEqual(
            view.import_progress_text(3_100_000, 12_000_000), "importing… 3.1 MB of 12.0 MB"
        )
        self.assertEqual(view.import_progress_text(0, 0), "importing… 0 B")

    def test_scan_message(self):
        self.assertEqual(
            view.scan_message(
                {
                    "mode": "quick",
                    "cancelled": False,
                    "seconds": 0.412,
                    "sessions": 135,
                    "warnings": [],
                }
            ),
            "rescan: 135 sessions, 0.4 s",
        )
        self.assertEqual(
            view.scan_message(
                {
                    "mode": "deep",
                    "cancelled": False,
                    "seconds": 3.0,
                    "sessions": 1,
                    "warnings": ["a", "b"],
                }
            ),
            "full rescan: 1 session, 3.0 s, 2 warnings",
        )
        self.assertEqual(
            view.scan_message({"mode": "deep", "cancelled": True}), "full rescan cancelled"
        )
        self.assertEqual(view.scan_message(None), "rescan: 0 sessions")

    def test_error_text(self):
        self.assertEqual(
            view.error_text(support.FakeError("the folder is gone")), "the folder is gone"
        )
        self.assertEqual(view.error_text(ValueError("bad\nvalue")), "bad value")
        self.assertEqual(view.error_text(KeyError()), "KeyError")
        error = RuntimeError("message")
        error.reason = ""
        self.assertEqual(view.error_text(error), "message")


class DetailTest(unittest.TestCase):
    tz = UTC

    def test_native(self):
        session = support.make_session("claude", 2, messages=1214)
        fields = view.detail_fields(session, None, self.tz)
        self.assertEqual(list(fields), list(view.DETAIL_FIELDS))
        self.assertEqual(fields["id"], session.id)
        self.assertEqual(fields["messages"], "1,214")
        self.assertEqual(fields["updated"], "2026-10-12 11:36")
        self.assertEqual(fields["cwd"], "/home/user/code/project-2")
        self.assertEqual(fields["local"], "/home/user/code/project-2   (exists)")
        self.assertEqual(fields["store"], "/home/user/.claude")
        self.assertEqual(fields["note"], "-")
        self.assertEqual(fields["import"], "-")

    def test_foreign_and_missing_values(self):
        session = support.make_session(
            "grok", 4, foreign=True, messages=None, note="not launchable:\nno binary"
        )
        session.cwd_exists = False
        fields = view.detail_fields(session, None, self.tz)
        self.assertEqual(fields["messages"], "-")
        self.assertEqual(fields["cwd"], "C:\\Work\\project-4")
        self.assertEqual(fields["local"], "/run/media/user/WINVOL/Work/project-4   (not found)")
        self.assertEqual(fields["note"], "not launchable: no binary")
        self.assertEqual(fields["import"], "checking…")
        session.cwd_local = None
        session.cwd = None
        session.updated = None
        session.started = None
        fields = view.detail_fields(session, None, self.tz)
        self.assertEqual(
            (fields["cwd"], fields["local"], fields["updated"]), ("-", "(not found)", "-")
        )

    def test_import_verdict(self):
        session = support.make_session("grok", 4, foreign=True)
        self.assertEqual(view.verdict_text(session, None), "checking…")
        self.assertEqual(view.verdict_text(session, (True, "")), "possible")
        self.assertEqual(
            view.verdict_text(session, (True, "cwd is rewritten")), "possible · cwd is rewritten"
        )
        self.assertEqual(
            view.verdict_text(session, (False, "Antigravity cannot be imported")),
            "not possible: Antigravity cannot be imported",
        )
        self.assertEqual(view.verdict_text(session, (False, "")), "not possible")
        self.assertEqual(
            view.verdict_text(session, (None, "engine has no can_import")),
            "unknown: engine has no can_import",
        )
        session.imported_as = "grok:aaaaaaaa:x"
        self.assertEqual(
            view.verdict_text(session, (False, "x")),
            "already imported; Enter resumes the Linux copy",
        )
        self.assertEqual(view.verdict_text(support.make_session("grok", 1), (False, "x")), "-")

    def test_nothing_selected(self):
        self.assertEqual(view.detail_fields(None), dict.fromkeys(view.DETAIL_FIELDS, "-"))


class PromptAndKeyTest(unittest.TestCase):
    def test_prompts(self):
        self.assertEqual(view.drive_of("C: · imported"), "C:")
        self.assertEqual(view.drive_of("Zephyro"), "Zephyro")
        self.assertEqual(view.drive_of(""), "another drive")
        self.assertEqual(
            view.foreign_prompt("C:"),
            "On C: [i] import and resume  [I] import only  [v] view  [Esc] cancel",
        )
        self.assertEqual(
            view.foreign_prompt("Zephyro"),
            "On Zephyro: [i] import and resume  [I] import only  [v] view  [Esc] cancel",
        )
        self.assertEqual(
            view.confirm_prompt("Site copy pass", "C:"),
            'Import "Site copy pass" from C: to Linux?  [y] yes  [n] no',
        )
        self.assertIn("x" * 39 + "…", view.confirm_prompt("x" * 100, "C:"))

    def test_prompts_keep_every_choice_on_a_narrow_terminal(self):
        full = view.foreign_prompt("C:")
        self.assertEqual(view.foreign_prompt("C:", 200), full)
        self.assertEqual(view.foreign_prompt("C:", len(full)), full)
        self.assertEqual(
            view.foreign_prompt("C:", len(full) - 1),
            "On C: [i] import+resume  [I] import  [v] view  [Esc]",
        )
        self.assertEqual(view.foreign_prompt("C:", 40), "[i] resume [I] import [v] view [Esc]")
        self.assertEqual(view.foreign_prompt("C:", 20), "[i] [I] [v] [Esc]")
        self.assertEqual(view.foreign_prompt("C:", 6), "[i] […")
        self.assertEqual(
            view.confirm_prompt("Site copy pass", "C:", 45),
            "Import from C: to Linux?  [y] yes  [n] no",
        )
        self.assertEqual(
            view.confirm_prompt("Site copy pass", "C:", 30), "Import?  [y] yes  [n] no"
        )
        self.assertEqual(view.confirm_prompt("Site copy pass", "C:", 16), "Import? [y] [n]")
        for width in range(1, 90):
            for text in (
                view.foreign_prompt("No Other Way", width),
                view.confirm_prompt("日本語 title", "No Other Way", width),
            ):
                self.assertLessEqual(view.text_width(text), width)
            if width >= 17:
                for choice in ("[i]", "[I]", "[v]", "[Esc]"):
                    self.assertIn(choice, view.foreign_prompt("No Other Way", width))

    def test_list_keys(self):
        expected = {
            ("up", None): "cursor_up",
            ("k", "k"): "cursor_up",
            ("down", None): "cursor_down",
            ("j", "j"): "cursor_down",
            ("pageup", None): "cursor_pageup",
            ("pagedown", None): "cursor_pagedown",
            ("home", None): "cursor_home",
            ("g", "g"): "cursor_home",
            ("end", None): "cursor_end",
            ("G", "G"): "cursor_end",
            ("left", None): "tab_prev",
            ("h", "h"): "tab_prev",
            ("shift+tab", None): "tab_prev",
            ("right", None): "tab_next",
            ("l", "l"): "tab_next",
            ("tab", "\t"): "tab_next",
            ("enter", "\r"): "resume",
            ("o", "o"): "resume_stay",
            ("i", "i"): "import",
            ("v", "v"): "view",
            ("slash", "/"): "filter",
            ("r", "r"): "rescan",
            ("R", "R"): "full_rescan",
            ("question_mark", "?"): "help",
            ("q", "q"): "quit",
            ("escape", None): "escape",
            ("0", "0"): "tab_digit",
            ("3", "3"): "tab_digit",
            ("9", "9"): "tab_digit",
        }
        for (key, character), action in expected.items():
            self.assertEqual(view.key_action("list", key, character), action, key)
        for key, character in (("x", "x"), ("I", "I"), ("y", "y"), ("f5", None), ("space", " ")):
            self.assertIsNone(view.key_action("list", key, character), key)

    def test_prompt_keys(self):
        self.assertEqual(view.key_action("foreign", "i", "i"), "prompt_import_resume")
        self.assertEqual(view.key_action("foreign", "I", "I"), "prompt_import_only")
        self.assertEqual(view.key_action("foreign", "v", "v"), "prompt_view")
        self.assertEqual(view.key_action("foreign", "escape"), "prompt_cancel")
        for key in ("enter", "o", "q", "j", "r", "R", "y"):
            self.assertIsNone(view.key_action("foreign", key, key if len(key) == 1 else None))
        self.assertEqual(view.key_action("confirm", "y", "y"), "confirm_yes")
        self.assertEqual(view.key_action("confirm", "n", "n"), "confirm_no")
        self.assertEqual(view.key_action("confirm", "escape"), "confirm_no")
        self.assertIsNone(view.key_action("confirm", "enter", "\r"))
        self.assertIsNone(view.key_action("confirm", "i", "i"))
        self.assertIn("foreign", view.MODAL_MODES)
        self.assertIn("confirm", view.MODAL_MODES)

    def test_filter_pager_and_unknown_modes(self):
        self.assertEqual(view.key_action("filter", "escape"), "filter_clear")
        self.assertEqual(view.key_action("filter", "enter", "\r"), "filter_accept")
        self.assertEqual(view.key_action("filter", "down"), "cursor_down")
        self.assertIsNone(view.key_action("filter", "q", "q"))  # typed into the box
        self.assertIsNone(view.key_action("filter", "j", "j"))
        self.assertEqual(view.key_action("pager", "q", "q"), "close")
        self.assertEqual(view.key_action("pager", "escape"), "close")
        self.assertEqual(view.key_action("pager", "j", "j"), "down")
        self.assertEqual(view.key_action("pager", "k", "k"), "up")
        self.assertEqual(view.key_action("pager", "pagedown"), "pagedown")
        self.assertEqual(view.key_action("pager", "space", " "), "pagedown")
        self.assertEqual(view.key_action("pager", "home"), "home")
        self.assertEqual(view.key_action("pager", "G", "G"), "end")
        self.assertEqual(view.key_action("help", "question_mark", "?"), "close")
        self.assertIsNone(view.key_action("nonsense", "q", "q"))

    def test_every_action_in_the_spec_table_has_a_key(self):
        actions = set(view.KEYMAPS["list"].values())
        self.assertEqual(
            actions,
            {
                "cursor_up",
                "cursor_down",
                "cursor_pageup",
                "cursor_pagedown",
                "cursor_home",
                "cursor_end",
                "tab_prev",
                "tab_next",
                "tab_digit",
                "resume",
                "resume_stay",
                "import",
                "view",
                "filter",
                "rescan",
                "full_rescan",
                "help",
                "quit",
                "escape",
            },
        )

    def test_help_lines(self):
        plain = view.help_lines()
        self.assertEqual(len(plain), len(view.HELP))
        self.assertTrue(plain[0].startswith("↑ ↓ k j"))
        for width in (120, 80, 60, 40, 30, 12):
            for line in view.help_lines(width):
                self.assertLessEqual(view.text_width(line), width, (width, line))
        joined = " ".join(" ".join(view.help_lines(40)).split())
        for _keys, action in view.HELP:
            self.assertIn(action, joined)


class TranscriptTest(unittest.TestCase):
    def test_wrap_line(self):
        self.assertEqual(view.wrap_line("short", 20), ["short"])
        self.assertEqual(view.wrap_line("", 20), [""])
        self.assertEqual(
            view.wrap_line("the quick brown fox jumps", 10), ["the quick", "brown fox", "jumps"]
        )
        self.assertEqual(
            view.wrap_line("    indented code line here", 12), ["    indented", "code line", "here"]
        )
        self.assertEqual(view.wrap_line("a" * 25, 10), ["a" * 10, "a" * 10, "a" * 5])
        self.assertEqual(
            view.wrap_line("see " + "x" * 12 + " end", 8), ["see", "x" * 8, "xxxx end"]
        )
        self.assertEqual(view.wrap_line("anything", 0), ["anything"])

    def test_wrap_wide(self):
        self.assertEqual(view.wrap_line("日本語のテキスト", 6), ["日本語", "のテキ", "スト"])
        self.assertEqual(view.wrap_line("日本語のテキスト", 5), ["日本", "語の", "テキ", "スト"])
        self.assertEqual(view.wrap_line("🚀🚀 go 🚀🚀🚀", 6), ["🚀🚀", "go", "🚀🚀🚀"])
        self.assertEqual(view.wrap_line("漢", 1), ["漢"])  # cannot fit; still terminates

    def test_wrap_never_exceeds_the_width(self):
        samples = [
            "the quick brown fox " * 9,
            "    deep    indent   " * 7,
            "x" * 97,
            "日本語 テキスト " * 11,
            "mixed 漢字 and 🚀 emoji, e\u0301 too " * 6,
            " " * 50,
            "\t\ttabbed\tline",
        ]
        for text in samples:
            for width in (2, 3, 7, 10, 33, 80):
                for line in view.wrap_text(text, width):
                    self.assertLessEqual(view.text_width(line), width, (text[:10], width))
        text = "the quick brown fox " * 9
        self.assertEqual(" ".join(view.wrap_text(text, 17)).split(), text.split())

    def test_wrap_text_keeps_line_breaks(self):
        self.assertEqual(
            view.wrap_text("one\n\ntwo\r\nthree\rfour", 80), ["one", "", "two", "three", "four"]
        )
        self.assertEqual(view.wrap_text("a\tb", 80), ["a   b"])
        self.assertEqual(view.wrap_text("bell\x07here", 80), ["bell here"])
        self.assertEqual(view.wrap_text("", 80), [""])

    def test_transcript_lines(self):
        tz = UTC
        lines = view.transcript_lines("user", "hello\nworld", "2026-10-10T17:58:00Z", 40, tz)
        self.assertEqual(
            lines,
            [
                ("role-user", "User  2026-10-10 17:58"),
                ("text", "hello"),
                ("text", "world"),
                ("blank", ""),
            ],
        )
        lines = view.transcript_lines("assistant", "reply", None, 40, tz)
        self.assertEqual(lines, [("role-assistant", "Assistant"), ("text", "reply"), ("blank", "")])
        self.assertEqual(
            view.transcript_lines("tool", "Bash", None, 40, tz),
            [("role-tool", "Tool  Bash"), ("blank", "")],
        )
        self.assertEqual(
            view.transcript_lines("narrator", "x", None, 40, tz)[0], ("role-assistant", "Assistant")
        )
        self.assertEqual(
            view.transcript_lines("user", None, None, 40, tz),
            [("role-user", "User"), ("text", ""), ("blank", "")],
        )

    def test_long_turn_is_cut_and_says_so(self):
        lines = view.transcript_lines("assistant", "x" * 250, None, 50, UTC, max_chars=100)
        self.assertEqual(
            [kind for kind, _ in lines], ["role-assistant", "text", "text", "note", "blank"]
        )
        self.assertEqual(lines[3][1], "[… 150 more characters not shown]")
        for width in (5, 20, 80):
            for kind, text in view.transcript_lines(
                "user", "word " * 400, "2026-10-10T17:58:00Z", width, UTC, max_chars=300
            ):
                self.assertLessEqual(view.text_width(text), width, (width, kind))

    def test_pager_status(self):
        self.assertEqual(view.pager_status(0, True, False), "loading… 0 turns")
        self.assertEqual(view.pager_status(120, True, False), "loading… 120 turns")
        self.assertEqual(view.pager_status(214, False, False), "214 turns")
        self.assertEqual(view.pager_status(1, False, False), "1 turn")
        self.assertEqual(view.pager_status(0, False, False), "no turns in this session")
        self.assertEqual(view.pager_status(2000, False, True), "showing first 2,000 turns")
        self.assertEqual(
            view.pager_status(7, False, False, "file vanished"), "7 turns · stopped: file vanished"
        )


class EngineTest(unittest.TestCase):
    """The default engine forwards to ``agent_sessions.api`` by name, at call
    time."""

    def test_forwards_to_the_api(self):
        fake_api = mock.Mock()
        fake_api.scan.return_value = {"mode": "quick"}
        fake_api.load_index.return_value = "INDEX"
        with mock.patch.object(engine_mod.importlib, "import_module", return_value=fake_api):
            engine = engine_mod.Engine()
            self.assertEqual(engine.load_index(), "INDEX")
            self.assertEqual(engine.scan(deep=True, progress="P", cancel="C"), {"mode": "quick"})
            engine.list_sessions(index="I")
            engine.status("I")
            engine.find_session("k", index="I")
            engine.can_import("S", index="I")
            engine.transcript("k", index="I")
            engine.import_session("k", progress="P", index="I")
            engine.open_session("k", index="I")
        fake_api.scan.assert_called_once_with(deep=True, progress="P", cancel="C")
        fake_api.list_sessions.assert_called_once_with(None, index="I")
        fake_api.status.assert_called_once_with(index="I")
        fake_api.find_session.assert_called_once_with("k", index="I")
        fake_api.can_import.assert_called_once_with("S", index="I")
        fake_api.transcript.assert_called_once_with("k", index="I")
        fake_api.import_session.assert_called_once_with("k", progress="P", index="I")
        fake_api.open_session.assert_called_once_with("k", here=False, dry_run=False, index="I")

    def test_where_and_providers(self):
        engine = engine_mod.Engine()
        session = support.make_session("claude", 1, foreign=True)
        self.assertEqual(engine.where(session, support.FakeIndex()), "C:")
        names = [name for name, _label in engine.providers()]
        self.assertEqual(sorted(names), sorted(view.PROVIDER_ORDER))
        self.assertEqual(dict(engine.providers())["antigravity"], "Antigravity")

    def test_missing_function_is_an_error_with_a_reason(self):
        class Hollow(engine_mod.Engine):
            module = "agent_sessions.model"  # a module with none of the functions

        for call in (
            lambda e: e.open_session("k"),
            lambda e: e.import_session("k"),
            lambda e: e.transcript("k"),
            lambda e: e.can_import("s"),
            lambda e: e.scan(),
        ):
            with self.assertRaises(engine_mod.EngineError) as caught:
                call(Hollow())
            self.assertIn("has no `", caught.exception.reason)
            self.assertEqual(view.error_text(caught.exception), caught.exception.reason)

    def test_unloadable_engine(self):
        class Broken(engine_mod.Engine):
            module = "agent_sessions.no_such_module"

        with self.assertRaises(engine_mod.EngineError) as caught:
            Broken().load_index()
        self.assertIn("could not be loaded", caught.exception.reason)

    def test_real_api_lists_through_the_engine(self):
        # Engine errors pass through untouched.
        with self.assertRaises(ValueError):
            engine_mod.Engine().list_sessions("no-such-provider", index=mock.Mock(sessions=[]))


class EntryTest(unittest.TestCase):
    """The launcher and the import rules."""

    launcher = str(REPO / "bin" / "agent-sessions")

    def run_py(self, code):
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_view_and_engine_need_no_textual(self):
        result = self.run_py(
            "import sys; sys.path.insert(0, 'src')\n"
            "import agent_sessions.tui, agent_sessions.tui.view, agent_sessions.tui.engine\n"
            "import agent_sessions.cli, agent_sessions.api\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('textual', 'rich')]\n"
            "print(bad)"
        )
        self.assertEqual(result.stdout.strip(), "[]", result.stderr)

    def test_cli_does_not_load_the_tui(self):
        result = self.run_py(
            "import sys, runpy\n"
            "sys.argv = ['agent-sessions', '--version']\n"
            "try:\n"
            "    runpy.run_path('bin/agent-sessions', run_name='__main__')\n"
            "except SystemExit:\n"
            "    pass\n"
            "print([m for m in sys.modules if 'tui' in m.split('.') or m.startswith('textual')])"
        )
        self.assertEqual(result.stdout.strip().splitlines()[-1], "[]", result.stderr)

    def test_no_engine_module_imports_the_tui(self):
        package = REPO / "src" / "agent_sessions"
        for path in sorted(package.rglob("*.py")):
            if "tui" in path.relative_to(package).parts:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    self.assertNotIn("textual", stripped, path)
                    self.assertNotRegex(stripped, r"\btui\b", path)

    def test_no_colour_or_spacing_in_python(self):
        tui = REPO / "src" / "agent_sessions" / "tui"
        for name in ("app.py", "view.py", "engine.py", "__init__.py", "__main__.py"):
            text = (tui / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"#[0-9a-fA-F]{6}\b", name)
            self.assertNotRegex(text, r"\bDEFAULT_CSS\b|\bCSS\s*=", name)
            self.assertNotRegex(text, r"\.styles\.\w+\s*=", name)
            self.assertNotRegex(text, r"Style\(|style=\"", name)
        css = (tui / "app.tcss").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,8}\s*;")
        self.assertNotRegex(css, r"rgb\(|hsl\(")

    def test_no_arguments_and_no_terminal_prints_help(self):
        result = subprocess.run(
            [sys.executable, self.launcher],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: agent-sessions", result.stdout)

    def test_other_subcommands_still_run(self):
        result = subprocess.run(
            [sys.executable, self.launcher, "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("agent-sessions", result.stdout)

    def test_tui_is_intercepted_before_the_cli(self):
        result = subprocess.run(
            [sys.executable, self.launcher, "tui", "--help"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: agent-sessions [tui]", result.stderr)
        self.assertNotIn("not implemented", result.stderr)
        result = subprocess.run(
            [sys.executable, self.launcher, "tui", "--bogus"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)

    def test_missing_textual_is_one_line_and_exit_1(self):
        result = self.run_py(
            "import sys; sys.path.insert(0, 'src')\n"
            "sys.modules['textual'] = None\n"  # as if it were not installed
            "from agent_sessions.tui.__main__ import main\n"
            "sys.exit(main([]))"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("./install.sh", lines[0])
        self.assertIn(str(REPO), lines[0])

    def test_install_files(self):
        desktop = (REPO / "share" / "agent-sessions.desktop").read_text(encoding="utf-8")
        for line in (
            "Name=Agent Sessions",
            "Terminal=false",
            "Categories=Utility;",
            "Exec=omarchy-launch-tui --app-id=org.omarchy.sessions agent-sessions",
        ):
            self.assertIn(line + "\n", desktop)
        self.assertRegex(desktop, r"(?m)^Icon=\S+$")
        requirements = (REPO / "requirements-tui.txt").read_text(encoding="utf-8")
        pins = [line for line in requirements.splitlines() if line and not line.startswith("#")]
        self.assertEqual(len(pins), 1)
        self.assertRegex(pins[0], r"^textual==\d+\.\d+\.\d+$")
        install = (REPO / "install.sh").read_text(encoding="utf-8")
        for needle in (
            "python3 -m venv",
            "requirements-tui.txt",
            ".local/bin/agent-sessions",
            "agent-sessions.desktop",
        ):
            self.assertIn(needle, install)

    def test_the_textual_pin_is_the_same_in_both_places(self):
        import tomllib

        requirements = (REPO / "requirements-tui.txt").read_text(encoding="utf-8")
        pins = [line for line in requirements.splitlines() if line and not line.startswith("#")]
        with open(REPO / "pyproject.toml", "rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["optional-dependencies"]["tui"], pins)


if __name__ == "__main__":
    unittest.main()
