"""Smoke test: the package imports and the CLI skeleton behaves."""

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import agent_sessions  # noqa: E402
from agent_sessions import cli, model, providers  # noqa: E402


def run(argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class SmokeTest(unittest.TestCase):
    def test_imports(self):
        import importlib

        for name in (
            "mounts",
            "discover",
            "pathmap",
            "index",
            "importer",
            "launch",
            "api",
            "fsutil",
            "providers.base",
        ):
            importlib.import_module(f"agent_sessions.{name}")
        self.assertEqual(agent_sessions.__version__, "0.1.0")

    def test_registry_holds_the_five_adapters(self):
        self.assertEqual(
            [p.name for p in providers.all_providers()],
            ["antigravity", "claude", "codex", "gemini", "grok"],
        )
        self.assertEqual(providers.get("claude").label, "Claude")
        with self.assertRaises(KeyError):
            providers.get("nope")

    def test_engine_does_not_import_textual(self):
        """Every module of the package outside ``tui/``, imported in a fresh
        interpreter: neither Textual nor the ``tui`` package comes with it.
        A fresh one because ``sys.modules`` of this process says nothing
        once the TUI's own tests have run in it (docs/REVIEW.md R-12)."""
        package = Path(_SRC) / "agent_sessions"
        names = sorted(
            ".".join(path.relative_to(package.parent).with_suffix("").parts)
            for path in package.rglob("*.py")
            if "tui" not in path.relative_to(package).parts and path.name != "__init__.py"
        )
        self.assertIn("agent_sessions.api", names)
        self.assertIn("agent_sessions.providers.claude", names)
        self.assertGreater(len(names), 15)
        code = (
            "import importlib, sys\n"
            f"sys.path.insert(0, {_SRC!r})\n"
            f"for name in {names!r}:\n"
            "    importlib.import_module(name)\n"
            "unwanted = ('textual.', 'agent_sessions.tui')\n"
            "loaded = [m for m in sys.modules if m == 'textual' or m.startswith(unwanted)]\n"
            "print(sorted(loaded))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "[]\n", ""))

    def test_help_exits_zero(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        for cmd in ("tui", "scan", "list", "open", "import", "show", "status", "verify"):
            self.assertIn(cmd, out)
        self.assertNotIn("menu", out)
        self.assertEqual(run(["menu", "claude"])[0], 2)

    def test_version(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("0.1.0", out)

    def test_stub_returns_one(self):
        self.assertEqual(run(["tui"]), (1, "", "agent-sessions: tui: not implemented\n"))

    def test_unknown_key_returns_one(self):
        """With an empty index (a temp home, no mounts) no key matches."""
        import os
        import tempfile
        from unittest import mock

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {
                    "AGENT_SESSIONS_HOME": os.path.join(tmp, "home"),
                    "AGENT_SESSIONS_CACHE": os.path.join(tmp, "cache"),
                    "AGENT_SESSIONS_STATE": os.path.join(tmp, "state"),
                    "AGENT_SESSIONS_MOUNTS": "[]",
                    "AGENT_SESSIONS_NOTIFY": "0",
                },
            ),
        ):
            code, out, err = run(["show", "k"])
            self.assertEqual((code, out), (1, ""))
            self.assertIn("no session matches 'k'", err)
            self.assertEqual(run(["open", "k", "--dry-run"])[0], 1)
            self.assertEqual(run(["open", "k"])[0], 1)
            self.assertEqual(run(["import", "k", "--open"])[0], 1)
            self.assertEqual(os.listdir(tmp), [])  # nothing was written

    def test_no_subcommand_returns_two(self):
        code, out, _ = run([])
        self.assertEqual(code, 2)
        self.assertIn("usage:", out)

    def test_make_key(self):
        key = model.make_key("claude", "/home/u/.claude", "abc-123")
        self.assertEqual(key, model.make_key("claude", "/home/u/.claude", "abc-123"))
        self.assertRegex(key, r"^claude:[0-9a-f]{8}:abc-123$")
        self.assertEqual(key, model.make_key("claude", Path("/home/u/.claude"), "abc-123"))
        self.assertNotEqual(key, model.make_key("claude", "/other/.claude", "abc-123"))


if __name__ == "__main__":
    unittest.main()
