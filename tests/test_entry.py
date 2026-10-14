"""How the program is entered: ``cli.main`` starts the TUI itself (REVIEW
R-13, SPEC "Packaging"), so an installed ``agent-sessions`` needs no launcher
script; what is said when Textual is missing, from a checkout and from an
installed package, and when there is no terminal; that every other command
still loads neither the ``tui`` package nor Textual; ``tui.__main__.main``;
and the checkout's launcher ``bin/agent-sessions`` (AUDIT A-01, A-21): which
Python it starts and with what, what it lets onto ``sys.path``, and the whole
of it run in a pseudo-terminal from a directory of planted modules.

Where the real app is started it is the launcher's, in a pseudo-terminal
made for that child alone, with a temp home, cache and state and an empty
mount list: it reaches its first screen and is sent ``q``."""

import ast
import contextlib
import importlib.util
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
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
import tui_support  # noqa: E402

from agent_sessions import cli, fsutil, logs, tui  # noqa: E402
from agent_sessions.tui import __main__ as tui_main  # noqa: E402

LAUNCHER = REPO / "bin" / "agent-sessions"


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def is_the_help(case, result, what):
    """``result`` (exit status, stdout, stderr) is the top-level help."""
    code, out, err = result
    case.assertEqual((code, err), (2, ""), what)
    case.assertTrue(out.startswith("usage: agent-sessions"), what)
    case.assertIn("open the full-screen browser", out, what)


class StartsTheTuiTest(unittest.TestCase):
    """``cli.main`` with ``tui``, and with no command."""

    def setUp(self):
        patch = mock.patch.object(tui, "run", return_value=0)
        self.started = patch.start()
        self.addCleanup(patch.stop)

    def test_tui_starts_it_and_returns_its_exit_status(self):
        self.assertEqual(run("tui"), (0, "", ""))
        self.started.assert_called_once_with(debug=False)
        for status in (1, 2, 143):  # Textual missing, no terminal, SIGTERM
            self.started.return_value = status
            self.assertEqual(run("tui"), (status, "", ""))

    def test_no_command_on_a_terminal_starts_it(self):
        with mock.patch.object(tui, "on_a_terminal", return_value=True):
            self.assertEqual(run(), (0, "", ""))
        self.started.assert_called_once_with(debug=False)

    def test_debug_reaches_it(self):
        """A-21: ``--debug`` and the TUI can be asked for together."""
        with mock.patch.object(tui, "on_a_terminal", return_value=True):
            for argv in (["--debug"], ["--debug", "tui"]):
                self.started.reset_mock()
                self.assertEqual(run(*argv), (0, "", ""))
                self.started.assert_called_once_with(debug=True)

    def test_no_command_and_no_terminal_is_the_help(self):
        for argv in ([], ["--debug"]):
            is_the_help(self, run(*argv), argv)  # the streams of ``run`` are no terminal
        self.started.assert_not_called()

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


def no_stdin() -> None:
    """For a child's ``preexec_fn``: start it with no file descriptor 0, as
    ``command <&-`` does."""
    os.close(0)


def streams(stdin=True, stdout=True, stderr=True):
    """``sys.stdin``, ``sys.stdout`` and ``sys.stderr`` replaced: True is a
    terminal, False is not one, None is a stream that was closed."""

    def stream(kind):
        return None if kind is None else mock.Mock(isatty=lambda: kind)

    return (
        mock.patch.object(sys, "stdin", stream(stdin)),
        mock.patch.object(sys, "stdout", stream(stdout)),
        mock.patch.object(sys, "stderr", stream(stderr)),
    )


