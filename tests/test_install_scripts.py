"""``install.sh`` and ``uninstall.sh``, run for real, but only ever on a copy
of the checkout in a temp directory, with ``HOME`` and ``XDG_DATA_HOME``
pointing into that directory too. The real home and the installation in it
are never written; a guard below reads them before and after to say so.

The scripts run with a ``PATH`` that holds nothing but the handful of
commands they use and stand-ins made here: a ``python3`` that makes a
directory shaped like a venv and records what ``pip`` was asked to install
(no network, no real venv), and recorders for ``desktop-file-validate`` and
``update-desktop-database``. Skipped where there is no ``bash``."""

import hashlib
import json
import os
import pty
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
DESKTOP = REPO / "share" / "agent-sessions.desktop"
# The launcher 0.2.0 and earlier installed (before ``TryExec``): its SHA-256 is
# in ``LAUNCHERS_BEFORE`` of both scripts.
LAUNCHER_0_2_0 = DESKTOP.read_text(encoding="utf-8").replace("TryExec=omarchy-launch-tui\n", "")
# What a user off Omarchy makes of it (README: another terminal in ``Exec``).
LAUNCHER_EDITED = DESKTOP.read_text(encoding="utf-8").replace(
    "Exec=omarchy-launch-tui --app-id=org.omarchy.sessions agent-sessions",
    "Exec=kitty agent-sessions",
)

