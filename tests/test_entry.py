"""How the program is entered: ``cli.main`` starts the TUI itself (REVIEW
R-13, SPEC "Packaging"), so an installed ``agent-sessions`` needs no launcher
script; what is said when Textual is missing, from a checkout and from an
installed package; and that every other command still loads neither the
``tui`` package nor Textual.

No test here starts the real app: ``tui.run`` is replaced, or Textual is
made to look missing in a child interpreter."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SRC = str(REPO / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

# Imported for what it does when it is loaded: it points AGENT_SESSIONS_STATE
# at a temp directory. A command that fails here writes to the log, and that
# must never be the user's own, also when this module is run by itself.
import support  # noqa: E402, F401

from agent_sessions import cli, fsutil, tui  # noqa: E402


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class StartsTheTuiTest(unittest.TestCase):
    """``cli.main`` with ``tui``, and with nothing on a terminal."""

    def setUp(self):
        patch = mock.patch.object(tui, "run", return_value=0)
        self.started = patch.start()
        self.addCleanup(patch.stop)

    def test_tui_starts_it_and_returns_its_exit_status(self):
        self.assertEqual(run("tui"), (0, "", ""))
        self.started.assert_called_once_with()
        self.started.return_value = 1  # as when Textual is missing
        self.assertEqual(run("tui"), (1, "", ""))

    def test_no_arguments_on_a_terminal_starts_it(self):
        with mock.patch.object(cli, "_on_a_terminal", return_value=True):
            self.assertEqual(run(), (0, "", ""))
        self.started.assert_called_once_with()

    def test_no_arguments_and_no_terminal_prints_the_help_and_exits_two(self):
        code, out, err = run()  # the streams of ``run`` are not a terminal
        self.assertEqual((code, err), (2, ""))
        self.assertTrue(out.startswith("usage: agent-sessions"))
        self.started.assert_not_called()

    def test_a_terminal_is_both_ends(self):
        for stdin, stdout, both in (
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ):
            with (
                mock.patch.object(sys, "stdin", mock.Mock(isatty=lambda value=stdin: value)),
                mock.patch.object(sys, "stdout", mock.Mock(isatty=lambda value=stdout: value)),
            ):
                self.assertIs(cli._on_a_terminal(), both)
        with mock.patch.object(sys, "stdin", None):  # a process started without one
            self.assertIs(cli._on_a_terminal(), False)

    def test_tui_takes_no_arguments_and_has_a_help(self):
        code, out, _err = run("tui", "--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: agent-sessions tui", out)
        self.assertEqual(run("tui", "extra")[0], 2)
        self.started.assert_not_called()

    def test_what_the_app_raises_is_an_internal_error_not_a_traceback(self):
        # It is logged: into the temp state directory, never the user's.
        state = fsutil.state_dir()
        self.assertEqual(os.environ.get("AGENT_SESSIONS_STATE"), str(state))
        self.assertNotEqual(state, Path.home() / ".local" / "state" / "agent-sessions")
        self.started.side_effect = RuntimeError("boom")
        code, out, err = run("tui")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("agent-sessions: internal error: RuntimeError: boom", err)
        self.assertIn(f"(details in {state}", err)


class CheckoutTest(unittest.TestCase):
    """``tui.checkout``: is this package a checkout's, or an installed one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))

    def checkout_at(self, root, files=tui.CHECKOUT_FILES) -> Path:
        package = root / "src" / "agent_sessions"
        package.mkdir(parents=True)
        for name in files:
            (root / name).write_text("", encoding="utf-8")
        return package

    def test_this_checkout_is_one(self):
        self.assertEqual(tui.checkout(), REPO)
        self.assertEqual(tui.install_hint(), tui.INSTALL_HINT.format(repo=REPO))

    def test_a_checkout_anywhere(self):
        package = self.checkout_at(self.tmp / "work tree")
        self.assertEqual(tui.checkout(package), self.tmp / "work tree")
        self.assertEqual(
            tui.install_hint(package),
            f"agent-sessions: the TUI needs Textual; run ./install.sh in {self.tmp / 'work tree'}",
        )

    def test_an_installed_package_is_not_one(self):
        site = self.tmp / "venv" / "lib" / "python3.14" / "site-packages"
        package = site / "agent_sessions"
        package.mkdir(parents=True)
        # What lies three levels above an installed package proves nothing,
        # even when it looks like a project (the old guess was that level).
        for name in tui.CHECKOUT_FILES:
            (self.tmp / "venv" / "lib" / name).write_text("", encoding="utf-8")
            (site / name).write_text("", encoding="utf-8")
        self.assertIsNone(tui.checkout(package))
        self.assertEqual(
            tui.install_hint(package),
            "agent-sessions: the TUI needs Textual; run: pip install 'agent-sessions[tui]'",
        )

    def test_a_src_directory_without_the_project_files_is_not_one(self):
        for missing in tui.CHECKOUT_FILES:
            kept = [name for name in tui.CHECKOUT_FILES if name != missing]
            package = self.checkout_at(self.tmp / f"without {missing}", kept)
            self.assertIsNone(tui.checkout(package), missing)
        package = self.checkout_at(self.tmp / "a directory")
        (self.tmp / "a directory" / "install.sh").unlink()
        (self.tmp / "a directory" / "install.sh").mkdir()  # not a file
        self.assertIsNone(tui.checkout(package))