class NoTerminalTest(unittest.TestCase):
    """The TUI is asked for and there is no terminal to run it on (AUDIT
    A-21): one line and exit status 2, from ``cli.main`` as from the
    launcher. It used to start, draw into the pipe and wait for keys."""

    def test_a_terminal_is_every_stream(self):
        for stdin in (True, False, None):
            for stdout in (True, False, None):
                for stderr in (True, False, None):
                    with contextlib.ExitStack() as stack:
                        for patch in streams(stdin, stdout, stderr):
                            stack.enter_context(patch)
                        self.assertIs(
                            tui.on_a_terminal(),
                            (stdin, stdout, stderr) == (True, True, True),
                            (stdin, stdout, stderr),
                        )

    def test_a_stream_that_was_closed_under_it(self):
        closed = io.StringIO()
        closed.close()  # isatty() raises ValueError
        with mock.patch.object(sys, "stdin", closed):
            self.assertIs(tui.on_a_terminal(), False)

    def test_cli_main_says_so_and_exits_two(self):
        for argv in (["tui"], ["--debug", "tui"]):
            # The streams of ``run`` are not a terminal.
            self.assertEqual(run(*argv), (2, "", tui.NO_TERMINAL + "\n"), argv)
        for argv in ([], ["--debug"]):  # no command: not a request for the TUI as such
            is_the_help(self, run(*argv), argv)

    def test_the_line(self):
        self.assertEqual(
            tui.NO_TERMINAL,
            "agent-sessions: the TUI needs a terminal (stdin, stdout and stderr)",
        )
        self.assertNotIn("\n", tui.NO_TERMINAL)

    def test_a_child_without_a_terminal(self):
        """Really without one: stdin from nowhere, the others pipes; and
        stdin closed (``agent-sessions <&-``), which was an AttributeError."""
        code = (
            f"import sys\nsys.path.insert(0, {_SRC!r})\n"
            "from agent_sessions import cli\nsys.exit(cli.main(sys.argv[1:]))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["tui"], []):
                for stdin in (subprocess.DEVNULL, None):
                    result = subprocess.run(
                        [sys.executable, "-c", code, *argv],
                        capture_output=True,
                        text=True,
                        stdin=stdin,
                        preexec_fn=no_stdin if stdin is None else None,
                        env=tui_support.pty_env(tmp),
                        timeout=60,
                        check=False,
                    )
                    said = (result.returncode, result.stdout, result.stderr)
                    if argv:
                        self.assertEqual(said, (2, "", tui.NO_TERMINAL + "\n"), stdin)
                    else:
                        is_the_help(self, said, stdin)
            self.assertEqual(os.listdir(tmp), [])  # and nothing was written on the way


class FakeApp:
    """In ``SessionsApp``'s place for ``tui.__main__.main``: what it was
    made with, and a ``run`` that does what the test says."""

    made: list = []
    status: int | None = 0
    note: object = None
    during = staticmethod(lambda: None)

    def __init__(self, engine=None):
        self.engine = engine
        self.return_code = None
        FakeApp.made.append(self)

    def run(self):
        FakeApp.during()
        self.return_code = FakeApp.status
        return FakeApp.note


class TuiMainTest(unittest.TestCase):
    """``tui.__main__.main``: what the launcher's two lines, ``tui.run`` and
    ``python -m agent_sessions.tui`` all call. The app is ``FakeApp``."""

    def setUp(self):
        FakeApp.made, FakeApp.status, FakeApp.note = [], 0, None
        FakeApp.during = staticmethod(lambda: None)
        self.app_module = types.SimpleNamespace(SessionsApp=FakeApp)
        for patch in (
            mock.patch.dict(sys.modules, {"agent_sessions.tui.app": self.app_module}),
            mock.patch.object(tui, "on_a_terminal", return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(logs.debug, False)

    def main(self, *argv, engine=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tui_main.main(list(argv), engine=engine)
        return code, out.getvalue(), err.getvalue()

    def test_it_runs_the_app_and_returns_its_exit_status(self):
        for status, expected in ((0, 0), (None, 0), (1, 1), (129, 129), (143, 143)):
            FakeApp.status = status
            self.assertEqual(self.main(), (expected, "", ""), status)
        self.assertEqual(len(FakeApp.made), 5)

    def test_the_engine_is_handed_on(self):
        engine = object()
        self.main(engine=engine)
        self.assertIs(FakeApp.made[0].engine, engine)
        with mock.patch.object(tui_main, "main", return_value=5) as main:
            self.assertEqual(tui.run(engine), 5)
            main.assert_called_once_with([], engine=engine)
            self.assertEqual(tui.run(debug=True), 5)
            main.assert_called_with(["--debug"], engine=None)

    def test_what_a_launch_had_to_say_is_printed_cleaned(self):
        FakeApp.note = "the terminal took a while"
        self.assertEqual(self.main(), (0, "", "agent-sessions: the terminal took a while\n"))
        FakeApp.note = tui_support.hostile("NOTE")  # a note can name a folder of a store
        _code, _out, err = self.main()
        self.assertTrue(err.startswith("agent-sessions: NOTE"))
        self.assertEqual(tui_support.unsafe_chars(err.rstrip("\n")), [])
        for nothing in (None, "", 0, ["x"]):
            FakeApp.note = nothing
            self.assertEqual(self.main(), (0, "", ""), nothing)

    def test_without_a_terminal_nothing_is_started(self):
        with mock.patch.object(tui, "on_a_terminal", return_value=False):
            for argv in ([], ["--debug"]):
                self.assertEqual(self.main(*argv), (2, "", tui.NO_TERMINAL + "\n"))
        self.assertEqual(FakeApp.made, [])

    def test_arguments(self):
        usage = "usage: python -m agent_sessions.tui [--debug]\n"
        for argv, status in (
            (["--help"], 0),
            (["-h"], 0),
            (["--debug", "--help"], 0),
            (["extra"], 2),
            (["tui"], 2),
            (["--debug", "extra"], 2),
            (["--help", "extra"], 2),
            (["--bogus"], 2),
        ):
            self.assertEqual(self.main(*argv), (status, "", usage), argv)
        self.assertEqual(FakeApp.made, [])

    def test_only_a_missing_textual_is_called_that(self):
        """Any other import that fails inside the app is a fault to see,
        not ``the TUI needs Textual``."""
        with (
            mock.patch.dict(sys.modules, {"agent_sessions.tui.app": None}),
            self.assertRaises(ImportError) as raised,
        ):
            self.main()
        self.assertEqual(raised.exception.name, "agent_sessions.tui.app")

    def test_debug_logs_more_and_keeps_the_copy_off_the_screen(self):
        """``--debug`` copies the log to stderr, and stderr is the terminal
        the app draws on: the copy is printed when the app has left it."""
        seen = {}

        def during():
            seen["level"] = logs.logger().level
            logs.logger().debug("a debug line")
            logs.logger().info("an info line")
            sys.stderr.write("<the screen>")

        FakeApp.during = staticmethod(during)
        code, out, err = self.main("--debug")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(seen["level"], 10)  # logging.DEBUG
        before, screen, after = err.partition("<the screen>")
        self.assertEqual((before, screen), ("", "<the screen>"))
        self.assertRegex(after, r"DEBUG a debug line\n.* INFO an info line\n$")
        log = fsutil.log_file().read_text(encoding="utf-8")
        self.assertIn("DEBUG a debug line", log)

    def test_debug_gives_the_copy_back_when_the_app_fails(self):
        def during():
            logs.logger().info("before it fell over")
            raise RuntimeError("boom")

        FakeApp.during = staticmethod(during)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(RuntimeError):
            tui_main.main(["--debug"])
        self.assertIn("INFO before it fell over\n", err.getvalue())

    def test_without_debug_nothing_is_copied(self):
        FakeApp.during = staticmethod(lambda: logs.logger().info("quietly"))
        self.assertEqual(self.main(), (0, "", ""))


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
        "from agent_sessions import cli, tui\n"
        "tui.on_a_terminal = lambda: True\n"  # as if a person were there
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
        for argv in (["tui"], []):  # no command is the TUI too
            result = self.child(
                "sys.modules['textual'] = None\n"
                "import contextlib, io\n"
                "from agent_sessions import cli\n"
                "with contextlib.redirect_stderr(io.StringIO()), "
                "contextlib.redirect_stdout(io.StringIO()):\n"
                f"    cli.main({argv!r})\n"
                "print('agent_sessions.tui' in sys.modules)\n"
            )
            self.assertEqual((result.returncode, result.stdout), (0, "True\n"), argv)

    def test_without_a_terminal_textual_is_not_even_looked_for(self):
        result = self.child(
            "from agent_sessions import cli\n"
            "code = cli.main(['tui'])\n"
            "drawing = sorted(m for m in sys.modules if m.split('.')[0] in ('textual', 'rich'))\n"
            "print(code, drawing)\n"
        )
        self.assertEqual((result.returncode, result.stdout), (0, "2 []\n"))
        self.assertEqual(result.stderr, tui.NO_TERMINAL + "\n")


# A module that says so when it is imported: planted under the names of
# modules the program imports, in the directory the program is started in.
PLANT = (
    "import os\n"
    "with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'RAN'), 'a') as fh:\n"
    "    fh.write(__name__ + '\\n')\n"
)
PLANTED = ("textual.py", "rich.py", "json.py", "logging.py", "argparse.py", "agent_sessions.py")


def plant(directory) -> Path:
    """Fill ``directory`` with planted modules; the file one of them makes
    when it runs."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in PLANTED:
        (directory / name).write_text(PLANT, encoding="utf-8")
    package = directory / "agent_sessions_plugins"
    package.mkdir()
    (package / "__init__.py").write_text(PLANT, encoding="utf-8")
    return directory / "RAN"


# In the place of ``.venv/bin/python``: writes down how it was started.
STUB_PYTHON = """#!{python}
import json, os, sys
with open(os.environ["STUB_LOG"], "w", encoding="utf-8") as fh:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}, fh)
sys.exit(7)
"""


class LauncherCase(unittest.TestCase):
    """A copy of the launcher beside a link to the real ``src``, in a temp
    directory: a checkout whose ``.venv`` is what the test puts there."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.root = self.tmp / "check out"
        (self.root / "bin").mkdir(parents=True)
        self.launcher = self.root / "bin" / "agent-sessions"
        shutil.copy(LAUNCHER, self.launcher)
        os.symlink(REPO / "src", self.root / "src")
        self.python = self.root / ".venv" / "bin" / "python"
        self.log = self.tmp / "stub.json"
        self.cwd = self.tmp / "cwd"
        self.marker = plant(self.cwd)
        self.env = tui_support.pty_env(self.tmp, STUB_LOG=str(self.log))

    def stub_python(self, text=None, mode=0o755):
        self.python.parent.mkdir(parents=True, exist_ok=True)
        self.python.write_text(
            STUB_PYTHON.format(python=sys.executable) if text is None else text, encoding="utf-8"
        )
        self.python.chmod(mode)

    def launch(self, *argv, stdin=subprocess.DEVNULL, **env):
        """Run the launcher; ``stdin=None`` starts it with stdin closed."""
        return subprocess.run(
            [sys.executable, str(self.launcher), *argv],
            capture_output=True,
            text=True,
            stdin=stdin,
            preexec_fn=no_stdin if stdin is None else None,
            env={**self.env, **env},
            cwd=str(self.cwd),
            timeout=60,
            check=False,
        )

    def started(self) -> dict:
        return json.loads(self.log.read_text(encoding="utf-8"))


class LauncherTest(LauncherCase):
    """AUDIT A-01 and A-21: what ``bin/agent-sessions`` starts for the TUI."""

    def test_the_tui_runs_under_the_venvs_python_cut_off_from_the_directory(self):
        self.stub_python()
        bootstrap = runpy.run_path(str(self.launcher), run_name="launcher")["BOOTSTRAP"]
        for argv, options in (
            ([], []),
            (["tui"], []),
            (["--debug"], ["--debug"]),
            (["--debug", "tui"], ["--debug"]),
            (["--debug", "--debug"], ["--debug"]),
            (["--deb", "tui"], ["--debug"]),  # as argparse reads it
            (["--d"], ["--debug"]),
        ):
            self.log.unlink(missing_ok=True)
            # On a terminal (made here, for the child alone): no command is
            # the TUI only there.
            run = tui_support.pty_run(
                [sys.executable, str(self.launcher), *argv], [], self.env, str(self.cwd)
            )
            self.assertEqual((run.status, tui_support.plain(run.output)), (7, ""), argv)
            # -P: the directory it is started in is not put on sys.path.
            self.assertEqual(
                self.started()["argv"],
                ["-P", "-c", bootstrap, str(self.root / "src"), *options],
                argv,
            )
            self.assertEqual(self.started()["cwd"], str(self.cwd))

    def test_the_environment_is_handed_on_as_it_is(self):
        """No ``PYTHONPATH`` of ours: every tool the TUI starts would
        inherit it. One the user set is theirs, and stays."""
        self.stub_python()
        self.launch("tui")
        env = self.started()["env"]
        self.assertNotIn("PYTHONPATH", env)
        for name, value in self.env.items():
            self.assertEqual(env[name], value, name)
        self.launch("tui", PYTHONPATH="/somewhere/of/the/users", MINE="kept")
        env = self.started()["env"]
        self.assertEqual(env["PYTHONPATH"], "/somewhere/of/the/users")
        self.assertEqual(env["MINE"], "kept")

    def test_the_two_lines_start_the_tui_from_src(self):
        """What the venv's Python is given: ``src`` goes on the path, the
        rest is the TUI's own argument list."""
        bootstrap = runpy.run_path(str(self.launcher), run_name="launcher")["BOOTSTRAP"]
        self.assertEqual(len(bootstrap.splitlines()), 2)
        for options, status, said in (
            (["--help"], 0, "usage: python -m agent_sessions.tui [--debug]\n"),
            (["extra"], 2, "usage: python -m agent_sessions.tui [--debug]\n"),
            ([], 2, tui.NO_TERMINAL + "\n"),  # no terminal here
            (["--debug"], 2, tui.NO_TERMINAL + "\n"),
        ):
            result = subprocess.run(
                [sys.executable, "-P", "-c", bootstrap, str(self.root / "src"), *options],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=self.env,
                cwd=str(self.cwd),
                timeout=60,
                check=False,
            )
            self.assertEqual((result.returncode, result.stdout, result.stderr), (status, "", said))
        self.assertFalse(self.marker.exists())

    def test_every_other_command_stays_on_this_python(self):
        self.stub_python()
        for argv in (
            ["--version"],
            ["tui", "--help"],
            ["--debug", "--version"],
            ["list", "--json"],
        ):
            result = self.launch(*argv)
            self.assertEqual(result.returncode, 0, (argv, result.stderr))
        for argv in (["tui", "extra"], ["tui", "--debug"], ["nope"]):
            self.assertEqual(self.launch(*argv).returncode, 2, argv)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.marker.exists())  # the directory was never on sys.path

    def test_nothing_in_the_current_directory_is_imported(self):
        """A-01. The same names beside a command that is not the TUI, and
        with the launcher run the way ``python -c`` runs it (the current
        directory first on ``sys.path``)."""
        for argv in (["--version"], ["list", "--json"], ["status"], ["tui"], []):
            self.launch(*argv)
        code = (
            "import sys, runpy\n"
            f"sys.argv = [{str(self.launcher)!r}, 'list', '--json']\n"
            f"runpy.run_path({str(self.launcher)!r}, run_name='__main__')\n"
        )
        (self.cwd / "runpy.py").unlink(missing_ok=True)
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=self.env,
            cwd=str(self.cwd),
            timeout=60,
            check=False,
        )
        self.assertEqual((result.returncode, result.stdout), (0, "[]\n"), result.stderr)
        self.assertFalse(
            self.marker.exists(), self.marker.read_text() if self.marker.exists() else ""
        )

    def test_a_venv_python_that_cannot_be_run_is_one_line(self):
        cases = (
            ("no permission to run it", None, 0o644, "Permission denied"),
            ("not a program", "not a program\n", 0o755, "Exec format error"),
        )
        for what, text, mode, reason in cases:
            self.stub_python(text, mode)
            result = self.launch("tui")
            self.assertEqual((result.returncode, result.stdout), (1, ""), what)
            self.assertEqual(
                result.stderr,
                f"agent-sessions: cannot run {self.python}: {reason}; "
                f"run ./install.sh in {self.root}\n",
                what,
            )
            self.python.unlink()

    def test_without_a_venv_this_python_runs_the_tui(self):
        """No ``.venv``, or one whose Python is a link to nowhere: the TUI's
        own ``main``, here, which says what is missing. (No terminal in this
        test, so that is what it says.)"""
        for dangling in (False, True):
            if dangling:
                self.python.parent.mkdir(parents=True)
                os.symlink(self.tmp / "nowhere" / "python", self.python)
            for argv in (["tui"], ["--debug", "tui"], ["--debug"], []):
                for stdin in (subprocess.DEVNULL, None):  # None: ``agent-sessions <&-``
                    result = self.launch(*argv, stdin=stdin)
                    said = (result.returncode, result.stdout, result.stderr)
                    if "tui" in argv:
                        expected = (2, "", tui.NO_TERMINAL + "\n")
                        self.assertEqual(said, expected, (dangling, argv, stdin))
                    else:
                        is_the_help(self, said, (dangling, argv, stdin))
        self.assertFalse(self.marker.exists())

    def test_stdin_closed_is_no_error_for_any_command(self):
        """``agent-sessions <&-`` was an AttributeError in the launcher."""
        self.stub_python()
        for argv, status in ((["--version"], 0), (["list", "--json"], 0), (["tui", "--help"], 0)):
            result = self.launch(*argv, stdin=None)
            self.assertEqual(result.returncode, status, (argv, result.stderr))
            self.assertNotIn("Error", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.launch("tui", stdin=None).returncode, 7)  # the venv's Python decides
        result = self.launch(stdin=None)
        is_the_help(self, (result.returncode, result.stdout, result.stderr), "no command")

    def test_a_python_that_is_too_old_says_so(self):
        """N-25: ``python3`` is whatever is first on PATH."""
        for version, shown in (
            ((3, 10, 12, "final", 0), "3.10"),
            ((3, 6, 0, "final", 0), "3.6"),
            ((2, 7, 18, "final", 0), "2.7"),
        ):
            code = (
                "import sys, runpy\n"
                f"sys.version_info = {version!r}\n"
                f"sys.argv = [{str(self.launcher)!r}, 'list']\n"
                f"runpy.run_path({str(self.launcher)!r}, run_name='__main__')\n"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=self.env,
                timeout=60,
                check=False,
            )
            self.assertEqual((result.returncode, result.stdout), (1, ""), shown)
            self.assertEqual(
                result.stderr,
                f"agent-sessions needs Python 3.11 or newer (this is {shown})\n",
            )

    def test_the_launcher_parses_on_an_old_python(self):
        """So that it gets as far as the line above. Nothing in it that came
        after Python 3.5, and nothing Python 2 cannot read; no old Python is
        installed to try it on, so the syntax tree is looked at instead."""
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertTrue(source.isascii())  # Python 2 wants a coding line otherwise
        tree = ast.parse(source, feature_version=(3, 7))
        too_new = (
            ast.JoinedStr,  # f-strings: 3.6
            ast.AnnAssign,  # annotated assignments: 3.6
            ast.NamedExpr,  # :=  3.8
            ast.Match,  # 3.10
            ast.AsyncFunctionDef,
            ast.Await,
            ast.Nonlocal,  # not in Python 2
            ast.Starred,  # [a, *b] is 3.5, and not in Python 2
        )
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, too_new, getattr(node, "lineno", None))
            if isinstance(node, ast.FunctionDef):
                self.assertIsNone(node.returns, node.name)
                arguments = node.args
                self.assertEqual(arguments.kwonlyargs + arguments.posonlyargs, [], node.name)
                for argument in arguments.args:
                    self.assertIsNone(argument.annotation, node.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "print")  # a statement in Python 2
        # The check comes before anything is imported from the program.
        check = source.index("sys.version_info < (3, 11)")
        self.assertLess(check, source.index("agent_sessions"))
        self.assertLess(check, source.index("def "))