# What the scripts read of the checkout; nothing else is copied.
COPIED = ("install.sh", "uninstall.sh", "requirements-tui.txt", "pyproject.toml", "bin", "share")
# The commands the scripts run, besides the stand-ins.
TOOLS = ("readlink", "dirname", "mkdir", "ln", "install", "cmp", "rm", "rmdir", "sha256sum")

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
    # It answers what it is asked and decides nothing: whether a version will
    # do is install.sh's own check, on the text printed here.
    code = args[1]
    if "importlib.metadata" in code:
        print("textual 8.2.8")
        sys.exit(0)
    if "import venv" in code:
        sys.exit(1 if os.environ.get("FAKE_NO_VENV") else 0)
    if "sys.version_info" in code and "print" in code:
        print(os.environ.get("FAKE_VERSION", "3.12.1"))
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
        """The floor is install.sh's own: the stand-in only says which version
        it is (3.10 is refused and 3.11 taken, whatever the other numbers)."""
        for version in ("3.10.14", "3.10.0", "3.10.99", "3.9.2", "3.2.11", "2.7.18", "2.11.0"):
            code, out, err = self.install(FAKE_VERSION=version)
            self.assertEqual((code, out), (1, ""), version)
            self.assertEqual(
                err,
                f"install.sh: python3 is Python {version}; agent-sessions needs Python 3.11 "
                "or newer (PYTHON=/path/to/python ./install.sh uses another one)\n",
            )
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse((self.repo / ".venv").exists())
        for version in ("3.11.0", "3.11.9", "3.12.1", "3.14.0", "3.100.0", "4.0.0"):
            self.assertEqual(self.install(FAKE_VERSION=version)[0], 0, version)

    def test_a_python_that_does_not_say_which_it_is_is_refused(self):
        for answer in ("", "three", "3", "3.x.1", "3.", ".11"):
            code, out, err = self.install(FAKE_VERSION=answer)
            self.assertEqual((code, out), (1, ""), answer)
            self.assertEqual(
                err,
                f"install.sh: python3 did not say which Python it is (it printed: {answer})\n",
            )
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse((self.repo / ".venv").exists())

    def test_the_stand_in_python_decides_nothing_itself(self):
        """Asked whether its version will do, it has no answer: a script that
        left the floor to the interpreter would fail on it, not pass."""
        for code in (
            "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)",
            "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)",
        ):
            done = subprocess.run(
                [str(self.fake / "python3"), "-c", code],
                capture_output=True,
                text=True,
                env=self.env(),
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
            self.assertEqual(done.returncode, 1)
            self.assertIn("asked something it does not know", done.stderr)

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

    def test_a_link_that_points_elsewhere_is_left_unless_forced(self):
        """Another checkout's link, some other program's, one that leads
        nowhere: none is repointed without ``--force`` (docs/AUDIT.md A-22),
        and the refusal comes before anything is made."""
        mine = self.home / "tools" / "my-own-tool"
        mine.parent.mkdir()
        mine.write_text("#!/bin/sh\n", encoding="utf-8")
        self.link.parent.mkdir(parents=True)
        for target in (str(mine), "/somewhere/else/bin/agent-sessions", "../x/agent-sessions"):
            os.symlink(target, self.link)
            code, out, err = self.install()
            self.assertEqual((code, out), (1, ""), target)
            self.assertEqual(
                err,
                f"install.sh: {self.link} points to {target}, not into this checkout; it was "
                "left as it is (--force points it here)\n",
            )
            self.assertEqual(os.readlink(self.link), target)
            self.assertFalse(self.desktop.exists())
            self.assertFalse((self.repo / ".venv").exists())
            self.assertEqual(self.calls("python")[-1][0], "-c")  # no venv, no pip

            code, out, err = self.install("--force")
            self.assertEqual(code, 0, err)
            ours = self.repo / "bin" / "agent-sessions"
            self.assertIn(f"agent-sessions -> {ours} (replaced the link to {target})\n", out)
            self.assertEqual(os.readlink(self.link), str(ours))
            self.assertEqual(mine.read_text(encoding="utf-8"), "#!/bin/sh\n")
            # Ours now: the next run has nothing to replace, forced or not.
            for args in ((), ("--force",)):
                code, out, _err = self.install(*args)
                self.assertEqual(code, 0)
                self.assertIn(f"agent-sessions -> {ours}\n", out)
            self.link.unlink()
            shutil.rmtree(self.repo / ".venv")
            self.desktop.unlink()

    def test_a_link_to_this_checkout_by_another_way_is_ours(self):
        way = self.tmp / "way"
        os.symlink(self.repo, way)
        self.link.parent.mkdir(parents=True)
        os.symlink(way / "bin" / "agent-sessions", self.link)
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn(f"agent-sessions -> {self.repo / 'bin' / 'agent-sessions'}\n", out)

    def test_force_does_not_replace_a_file_in_the_link_s_place(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("mine", encoding="utf-8")
        code, _out, err = self.install("--force")
        self.assertEqual(code, 1)
        self.assertIn("exists and is not a symbolic link", err)
        self.assertEqual(self.link.read_text(encoding="utf-8"), "mine")

    # --- a launcher that is there already (A-22) ---

    def kept_line(self, why="it is not a launcher this script installed") -> str:
        return f"kept {self.desktop}: {why} (--force replaces it)\n"

    def test_a_launcher_someone_edited_is_kept_and_reported(self):
        self.apps.mkdir(parents=True)
        self.stand_in("update-desktop-database")
        for text in (LAUNCHER_EDITED, LAUNCHER_0_2_0 + "X-Mine=1\n", "", "[Desktop Entry]\n"):
            self.desktop.write_text(text, encoding="utf-8")
            for _run in range(2):  # every run, not only the first
                code, out, err = self.install()
                self.assertEqual((code, err), (0, ""), text)  # no launcher warning either
                self.assertEqual(out.splitlines()[-1] + "\n", self.kept_line())
                self.assertNotIn("desktop launcher:", out)
                self.assertEqual(self.desktop.read_text(encoding="utf-8"), text)
            self.assertTrue(self.link.is_symlink())  # the rest was installed
        self.assertEqual(self.calls("update-desktop-database"), [])

        code, out, _err = self.install("--force")
        self.assertEqual(code, 0)
        self.assertIn(
            f"desktop launcher: {self.desktop} (replaced a launcher that was not this "
            "checkout's)\n",
            out,
        )
        self.assertEqual(self.desktop.read_bytes(), DESKTOP.read_bytes())

    def test_the_launcher_of_an_earlier_version_is_replaced(self):
        """One this script installed and nobody touched: the new one takes
        its place, and the run says so. Edited by one byte, it is kept."""
        self.apps.mkdir(parents=True)
        self.desktop.write_text(LAUNCHER_0_2_0, encoding="utf-8")
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn(
            f"desktop launcher: {self.desktop} (replaced the launcher of an earlier version)\n",
            out,
        )
        self.assertEqual(self.desktop.read_bytes(), DESKTOP.read_bytes())
        self.desktop.write_text(LAUNCHER_0_2_0 + "\n", encoding="utf-8")
        code, out, _err = self.install()
        self.assertIn(self.kept_line(), out)
        self.assertEqual(self.desktop.read_text(encoding="utf-8"), LAUNCHER_0_2_0 + "\n")

    def test_without_a_hashing_tool_an_earlier_launcher_is_kept_not_guessed_at(self):
        (self.tools / "sha256sum").unlink()
        self.apps.mkdir(parents=True)
        self.desktop.write_text(LAUNCHER_0_2_0, encoding="utf-8")
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn(self.kept_line(), out)
        self.assertEqual(self.desktop.read_text(encoding="utf-8"), LAUNCHER_0_2_0)

    def test_an_unchanged_launcher_is_not_written_again(self):
        self.assertEqual(self.install()[0], 0)
        then = 1_600_000_000
        os.utime(self.desktop, (then, then))
        before = os.stat(self.desktop)
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn(f"desktop launcher: {self.desktop}\n", out)
        after = os.stat(self.desktop)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_a_launcher_that_is_a_link_is_kept_and_never_written_through(self):
        dotfiles = self.home / "dotfiles" / "agent-sessions.desktop"
        dotfiles.parent.mkdir()
        dotfiles.write_text("precious", encoding="utf-8")
        self.apps.mkdir(parents=True)
        os.symlink(dotfiles, self.desktop)
        code, out, _err = self.install()
        self.assertEqual(code, 0)
        self.assertIn(
            self.kept_line("it is a symbolic link, not a launcher this script installed"), out
        )
        self.assertTrue(self.desktop.is_symlink())
        code, out, _err = self.install("--force")
        self.assertEqual(code, 0)
        self.assertIn(
            f"desktop launcher: {self.desktop} (replaced a symbolic link to {dotfiles})\n", out
        )
        self.assertFalse(self.desktop.is_symlink())
        self.assertEqual(self.desktop.read_bytes(), DESKTOP.read_bytes())
        self.assertEqual(dotfiles.read_text(encoding="utf-8"), "precious")

    def test_a_directory_in_the_launcher_s_place_stops_the_run_first(self):
        self.desktop.mkdir(parents=True)
        for args in ((), ("--force",)):
            code, out, err = self.install(*args)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(
                err,
                f"install.sh: {self.desktop} is a directory; move it away and run ./install.sh "
                "again\n",
            )
        self.assertEqual(os.listdir(self.desktop), [])
        self.assertFalse(os.path.lexists(self.link))
        self.assertFalse((self.repo / ".venv").exists())

    def test_arguments(self):
        usage = "usage: ./install.sh [--dev] [--force]\n"
        self.assertEqual(self.install("--help"), (0, usage, ""))
        self.assertEqual(self.install("--nope"), (2, "", usage))
        self.assertEqual(self.install("--dev", "extra")[0], 2)
        self.assertEqual(self.calls(), [])  # nothing was run for any of them
        self.assertEqual(os.listdir(self.home), [])

    def test_no_home(self):
        code, _out, err = self.install(HOME="")
        self.assertEqual((code, err), (1, "install.sh: HOME is not set\n"))
        self.assertEqual(self.calls(), [])

    def test_a_relative_home_is_refused_as_uninstall_refuses_it(self):
        for home in ("relhome", "./home", "~"):
            for name in ("install.sh", "uninstall.sh"):
                code, out, err = self.script(name, HOME=home)
                self.assertEqual((code, out), (1, ""), (name, home))
                self.assertEqual(err, f"{name}: HOME is not an absolute path: {home}\n")
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse((self.tmp / "relhome").exists())  # the scripts run in self.tmp

    def test_a_relative_xdg_data_home_is_ignored_by_both_scripts(self):
        """The XDG specification: a relative path is invalid and to be
        ignored. Taken as it is, the launcher went below whatever directory
        the script was run from, and uninstall.sh looked below another."""
        warning = (
            "warning: XDG_DATA_HOME is not an absolute path (rel/data); using "
            f"{self.home}/.local/share\n"
        )
        code, out, err = self.install(XDG_DATA_HOME="rel/data")
        self.assertEqual(code, 0)
        self.assertTrue(err.startswith(warning), err)
        self.assertIn(f"desktop launcher: {self.desktop}\n", out)
        self.assertTrue(self.desktop.is_file())
        self.assertFalse((self.tmp / "rel").exists())
        code, out, err = self.uninstall(XDG_DATA_HOME="rel/data")
        self.assertEqual((code, err), (0, warning))
        self.assertIn(f"removed {self.desktop}\n", out)
        self.assertFalse(self.desktop.exists())

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
        for text in (
            original + "X-Mine=1\n",
            original.replace("Agent Sessions", "Mine"),
            LAUNCHER_EDITED,
            LAUNCHER_0_2_0 + "\n",
            "",
        ):
            self.desktop.write_text(text, encoding="utf-8")
            code, out, _err = self.uninstall()
            self.assertEqual(code, 0)
            self.assertIn(
                f"left {self.desktop}: it is not a launcher ./install.sh installed\n", out
            )
            self.assertEqual(self.desktop.read_text(encoding="utf-8"), text)
        self.desktop.unlink()
        os.symlink(self.repo / "share" / "agent-sessions.desktop", self.desktop)
        code, out, _err = self.uninstall()
        self.assertIn(f"left {self.desktop}: it is a symbolic link", out)
        self.assertTrue(self.desktop.is_symlink())

    def test_the_launcher_of_an_earlier_version_is_this_script_s_too(self):
        """Installed by 0.2.0 and never run again since the desktop file
        changed: still what ./install.sh put there, so it is taken back."""
        self.desktop.write_text(LAUNCHER_0_2_0, encoding="utf-8")
        code, out, _err = self.uninstall()
        self.assertEqual(code, 0)
        self.assertIn(f"removed {self.desktop}\n", out)
        self.assertFalse(self.desktop.exists())

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

    # --- --purge with AGENT_SESSIONS_STATE and AGENT_SESSIONS_CACHE set (N-24) ---

    STATE_FILES = (
        "agent-sessions.log",
        "agent-sessions.log.1",
        "agent-sessions.log.3",
        "agent-sessions.lock",
        "imports.jsonl",
        "imports.json",
        "handoff/claude-aaaaaaaa-0000-4000-8000-000000000001.md",
        "handoff/codex-x.md.4242-18f3a.tmp",
    )
    CACHE_FILES = ("index.json", "index.json.4242.tmp", "index.json.unusable", "index.lock")

    def named(self, name, files, others=()):
        """A directory an environment variable names, with the program's
        ``files`` in it and ``others`` that are not the program's."""
        top = self.tmp / name
        for rel in (*files, *others):
            (top / rel).parent.mkdir(parents=True, exist_ok=True)
            (top / rel).write_text("x", encoding="utf-8")
        return top

    def below(self, top):
        return sorted(
            os.path.relpath(os.path.join(base, name), top)
            for base, dirs, files in os.walk(top)
            for name in dirs + files
        )

    def test_purge_takes_the_program_s_files_out_of_the_directories_the_environment_names(self):
        state = self.named("my state", self.STATE_FILES)
        cache = self.named("my cache", self.CACHE_FILES)
        code, out, err = self.uninstall(
            "--purge", "--yes", AGENT_SESSIONS_STATE=str(state), AGENT_SESSIONS_CACHE=f"{cache}/"
        )
        self.assertEqual((code, err), (0, ""))
        for top, files in ((state, self.STATE_FILES), (cache, self.CACHE_FILES)):
            for rel in files:
                self.assertIn(f"removed {top / rel}\n", out)
            self.assertIn(f"removed {top}\n", out)
            self.assertFalse(top.exists())
        self.assertIn(f"removed {state / 'handoff'}\n", out)
        self.assertIn("to be removed, the program's own files and nothing else:\n", out)
        for directory in self.dirs:  # and the three default ones, as before
            self.assertIn(f"removed {directory}\n", out)
            self.assertFalse(directory.exists())
        self.assert_kept()
        self.assertTrue(self.tmp.is_dir())

    def test_what_else_is_in_a_named_directory_stays_and_so_does_the_directory(self):
        others = ("notes.txt", "handoff/mine.md", "handoff/sub/claude-x.md", "agent-sessions.logs")
        state = self.named("my state", self.STATE_FILES, others)
        cache = self.named(
            "my cache", self.CACHE_FILES, ("index.json.bak", "other.json", "index.lock.1")
        )
        code, out, err = self.uninstall(
            "--purge", "--yes", AGENT_SESSIONS_STATE=str(state), AGENT_SESSIONS_CACHE=str(cache)
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            self.below(state),
            ["agent-sessions.logs", "handoff", "handoff/mine.md", "handoff/sub"]
            + ["handoff/sub/claude-x.md", "notes.txt"],
        )
        self.assertEqual(self.below(cache), ["index.json.bak", "index.lock.1", "other.json"])
        for top in (state / "handoff", state, cache):
            self.assertIn(f"left {top}: other files are in it\n", out)
            self.assertNotIn(f"removed {top}\n", out)

    def test_a_cache_named_by_its_index_file(self):
        """``AGENT_SESSIONS_CACHE`` that ends in ``.json`` is the index file
        itself: that file goes, and its directory only when it is empty."""
        ours = ("custom.json", "custom.json.7.tmp", "custom.json.unusable", "custom.lock")
        cache = self.named("idx", ours, ("index.json", "index.lock"))
        code, out, _err = self.uninstall(
            "--purge", "--yes", AGENT_SESSIONS_CACHE=str(cache / "custom.json")
        )
        self.assertEqual(code, 0)
        for rel in ours:
            self.assertIn(f"removed {cache / rel}\n", out)
        # Not the index here, nor its lock: not ours.
        self.assertEqual(self.below(cache), ["index.json", "index.lock"])
        self.assertIn(f"left {cache}: other files are in it\n", out)
        (cache / "index.json").unlink()
        (cache / "index.lock").unlink()
        (cache / "custom.json").write_text("x", encoding="utf-8")
        code, out, _err = self.uninstall(
            "--purge", "--yes", AGENT_SESSIONS_CACHE=str(cache / "custom.json")
        )
        self.assertIn(f"removed {cache}\n", out)
        self.assertFalse(cache.exists())

    def test_a_named_directory_that_is_someone_s_home_loses_our_files_only(self):
        (self.home / "agent-sessions.log").write_text("x", encoding="utf-8")
        (self.home / "index.json").write_text("x", encoding="utf-8")
        code, out, _err = self.uninstall(
            "--purge",
            "--yes",
            AGENT_SESSIONS_STATE=str(self.home),
            AGENT_SESSIONS_CACHE=str(self.home),
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "agent-sessions.log").exists())
        self.assertFalse((self.home / "index.json").exists())
        self.assertEqual(out.count(f"left {self.home}: other files are in it\n"), 2)
        self.assert_kept()

    def test_named_directories_that_are_not_looked_into(self):
        """A relative path, a symbolic link, a directory with nothing of the
        program's in it, one that does not exist: nothing is removed, and the
        first two are named with the reason."""
        real = self.named("real state", self.STATE_FILES)
        link = self.tmp / "state link"
        os.symlink(real, link)
        empty = self.tmp / "empty"
        empty.mkdir()
        foreign = self.named("foreign", (), ("notes.txt", "handoff/mine.md"))
        self.named("rel", self.STATE_FILES)  # below the directory the script runs in
        for directory in self.dirs:
            shutil.rmtree(directory)
        for value, note in (
            ("rel", "left rel: AGENT_SESSIONS_STATE is not an absolute path\n"),
            (str(link), f"left {link}: it is a symbolic link (AGENT_SESSIONS_STATE)\n"),
            (str(empty), ""),
            (str(foreign), ""),
            (str(self.tmp / "gone"), ""),
        ):
            code, out, err = self.uninstall("--purge", "--yes", AGENT_SESSIONS_STATE=value)
            self.assertEqual((code, err), (0, ""), value)
            self.assertIn("nothing to purge: no cache, state or configuration directory\n", out)
            self.assertEqual(note in out if note else "left " + value not in out, True, out)
            gone = [line for line in out.splitlines() if line.startswith("removed ")]
            self.assertEqual(
                [line for line in gone if line[8:] not in (str(self.link), str(self.desktop))], []
            )
        self.assertEqual(len(self.below(real)), len(self.STATE_FILES) + 1)
        self.assertEqual(len(self.below(self.tmp / "rel")), len(self.STATE_FILES) + 1)
        self.assertEqual(self.below(empty), [])
        self.assertEqual(self.below(foreign), ["handoff", "handoff/mine.md", "notes.txt"])

    def test_a_named_directory_is_left_alone_without_purge_and_asked_about_with_it(self):
        state = self.named("my state", self.STATE_FILES)
        self.assertEqual(self.uninstall(AGENT_SESSIONS_STATE=str(state))[0], 0)
        self.assertEqual(len(self.below(state)), len(self.STATE_FILES) + 1)
        self.assertEqual(self.install()[0], 0)
        master, slave = pty.openpty()
        try:
            os.write(master, b"n\n")
            code, out, err = self.uninstall("--purge", stdin=slave, AGENT_SESSIONS_STATE=str(state))
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual((code, err), (1, "Remove them? [y/N] "))
        for rel in self.STATE_FILES:
            self.assertIn(f"  {state / rel}\n", out)
        self.assertIn(f"and, when nothing else is left in them:\n  {state / 'handoff'}\n", out)
        self.assertEqual(len(self.below(state)), len(self.STATE_FILES) + 1)

    def test_a_default_directory_named_by_the_environment_goes_whole_once(self):
        code, out, err = self.uninstall(
            "--purge",
            "--yes",
            AGENT_SESSIONS_STATE=str(self.dirs[1]),
            AGENT_SESSIONS_CACHE=str(self.dirs[0] / "index.json"),
        )
        self.assertEqual((code, err), (0, ""))
        for directory in self.dirs:
            self.assertEqual(out.count(f"removed {directory}\n"), 1)
            self.assertFalse(directory.exists())
        self.assertNotIn("the program's own files", out)

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
                self.assertRegex(code, r'rm -r?f -- "\$(link|desktop|dir|file)"')
                # Only a directory that is the program's whole goes with what is in it.
                if "rm -rf" in code:
                    self.assertIn('rm -rf -- "$dir"', code)
        text = (REPO / "uninstall.sh").read_text(encoding="utf-8")
        self.assertEqual(text.count("rm -rf --"), 1)
        whole = re.search(r'for dir in ((?:"\$HOME/[^"]+"[ \\\n]*)+); do', text)
        self.assertIsNotNone(whole)
        self.assertEqual(
            re.findall(r'"\$HOME/([^"]+)"', whole.group(1)),
            [".cache/agent-sessions", ".local/state/agent-sessions", ".config/agent-sessions"],
        )

    def launchers_before(self, name) -> list[str]:
        text = (REPO / name).read_text(encoding="utf-8")
        block = re.search(r"(?m)^LAUNCHERS_BEFORE=\(\n((?:  .*\n)+)\)$", text)
        if block is None:
            self.fail(f"{name}: no LAUNCHERS_BEFORE list")
        hashes = re.findall(r"(?m)^  (\S+)", block.group(1))
        for value in hashes:
            self.assertRegex(value, r"^[0-9a-f]{64}$")
        return hashes

    def test_both_scripts_know_the_same_earlier_launchers(self):
        known = self.launchers_before("install.sh")
        self.assertEqual(self.launchers_before("uninstall.sh"), known)
        self.assertEqual(len(set(known)), len(known))
        current = hashlib.sha256(DESKTOP.read_bytes()).hexdigest()
        self.assertNotIn(current, known)  # the one in the checkout is compared byte for byte
        self.assertIn(hashlib.sha256(LAUNCHER_0_2_0.encode("utf-8")).hexdigest(), known)
        self.assertNotIn(hashlib.sha256(LAUNCHER_EDITED.encode("utf-8")).hexdigest(), known)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_every_launcher_the_history_holds_is_known(self):
        """A change of the desktop file without its old hash added to the
        lists would turn every installed launcher into "someone's own"."""

        def git(*argv):
            return subprocess.run(
                ["git", "-C", str(REPO), *argv],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )

        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0 or os.path.realpath(top.stdout.decode().strip()) != str(REPO):
            self.skipTest("not a git checkout: the earlier versions cannot be read")
        commits = git("log", "--format=%H", "--", "share/agent-sessions.desktop")
        self.assertEqual(commits.returncode, 0, commits.stderr)
        known = set(self.launchers_before("install.sh"))
        known.add(hashlib.sha256(DESKTOP.read_bytes()).hexdigest())
        seen = 0
        for commit in commits.stdout.decode().split():
            shown = git("show", f"{commit}:share/agent-sessions.desktop")
            if shown.returncode != 0:
                continue  # the commit that removed or moved it
            seen += 1
            self.assertIn(hashlib.sha256(shown.stdout).hexdigest(), known, commit)
        self.assertGreaterEqual(seen, 1)

    def test_the_headers_say_what_else_is_written(self):
        """A-22: "nothing outside those three places is written" was not
        true of pip and of update-desktop-database."""
        install = (REPO / "install.sh").read_text(encoding="utf-8")
        head = install.split("set -euo pipefail")[0]
        self.assertNotIn("nothing outside", head)
        for words in ("download cache (~/.cache/pip", "mimeinfo.cache", "--force"):
            self.assertIn(words, head)
        uninstall = (REPO / "uninstall.sh").read_text(encoding="utf-8").split("set -euo")[0]
        for words in ("AGENT_SESSIONS_STATE", "AGENT_SESSIONS_CACHE", "mimeinfo.cache", "pip"):
            self.assertIn(words, uninstall)

    def test_the_desktop_file_says_what_it_needs(self):
        """N-15: without ``TryExec`` a menu shows a launcher that cannot start."""
        lines = DESKTOP.read_text(encoding="utf-8").splitlines()
        self.assertIn("TryExec=omarchy-launch-tui", lines)
        (command,) = [line for line in lines if line.startswith("Exec=")]
        self.assertEqual(command.split("=", 1)[1].split()[0], "omarchy-launch-tui")
        self.assertLess(lines.index("TryExec=omarchy-launch-tui"), lines.index(command))

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
