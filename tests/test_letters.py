"""Drive letters (docs/SPEC.md "Drive letters"): the evidence an adapter adds
(``Provider.path_evidence``; Claude's ``.claude.json``), how the index caches
it and hands it to the path map, and the ``map`` command with the override
file it edits."""

import contextlib
import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_WIN, World
from support import FIXTURES, FakeProvider, Sandbox, claude_transcript

from agent_sessions import api, cli, pathmap
from agent_sessions import index as index_mod
from agent_sessions.errors import ApiError
from agent_sessions.fsutil import cache_file
from agent_sessions.model import Store
from agent_sessions.providers import base
from agent_sessions.providers import claude as claude_mod
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.claude import Claude
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok

POSIX = os.name == "posix"
ROOT_USER = POSIX and os.geteuid() == 0
FIXTURE_KEYS = ["C:/Users/user", "D:\\Other\\Dir2", "F:/Proj/My Site", "d:/Other/Dir"]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


# --- the Claude adapter -------------------------------------------------------------


class ClaudeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self, {"claude": Claude})
        self.profile = self.box.root / "profile"
        (self.profile / ".claude" / "projects").mkdir(parents=True)
        self.store = Store("claude", str(self.profile / ".claude"), "foreign", str(self.box.root))
        self.config = self.profile / ".claude.json"
        self.provider = Claude()

    def evidence(self, content=None) -> tuple[list[str], list[str]]:
        """``(paths, warnings)`` with ``content`` as the file (bytes, text or
        a value to write as JSON)."""
        if content is not None:
            if not isinstance(content, (bytes, str)):
                content = json.dumps(content)
            self.config.write_bytes(
                content.encode("utf-8") if isinstance(content, str) else content
            )
        with base.collect_warnings() as collected:
            paths = self.provider.path_evidence(self.store)
        return paths, collected.items

    def test_the_file_sits_beside_the_store(self):
        self.assertEqual(self.provider.evidence_files(self.store), [str(self.config)])
        root = Store("claude", "/.claude", "foreign", "/")
        self.assertEqual(self.provider.evidence_files(root), ["/.claude.json"])

    def test_the_keys_of_the_projects_object(self):
        fixture = Store("claude", str(FIXTURES / "claude" / "windows" / ".claude"), "foreign", "/")
        self.assertEqual(self.provider.path_evidence(fixture), FIXTURE_KEYS)
        self.assertEqual(
            self.evidence({"projects": {"F:\\b": {}, "C:\\a": 5, "": None}, "other": 1}),
            (["", "C:\\a", "F:\\b"], []),  # sorted; what a value is does not matter
        )
        self.assertEqual(
            self.evidence(b"\xef\xbb\xbf" + b'{"projects": {"C:\\\\x": {}}}')[0], ["C:\\x"]
        )

    def test_nothing_from_a_file_that_is_missing_or_not_what_it_should_be(self):
        self.assertEqual(self.evidence(), ([], []))  # no file: the normal case
        for content in (
            b"",
            b"{",
            b"not json at all",
            b"\xff\xfe\x00\x01",
            "[]",
            "null",
            "5",
            '"text"',
            {},
            {"projects": None},
            {"projects": []},
            {"projects": ["C:\\x"]},
            {"projects": "C:\\x"},
            {"projects": 7},
            {"Projects": {"C:\\x": {}}},  # another key
            "[" * 100_000,  # nested deeper than the parser goes
            '{"projects": ' + "[" * 100_000,
        ):
            with self.subTest(content=str(content)[:30]):
                self.assertEqual(self.evidence(content), ([], []))

    def test_a_file_over_the_cap_is_not_read(self):
        self.assertEqual(claude_mod._CONFIG_MAX, 16 * 1024 * 1024)
        with mock.patch.object(claude_mod, "_CONFIG_MAX", 64):
            self.assertEqual(self.evidence({"projects": {"C:\\x": {}}})[0], ["C:\\x"])
            paths, warnings = self.evidence({"projects": {"C:\\" + "x" * 64: {}}})
        self.assertEqual(paths, [])
        self.assertEqual(warnings, [f"{self.config}: larger than 64 B; not read"])

    def test_what_is_not_a_regular_file_is_not_opened(self):
        self.config.mkdir()
        paths, warnings = self.evidence()
        self.assertEqual((paths, warnings), ([], [f"{self.config}: not a regular file; not read"]))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_named_pipe_does_not_hang_the_scan(self):
        os.mkfifo(self.config)  # opening it for reading would wait for a writer
        paths, warnings = self.evidence()
        self.assertEqual((paths, warnings), ([], [f"{self.config}: not a regular file; not read"]))

    @unittest.skipIf(ROOT_USER or not POSIX, "needs file modes that bind")
    def test_a_file_that_cannot_be_read(self):
        self.config.write_text('{"projects": {"C:\\\\x": {}}}', encoding="utf-8")
        self.config.chmod(0)
        self.addCleanup(self.config.chmod, 0o600)
        self.assertEqual(self.evidence(), ([], []))

    def test_the_other_adapters_add_none(self):
        provider = base.Provider()
        self.assertEqual(
            (provider.evidence_files(self.store), provider.path_evidence(self.store)), ([], [])
        )
        for adapter in (Antigravity, Codex, Gemini, Grok):
            with self.subTest(adapter=adapter.name):
                self.assertEqual(adapter().evidence_files(self.store), [])
                self.assertEqual(adapter().path_evidence(self.store), [])


