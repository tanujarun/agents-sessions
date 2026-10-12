"""api.import_options: what an import of one session can be, told to a front
end before it asks the user anything (docs/SPEC.md "Import"), and
``can_import``, the short form of the same answer. The machine is the one of
tests/fixture_world.py."""

import collections
import contextlib
import dataclasses
import io
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import (
    AGY_NO_CWD,
    AGY_WIN,
    CLAUDE_NATIVE,
    CLAUDE_WIN,
    CLAUDE_WIN_PLAIN,
    CODEX_WIN,
    GEMINI_WIN,
    GROK_WIN,
    World,
)
from support import LOG_NAME

from agent_sessions import api, cli, importer
from agent_sessions.api import ImportOptions
from agent_sessions.providers import antigravity as agy_mod
from agent_sessions.providers import gemini as gemini_mod
from agent_sessions.providers.base import ExperimentalOnly, NotSupported
from agent_sessions.providers.claude import Claude

RECORDED = "F:\\Proj\\My Site"
NOT_FOUND = f"the project folder {RECORDED} was not found on any mounted drive"
NOT_RECORDED = "the project folder of this session is not recorded"
AS_IT_IS = ImportOptions(True, "", False, False, "")
EXPERIMENTAL = ImportOptions(False, agy_mod.EXPERIMENTAL_ONLY, False, True, agy_mod.IMPORT_WARNING)


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root) -> dict:
    """Every file and directory below ``root``: a regular file with its
    bytes and mode, anything else with its kind. The log is left out (every
    scan adds to it)."""
    found: dict[str, object] = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            if name.startswith(LOG_NAME):
                continue
            path = os.path.join(base, name)
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode):
                with open(path, "rb") as fh:
                    found[path] = (stat.S_IMODE(mode), fh.read())
            else:
                found[path] = mode
    return found


class OptionsCase(unittest.TestCase):
    foreign: tuple[str, ...] = ("antigravity", "claude", "codex", "gemini", "grok")
    native: tuple[str, ...] = ()

    def setUp(self):
        self.w = World(self, self.foreign, self.native)
        self.home = self.w.home
        self.index = self.w.scan()
        self.before = snapshot(self.w.box.root)
        self.addCleanup(self.assert_nothing_was_written)

    def assert_nothing_was_written(self):
        """Asking writes nothing, anywhere: not in a store, not in the state
        directory, not on the drives."""
        if getattr(self, "writes", False):
            return
        self.assertEqual(snapshot(self.w.box.root), self.before)

    def source(self, session_id):
        return self.w.session(session_id, "foreign")

    def options(self, ref, **kwargs) -> ImportOptions:
        """``import_options``, and ``can_import`` held to the same answer."""
        options = api.import_options(ref, **kwargs)
        self.assertIsInstance(options, ImportOptions)
        self.assertEqual(api.can_import(ref, **kwargs), (options.possible, options.reason))
        self.assertEqual(options.possible, not options.reason)
        self.assertEqual(bool(options.warning), options.experimental)
        if options.possible:
            self.assertFalse(options.needs_folder or options.experimental)
        return options


