"""textsafe.py, and everything that shows text from a store: the CLI's
output, titles, the desktop notification. docs/REVIEW.md R-03 and R-09.

The machine is the one of tests/fixture_world.py with a third drive whose
mount path and label hold escape sequences, and Claude sessions whose folder,
prompt, reply and tool name do."""

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import World
from support import FIXTURES, claude_transcript, write_jsonl

from agent_sessions import cli, launch, textsafe
from agent_sessions.model import Store
from agent_sessions.providers import claude as claude_mod
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import clean_title
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok
from agent_sessions.tui import view

ESC = "\x1b"
# Title and screen escapes, a C1 CSI, a bidirectional override, DEL.
HOSTILE = f"{ESC}]0;PWNED-TITLE\x07{ESC}[2J\x9b31m\u202e\x7f"
ALLOWED_BREAKS = {"\n", "\t"}

A = "aaaaaaaa-0000-4000-8000-000000000001"  # foreign, on the hostile drive
B = "bbbbbbbb-0000-4000-8000-000000000002"  # foreign, its folder exists
C = "cccccccc-0000-4000-8000-000000000003"  # native


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def unsafe_in(text: str, allowed=frozenset()) -> list[str]:
    return sorted({ch for ch in text if ch in textsafe.UNSAFE and ch not in allowed})


class CleanTest(unittest.TestCase):
    def test_every_unsafe_character_becomes_one_space(self):
        for ch in sorted(textsafe.UNSAFE):
            self.assertEqual(textsafe.clean(f"a{ch}b"), "a b", hex(ord(ch)))
        self.assertEqual(len(textsafe.clean(HOSTILE)), len(HOSTILE))
        self.assertEqual(unsafe_in(textsafe.clean(HOSTILE)), [])

    def test_the_set_is_what_the_module_says(self):
        points = {ord(ch) for ch in textsafe.UNSAFE}
        wanted = set(range(0x20)) | set(range(0x7F, 0xA0))
        wanted |= {0x061C, 0x200E, 0x200F, 0x2028, 0x2029, 0xFEFF}
        wanted |= set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
        self.assertEqual(points, wanted)

    def test_other_text_is_left_alone(self):
        for text in (
            "",
            "plain",
            "日本語 ok",
            "é ñ ü",
            "👩‍💻 zero-width joiner",
            "a\u00a0b",
            "\udcff",
        ):
            self.assertEqual(textsafe.clean(text), text)
            self.assertEqual(textsafe.clean(text, multiline=True), text)
        self.assertEqual(textsafe.clean(None), "")
        self.assertEqual(textsafe.clean(12), "12")

    def test_multiline_keeps_line_feed_and_tab_only(self):
        self.assertEqual(
            textsafe.clean(f"a\n\tb\r\n{ESC}[0mc\u2028d\x0be", multiline=True),
            "a\n\tb \n [0mc d e",
        )
        self.assertEqual(textsafe.clean("a\n\tb"), "a  b")

    def test_view_and_titles_use_the_same_rule(self):
        self.assertEqual(view.clean(HOSTILE), textsafe.clean(HOSTILE))
        self.assertEqual(view.clean("a\nb\tc\x1b[0m"), "a b c [0m")  # as before
        self.assertEqual(clean_title(f"  fix{ESC}[2J the\u202e \x9bbug\u2029 "), "fix [2J the bug")
        self.assertEqual(unsafe_in(clean_title(HOSTILE * 5)), [])


class JsonEscapeTest(unittest.TestCase):
    def test_round_trip_is_exact_and_nothing_unsafe_is_left(self):
        value = {
            "every": "".join(sorted(textsafe.UNSAFE)),
            f"key{HOSTILE}": [HOSTILE, f"back\\slash\\{HOSTILE}", '"quoted"', "日本語", "\udcff"],
            "nested": {"n": 1, "f": 1.5, "t": True, "z": None},
        }
        for options in ({}, {"indent": 2, "sort_keys": True}):
            document = json.dumps(value, ensure_ascii=False, **options)
            escaped = textsafe.json_escape(document)
            self.assertEqual(json.loads(escaped), value)
            self.assertEqual(unsafe_in(escaped, {"\n"}), [])
        self.assertIn("\\u009b", textsafe.json_escape(json.dumps("\x9b", ensure_ascii=False)))
        self.assertIn("\\u202e", textsafe.json_escape(json.dumps("\u202e", ensure_ascii=False)))

    def test_a_document_without_such_characters_is_unchanged(self):
        document = json.dumps({"a": ["b", "日本語", 1]}, ensure_ascii=False, indent=2)
        self.assertEqual(textsafe.json_escape(document), document)