# --- the index ------------------------------------------------------------------------


class Counting(Claude):
    """Claude's adapter, counting how often the evidence is read."""

    asked: list = []

    def path_evidence(self, store):
        type(self).asked.append(store.root)
        return super().path_evidence(store)


class IndexEvidenceTest(unittest.TestCase):
    """The fixture's Windows profile on ``WinC``; its ``.claude.json`` names
    ``C:/Users/user``, ``F:/Proj/My Site``, ``d:/Other/Dir`` and
    ``D:\\Other\\Dir2``. No session of the fixture recorded anything on D:."""

    def setUp(self):
        self.w = World(self, foreign=("claude",), native=("claude",))
        Counting.asked = []
        patch = mock.patch.dict("agent_sessions.providers._REGISTRY", {"claude": Counting})
        patch.start()
        self.addCleanup(patch.stop)
        self.store = str(self.w.profile / ".claude")
        self.config = self.w.profile / ".claude.json"
        self.d = self.w.box.mount("DataD")

    def letters(self) -> dict:
        status = api.status()
        return {
            row["letter"]: (row["mount"], row["how"], row["paths"]) for row in status["letters"]
        }

    def unresolved(self) -> list:
        return [(r["letter"], r["reason"], r["paths"]) for r in api.status()["unresolved_letters"]]

    def test_a_letter_no_session_recorded_resolves_by_the_evidence(self):
        api.scan(deep=True)
        self.assertNotIn("D:", self.letters())
        self.assertEqual(self.unresolved(), [("D:", "no evidence", 2)])
        (self.d / "Other" / "Dir2").mkdir(parents=True)
        api.scan()
        self.assertEqual(self.letters()["D:"], (str(self.d), "evidence", 1))
        self.assertEqual(self.unresolved(), [])
        (self.d / "Other" / "dir").mkdir()  # the key with the lower-case letter, another folder
        self.assertEqual(api.load_index().pathmap.letters()[1]["paths"], 2)
        status = api.status()
        (drive,) = [m for m in status["mounts"] if m["path"] == str(self.d)]
        self.assertEqual(drive["letter"], "D:")
        # It is the store's letter table that is remembered with its volume.
        self.assertEqual(
            api.load_index().volumes[str(self.w.c)]["letters"],
            {"C:": str(self.w.c), "D:": str(self.d), "F:": str(self.w.f)},
        )

    def test_a_session_on_that_letter_then_maps(self):
        (self.d / "Other" / "Dir2").mkdir(parents=True)
        (self.d / "Work").mkdir()
        sid = "eeeeeeee-1111-4222-8333-444444444444"
        claude_transcript(self.store, sid, "D:\\Work", project="D--Work")
        gone = "ffffffff-1111-4222-8333-444444444444"
        claude_transcript(self.store, gone, "D:\\Hypermedia\\Ep2", project="D--Hypermedia-Ep2")
        api.scan(deep=True)
        sessions = {s.id: s for s in api.list_sessions()}
        self.assertEqual(
            (sessions[sid].cwd_local, sessions[sid].cwd_exists), (str(self.d / "Work"), True)
        )
        # A folder that is gone still maps to nothing, whatever the letter.
        self.assertEqual((sessions[gone].cwd_local, sessions[gone].cwd_exists), (None, False))

    def test_the_file_is_read_once_and_again_only_when_it_changed(self):
        api.scan(deep=True)
        self.assertEqual(Counting.asked, [self.store])  # the native store is not asked
        entry = json.loads(cache_file().read_text(encoding="utf-8"))["parsed"][
            f"claude:{self.store}"
        ]
        st = os.stat(self.config)
        self.assertEqual(
            entry["evidence"],
            {
                "sig": [[str(self.config), st.st_mtime_ns, st.st_size]],
                "paths": FIXTURE_KEYS,
                "warnings": [],
            },
        )
        native = json.loads(cache_file().read_text(encoding="utf-8"))["parsed"]
        self.assertEqual(
            native[f"claude:{self.w.home / '.claude'}"]["evidence"],
            {"sig": [], "paths": [], "warnings": []},
        )
        for deep in (False, True, None, False):
            api.scan(deep=deep)
        api.load_index()
        api.status()
        self.assertEqual(Counting.asked, [self.store])

        self.config.write_text(json.dumps({"projects": {"Q:\\new": {}}}), encoding="utf-8")
        api.scan(deep=False)
        self.assertEqual(Counting.asked, [self.store] * 2)
        self.assertEqual(self.unresolved(), [("Q:", "no evidence", 1)])
        os.utime(self.config, ns=(st.st_atime_ns, st.st_mtime_ns + 7 * 10**9))  # touched only
        api.scan(deep=False)
        self.assertEqual(len(Counting.asked), 3)
        self.config.unlink()
        api.scan(deep=False)
        api.scan(deep=False)
        self.assertEqual(len(Counting.asked), 4)  # once for "gone", then cached as gone
        self.assertEqual(self.unresolved(), [])

    def test_another_reader_reads_it_again(self):
        api.scan(deep=True)
        with mock.patch.object(index_mod, "reader_id", return_value="another reader"):
            api.scan(deep=False)
            api.scan(deep=False)
        self.assertEqual(len(Counting.asked), 2)

    def test_verify_agrees(self):
        (self.d / "Other" / "Dir2").mkdir(parents=True)
        api.scan(deep=True)
        result = api.verify()
        self.assertEqual((result["ok"], result["differences"]), (True, []))

    def test_what_the_file_cannot_give_is_a_warning_while_the_drive_is_there(self):
        self.config.unlink()
        self.config.mkdir()
        summary = api.scan(deep=True)
        warning = f"{self.config}: not a regular file; not read"
        self.assertIn(warning, summary["warnings"])
        self.assertIn(warning, api.status()["warnings"])
        self.w.box.unmount(self.w.c)
        self.assertNotIn(warning, api.load_index().warnings)

    def test_evidence_of_a_drive_that_is_away_is_not_used(self):
        api.scan(deep=True)
        self.assertEqual(self.unresolved(), [("D:", "no evidence", 2)])
        self.w.box.unmount(self.w.c)
        index = api.load_index()
        self.assertEqual((index.pathmap.letters(), index.pathmap.unresolved()), ([], []))
        self.assertTrue(self.w.session(CLAUDE_WIN, "foreign", index).offline)

    def test_an_adapter_that_fails_costs_the_evidence_not_the_scan(self):
        class Failing(Counting):
            def path_evidence(self, store):
                type(self).asked.append(store.root)
                raise RuntimeError("boom")

        with mock.patch.dict("agent_sessions.providers._REGISTRY", {"claude": Failing}):
            summary = api.scan(deep=True)
            warning = f"{self.store}: path evidence not read: RuntimeError: boom"
            self.assertIn(warning, summary["warnings"])
            self.assertGreater(summary["sessions"], 2)
            api.scan(deep=False)
            self.assertEqual(len(Counting.asked), 2)  # not cached: asked again
        api.scan(deep=False)  # the real adapter again
        self.assertNotIn(warning, api.load_index().warnings)
        self.assertEqual(self.unresolved(), [("D:", "no evidence", 2)])

    def test_what_an_adapter_returns_is_held_to_a_form_and_a_size(self):
        class Sloppy(Counting):
            def path_evidence(self, store):
                return [
                    "Q:\\ok",
                    5,
                    None,
                    "",
                    "Q:\\ok",
                    "R:\\" + "x" * index_mod.CWD_MAX,
                    ["S:\\x"],
                ]

        with mock.patch.dict("agent_sessions.providers._REGISTRY", {"claude": Sloppy}):
            api.scan(deep=True)
        entry = api.load_index().parsed[f"claude:{self.store}"]["evidence"]
        self.assertEqual((entry["paths"], entry["warnings"]), (["Q:\\ok"], []))

        class Many(Counting):
            def path_evidence(self, store):
                return [f"Q:\\dir{n}" for n in range(5)]

        with (
            mock.patch.dict("agent_sessions.providers._REGISTRY", {"claude": Many}),
            mock.patch.object(index_mod, "EVIDENCE_MAX", 3),
            mock.patch.object(index_mod, "reader_id", return_value="many"),
        ):
            summary = api.scan(deep=False)
        entry = api.load_index().parsed[f"claude:{self.store}"]["evidence"]
        self.assertEqual(entry["paths"], ["Q:\\dir0", "Q:\\dir1", "Q:\\dir2"])
        self.assertIn(
            f"{self.store}: 5 recorded paths; only 3 are used to work out drive letters",
            summary["warnings"],
        )
        self.assertEqual(index_mod.EVIDENCE_MAX, 10_000)

    def test_a_hostile_key_stays_data(self):
        evil = "Q:\\evil\x1b[2J\u202e\nname"
        self.config.write_text(json.dumps({"projects": {evil: {}, "--help": {}}}), encoding="utf-8")
        api.scan(deep=True)
        self.assertEqual(self.unresolved(), [("Q:", "no evidence", 1)])
        for argv in (("status",), ("map",), ("list",)):
            code, out, err = run(*argv)
            self.assertEqual((code, err), (0, ""))
            for bad in ("\x1b", "\u202e"):
                self.assertNotIn(bad, out)


