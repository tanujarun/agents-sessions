"""``install.sh`` and ``uninstall.sh``, run for real, but only ever on a copy
of the checkout in a temp directory, with ``HOME`` and ``XDG_DATA_HOME``
pointing into that directory too. The real home and the installation in it
are never written; a guard below reads them before and after to say so.

The scripts run with a ``PATH`` that holds nothing but the handful of
commands they use and stand-ins made here: a ``python3`` that makes a
directory shaped like a venv and records what ``pip`` was asked to install
(no network, no real venv), and recorders for ``desktop-file-validate`` and
``update-desktop-database``. Skipped where there is no ``bash``."""

import json
import os
import pty
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

# What the scripts read of the checkout; nothing else is copied.
COPIED = ("install.sh", "uninstall.sh", "requirements-tui.txt", "pyproject.toml", "bin", "share")
# The commands the scripts run, besides the stand-ins.
TOOLS = ("readlink", "dirname", "mkdir", "ln", "install", "cmp", "rm")

FAKE_PYTHON = r"""#!{python}
# A stand-in for python3, and for the python of the venv it "creates".
import json, os, shutil, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(["python", *args]) + "\n")
if args[:2] == ["-m", "venv"]:
    if os.environ.get("FAKE_VENV_FAILS"):
        sys.exit("Error: could not create the venv")
    target = os.path.join(args[2], "bin")
    os.makedirs(target, exist_ok=True)
    shutil.copy(os.path.realpath(__file__), os.path.join(target, "python"))
    sys.exit(0)
if args[:2] == ["-m", "pip"]:
    if os.environ.get("FAKE_NO_PIP"):
        sys.exit("No module named pip")
    sys.exit(1 if args[2] == "install" and os.environ.get("FAKE_PIP_FAILS") else 0)
if args[:1] == ["-c"]:
    code = args[1]
    version = tuple(int(part) for part in os.environ.get("FAKE_VERSION", "3.12.1").split("."))
    if "importlib.metadata" in code:
        print("textual 8.2.8")
    elif "import venv" in code:
        sys.exit(1 if os.environ.get("FAKE_NO_VENV") else 0)
    elif "print" in code:
        print("%d.%d.%d" % version)
    else:
        sys.exit(0 if version >= (3, 11) else 1)
    sys.exit(0)
sys.exit("the stand-in python was asked something it does not know: %r" % (args,))
"""

RECORDER = r"""#!{python}
import json, os, sys

name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\n")
sys.exit(int(os.environ.get("FAKE_" + name.upper().replace("-", "_"), "0")))
"""


def real_installation():
    """What the real home holds of an installation, read and never written:
    the scripts under test must leave it exactly as it is."""
    home = Path.home()
    seen = []
    for path in (
        home / ".local" / "bin" / "agent-sessions",
        home / ".local" / "share" / "applications" / "agent-sessions.desktop",
    ):
        try:
            st = os.lstat(path)
        except OSError:
            seen.append(None)
            continue
        target = os.readlink(path) if stat.S_ISLNK(st.st_mode) else None
        seen.append((st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino, target))
    return seen


