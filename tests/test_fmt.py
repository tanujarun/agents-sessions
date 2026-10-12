"""fmt.py: an age, a byte count, a project's name and a local time as a
listing shows them, and that none of them raises whatever it is given
(AUDIT A-26); the cached times a listing computes with."""

import contextlib
import io
import json
import os
import re
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakePath, FakeProvider, Sandbox

from agent_sessions import api, cli, fmt
from agent_sessions.fsutil import cache_file
from agent_sessions.index import Index

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
# What a cache or a store may hold where a time, a size or a folder is expected.
ODD = (
    None,
    "",
    "garbage",
    "2026-10-05T10:00:00",  # no zone
    "9999-12-31T23:59:59.000000Z",
    "0001-01-01T00:00:00Z",
    "2026-13-45T99:99:99Z",
    "\ud800",
    5,
    5.5,
    float("nan"),
    float("inf"),
    -(10**30),
    10**30,
    True,
    b"2026",
    [],
    {},
    object(),
)


def stamp(ago: timedelta) -> str:
    return (NOW - ago).isoformat(timespec="microseconds").replace("+00:00", "Z")


class AgeTest(unittest.TestCase):
    def test_the_steps(self):
        cases = [
            (timedelta(seconds=5), "now"),
            (timedelta(seconds=59), "now"),
            (timedelta(minutes=1), "1m"),
            (timedelta(minutes=59), "59m"),
            (timedelta(hours=1), "1h"),
            (timedelta(hours=23), "23h"),
            (timedelta(days=1), "1d"),
            (timedelta(days=13), "13d"),
            (timedelta(days=14), "2w"),
            (timedelta(days=59), "8w"),
            (timedelta(days=60), "2mo"),
            (timedelta(days=364), "12mo"),
            (timedelta(days=365), "1y"),
            (timedelta(days=800), "2y"),
            (timedelta(days=-1), "now"),  # a clock that is ahead
        ]
        for ago, expected in cases:
            with self.subTest(ago=ago):
                self.assertEqual(fmt.age(stamp(ago), NOW), expected)

    def test_a_time_without_a_zone_is_utc(self):
        """AUDIT A-26: ``list`` ended with ``TypeError: can't subtract
        offset-naive and offset-aware datetimes`` on such a cached time."""
        self.assertEqual(fmt.age("2026-10-12T09:00:00", NOW), "3h")
        self.assertEqual(fmt.age("2026-10-12T09:00:00Z", NOW.replace(tzinfo=None)), "3h")

    def test_no_clock_given_is_now(self):
        recent = datetime.now(UTC) - timedelta(hours=5, minutes=1)
        self.assertEqual(fmt.age(recent.isoformat()), "5h")

    def test_nothing_it_is_given_makes_it_raise(self):
        for value in ODD:
            with self.subTest(value=repr(value)[:30]):
                self.assertRegex(fmt.age(value, NOW), r"^(-|now|\d+(m|h|d|w|mo|y))$")
        self.assertEqual(fmt.age("garbage", NOW), fmt.EMPTY)
        self.assertEqual(fmt.age(None, NOW), fmt.EMPTY)
        self.assertEqual(fmt.age(5, NOW), fmt.EMPTY)


class LocalTimeTest(unittest.TestCase):
    def test_in_a_zone(self):
        east = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(fmt.local_time("2026-10-10T12:28:00Z", east), "2026-10-10 17:58")
        self.assertEqual(fmt.local_time("2026-10-10T12:28:00", UTC), "2026-10-10 12:28")

    def test_a_time_no_zone_can_express_is_empty(self):
        """AUDIT A-26: the TUI ended with ``OverflowError: date value out of
        range`` on the last second of the year 9999, east of Greenwich."""
        east = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(fmt.local_time("9999-12-31T23:59:59.000000Z", east), fmt.EMPTY)
        west = timezone(timedelta(hours=-8))
        self.assertEqual(fmt.local_time("0001-01-01T00:00:00Z", west), fmt.EMPTY)

    def test_nothing_it_is_given_makes_it_raise(self):
        for value in ODD:
            for zone in (None, UTC, timezone(timedelta(hours=14)), timezone(timedelta(hours=-12))):
                with self.subTest(value=repr(value)[:30], zone=zone):
                    self.assertRegex(
                        fmt.local_time(value, zone), r"^(-|\d{4}-\d{2}-\d{2} \d{2}:\d{2})$"
                    )


class SizeTest(unittest.TestCase):
    def test_units(self):
        cases = [
            (0, "0 B"),
            (812, "812 B"),
            (999, "999 B"),
            (1000, "1.0 kB"),
            (116701, "116.7 kB"),
            (123697588, "123.7 MB"),
            (1_400_000_000, "1.4 GB"),
            (5 * 10**12, "5000.0 GB"),
            (812.9, "812 B"),
        ]
        for count, expected in cases:
            with self.subTest(count=count):
                self.assertEqual(fmt.size(count), expected)

    def test_the_unit_is_chosen_after_rounding(self):
        """Both copies this one replaces printed ``1000.0 kB`` for 999,950
        bytes (AUDIT N-36)."""
        self.assertEqual(fmt.size(999_949), "999.9 kB")
        self.assertEqual(fmt.size(999_950), "1.0 MB")
        self.assertEqual(fmt.size(999_999_999), "1.0 GB")
        for count in range(990_000, 1_010_000, 7):
            self.assertNotIn("1000.0", fmt.size(count))

    def test_nothing_it_is_given_makes_it_raise(self):
        for value in ODD:
            with self.subTest(value=repr(value)[:30]):
                self.assertRegex(fmt.size(value), r"^(-|\d+ B|\d+\.\d (kB|MB|GB))$")
        self.assertEqual(fmt.size(None), "0 B")
        self.assertEqual(fmt.size(-5), "0 B")
        self.assertEqual(fmt.size("12"), fmt.EMPTY)
        self.assertEqual(fmt.size(float("nan")), fmt.EMPTY)