class ChildCase(unittest.TestCase):
    """A fresh interpreter with a temp home, cache and state: what it loads
    and prints is this command's alone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "AGENT_SESSIONS_HOME": str(self.tmp / "home"),
            "AGENT_SESSIONS_CACHE": str(self.tmp / "cache"),
            "AGENT_SESSIONS_STATE": str(self.tmp / "state"),
            "AGENT_SESSIONS_MOUNTS": "[]",
            "AGENT_SESSIONS_NOTIFY": "0",
        }

    def child(self, code, src=_SRC):
        return subprocess.run(
            [sys.executable, "-c", f"import sys\nsys.path.insert(0, {str(src)!r})\n{code}"],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(self.tmp),
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )


class MissingTextualTest(ChildCase):
    """Textual made to look missing (``sys.modules['textual'] = None``): one
    line that fits where the program runs from, and exit status 1."""

    CODE = (
        "sys.modules['textual'] = None\n"  # as if it were not installed
        "from agent_sessions import cli\n"
        "sys.exit(cli.main(['tui']))\n"
    )

    def test_in_a_checkout_it_names_install_sh_and_the_checkout(self):
        result = self.child(self.CODE)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(
            result.stderr,
            f"agent-sessions: the TUI needs Textual; run ./install.sh in {REPO}\n",
        )

    def test_in_an_installed_package_it_names_the_extra(self):
        # The package as pip lays it out: in a site-packages directory, with
        # nothing of the checkout around it.
        site = self.tmp / "venv" / "lib" / "python3" / "site-packages"
        shutil.copytree(
            REPO / "src" / "agent_sessions",
            site / "agent_sessions",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        result = self.child(self.CODE, src=site)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(
            result.stderr,
            "agent-sessions: the TUI needs Textual; run: pip install 'agent-sessions[tui]'\n",
        )


class EngineCommandsStayLightTest(ChildCase):
    """The lazy import is in one function: a command that is not the TUI
    loads neither ``agent_sessions.tui`` nor Textual."""

    COMMANDS: tuple[list[str], ...] = (
        ["--version"],
        ["--help"],
        ["list", "--json"],
        ["list"],
        ["scan", "--json"],
        ["status", "--json"],
        ["verify"],
        ["map"],
        ["show", "nope"],
        ["open", "nope", "--dry-run"],
        ["import", "nope", "--dry-run"],
        ["unimport", "nope", "--dry-run"],
        ["export", "nope"],
        ["continue", "nope", "--dry-run"],
        ["mount", "nope"],
        ["forget", "nope", "--yes"],
        [],  # no terminal here: the help
    )

    def test_no_command_but_tui_loads_the_tui(self):
        code = (
            "import contextlib, io\n"
            "from agent_sessions import cli\n"
            f"for argv in {self.COMMANDS!r}:\n"
            "    with contextlib.redirect_stdout(io.StringIO()), "
            "contextlib.redirect_stderr(io.StringIO()):\n"
            "        try:\n"
            "            cli.main(list(argv))\n"
            "        except SystemExit:\n"
            "            pass\n"
            "unwanted = ('textual.', 'agent_sessions.tui')\n"
            "print(sorted(m for m in sys.modules if m == 'textual' or m.startswith(unwanted)))\n"
        )
        result = self.child(code)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "[]\n", ""))

    def test_every_command_of_the_parser_was_tried(self):
        """A subcommand that is added later cannot be left out of the list."""
        tried = {argv[0] for argv in self.COMMANDS if argv}
        self.assertEqual(sorted(tried - {"--version", "--help"} | {"tui"}), cli.subcommands())

    def test_tui_alone_loads_the_package(self):
        result = self.child(
            "sys.modules['textual'] = None\n"
            "import contextlib, io\n"
            "from agent_sessions import cli\n"
            "with contextlib.redirect_stderr(io.StringIO()):\n"
            "    cli.main(['tui'])\n"
            "print('agent_sessions.tui' in sys.modules)\n"
        )
        self.assertEqual((result.returncode, result.stdout), (0, "True\n"))


if __name__ == "__main__":
    unittest.main()