@unittest.skipUnless(BASH, "bash is not installed")
class ScriptCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = real_installation()

    @classmethod
    def tearDownClass(cls):
        assert real_installation() == cls.before, "the real home's installation changed"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.repo = self.tmp / "check out"  # a space: the scripts quote their paths
        self.repo.mkdir()
        for name in COPIED:
            if (REPO / name).is_dir():
                shutil.copytree(REPO / name, self.repo / name)
            else:
                shutil.copy2(REPO / name, self.repo / name)
        self.home = self.tmp / "home of me"
        self.home.mkdir()
        self.assertNotEqual(self.home, Path.home())
        self.log = self.tmp / "calls.jsonl"
        self.log.touch()
        self.fake = self.tmp / "fake-bin"
        self.fake.mkdir()
        self.stand_in("python3", FAKE_PYTHON)
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool)
            self.assertIsNotNone(found, tool)
            os.symlink(found, self.tools / tool)
        self.link = self.home / ".local" / "bin" / "agent-sessions"
        self.apps = self.home / ".local" / "share" / "applications"
        self.desktop = self.apps / "agent-sessions.desktop"

    def stand_in(self, name, text=RECORDER):
        path = self.fake / name
        path.write_text(text.replace("{python}", sys.executable), encoding="utf-8")
        path.chmod(0o755)

    def env(self, **extra):
        env = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "PATH": os.pathsep.join([str(self.fake), str(self.tools)]),
            "FAKE_LOG": str(self.log),
        }
        env.update(extra)
        return {key: value for key, value in env.items() if value is not None}

    def script(self, name, *args, stdin=subprocess.DEVNULL, **extra):
        """Run a script of the COPY; (exit status, stdout, stderr)."""
        done = subprocess.run(
            [BASH, str(self.repo / name), *args],
            capture_output=True,
            text=True,
            env=self.env(**extra),
            cwd=str(self.tmp),
            stdin=stdin,
            timeout=60,
            check=False,
        )
        return done.returncode, done.stdout, done.stderr

    def install(self, *args, **extra):
        return self.script("install.sh", *args, **extra)

    def uninstall(self, *args, **extra):
        return self.script("uninstall.sh", *args, **extra)

    def calls(self, name=None):
        lines = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [call[1:] for call in lines if name is None or call[0] == name]

    def pip_installs(self):
        return [call[3:] for call in self.calls("python") if call[:3] == ["-m", "pip", "install"]]

    def home_files(self):
        return sorted(
            os.path.relpath(os.path.join(base, name), self.home)
            for base, _dirs, files in os.walk(self.home)
            for name in files
        )


