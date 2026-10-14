"""cli.py: every subcommand but tui, through main()."""

import contextlib
import io
import json
import re
import shlex
import shutil
import sys
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CODEX_SEGMENTED,
    CODEX_WIN,
    GEMINI_WIN,
    GROK_NATIVE,
    GROK_WIN,
    World,
    files_below,
)
from support import FakePath, Sandbox, claude_transcript, state_names

from agent_sessions import api, cli, fsutil, launch, mounts, providers
from agent_sessions.fsutil import cache_file
from agent_sessions.index import CACHE_VERSION, Index
from agent_sessions.model import make_key


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
        self.path = FakePath(self, self.box.root / "bin")  # every command is installed
        now = datetime.now(UTC)
        stamp = lambda **ago: (now - timedelta(**ago)).isoformat()  # noqa: E731
        self.box.session(
            self.native,
            "n1",
            title="Fix the  menu\nlag",
            updated=stamp(days=2),
            cwd=str(self.box.home),
        )
        self.box.session(self.native, "n2", title="Older one", updated=stamp(days=40))
        self.box.session(
            self.foreign,
            "f1",
            title="Pill content",
            updated=stamp(hours=3),
            cwd=r"C:\Users\arun\Site Work",
        )
        self.box.session(
            self.foreign, "n2", title="Older one", updated=stamp(days=41), cwd=r"E:\gone"
        )

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
        self.assertEqual(len(columns), 1)  # aligned

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
        self.assertEqual(
            set(rows[0]),
            {
                "provider",
                "id",
                "key",
                "title",
                "cwd",
                "cwd_local",
                "cwd_exists",
                "started",
                "updated",
                "messages",
                "store",
                "origin",
                "mount",
                "mount_label",
                "files",
                "launchable",
                "imported_as",
                "note",
                "offline",
            },
        )
        f1 = rows[0]
        self.assertEqual(
            (f1["cwd_local"], f1["cwd_exists"], f1["launchable"], f1["origin"]),
            (str(self.site), True, False, "foreign"),
        )
        self.assertEqual(rows[1]["launchable"], True)
        self.assertEqual(rows[3]["imported_as"], make_key("fake", self.native, "n2"))

    def test_list_provider_filter(self):
        run("scan")
        self.assertEqual(len(json.loads(run("list", "--provider", "fake", "--json")[1])), 4)
        code, _, err = run("list", "--provider", "nope")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    NOTICE = "agent-sessions: no usable cache; scanning…\n"

    def test_list_without_a_cache_scans_first(self):
        """No cache file: `list` scans by itself and says so on stderr;
        stdout is the listing a scan followed by `list` gives."""
        self.assertFalse(cache_file().exists())
        self.assertEqual(Index.load().load_error, "missing")
        self.assertEqual(api.load_index().sessions, [])  # the API itself does not scan
        self.assertFalse(cache_file().exists())
        code, out, err = run("list")
        self.assertEqual((code, err), (0, self.NOTICE))
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["3h", "2d", "5w", "5w"])
        self.assertTrue(cache_file().exists())
        self.assertIsNone(Index.load().load_error)
        self.assertEqual(run("list"), (0, out, ""))  # the cache is there now

        cache_file().unlink()
        code, as_json, err = run("list", "--json")
        self.assertEqual((code, err), (0, self.NOTICE))
        self.assertEqual([r["id"] for r in json.loads(as_json)], ["f1", "n1", "n2", "n2"])
        self.assertEqual(run("list", "--json"), (0, as_json, ""))
        self.assertNotIn("load_error", json.loads(cache_file().read_text(encoding="utf-8")))

    def test_list_with_a_corrupt_cache_scans_first(self):
        run("scan")
        want = run("list")
        want_json = run("list", "--json")
        self.assertEqual((want[2], want_json[2]), ("", ""))
        good = cache_file().read_text(encoding="utf-8")
        for damage in (
            "{not json",
            "",
            "[]",
            good.replace(f'"version":{CACHE_VERSION}', '"version":0'),
            good[: len(good) // 2],
        ):
            for argv, expected in ((("list",), want), (("list", "--json"), want_json)):
                with self.subTest(damage=damage[:20], argv=argv):
                    cache_file().write_text(damage, encoding="utf-8")
                    self.assertRegex(Index.load().load_error, r"^unreadable: .+")
                    self.assertEqual(api.load_index().sessions, [])
                    code, out, err = run(*argv)
                    self.assertEqual((code, out), (0, expected[1]))
                    kept = cache_file().with_name("index.json.unusable")
                    self.assertRegex(
                        err,
                        rf"^{re.escape(self.NOTICE)}agent-sessions: "
                        rf"{re.escape(str(cache_file()))}: "
                        rf"the cache could not be used \(unreadable: .+\); it was kept as "
                        rf"{re.escape(str(kept))} and a new one was written\n$",
                    )
                    self.assertEqual(kept.read_text(encoding="utf-8"), damage)
                    self.assertIsNone(Index.load().load_error)
                    self.assertEqual(run(*argv), expected)

    def test_scan_json(self):
        code, out, err = run("scan", "--deep", "--json")
        self.assertEqual((code, err), (0, ""))
        summary = json.loads(out)
        self.assertEqual(
            (summary["mode"], summary["sessions"], summary["stores"], summary["cancelled"]),
            ("deep", 4, 2, False),
        )
        self.assertEqual(sorted(summary["walk"]), sorted(m["path"] for m in summary["mounts"]))
        self.assertEqual(
            set(summary["walk"][str(self.disk)]),
            {"dirs", "pruned", "errors", "mount_skips", "seconds", "error_paths"},
        )
        self.assertIsNone(json.loads(run("scan", "--json")[1])["walk"])

    def test_status(self):
        run("scan")
        code, out, err = run("status")
        self.assertEqual((code, err), (0, ""))
        for needle in (
            f"index: {cache_file()}",
            "mounts",
            "stores",
            "providers",
            "drive letters",
            "warnings: 0",
            str(self.native),
            str(self.foreign),
            "2 listed / 2 candidates / 0 skipped",
            "evidence, 1 path",
            "no evidence, 1 path",
        ):
            self.assertIn(needle, out)
        self.assertRegex(out, r"C:\s+Win Disk\s+" + str(self.disk).replace(" ", r"\s"))
        self.assertRegex(out, r"E:\s+->\s+unresolved")
        self.assertRegex(out, r"fake\s+native\s+" + str(self.native))

    def test_status_json(self):
        run("scan")
        self.path.missing.add("crush")  # not on PATH at all
        self.path.wrapper("copilot")  # on PATH, but only the script that would install it
        code, out, _ = run("status", "--json")
        status = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(
            set(status),
            {
                "cache",
                "log",
                "last_quick",
                "last_deep",
                "mounts",
                "volumes",
                "providers",
                "stores",
                "skipped_stores",
                "letters",
                "unresolved_letters",
                "tools",
                "no_reader",
                "warnings",
            },
        )
        # Only what is really installed is an "installed tool with no reader".
        bin_dir = self.box.root / "bin"
        self.assertEqual(
            status["no_reader"],
            [
                {"tool": "cursor-agent", "path": str(bin_dir / "cursor-agent")},
                {"tool": "opencode", "path": str(bin_dir / "opencode")},
            ],
        )
        self.assertEqual(
            [(t["tool"], t["detail"], t["wrapper"], t["reader"]) for t in status["tools"]],
            [
                (
                    "copilot",
                    "not installed (the command on PATH would install it)",
                    True,
                    False,
                ),
                ("crush", "not found", False, False),
                ("cursor-agent", "installed", False, False),
                ("fake-agent", "installed", False, True),
                ("opencode", "installed", False, False),
            ],
        )
        self.assertEqual(
            [(t["path"], t["installed"]) for t in status["tools"]],
            [
                (str(bin_dir / "copilot"), False),
                (None, False),
                (str(bin_dir / "cursor-agent"), True),
                (str(bin_dir / "fake-agent"), True),
                (str(bin_dir / "opencode"), True),
            ],
        )
        self.assertEqual(
            status["providers"],
            [
                {
                    "name": "fake",
                    "label": "Fake",
                    "binary": "fake-agent",
                    "installed": str(bin_dir / "fake-agent"),
                    "tool": "installed",
                    "resumable": True,
                    "reader": True,
                    "stores": 2,
                    "sessions": 4,
                    "offline": 0,
                }
            ],
        )
        self.assertEqual(
            [(s["origin"], s["listed"], s["candidates"], s["skipped"]) for s in status["stores"]],
            [("native", 2, 2, 0), ("foreign", 2, 2, 0)],
        )
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

        # A store a quick scan never looks for, made after the last deep scan:
        # stale, with the advice (AUDIT A-20).
        hidden = self.box.store(self.disk / "node_modules" / "x")
        code, out, _ = run("verify")
        self.assertEqual(code, 0)
        self.assertIn(f"stale: store new since the last deep scan: fake {hidden}\n", out)
        self.assertIn(
            "1 store appeared since the last deep scan; run `agent-sessions scan --deep` "
            "first to compare them too\n",
            out,
        )
        self.assertIn("verify: the index matches the disks", out)
        # Made before it: a fault of the index.
        data = json.loads(cache_file().read_text(encoding="utf-8"))
        later = datetime.now(UTC) + timedelta(seconds=2)
        data["last_deep"] = later.isoformat(timespec="seconds").replace("+00:00", "Z")
        cache_file().write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = run("verify")
        self.assertEqual(code, 1)
        self.assertIn(f"DIFFERENT: store on disk but not in the index: fake {hidden}", out)
        self.assertIn("verify: 1 difference", out)
        self.assertIn("scan --deep", run("verify", "--help")[1])

    def test_scan_says_itself_what_it_could_not_keep(self):
        """AUDIT A-25: with a cache that cannot be written, ``scan`` said
        ``warnings: 1 (see status)``, and ``status`` had none."""
        self.box.cache.write_text("a file where the directory should be", encoding="utf-8")
        code, out, err = run("scan")
        self.assertEqual(code, 0)
        self.assertNotIn("warnings:", out)
        self.assertRegex(
            err, rf"^agent-sessions: {re.escape(str(cache_file()))}: cache not saved: "
        )
        self.assertEqual(run("status")[1].count("warnings: 0"), 1)
        code, out, err = run("scan", "--json")
        self.assertEqual((code, err), (0, ""))  # JSON has it, in its warnings
        self.assertEqual(len(api.cache_warnings(json.loads(out))), 1)

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
            cli._out("caf\xe9 \udcff")  # the odd byte of a file name: a space, as a control is
            cli._json("caf\xe9 \udcff")  # exact, in what JSON has for it
        text.flush()
        self.assertEqual(text.buffer.getvalue(), b'caf\\xe9  \n"caf\\u00e9 \\udcff"\n')


class SessionCommandsTest(unittest.TestCase):
    """open, import, show and ``list --keys`` on the fixture machine."""

    def setUp(self):
        self.w = World(self, native=("claude", "codex", "grok"))
        self.home = self.w.home
        self.index = self.w.scan()
        self.native_key = self.w.session(CLAUDE_NATIVE, "native", self.index).key
        self.foreign_key = self.w.session(CLAUDE_WIN, "foreign", self.index).key

    def command(self, session_id, cwd, app="claude", verb="--resume") -> str:
        return (
            f"cd {shlex.quote(str(cwd))} && omarchy-launch-tui "
            f"--app-id=org.omarchy.{app} {app} {verb} {session_id}\n"
        )

    # --- open ---------------------------------------------------------------

    def test_open_dry_run_prints_the_command_and_starts_nothing(self):
        expected = self.command(CLAUDE_NATIVE, self.home / "My Proj")
        self.assertIn("'", expected)  # the space is quoted for a shell
        for ref in (self.native_key, CLAUDE_NATIVE, self.native_key[:20]):
            self.assertEqual(run("open", ref, "--dry-run"), (0, expected, ""))
        self.assertEqual(
            run("open", CODEX_SEGMENTED, "--dry-run"),
            (0, self.command(CODEX_SEGMENTED, self.home, "codex", "resume"), ""),
        )
        self.assertEqual((self.w.started, self.w.notified), ([], []))

    def test_open_dry_run_says_when_the_line_is_not_what_would_run(self):
        """AUDIT N-22: the control characters of a folder's name are printed
        as spaces, so the ``cd`` line of such a folder is not the command."""
        odd = self.home / "ev\x1b[2Jil \u202edir"
        odd.mkdir()
        session_id = "aaaaaaaa-0000-4000-8000-0000000000e1"
        claude_transcript(self.home / ".claude", session_id, str(odd))
        self.w.scan()
        code, out, err = run("open", session_id, "--dry-run")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        shown = shlex.quote(str(odd)).replace("\x1b", " ").replace("\u202e", " ")
        self.assertEqual(
            lines[0],
            f"cd {shown} && omarchy-launch-tui --app-id=org.omarchy.claude claude "
            f"--resume {session_id}",
        )
        self.assertNotIn("\x1b", out)
        self.assertEqual(lines[1], cli._NOT_AS_SHOWN)
        self.assertIn("not exactly what runs", lines[1])
        # A name with a space or a quote is shown exactly, quoted: no note.
        code, out, err = run("open", CLAUDE_NATIVE, "--dry-run")
        self.assertEqual(
            out.splitlines(), [self.command(CLAUDE_NATIVE, self.home / "My Proj")[:-1]]
        )
        self.assertEqual((self.w.started, self.w.notified), ([], []))

    def test_open_dry_run_shows_the_terminal_that_would_be_used(self):
        cd = f"cd {shlex.quote(str(self.home / 'My Proj'))} && "
        tail = f"claude --resume {CLAUDE_NATIVE}\n"
        self.w.installed.discard("omarchy-launch-tui")
        self.w.installed.update(("xdg-terminal-exec", "wezterm"))
        self.assertEqual(
            run("open", CLAUDE_NATIVE, "--dry-run"),
            (0, f"{cd}xdg-terminal-exec --app-id=org.omarchy.claude -- {tail}", ""),
        )
        self.w.installed.discard("xdg-terminal-exec")
        self.assertEqual(
            run("open", CLAUDE_NATIVE, "--dry-run"), (0, f"{cd}wezterm start -- {tail}", "")
        )
        with mock.patch.dict("os.environ", {"AGENT_SESSIONS_TERMINAL": "myterm --title 'a b' -e"}):
            self.w.installed.add("myterm")
            self.assertEqual(
                run("open", CLAUDE_NATIVE, "--dry-run"),
                (0, f"{cd}myterm --title 'a b' -e {tail}", ""),
            )
        self.w.installed.difference_update(("wezterm", "myterm"))
        code, out, err = run("open", CLAUDE_NATIVE, "--dry-run")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("set AGENT_SESSIONS_TERMINAL to the command that starts yours", err)
        self.assertEqual((self.w.started, self.w.notified), ([], []))

    def test_open_launches_and_prints_the_plan(self):
        code, out, err = run("open", GROK_NATIVE)
        self.assertEqual(
            (code, out, err), (0, self.command(GROK_NATIVE, self.home / "proj", "grok"), "")
        )
        self.assertEqual(
            self.w.started,
            [
                (
                    [
                        "omarchy-launch-tui",
                        "--app-id=org.omarchy.grok",
                        "grok",
                        "--resume",
                        GROK_NATIVE,
                    ],
                    str(self.home / "proj"),
                )
            ],
        )

    def test_open_prints_the_note_when_the_folder_is_gone(self):
        shutil.rmtree(self.home / "My Proj")
        code, out, err = run("open", CLAUDE_NATIVE, "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            self.command(CLAUDE_NATIVE, self.home)
            + "note: project folder is gone; starting in the home directory\n",
        )

    def test_open_foreign_fails_with_the_reason(self):
        for extra in ((), ("--dry-run",)):
            self.assertEqual(
                run("open", self.foreign_key, *extra),
                (1, "", "agent-sessions: this session is on C:; import it first\n"),
            )
        self.assertEqual(self.w.started, [])
        self.assertEqual(len(self.w.notified), 1)  # the real attempt only

    def test_open_here_prints_first_then_execs(self):
        calls = []
        out = io.StringIO()
        with (
            mock.patch.object(launch.os, "chdir", lambda path: calls.append(("chdir", path))),
            mock.patch.object(
                launch.os,
                "execvp",
                lambda file, argv: calls.append(("execvp", argv, out.getvalue())),
            ),
            contextlib.redirect_stdout(out),
        ):
            code = cli.main(["open", GROK_NATIVE, "--here"])
        printed = f"cd {self.home / 'proj'} && grok --resume {GROK_NATIVE}\n"
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [
                ("chdir", str(self.home / "proj")),
                ("execvp", ["grok", "--resume", GROK_NATIVE], printed),
            ],
        )
        self.assertEqual(out.getvalue(), printed)
        self.assertEqual(run("open", GROK_NATIVE, "--here", "--dry-run"), (0, printed, ""))
        self.assertEqual(self.w.started, [])

    def test_unknown_and_ambiguous_keys(self):
        for command in ("open", "show", "import"):
            code, out, err = run(command, "claude:nope")
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(
                err,
                "agent-sessions: no session matches 'claude:nope'; `list --keys` shows the keys\n",
            )
            code, out, err = run(command, "claude:")
            self.assertEqual((code, out), (1, ""))
            self.assertIn("'claude:' matches 8 sessions: claude:", err)
            self.assertIn(self.native_key, err)
            self.assertIn(self.foreign_key, err)
        code, _, err = run("show", "c")
        self.assertRegex(err, r"'c' matches 11 sessions: (\S+, ){8}and 3 more\n$")
        self.assertEqual(run("open", "", "--dry-run")[2], "agent-sessions: no session key given\n")
        self.assertEqual((self.w.started, self.w.notified), ([], []))

    # --- import -------------------------------------------------------------

    def test_import_dry_run_says_what_would_happen_and_writes_nothing(self):
        code, out, err = run("import", self.foreign_key, "--dry-run")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        source = self.w.profile / ".claude" / "projects" / "F--Proj-My-Site"
        target = [line.split(" -> ")[1] for line in lines if line.startswith("copy ")]
        self.assertEqual(
            lines[0],
            f"import {self.foreign_key} -> {api.import_plan(self.foreign_key)['native_key']}",
        )
        self.assertEqual(lines[1], f"project folder: F:\\Proj\\My Site -> {self.w.site}")
        self.assertTrue(lines[2].startswith(f"copy {source / CLAUDE_WIN}/ -> "))
        self.assertTrue(lines[2].endswith(f"{CLAUDE_WIN}/ (1 file, 28 B)"), lines[2])
        self.assertTrue(lines[3].startswith(f"copy {source / CLAUDE_WIN}.jsonl -> "))
        self.assertRegex(lines[3], r"\.jsonl \(4\.9 kB\)$")
        self.assertTrue(all(str(self.home / ".claude" / "projects") in t for t in target))
        self.assertRegex(lines[4], r"^free space on .*: [0-9.]+ [kMG]?B; 5\.5 kB needed$")
        self.assertEqual(lines[5:], ["dry run: nothing was written"])

        code, out, err = run("import", CODEX_WIN, "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("\nrewrite ", out)
        self.assertIn(": working directories in session_meta and turn_context records\n", out)
        self.assertIn(f"\n  session_meta payload.cwd: F:\\Proj\\My Site -> {self.w.site}\n", out)
        self.assertIn("\n  turn_context payload.workspace_roots[]: F:\\Proj\\My Site -> ", out)

        # Grok: the group directory is named the way the tool names it, for
        # a folder with '-' or '_' in its path too (the temp directory's).
        code, out, err = run("import", GROK_WIN, "--dry-run")
        self.assertEqual((code, err), (0, ""))
        group = urllib.parse.quote(str(self.w.site), safe="")
        self.assertRegex(group, r"[-_]")
        self.assertIn(f" -> {self.home / '.grok' / 'sessions' / group / GROK_WIN}/ (", out)
        self.assertIn(": info.cwd and grok_home in summary.json\n", out)
        self.assertIn(f"\n  info.cwd: F:\\Proj\\My Site -> {self.w.site}\n", out)

        # Nothing was written anywhere: the stores are as the fixtures were.
        self.assertEqual(state_names(), [])  # the log apart
        self.assertNotIn(CLAUDE_WIN, " ".join(files_below(self.home / ".claude")))
        self.assertNotIn(CODEX_WIN, " ".join(files_below(self.home / ".codex")))
        self.assertNotIn(GROK_WIN, " ".join(files_below(self.home / ".grok")))

    def test_import_prints_the_native_key_and_is_idempotent(self):
        code, out, err = run("import", self.foreign_key)
        native = self.w.session(CLAUDE_WIN, "native")
        self.assertEqual((code, out), (0, native.key + "\n"))
        self.assertEqual(
            err, f"agent-sessions: imported 2 items (5.0 kB) into {self.home / '.claude'}\n"
        )
        self.assertTrue(native.launchable)
        for ref in (self.foreign_key, CLAUDE_WIN):
            self.assertEqual(
                run("import", ref),
                (0, native.key + "\n", "agent-sessions: already imported; nothing was copied\n"),
            )
            self.assertEqual(
                run("import", ref, "--dry-run"),
                (0, f"already imported: {native.key}\ndry run: nothing was written\n", ""),
            )
        code, out, _ = run("list", "--keys")
        row = next(line for line in out.splitlines() if line.endswith(self.foreign_key))
        self.assertRegex(row, r"^\S+\s+C: · imported\s+claude\s+title-69\s+My Site\s+claude:")
        self.assertEqual(
            run("open", self.foreign_key, "--dry-run"),
            (0, self.command(CLAUDE_WIN, self.w.site), ""),
        )

    def test_import_open_resumes_the_copy(self):
        code, out, err = run("import", CODEX_WIN, "--open")
        native = self.w.session(CODEX_WIN, "native")
        self.assertEqual(code, 0)
        self.assertEqual(
            out, native.key + "\n" + self.command(CODEX_WIN, self.w.site, "codex", "resume")
        )
        self.assertIn("imported 1 item (9.6 kB)", err)
        self.assertEqual(
            self.w.started,
            [
                (
                    [
                        "omarchy-launch-tui",
                        "--app-id=org.omarchy.codex",
                        "codex",
                        "resume",
                        CODEX_WIN,
                    ],
                    str(self.w.site),
                )
            ],
        )

    def test_import_refusals_exit_one_with_the_reason(self):
        for ref, needle in (
            (AGY_WIN, "when it cannot read one; --experimental imports it all the same\n"),
            (GEMINI_WIN, "Gemini CLI is not installed on this machine"),
            (CLAUDE_NATIVE, "this session is already on Linux"),
        ):
            code, out, err = run("import", ref, "--open")
            self.assertEqual((code, out), (1, ""), ref)
            self.assertTrue(err.startswith("agent-sessions: "), err)
            self.assertIn(needle, err)
        shutil.rmtree(self.w.site)
        self.w.scan()
        self.assertEqual(
            run("import", self.foreign_key),
            (
                1,
                "",
                "agent-sessions: the project folder F:\\Proj\\My Site was not found on any "
                "mounted drive; use --to DIR to import it into a folder you choose\n",
            ),
        )
        self.assertEqual(self.w.started, [])
        self.assertEqual(state_names(), [])  # the log apart

    # --- show ---------------------------------------------------------------

    def test_show_on_each_providers_fixture(self):
        cases = {  # id: (provider label, where, turns as printed)
            CLAUDE_NATIVE: ("Claude", "Linux", "user:\ntext-5\n"),
            CODEX_SEGMENTED: (
                "Codex",
                "Linux",
                "user:\ntext-25\n\nassistant:\ntext-26\n\n[tool: exec]\n\n"
                "user:\ntext-146\n\nassistant:\ntext-151\n",
            ),
            GROK_NATIVE: ("Grok", "Linux", None),
            AGY_WIN: (
                "Antigravity",
                "C:",
                "user:\ntext-3\n\n[tool: list_dir]\n\nassistant:\ntext-9\n\n"
                "user:\ntext-11\n\nassistant:\ntext-12\n\n"
                "user:\ntext-13\n\nassistant:\ntext-14\n",
            ),
            GEMINI_WIN: ("Gemini", "C:", "user:\ntext-prompt-1\n\nassistant:\ntext-reply-1\n"),
            CLAUDE_WIN: ("Claude", "C:", "user:\ntext-65\n"),
        }
        for session_id, (label, where, turns) in cases.items():
            with self.subTest(provider=label, where=where):
                session = api.find_session(session_id, self.index)
                code, out, err = run("show", session.key)
                self.assertEqual((code, err), (0, ""))
                header, _, body = out.partition("\n\n")
                rows = dict(line.split(None, 1) for line in header.splitlines())
                self.assertEqual(list(rows)[:4], ["title", "provider", "where", "cwd"])
                self.assertEqual(list(rows)[-4:], ["started", "updated", "messages", "key"])
                self.assertEqual(
                    (
                        rows["title"],
                        rows["provider"],
                        rows["where"],
                        rows["cwd"],
                        rows["started"],
                        rows["updated"],
                        rows["messages"],
                        rows["key"],
                    ),
                    (
                        session.title,
                        label,
                        where,
                        session.cwd,
                        session.started,
                        session.updated,
                        str(session.messages),
                        session.key,
                    ),
                )
                self.assertEqual(rows.get("local"), session.cwd_local)
                if turns is not None:
                    self.assertEqual(body, turns)
                self.assertRegex(body, r"^user:\n")
                self.assertEqual(run("show", session_id)[1], out)
        grok = run("show", GROK_NATIVE)[1]
        self.assertIn("\n\nassistant:\ntext-55\n\n[tool: name-56]\n\nassistant:\ntext-62\n", grok)

    def test_show_groups_tool_calls_and_keeps_text_lines(self):
        turns = [
            api.Turn("user", "one\ntwo", None),
            api.Turn("tool", "Bash", None),
            api.Turn("tool", "Read", None),
            api.Turn("assistant", "done", None),
            api.Turn("tool", "Edit", None),
        ]
        with mock.patch.object(api, "transcript", return_value=iter(turns)):
            out = run("show", CLAUDE_NATIVE)[1]
        self.assertEqual(
            out.partition("\n\n")[2],
            "user:\none\ntwo\n\n[tool: Bash]\n[tool: Read]\n\nassistant:\ndone\n\n[tool: Edit]\n",
        )

    def test_show_stops_quietly_when_the_reader_goes_away(self):
        real, count = cli._out, []

        def closing(text):
            count.append(text)
            if len(count) > 3:
                raise BrokenPipeError(32, "Broken pipe")
            real(text)

        with mock.patch.object(cli, "_out", closing):
            code, out, err = run("show", CODEX_SEGMENTED)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 3)

    # --- list --keys --------------------------------------------------------

    def test_list_keys_adds_a_last_column(self):
        plain = run("list")[1].splitlines()
        keyed = run("list", "--keys")[1].splitlines()
        keys = [s.key for s in api.list_sessions(index=self.index)]
        self.assertEqual(len(plain), len(keys))
        self.assertEqual([line.split()[-1] for line in keyed], keys)
        for short, long, key in zip(plain, keyed, keys, strict=False):
            self.assertNotIn(key, short)
            self.assertTrue(long.startswith(short.rstrip()[:10]))
            self.assertEqual(" ".join(long.split()[:-1]), " ".join(short.split()))


class FindSessionTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        native = self.box.store(self.box.home)
        disk = self.box.mount("Disk")
        foreign = self.box.store(disk / "Users" / "u")
        self.box.session(native, "aaa-1")
        self.box.session(native, "both")
        self.box.session(foreign, "aaa-2")
        self.box.session(foreign, "both")
        api.scan(deep=True)
        self.index = api.load_index()
        self.native_key = make_key("fake", native, "both")
        self.foreign_key = make_key("fake", foreign, "both")

    def test_full_key_prefix_and_bare_id(self):
        find = lambda ref: api.find_session(ref, self.index)  # noqa: E731
        both = find(self.native_key)
        self.assertEqual((both.origin, both.id), ("native", "both"))
        self.assertEqual(find(self.foreign_key).origin, "foreign")
        self.assertIs(find(both), both)  # a Session passes through
        self.assertEqual(find(self.native_key[:-2]), both)  # unique prefix
        self.assertEqual(find("aaa-1").id, "aaa-1")
        self.assertEqual(find("  aaa-2\n").id, "aaa-2")
        self.assertEqual(api.find_session("aaa-1").id, "aaa-1")  # loads the index itself
        # An id listed natively and on a drive: the native copy.
        self.assertEqual(find("both"), both)

    def test_not_found(self):
        for ref, needle in (
            ("zzz", "no session matches 'zzz'"),
            ("", "no session key given"),
            (None, "no session key given"),
            ("aaa", "no session matches 'aaa'"),
            ("fake:", "'fake:' matches 4 sessions: fake:"),
            (self.native_key[:10], "matches 2 sessions"),
        ):
            with self.assertRaises(api.NotFound) as caught:
                api.find_session(ref, self.index)
            self.assertIn(needle, caught.exception.reason)
            self.assertIsInstance(caught.exception, api.ApiError)
        with self.assertRaises(api.NotFound) as caught:
            api.find_session(self.native_key[:10], self.index)
        self.assertIn(self.native_key, caught.exception.reason)
        with self.assertRaises(api.NotFound):
            list(api.transcript("zzz", self.index))

    def test_transcript_goes_through_the_adapter(self):
        turns = [api.Turn("user", "hello", None)]
        with mock.patch("support.FakeProvider.transcript", return_value=turns, create=True):
            self.assertEqual(list(api.transcript("aaa-1", self.index)), turns)


