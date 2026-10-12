"""``export``: a transcript as Markdown or JSON lines (docs/SPEC.md "Continue
as a new session"): the documents ``export.markdown`` and ``export.json_lines``
make, the file ``export.write`` leaves, the facade (``api.export_transcript``,
``api.export_file``) and the command.

A transcript is text from a store, possibly one on another drive: the
Markdown holds nothing a terminal would act on, the JSON lines are exact
data. The machine is the one of tests/fixture_world.py."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CODEX_NATIVE,
    CODEX_WIN,
    GEMINI_WIN,
    GROK_NATIVE,
    GROK_WIN,
    World,
    tree_state,
)
from support import claude_transcript, state_names

from agent_sessions import __version__, api, cli, export, textsafe
from agent_sessions.errors import ExportFailed, NotFound, Offline
from agent_sessions.model import Session, Store
from agent_sessions.providers.base import Turn
from agent_sessions.providers.claude import Claude

ESC = "\x1b"
# Title and screen escapes, a C1 CSI, a bidirectional override, DEL.
HOSTILE = f"{ESC}]0;PWNED-TITLE\x07{ESC}[2J\x9b31m\u202e\x7f"
SID = "aaaaaaaa-0000-4000-8000-000000000001"
T1 = "2026-10-01T10:00:00.000000Z"
T2 = "2026-10-01T10:00:05.000000Z"


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


def session(**fields):
    """A session with the conversation fields of a test, some replaced."""
    values = {
        "id": SID,
        "title": "Fix the menu",
        "cwd": "F:\\Proj\\My Site",
        "started": T1,
        "updated": T2,
        "messages": 2,
    }
    values.update(fields)
    return Session.for_store(Store("claude", "/store"), **values)


def written(root) -> dict:
    """path -> (kind, size, mtime_ns) of everything below ``root``: what a
    write would change. (Access times are left out: looking at a symbolic
    link moves its own.)"""
    return {path: row[:3] for path, row in tree_state(root).items()}


def document(pieces) -> str:
    """What a file of these pieces holds."""
    return "".join(piece + "\n" for piece in pieces)


class MarkdownTest(unittest.TestCase):
    def test_header_sections_and_tool_lines(self):
        turns = [
            Turn("user", "first line\n\tsecond line", T1),
            Turn("assistant", "the answer", T2),
            Turn("tool", "Bash", T2),
            Turn("tool", "Read", None),
            Turn("assistant", "done", None),
            Turn("tool", "Edit", None),
            Turn("user", "thanks", None),
        ]
        self.assertEqual(
            document(export.markdown(session(), "Claude", turns)),
            "# Fix the menu\n"
            "\n"
            "- tool: Claude\n"
            f"- session: {SID}\n"
            "- folder: F:\\Proj\\My Site\n"
            f"- started: {T1}\n"
            f"- updated: {T2}\n"
            "- messages: 2\n"
            f"- exported by agent-sessions {__version__}\n"
            "\n"
            f"## User ({T1})\n"
            "\n"
            "first line\n"
            "\tsecond line\n"
            "\n"
            f"## Assistant ({T2})\n"
            "\n"
            "the answer\n"
            "\n"
            "> tool: Bash\n"
            "> tool: Read\n"
            "\n"
            "## Assistant\n"
            "\n"
            "done\n"
            "\n"
            "> tool: Edit\n"
            "\n"
            "## User\n"
            "\n"
            "thanks\n",
        )

    def test_what_is_not_known_is_said_so(self):
        empty = session(title="", cwd=None, started=None, updated=None, messages=None)
        (header,) = export.markdown(empty, "Grok", [])
        self.assertEqual(
            header.splitlines(),
            [
                f"# {SID}",  # no title: the id
                "",
                "- tool: Grok",
                f"- session: {SID}",
                "- folder: not recorded",
                "- started: unknown",
                "- updated: unknown",
                "- messages: unknown",
                f"- exported by agent-sessions {__version__}",
            ],
        )
        self.assertIn("- messages: 0\n", document(export.markdown(session(messages=0), "x", [])))

    def test_a_role_no_adapter_gives_today_still_gets_a_section(self):
        pieces = list(export.markdown(session(), "Claude", [Turn("system", "note", None)]))
        self.assertEqual(pieces[1], "\n## system\n\nnote")
        pieces = list(export.markdown(session(), "Claude", [Turn(HOSTILE, "note", None)]))
        self.assertEqual(pieces[1], f"\n## {textsafe.clean(HOSTILE).strip()}\n\nnote")
        pieces = list(export.markdown(session(), "Claude", [Turn("\n", "note", None)]))
        self.assertEqual(pieces[1], "\n## unknown\n\nnote")

    def test_nothing_in_it_is_acted_on_by_a_terminal_and_no_value_forges_a_line(self):
        evil = session(
            title=f"title{HOSTILE}\n## User\n\nforged",
            cwd=f"C:\\x{HOSTILE}\n- session: forged",
            started=f"{T1}\n- forged: 1",
            updated=f"{HOSTILE}",
        )
        turns = [
            Turn("user", f"line one{HOSTILE}\r\nline two\ttabbed\u2028same line", f"{T1}\n## x"),
            Turn("tool", f"Bash{ESC}[1m\n> tool: forged", None),
        ]
        pieces = list(export.markdown(evil, f"Claude\n- tool: forged{HOSTILE}", turns))
        text = document(pieces)
        self.assertEqual(unsafe_in(text, {"\n", "\t"}), [])
        lines = text.split("\n")
        # The header is its nine lines whatever the values hold.
        self.assertEqual(len(pieces[0].split("\n")), 9)
        self.assertEqual(lines[0], "# " + textsafe.clean(evil.title))
        self.assertEqual([line for line in lines if line.startswith("- tool:")], [lines[2]])
        self.assertEqual(len([line for line in lines if line.startswith("- session:")]), 1)
        self.assertEqual(len([line for line in lines if line.startswith("## ")]), 1)
        self.assertEqual(len([line for line in lines if line.startswith("> tool:")]), 1)
        # The text of a turn keeps its own lines and tabs, and only those.
        self.assertEqual(
            pieces[1].split("\n")[3:],
            [textsafe.clean(f"line one{HOSTILE} "), "line two\ttabbed same line"],
        )
        self.assertEqual(pieces[2], "\n> tool: " + textsafe.clean(turns[1].text))

    def test_it_is_made_piece_by_piece(self):
        pulled = []

        def turns():
            for number in range(1000):
                pulled.append(number)
                yield Turn("user", f"prompt {number}", None)

        pieces = export.markdown(session(), "Claude", turns())
        self.assertEqual(pulled, [])
        next(pieces)  # the header needs no turn
        self.assertEqual(pulled, [])
        self.assertEqual(next(pieces), "\n## User\n\nprompt 0")
        self.assertEqual(pulled, [0])


class JsonLinesTest(unittest.TestCase):
    def test_one_object_per_turn_with_exact_text(self):
        turns = [
            Turn("user", f'a{HOSTILE}\r\nb\u2028c\ufeff\t"q" \\ é 漢 😀', T1),
            Turn("assistant", "", None),
            Turn("tool", f"Bash{ESC}[1m", T2),
        ]
        lines = list(export.json_lines(turns))
        self.assertEqual(len(lines), 3)
        for line, turn in zip(lines, turns, strict=True):
            self.assertEqual(unsafe_in(line), [])  # no line break either: one line is one turn
            row = json.loads(line)
            self.assertEqual(row, {"role": turn.role, "text": turn.text, "ts": turn.ts})
            self.assertEqual(list(row), ["role", "text", "ts"])
        self.assertIn("é 漢 😀", lines[0])  # text that is harmless stays as it is
        self.assertEqual(lines[1], '{"role": "assistant", "text": "", "ts": null}')

    def test_it_is_made_line_by_line(self):
        pulled = []

        def turns():
            for number in range(1000):
                pulled.append(number)
                yield Turn("user", "x", None)

        lines = export.json_lines(turns())
        self.assertEqual(pulled, [])
        next(lines)
        self.assertEqual(pulled, [0])


class WriteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agent-sessions-test-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.file = self.dir / "out.md"
        mask = os.umask(0o022)  # whatever the mask, the file is for its owner alone
        self.addCleanup(os.umask, mask)

    def refused(self, path, pieces=("x",), **kwargs) -> str:
        with self.assertRaises(ExportFailed) as caught:
            export.write(path, pieces, **kwargs)
        return caught.exception.reason

    def test_the_file_is_the_pieces_each_on_its_line_and_private(self):
        size = export.write(self.file, ["# title", "\n## User\n\ntext é"])
        data = self.file.read_bytes()
        self.assertEqual(data, "# title\n\n## User\n\ntext é\n".encode())
        self.assertEqual(size, len(data))
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(os.listdir(self.dir), ["out.md"])  # no temp file is left
        # A generator is taken as it comes; no piece gives an empty file.
        self.assertEqual(export.write(self.dir / "empty", iter(())), 0)
        self.assertEqual((self.dir / "empty").read_bytes(), b"")

    def test_one_spelling_of_the_path(self):
        export.write(f"{self.dir}//sub/../out.md", ["x"])
        self.assertEqual(self.file.read_bytes(), b"x\n")

    def test_a_file_that_exists_is_not_replaced_unless_asked(self):
        self.file.write_text("mine", encoding="utf-8")
        self.file.chmod(0o644)
        before = self.file.stat()
        self.assertEqual(
            self.refused(self.file),
            f"{self.file} exists; nothing was written (--force replaces it)",
        )
        self.assertEqual(self.file.read_text(encoding="utf-8"), "mine")
        self.assertEqual(self.file.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(os.listdir(self.dir), ["out.md"])
        # Asked: replaced whole, and private whatever mode the old one had.
        self.assertEqual(export.write(self.file, ["new"], overwrite=True), 4)
        self.assertEqual(self.file.read_bytes(), b"new\n")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertNotEqual(self.file.stat().st_ino, before.st_ino)  # moved into place
        self.assertEqual(os.listdir(self.dir), ["out.md"])

    def test_a_file_that_appears_at_the_last_moment_is_not_replaced(self):
        def pieces():
            yield "first"
            self.file.write_text("theirs", encoding="utf-8")  # while ours is being written
            yield "second"

        self.assertIn("exists; nothing was written", self.refused(self.file, pieces()))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "theirs")
        self.assertEqual(os.listdir(self.dir), ["out.md"])

    def test_where_there_are_no_hard_links(self):
        for error in (PermissionError(1, "Operation not permitted"), NotImplementedError()):
            with mock.patch.object(export.os, "link", side_effect=error):
                target = self.dir / type(error).__name__
                self.assertEqual(export.write(target, ["x"]), 2)
                self.assertEqual(target.read_bytes(), b"x\n")
                # The name is looked at first, then.
                self.assertIn("exists; nothing was written", self.refused(target, ["y"]))
                self.assertEqual(target.read_bytes(), b"x\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["NotImplementedError", "PermissionError"])

    def test_paths_that_will_not_do(self):
        (self.dir / "sub").mkdir()
        os.mkfifo(self.dir / "pipe")
        os.symlink(self.dir / "nowhere", self.dir / "link")
        cases = (
            ("out.md", "the file to write must be an absolute path: out.md"),
            ("", "the file to write must be an absolute path: "),
            (None, "the file to write is not a path"),
            (7, "the file to write is not a path"),
            (f"{self.file}\0", "the file to write must be an absolute path: "),
            (
                self.dir / "none" / "out.md",
                f"the directory {self.dir / 'none'} does not exist; nothing was written",
            ),
            (self.dir / "sub", f"{self.dir / 'sub'} is a directory; nothing was written"),
            (self.dir / "pipe", f"{self.dir / 'pipe'} is not a regular file; nothing was written"),
            (self.dir / "link", f"{self.dir / 'link'} is not a regular file; nothing was written"),
        )
        before = written(self.dir)
        for path, reason in cases:
            for overwrite in (False, True):
                with self.subTest(path=path, overwrite=overwrite):
                    pulled = []
                    pieces = (pulled.append(1) or "x" for _ in range(3))
                    self.assertIn(reason, self.refused(path, pieces, overwrite=overwrite))
                    self.assertEqual(pulled, [])  # refused before the transcript is read
        self.assertEqual(written(self.dir), before)

    def test_a_write_that_fails_leaves_the_old_file_and_no_temp_file(self):
        self.file.write_text("mine", encoding="utf-8")

        def broken():
            yield "half"
            raise OSError(28, "No space left on device")

        self.assertEqual(
            self.refused(self.file, broken(), overwrite=True),
            f"{self.file} was not written: No space left on device; nothing was left behind",
        )

        def interrupted():
            yield "half"
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            export.write(self.file, interrupted(), overwrite=True)
        with self.assertRaises(ZeroDivisionError):
            export.write(self.dir / "new", (1 // 0 for _ in range(1)))
        self.assertEqual(self.file.read_text(encoding="utf-8"), "mine")
        self.assertEqual(os.listdir(self.dir), ["out.md"])
        # A directory that takes no new file.
        locked = self.dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        if os.access(locked, os.W_OK):
            self.skipTest("this user writes everywhere")
        self.assertIn("was not written: Permission denied", self.refused(locked / "out.md"))

    def test_text_that_is_not_unicode_is_escaped_not_fatal(self):
        (line,) = export.json_lines([Turn("user", "half \ud83d pair", None)])
        export.write(self.file, [line, "markdown \udc80"])
        first, second = self.file.read_bytes().split(b"\n")[:2]
        self.assertEqual(json.loads(first)["text"], "half \ud83d pair")  # the same text again
        self.assertEqual(second, b"markdown \\udc80")


class ExportCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("claude", "codex", "grok", "antigravity", "gemini")
    native: tuple[str, ...] = ("claude", "codex", "grok")

    def setUp(self):
        self.w = World(self, self.foreign, self.native)
        self.home = self.w.home
        self.index = self.w.scan()
        self.out = self.home / "out"
        self.out.mkdir()


class FacadeTest(ExportCase):
    def test_every_provider_exports_the_turns_its_transcript_has(self):
        for session_id, label in (
            (CLAUDE_NATIVE, "Claude"),
            (CLAUDE_WIN, "Claude"),
            (CODEX_NATIVE, "Codex"),
            (CODEX_WIN, "Codex"),
            (GROK_NATIVE, "Grok"),
            (GROK_WIN, "Grok"),
            (AGY_WIN, "Antigravity"),
            (GEMINI_WIN, "Gemini"),
        ):
            with self.subTest(session_id):
                found = self.w.session(session_id, index=self.index)
                turns = list(api.transcript(found, index=self.index))
                self.assertTrue(turns)
                pieces = list(api.export_transcript(found.key, index=self.index))
                self.assertEqual(
                    pieces, list(export.markdown(found, label, turns)), "the document of its turns"
                )
                text = document(pieces)
                self.assertIn(f"\n- tool: {label}\n- session: {session_id}\n", text)
                self.assertEqual(
                    text.count("\n## User"), sum(turn.role == "user" for turn in turns)
                )
                self.assertEqual(
                    text.count("\n## Assistant"), sum(turn.role == "assistant" for turn in turns)
                )
                self.assertEqual(
                    text.count("\n> tool: "), sum(turn.role == "tool" for turn in turns)
                )
                rows = [json.loads(line) for line in api.export_transcript(found, "json")]
                self.assertEqual(rows, [turn._asdict() for turn in turns])
        self.assertEqual(state_names(), [])

    def test_refused_at_once_and_read_only_when_asked_for(self):
        with self.assertRaises(NotFound):
            api.export_transcript("claude:0000")
        for fmt in ("", "markdown", "MD", "jsonl", None, 7):
            with self.assertRaises(ExportFailed) as caught:
                api.export_transcript(CLAUDE_NATIVE, fmt)
            self.assertIn("it is one of: md, json", caught.exception.reason)
        self.assertEqual(api.EXPORT_FORMATS, ("md", "json"))
        read = []

        def transcript(provider, found):
            read.append(found.id)
            yield Turn("user", "x", None)

        with mock.patch.object(Claude, "transcript", transcript):
            pieces = api.export_transcript(CLAUDE_NATIVE)
            self.assertEqual(read, [])  # nothing was read to make the call
            next(pieces)
            self.assertEqual(read, [])  # nor for the header
            next(pieces)
            self.assertEqual(read, [CLAUDE_NATIVE])

    def test_an_offline_session_is_refused(self):
        self.w.box.unmount(self.w.c)
        offline = self.w.session(CLAUDE_WIN, "foreign")
        self.assertTrue(offline.offline)
        for fmt in ("md", "json"):
            with self.assertRaises(Offline) as caught:
                api.export_transcript(offline.key, fmt)
            self.assertEqual(caught.exception.reason, "the drive WinC is not mounted")
        with self.assertRaises(Offline):
            api.export_file(offline.key, str(self.out / "x.md"))
        self.assertEqual(os.listdir(self.out), [])

    def test_export_file(self):
        target = self.out / "session.md"
        size = api.export_file(CLAUDE_NATIVE, str(target))
        self.assertEqual(target.read_text(encoding="utf-8"), document(self.pieces()))
        self.assertEqual(
            (size, stat.S_IMODE(target.stat().st_mode)), (target.stat().st_size, 0o600)
        )
        with self.assertRaises(ExportFailed) as caught:
            api.export_file(GROK_NATIVE, str(target))
        self.assertIn("exists; nothing was written", caught.exception.reason)
        api.export_file(GROK_NATIVE, str(target), fmt="json", force=True)
        rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows, [turn._asdict() for turn in api.transcript(GROK_NATIVE)])
        self.assertEqual(os.listdir(self.out), ["session.md"])
        for bad in ("relative.md", "", None, str(self.out / "none" / "x.md"), str(self.out)):
            with self.assertRaises(ExportFailed):
                api.export_file(CLAUDE_NATIVE, bad, force=True)
        with self.assertRaises(NotFound):
            api.export_file("claude:0000", str(self.out / "x.md"))
        self.assertEqual(os.listdir(self.out), ["session.md"])
        self.assertEqual(state_names(), [])

    def pieces(self, session_id=CLAUDE_NATIVE, fmt="md"):
        return list(api.export_transcript(session_id, fmt))

    def test_nothing_is_written_into_a_session_store(self):
        native = self.home / ".claude"
        foreign = self.w.profile / ".claude"
        link = self.home / "elsewhere"
        os.symlink(foreign / "projects", link)
        before = written(self.w.c), written(self.home)
        existing = self.w.session(CLAUDE_WIN, "foreign").files[0]
        for path, store in (
            (native / "x.md", native),
            (native / "projects" / "deep" / "x.md", native),
            (foreign / "x.md", foreign),
            (Path(existing), foreign),  # a session's own file, with --force
            (link / "x.md", foreign),  # whatever link leads there
        ):
            for force in (False, True):
                with self.subTest(path=path, force=force):
                    with self.assertRaises(ExportFailed) as caught:
                        api.export_file(CLAUDE_NATIVE, str(path), force=force)
                    self.assertEqual(
                        caught.exception.reason,
                        f"{path} is inside the session store {store}; nothing was written",
                    )
        self.assertEqual((written(self.w.c), written(self.home)), before)
        # A folder that only starts like a store is not one.
        beside = self.home / ".claude-notes"
        beside.mkdir()
        api.export_file(CLAUDE_NATIVE, str(beside / "x.md"))


class CommandTest(ExportCase):
    foreign = ("claude", "grok")
    native = ("claude", "grok")

    def test_markdown_to_stdout(self):
        for ref in (CLAUDE_NATIVE, self.w.session(GROK_WIN, "foreign").key):
            code, out, err = run("export", ref)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, document(api.export_transcript(ref)))
            self.assertEqual(run("export", ref, "--format", "md"), (0, out, ""))
        self.assertEqual(state_names(), [])

    def test_json_lines_to_stdout(self):
        code, out, err = run("export", GROK_NATIVE, "--format", "json")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("\n"))
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(rows, [turn._asdict() for turn in api.transcript(GROK_NATIVE)])
        self.assertGreater(len(rows), 1)

    def test_to_a_file(self):
        target = self.out / "s.md"
        code, out, err = run("export", CLAUDE_NATIVE, "-o", str(target))
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, f"agent-sessions: wrote {target} ({target.stat().st_size} B)\n")
        self.assertEqual(target.read_text(encoding="utf-8"), run("export", CLAUDE_NATIVE)[1])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        # It is there now: refused, and replaced only with --force.
        code, out, err = run("export", GROK_NATIVE, "-o", str(target), "--format", "json")
        self.assertEqual(
            (code, out, err),
            (
                1,
                "",
                f"agent-sessions: {target} exists; nothing was written (--force replaces it)\n",
            ),
        )
        self.assertEqual(target.read_text(encoding="utf-8"), run("export", CLAUDE_NATIVE)[1])
        code, out, err = run(
            "export", GROK_NATIVE, "-o", str(target), "--format", "json", "--force"
        )
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            run("export", GROK_NATIVE, "--format", "json")[1],
        )
        self.assertEqual(os.listdir(self.out), ["s.md"])

    def test_a_relative_path_and_a_tilde_are_the_commands_to_resolve(self):
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(self.out)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(run("export", CLAUDE_NATIVE, "-o", "rel.md")[0], 0)
            self.assertEqual(run("export", CLAUDE_NATIVE, "-o", "~/out/home.md")[0], 0)
        self.assertEqual(sorted(os.listdir(self.out)), ["home.md", "rel.md"])

    def test_refusals_exit_one_with_the_reason(self):
        self.assertEqual(
            run("export", CLAUDE_NATIVE, "-o", str(self.out)),
            (1, "", f"agent-sessions: {self.out} is a directory; nothing was written\n"),
        )
        code, out, err = run("export", "claude:0000")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no session matches", err)
        self.w.box.unmount(self.w.c)
        for extra in ((), ("--format", "json"), ("-o", str(self.out / "x.md"))):
            self.assertEqual(
                run("export", self.w.session(CLAUDE_WIN, "foreign").key, *extra),
                (1, "", "agent-sessions: the drive WinC is not mounted\n"),
            )
        self.assertEqual(os.listdir(self.out), [])
        # A format that is none is a usage error.
        code, out, err = run("export", CLAUDE_NATIVE, "--format", "html")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("invalid choice: 'html'", err)

    def test_what_a_hostile_session_holds_is_safe_on_the_terminal_and_exact_in_json(self):
        reply = f"reply {ESC}]52;c;ZWNobyBwd25lZA==\x07 {ESC}[31mred\ttab\nline2\u202e"
        claude_transcript(
            self.home / ".claude",
            SID,
            f"/home/user/pro{HOSTILE}j\n- session: forged",
            prompt="what is this\u202e",
            reply=reply,
            tool=f"Bash{ESC}[1m",
        )
        self.w.scan()
        code, out, err = run("export", SID)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(unsafe_in(out, {"\n", "\t"}), [])
        self.assertIn("red\ttab\nline2 \n", out)
        self.assertIn("\n> tool: " + textsafe.clean(f"Bash{ESC}[1m") + "\n", out)
        self.assertEqual(out.count("- session: "), 2)  # the forged one is not a line of its own
        self.assertEqual(len([line for line in out.split("\n") if line.startswith("- ses")]), 1)
        code, out, err = run("export", SID, "--format", "json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(unsafe_in(out, {"\n"}), [])
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(
            [(row["role"], row["text"]) for row in rows],
            [("user", "what is this\u202e"), ("assistant", reply), ("tool", f"Bash{ESC}[1m")],
        )
        # The file holds the same documents.
        for fmt in ("md", "json"):
            target = self.out / f"evil.{fmt}"
            self.assertEqual(run("export", SID, "-o", str(target), "--format", fmt)[0], 0)
            self.assertEqual(
                target.read_text(encoding="utf-8"), run("export", SID, "--format", fmt)[1]
            )


if __name__ == "__main__":
    unittest.main()
