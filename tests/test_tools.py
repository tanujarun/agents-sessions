"""launch.tool_state: is the tool behind a command installed, or is the
command only a script that would install it (docs/SPEC.md "Tool detection",
docs/FORMATS.md "What is on PATH").

Everything is read from a fake PATH directory and a fake mise tree in a temp
directory (``PATH`` and ``MISE_DATA_DIR`` are set here); the machine's own
commands and installs are never looked at, and no program is started."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_NATIVE, CODEX_NATIVE, World
from support import WRAPPER, FakePath, Sandbox

from agent_sessions import api, cli, launch
from agent_sessions.launch import INSTALLED, NOT_FOUND, NOT_INSTALLED, ToolState, tool_state

POSIX = os.name == "posix"
# The four wrappers of ~/.local/bin, byte for byte (FORMATS "What is on PATH").
REAL_WRAPPERS = {
    "claude": (
        "claude",
        '#!/bin/bash\nexport MISE_MINIMUM_RELEASE_AGE=0\nmise use -g --quiet "claude" || exit 1\n'
        'exec mise x "claude" -- "claude" "$@"\n',
    ),
    "grok": (
        "npm:@xai-official/grok",
        "#!/bin/bash\nexport MISE_MINIMUM_RELEASE_AGE=0\n"
        'mise use -g --quiet "npm:@xai-official/grok" || exit 1\n'
        'exec mise x "npm:@xai-official/grok" -- "grok" "$@"\n',
    ),
}


class ToolCase(unittest.TestCase):
    """A real ``PATH`` with one directory, and a mise data directory."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="agent-sessions-test-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.installs = self.tmp / "mise" / "installs"
        env = mock.patch.dict(
            os.environ, {"PATH": str(self.bin), "MISE_DATA_DIR": str(self.tmp / "mise")}
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_DATA_HOME", None)
        launch.forget_tools()
        self.addCleanup(launch.forget_tools)
        # Nothing here may start a program.
        for name in ("Popen", "run", "call", "check_output"):
            patch = mock.patch.object(subprocess, name, side_effect=AssertionError("a process"))
            patch.start()
            self.addCleanup(patch.stop)

    def command(self, name, content, mode=0o755) -> Path:
        path = self.bin / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        launch.forget_tools()
        return path

    def wrapper(self, tool, spec=None) -> Path:
        return self.command(tool, WRAPPER.format(spec=spec or tool, tool=tool))

    def install(self, directory, *parts, mode=0o755) -> Path:
        """An executable in the fake mise tree: ``installs/<directory>/<parts…>``."""
        path = self.installs.joinpath(directory, *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF the tool\n")
        path.chmod(mode)
        launch.forget_tools()
        return path

    def state(self, name) -> tuple[bool, bool, str]:
        found = tool_state(name)
        return found.installed, found.wrapper, found.detail


class StateTest(ToolCase):
    def test_nothing_on_path(self):
        self.assertEqual(tool_state("claude"), ToolState(None, False, False, NOT_FOUND))
        self.assertEqual(tool_state(""), ToolState(None, False, False, NOT_FOUND))
        self.assertEqual((INSTALLED, NOT_FOUND), ("installed", "not found"))
        self.assertEqual(NOT_INSTALLED, "not installed (the command on PATH would install it)")

    def test_a_real_program_is_installed(self):
        path = self.command("agy", b"\x7fELF\x02\x01\x01" + b"\0" * 64)
        self.assertEqual(tool_state("agy"), ToolState(str(path), True, False, INSTALLED))

    def test_the_wrappers_of_this_machine(self):
        """The real text, with what mise has behind it: a program right in
        the version directory (claude), and one three directories down
        (grok's npm package)."""
        for tool, (_spec, text) in REAL_WRAPPERS.items():
            path = self.command(tool, text)
            self.assertEqual(tool_state(tool), ToolState(str(path), False, True, NOT_INSTALLED))
        self.install("claude", "2.1.296", "claude")
        self.install("npm-xai-official-grok", "1.0.50", "node_modules", ".bin", "grok")
        for tool in REAL_WRAPPERS:
            self.assertEqual(self.state(tool), (True, True, INSTALLED))

    def test_where_the_executable_may_lie(self):
        self.wrapper("codex")
        for parts, expected in (
            (("0.162.0", "codex"), True),
            (("0.162.0", "bin", "codex"), True),
            (("0.162.0", "a", "b", "codex"), True),
            (("0.162.0", "a", "b", "c", "codex"), False),  # too deep
            (("codex",), False),  # not inside a version directory
            (("0.162.0", "bin", "codex-code-mode-host"), False),  # another name
        ):
            with self.subTest(parts=parts):
                path = self.install("codex", *parts)
                self.assertEqual(self.state("codex")[0], expected)
                os.unlink(path)
                launch.forget_tools()
                self.assertFalse(self.state("codex")[0])

    def test_what_counts_as_an_executable(self):
        self.wrapper("codex")
        version = self.installs / "codex" / "1.0"
        plain = self.install("codex", "1.0", "codex", mode=0o644)
        if POSIX:
            self.assertFalse(self.state("codex")[0])  # a file that cannot be run
        os.unlink(plain)
        (version / "codex").mkdir()  # a directory of that name
        launch.forget_tools()
        self.assertFalse(self.state("codex")[0])
        os.rmdir(version / "codex")
        real = self.install("codex", "1.0", "libexec", "codex-real")
        os.symlink(real, version / "codex")  # a link to an executable
        launch.forget_tools()
        self.assertTrue(self.state("codex")[0])
        os.unlink(real)  # ... which is gone
        launch.forget_tools()
        self.assertFalse(self.state("codex")[0])

    def test_an_install_that_did_not_finish(self):
        """What the gemini wrapper left here: a version directory with a
        ``package.json`` and nothing to run."""
        self.wrapper("gemini")
        version = self.installs / "gemini" / "0.63.0"
        (version / ".config" / "aube").mkdir(parents=True)
        (version / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.state("gemini"), (False, True, NOT_INSTALLED))

    def test_any_version_will_do_and_links_to_versions_are_followed_once(self):
        self.wrapper("claude")
        root = self.installs / "claude"
        (root / "2.1.281").mkdir(parents=True)
        self.install("claude", "2.1.296", "claude")
        os.symlink("./2.1.296", root / "latest")
        os.symlink("./2.1.296", root / "2")
        (root / ".mise.backend.toml").write_text('short = "claude"\n', encoding="utf-8")
        with mock.patch.object(os.path, "isfile", wraps=os.path.isfile) as looked:
            self.assertTrue(self.state("claude")[0])
        looked_in = {os.path.dirname(call.args[0]) for call in looked.call_args_list}
        self.assertLessEqual(len(looked_in), 2)  # one real directory per version, no link twice

    def test_a_tree_without_end_is_not_walked_without_end(self):
        self.wrapper("codex")
        wide = self.installs / "codex" / "1.0"
        for number in range(30):
            (wide / f"d{number:02}").mkdir(parents=True)
        with mock.patch.object(launch, "SEARCH_ENTRIES", 10):
            self.assertFalse(self.state("codex")[0])
        self.install("codex", "1.0", "d29", "codex")
        self.assertTrue(self.state("codex")[0])
        with mock.patch.object(launch, "SEARCH_ENTRIES", 10):
            launch.forget_tools()
            self.assertFalse(self.state("codex")[0])  # given up before it was reached


class WrapperTextTest(ToolCase):
    """Recognised by its last command: ``exec mise x <spec> -- <tool> "$@"``."""

    def is_wrapper(self, text) -> bool:
        self.command("tool", text)
        return tool_state("tool").wrapper

    def test_quoting_and_mise_exec_are_tolerated(self):
        head = "#!/bin/bash\nmise use -g --quiet tool || exit 1\n"
        for last in (
            'exec mise x "tool" -- "tool" "$@"',
            'exec mise x tool -- tool "$@"',
            "exec mise x 'tool' -- 'tool' \"$@\"",
            'exec mise exec "tool" -- "tool" "$@"',
            'exec   mise\tx   "tool"  --  "tool"   "$@"',
            'exec /usr/bin/mise x "tool" -- "tool" "$@"',
            '  exec mise x "tool" -- "tool" "$@"  ',
        ):
            with self.subTest(last=last):
                self.assertTrue(self.is_wrapper(head + last + "\n"))
                self.assertEqual(self.state("tool"), (False, True, NOT_INSTALLED))
        self.assertTrue(
            self.is_wrapper(head + 'exec mise x "tool" -- "tool" "$@"')
        )  # no line break
        self.assertTrue(
            self.is_wrapper(
                "#!/bin/sh\n\n# a comment\n"
                + head.split("\n", 1)[1]
                + '\n# last\nexec mise x "tool" -- "tool" "$@"\n\n'
            )
        )
        self.assertTrue(self.is_wrapper('#!/bin/bash\r\nexec mise x "tool" -- "tool" "$@"\r\n'))

    def test_the_spec_and_the_tool_are_taken_from_the_exec_line(self):
        self.command("grok", REAL_WRAPPERS["grok"][1])
        self.assertEqual(
            launch._wrapper(str(self.bin / "grok")), ("npm:@xai-official/grok", "grok")
        )
        # The executable is looked for under the tool's name, not the command's.
        self.command("g", WRAPPER.format(spec="npm:@xai-official/grok", tool="grok"))
        self.install("npm-xai-official-grok", "1.0.50", "node_modules", ".bin", "grok")
        self.assertEqual(self.state("g"), (True, True, INSTALLED))

    def test_what_is_not_such_a_wrapper_counts_as_installed(self):
        for name, text in (
            ("another script", '#!/bin/sh\nexec "/opt/venv/bin/python" -m prompt_maxxer "$@"\n'),
            (
                "detached app",
                '#!/usr/bin/env bash\nsetsid -f "$HOME/Applications/X.AppImage" "$@"\n',
            ),
            ("mise run", '#!/bin/bash\nexec mise run tool "$@"\n'),
            ("no exec", '#!/bin/bash\nmise x "tool" -- "tool" "$@"\n'),
            ("no separator", '#!/bin/bash\nexec mise x "tool" "tool" "$@"\n'),
            ("no arguments passed on", '#!/bin/bash\nexec mise x "tool" -- "tool"\n'),
            ("other arguments", '#!/bin/bash\nexec mise x "tool" -- "tool" --yolo "$@"\n'),
            ("an option for a spec", '#!/bin/bash\nexec mise x -q -- "tool" "$@"\n'),
            ("a path for a tool", '#!/bin/bash\nexec mise x "tool" -- "/bin/tool" "$@"\n'),
            ("not the last command", '#!/bin/bash\nexec mise x "tool" -- "tool" "$@"\necho done\n'),
            ("quotes that do not close", '#!/bin/bash\nexec mise x "tool -- "tool" "$@"\n'),
            ("no shebang", 'exec mise x "tool" -- "tool" "$@"\n'),
            ("only a shebang", "#!/bin/bash\n"),
            ("empty", ""),
            (
                "a long program",
                "#!/bin/bash\n" + "true\n" * 9 + 'exec mise x "tool" -- "tool" "$@"\n',
            ),
            (
                "a large file",
                "#!/bin/bash\n# " + "x" * 5000 + '\nexec mise x "tool" -- "tool" "$@"\n',
            ),
        ):
            with self.subTest(name=name):
                self.assertFalse(self.is_wrapper(text))
                self.assertEqual(self.state("tool"), (True, False, INSTALLED))
        for name, data in (
            ("a program", b"\x7fELF" + b"\0" * 200),
            ("a NUL in a script", b'#!/bin/bash\n\0\nexec mise x "tool" -- "tool" "$@"\n'),
            ("not UTF-8", b'#!/bin/bash\n# \xff\xfe\nexec mise x "tool" -- "tool" "$@"\n'),
        ):
            with self.subTest(name=name):
                self.command("tool", data)
                self.assertEqual(self.state("tool"), (True, False, INSTALLED))

    @unittest.skipUnless(POSIX and os.geteuid() != 0, "needs a file that cannot be read")
    def test_a_command_that_cannot_be_read_counts_as_installed(self):
        self.command("tool", WRAPPER.format(spec="tool", tool="tool"), mode=0o111)
        self.assertEqual(self.state("tool"), (True, False, INSTALLED))

    def test_the_wrapper_is_read_and_never_run(self):
        marker = self.tmp / "ran"
        self.command("tool", f'#!/bin/sh\ntouch "{marker}"\nexec mise x "tool" -- "tool" "$@"\n')
        self.assertEqual(self.state("tool"), (False, True, NOT_INSTALLED))
        self.assertFalse(marker.exists())
        self.assertEqual(stat.S_IMODE((self.bin / "tool").stat().st_mode), 0o755)


class InstallDirectoryTest(ToolCase):
    def test_the_name_mise_gives_a_spec(self):
        """Read off ``~/.local/share/mise/installs`` on 2026-10-12."""
        for spec, name in (
            ("claude", "claude"),
            ("codex", "codex"),
            ("opencode", "opencode"),
            ("npm:playwright", "npm-playwright"),
            ("npm:@xai-official/grok", "npm-xai-official-grok"),
        ):
            self.assertEqual(launch._install_name(spec), name)
        # A version is not part of the name; the scope of an npm package is.
        self.assertEqual(launch._install_name("claude@2.1"), "claude")
        self.assertEqual(
            launch._install_name("npm:@xai-official/grok@1.0.50"), "npm-xai-official-grok"
        )
        self.assertEqual(launch._install_name("npm:@scope/Tool_Name"), "npm-scope-tool-name")

    def test_the_data_directory(self):
        home = os.path.expanduser("~")
        cases = (
            ({"MISE_DATA_DIR": "/data/mise"}, "/data/mise/installs"),
            ({"MISE_DATA_DIR": "/data/mise", "XDG_DATA_HOME": "/xdg"}, "/data/mise/installs"),
            ({"XDG_DATA_HOME": "/xdg"}, "/xdg/mise/installs"),
            ({}, os.path.join(home, ".local", "share", "mise", "installs")),
            ({"MISE_DATA_DIR": "relative", "XDG_DATA_HOME": "/xdg"}, "/xdg/mise/installs"),
            (
                {"MISE_DATA_DIR": "", "XDG_DATA_HOME": "also/relative"},
                os.path.join(home, ".local", "share", "mise", "installs"),
            ),
        )
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                for name in ("MISE_DATA_DIR", "XDG_DATA_HOME"):
                    if name not in env:
                        os.environ.pop(name, None)
                self.assertEqual(launch._mise_installs(), expected)

    def test_xdg_data_home_is_honoured(self):
        self.wrapper("claude")
        xdg = self.tmp / "xdg"
        tool = xdg / "mise" / "installs" / "claude" / "1" / "claude"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"\x7fELF")
        tool.chmod(0o755)
        self.assertFalse(self.state("claude")[0])  # not in $MISE_DATA_DIR
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}):
            os.environ.pop("MISE_DATA_DIR")
            self.assertTrue(self.state("claude")[0])  # another tree: asked again


