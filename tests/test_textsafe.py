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
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import World
from support import CHILD_TIMEOUT, FIXTURES, LAUNCHER, claude_transcript, write_jsonl

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


# The categories ``textsafe`` replaces: controls, format characters,
# surrogates, the line and the paragraph separator.
REPLACED = ("Cc", "Cf", "Cs", "Zl", "Zp")
TAGS = range(0xE0000, 0xE0080)
UNICODE = tuple(int(part) for part in unicodedata.unidata_version.split("."))


class CleanTest(unittest.TestCase):
    def test_every_unsafe_character_becomes_one_space(self):
        for ch in sorted(textsafe.UNSAFE):
            self.assertEqual(textsafe.clean(f"a{ch}b"), "a b", hex(ord(ch)))
            self.assertEqual(textsafe.clean(f"a{ch}b", multiline=True), "a b", hex(ord(ch))) if (
                ch not in "\n\t"
            ) else None
        self.assertEqual(len(textsafe.clean(HOSTILE)), len(HOSTILE))
        self.assertEqual(unsafe_in(textsafe.clean(HOSTILE)), [])

    def test_the_set_is_what_the_module_says(self):
        """Held against this Python's own Unicode tables, not against a copy
        of the list: every character of the five categories is in the set
        (for a Unicode no newer than the one the list was made from), and
        nothing else is but the unassigned rest of the tag block."""
        by_category = {
            chr(point)
            for point in range(sys.maxunicode + 1)
            if unicodedata.category(chr(point)) in REPLACED
        }
        if UNICODE <= (16, 0, 0):
            self.assertEqual(sorted(by_category - textsafe.UNSAFE), [])
        extra = textsafe.UNSAFE - by_category
        # What this Python's older tables do not know yet, and the tag block.
        self.assertEqual({unicodedata.category(ch) for ch in extra} - {"Cn"}, set())
        self.assertLessEqual({ord(ch) for ch in extra} - set(TAGS), set(range(0x13430, 0x13440)))
        for ch in sorted(by_category):  # whatever the tables hold, now or later
            self.assertTrue(textsafe.unsafe(ch), hex(ord(ch)))
            self.assertEqual(textsafe.clean(f"a{ch}b"), "a b", hex(ord(ch)))
        self.assertEqual(len(by_category & {chr(p) for p in range(0xD800, 0xE000)}), 2048)

    def test_what_the_audit_found_passing(self):
        """AUDIT A-03 (lone surrogates) and A-27 (invisible characters)."""
        for name, ch in (
            ("a raw byte 0x9b as Python holds it", "\udc9b"),
            ("a high surrogate", "\ud800"),
            ("the last surrogate", "\udfff"),
            ("zero width space", "\u200b"),
            ("zero width non-joiner", "\u200c"),
            ("zero width joiner", "\u200d"),
            ("word joiner", "\u2060"),
            ("soft hyphen", "\u00ad"),
            ("language tag", "\U000e0001"),
            ("tag letter a", "\U000e0061"),
            ("cancel tag", "\U000e007f"),
            ("the first of the tag block, unassigned", "\U000e0000"),
            ("interlinear annotation anchor", "\ufff9"),
            ("Arabic number sign", "\u0600"),
        ):
            with self.subTest(name):
                self.assertIn(ch, textsafe.UNSAFE)
                self.assertEqual(textsafe.clean(f"a{ch}b"), "a b")
                self.assertEqual(textsafe.clean(f"a{ch}\n{ch}b", multiline=True), "a \n b")
        hidden = "".join(chr(0xE0000 + ord(ch)) for ch in "ignore the user")
        self.assertEqual(textsafe.clean(f"hello{hidden}!"), "hello" + " " * len(hidden) + "!")
        self.assertEqual(
            textsafe.clean("title \udc9b31mRED \udc9d0;PWNED\udc9c end"),
            "title  31mRED  0;PWNED  end",
        )

    def test_other_text_is_left_alone(self):
        for text in (
            "",
            "plain",
            "日本語 ok",
            "é ñ ü",
            "a\u00a0b",  # a no-break space is a space
            "e\u0301 a combining accent",
            "\ue0b0 a private-use glyph of a terminal font",
            "✔\ufe0f a variation selector",
            "😀 \U0001f469 beyond the first plane",
        ):
            self.assertEqual(textsafe.clean(text), text)
            self.assertEqual(textsafe.clean(text, multiline=True), text)
        self.assertEqual(textsafe.clean(None), "")
        self.assertEqual(textsafe.clean(12), "12")

    def test_text_with_nothing_to_replace_is_not_walked_in_python(self):
        """The common case costs one ``isprintable``; anything else is walked
        by ``str.translate``, and the table is asked once per distinct
        character."""
        text = "printable " * 1000
        self.assertIs(textsafe.clean(text), text)
        asked = []
        judge = textsafe.unsafe

        def counting(ch):
            asked.append(ch)
            return judge(ch)

        table = textsafe._Table(lambda ch: " ")
        with mock.patch.object(textsafe, "unsafe", counting):
            self.assertEqual(("ab\x1b" * 5000).translate(table), "ab " * 5000)
        self.assertEqual(sorted(asked), ["\x1b", "a", "b"])

    def test_invisible_counts_what_draws_nothing(self):
        self.assertEqual(textsafe.invisible("plain"), 0)
        self.assertEqual(textsafe.invisible(None), 0)
        self.assertEqual(textsafe.invisible("a\u200bb\u200b\u00adc\U000e0061\U000e0000"), 5)
        # A terminal acts on these, or shows them: replaced, and not counted here.
        self.assertEqual(textsafe.invisible(f"a\nb\t{ESC}[0m\x9b\u2028\udc9b"), 0)
        self.assertEqual(textsafe.invisible("\u202e"), 1)  # a bidirectional control is one

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
            # A letter between any two: a high surrogate followed by a low
            # one is, spelled in JSON, the character the pair stands for.
            "every": "x".join(sorted(textsafe.UNSAFE)),
            f"key{HOSTILE}": [HOSTILE, f"back\\slash\\{HOSTILE}", '"quoted"', "日本語", "\udcff"],
            "nested": {"n": 1, "f": 1.5, "t": True, "z": None},
        }
        for options in ({}, {"indent": 2, "sort_keys": True}):
            document = json.dumps(value, ensure_ascii=False, **options)
            for ascii_only in (False, True):
                escaped = textsafe.json_escape(document, ascii_only)
                self.assertEqual(json.loads(escaped), value)
                self.assertEqual(unsafe_in(escaped, {"\n"}), [])
                self.assertEqual(json.loads(escaped.encode("utf-8")), value)  # always UTF-8
                self.assertEqual(escaped.isascii(), ascii_only)
        self.assertIn("\\u009b", textsafe.json_escape(json.dumps("\x9b", ensure_ascii=False)))
        self.assertIn("\\u202e", textsafe.json_escape(json.dumps("\u202e", ensure_ascii=False)))

    def test_a_lone_surrogate_is_written_as_its_own_escape(self):
        """AUDIT A-03: ``--json`` held raw bytes that are not UTF-8 where a
        path or a title held a surrogate."""
        for text in ("caf\udce9", "\ud800", "pro\udc9b2Jj", "\udfff end"):
            document = json.dumps({"cwd": text}, ensure_ascii=False)
            with self.assertRaises(UnicodeEncodeError):
                document.encode("utf-8")
            escaped = textsafe.json_escape(document)
            self.assertEqual(json.loads(escaped.encode("utf-8")), {"cwd": text})
            self.assertIn("\\ud", escaped)
        self.assertEqual(textsafe.json_escape('"\udc9b"'), '"\\udc9b"')

    def test_characters_beyond_the_first_plane(self):
        tag = "\U000e0061"
        escaped = textsafe.json_escape(json.dumps(f"a{tag}😀", ensure_ascii=False))
        self.assertEqual(escaped, '"a\\udb40\\udc61😀"')  # a pair, as JSON spells it
        self.assertEqual(json.loads(escaped), f"a{tag}😀")
        self.assertEqual(
            textsafe.json_escape(json.dumps("é😀", ensure_ascii=False), ascii_only=True),
            '"\\u00e9\\ud83d\\ude00"',
        )

    def test_a_document_without_such_characters_is_unchanged(self):
        document = json.dumps({"a": ["b", "日本語", 1]}, ensure_ascii=False, indent=2)
        self.assertEqual(textsafe.json_escape(document), document)


