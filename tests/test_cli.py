"""cli.py: every subcommand but tui, through main()."""

import contextlib
import io
import json
import shlex
import shutil
import sys
import unittest
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
from support import Sandbox, state_names

from agent_sessions import api, cli, launch
from agent_sessions.fsutil import cache_file
from agent_sessions.index import Index
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
        which = mock.patch(
            "agent_sessions.index.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"
        )
        which.start()
        self.addCleanup(which.stop)
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
            good.replace('"version":1', '"version":0'),
            good[: len(good) // 2],
        ):
            for argv, expected in ((("list",), want), (("list", "--json"), want_json)):
                with self.subTest(damage=damage[:20], argv=argv):
                    cache_file().write_text(damage, encoding="utf-8")
                    self.assertRegex(Index.load().load_error, r"^unreadable: .+")
                    self.assertEqual(api.load_index().sessions, [])
                    self.assertEqual(run(*argv), (0, expected[1], self.NOTICE))
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
        with mock.patch(
            "agent_sessions.api.shutil.which",
            side_effect=lambda name: None if name == "crush" else f"/bin/{name}",
        ):
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
                "providers",
                "stores",
                "skipped_stores",
                "letters",
                "unresolved_letters",
                "no_reader",
                "warnings",
            },
        )
        self.assertEqual(
            [t["tool"] for t in status["no_reader"]], ["copilot", "cursor-agent", "opencode"]
        )
        self.assertEqual(
            status["providers"],
            [
                {
                    "name": "fake",
                    "label": "Fake",
                    "binary": "fake-agent",
                    "installed": "/bin/fake-agent",
                    "resumable": True,
                    "reader": True,
                    "stores": 2,
                    "sessions": 4,
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

        code, out, err = run("import", GROK_WIN, "--dry-run")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Grok's directory encoding of '-', '.', '_', '~' is unverified", err)

        # Nothing was written anywhere: the stores are as the fixtures were.
        self.assertEqual(state_names(), [])  # the log apart
        self.assertNotIn(CLAUDE_WIN, " ".join(files_below(self.home / ".claude")))
        self.assertNotIn(CODEX_WIN, " ".join(files_below(self.home / ".codex")))

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
            (AGY_WIN, "Antigravity keeps conversations in SQLite files"),
            (GEMINI_WIN, "Gemini CLI is not installed on this machine"),
            (GROK_WIN, "Grok's directory encoding"),
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
                "mounted drive\n",
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