class CacheTest(ToolCase):
    def test_a_file_is_read_once_per_run(self):
        self.wrapper("claude")
        self.install("claude", "1", "claude")
        with (
            mock.patch.object(launch, "open_ro", wraps=launch.open_ro) as opened,
            mock.patch.object(os, "listdir", wraps=os.listdir) as listed,
        ):
            first = tool_state("claude")
            for _ in range(50):
                self.assertIs(tool_state("claude"), first)
            self.assertEqual((opened.call_count, listed.call_count), (1, 1))
            launch.forget_tools()
            self.assertEqual(tool_state("claude"), first)
            self.assertEqual(opened.call_count, 2)

    def test_what_changed_is_seen_after_forget_tools(self):
        self.wrapper("claude")
        self.assertFalse(tool_state("claude").installed)
        path = self.installs / "claude" / "1" / "claude"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x7fELF")
        path.chmod(0o755)
        self.assertFalse(tool_state("claude").installed)  # remembered
        launch.forget_tools()
        self.assertTrue(tool_state("claude").installed)

    def test_a_command_that_appears_or_goes_is_seen_at_once(self):
        self.assertEqual(tool_state("agy").detail, NOT_FOUND)
        path = self.bin / "agy"
        path.write_bytes(b"\x7fELF")
        path.chmod(0o755)
        self.assertEqual(tool_state("agy").detail, INSTALLED)
        os.unlink(path)
        self.assertEqual(tool_state("agy").detail, NOT_FOUND)


