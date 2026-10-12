"""The session id grammar, held in three places that do not rely on each
other: the index lists no session whose id fails it, ``launch.plan`` builds
no command for one, ``importer.check`` accepts no plan for one.
docs/REVIEW.md R-01.

An id is read from files on another drive. As ``--config=…`` or ``--yolo`` it
would be an option of the tool that resumes the session."""

import contextlib
import dataclasses
import io
import json
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import CLAUDE_NATIVE, CLAUDE_WIN, CODEX_NATIVE, GROK_NATIVE, REAL, World
from support import FIXTURES, FakeProvider, Sandbox, write_jsonl

from agent_sessions import api, cli, importer, journal, launch, textsafe
from agent_sessions.model import UUID_PATTERN, Session, Store
from agent_sessions.providers.antigravity import Antigravity
from agent_sessions.providers.base import Provider
from agent_sessions.providers.claude import Claude
from agent_sessions.providers.codex import Codex
from agent_sessions.providers.gemini import Gemini
from agent_sessions.providers.grok import Grok

GOOD = "01a04ca1-6e27-7817-9f54-07105f962073"
CODEX_FLAG = "--config=shell_environment_policy.inherit=all"  # the review's reproduction
ROLLOUT = f"rollout-2026-08-29T13-57-23-{GOOD}.jsonl"
BAD_IDS = (
    CODEX_FLAG,
    "--yolo",
    "-c",
    "",
    " ",
    "not-a-uuid",
    GOOD + "\n",  # ``fullmatch``, not ``match`` with ``$``
    "\n" + GOOD,
    GOOD + "\x00",
    GOOD[:-1],
    GOOD + "0",
    GOOD.replace("-", ""),
    GOOD.replace("0", "g", 1),
    GOOD.replace("0", "\uff10", 1),  # a full-width digit zero
    f"x {GOOD}",
)
NOT_IMPORTED = "the id of this session is not in the form its tool gives ids; nothing was imported"


