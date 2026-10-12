"""What keeps the package, its help texts and its documents in step:

- the engine's messages name no command-line flag (the TUI shows them too);
  the command line adds its flag at its own boundary (``cli._REMEDIES``);
- the help: every command in one group of the top-level help, a description
  each, examples that the parser accepts, 80 columns;
- the documents: every command in the README and in SPEC's CLI sections,
  every module in ``filemap.md``, one version everywhere;
- the package metadata.

Reads files of the checkout and runs ``cli.main`` on ``--help``; nothing here
touches a store."""

import ast
import contextlib
import io
import os
import re
import shlex
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import agent_sessions  # noqa: E402
from agent_sessions import api, cli, errors, unimport  # noqa: E402

PACKAGE = REPO / "src" / "agent_sessions"

# A long option as a message would name it: two dashes and a word, not inside
# a longer word (``session-list--row`` is a style class, not an option).
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]*")


def run(*argv, columns=None):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env = {"COLUMNS": str(columns)} if columns else {}
    with (
        mock.patch.dict(os.environ, env),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        if not columns:
            os.environ.pop("COLUMNS", None)
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def texts(path):
    """Every string literal of a module that is not a docstring, with its
    line number. The pieces of an f-string come one by one."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            docstrings.add(id(node.value))  # a string that documents an attribute
    for node in ast.walk(tree):
        known = isinstance(node, ast.Constant) and id(node) in docstrings
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and not known:
            yield node.lineno, node.value


class NeutralMessagesTest(unittest.TestCase):
    """No message of the engine or of the TUI names a flag of the command
    line: the same reason is shown by the TUI, which has no flags."""

    def messages_with_a_flag(self, path):
        found = []
        for lineno, text in texts(path):
            # An element of a command line (``--resume``, ``--app-id=x``) is
            # one word; a message that names a flag has words around it.
            if FLAG.search(text) and len(text.split()) > 1:
                found.append((lineno, text))
        return found

    def test_no_message_outside_the_command_line_names_a_flag(self):
        checked = 0
        for path in sorted(PACKAGE.rglob("*.py")):
            if path.name == "cli.py":
                continue
            checked += 1
            self.assertEqual(self.messages_with_a_flag(path), [], path.relative_to(REPO))
        self.assertGreater(checked, 25)

    def test_the_check_finds_what_it_is_for(self):
        """On the command line's own module it finds plenty; a command's
        elements and style classes are not taken for messages."""
        found = [text for _line, text in self.messages_with_a_flag(PACKAGE / "cli.py")]
        self.assertIn("use --to DIR to import it into a folder you choose", found)
        self.assertTrue(FLAG.search("removed only with --force"))
        self.assertTrue(FLAG.search("(--force replaces it)"))
        self.assertTrue(FLAG.search("`list --keys` shows the keys"))
        self.assertFalse(FLAG.search("session-list--row"))
        self.assertFalse(FLAG.search("a -- b"))
        launch = [text for _line, text in texts(PACKAGE / "launch.py")]
        self.assertIn("--app-name=agent-sessions", launch)  # seen, and one word

    def test_the_reasons_that_had_a_flag_say_it_in_plain_words(self):
        self.assertIn("can only be undone by force", unimport.LEGACY)
        self.assertIn("removed only by a forced undo", unimport.EXTRA)
        self.assertEqual(api.LEFT_FOR_A_FORCED_UNDO, unimport.EXTRA)


class RemediesTest(unittest.TestCase):
    """``ApiError.remedy``: the word the engine gives with a refusal that
    asking in another way gets past, and the flag the command line adds."""

    WORDS = sorted(getattr(errors, name) for name in dir(errors) if name.startswith("REMEDY_"))

    def test_the_command_line_has_a_hint_for_every_word(self):
        self.assertEqual(sorted(cli._REMEDIES), self.WORDS)
        self.assertEqual(len(self.WORDS), 5)
        for word, hint in cli._REMEDIES.items():
            self.assertRegex(hint, FLAG, word)

    def test_an_error_has_no_remedy_unless_one_is_given(self):
        for kind in (
            errors.ApiError,
            errors.NotFound,
            errors.LaunchFailed,
            errors.ImportFailed,
            errors.UndoFailed,
            errors.ExportFailed,
            errors.Offline,
            errors.MountFailed,
        ):
            self.assertEqual(kind("why").remedy, "")
            self.assertEqual(kind("why", remedy=errors.REMEDY_FORCE).remedy, "force")
            self.assertEqual(cli._reason(kind("why")), "why")
        failed = errors.UndoFailed("why", differences=["a"], remedy=errors.REMEDY_FORCE)
        self.assertEqual((failed.differences, failed.no_record), (["a"], False))

    def test_the_hint_is_added_where_the_reason_is_printed(self):
        for word, command, printed in (
            (errors.REMEDY_FORCE, ("unimport", "k", "--dry-run"), "why (--force)"),
            (errors.REMEDY_FOLDER, ("continue", "k", "--dry-run"), "why (--in DIR)"),
            (
                errors.REMEDY_OVERWRITE,
                ("export", "k", "-o", "/nowhere/x"),
                "why (--force replaces it)",
            ),
            (errors.REMEDY_KEYS, ("show", "k"), "why; `list --keys` shows the keys"),
            (
                errors.REMEDY_EXPERIMENTAL,
                ("import", "k", "--dry-run"),
                "why; --experimental imports it all the same",
            ),
            ("", ("show", "k"), "why"),
            ("a word of a later engine", ("show", "k"), "why"),
        ):
            refusal = errors.ApiError("why", remedy=word)
            with (
                mock.patch.object(api, "unimport", side_effect=refusal),
                mock.patch.object(api, "continue_session", side_effect=refusal),
                mock.patch.object(api, "export_file", side_effect=refusal),
                mock.patch.object(api, "load_index", side_effect=refusal),
            ):
                self.assertEqual(run(*command), (1, "", f"agent-sessions: {printed}\n"), word)

    def test_what_an_undo_left_for_a_forced_one_names_the_flag(self):
        report = api.UnimportReport(
            "antigravity",
            "id",
            None,
            None,
            left=[("/n/a.db-wal", unimport.EXTRA), ("/n/dir", unimport.NOT_EMPTY)],
            dry_run=True,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_undo(report)
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                f"would leave /n/a.db-wal ({unimport.EXTRA}: --force)",
                f"would leave /n/dir ({unimport.NOT_EMPTY})",
            ],
        )


class HelpTest(unittest.TestCase):
    COMMANDS = cli.subcommands()
    WITH_EXAMPLES = ("import", "unimport", "continue", "export", "mount", "map", "list", "open")

    def help(self, *argv, columns=None) -> str:
        code, out, err = run(*argv, "--help", columns=columns)
        self.assertEqual((code, err), (0, ""), argv)
        return out

    def test_the_commands(self):
        self.assertEqual(
            self.COMMANDS,
            sorted(
                [
                    "continue",
                    "export",
                    "forget",
                    "import",
                    "list",
                    "map",
                    "mount",
                    "open",
                    "scan",
                    "show",
                    "status",
                    "tui",
                    "unimport",
                    "verify",
                ]
            ),
        )
        self.assertEqual(sorted(cli._COMMANDS), self.COMMANDS)  # each has its function

    def test_every_command_is_in_one_group_of_the_top_level_help(self):
        grouped = [name for _title, commands in cli.GROUPS for name, _summary in commands]
        self.assertEqual(sorted(grouped), self.COMMANDS)  # each once, and no other
        self.assertEqual(
            [title for title, _commands in cli.GROUPS],
            ["browse", "act on a session", "drives", "maintenance"],
        )
        text = self.help()
        for title, commands in cli.GROUPS:
            self.assertIn(f"\n{title}:\n", text)
            for name, summary in commands:
                self.assertRegex(text, rf"(?m)^  {name} +{re.escape(summary)}$")
                self.assertNotIn("\n", summary)
        # In the order of the table, a group after the other.
        positions = [text.index(f"\n{title}:\n") for title, _commands in cli.GROUPS]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(text.startswith("usage: agent-sessions [-h] [--version] [--debug]"))

    def test_every_command_has_a_description_and_its_usage(self):
        for name in self.COMMANDS:
            text = self.help(name)
            head, _, rest = text.partition("\n\n")
            self.assertTrue(head.startswith(f"usage: agent-sessions {name} "), text)
            description = rest.partition("\n\n")[0]
            self.assertGreater(len(description), 60, name)
            self.assertTrue(description.rstrip().endswith("."), name)

    def test_examples_where_they_help_and_every_one_is_a_command_that_parses(self):
        parser = cli.build_parser()
        seen = 0
        for name in self.COMMANDS:
            text = self.help(name)
            self.assertEqual("\nexamples:\n" in text, name in self.WITH_EXAMPLES, name)
            if name not in self.WITH_EXAMPLES:
                continue
            lines = text.partition("\nexamples:\n")[2].splitlines()
            commands = [line[2:] for line in lines if line.startswith("  agent-sessions ")]
            self.assertGreaterEqual(len(commands), 2, name)
            self.assertEqual(
                [line for line in lines if not line.startswith(("  agent-sessions ", "      "))],
                [],
                name,
            )
            for line in commands:
                argv = shlex.split(line.partition(" | ")[0])[1:]
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    try:
                        args = parser.parse_args(argv)
                    except SystemExit:
                        self.fail(f"{line!r} does not parse: {err.getvalue()}")
                seen += 1
            # At least one example is of the command itself.
            self.assertTrue(any(line.split()[1] == name for line in commands), name)
            del args
        self.assertGreater(seen, 20)

    def test_every_help_fits_80_columns_whatever_the_terminal(self):
        for columns in (None, 80, 200):
            for argv in [()] + [(name,) for name in self.COMMANDS]:
                for line in self.help(*argv, columns=columns).splitlines():
                    self.assertLessEqual(len(line), 80, (argv, columns, line))
        self.assertEqual(self.help(columns=200), self.help(columns=80))
        # A narrower terminal gets narrower paragraphs.
        narrow = self.help("import", columns=60).partition("\nexamples:\n")[0]
        self.assertLessEqual(max(map(len, narrow.splitlines())), 60)

    def test_an_unknown_command_is_a_usage_error(self):
        code, out, err = run("menu")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage: agent-sessions", err)


def filemap_paths():
    """Every path ``filemap.md`` names in backticks, braces expanded."""
    found = set()
    for token in re.findall(r"`([^`\n]+)`", (REPO / "filemap.md").read_text(encoding="utf-8")):
        brace = re.fullmatch(r"(.*)\{([^{}]+)\}(.*)", token)
        if brace:
            head, names, tail = brace.groups()
            found.update(f"{head}{name}{tail}" for name in names.split(","))
        else:
            found.add(token)
    return found


class DocumentsTest(unittest.TestCase):
    """The documents cannot fall behind the code without a test saying so."""

    def read(self, name) -> str:
        return (REPO / name).read_text(encoding="utf-8")

    def section(self, text, heading) -> str:
        """The body of a ``##`` or ``###`` section, up to the next heading
        of the same or a higher level."""
        match = re.search(rf"(?m)^(#+) {re.escape(heading)}\n", text)
        if match is None:
            self.fail(f"no section {heading!r}")
        level = len(match.group(1))
        rest = text[match.end() :]
        end = re.search(rf"(?m)^#{{1,{level}}} ", rest)
        return rest[: end.start()] if end else rest

    def names(self, text, command) -> bool:
        return re.search(rf"agent-sessions \[?{command}\]?(?![\w-])", text) is not None

    def test_every_command_is_in_the_readme(self):
        readme = self.read("README.md")
        for command in cli.subcommands():
            self.assertTrue(self.names(readme, command), command)

    def test_every_command_is_in_the_cli_sections_of_the_spec(self):
        spec = self.read("docs/SPEC.md")
        sections = self.section(spec, "CLI contract") + self.section(spec, "CLI additions")
        for command in cli.subcommands():
            self.assertTrue(self.names(sections, command), command)

    def test_every_command_line_in_the_readme_is_one_the_parser_takes(self):
        """The lines of the README's ``sh`` blocks that run ``agent-sessions``
        (after variable assignments, before a ``#`` comment): each parses."""
        parser = cli.build_parser()
        blocks = re.findall(r"(?ms)^```sh\n(.*?)^```", self.read("README.md"))
        self.assertGreater(len(blocks), 8)
        seen = set()
        for line in "\n".join(blocks).splitlines():
            words = shlex.split(line, comments=True)
            while words and re.fullmatch(r"[A-Z_]+=.*", words[0]):
                words.pop(0)
            if not words or words[0] != "agent-sessions":
                continue
            with contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    args = parser.parse_args(words[1:])
                except SystemExit:
                    self.fail(f"{line!r} does not parse: {err.getvalue()}")
            seen.add(args.command or "tui")
        self.assertEqual(sorted(seen), cli.subcommands())  # and each command is shown once

    def test_the_make_targets_the_readme_names_exist(self):
        makefile = self.read("Makefile")
        named = set(re.findall(r"(?m)^make ([a-z-]+)", self.read("README.md")))
        self.assertGreaterEqual(len(named), 8)
        for target in sorted(named):
            self.assertRegex(makefile, rf"(?m)^{target}:", target)

    def test_links_and_test_files_the_documents_name_exist(self):
        for name in (
            "README.md",
            "CHANGELOG.md",
            "docs/SECURITY.md",
            "docs/ADDING_A_PROVIDER.md",
            "docs/SPEC.md",
        ):
            text = self.read(name)
            base = (REPO / name).parent
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
                if not target.startswith(("http://", "https://")):
                    self.assertTrue((base / target).exists(), f"{name}: link to {target}")
            for path in re.findall(r"`(tests/[\w/]+\.py)`", text):
                self.assertTrue((REPO / path).is_file(), f"{name}: {path}")
        # The modules a defence is said to live in are modules of the package.
        security = self.read("docs/SECURITY.md")
        implemented = re.findall(r"(?m)^- Implemented: (.*(?:\n  .*)*)", security)
        self.assertEqual(len(implemented), 12)
        for entry in implemented:
            modules = re.findall(r"`((?:providers/|tui/)?\w+\.py)`", entry)
            self.assertTrue(modules, entry)
            for module in modules:
                self.assertTrue((PACKAGE / module).is_file(), module)

    def test_the_readme_names_every_environment_variable_the_code_reads(self):
        readme = self.read("README.md")
        read = set()
        for path in PACKAGE.rglob("*.py"):
            for _line, text in texts(path):
                if re.fullmatch(r"AGENT_SESSIONS_[A-Z_]+", text):
                    read.add(text)
        self.assertGreaterEqual(len(read), 6)
        for name in sorted(read | {"TERMINAL", "NO_COLOR"}):
            self.assertIn(f"`{name}`", readme, name)

    def test_the_file_map_lists_every_module_and_document(self):
        listed = filemap_paths()
        wanted = [path.relative_to(REPO).as_posix() for path in sorted(PACKAGE.rglob("*.py"))]
        wanted += [f"src/agent_sessions/tui/{name}" for name in ("app.tcss",)]
        wanted += [
            path.relative_to(REPO).as_posix() for path in sorted((REPO / "tests").glob("*.py"))
        ]
        wanted += [
            path.relative_to(REPO).as_posix() for path in sorted((REPO / "docs").glob("*.md"))
        ]
        wanted += [
            path.name
            for path in sorted(REPO.iterdir())
            if path.is_file() and not path.name.startswith(".") and path.name != "filemap.md"
        ]
        wanted += ["bin/agent-sessions", "share/agent-sessions.desktop", ".github/workflows/ci.yml"]
        self.assertGreater(len(wanted), 90)
        self.assertEqual([path for path in wanted if path not in listed], [])

    def test_the_file_map_names_no_file_that_is_gone(self):
        for path in sorted(filemap_paths()):
            if re.fullmatch(r"(src|tests|docs|bin|share)/[\w./-]+\.(py|md|tcss|sh|desktop)", path):
                self.assertTrue((REPO / path).is_file(), path)

    def test_one_version_everywhere(self):
        version = agent_sessions.__version__
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(run("--version"), (0, f"agent-sessions {version}\n", ""))
        changelog = self.read("CHANGELOG.md")
        newest = re.search(r"(?m)^## \[(\d+\.\d+\.\d+)\]", changelog)
        self.assertIsNotNone(newest)
        self.assertEqual(newest.group(1), version)
        self.assertIn(f"Version {version}.", self.section(self.read("docs/SPEC.md"), "Packaging"))


class MetadataTest(unittest.TestCase):
    def setUp(self):
        with open(REPO / "pyproject.toml", "rb") as handle:
            self.project = tomllib.load(handle)["project"]

    def test_what_an_index_page_would_show(self):
        project = self.project
        self.assertEqual(project["name"], "agent-sessions")
        self.assertEqual(project["readme"], "README.md")
        self.assertTrue((REPO / "README.md").is_file())
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], [])  # the engine: standard library only
        self.assertGreaterEqual(len(project["keywords"]), 4)
        classifiers = project["classifiers"]
        self.assertEqual(classifiers, sorted(classifiers))
        for minor in (11, 12, 13, 14):
            self.assertIn(f"Programming Language :: Python :: 3.{minor}", classifiers)
        self.assertIn("Operating System :: POSIX :: Linux", classifiers)
        self.assertIn("Environment :: Console", classifiers)
        self.assertIn("Environment :: Console :: Curses", classifiers)

    def test_no_license_is_claimed_before_the_owner_chose_one(self):
        self.assertNotIn("license", self.project)
        self.assertNotIn("license-files", self.project)
        self.assertEqual([c for c in self.project["classifiers"] if c.startswith("License")], [])
        self.assertFalse(list(REPO.glob("LICEN[CS]E*")))
        self.assertIn("No license", (REPO / "README.md").read_text(encoding="utf-8"))

    def test_the_entry_point_and_the_packaged_stylesheet(self):
        self.assertEqual(self.project["scripts"], {"agent-sessions": "agent_sessions.cli:main"})
        with open(REPO / "pyproject.toml", "rb") as handle:
            tool = tomllib.load(handle)["tool"]["setuptools"]
        self.assertEqual(tool["package-data"], {"agent_sessions.tui": ["app.tcss"]})
        self.assertTrue((PACKAGE / "tui" / "app.tcss").is_file())
        self.assertEqual(tool["dynamic"]["version"], {"attr": "agent_sessions.__version__"})


if __name__ == "__main__":
    unittest.main()
