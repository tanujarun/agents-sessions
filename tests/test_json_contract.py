"""The ``--json`` contract (docs/REVIEW.md R-07): every JSON output of the
CLI against docs/JSON.md.

The tables of that file are the schema. They are read here and every real
output is checked against them: the exact set of keys of every object, the
type of every value, whether it may be null, and the orderings the file
promises. A field that is added, renamed or dropped, or whose type changes,
fails here until the document says so too."""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_world import AGY_NO_CWD, CLAUDE_WIN, World
from support import REPO, Sandbox, claude_transcript

from agent_sessions import api, cli, launch

DOC = REPO / "docs" / "JSON.md"
# What each command prints, by the name of its table. Changing an entry is
# changing the contract.
OUTPUTS = {"list": "array of Session", "scan": "ScanSummary", "status": "Status"}
TABLES = {
    "Session",
    "ScanSummary",
    "Mount",
    "WalkStats",
    "ProviderCount",
    "Status",
    "StatusMount",
    "ProviderStatus",
    "StoreStatus",
    "SkippedStore",
    "DriveLetter",
    "UnresolvedLetter",
    "Tool",
    "NoReaderTool",
}
SESSION_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")
SCAN_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
QDRIVE = "aaaaaaaa-1111-4222-8333-444444444444"
DESKTOP = "bbbbbbbb-1111-4222-8333-444444444444"
GONE = "cccccccc-1111-4222-8333-444444444444"


def read_doc() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """``(tables, outputs)`` of docs/JSON.md: table name -> field -> type,
    and command -> the type its ``Output:`` line names."""
    tables: dict[str, dict[str, str]] = {}
    outputs: dict[str, str] = {}
    command = table = None
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if found := re.fullmatch(r"## `(\w+) --json`", line):
            command, table = found.group(1), None
        elif line.startswith("## "):
            command = table = None
        elif found := re.fullmatch(r"### (\w+)", line):
            table = found.group(1)
            tables[table] = {}
        elif (found := re.fullmatch(r"Output: `(.+)`", line)) and command:
            outputs[command] = found.group(1)
        elif (found := re.match(r"\| `(\w+)` \| ([^|]+) \| ", line)) and table:
            tables[table][found.group(1)] = found.group(2).strip()
    return tables, outputs


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Checker:
    """Walks a value by a type of the document and notes what it met."""

    def __init__(self, case, tables):
        self.case, self.tables = case, tables
        self.seen: set[str] = set()  # tables an object was checked against
        self.null: set[tuple[str, str]] = set()  # (table, field) seen null
        self.filled: set[tuple[str, str]] = set()  # ... and seen with a value

    def check(self, value, kind, where="$", field=None):
        case = self.case
        if kind.endswith(" or null"):
            if value is None:
                if field:
                    self.null.add(field)
                return
            kind = kind[: -len(" or null")]
        else:
            case.assertIsNotNone(value, f"{where} is null and is not documented as nullable")
        if field:
            self.filled.add(field)
        if kind.startswith("array of "):
            case.assertIs(type(value), list, where)
            for number, item in enumerate(value):
                self.check(item, kind[len("array of ") :], f"{where}[{number}]")
        elif kind.startswith("map of string to "):
            case.assertIs(type(value), dict, where)
            for key, item in value.items():
                case.assertIs(type(key), str, where)
                self.check(item, kind[len("map of string to ") :], f"{where}[{key!r}]")
        elif kind == "string":
            case.assertIs(type(value), str, where)
        elif kind == "integer":
            case.assertIs(type(value), int, where)
        elif kind == "number":
            case.assertIn(type(value), (int, float), where)
        elif kind == "boolean":
            case.assertIs(type(value), bool, where)
        else:
            case.assertIn(kind, self.tables, f"{where}: the document has no table {kind}")
            case.assertIs(type(value), dict, where)
            table = self.tables[kind]
            case.assertEqual(sorted(value), sorted(table), f"{where}: the keys of a {kind}")
            self.seen.add(kind)
            for name, inner in table.items():
                self.check(value[name], inner, f"{where}.{name}", (kind, name))