class ScanReadsOnceTest(unittest.TestCase):
    def test_a_listing_of_many_sessions_reads_the_wrapper_once(self):
        box = Sandbox(self)
        path = FakePath(self, box.root / "bin")
        path.wrapper("fake-agent")
        store = box.store(box.home)
        for number in range(40):
            box.session(store, f"s{number}")
        wrapper = str(path.directory / "fake-agent")
        with mock.patch.object(launch, "open_ro", wraps=launch.open_ro) as opened:

            def reads() -> int:
                return [call.args[0] for call in opened.call_args_list].count(wrapper)

            api.scan()
            self.assertEqual(reads(), 1)
            for _ in range(5):
                sessions = api.list_sessions()
                api.status()
            self.assertEqual(reads(), 1)
            # Nor is any other command read more than once (status asks for a few).
            self.assertEqual(
                opened.call_count, len({call.args[0] for call in opened.call_args_list})
            )
            self.assertEqual(len(sessions), 40)
            self.assertFalse(any(s.launchable for s in sessions))
            api.scan()  # a scan looks again
            self.assertEqual(reads(), 2)

    def test_a_tool_installed_while_the_program_runs_is_seen_by_the_next_scan(self):
        box = Sandbox(self)
        path = FakePath(self, box.root / "bin")
        path.wrapper("fake-agent")
        box.session(box.store(box.home), "a")
        api.scan()
        self.assertEqual(
            [(s.launchable, s.note) for s in api.list_sessions()],
            [(False, "Fake CLI is not installed")],
        )
        tool = Path(os.environ["MISE_DATA_DIR"]) / "installs" / "fake-agent" / "1.0" / "fake-agent"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"\x7fELF")
        tool.chmod(0o755)
        self.assertFalse(api.list_sessions()[0].launchable)  # not looked at again yet
        api.scan()
        self.assertEqual([(s.launchable, s.note) for s in api.list_sessions()], [(True, None)])