class InstallTest(ScriptCase):
    def test_a_first_run(self):
        code, out, err = self.install()
        self.assertEqual((code, err), (0, self.no_launcher_warning() + self.path_warning()))
        self.assertEqual(
            out.splitlines(),
            [
                f"venv: {self.repo / '.venv'} (textual 8.2.8)",
                f"agent-sessions -> {self.repo / 'bin' / 'agent-sessions'}",
                f"desktop launcher: {self.desktop}",
            ],
        )
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(self.repo / "bin" / "agent-sessions"))
        self.assertEqual(
            self.desktop.read_bytes(), (REPO / "share" / "agent-sessions.desktop").read_bytes()
        )
        self.assertEqual(stat.S_IMODE(self.desktop.stat().st_mode), 0o644)
        self.assertEqual(
            self.home_files(),
            [".local/bin/agent-sessions", ".local/share/applications/agent-sessions.desktop"],
        )
        # The venv is made in the checkout, and Textual alone goes into it.
        self.assertIn(["-m", "venv", str(self.repo / ".venv")], self.calls("python"))
        self.assertEqual(
            self.pip_installs(),
            [
                [
                    "--quiet",
                    "--disable-pip-version-check",
                    "-r",
                    str(self.repo / "requirements-tui.txt"),
                ]
            ],
        )

    def no_launcher_warning(self) -> str:
        return (
            "warning: the desktop launcher starts the TUI with omarchy-launch-tui, which is "
            "not on PATH; start agent-sessions from a terminal instead\n"
        )

    def path_warning(self) -> str:
        return ""  # see test_a_warning_when_the_bin_directory_is_not_on_path

    def env(self, **extra):
        # ~/.local/bin on PATH unless a test takes it off.
        env = super().env(**extra)
        if "PATH" not in extra:
            env["PATH"] += os.pathsep + str(self.home / ".local" / "bin")
        return env

    def test_a_second_run_changes_nothing(self):
        first = self.install()
        before = (os.readlink(self.link), self.desktop.read_bytes(), self.home_files())
        venvs = len([c for c in self.calls("python") if c[:2] == ["-m", "venv"]])
        second = self.install()
        self.assertEqual(second, first)
        self.assertEqual(
            (os.readlink(self.link), self.desktop.read_bytes(), self.home_files()), before
        )
        # The venv that is there is used, not made again.
        self.assertEqual(len([c for c in self.calls("python") if c[:2] == ["-m", "venv"]]), venvs)
        self.assertEqual(len(self.pip_installs()), 2)

    def test_the_development_tools_only_with_dev(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install("--dev")[0], 0)
        plain, dev = self.pip_installs()
        self.assertNotIn("dev", " ".join(plain))
        self.assertNotIn("-e", plain)
        self.assertEqual(dev[-2:], ["-e", f"{self.repo}[tui,dev]"])

    def test_a_python_older_than_3_11_is_refused_before_anything_is_made(self):
        for version in ("3.10.14", "3.9.2", "2.7.18"):
            code, out, err = self.install(FAKE_VERSION=version)
            self.assertEqual((code, out), (1, ""), version)
            self.assertEqual(
                err,
                f"install.sh: python3 is Python {version}; agent-sessions needs Python 3.11 "
                "or newer (PYTHON=/path/to/python ./install.sh uses another one)\n",
            )
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse((self.repo / ".venv").exists())
        for version in ("3.11.0", "3.14.0", "4.0.0"):
            self.assertEqual(self.install(FAKE_VERSION=version)[0], 0, version)

    def test_no_python_at_all(self):
        code, out, err = self.install(PYTHON="python3.99")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err, "install.sh: python3.99 was not found; agent-sessions needs Python 3.11 or newer\n"
        )
        self.assertEqual(os.listdir(self.home), [])

    def test_python_names_the_interpreter(self):
        shutil.copy(self.fake / "python3", self.fake / "python3.13")
        (self.fake / "python3").unlink()
        self.assertEqual(self.install()[0], 1)  # no python3 now
        self.assertEqual(self.install(PYTHON="python3.13")[0], 0)

    def test_no_venv_module(self):
        code, out, err = self.install(FAKE_NO_VENV="1")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "install.sh: python3 has no venv module with pip (on Debian and Ubuntu: install "
            "python3-venv)\n",
        )
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse((self.repo / ".venv").exists())

    def test_a_venv_that_cannot_be_made_or_has_no_pip(self):
        code, _out, err = self.install(FAKE_VENV_FAILS="1")
        self.assertEqual(code, 1)
        self.assertIn(f"install.sh: could not create {self.repo / '.venv'}\n", err)
        code, _out, err = self.install(FAKE_NO_PIP="1")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"install.sh: {self.repo / '.venv'} has no pip; remove {self.repo / '.venv'} and "
            "run ./install.sh again\n",
        )
        code, _out, err = self.install(FAKE_PIP_FAILS="1")
        self.assertEqual(code, 1)
        self.assertEqual(err, "install.sh: pip could not install Textual (requirements-tui.txt)\n")
        self.assertEqual(os.listdir(self.home), [])  # nothing was linked or installed

    def test_a_warning_when_the_bin_directory_is_not_on_path(self):
        path = os.pathsep.join([str(self.fake), str(self.tools)])
        code, _out, err = self.install(PATH=path)
        self.assertEqual(code, 0)
        self.assertIn(
            f"warning: {self.home / '.local' / 'bin'} is not on PATH; add it, or run {self.link}\n",
            err,
        )
        code, _out, err = self.install()
        self.assertEqual(code, 0)
        self.assertNotIn("is not on PATH; add it", err)

    def test_the_desktop_file_is_validated_when_the_validator_exists(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.calls("desktop-file-validate"), [])  # there is none: installed
        self.assertTrue(self.desktop.is_file())
        self.desktop.unlink()
        self.stand_in("desktop-file-validate")
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(
            self.calls("desktop-file-validate"),
            [[str(self.repo / "share" / "agent-sessions.desktop")]],
        )
        self.assertTrue(self.desktop.is_file())
        self.desktop.unlink()
        code, _out, err = self.install(FAKE_DESKTOP_FILE_VALIDATE="1")
        self.assertEqual(code, 1)
        self.assertIn("agent-sessions.desktop is not a valid desktop file\n", err)
        self.assertFalse(self.desktop.exists())

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "no desktop-file-validate here")
    def test_the_real_validator_accepts_the_desktop_file(self):
        done = subprocess.run(
            ["desktop-file-validate", str(REPO / "share" / "agent-sessions.desktop")],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_the_desktop_database_is_refreshed_when_its_tool_exists(self):
        self.stand_in("update-desktop-database")
        self.stand_in("omarchy-launch-tui")
        code, _out, err = self.install()
        self.assertEqual((code, err), (0, ""))  # and no warning about the launcher
        self.assertEqual(self.calls("update-desktop-database"), [["-q", str(self.apps)]])
        self.assertEqual(self.calls("omarchy-launch-tui"), [])  # looked for, never run

    def test_xdg_data_home_is_where_the_launcher_goes(self):
        data = self.tmp / "data dir"
        self.assertEqual(self.install(XDG_DATA_HOME=str(data))[0], 0)
        self.assertTrue((data / "applications" / "agent-sessions.desktop").is_file())
        self.assertFalse(self.desktop.exists())
        self.assertEqual(self.install(XDG_DATA_HOME=None)[0], 0)  # unset: ~/.local/share
        self.assertTrue(self.desktop.is_file())

    def test_a_file_that_is_not_a_link_is_not_replaced(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        code, _out, err = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            f"install.sh: {self.link} exists and is not a symbolic link; move it away and run "
            "./install.sh again\n",
        )
        self.assertEqual(self.link.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        self.assertFalse(self.desktop.exists())

    def test_a_link_to_another_checkout_is_pointed_here(self):
        self.link.parent.mkdir(parents=True)
        os.symlink("/somewhere/else/bin/agent-sessions", self.link)  # dangling, too
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(os.readlink(self.link), str(self.repo / "bin" / "agent-sessions"))

    def test_arguments(self):
        self.assertEqual(self.install("--help"), (0, "usage: ./install.sh [--dev]\n", ""))
        self.assertEqual(self.install("--nope"), (2, "", "usage: ./install.sh [--dev]\n"))
        self.assertEqual(self.install("--dev", "extra")[0], 2)
        self.assertEqual(self.calls(), [])  # nothing was run for any of them
        self.assertEqual(os.listdir(self.home), [])

    def test_no_home(self):
        code, _out, err = self.install(HOME="")
        self.assertEqual((code, err), (1, "install.sh: HOME is not set\n"))
        self.assertEqual(self.calls(), [])

    def test_the_installed_link_runs_the_copy_s_launcher(self):
        """The link is a way to the checkout's launcher: run through it (with
        the real Python, on ``--version``) it answers as the launcher does."""
        self.assertEqual(self.install()[0], 0)
        shutil.copytree(
            REPO / "src", self.repo / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
        done = subprocess.run(
            [sys.executable, str(self.link), "--version"],
            capture_output=True,
            text=True,
            env={"HOME": str(self.home), "PATH": str(self.tools)},
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertRegex(done.stdout, r"^agent-sessions \d+\.\d+\.\d+\n$")


class UninstallTest(ScriptCase):
    LEFT = "left as they are: the session stores, and this checkout with its .venv\n"

    def setUp(self):
        super().setUp()
        self.assertEqual(self.install()[0], 0)
        self.dirs = [
            self.home / ".cache" / "agent-sessions",
            self.home / ".local" / "state" / "agent-sessions",
            self.home / ".config" / "agent-sessions",
        ]
        for directory in self.dirs:
            (directory / "sub").mkdir(parents=True)
            (directory / "sub" / "file").write_text("x", encoding="utf-8")
        # What must survive everything: a session store, and neighbours.
        self.kept = [
            self.home / ".claude" / "projects" / "-home-me" / "s.jsonl",
            self.home / ".codex" / "sessions" / "rollout.jsonl",
            self.home / ".cache" / "other" / "file",
            self.home / ".config" / "other" / "file",
            self.home / ".local" / "state" / "other" / "file",
            self.home / ".local" / "bin" / "other",
            self.apps / "other.desktop",
        ]
        for path in self.kept:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep", encoding="utf-8")

    def assert_kept(self):
        for path in self.kept:
            self.assertEqual(path.read_text(encoding="utf-8"), "keep", path)
        self.assertTrue((self.repo / ".venv" / "bin" / "python").is_file())
        self.assertTrue((self.repo / "install.sh").is_file())

    def test_it_removes_the_link_and_the_launcher_and_nothing_else(self):
        code, out, err = self.uninstall()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out,
            f"removed {self.link}\n"
            f"removed {self.desktop}\n" + self.LEFT + "to remove the checkout too: rm -rf "
            f"{str(self.repo).replace(' ', chr(92) + ' ')}\n",
        )
        self.assertFalse(os.path.lexists(self.link))
        self.assertFalse(self.desktop.exists())
        for directory in self.dirs:  # not asked for: not purged
            self.assertTrue((directory / "sub" / "file").is_file())
        self.assert_kept()

    def test_a_second_run_finds_nothing_and_is_not_an_error(self):
        self.uninstall()
        code, out, err = self.uninstall()
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"no {self.link}\n", out)
        self.assertIn(f"no {self.desktop}\n", out)
        self.assert_kept()

    def test_a_link_that_points_elsewhere_is_left(self):
        self.link.unlink()
        os.symlink("/somewhere/else/bin/agent-sessions", self.link)
        code, out, _err = self.uninstall()
        self.assertEqual(code, 0)
        self.assertIn(
            f"left {self.link}: it points to /somewhere/else/bin/agent-sessions, not into this "
            "checkout\n",
            out,
        )
        self.assertEqual(os.readlink(self.link), "/somewhere/else/bin/agent-sessions")
        self.assertFalse(self.desktop.exists())  # the launcher was this checkout's

    def test_a_link_to_another_copy_of_the_checkout_is_left(self):
        other = self.tmp / "other checkout"
        shutil.copytree(self.repo, other, symlinks=True)
        self.link.unlink()
        os.symlink(other / "bin" / "agent-sessions", self.link)
        self.assertEqual(self.uninstall()[0], 0)
        self.assertTrue(self.link.is_symlink())
        # And that copy's own script takes it.
        self.assertEqual(self.script(str(other / "uninstall.sh"))[0], 0)
        self.assertFalse(os.path.lexists(self.link))

    def test_a_file_in_the_link_s_place_is_left(self):
        self.link.unlink()
        self.link.write_text("mine", encoding="utf-8")
        code, out, _err = self.uninstall()
        self.assertEqual(code, 0)
        self.assertIn(f"left {self.link}: it is not a symbolic link", out)
        self.assertEqual(self.link.read_text(encoding="utf-8"), "mine")

    def test_a_launcher_that_is_not_this_checkout_s_is_left(self):
        original = self.desktop.read_text(encoding="utf-8")
        for text in (original + "X-Mine=1\n", original.replace("Agent Sessions", "Mine"), ""):
            self.desktop.write_text(text, encoding="utf-8")
            code, out, _err = self.uninstall()
            self.assertEqual(code, 0)
            self.assertIn(
                f"left {self.desktop}: it is not the launcher this checkout installs\n", out
            )
            self.assertEqual(self.desktop.read_text(encoding="utf-8"), text)
        self.desktop.unlink()
        os.symlink(self.repo / "share" / "agent-sessions.desktop", self.desktop)
        code, out, _err = self.uninstall()
        self.assertIn(f"left {self.desktop}: it is a symbolic link", out)
        self.assertTrue(self.desktop.is_symlink())

    def test_the_desktop_database_is_refreshed(self):
        self.stand_in("update-desktop-database")
        self.assertEqual(self.uninstall()[0], 0)
        self.assertEqual(self.calls("update-desktop-database"), [["-q", str(self.apps)]])

    def test_purge_without_a_terminal_needs_yes_and_removes_nothing_at_all(self):
        code, out, err = self.uninstall("--purge")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "uninstall.sh: not a terminal to ask on; pass --yes to purge without the question\n",
        )
        self.assertTrue(self.link.is_symlink())  # not half an uninstall
        self.assertTrue(self.desktop.is_file())
        for directory in self.dirs:
            self.assertTrue((directory / "sub" / "file").is_file())

    def test_purge_with_yes_removes_the_three_directories(self):
        code, out, err = self.uninstall("--purge", "--yes")
        self.assertEqual((code, err), (0, ""))
        for directory in self.dirs:
            self.assertFalse(directory.exists())
            self.assertIn(f"removed {directory}\n", out)
            self.assertTrue(directory.parent.is_dir())
        self.assertIn(self.LEFT, out)
        self.assert_kept()
        # Again: nothing is left to purge, and that is not an error.
        code, out, err = self.uninstall("--purge", "--yes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("nothing to purge: no cache, state or configuration directory\n", out)

    def test_yes_alone_purges_nothing(self):
        self.assertEqual(self.uninstall("--yes")[0], 0)
        for directory in self.dirs:
            self.assertTrue((directory / "sub" / "file").is_file())

    def test_a_directory_that_is_a_link_is_not_followed(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "precious").write_text("keep", encoding="utf-8")
        shutil.rmtree(self.dirs[0])
        os.symlink(elsewhere, self.dirs[0])
        code, out, _err = self.uninstall("--purge", "--yes")
        self.assertEqual(code, 0)
        self.assertNotIn(f"removed {self.dirs[0]}\n", out)
        self.assertTrue(self.dirs[0].is_symlink())
        self.assertEqual((elsewhere / "precious").read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.dirs[1].exists())

    def answer(self, text):
        """Run ``uninstall.sh --purge`` with a terminal for its stdin (a pty
        made here, for this child alone) and type ``text`` on it."""
        master, slave = pty.openpty()
        try:
            os.write(master, text.encode())
            return self.uninstall("--purge", stdin=slave)
        finally:
            os.close(master)
            os.close(slave)

    def test_purge_asks_and_only_y_is_a_yes(self):
        for text in ("n\n", "\n", "maybe\n"):
            code, out, err = self.answer(text)
            self.assertEqual(code, 1, text)
            self.assertEqual(err, "Remove them? [y/N] ")
            self.assertTrue(out.endswith("nothing was purged\n"), out)
            for directory in self.dirs:
                self.assertIn(f"  {directory}\n", out)
                self.assertTrue((directory / "sub" / "file").is_file())
        code, out, err = self.answer("y\n")
        self.assertEqual((code, err), (0, "Remove them? [y/N] "))
        for directory in self.dirs:
            self.assertFalse(directory.exists())
        self.assert_kept()

    def test_arguments(self):
        usage = "usage: ./uninstall.sh [--purge [--yes]]\n"
        self.assertEqual(self.uninstall("--help"), (0, usage, ""))
        self.assertEqual(self.uninstall("--force"), (2, "", usage))
        self.assertEqual(self.uninstall("--purge", "--yes", "x"), (2, "", usage))
        self.assertTrue(self.link.is_symlink())
        for home in ("", "relative/home"):
            code, _out, err = self.uninstall("--purge", "--yes", HOME=home)
            self.assertEqual(code, 1, home)
            self.assertIn("uninstall.sh: HOME is not", err)
        for directory in self.dirs:
            self.assertTrue((directory / "sub" / "file").is_file())


class ScriptTextTest(unittest.TestCase):
    """What can be read off the scripts without running them."""

    def test_both_are_strict_and_executable(self):
        for name in ("install.sh", "uninstall.sh"):
            path = REPO / name
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "#!/usr/bin/env bash", name)
            self.assertIn("set -euo pipefail", lines, name)
            self.assertTrue(os.access(path, os.X_OK), name)

    def test_uninstall_names_no_session_store_in_a_command(self):
        """The stores appear in its comments and in one message, never in
        anything it removes."""
        for line in (REPO / "uninstall.sh").read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            if "rm " in code and not code.lstrip().startswith("echo "):
                for store in (".claude", ".codex", ".grok", ".gemini"):
                    self.assertNotIn(store, code)
                self.assertRegex(code, r'rm -r?f -- "\$(link|desktop|dir)"')

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck_is_clean(self):
        done = subprocess.run(
            ["shellcheck", str(REPO / "install.sh"), str(REPO / "uninstall.sh")],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