def keys_in_order(text) -> list[list[str]]:
    """The keys of every object of a JSON text, as they are written."""
    found = []

    def note(pairs):
        found.append([key for key, _value in pairs])
        return dict(pairs)

    json.loads(text, object_pairs_hook=note)
    return found


class DocumentTest(unittest.TestCase):
    def test_the_document_has_the_tables_and_outputs_this_suite_pins(self):
        tables, outputs = read_doc()
        self.assertEqual(set(tables), TABLES)
        self.assertEqual(outputs, OUTPUTS)
        for name, table in tables.items():
            self.assertTrue(table, f"the table {name} has no rows")
            for field, kind in table.items():
                base = kind.removesuffix(" or null")
                for prefix in ("array of ", "map of string to "):
                    base = base.removeprefix(prefix)
                self.assertIn(
                    base,
                    {"string", "integer", "number", "boolean"} | TABLES,
                    f"{name}.{field}: {kind!r} is not a type the document defines",
                )

    def test_every_command_with_a_json_flag_is_in_the_document(self):
        parser = cli.build_parser()
        actions = next(a for a in parser._actions if a.dest == "command")
        with_json = sorted(
            name
            for name, sub in actions.choices.items()
            if any("--json" in a.option_strings for a in sub._actions)
        )
        self.assertEqual(with_json, sorted(OUTPUTS))