class WiringTest(unittest.TestCase):
    """The answer where it is used: the index, ``status``, ``launch.plan``."""

    def setUp(self):
        self.w = World(self, foreign=(), native=("claude", "codex"))
        self.mise = Path(os.environ["MISE_DATA_DIR"])
        self.w.path.wrapper("codex")  # only the script that would install it
        self.w.path.wrapper("claude")
        tool = self.mise / "installs" / "claude" / "2.1.296" / "claude"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"\x7fELF")
        tool.chmod(0o755)
        self.index = self.w.scan()

    def test_launchable_and_the_note(self):
        claude = self.w.session(CLAUDE_NATIVE, "native", self.index)
        codex = self.w.session(CODEX_NATIVE, "native", self.index)
        self.assertEqual((claude.launchable, claude.note), (True, None))
        self.assertEqual((codex.launchable, codex.note), (False, "Codex CLI is not installed"))

    def test_a_wrapper_whose_tool_is_missing_is_never_started(self):
        codex = self.w.session(CODEX_NATIVE, "native", self.index)
        reason = "codex is not installed (the command on PATH would install it)"
        for options in (dict(dry_run=True), dict(), dict(here=True, dry_run=True)):
            with self.assertRaises(api.LaunchFailed) as caught:
                api.open_session(codex, **options)
            self.assertEqual(caught.exception.reason, reason)
        with (
            mock.patch("os.execvp") as execvp,
            mock.patch("os.chdir"),
            self.assertRaises(api.LaunchFailed),
        ):
            api.open_session(codex, here=True)
        execvp.assert_not_called()
        self.assertEqual(self.w.started, [])
        self.assertEqual(self.w.notified, [reason, reason])
        # The wrapper whose tool is there is started as before, through the wrapper.
        plan = api.open_session(CLAUDE_NATIVE)
        self.assertEqual(plan.argv, ["claude", "--resume", CLAUDE_NATIVE])
        self.assertEqual(self.w.started, [(plan.command, plan.cwd)])

    def test_status(self):
        self.w.path.wrapper("crush")
        self.w.path.wrapper("opencode")
        tool = self.mise / "installs" / "opencode" / "1.18.35" / "opencode"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(b"\x7fELF")
        tool.chmod(0o755)
        status = api.status()
        bin_dir = self.w.path.directory
        self.assertEqual(
            {p["name"]: (p["installed"], p["tool"], p["resumable"]) for p in status["providers"]},
            {
                "antigravity": (str(bin_dir / "agy"), INSTALLED, True),
                "claude": (str(bin_dir / "claude"), INSTALLED, True),
                "codex": (None, NOT_INSTALLED, True),
                "gemini": (None, NOT_FOUND, False),
                "grok": (str(bin_dir / "grok"), INSTALLED, True),
            },
        )
        self.assertEqual(
            {t["tool"]: (t["detail"], t["wrapper"], t["reader"]) for t in status["tools"]},
            {
                "agy": (INSTALLED, False, True),
                "claude": (INSTALLED, True, True),
                "codex": (NOT_INSTALLED, True, True),
                "copilot": (NOT_FOUND, False, False),
                "crush": (NOT_INSTALLED, True, False),
                "cursor-agent": (NOT_FOUND, False, False),
                "gemini": (NOT_FOUND, False, True),
                "grok": (INSTALLED, False, True),
                "opencode": (INSTALLED, True, False),
            },
        )
        self.assertEqual(
            [t["tool"] for t in status["tools"]], sorted(t["tool"] for t in status["tools"])
        )
        # Only what is really installed is an "installed tool with no reader".
        self.assertEqual(
            status["no_reader"], [{"tool": "opencode", "path": str(bin_dir / "opencode")}]
        )

    def test_status_as_text(self):
        self.w.path.wrapper("crush")
        out = []
        with mock.patch.object(cli, "_out", lambda text, multiline=False: out.append(str(text))):
            self.assertEqual(cli.main(["status"]), 0)
        text = "\n".join(out)
        bin_dir = self.w.path.directory
        start = out.index("tools")
        rows = [" ".join(row.split()) for row in out[start + 1 : out.index("", start)]]
        self.assertEqual(
            rows,
            [
                f"agy installed {bin_dir / 'agy'}",
                f"claude installed {bin_dir / 'claude'} (wrapper)",
                f"codex {NOT_INSTALLED} {bin_dir / 'codex'} (wrapper)",
                "copilot not found",
                f"crush {NOT_INSTALLED} {bin_dir / 'crush'} (wrapper)",
                "cursor-agent not found",
                "gemini not found",
                f"grok installed {bin_dir / 'grok'}",
                "opencode not found",
            ],
        )
        self.assertNotIn("installed tools with no reader", text)
        self.assertRegex(text, rf"codex\s+1 store\s+\d+ sessions?\s+codex {NOT_INSTALLED[:13]}")


if __name__ == "__main__":
    unittest.main()