class AnswersTest(OptionsCase):
    def test_importable_as_it_is(self):
        for session_id in (CLAUDE_WIN, CLAUDE_WIN_PLAIN, CODEX_WIN, GROK_WIN):
            source = self.source(session_id)
            for ref in (source, source.key):
                self.assertEqual(self.options(ref), AS_IT_IS)
            self.assertEqual(self.options(source, index=self.index), AS_IT_IS)
            # A flag that is not needed changes nothing.
            self.assertEqual(api.can_import(source, experimental=True), (True, ""))

    def test_the_folder_is_gone(self):
        shutil.rmtree(self.w.site)
        self.w.scan()
        self.before = snapshot(self.w.box.root)
        for session_id in (CLAUDE_WIN, CODEX_WIN, GROK_WIN):
            source = self.source(session_id)
            self.assertEqual(
                self.options(source), ImportOptions(False, NOT_FOUND, needs_folder=True)
            )
            # With a folder it is possible, and a folder alone is what it takes.
            self.assertEqual(self.options(source, target=str(self.home)), AS_IT_IS)
            self.assertEqual(api.can_import(source, experimental=True), (False, NOT_FOUND))

    def test_the_folder_is_not_recorded(self):
        source = dataclasses.replace(self.source(CLAUDE_WIN), cwd=None)
        self.assertEqual(
            self.options(source), ImportOptions(False, NOT_RECORDED, needs_folder=True)
        )
        self.assertEqual(self.options(source, target=str(self.home)), AS_IT_IS)

    def test_antigravity_works_only_as_an_experimental_import(self):
        for session_id in (AGY_WIN, AGY_NO_CWD):  # with a folder and without one
            source = self.source(session_id)
            self.assertEqual(self.options(source), EXPERIMENTAL)
            self.assertEqual(self.options(source.key, target=str(self.home)), EXPERIMENTAL)
            self.assertEqual(api.can_import(source, experimental=True), (True, ""))
        self.assertIn("has been resumed by hand", EXPERIMENTAL.warning)
        self.assertIn("starts a new conversation without an error", EXPERIMENTAL.warning)
        self.assertIn("--experimental", EXPERIMENTAL.reason)

    def test_an_antigravity_conversation_that_the_flag_would_not_help(self):
        source = self.source(AGY_WIN)
        wal = Path(source.files[0] + "-wal")
        wal.write_bytes(b"frames")
        self.before = snapshot(self.w.box.root)
        options = self.options(source)
        self.assertIn("write-ahead log that is not empty", options.reason)
        self.assertEqual(
            (options.possible, options.needs_folder, options.experimental, options.warning),
            (False, False, False, ""),
        )
        self.assertEqual(api.can_import(source, experimental=True), (False, options.reason))

    def test_gemini_is_refused(self):
        source = self.source(GEMINI_WIN)
        self.assertEqual(self.options(source), ImportOptions(False, gemini_mod.IMPORT_UNSAFE))
        self.assertEqual(
            self.options(source, target=str(self.home)),
            ImportOptions(False, gemini_mod.IMPORT_UNSAFE),
        )
        self.assertEqual(
            api.can_import(source, experimental=True), (False, gemini_mod.IMPORT_UNSAFE)
        )

    def test_a_reference_that_names_nothing(self):
        options = self.options("claude:nope")
        self.assertEqual(
            options,
            ImportOptions(False, "no session matches 'claude:nope'; `list --keys` shows the keys"),
        )
        self.assertEqual(self.options("").reason, "no session key given")
        self.assertIn("matches", self.options("claude:").reason)

    def test_an_id_that_is_not_in_the_tools_form(self):
        source = self.source(CLAUDE_WIN)
        session = dataclasses.replace(source, id="--yolo")
        bad = dataclasses.replace(api._plan(source, self.index), session_id="--yolo")
        with mock.patch.object(Claude, "import_plan", return_value=bad):
            options = self.options(session, index=self.index)
        self.assertEqual(
            options,
            ImportOptions(
                False,
                "the id of this session is not in the form its tool gives ids; "
                "nothing was imported",
            ),
        )
        # The real adapter does not plan it in the first place.
        self.assertFalse(self.options(session, index=self.index).possible)

    def test_no_free_space(self):
        usage = collections.namedtuple("usage", "total used free")
        with mock.patch.object(importer.shutil, "disk_usage", return_value=usage(10**9, 10**9, 5)):
            for session_id in (CLAUDE_WIN, CODEX_WIN, GROK_WIN):
                options = self.options(self.source(session_id))
                self.assertIn("not enough free space", options.reason)
                self.assertEqual(
                    (options.possible, options.needs_folder, options.experimental),
                    (False, False, False),
                )
            # Not enough for the experimental copy either: the flag is not offered.
            options = self.options(self.source(AGY_WIN))
            self.assertIn("not enough free space", options.reason)
            self.assertEqual((options.experimental, options.warning), (False, ""))

    def test_a_folder_would_not_help_when_something_else_is_in_the_way(self):
        shutil.rmtree(self.w.site)
        self.w.scan()
        usage = collections.namedtuple("usage", "total used free")
        with mock.patch.object(importer.shutil, "disk_usage", return_value=usage(10**9, 10**9, 5)):
            self.before = snapshot(self.w.box.root)
            options = self.options(self.source(CLAUDE_WIN))
        self.assertIn("not enough free space", options.reason)
        self.assertFalse(options.needs_folder)
        # An id that is taken in the native store.
        taken = self.home / ".codex" / "sessions" / "2025" / "01" / "02"
        taken.mkdir(parents=True)
        (taken / f"rollout-2025-01-02T03-04-05-{CODEX_WIN}.jsonl").write_bytes(b"{}\n")
        self.before = snapshot(self.w.box.root)
        options = self.options(self.source(CODEX_WIN))
        self.assertIn("already in the native store", options.reason)
        self.assertFalse(options.needs_folder)

    def test_a_folder_that_will_not_do(self):
        afile = self.home / "a file"
        afile.write_text("x", encoding="utf-8")
        self.before = snapshot(self.w.box.root)
        for target, needle in (
            ("relative", "must be an absolute path"),
            (str(self.home / "nowhere"), "does not exist"),
            (str(afile), "is not a directory"),
            ("/\0", "has a NUL character"),
        ):
            for session_id in (CLAUDE_WIN, AGY_WIN, GEMINI_WIN, "claude:nope"):
                options = self.options(session_id, target=target)
                self.assertIn(needle, options.reason)
                self.assertEqual(options, ImportOptions(False, options.reason))