class ProjectTest(unittest.TestCase):
    def test_the_last_segment_whichever_separator(self):
        self.assertEqual(fmt.project(r"F:\AI Projects\Website Works"), "Website Works")
        self.assertEqual(fmt.project("/home/u/proj/"), "proj")
        self.assertEqual(fmt.project("C:\\"), "C:")
        self.assertEqual(fmt.project(r"C:\a/b\\c"), "c")

    def test_nothing_it_is_given_makes_it_raise(self):
        for value in ODD:
            with self.subTest(value=repr(value)[:30]):
                self.assertIsInstance(fmt.project(value), str)
        for value in (None, "", "/", "\\\\", 5):
            self.assertEqual(fmt.project(value), fmt.EMPTY)


class ClockTest(unittest.TestCase):
    def test_utc_now_is_a_time_to_the_second(self):
        text = fmt.utc_now()
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        then = datetime.fromisoformat(text.replace("Z", "+00:00"))
        self.assertLess(abs((datetime.now(UTC) - then).total_seconds()), 5)


class OneCopyTest(unittest.TestCase):
    def test_the_command_line_uses_these(self):
        self.assertIs(cli._age, fmt.age)
        self.assertIs(cli._size, fmt.size)
        self.assertIs(cli._project, fmt.project)

    def test_the_module_imports_nothing_of_the_package(self):
        source = Path(fmt.__file__).read_text(encoding="utf-8")
        own = r"^\s*(from \.|import agent_sessions|from agent_sessions)"
        self.assertIsNone(re.search(own, source, re.M))


class CachedTimesTest(unittest.TestCase):
    """AUDIT A-26 (``v/v_cache_times.py``): a time in the cache that is text
    and no time stopped ``list`` every time, and no scan mended it."""

    def setUp(self):
        self.box = Sandbox(self)
        self.native = self.box.store(self.box.home)
        FakePath(self, self.box.root / "bin")
        self.box.session(self.native, "a", updated="2026-10-01T10:00:00Z")
        self.box.session(self.native, "b", updated="2026-10-02T10:00:00Z")
        api.scan(deep=True)

    def damage(self, hint: str, **times) -> None:
        data = json.loads(cache_file().read_text())
        for entry in data["parsed"].values():
            entry["candidates"][hint]["session"].update(times)
        cache_file().write_text(json.dumps(data))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_cached_time_that_is_not_one_is_dropped_on_load(self):
        for value in (
            "2026-10-05T10:00:00",  # the audit's two
            "9999-12-31T23:59:59.000000Z",
            "yesterday",
            "",
            "2026-10-05T10:00:00+05:30",  # a time, not as parse_ts writes it
            "2026-10-05 10:00:00.000000Z",
            "1989-12-31T23:59:59.000000Z",
        ):
            with self.subTest(value=value):
                self.damage("a", updated=value, started=value)
                index = Index.load()
                self.assertIsNone(index.load_error)
                cached = next(iter(index.parsed.values()))["candidates"]
                self.assertEqual(
                    (cached["a"]["session"]["updated"], cached["a"]["session"]["started"]),
                    (None, None),
                )
                self.assertEqual(cached["b"]["session"]["updated"], "2026-10-02T10:00:00.000000Z")
                with mock.patch.dict(os.environ, {"TZ": "Asia/Kolkata"}):
                    time.tzset()
                    self.addCleanup(time.tzset)
                    for argv in (("list",), ("list", "--json"), ("status",)):
                        code, out, err = self.run_cli(*argv)
                        self.assertEqual((code, err), (0, ""), argv)
                    code, out, _ = self.run_cli("list")
                self.assertRegex(out.splitlines()[-1], r"^-\s")  # no time: last, no age

    def test_a_time_as_parse_ts_writes_it_is_kept(self):
        self.damage("a", updated="2026-10-05T10:00:00.000000Z", started=None)
        cached = next(iter(Index.load().parsed.values()))["candidates"]["a"]
        self.assertEqual(cached["session"]["updated"], "2026-10-05T10:00:00.000000Z")
        self.assertNotEqual(cached["sig"], [])

    def test_the_next_scan_reads_the_session_again(self):
        """The file did not change, so its signature still stood: the bad
        time would have stayed for good."""
        self.damage("a", updated="9999-12-31T23:59:59.000000Z")
        FakeProvider.parsed.clear()
        summary = api.scan(deep=False)
        self.assertEqual((summary["parsed"], summary["cached"]), (1, 1))
        self.assertEqual(FakeProvider.parsed, ["a"])
        mended = {s.id: s.updated for s in api.list_sessions()}
        self.assertEqual(mended["a"], "2026-10-01T10:00:00.000000Z")
        self.assertEqual(api.scan(deep=False)["parsed"], 0)


if __name__ == "__main__":
    unittest.main()
