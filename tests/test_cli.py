"""cli.py: scan, list, status, verify through main()."""

import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import Sandbox  # noqa: E402

from agent_sessions import cli  # noqa: E402
from agent_sessions.fsutil import cache_file  # noqa: E402
from agent_sessions.model import make_key  # noqa: E402


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.native = self.box.store(self.box.home)
        self.disk = self.box.mount("Win Disk")
        (self.disk / "Windows").mkdir()
        self.foreign = self.box.store(self.disk / "Users" / "arun")
        self.site = self.disk / "Users" / "arun" / "Site Work"
        self.site.mkdir()
        which = mock.patch("agent_sessions.index.shutil.which",
                           side_effect=lambda name: f"/usr/bin/{name}")
        which.start()
        self.addCleanup(which.stop)
        now = datetime.now(timezone.utc)
        stamp = lambda **ago: (now - timedelta(**ago)).isoformat()   # noqa: E731
        self.box.session(self.native, "n1", title="Fix the  menu\nlag", updated=stamp(days=2),
                         cwd=str(self.box.home))
        self.box.session(self.native, "n2", title="Older one", updated=stamp(days=40))
        self.box.session(self.foreign, "f1", title="Pill content", updated=stamp(hours=3),
                         cwd=r"C:\Users\arun\Site Work")
        self.box.session(self.foreign, "n2", title="Older one", updated=stamp(days=41),
                         cwd=r"E:\gone")

    def test_scan_then_list(self):
        code, out, err = run("scan")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("scan: deep, 2 mounts", out)
        self.assertIn("fake  2 stores  4 sessions", out)
        self.assertIn("sessions: 4 listed of 4 candidates (4 parsed, 0 from cache)", out)
        self.assertRegex(out, r"Win Disk\s+\S+Win Disk\s+\d+ dirs\s+1 pruned\s+0 unreadable")
        code, out, _ = run("scan")
        self.assertEqual(code, 0)
        self.assertIn("scan: quick", out)
        self.assertIn("(0 parsed, 4 from cache)", out)
        self.assertNotIn("dirs", out)

        code, out, err = run("list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["3h", "2d", "5w", "5w"])
        self.assertEqual(lines[0].split(None, 3)[:3], ["3h", "C:", "fake"])
        self.assertRegex(lines[0], r"^3h\s+C:\s+fake\s+Pill content\s+Site Work$")
        self.assertRegex(lines[1], r"^2d\s+Linux\s+fake\s+Fix the menu lag\s+user$")
        self.assertRegex(lines[2], r"^5w\s+Linux\s+fake\s+Older one\s+-$")
        self.assertRegex(lines[3], r"^5w\s+C: · imported\s+fake\s+Older one\s+gone$")
        columns = {line.index("fake") for line in lines}
        self.assertEqual(len(columns), 1)   # aligned

    def test_list_json_is_byte_identical_and_complete(self):
        run("scan", "--deep")
        first = run("list", "--json")
        second = run("list", "--json")
        self.assertEqual(first, second)
        self.assertEqual((first[0], first[2]), (0, ""))
        run("scan")
        run("scan", "--deep")
        self.assertEqual(run("list", "--json"), first)
        rows = json.loads(first[1])
        self.assertEqual([r["id"] for r in rows], ["f1", "n1", "n2", "n2"])
        self.assertEqual(set(rows[0]), {
            "provider", "id", "key", "title", "cwd", "cwd_local", "cwd_exists", "started",
            "updated", "messages", "store", "origin", "mount", "mount_label", "files",
            "launchable", "imported_as", "note"})
        f1 = rows[0]
        self.assertEqual((f1["cwd_local"], f1["cwd_exists"], f1["launchable"], f1["origin"]),
                         (str(self.site), True, False, "foreign"))
        self.assertEqual(rows[1]["launchable"], True)
        self.assertEqual(rows[3]["imported_as"], make_key("fake", self.native, "n2"))

    def test_list_provider_filter(self):
        run("scan")
        self.assertEqual(len(json.loads(run("list", "--provider", "fake", "--json")[1])), 4)
        code, _, err = run("list", "--provider", "nope")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_list_without_an_index(self):
        code, out, err = run("list")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("no index yet", err)
        self.assertEqual(run("list", "--json"), (0, "[]\n", ""))
        self.assertFalse(cache_file().exists())

    def test_scan_json(self):
        code, out, err = run("scan", "--deep", "--json")
        self.assertEqual((code, err), (0, ""))
        summary = json.loads(out)
        self.assertEqual((summary["mode"], summary["sessions"], summary["stores"],
                          summary["cancelled"]), ("deep", 4, 2, False))
        self.assertEqual(sorted(summary["walk"]), sorted(m["path"] for m in summary["mounts"]))
        self.assertEqual(set(summary["walk"][str(self.disk)]),
                         {"dirs", "pruned", "errors", "mount_skips", "seconds", "error_paths"})
        self.assertIsNone(json.loads(run("scan", "--json")[1])["walk"])

    def test_status(self):
        run("scan")
        code, out, err = run("status")
        self.assertEqual((code, err), (0, ""))
        for needle in (
                f"index: {cache_file()}", "mounts", "stores", "providers", "drive letters",
                "warnings: 0", str(self.native), str(self.foreign),
                "2 listed / 2 candidates / 0 skipped", "evidence, 1 path",
                "no evidence, 1 path"):
            self.assertIn(needle, out)
        self.assertRegex(out, r"C:\s+Win Disk\s+" + str(self.disk).replace(" ", r"\s"))
        self.assertRegex(out, r"E:\s+->\s+unresolved")
        self.assertRegex(out, r"fake\s+native\s+" + str(self.native))

    def test_status_json(self):
        run("scan")
        with mock.patch("agent_sessions.api.shutil.which",
                        side_effect=lambda name: None if name == "crush" else f"/bin/{name}"):
            code, out, _ = run("status", "--json")
        status = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(set(status), {
            "cache", "last_quick", "last_deep", "mounts", "providers", "stores",
            "skipped_stores", "letters", "unresolved_letters", "no_reader", "warnings"})
        self.assertEqual([t["tool"] for t in status["no_reader"]],
                         ["copilot", "cursor-agent", "opencode"])
        self.assertEqual(status["providers"], [{
            "name": "fake", "label": "Fake", "binary": "fake-agent",
            "installed": "/bin/fake-agent", "resumable": True, "reader": True,
            "stores": 2, "sessions": 4}])
        self.assertEqual([(s["origin"], s["listed"], s["candidates"], s["skipped"])
                          for s in status["stores"]],
                         [("native", 2, 2, 0), ("foreign", 2, 2, 0)])
        self.assertEqual(run("status", "--json")[1], run("status", "--json")[1])

    def test_status_before_any_scan(self):
        code, out, _ = run("status")
        self.assertEqual(code, 0)
        self.assertIn("last scan: never; last deep scan: never", out)
        self.assertIn("not walked yet", out)

    def test_verify(self):
        run("scan")
        code, out, err = run("verify")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 stores, 4 sessions on disk", out)
        self.assertIn("verify: the index matches the disks", out)

        hidden = self.box.store(self.disk / "node_modules" / "x")
        code, out, _ = run("verify")
        self.assertEqual(code, 1)
        self.assertIn(f"DIFFERENT: store on disk but not in the index: fake {hidden}", out)
        self.assertIn("verify: 1 difference", out)

    def test_verify_without_an_index_fails(self):
        code, out, _ = run("verify")
        self.assertEqual(code, 1)
        self.assertIn("store on disk but not in the index", out)

    def test_bad_mounts_env(self):
        with mock.patch.dict("os.environ", {"AGENT_SESSIONS_MOUNTS": "{"}):
            code, _, err = run("scan")
        self.assertEqual(code, 1)
        self.assertIn("AGENT_SESSIONS_MOUNTS", err)

    def test_path_that_is_not_utf8_does_not_break_output(self):
        run("scan")
        text = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with contextlib.redirect_stdout(text):
            cli._out("caf\xe9 \udcff")
        text.flush()
        self.assertEqual(text.buffer.getvalue(), b"caf\\xe9 \\udcff\n")


class AgeTest(unittest.TestCase):
    def test_ages(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        cases = [(timedelta(seconds=5), "now"), (timedelta(minutes=7), "7m"),
                 (timedelta(hours=23), "23h"), (timedelta(days=2), "2d"),
                 (timedelta(days=13), "13d"), (timedelta(days=21), "3w"),
                 (timedelta(days=75), "2mo"), (timedelta(days=800), "2y"),
                 (timedelta(days=-1), "now")]
        for ago, expected in cases:
            stamp = (now - ago).isoformat(timespec="microseconds").replace("+00:00", "Z")
            self.assertEqual(cli._age(stamp, now), expected)
        self.assertEqual(cli._age(None, now), "-")
        self.assertEqual(cli._age("garbage", now), "-")

    def test_project(self):
        self.assertEqual(cli._project(r"F:\AI Projects\Website Works"), "Website Works")
        self.assertEqual(cli._project("/home/u/proj/"), "proj")
        self.assertEqual(cli._project("C:\\"), "C:")
        self.assertEqual(cli._project(None), "-")


if __name__ == "__main__":
    unittest.main()