class NativeAndImportedTest(OptionsCase):
    foreign = ("claude",)
    native = ("claude",)

    def test_a_native_session(self):
        own = self.w.session(CLAUDE_NATIVE, "native")
        for ref in (own, own.key, CLAUDE_NATIVE):
            self.assertEqual(
                self.options(ref), ImportOptions(False, "this session is already on Linux")
            )
        self.assertEqual(
            self.options(own, target=str(self.home)).reason, "this session is already on Linux"
        )

    def test_already_imported(self):
        self.writes = True  # this test imports; what it asks afterwards is compared below
        native = api.import_session(self.source(CLAUDE_WIN))
        before = snapshot(self.w.box.root)
        # Both sides of the pair, and a bare id (which names the native one).
        for ref in (self.source(CLAUDE_WIN), native, native.key, CLAUDE_WIN):
            self.assertEqual(self.options(ref), ImportOptions(False, "already imported"))
            self.assertEqual(
                self.options(ref, target=str(self.home)), ImportOptions(False, "already imported")
            )
        self.assertEqual(self.options(self.source(CLAUDE_WIN_PLAIN)), AS_IT_IS)
        self.assertEqual(snapshot(self.w.box.root), before)


class OfflineTest(OptionsCase):
    foreign = ("claude", "antigravity")

    def test_a_session_whose_drive_is_not_mounted(self):
        claude, agy = self.source(CLAUDE_WIN), self.source(AGY_WIN)
        self.w.box.unmount(self.w.c)
        index = api.load_index()
        self.before = snapshot(self.w.box.root)
        for session in (claude, agy):
            offline = self.w.session(session.id, "foreign", index)
            self.assertTrue(offline.offline)
            for ref in (offline, offline.key):
                self.assertEqual(
                    self.options(ref), ImportOptions(False, "the drive WinC is not mounted")
                )
            self.assertEqual(
                api.can_import(offline, experimental=True),
                (False, "the drive WinC is not mounted"),
            )