class BehindTheFacadeTest(unittest.TestCase):
    def test_the_command_line_reaches_the_engine_through_the_facade_alone(self):
        """AUDIT N-16: SPEC says neither front end reaches behind ``api``;
        ``cli`` imported ``mounts.MountsError`` and ``providers.all_providers``."""
        source = Path(cli.__file__).read_text(encoding="utf-8")
        engine = {"api", "fmt", "logs", "textsafe", "__version__", "tui"}
        imported = set()
        for line in source.splitlines():
            found = re.match(r"\s*from \.(\w*) import (.+)", line)
            if found and found.group(1):
                imported.add(found.group(1))
            elif found:
                imported |= {name.strip() for name in found.group(2).split(",")}
        typing_only = {"index", "launch", "model"}  # under TYPE_CHECKING: names for annotations
        self.assertEqual(imported - typing_only, engine)
        self.assertIs(api.MountsError, mounts.MountsError)
        self.assertIs(api.all_providers, providers.all_providers)
        self.assertLessEqual({"MountsError", "all_providers"}, set(api.__all__))
        self.assertFalse(hasattr(cli, "MountsError") or hasattr(cli, "all_providers"))

    def test_no_dead_code_is_left(self):
        """AUDIT N-18: three functions nothing called."""
        self.assertFalse(hasattr(cli, "_not_implemented"))
        self.assertFalse(hasattr(fsutil, "cache_dir"))
        self.assertFalse(hasattr(launch, "_spawn"))
        self.assertIs(launch.starter, launch._detached)  # the hook's default, under its one name


class AgeTest(unittest.TestCase):
    def test_ages(self):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        cases = [
            (timedelta(seconds=5), "now"),
            (timedelta(minutes=7), "7m"),
            (timedelta(hours=23), "23h"),
            (timedelta(days=2), "2d"),
            (timedelta(days=13), "13d"),
            (timedelta(days=21), "3w"),
            (timedelta(days=75), "2mo"),
            (timedelta(days=800), "2y"),
            (timedelta(days=-1), "now"),
        ]
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