def can_draw() -> bool:
    """Whether the launcher finds a Python with Textual: the checkout's
    venv, else the Python that runs these tests."""
    venv = REPO / ".venv" / "bin" / "python"
    return venv.is_file() or importlib.util.find_spec("textual") is not None


@unittest.skipUnless(can_draw(), "no Python with Textual for the launcher (run ./install.sh)")
class LauncherOnATerminalTest(unittest.TestCase):
    """The real launcher, started the way a user starts it: in a terminal
    (a pseudo-terminal made here for this child alone; no window is opened),
    in a directory that holds modules named like the ones the program
    imports. AUDIT A-01: ``agent-sessions`` in a folder of a mounted drive
    ran that folder's ``json.py`` as the user.

    The engine is the real one on an empty temp home with an empty mount
    list. The only key sent is ``q``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.cwd = self.tmp / "a folder of some drive"
        self.marker = plant(self.cwd)

    def play(self, *argv, **env):
        return tui_support.pty_run(
            [sys.executable, str(LAUNCHER), *argv],
            [("scanned just now", b"q")],  # the first screen, its scan done
            tui_support.pty_env(self.tmp, **env),
            str(self.cwd),
        )

    def test_the_first_screen_and_q_with_planted_modules_all_around(self):
        for argv in ([], ["tui"]):
            run = self.play(*argv)
            screen = tui_support.plain(run.output)
            self.assertEqual(run.status, 0, screen[-2000:])
            self.assertIn("agent-sessions", screen)
            self.assertIn("No sessions found", screen)
            self.assertIn("q quit", screen)
            self.assertNotIn("Traceback", screen)
            self.assertFalse(
                self.marker.exists(), self.marker.read_text() if self.marker.exists() else ""
            )
            # The terminal is given back: the alternate screen is left.
            self.assertGreater(run.output.rfind(b"\x1b[?1049l"), run.output.find(b"\x1b[?1049h"))

    def test_the_planted_modules_would_have_run(self):
        """The control: the way the launcher used to start the TUI, the same
        directory does run its ``json.py``."""
        venv = REPO / ".venv" / "bin" / "python"
        python = str(venv) if venv.is_file() else sys.executable
        result = subprocess.run(
            [python, "-c", "import json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=tui_support.pty_env(self.tmp),
            cwd=str(self.cwd),
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "json\n")

    def test_debug_starts_the_tui_and_its_copy_of_the_log_waits_for_the_end(self):
        """A-21: ``agent-sessions --debug`` said that the TUI needs Textual."""
        for argv in (["--debug"], ["--debug", "tui"]):
            run = self.play(*argv)
            self.assertEqual(run.status, 0, tui_support.plain(run.output)[-2000:])
            left = run.output.rfind(b"\x1b[?1049l")
            self.assertGreater(left, 0)
            drawn, after = (
                tui_support.plain(run.output[:left]),
                tui_support.plain(run.output[left:]),
            )
            self.assertIn("No sessions found", drawn)
            self.assertNotIn(" INFO scan", drawn)  # not over the interface
            self.assertRegex(after, r"\d\dZ \d+ INFO scan \w+: ")
            self.assertTrue((self.tmp / "state" / "agent-sessions.log").is_file())
        self.assertFalse(self.marker.exists())

    def test_the_users_own_pythonpath_does_not_stop_it(self):
        run = self.play(PYTHONPATH=str(self.tmp / "nothing here"))
        self.assertEqual(run.status, 0, tui_support.plain(run.output)[-2000:])


if __name__ == "__main__":
    unittest.main()