class AnyAdapterTest(OptionsCase):
    """The question is asked of the adapter, whatever it answers: one whose
    import is experimental and needs a folder as well gets both."""

    foreign = ("claude",)

    def setUp(self):
        super().setUp()
        self.pending: list = []  # what the next calls of Claude's plan answer
        self.calls: list[tuple] = []
        real = Claude.import_plan

        def import_plan(provider, s, pathmap, target=None, experimental=False):
            self.calls.append((target, experimental))
            answer = self.pending.pop(0)
            if answer is not None:
                raise answer
            return real(provider, s, pathmap, target, experimental)

        patch = mock.patch.object(Claude, "import_plan", import_plan)
        patch.start()
        self.addCleanup(patch.stop)

    def planned(self, answers):
        """What Claude's plan answers from now on, one per call: an
        exception to raise, or None for the real plan. Returns the list the
        calls are noted in, as ``(target, experimental)``."""
        self.pending[:] = answers
        self.calls = []
        return self.calls

    def test_experimental_and_a_folder(self):
        source = self.source(CLAUDE_WIN)
        calls = self.planned(
            [
                ExperimentalOnly("try it with the flag", "be careful"),
                importer.NoFolder("no folder"),
                None,
            ]
        )
        self.assertEqual(
            api.import_options(source),
            ImportOptions(False, "try it with the flag", True, True, "be careful"),
        )
        self.assertEqual(calls, [(None, False), (None, True), (str(self.home), True)])
        # Asked the other way round, the first refusal is still the reason.
        calls = self.planned(
            [importer.NoFolder("no folder"), ExperimentalOnly("flag", "careful"), None]
        )
        self.assertEqual(
            api.import_options(source), ImportOptions(False, "no folder", True, True, "careful")
        )
        self.assertEqual(calls, [(None, False), (str(self.home), False), (str(self.home), True)])

    def test_can_import_needs_both_then(self):
        source = self.source(CLAUDE_WIN)
        for kwargs, expected in (
            ({}, False),
            ({"experimental": True}, False),  # the folder is still missing
            ({"target": str(self.home)}, False),  # the flag is
            ({"target": str(self.home), "experimental": True}, True),
        ):
            answers = [ExperimentalOnly("flag", "careful"), importer.NoFolder("no folder"), None]
            if "target" in kwargs:
                answers = [ExperimentalOnly("flag", "careful"), None]
            self.planned(answers)
            self.assertEqual(api.can_import(source, **kwargs)[0], expected, kwargs)

    def test_an_adapter_that_refuses_whatever_it_is_given(self):
        source = self.source(CLAUDE_WIN)
        for answers, reason in (
            ([ExperimentalOnly("flag", "careful")] * 2, "flag"),
            ([importer.NoFolder("no folder")] * 2, "no folder"),
            ([ExperimentalOnly("flag", "careful"), NotSupported("not this one")], "not this one"),
            ([importer.NoFolder("no folder"), api.ImportFailed("broken")], "broken"),
            ([NotSupported("never")], "never"),
        ):
            calls = self.planned(answers)
            self.assertEqual(api.import_options(source), ImportOptions(False, reason))
            self.assertEqual(len(calls), len(answers))

    def test_the_stand_in_for_a_folder_that_is_still_to_be_chosen(self):
        self.assertEqual(api._some_folder(), str(self.home))
        with mock.patch.dict(os.environ, {"AGENT_SESSIONS_HOME": str(self.home / "gone")}):
            self.assertEqual(api._some_folder(), os.path.abspath(os.sep))

    def test_something_nobody_planned_for_is_not_an_answer(self):
        self.planned([RuntimeError("a bug")])
        with self.assertRaises(RuntimeError):
            api.import_options(self.source(CLAUDE_WIN))


class WarningOnTheCommandLineTest(OptionsCase):
    foreign = ("antigravity", "claude")

    def test_the_warning_comes_before_an_experimental_import_and_only_then(self):
        warning = f"agent-sessions: {agy_mod.IMPORT_WARNING}\n"
        code, out, err = run("import", AGY_WIN, "--experimental", "--dry-run")
        self.assertEqual((code, err), (0, warning))
        self.assertTrue(out.endswith("dry run: nothing was written\n"))
        # A provider whose import is not experimental: the flag says nothing.
        code, out, err = run("import", CLAUDE_WIN, "--experimental", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        # Without the flag there is the refusal, not the warning.
        code, out, err = run("import", AGY_WIN, "--dry-run")
        self.assertEqual((code, err), (1, f"agent-sessions: {agy_mod.EXPERIMENTAL_ONLY}\n"))
        # A key that names nothing: the usual answer, once.
        code, out, err = run("import", "nope", "--experimental")
        self.assertEqual(code, 1)
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("no session matches 'nope'", err)

    def test_the_import_itself(self):
        self.writes = True
        code, out, err = run("import", AGY_WIN, "--experimental")
        self.assertEqual((code, out), (0, self.w.session(AGY_WIN, "native").key + "\n"))
        first, second = err.splitlines()
        self.assertEqual(first, f"agent-sessions: {agy_mod.IMPORT_WARNING}")
        self.assertIn("imported 3 items", second)
        # Imported already: nothing experimental is about to happen.
        code, _, err = run("import", AGY_WIN, "--experimental")
        self.assertEqual((code, err), (0, "agent-sessions: already imported; nothing was copied\n"))


if __name__ == "__main__":
    unittest.main()