class NoEvidenceFilesTest(unittest.TestCase):
    def test_an_adapter_without_files_is_never_asked(self):
        class Asked(FakeProvider):
            def path_evidence(self, store):
                raise AssertionError("asked")

        box = Sandbox(self, {"fake": Asked})
        disk = box.mount("Disk")
        box.session(box.store(disk / "Users" / "x"), "a")
        self.assertEqual(api.scan(deep=True)["warnings"], [])
        entry = api.load_index().parsed[f"fake:{disk / 'Users' / 'x' / '.fake'}"]
        self.assertEqual(entry["evidence"], {"sig": [], "paths": [], "warnings": []})

    def test_an_adapter_whose_file_list_fails(self):
        class Broken(FakeProvider):
            def evidence_files(self, store):
                raise OSError("no list")

        box = Sandbox(self, {"fake": Broken})
        disk = box.mount("Disk")
        root = box.store(disk / "Users" / "x")
        box.session(root, "a")
        summary = api.scan(deep=True)
        self.assertEqual(summary["warnings"], [f"{root}: path evidence not read: OSError: no list"])
        self.assertEqual(summary["sessions"], 1)


# --- the map command and the override file -----------------------------------------


class MapTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(self)
        self.config = self.box.home / ".config" / "agent-sessions"
        self.file = self.config / "pathmap.json"
        self.disk = self.box.mount("Win Disk")
        (self.disk / "Windows").mkdir()
        self.data = self.box.mount("Data Disk")
        (self.data / "proj").mkdir()
        self.foreign = self.box.store(self.disk / "Users" / "arun")
        self.box.session(self.foreign, "c", cwd="C:\\Users\\arun")
        self.box.session(self.foreign, "d", cwd="D:\\proj\\gone")
        api.scan(deep=True)

    def saved(self) -> dict:
        return json.loads(self.file.read_text(encoding="utf-8"))

    def test_without_arguments_it_prints_the_letters(self):
        code, out, err = run("map")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(
            lines[0], rf"^  C:  ->  {self.disk}\s+evidence, 1 path, recorded on {self.disk}$"
        )
        self.assertRegex(
            lines[1], rf"^  D:  ->  unresolved\s+no evidence, 1 path, recorded on {self.disk}$"
        )
        status = run("status")[1]
        self.assertIn("drive letters\n" + out, status)  # the same lines as `status` has
        self.assertFalse(self.file.exists())  # printing writes nothing
        self.assertFalse(self.config.exists())

    def test_without_arguments_and_nothing_recorded(self):
        Sandbox(self)
        self.assertEqual(run("map"), (0, "  none recorded\n", ""))

    def test_a_drive_that_is_away_keeps_its_letter_in_the_table(self):
        self.box.unmount(self.disk)
        self.assertEqual(
            run("map")[1], f"  C:  ->  {self.disk}  as last resolved; the drive is not mounted\n"
        )

    def test_set_writes_the_override_file(self):
        self.assertEqual(run("map", "D:", str(self.data)), (0, f"D: -> {self.data}\n", ""))
        self.assertEqual(
            self.file.read_text(encoding="utf-8"),
            json.dumps({"D:": str(self.data)}, indent=2) + "\n",
        )
        self.assertEqual(os.listdir(self.config), ["pathmap.json"])  # no temp file left
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o700)
        # It takes effect in the next listing, with no scan between.
        out = run("map")[1]
        self.assertRegex(out, rf"D:  ->  {self.data}\s+override, 1 path, recorded on {self.disk}")
        status = api.status()
        self.assertEqual(
            [m["letter"] for m in status["mounts"] if m["path"] == str(self.data)], ["D:"]
        )
        self.assertEqual(status["warnings"], [])

    def test_set_keeps_the_other_entries_and_replaces_every_spelling_of_the_letter(self):
        self.config.mkdir(parents=True)
        other = {"/home/user": str(self.box.home), "junk": 5, "E:": "/not/checked/here"}
        self.file.write_text(
            json.dumps(dict(other, **{"d:": "/old", "D:\\": "/older", "D:": "/oldest"})),
            encoding="utf-8",
        )
        self.file.chmod(0o644)
        self.assertEqual(run("map", "d:", str(self.data) + "/")[1], f"D: -> {self.data}\n")
        self.assertEqual(self.saved(), dict(other, **{"D:": str(self.data)}))
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)  # tightened

    def test_a_tilde_is_expanded_by_the_command(self):
        """AUDIT N-20: ``--to``, ``--in`` and ``-o`` expanded it, ``map`` did
        not, and refused the directory as not absolute."""
        with mock.patch.dict(os.environ, {"HOME": str(self.data)}):
            self.assertEqual(run("map", "D:", "~/proj"), (0, f"D: -> {self.data / 'proj'}\n", ""))
            self.assertEqual(run("map", "E:", "~"), (0, f"E: -> {self.data}\n", ""))
            # The engine takes an absolute path and nothing else, as before.
            with self.assertRaises(ApiError):
                api.set_letter("F:", "~/proj")
        self.assertEqual(self.saved(), {"D:": str(self.data / "proj"), "E:": str(self.data)})

    def test_set_again_replaces(self):
        run("map", "D:", str(self.data))
        run("map", "D:", str(self.disk))
        run("map", "Z:", str(self.data))
        self.assertEqual(self.saved(), {"D:": str(self.disk), "Z:": str(self.data)})

    def test_what_is_not_a_letter_or_not_an_existing_absolute_directory(self):
        a_file = self.data / "file.txt"
        a_file.write_text("x", encoding="utf-8")
        cases = (
            (("D", str(self.data)), "'D' is not a drive letter; write it like D:"),
            (("DD:", str(self.data)), "'DD:' is not a drive letter; write it like D:"),
            (("1:", str(self.data)), "'1:' is not a drive letter; write it like D:"),
            (("D:\\x", str(self.data)), "'D:\\\\x' is not a drive letter; write it like D:"),
            (("D:\n", str(self.data)), "'D:\\n' is not a drive letter; write it like D:"),
            (("/home", str(self.data)), "'/home' is not a drive letter; write it like D:"),
            (("D:", "relative/dir"), "relative/dir is not an absolute path"),
            (("D:", "."), ". is not an absolute path"),
            (
                ("D:", str(self.data / "missing")),
                f"{self.data / 'missing'} is not a directory that exists",
            ),
            (("D:", str(a_file)), f"{a_file} is not a directory that exists"),
        )
        for argv, reason in cases:
            with self.subTest(argv=argv):
                self.assertEqual(run("map", *argv), (1, "", f"agent-sessions: {reason}\n"))
        self.assertFalse(self.config.exists())

    def test_usage(self):
        code, out, err = run("map", "D:")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(
            err, "agent-sessions: map: say which directory D: stands for, or use --remove\n"
        )
        self.assertEqual(
            run("map", "D:", "--remove", "E:"),
            (2, "", "agent-sessions: map: --remove takes the letter alone\n"),
        )
        self.assertEqual(run("map", "D:", str(self.data), "extra")[0], 2)
        self.assertEqual(run("map", "--remove")[0], 2)
        self.assertFalse(self.config.exists())
        code, out, _err = run("map", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--remove LETTER:", out)

    def test_an_existing_file_that_cannot_be_read_is_not_overwritten(self):
        self.config.mkdir(parents=True)
        for content in (b"{ not json", b"[]", b'"text"', b"", b"\xff\xfe", b"[" * 100_000):
            with self.subTest(content=content[:12]):
                self.file.write_bytes(content)
                for argv in (("D:", str(self.data)), ("--remove", "D:")):
                    code, out, err = run("map", *argv)
                    self.assertEqual((code, out), (1, ""))
                    self.assertRegex(
                        err,
                        rf"^agent-sessions: {self.file} "
                        r"(cannot be read \(.+\)|is not a JSON object); it was not changed\n$",
                    )
                self.assertEqual(self.file.read_bytes(), content)
        self.file.unlink()
        self.file.mkdir()
        self.assertEqual(run("map", "D:", str(self.data))[0], 1)
        self.assertTrue(self.file.is_dir())

    @unittest.skipIf(ROOT_USER or not POSIX, "needs file modes that bind")
    def test_a_file_without_read_permission_is_not_overwritten(self):
        self.config.mkdir(parents=True)
        self.file.write_text('{"E:": "/keep"}', encoding="utf-8")
        self.file.chmod(0)
        code, _out, err = run("map", "D:", str(self.data))
        self.assertEqual(code, 1)
        self.assertIn("cannot be read (Permission denied); it was not changed", err)
        self.file.chmod(0o600)
        self.assertEqual(self.saved(), {"E:": "/keep"})

    @unittest.skipIf(ROOT_USER or not POSIX, "needs file modes that bind")
    def test_a_directory_that_cannot_be_written(self):
        self.config.mkdir(parents=True)
        self.config.chmod(0o500)
        self.addCleanup(self.config.chmod, 0o700)
        self.assertEqual(
            run("map", "D:", str(self.data)),
            (1, "", f"agent-sessions: {self.file} was not written: Permission denied\n"),
        )

    def test_remove(self):
        run("map", "D:", str(self.data))
        run("map", "E:", str(self.data))
        self.assertEqual(run("map", "--remove", "d:"), (0, "D: is no longer set by hand\n", ""))
        self.assertEqual(self.saved(), {"E:": str(self.data)})
        self.assertEqual(
            run("map", "--remove", "D:"),
            (1, "", f"agent-sessions: D: has no entry in {self.file}\n"),
        )
        self.assertEqual(run("map", "--remove", "E:")[0], 0)
        self.assertEqual(self.saved(), {})
        self.assertEqual(run("map", "--remove", "nope")[0], 1)
        self.assertRegex(run("map")[1], r"D:  ->  unresolved")

    def test_remove_without_a_file(self):
        self.assertEqual(
            run("map", "--remove", "D:"),
            (1, "", f"agent-sessions: D: has no entry in {self.file}\n"),
        )
        self.assertFalse(self.config.exists())

    def test_the_engine_functions(self):
        self.assertEqual(api.set_letter("d:", self.data), ("D:", str(self.data)))
        self.assertEqual(pathmap.override_file(), self.file)
        self.assertEqual(api.remove_letter("D:"), "D:")
        for call, args in ((api.set_letter, (5, str(self.data))), (api.remove_letter, (None,))):
            with self.assertRaises(ApiError):
                call(*args)

    def test_a_name_that_is_not_utf8_round_trips(self):
        odd = os.fsdecode(os.fsencode(str(self.data)) + b"/caf\xe9")
        try:
            os.mkdir(odd)
        except OSError:
            self.skipTest("this filesystem takes no such name")
        self.assertEqual(run("map", "D:", odd)[0], 0)
        self.assertEqual(self.saved(), {"D:": odd})
        self.assertEqual(api.load_index().pathmap.to_local("D:\\", str(self.disk)), (odd, True))


if __name__ == "__main__":
    unittest.main()