def run(*argv):
    """Run cli.main; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def codex_rollout(root, session_id, name=ROLLOUT) -> Path:
    """A Codex thread of one user turn whose ``session_meta`` names
    ``session_id``."""
    turn = {"type": "UserMessage", "content": [{"type": "text", "text": "fix the bug"}]}
    return write_jsonl(
        Path(root) / "sessions" / "2026" / "08" / "29" / name,
        [
            {
                "timestamp": "2026-08-29T08:31:14.474Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "F:\\Proj\\My Site", "source": "cli"},
            },
            {
                "timestamp": "2026-08-29T08:31:15.000Z",
                "ordinal": 1,
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": turn},
            },
        ],
    )


def grok_session(root, dir_name, info_id=None) -> Path:
    session = Path(root) / "sessions" / "%2Fhome%2Fuser" / dir_name
    write_jsonl(session / "chat_history.jsonl", [{"type": "assistant", "content": "hi"}])
    info = {"cwd": "/home/user"}
    if info_id is not None:
        info["id"] = info_id
    (session / "summary.json").write_text(
        json.dumps({"info": info, "session_summary": "fix the bug"}), encoding="utf-8"
    )
    return session / "summary.json"


class GrammarTest(unittest.TestCase):
    def test_the_default_is_a_uuid_in_either_case(self):
        provider = Provider()
        self.assertIs(provider.id_pattern, UUID_PATTERN)
        for good in (GOOD, GOOD.upper(), "00000000-0000-0000-0000-000000000000"):
            self.assertTrue(provider.valid_id(good), good)
        for bad in (*BAD_IDS, None, 7, b"x", [GOOD]):
            self.assertFalse(provider.valid_id(bad), repr(bad))

    def test_every_adapter_keeps_the_default(self):
        # docs/FORMATS.md shows UUIDs for all five; this fails when one changes.
        for adapter in (Antigravity, Claude, Codex, Gemini, Grok):
            self.assertIs(adapter.id_pattern, UUID_PATTERN, adapter.name)


class IndexRefusesTest(unittest.TestCase):
    """Layer one. The adapters are the real ones; the stores are foreign."""

    def setUp(self):
        self.w = World(self, foreign=())
        self.profile = self.w.profile

    def refused(self, provider: str, bad_id: str, store: Path, candidate_file: Path) -> str:
        """Scan; the provider lists nothing, and one warning names the store
        and the candidate file and quotes the id, cleaned and cut."""
        index = self.w.scan()
        self.assertEqual([s.id for s in index.sessions if s.provider == provider], [])
        mine = [w for w in index.warnings if "session id" in w]
        self.assertEqual(len(mine), 1, index.warnings)
        shown = textsafe.clean(bad_id)
        shown = shown[:40] + "…" if len(shown) > 40 else shown
        label = REAL[provider].label
        self.assertEqual(
            mine[0],
            f"{store}: {candidate_file.relative_to(store)}: session id '{shown}' "
            f"is not in the form {label} ids have; not listed",
        )
        # The cache holds the refusal: a second scan parses nothing and says the same.
        summary = api.scan(deep=False)
        self.assertEqual(summary["parsed"], 0)
        self.assertEqual([w for w in summary["warnings"] if "session id" in w], mine)
        return mine[0]

    def test_codex_payload_id(self):
        store = self.profile / ".codex"
        warning = self.refused("codex", CODEX_FLAG, store, codex_rollout(store, CODEX_FLAG))
        self.assertIn(f"'{CODEX_FLAG[:40]}…'", warning)  # cut to 40 characters
        self.assertNotIn(CODEX_FLAG, warning)

    def test_grok_summary_id_and_directory_name(self):
        store = self.profile / ".grok"
        summary = grok_session(store, GOOD, info_id="--yolo")
        self.refused("grok", "--yolo", store, summary.parent)
        shutil.rmtree(store)
        summary = grok_session(store, "--always-approve")
        self.refused("grok", "--always-approve", store, summary.parent)

    def test_antigravity_file_name(self):
        root = self.profile / ".gemini" / "antigravity-cli"
        (root / "conversations").mkdir(parents=True)
        source = FIXTURES / "antigravity" / "windows" / ".gemini" / "antigravity-cli"
        db = root / "conversations" / "--sandbox=off.db"
        shutil.copy(source / "conversations" / "45184a9e-8a4e-4f3f-8c4c-f0e85aeecaaf.db", db)
        self.refused("antigravity", "--sandbox=off", root, db)

    def test_gemini_session_id(self):
        store = self.profile / ".gemini"
        path = write_jsonl(
            store / "tmp" / "p" / "chats" / "session-2026-08-30T10-00-x.jsonl",
            [
                {"sessionId": "--resume\x1b[2J", "projectHash": "h"},
                {"id": "m1", "type": "user", "content": "hello"},
            ],
        )
        warning = self.refused("gemini", "--resume\x1b[2J", store, path)
        self.assertNotIn("\x1b", warning)

    def test_a_good_id_beside_a_bad_one_is_listed(self):
        codex_rollout(self.profile / ".codex", CODEX_FLAG)
        other = "01a04cae-468f-7291-95d7-556e159046ee"
        codex_rollout(self.profile / ".codex", other, f"rollout-2026-08-29T14-11-24-{other}.jsonl")
        index = self.w.scan()
        self.assertEqual([s.id for s in index.sessions], [other])


class IndexCentralTest(unittest.TestCase):
    """Layer one does not depend on the adapter: whatever ``parse`` returns
    and whatever the cache holds, the index checks the id itself."""

    def setUp(self):
        self.box = Sandbox(self)
        self.native = self.box.store(self.box.home)
        strict = mock.patch.object(FakeProvider, "id_pattern", UUID_PATTERN)
        strict.start()
        self.addCleanup(strict.stop)

    def test_an_adapter_that_returns_any_id(self):
        for number, bad in enumerate(BAD_IDS):
            self.box.session(self.native, f"bad{number}", id=bad)
        self.box.session(self.native, "good", id=GOOD)
        self.box.session(self.native, "upper", id=GOOD.upper().replace("0", "1"))
        summary = api.scan()
        self.assertEqual(
            sorted(s.id for s in api.list_sessions()),
            sorted([GOOD, GOOD.upper().replace("0", "1")]),
        )
        refusals = [w for w in summary["warnings"] if "session id" in w]
        self.assertEqual(len(refusals), len(BAD_IDS) - 1)  # the empty id was never a session
        for warning in refusals:
            self.assertEqual(textsafe.clean(warning), warning)
            self.assertTrue(warning.startswith(f"{self.native}: "), warning)
        row = api.status()["stores"][0]
        self.assertEqual((row["listed"], row["skipped"]), (2, len(BAD_IDS)))

    def test_a_cached_entry_with_a_bad_id_is_not_adopted(self):
        # A cache written before the rule existed (here: by a lenient reader).
        self.box.session(self.native, "old", id="--yolo")
        with mock.patch.object(FakeProvider, "id_pattern", re.compile(r"(?s).*")):
            api.scan()
            self.assertEqual([s.id for s in api.list_sessions()], ["--yolo"])
            cached = json.loads(self.box.cache.joinpath("index.json").read_text(encoding="utf-8"))
        entry = cached["parsed"][f"fake:{self.native}"]["candidates"]["old"]
        self.assertEqual(entry["session"]["id"], "--yolo")
        # Listing from that cache alone, no scan:
        index = api.load_index()
        self.assertEqual(index.sessions, [])
        self.assertEqual(len([w for w in index.warnings if "session id '--yolo'" in w]), 1)
        self.assertEqual(index.store_rows[0]["skipped"], 1)
        with self.assertRaises(api.NotFound):
            api.open_session("--yolo", dry_run=True)
        # A scan does not take the entry over either, though its signature still matches.
        FakeProvider.parsed = []
        summary = api.scan()
        self.assertEqual(FakeProvider.parsed, ["old"])
        self.assertEqual(summary["sessions"], 0)
        self.assertEqual(len([w for w in summary["warnings"] if "session id '--yolo'" in w]), 1)
        cached = json.loads(self.box.cache.joinpath("index.json").read_text(encoding="utf-8"))
        entry = cached["parsed"][f"fake:{self.native}"]["candidates"]["old"]
        self.assertIsNone(entry["session"])


class LaunchRefusesTest(unittest.TestCase):
    """Layer two: sessions handed to ``launch`` directly, as if the index
    had let them through."""

    def setUp(self):
        self.w = World(self, foreign=(), native=("claude", "codex", "grok"))
        self.index = self.w.scan()

    def failure(self, session, provider=None, **kwargs) -> str:
        provider = provider or api._provider(session)
        with self.assertRaises(api.LaunchFailed) as caught:
            launch.launch(session, provider, **kwargs)
        return caught.exception.reason

    def test_a_native_session_with_a_bad_id_is_not_resumed(self):
        for good, tool in (
            (CODEX_NATIVE, "Codex"),
            (GROK_NATIVE, "Grok"),
            (CLAUDE_NATIVE, "Claude"),
        ):
            real = self.w.session(good, "native", self.index)
            for bad in BAD_IDS:
                session = dataclasses.replace(real, id=bad)
                for kwargs in ({"dry_run": True}, {}, {"here": True, "dry_run": True}):
                    reason = self.failure(session, **kwargs)
                    self.assertEqual(
                        reason,
                        f"the id of this session is not in the form {tool} ids have; "
                        "it was not resumed",
                    )
        self.assertEqual(self.w.started, [])
        self.assertTrue(self.w.notified)

    def test_no_argument_from_session_data_may_be_an_option(self):
        """With a grammar that lets the id through (a provider whose ids are
        not UUIDs), the argv itself is still checked."""
        real = self.w.session(CODEX_NATIVE, "native", self.index)
        with mock.patch.object(Codex, "id_pattern", re.compile(r"(?s).+")):
            for bad in ("--yolo", "-c", CODEX_FLAG, "-", "-\x00"):
                session = dataclasses.replace(real, id=bad)
                reason = self.failure(session, dry_run=True)
                self.assertEqual(
                    reason,
                    "the data of this session would be read as an option of codex; "
                    "it was not resumed",
                )
            for bad in ("a\x00b", "\x00"):
                session = dataclasses.replace(real, id=bad)
                self.assertEqual(
                    self.failure(session, dry_run=True),
                    "the data of this session holds a NUL character; it was not resumed",
                )
            # An id that is no option goes through, and is its own argument.
            plan = launch.plan(dataclasses.replace(real, id="thread name"), Codex())
            self.assertEqual(plan.argv, ["codex", "resume", "thread name"])
        self.assertEqual(self.w.started, [])

    def test_the_tools_own_options_are_not_session_data(self):
        for good, argv in (
            (CLAUDE_NATIVE, ["claude", "--resume", CLAUDE_NATIVE]),
            (GROK_NATIVE, ["grok", "--resume", GROK_NATIVE]),
            (CODEX_NATIVE, ["codex", "resume", CODEX_NATIVE]),
        ):
            session = self.w.session(good, "native", self.index)
            self.assertEqual(launch.plan(session, api._provider(session)).argv, argv)

    def test_any_field_an_adapter_puts_in_the_command_is_checked(self):
        class Wordy(Claude):
            def resume_argv(self, s):
                return [self.binary, "--resume", s.id, "--add-dir", s.cwd]

        real = self.w.session(CLAUDE_NATIVE, "native", self.index)
        plan = launch.plan(real, Wordy())
        self.assertEqual(plan.argv[3:], ["--add-dir", real.cwd])
        for bad, reason in (
            ("--dangerously-skip-permissions", "would be read as an option of claude"),
            ("/x\x00y", "holds a NUL character"),
        ):
            session = dataclasses.replace(real, cwd=bad)
            self.assertIn(reason, self.failure(session, Wordy()))

        # Session data glued to an option is refused whatever it holds:
        # nothing here knows where the option ends and the data begins.
        class Glued(Claude):
            def resume_argv(self, s):
                return [self.binary, "--resume", s.id, f"--name={s.title}"]

        self.assertIn(
            "would be read as an option of claude", self.failure(real, Glued(), dry_run=True)
        )

        # A command that cannot be compared with its second build: every
        # element after the program's name counts as session data.
        class Odd(Claude):
            def resume_argv(self, s):
                return [self.binary, "--resume", s.id] + (["--extra"] if s.title == "x" else [])

        class Fragile(Claude):
            def resume_argv(self, s):
                if s.title != "x":
                    raise KeyError(s.title)
                return [self.binary, "--resume", s.id]

        for adapter in (Odd(), Fragile()):
            self.assertIn(
                "would be read as an option of claude",
                self.failure(dataclasses.replace(real, title="x"), adapter, dry_run=True),
            )

    def test_the_reviews_reproduction_through_the_cli(self):
        """A native store built by hand, as the review did."""
        shutil.rmtree(self.w.home / ".codex")
        shutil.rmtree(self.w.home / ".grok")
        codex_rollout(self.w.home / ".codex", CODEX_FLAG)
        grok_session(self.w.home / ".grok", GOOD, info_id="--yolo")
        self.w.scan()
        for ref in (CODEX_FLAG, "--yolo", "codex:", "grok:"):
            code, out, err = run("open", "--dry-run", "--", ref)
            self.assertEqual((code, out), (1, ""), ref)
            self.assertIn("no session matches", err)
        code, out, _err = run("list", "--keys")
        self.assertNotIn("--yolo", out)
        self.assertNotIn("--config", out)


class ImporterRefusesTest(unittest.TestCase):
    """Layer three: a plan for a bad id is refused before anything is
    looked at or written."""

    def setUp(self):
        self.w = World(self, foreign=("claude",))
        self.index = self.w.scan()
        self.source = self.w.session(CLAUDE_WIN, "foreign", self.index)

    def test_check_refuses_the_plan(self):
        plan = api._plan(self.source, self.index)
        importer.check(plan)  # the real one is fine
        for bad in BAD_IDS:
            broken = dataclasses.replace(plan, session_id=bad)
            for action in (importer.check, importer.execute):
                with self.assertRaises(importer.ImportFailed) as caught:
                    action(broken)
                self.assertEqual(caught.exception.reason, NOT_IMPORTED)
        self.assertFalse((self.w.home / ".claude").exists())
        self.assertFalse(journal.path().exists())

    def test_a_session_with_a_bad_id_cannot_be_imported(self):
        """Through the facade, with an adapter that plans it all the same."""
        session = dataclasses.replace(self.source, id="--yolo")
        bad = dataclasses.replace(api._plan(self.source, self.index), session_id="--yolo")
        with mock.patch.object(Claude, "import_plan", return_value=bad):
            self.assertEqual(api.can_import(session, self.index), (False, NOT_IMPORTED))
            for action in (api.import_session, api.import_plan):
                with self.assertRaises(api.ImportFailed) as caught:
                    action(session, index=self.index)
                self.assertEqual(caught.exception.reason, NOT_IMPORTED)
        # The real adapter does not plan it in the first place.
        self.assertFalse(api.can_import(session, self.index)[0])
        self.assertFalse((self.w.home / ".claude").exists())
        self.assertFalse(journal.path().exists())

    def test_the_plan_carries_the_providers_grammar(self):
        plan = api._plan(self.source, self.index)
        self.assertIs(plan.id_pattern, Claude.id_pattern)
        session = Session.for_store(Store("claude", "/x", "foreign"), id="thread-1", cwd="/")
        with mock.patch.object(Claude, "id_pattern", re.compile(r"thread-\d+")):
            started = importer.start_plan(session, "/native", self.index.pathmap, Claude.id_pattern)
        self.assertEqual(started.id_pattern.pattern, r"thread-\d+")


if __name__ == "__main__":
    unittest.main()