class ContractTest(unittest.TestCase):
    """One machine that has a little of everything the outputs can hold."""

    tables: dict[str, dict[str, str]]

    @classmethod
    def setUpClass(cls):
        cls.tables, _outputs = read_doc()

    def setUp(self):
        self.w = World(self, native=("claude", "codex", "grok"))
        profile = self.w.profile
        # A session on a drive letter nothing resolves.
        claude_transcript(profile / ".claude", QDRIVE, "Q:\\Nowhere\\x", project="Q--Nowhere-x")
        # A store that is found and skipped on purpose.
        desktop = profile / "AppData" / "Roaming" / "Claude" / "local-agent-mode-sessions" / "a"
        claude_transcript(desktop / ".claude", DESKTOP, "C:\\Users\\user")
        # A drive with a store that is then unplugged.
        self.gone = self.w.box.mount("Gone")
        claude_transcript(self.gone / "Users" / "x" / ".claude", GONE, "C:\\Users\\x")
        # Tools: one that is only a wrapper, one that is installed and has no reader.
        self.w.path.wrapper("crush")
        self.w.installed.add("opencode")
        launch.forget_tools()
        self.scan_deep = self.json("scan", "--deep", "--json")
        self.w.box.unmount(self.gone)
        api.import_session(self.w.session(CLAUDE_WIN, "foreign"))  # an imported pair
        self.json("scan", "--json")  # deep once more: the set of mounts changed
        self.scan_quick = self.json("scan", "--json")

    def json(self, *argv):
        code, out, err = run(*argv)
        self.assertEqual((code, err), (0, ""), err)
        self.assertTrue(out.endswith("\n"))
        self.raw = out
        return json.loads(out)

    def checked(self, value, command) -> Checker:
        checker = Checker(self, self.tables)
        checker.check(value, OUTPUTS[command])
        return checker

    # --- shapes ---------------------------------------------------------------

    def test_every_output_has_the_documented_keys_and_types(self):
        listing = self.json("list", "--json")
        status = self.json("status", "--json")
        seen: set[str] = set()
        null: set[tuple[str, str]] = set()
        filled: set[tuple[str, str]] = set()
        for value, command in (
            (listing, "list"),
            (self.scan_deep, "scan"),
            (self.scan_quick, "scan"),
            (status, "status"),
        ):
            checker = self.checked(value, command)
            seen |= checker.seen
            null |= checker.null
            filled |= checker.filled
        # Nothing in the document went unchecked: an object of every table...
        self.assertEqual(seen, TABLES)
        # ... every field with a value, and these nullable ones both ways.
        documented = {(t, f) for t, fields in self.tables.items() for f in fields}
        self.assertEqual(documented - filled, set())
        self.assertLessEqual(
            {
                ("Session", "cwd"),
                ("Session", "cwd_local"),
                ("Session", "imported_as"),
                ("Session", "note"),
                ("ScanSummary", "walk"),
                ("StatusMount", "letter"),
                ("StatusMount", "walk"),
                ("ProviderStatus", "installed"),
                ("Tool", "path"),
            },
            null,
        )
        nullable = {(t, f) for t, fs in self.tables.items() for f, k in fs.items() if "null" in k}
        self.assertLessEqual(null, nullable)

    def test_status_before_any_scan(self):
        Sandbox(self)  # an empty machine over the one of setUp
        status = self.json("status", "--json")
        checker = self.checked(status, "status")
        self.assertLessEqual({("Status", "last_quick"), ("Status", "last_deep")}, checker.null)
        # No cache: the listing scans first, says so on stderr, and is still the array.
        self.assertEqual(
            run("list", "--provider", "fake", "--json"),
            (0, "[]\n", "agent-sessions: no usable cache; scanning…\n"),
        )

    def test_a_manual_drive_letter_no_session_uses(self):
        self.w.override({"/home/user": str(self.w.home), "Z:": str(self.w.f)})
        status = self.json("status", "--json")
        checker = self.checked(status, "status")
        self.assertIn(("DriveLetter", "source_mount"), checker.null)
        self.assertIsNone(status["letters"][0]["source_mount"])  # null sorts first

    # --- values the document names ----------------------------------------------

    def test_sessions(self):
        listing = self.json("list", "--json")
        self.assertGreater(len(listing), 8)
        for row in listing:
            self.assertEqual(row, api.find_session(row["key"]).to_dict())
            self.assertIn(row["origin"], ("native", "foreign"))
            self.assertRegex(row["key"], rf"^{row['provider']}:[0-9a-f]{{8}}:{row['id']}$")
            self.assertLessEqual(len(row["title"]), 80)
            self.assertNotIn("\n", row["title"])
            for stamp in (row["started"], row["updated"]):
                if stamp is not None:
                    self.assertRegex(stamp, SESSION_TIME)
            self.assertEqual(row["cwd_exists"], row["cwd_local"] is not None)
            if row["cwd_local"] is not None:
                self.assertTrue(os.path.isdir(row["cwd_local"]))
            if row["origin"] == "native":
                self.assertIsNone(row["imported_as"])
            else:
                self.assertFalse(row["launchable"])
            self.assertTrue(all(os.path.isabs(path) for path in row["files"]))
        by_id = {(row["origin"], row["id"]): row for row in listing}
        self.assertEqual(
            by_id["foreign", CLAUDE_WIN]["imported_as"], by_id["native", CLAUDE_WIN]["key"]
        )
        self.assertIsNone(by_id["foreign", AGY_NO_CWD]["cwd"])
        self.assertEqual(by_id["foreign", QDRIVE]["cwd"], "Q:\\Nowhere\\x")
        self.assertNotIn(GONE, [row["id"] for row in listing])  # its drive is not mounted
        self.assertNotIn(DESKTOP, [row["id"] for row in listing])  # skipped on purpose
        self.assertEqual(
            {row["provider"] for row in listing},
            {"antigravity", "claude", "codex", "gemini", "grok"},
        )
        only = self.json("list", "--provider", "codex", "--json")
        self.assertEqual(only, [row for row in listing if row["provider"] == "codex"])

    def test_scan(self):
        deep, quick = self.scan_deep, self.scan_quick
        self.assertEqual((deep["mode"], quick["mode"]), ("deep", "quick"))
        self.assertEqual((deep["cancelled"], quick["cancelled"]), (False, False))
        self.assertIsNone(quick["walk"])
        self.assertEqual(sorted(deep["walk"]), [m["path"] for m in deep["mounts"]])
        self.assertEqual(
            sorted(deep["by_provider"]), ["antigravity", "claude", "codex", "gemini", "grok"]
        )
        for summary in (deep, quick):
            self.assertEqual(
                summary["sessions"], sum(p["sessions"] for p in summary["by_provider"].values())
            )
            self.assertEqual(
                summary["stores"], sum(p["stores"] for p in summary["by_provider"].values())
            )
            self.assertEqual(summary["parsed"] + summary["cached"], summary["candidates"])
        self.assertEqual((deep["stores_not_mounted"], quick["stores_not_mounted"]), (0, 1))
        self.assertEqual((deep["skipped_stores"], quick["skipped_stores"]), (1, 1))

    def test_status(self):
        status = self.json("status", "--json")
        self.assertEqual(status["cache"], str(api.cache_file()))
        self.assertEqual(
            status["log"], os.path.join(os.environ["AGENT_SESSIONS_STATE"], "agent-sessions.log")
        )
        self.assertRegex(status["last_quick"], SCAN_TIME)
        self.assertRegex(status["last_deep"], SCAN_TIME)
        unmounted = [m for m in status["mounts"] if not m["mounted"]]
        self.assertEqual(
            unmounted,
            [
                {
                    "path": str(self.gone),
                    "label": "Gone",
                    "fstype": "",
                    "device": "",
                    "mounted": False,
                    "letter": None,
                    "walk": None,
                }
            ],
        )
        self.assertEqual([s["present"] for s in status["stores"]].count(False), 1)
        self.assertEqual(
            [(s["provider"], s["reason"], s["mounted"]) for s in status["skipped_stores"]],
            [("claude", "Claude Desktop history", True)],
        )
        self.assertEqual(
            {row["letter"]: row["how"] for row in status["letters"]},
            {"C:": "system volume", "F:": "evidence"},
        )
        self.assertEqual(
            [
                (row["letter"], row["reason"], row["candidates"])
                for row in status["unresolved_letters"]
            ],
            [("Q:", "no evidence", [])],
        )
        tools = {row["tool"]: row for row in status["tools"]}
        self.assertEqual(
            {name: row["detail"] for name, row in tools.items()},
            {
                "agy": "installed",
                "claude": "installed",
                "codex": "installed",
                "copilot": "not found",
                "crush": "not installed (the command on PATH would install it)",
                "cursor-agent": "not found",
                "gemini": "not found",
                "grok": "installed",
                "opencode": "installed",
            },
        )
        self.assertEqual(
            status["no_reader"], [{"tool": "opencode", "path": tools["opencode"]["path"]}]
        )
        for provider in status["providers"]:
            tool = tools[provider["binary"]]
            self.assertTrue(tool["reader"])
            self.assertEqual(provider["tool"], tool["detail"])
            self.assertEqual(provider["installed"], tool["path"] if tool["installed"] else None)
        self.assertEqual(
            {p["name"]: p["resumable"] for p in status["providers"]},
            {"antigravity": True, "claude": True, "codex": True, "gemini": False, "grok": True},
        )

    # --- orderings ---------------------------------------------------------------

    def test_the_keys_of_every_object_are_in_alphabetical_order(self):
        for argv in (("list", "--json"), ("status", "--json"), ("scan", "--json")):
            self.json(*argv)
            objects = keys_in_order(self.raw)
            self.assertGreater(len(objects), 3)
            for keys in objects:
                self.assertEqual(keys, sorted(keys))
            self.assertTrue(self.raw.startswith(("[\n  {\n", "{\n  ")))  # two-space indent

    def test_the_order_of_the_listing(self):
        listing = self.json("list", "--json")
        keys = [row["key"] for row in listing]
        self.assertEqual(len(keys), len(set(keys)))
        dated = [row for row in listing if row["updated"] is not None]
        undated = [row for row in listing if row["updated"] is None]
        self.assertEqual(listing, dated + undated)  # no "updated": last
        self.assertEqual(
            [(row["updated"], row["key"]) for row in dated],
            sorted(
                sorted((row["updated"], row["key"]) for row in dated),
                key=lambda pair: pair[0],
                reverse=True,
            ),
        )
        self.assertEqual([row["key"] for row in undated], sorted(row["key"] for row in undated))
        # The order is the engine's, with a tie in it: two sessions updated at the same moment.
        self.assertEqual(keys, [s.key for s in api.list_sessions()])

    def test_the_order_of_every_array(self):
        status = self.json("status", "--json")
        mounted = [m["path"] for m in status["mounts"] if m["mounted"]]
        unmounted = [m["path"] for m in status["mounts"] if not m["mounted"]]
        self.assertEqual([m["path"] for m in status["mounts"]], sorted(mounted) + sorted(unmounted))
        self.assertEqual(mounted + unmounted, [m["path"] for m in status["mounts"]])

        def in_order(rows, key):
            values = [key(row) for row in rows]
            self.assertGreater(len(values), 0)
            self.assertEqual(values, sorted(values))

        in_order(status["providers"], lambda r: r["name"])
        in_order(status["stores"], lambda r: f"{r['provider']}:{r['root']}")
        in_order(status["skipped_stores"], lambda r: (r["provider"], r["root"]))
        in_order(status["letters"], lambda r: (r["source_mount"] or "", r["letter"]))
        in_order(status["unresolved_letters"], lambda r: (r["source_mount"], r["letter"]))
        in_order(status["tools"], lambda r: r["tool"])
        in_order(status["no_reader"], lambda r: r["tool"])
        for summary in (self.scan_deep, self.scan_quick):
            in_order(summary["mounts"], lambda r: r["path"])
            self.assertEqual(summary["warnings"], sorted(set(summary["warnings"])))
        self.assertEqual(status["warnings"], sorted(set(status["warnings"])))
        for stats in self.scan_deep["walk"].values():
            self.assertEqual(stats["error_paths"], sorted(stats["error_paths"]))

    def test_the_same_index_gives_the_same_bytes(self):
        first = run("list", "--json")
        self.assertEqual(run("list", "--json"), first)
        self.assertEqual(run("status", "--json"), run("status", "--json"))
        run("scan")
        run("scan", "--deep")
        self.assertEqual(run("list", "--json"), first)

    # --- the rules for every output ------------------------------------------------

    def test_what_a_terminal_would_act_on_is_escaped_and_the_data_is_exact(self):
        folder = "C:\\Users\\user\\evil\x1b]0;x\x07 \u202egnp.exe\u2028\x9b31m"
        claude_transcript(
            self.w.profile / ".claude",
            "dddddddd-1111-4222-8333-444444444444",
            folder,
            prompt="a title\x1b[2J with\nan escape",
            project="esc",
        )
        self.assertEqual(run("scan", "--json")[0], 0)
        code, out, _err = run("list", "--provider", "claude", "--json")
        self.assertEqual(code, 0)
        for bad in ("\x1b", "\x07", "\u202e", "\u2028", "\x9b"):
            self.assertNotIn(bad, out)
        self.assertIn("evil\\u001b]0;x\\u0007 \\u202egnp.exe\\u2028\\u009b31m", out)
        rows = {row["id"]: row for row in json.loads(out)}
        row = rows["dddddddd-1111-4222-8333-444444444444"]
        self.assertEqual(row["cwd"], folder)  # the parser gives the characters back
        self.assertEqual(row["title"], "a title [2J with an escape")  # cleaned when it was read
        self.assertIn("F:\\Proj\\My Site", {r["cwd"] for r in rows.values()})

    def test_a_path_that_is_not_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8")
        with contextlib.redirect_stdout(stream):
            cli._json({"files": [os.fsdecode(b"/store/caf\xe9.jsonl")], "title": "é"})
        stream.flush()
        self.assertEqual(
            raw.getvalue(),
            b'{\n  "files": [\n    "/store/caf\\udce9.jsonl"\n  ],\n  "title": "\xc3\xa9"\n}\n',
        )
        back = json.loads(raw.getvalue())
        self.assertEqual(os.fsencode(back["files"][0]), b"/store/caf\xe9.jsonl")

    def test_a_failure_prints_no_document(self):
        os.environ["AGENT_SESSIONS_MOUNTS"] = "{ not json"
        for argv in (("scan", "--json"), ("list", "--json"), ("status", "--json")):
            code, out, err = run(*argv)
            self.assertEqual((code, out), (1, ""), argv)
            self.assertTrue(err.startswith("agent-sessions: "))
            self.assertEqual(err.count("\n"), 1)


if __name__ == "__main__":
    unittest.main()