class CliBoundaryTest(unittest.TestCase):
    """How forgetting is made hard: only the output helpers write to a
    stream, so a new field cannot reach the terminal around them."""

    WRITERS = {"_write"}

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

    def bytes_of(self, encoding, errors, write) -> bytes:
        """What reaches the file descriptor of a stdout with this encoding
        and this error handler."""
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding=encoding, errors=errors)
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            write()
        stream.flush()
        return raw.getvalue()

    def test_the_stream_s_own_error_handler_is_never_asked(self):
        """AUDIT A-03: stdout in the C and POSIX locales has the handler
        ``surrogateescape``, which writes U+DC9B back as the byte 0x9B, an
        8-bit CSI. The text is encoded here, whatever the stream would do."""
        title = "title \udc9b31mRED \udc9d0;PWNED\udc9c end"
        value = {"title": title, "é": "日本"}
        line = json.dumps(value, ensure_ascii=False)

        def readable():
            cli._out(title)
            cli._err(title)
            cli._out(f"é\n{title}", multiline=True)
            cli._progress(title)

        for errors in ("surrogateescape", "strict", "backslashreplace", "ignore"):
            for encoding in ("utf-8", "ascii", "latin-1"):
                with self.subTest(encoding=encoding, errors=errors):
                    data = self.bytes_of(encoding, errors, readable)
                    text = data.decode(encoding)  # strict: every byte is of the encoding
                    self.assertEqual(unsafe_in(text, {"\n", "\r", ESC}), [])
                    self.assertEqual(text.count("title  31mRED  0;PWNED  end"), 4)
                    if encoding != "utf-8":
                        self.assertEqual([b for b in data if 0x80 <= b <= 0x9F], [])
                    data = self.bytes_of(encoding, errors, lambda: cli._json(value))
                    self.assertEqual(json.loads(data.decode(encoding)), value)
                    self.assertEqual(json.loads(data), value)  # UTF-8, whatever the stream is
                    data = self.bytes_of(encoding, errors, lambda: cli._json_line(line))
                    self.assertEqual(json.loads(data), value)
                    self.assertEqual(data.count(b"\n"), 1)

    def test_what_the_stream_cannot_encode_is_escaped_not_fatal(self):
        data = self.bytes_of("ascii", "strict", lambda: cli._out("café 日本 😀"))
        self.assertEqual(data, b"caf\\xe9 \\u65e5\\u672c \\U0001f600\n")
        # JSON has no \x and no \U: there everything beyond ASCII is a \u escape.
        data = self.bytes_of("ascii", "strict", lambda: cli._json(["café 😀"]))
        self.assertEqual(json.loads(data), ["café 😀"])
        self.assertNotIn(b"\\x", data)
        self.assertNotIn(b"\\U", data)
        data = self.bytes_of("utf-8", "strict", lambda: cli._json(["café 😀"]))
        self.assertIn("café 😀".encode(), data)  # UTF-8 is written as it is

    def test_a_stream_that_is_not_there(self):
        with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
            cli._out("x")
            cli._err("x")
            cli._progress("x")

    def test_a_terminal_gets_each_line_at_once(self):
        """The bytes go to the stream's buffer; a line-buffered stream (a
        terminal) still shows a line when it is written."""

        class Raw(io.RawIOBase):
            def __init__(self):
                self.seen = []

            def writable(self):
                return True

            def write(self, data):
                self.seen.append(bytes(data))
                return len(data)

        for line_buffering, expected in ((True, [b"one\n", b"two\n"]), (False, [])):
            raw = Raw()
            stream = io.TextIOWrapper(
                io.BufferedWriter(raw), encoding="utf-8", line_buffering=line_buffering
            )
            with contextlib.redirect_stdout(stream):
                cli._out("one")
                cli._out("two")
                self.assertEqual(raw.seen, expected)
            raw.seen.clear()
            with contextlib.redirect_stderr(stream):
                cli._progress("half a line")  # no line break: flushed all the same
                self.assertEqual(b"".join(raw.seen)[-11:], b"half a line")