class CliBoundaryTest(unittest.TestCase):
    """How forgetting is made hard: only the output helpers write to a
    stream, so a new field cannot reach the terminal around them."""

    WRITERS = {"_emit", "_progress", "_ask"}

    def test_only_the_output_helpers_write(self):
        source = Path(cli.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                if isinstance(target, ast.Name) and target.id == "print":
                    offenders.append((function.name, node.lineno, "print"))
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in ("write", "writelines")
                    and function.name not in self.WRITERS
                ):
                    offenders.append((function.name, node.lineno, target.attr))
        self.assertEqual(offenders, [])
        self.assertEqual(source.count("print("), 0)

    def test_the_helpers_make_their_text_safe(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli._out(f"row{HOSTILE}\nforged row")
            cli._out(f"text{HOSTILE}\n\tmore", multiline=True)
            cli._err(f"reason{HOSTILE}\nmore")
            cli._json({"cwd": HOSTILE})
        lines = out.getvalue().split("\n")
        self.assertEqual(lines[0], textsafe.clean(f"row{HOSTILE} forged row"))
        self.assertEqual(lines[1:3], [textsafe.clean(f"text{HOSTILE}"), "\tmore"])
        self.assertEqual(json.loads("\n".join(lines[3:])), {"cwd": HOSTILE})
        self.assertEqual(
            err.getvalue(), textsafe.clean(f"agent-sessions: reason{HOSTILE} more") + "\n"
        )
        self.assertEqual(unsafe_in(out.getvalue() + err.getvalue(), ALLOWED_BREAKS), [])

    def test_the_progress_line_carries_only_its_own_escape(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._progress(f"scanning Evil{HOSTILE}… 12 dirs")
            cli._progress()
        first, second = err.getvalue().split("\r\x1b[K")[1:]
        self.assertEqual(first, textsafe.clean(f"scanning Evil{HOSTILE}… 12 dirs"))
        self.assertEqual(second, "")


class HostileStoreTest(unittest.TestCase):
    """Every command, on stores that hold escape sequences wherever a store
    can hold text."""

    def setUp(self):
        self.w = World(self, foreign=())
        self.home = self.w.home
        self.evil = self.w.box.mount(f"Evil{ESC}[2JDisk", label=f"Evil\x9b1mLabel{ESC}c")
        self.evil_store = self.evil / "Users" / "u" / ".claude"
        self.cwd_a = f"C:\\Users\\u\\proj{HOSTILE}\n1m   Linux  claude  forged row"
        self.reply = f"reply {ESC}]52;c;ZWNobyBwd25lZA==\x07 {ESC}[31mred\ttab\nline2\u202e"
        for project in ("C--Users-u", "C--Users-u-copy"):  # the second copy gives a warning
            claude_transcript(
                self.evil_store,
                A,
                self.cwd_a,
                prompt="what is this\u202e",
                reply=self.reply,
                project=project,
                tool=f"Bash{ESC}[1m",
            )
        folder = self.w.f / "Proj" / f"ev{ESC}[2Jil"
        folder.mkdir(parents=True)
        self.cwd_b = f"F:\\Proj\\ev{ESC}[2Jil"
        claude_transcript(self.w.profile / ".claude", B, self.cwd_b, project="F--Proj-ev")
        self.cwd_c = self.home / f"pro{ESC}[2Jj"
        self.cwd_c.mkdir()
        claude_transcript(self.home / ".claude", C, str(self.cwd_c))
        self.w.override({"F:": str(self.w.f)})
        self.index = self.w.scan()
        self.assertEqual(sorted(s.id for s in self.index.sessions), [A, B, C])

    def assert_safe(self, *texts):
        for text in texts:
            self.assertEqual(unsafe_in(text, ALLOWED_BREAKS), [], repr(text))

    def test_the_stores_do_hold_them(self):
        # Without this the other tests would pass on harmless data.
        a = self.w.session(A)
        self.assertEqual(a.cwd, self.cwd_a)
        self.assertIn(ESC, a.store)
        self.assertIn(ESC, a.mount_label)
        self.assertTrue(any(ESC in warning for warning in self.index.warnings))

    def test_readable_output_of_every_command(self):
        a, b, c = (self.w.session(ident) for ident in (A, B, C))
        commands = [
            ("scan",),
            ("scan", "--deep"),
            ("list",),
            ("list", "--keys"),
            ("list", "--provider", "claude"),
            ("status",),
            ("verify",),
            ("show", a.key),
            ("show", c.key),
            ("export", a.key),
            ("export", a.key, "--format", "json"),
            ("export", c.key),
            ("continue", c.key, "--dry-run"),
            ("continue", a.key, "--dry-run"),
            ("continue", c.key),
            ("open", c.key, "--dry-run"),
            ("open", a.key, "--dry-run"),
            ("open", a.key),
            ("import", b.key, "--dry-run"),
            ("import", a.key, "--dry-run"),
            ("import", a.key),
            ("open", f"nothing{HOSTILE}"),
        ]
        seen = ""
        for argv in commands:
            code, out, err = run(*argv)
            self.assertIn(code, (0, 1), argv)
            self.assertNotIn("Traceback", err, argv)
            self.assert_safe(out, err)
            seen += out + err
        # The values were printed, with the escapes taken out of them.
        for value in (
            self.cwd_a,
            self.cwd_b,
            str(self.cwd_c),
            a.store,
            a.mount_label,
            f"[tool: Bash{ESC}[1m]",
        ):
            self.assertIn(textsafe.clean(value), seen, repr(value))
        self.assertIn("red\ttab\nline2 \n", seen)  # transcript text keeps its lines
        self.assert_safe(*self.w.notified)
        self.assertTrue(self.w.notified)

    def test_list_does_not_let_a_value_forge_a_row(self):
        code, out, _err = run("list", "--keys")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)
        self.assertEqual(len(out.split("\n")), 4)

    def test_json_output_is_exact_and_safe_to_print(self):
        code, out, err = run("list", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(unsafe_in(out, {"\n"}), [])
        self.assertEqual(json.loads(out), [s.to_dict() for s in self.index.sessions])
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertEqual(rows[A]["cwd"], self.cwd_a)
        self.assertEqual(rows[A]["mount_label"], f"Evil\x9b1mLabel{ESC}c")
        self.assertEqual(rows[C]["cwd_local"], str(self.cwd_c))
        for argv in (("status", "--json"), ("scan", "--json"), ("scan", "--deep", "--json")):
            code, out, err = run(*argv)
            self.assertEqual((code, err), (0, ""), argv)
            self.assertEqual(unsafe_in(out, {"\n"}), [], argv)
            data = json.loads(out)
            self.assertTrue(any(ESC in warning for warning in data["warnings"]), argv)


class IdFallbackTitleTest(unittest.TestCase):
    """A session with no title and no prompt is titled by the start of its
    id. Each adapter is asked directly: the index would refuse these ids."""

    BAD = f"{ESC}c{ESC}[2Jabcdef-not-a-uuid"
    CLEAN = "c [2Jab"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def title(self, provider, root) -> str:
        store = Store(provider.name, str(root), "foreign")
        sessions = provider.list_sessions(store)
        self.assertEqual(len(sessions), 1, sessions)
        self.assertEqual(sessions[0].id, self.BAD)
        return sessions[0].title

    def test_claude(self):
        title, source = claude_mod._title(claude_mod._Scan(), self.BAD)
        self.assertEqual((title, source), (self.CLEAN, "id"))

    def test_codex(self):
        root = self.tmp / ".codex"
        name = "rollout-2026-08-29T13-57-23-01a04ca1-6e27-7817-9f54-07105f962073.jsonl"
        reply = {"type": "AgentMessage", "content": [{"type": "text", "text": "hi"}]}
        write_jsonl(
            root / "sessions" / "2026" / "08" / "29" / name,
            [
                {
                    "timestamp": "2026-08-29T08:31:14Z",
                    "type": "session_meta",
                    "payload": {"id": self.BAD},
                },
                {
                    "timestamp": "2026-08-29T08:31:15Z",
                    "type": "event_msg",
                    "payload": {"type": "item_completed", "item": reply},
                },
            ],
        )
        self.assertEqual(self.title(Codex(), root), self.CLEAN)

    def test_grok(self):
        root = self.tmp / ".grok"
        session = root / "sessions" / "%2Fhome%2Fu" / "01a08073-6af7-7c7c-9dba-e3859e9a2ac8"
        write_jsonl(session / "chat_history.jsonl", [{"type": "assistant", "content": "hi"}])
        (session / "summary.json").write_text(
            json.dumps({"info": {"id": self.BAD, "cwd": "/home/u"}}), encoding="utf-8"
        )
        self.assertEqual(self.title(Grok(), root), self.CLEAN)

    def test_antigravity(self):
        root = self.tmp / "antigravity-cli"
        (root / "conversations").mkdir(parents=True)
        source = FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli"
        shutil.copy(
            source / "conversations" / "45184a9e-8a4e-4f3f-8c4c-f0e85aeecaaf.db",
            root / "conversations" / f"{self.BAD}.db",
        )
        self.assertEqual(self.title(Antigravity(), root), self.CLEAN)

    def test_gemini(self):
        root = self.tmp / ".gemini"
        write_jsonl(
            root / "tmp" / "proj" / "chats" / "session-2026-08-30T10-00-5b0c3e6a.jsonl",
            [
                {"sessionId": self.BAD, "projectHash": "h", "startTime": "2026-08-30T10:00:00Z"},
                {
                    "id": "m1",
                    "type": "user",
                    "content": [{"inlineData": {"mimeType": "image/png"}}],
                },
            ],
        )
        self.assertEqual(self.title(Gemini(), root), self.CLEAN)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"AGENT_SESSIONS_NOTIFY": "1"})
        env.start()
        self.addCleanup(env.stop)
        which = mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
        which.start()
        self.addCleanup(which.stop)

    def body(self, reason) -> list[str]:
        with mock.patch.object(launch.subprocess, "Popen") as popen:
            launch._notify(reason)
        popen.assert_called_once()
        return popen.call_args[0][0]

    def test_the_body_is_one_safe_line(self):
        command = self.body(f"the project folder C:\\x{HOSTILE}\nis gone")
        self.assertEqual(
            command[:3], ["/usr/bin/notify-send", "--app-name=agent-sessions", launch.NOTIFY_TITLE]
        )
        self.assertEqual(len(command), 4)
        self.assertEqual(unsafe_in(command[3]), [])
        self.assertEqual(command[3], textsafe.clean(f"the project folder C:\\x{HOSTILE} is gone"))

    def test_no_text_can_be_read_as_an_option(self):
        for reason in ("--action=x=y", "-u critical", f"{ESC}--hint=string:x:y", "  --wait", "-"):
            command = self.body(reason)
            self.assertEqual(len(command), 4)
            self.assertFalse(command[3].startswith("-"), repr(command[3]))
            self.assertEqual(command[3].strip(), textsafe.clean(reason).strip())
        # Everything before the body is this module's own text.
        self.assertEqual(self.body("plain")[3], "plain")

    def test_a_replaced_notifier_gets_clean_text_too(self):
        seen = []
        with mock.patch.object(launch, "notifier", seen.append):
            launch._tell(f"folder{HOSTILE}gone")
        self.assertEqual(seen, [textsafe.clean(f"folder{HOSTILE}gone")])


if __name__ == "__main__":
    unittest.main()
