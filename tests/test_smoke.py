"""Smoke test: the package imports and the CLI skeleton behaves."""

import contextlib
import io
import re
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

        for name in ("mounts", "discover", "pathmap", "index", "importer",
                     "launch", "providers.base"):
            importlib.import_module(f"agent_sessions.{name}")
        self.assertEqual(agent_sessions.__version__, "0.1.0")

    def test_registry_starts_empty(self):
        self.assertEqual(providers.all_providers(), [])
        with self.assertRaises(KeyError):
            providers.get("claude")

    def test_help_exits_zero(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        for cmd in ("scan", "list", "menu", "open", "import", "show",
                    "status", "verify"):
            self.assertIn(cmd, out)

    def test_version(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("0.1.0", out)

    def test_stub_returns_one(self):
        code, _, err = run(["scan"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "agent-sessions: scan: not implemented\n")
        self.assertEqual(run(["open", "k", "--dry-run"])[0], 1)

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