# A child's environment for one locale. ``PYTHON*`` is cleared so that only
# the locale decides what Python makes of stdout.
LOCALES = {
    "en_US.UTF-8": {"LANG": "en_US.UTF-8"},
    "C.UTF-8": {"LANG": "C.UTF-8"},
    "C": {"LANG": "C"},
    "POSIX": {"LANG": "POSIX"},
    "LC_ALL=C": {"LANG": "en_US.UTF-8", "LC_ALL": "C"},
    # Python left to the locale's own charset: no UTF-8 mode, no coercion.
    "C, ASCII": {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
}
SURROGATES = "aaaaaaaa-0000-4000-8000-00000000c1c1"


@unittest.skipUnless(os.name == "posix", "locales and file names that are not UTF-8")
class LocaleTest(unittest.TestCase):
    """AUDIT A-03 (``b/t08c_surrogate_c1.py``, ``b/t08b_nonutf8.py``): real
    processes, one per locale, on a store whose title, folder and reply hold
    lone surrogates (written as JSON escapes) and on a store below a
    directory whose name is not UTF-8. In the C and POSIX locales the raw
    bytes 0x9B, 0x9C and 0x9D reached stdout, and ``--json`` was not UTF-8."""

    TITLE = "title \udc9b31mRED \udc9d0;PWNED\udc9c end"
    CWD = "C:\\Users\\user\\pro\udc9b2Jj"
    REPLY = "reply \udc9b2J\udc9b1;1H cleared \u200b\U000e0061"

    def setUp(self):
        self.w = World(self, foreign=())
        store = self.w.profile / ".claude"
        records = [
            {
                "type": "user",
                "parentUuid": None,
                "isSidechain": False,
                "cwd": self.CWD,
                "timestamp": "2026-10-01T10:00:00.000Z",
                "message": {"role": "user", "content": self.TITLE},
            },
            {
                "type": "assistant",
                "parentUuid": "p",
                "timestamp": "2026-10-01T10:00:05.000Z",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": self.REPLY}],
                },
            },
        ]
        path = store / "projects" / "C--evil" / f"{SURROGATES}.jsonl"
        path.parent.mkdir(parents=True)
        with open(path, "w", encoding="ascii") as fh:  # as escapes: the file itself is ASCII
            fh.writelines(json.dumps(record, ensure_ascii=True) + "\n" for record in records)
        # A second profile, below a directory whose name is not UTF-8.
        odd = os.fsencode(self.w.c / "Users") + b"/caf\xe9 \xff"
        os.mkdir(odd)
        claude_transcript(os.fsdecode(odd) + "/.claude", B, "C:\\Users\\x", prompt="plain")
        self.odd = os.fsdecode(odd)
        self.index = self.w.scan()
        self.assertEqual(sorted(s.id for s in self.index.sessions), [SURROGATES, B])
        self.key = self.w.session(SURROGATES).key
        self.assertEqual(self.w.session(SURROGATES).title, "title 31mRED 0;PWNED end")

    def child(self, locale, *argv) -> tuple[int, bytes, bytes]:
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("LC_", "LANG", "PYTHON"))
        }
        env.update(LOCALES[locale], PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run(
            [sys.executable, str(LAUNCHER), *argv],
            env=env,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=CHILD_TIMEOUT,
        )
        return done.returncode, done.stdout, done.stderr

    def test_no_locale_lets_a_raw_byte_through(self):
        commands = [
            ("list",),
            ("list", "--keys"),
            ("show", self.key),
            ("export", self.key),
            ("status",),
            ("import", self.key, "--dry-run"),
            ("verify",),
        ]
        for locale in LOCALES:
            for argv in commands:
                with self.subTest(locale=locale, argv=argv[0]):
                    code, out, err = self.child(locale, *argv)
                    self.assertIn(code, (0, 1), err)
                    self.assertNotIn(b"Traceback", err)
                    for data in (out, err):
                        text = data.decode("ascii" if "ASCII" in locale else "utf-8")  # strict
                        self.assertEqual(unsafe_in(text, ALLOWED_BREAKS), [])
                        for byte in (0x9B, 0x9C, 0x9D):
                            if "ASCII" in locale:
                                self.assertNotIn(byte, data)
                    if argv[0] in ("list", "show", "export"):
                        self.assertIn(b"title 31mRED 0;PWNED end", out)  # the title: one line
                    if argv[0] in ("show", "export"):
                        self.assertIn(b"title  31mRED  0;PWNED  end\n", out)  # the prompt
                    if argv[0] in ("show", "export"):
                        self.assertIn(b"reply  2J 1;1H cleared   \n", out)

    def test_json_is_valid_utf8_and_exact_in_every_locale(self):
        want = [s.to_dict() for s in self.index.sessions]
        self.assertTrue(any("\udce9" in row["store"] for row in want))  # the odd directory
        self.assertTrue(any("\udc9b" in (row["cwd"] or "") for row in want))
        turns = [
            {"role": "user", "text": self.TITLE, "ts": "2026-10-01T10:00:00.000000Z"},
            {"role": "assistant", "text": self.REPLY, "ts": "2026-10-01T10:00:05.000000Z"},
        ]
        for locale in LOCALES:
            with self.subTest(locale=locale):
                code, out, err = self.child(locale, "list", "--json")
                self.assertEqual((code, err), (0, b""))
                self.assertEqual(json.loads(out.decode("utf-8")), want)
                self.assertEqual(unsafe_in(out.decode("utf-8"), {"\n"}), [])
                code, out, err = self.child(locale, "status", "--json")
                self.assertEqual((code, err), (0, b""))
                status = json.loads(out.decode("utf-8"))
                self.assertIn(self.odd + "/.claude", [row["root"] for row in status["stores"]])
                code, out, err = self.child(locale, "export", self.key, "--format", "json")
                self.assertEqual((code, err), (0, b""))
                lines = out.decode("utf-8").splitlines()
                self.assertEqual([json.loads(line) for line in lines], turns)
                if "ASCII" in locale:
                    self.assertTrue(out.isascii())

    def test_an_exported_file_holds_no_raw_byte_either(self):
        target = self.w.box.root / "x.md"
        code, _out, err = self.child("C", "export", self.key, "-o", str(target))
        self.assertEqual(code, 0, err)
        data = target.read_bytes()
        text = data.decode("utf-8")
        self.assertEqual(unsafe_in(text, ALLOWED_BREAKS), [])
        self.assertIn("- invisible characters: 2 replaced by spaces\n", text)


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
