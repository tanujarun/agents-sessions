"""argparse entry point; all subcommands."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import sys
from collections.abc import Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, TextIO

from . import __version__, api, fmt, logs, textsafe

if TYPE_CHECKING:
    from .index import Index
    from .launch import LaunchPlan
    from .model import Session

PROG = "agent-sessions"


HELP_WIDTH = 78  # every help text fits a terminal of 80 columns

# The commands as the top-level help groups them, each with its one-line
# summary. ``build_parser`` takes the summaries from here, and a test holds
# the table to the parser (every command in exactly one group).
GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "browse",
        (
            ("tui", "open the full-screen browser"),
            ("list", "list the sessions, newest first"),
            ("show", "print a session's details and its transcript"),
            ("status", "drives, stores, counts per provider, tools, warnings"),
        ),
    ),
    (
        "act on a session",
        (
            ("open", "resume a session in a new terminal window"),
            ("import", "copy a session of another drive into this system's store"),
            ("unimport", "remove the copy an import made"),
            ("continue", "start a new session that carries an old one on"),
            ("export", "write a transcript as Markdown or JSON lines"),
        ),
    ),
    (
        "drives",
        (
            ("mount", "mount a remembered drive, then scan it"),
            ("forget", "drop a drive that is not mounted from the index"),
            ("map", "print the drive letters, or set one by hand"),
        ),
    ),
    (
        "maintenance",
        (
            ("scan", "refresh the index"),
            ("verify", "compare the index with a walk of every drive"),
        ),
    ),
)

_DESCRIPTION = (
    "Find, list and resume saved AI coding-agent sessions (Claude Code, Codex, Grok, "
    "Antigravity, Gemini CLI) on this machine and on every mounted drive. With no command "
    "on a terminal, the full-screen browser starts."
)

_KEY_NOTE = (
    f"A session is named by its key (`{PROG} list --keys`), by a unique start\n"
    "of its key, or by its id."
)


class _HelpFormatter(argparse.HelpFormatter):
    """Help at a fixed width, narrower only when the terminal is. Text with
    line breaks of its own (the command groups, the examples) is kept as it
    is written; everything else is wrapped."""

    def __init__(self, prog: str, **kwargs: Any) -> None:
        # Whatever else argparse passes (it differs between Python versions)
        # goes through; the width is ours.
        columns = shutil.get_terminal_size().columns
        kwargs["width"] = max(min(HELP_WIDTH, columns - 2), 40)
        super().__init__(prog, **kwargs)

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        if "\n" not in text:
            return super()._fill_text(text, width, indent)
        return "\n".join(indent + line for line in text.splitlines())


def _groups_text() -> str:
    """The grouped command list of the top-level help."""
    width = max(len(name) for _title, commands in GROUPS for name, _summary in commands)
    lines = []
    for title, commands in GROUPS:
        lines.append(f"{title}:")
        lines += [f"  {name.ljust(width)}  {summary}" for name, summary in commands]
        lines.append("")
    lines.append(f"`{PROG} <command> --help` shows a command's options and examples.")
    lines.append(_KEY_NOTE)
    return "\n".join(lines)


def _examples(*lines: str) -> str:
    """An epilog: command lines as they are typed, each with what it does
    on the lines below it."""
    return "examples:\n" + "\n".join(f"  {line}" for line in lines)


def subcommands(parser: argparse.ArgumentParser | None = None) -> list[str]:
    """The names of the parser's subcommands, sorted."""
    parser = parser if parser is not None else build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        usage=f"{PROG} [-h] [--version] [--debug] [<command> ...]",
        description=_DESCRIPTION,
        epilog=_groups_text(),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--debug", action="store_true", help="log more, and to stderr as well as to the log file"
    )
    # The commands are listed by ``_groups_text``, not by argparse.
    sub = parser.add_subparsers(
        dest="command", metavar="<command>", prog=PROG, help=argparse.SUPPRESS
    )
    summaries = {name: summary for _title, commands in GROUPS for name, summary in commands}

    def command(
        name: str, description: str, examples: str | None = None
    ) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            help=summaries[name],
            description=description,
            epilog=examples,
            formatter_class=_HelpFormatter,
        )

    command(
        "tui",
        "Open the full-screen browser: the sessions of every provider in one list, with keys "
        "to resume, import, continue, view and mount. Press ? in it for the keys. This is "
        f"also what `{PROG}` with no command does on a terminal. It needs Textual.",
    )

    p = command(
        "scan",
        "Refresh the index: look at the known stores again and read the sessions that "
        "changed. A full walk of every drive (--deep) is made by itself when the set of "
        "mounted drives changed or the last one is older than a day.",
    )
    p.add_argument("--deep", action="store_true", help="walk every mount completely")
    p.add_argument("--json", action="store_true", help="JSON output")

    p = command(
        "list",
        "List the sessions from the index, newest first: age, where the session is (Linux, "
        "a drive letter or a drive's label), provider, title and project folder. Sessions of "
        "a drive that is not mounted are listed as offline.",
        _examples(
            f"{PROG} list",
            f"{PROG} list --provider claude --keys",
            "    the key of each session as a last column, for the other commands",
            f"{PROG} list --json",
            "    every field of every session (docs/JSON.md)",
        ),
    )
    p.add_argument(
        "--provider",
        metavar="P",
        help="only this provider: " + ", ".join(provider.name for provider in api.all_providers()),
        choices=[provider.name for provider in api.all_providers()],
    )
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--keys", action="store_true", help="add the session key as a last column")
    p.add_argument(
        "--mounted",
        action="store_true",
        help="leave out the sessions of drives that are not mounted",
    )

    key_help = "a session key (`list --keys`), a unique start of one, or a session id"
    p = command(
        "open",
        "Resume a session of this system's own store with its tool, in a new terminal "
        "window, in the session's folder. A session of another drive is not resumed where "
        "it is: import it first, or continue it as a new session. The tool is started only "
        "when it is installed.",
        _examples(
            f"{PROG} open claude:1a2b3c4d --dry-run",
            "    print the folder and the command, start nothing",
            f"{PROG} open claude:1a2b3c4d",
            f"{PROG} open claude:1a2b3c4d --here",
            "    in this terminal: the tool takes the place of this command",
        ),
    )
    p.add_argument("key", help=key_help)
    p.add_argument("--dry-run", action="store_true", help="print the command, do not run it")
    p.add_argument(
        "--here", action="store_true", help="resume in this terminal instead of a new window"
    )

    p = command(
        "import",
        "Copy a session from a store on another drive into this system's own store, so that "
        "its tool can resume it here. The source is never changed, nothing that exists is "
        "overwritten, and the copy is recorded so that `unimport` can take it back. Prints "
        "the key of the copy.",
        _examples(
            f"{PROG} import claude:1a2b3c4d --dry-run",
            "    what would be copied and rewritten; nothing is written",
            f"{PROG} import claude:1a2b3c4d --open",
            "    import, then resume the copy in a new window",
            f"{PROG} import codex:5e6f7a8b --to ~/Work/site",
            "    for a session whose own folder is gone or was not recorded",
            f"{PROG} import antigravity:9c0d1e2f --experimental",
            "    an import that no real resume has confirmed yet",
        ),
    )
    p.add_argument("key", help=key_help)
    p.add_argument(
        "--to",
        metavar="DIR",
        help=(
            "import into this folder instead of the one the session recorded "
            "(an absolute path, or one that starts with ~; the folder must exist)"
        ),
    )
    p.add_argument(
        "--experimental",
        action="store_true",
        help=(
            "allow an import that no resume has proven yet (Antigravity: a plain copy "
            "of the conversation; check after the first resume that its turns are there)"
        ),
    )
    p.add_argument("--open", action="store_true", help="resume after import")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be copied and rewritten, write nothing",
    )

    p = command(
        "unimport",
        "Take an import back: remove from this system's store what the last import of the "
        "session created, and nothing else. Refused when the copy was used since. "
        "Directories are removed only when they are empty, and a line the import added "
        "to a file of the tool's is left; both are reported.",
        _examples(
            f"{PROG} unimport claude:1a2b3c4d --dry-run",
            "    what would be removed and what would be left; nothing is removed",
            f"{PROG} unimport claude:1a2b3c4d",
            "    asks before it removes anything",
            f"{PROG} unimport claude:1a2b3c4d --force --yes",
            "    also when the copy was used since: its new turns are lost",
        ),
    )
    p.add_argument("key", help=key_help + ": the imported copy, or what it was imported from")
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "remove the copy even when it was used since the import, and what the tool "
            "made beside it; needed for an import recorded without a list of its files"
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print what would be removed, remove nothing"
    )
    p.add_argument("--yes", action="store_true", help="do not ask")

    p = command(
        "show",
        "Print what is known about a session (title, provider, where it is, folder, dates, "
        "message count, key), then its transcript as plain text: the user's and the "
        "assistant's turns, and one line per tool call.",
    )
    p.add_argument("key", help=key_help)

    p = command(
        "export",
        "Print the transcript of a session as a document: Markdown (a header, then a "
        "section per turn and a line per tool call) or JSON lines (one object per turn: "
        "role, text, ts). With -o it is written to a file that only you can read.",
        _examples(
            f"{PROG} export claude:1a2b3c4d | less",
            f"{PROG} export claude:1a2b3c4d -o ~/notes/session.md",
            "    refused when the file exists, unless --force is given",
            f"{PROG} export claude:1a2b3c4d --format json",
            "    one JSON object per turn (docs/JSON.md)",
        ),
    )
    p.add_argument("key", help=key_help)
    p.add_argument(
        "-o",
        dest="output",
        metavar="FILE",
        help="write to this file instead of stdout; a file that exists is not replaced",
    )
    p.add_argument(
        "--format",
        choices=api.EXPORT_FORMATS,
        default=api.EXPORT_FORMATS[0],
        help="md: Markdown (the default); json: JSON lines",
    )
    p.add_argument("--force", action="store_true", help="with -o: replace a file that exists")

    p = command(
        "continue",
        "Start a tool as a NEW session that continues an old one: for a session that "
        "cannot be resumed where it is wanted. The transcript is exported to a file in "
        "the state directory, and the tool is started with a fixed first message that "
        "names that file. The tool sends that message to its model as soon as it starts.",
        _examples(
            f"{PROG} continue gemini:1a2b3c4d --with claude --dry-run",
            "    the folder, the command and the transcript file; starts nothing",
            f"{PROG} continue codex:5e6f7a8b --in ~/Work/site",
            "    for a session whose own folder is gone",
            f"{PROG} continue claude:9c0d1e2f --with codex --here",
            "    in this terminal instead of a new window",
        ),
    )
    p.add_argument("key", help=key_help)
    p.add_argument(
        "--with",
        dest="tool",
        metavar="TOOL",
        help=(
            "the tool to start instead of the session's own: "
            + ", ".join(provider.name for provider in api.all_providers())
        ),
    )
    p.add_argument(
        "--in",
        dest="folder",
        metavar="DIR",
        help=(
            "start in this folder instead of the session's own "
            "(an absolute path, or one that starts with ~; the folder must exist)"
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the folder, the command and where the transcript would be written; "
            "write nothing and start nothing"
        ),
    )
    p.add_argument(
        "--here",
        action="store_true",
        help="start the tool in this terminal instead of a new window",
    )

    p = command(
        "status",
        "Print what the index knows: where the index and the log are, the drives and "
        "whether each is mounted, the stores and how many sessions each lists, the "
        "providers and whether their tools are installed, the drive letters, and the "
        "warnings of the last scan.",
    )
    p.add_argument("--json", action="store_true", help="JSON output")

    command(
        "verify",
        "Walk every mounted drive without the index, count the sessions on disk and "
        "compare them with what the index lists. Exit status 1 when they differ. Sessions "
        "of drives that are not mounted are left out.",
    )

    drive_help = "a remembered drive: its label, drive letter (C:), mount path or volume id"
    p = command(
        "mount",
        "Mount a drive whose sessions are listed as offline, then scan it. The mount is "
        "`udisksctl mount`; when it needs a password, the desktop asks for it.",
        _examples(
            f"{PROG} status",
            "    the remembered drives, with their letters and volume ids",
            f"{PROG} mount C:",
            f"{PROG} mount Zephyro",
        ),
    )
    p.add_argument("drive", help=drive_help)

    p = command(
        "forget",
        "Drop a remembered drive that is not mounted, and with it its sessions, from the "
        "index. Nothing on the drive is touched; a scan finds it again once it is mounted.",
    )
    p.add_argument("drive", help=drive_help)
    p.add_argument("--yes", action="store_true", help="do not ask")

    p = command(
        "map",
        "With no argument, print the drive letters and what each stands for. "
        "`map D: DIR` says by hand that D: is DIR; `map --remove D:` takes that back.",
        _examples(
            f"{PROG} map",
            f"{PROG} map D: '/run/media/me/Data'",
            "    written to ~/.config/agent-sessions/pathmap.json",
            f"{PROG} map --remove D:",
        ),
    )
    p.add_argument("letter", nargs="?", metavar="LETTER:", help="a drive letter, like D:")
    p.add_argument("dir", nargs="?", metavar="DIR", help="the directory the letter stands for")
    p.add_argument("--remove", metavar="LETTER:", help="remove the hand-made entry of a letter")
    return parser


# --- output -----------------------------------------------------------------
#
# Titles, folders, ids, keys, paths, mount labels, warnings and transcript
# text come from stores on other drives and may hold terminal escape
# sequences. Nothing below this block writes to a stream itself: readable text
# goes through ``_out`` or ``_err``, JSON through ``_json``, the progress line
# through ``_progress``, a question through ``_ask``, and each of those makes
# its text safe on the way out (``textsafe``). tests/test_textsafe.py holds
# this module to that.


def _emit(stream: TextIO, text: str) -> None:
    """Write one line that is already safe; a path with bytes that are not
    UTF-8 is escaped, not fatal."""
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding) + "\n")


def _out(text: object, multiline: bool = False) -> None:
    """Print one line. Control characters in it, a line break included,
    become spaces; ``multiline`` keeps line breaks and tabs, for transcript
    text."""
    _emit(sys.stdout, textsafe.clean(text, multiline))


def _err(text: object) -> None:
    """One line on stderr, after the program's name."""
    _emit(sys.stderr, f"{PROG}: {textsafe.clean(text)}")


def _json(value: object) -> None:
    """Print a value as JSON: exact data, with the characters a terminal
    would act on written as ``\\uXXXX`` escapes."""
    document = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    _emit(sys.stdout, textsafe.json_escape(document))


def _json_line(document: str) -> None:
    """Print one line of JSON text the engine made (``export --format
    json``): exact data, and like ``_json`` with nothing in it a terminal
    would act on."""
    _emit(sys.stdout, textsafe.json_escape(document))


def _progress(text: str = "") -> None:
    """Replace the progress line on stderr; no text clears it."""
    sys.stderr.write("\r\033[K" + textsafe.clean(text))
    sys.stderr.flush()


def _ask(question: str) -> bool:
    """Ask a yes/no question and read the answer from stdin; only ``y`` or
    ``yes`` is a yes. The question goes to stderr, so stdout stays what the
    command prints."""
    sys.stderr.write(textsafe.clean(question) + " [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _table(rows: Iterable[Iterable[object]], indent: str = "") -> None:
    """Print rows of cells in aligned columns."""
    table = [[str(cell) for cell in row] for row in rows]
    if not table:
        return
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        _out((indent + "  ".join(cells)).rstrip())


def _count(n: int, word: str, plural: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


# How a listing words a byte count, an age and a project's name: the one
# copy both front ends use (``fmt``).
_size = fmt.size
_age = fmt.age
_project = fmt.project


def _walk_rows(mounts: list[dict], walks: dict | None) -> list[list[str]]:
    rows = []
    for mount in mounts:
        walk = walks.get(mount["path"]) if walks else None
        if not walk:
            continue
        rows.append(
            [
                mount["label"],
                mount["path"],
                _count(walk["dirs"], "dir"),
                f"{walk['pruned']:,} pruned",
                f"{walk['errors']:,} unreadable",
                f"{walk['seconds']:.2f} s",
            ]
        )
    return rows


# --- commands ---------------------------------------------------------------


def _scan(args: argparse.Namespace) -> int:
    progress = None
    if sys.stderr.isatty() and not args.json:

        def progress(label: str, dirs: int) -> None:
            _progress(f"scanning {label}… {dirs:,} dirs")

    summary = api.scan(deep=True if args.deep else None, progress=progress)
    if progress is not None and summary["walk"] is not None:
        _progress()
    if args.json:
        _json(summary)
        return 0
    _print_scan(summary)
    return 0


def _print_scan(summary: dict) -> None:
    """A scan summary for a person."""
    _out(
        f"scan: {summary['mode']}, {_count(len(summary['mounts']), 'mount')}, "
        f"{summary['seconds']:.2f} s"
    )
    _table(_walk_rows(summary["mounts"], summary["walk"]), "  ")
    extra = []
    if summary["stores_not_mounted"]:
        extra.append(f"{summary['stores_not_mounted']} more on drives not mounted")
    if summary["skipped_stores"]:
        extra.append(f"{summary['skipped_stores']} skipped")
    _out(f"stores: {summary['stores']}" + (f" ({', '.join(extra)})" if extra else ""))
    _table(
        [
            [
                name,
                _count(row["stores"], "store"),
                _count(row["sessions"], "session"),
                f"{row['offline']:,} offline" if row["offline"] else "",
            ]
            for name, row in sorted(summary["by_provider"].items())
        ],
        "  ",
    )
    offline = summary["offline"]
    _out(
        f"sessions: {summary['sessions'] - offline:,} listed of "
        f"{_count(summary['candidates'], 'candidate')} "
        f"({summary['parsed']:,} parsed, {summary['cached']:,} from cache)"
        + (f"; {offline:,} more offline, on drives not mounted" if offline else "")
    )
    if summary["warnings"]:
        _out(f"warnings: {len(summary['warnings'])} (see `{PROG} status`)")


def _list(args: argparse.Namespace) -> int:
    index = api.load_index(offline=not args.mounted)
    if index.load_error is not None:
        # No cache, or one that cannot be used: an empty listing would look
        # like "no sessions". stdout stays the listing alone.
        _err("no usable cache; scanning…")
        api.scan()
        index = api.load_index(offline=not args.mounted)
    sessions = api.list_sessions(args.provider, index=index)
    if args.json:
        _json([s.to_dict() for s in sessions])
        return 0
    if index.last_quick is None:
        _err(f"no index yet; run `{PROG} scan`")
    _table(
        [
            [
                _age(s.updated or s.started),
                index.where(s),
                s.provider,
                s.title or "-",
                _project(s.cwd),
            ]
            + ([s.key] if args.keys else [])
            for s in sessions
        ]
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    status = api.status()
    if args.json:
        _json(status)
        return 0
    _out(f"index: {status['cache']}")
    _out(f"log: {status['log']}")
    _out(
        f"last scan: {status['last_quick'] or 'never'}; "
        f"last deep scan: {status['last_deep'] or 'never'}"
    )

    _out("")
    _out("mounts")
    rows = []
    for mount in status["mounts"]:
        walk = mount["walk"]
        if not mount["mounted"]:
            detail = ""
        elif walk:
            detail = (
                f"{_count(walk['dirs'], 'dir')}, {walk['pruned']:,} pruned, "
                f"{walk['errors']:,} unreadable, {walk['seconds']:.2f} s"
            )
        else:
            detail = "not walked yet"
        rows.append(
            [
                mount["letter"] or "",
                mount["label"],
                mount["path"],
                mount["fstype"],
                mount["uuid"],
                _STATES[mount["state"]] if mount["mounted"] or mount["uuid"] else _NO_VOLUME_ID,
                detail,
            ]
        )
    _table(rows, "  ")
    for mount in status["mounts"]:
        for path in (mount["walk"] or {}).get("error_paths", []):
            _out(f"  unreadable: {path}")

    _out("")
    _out("stores")
    rows = []
    for store in status["stores"]:
        flags = []
        if not store["present"]:
            flags.append("not mounted")
        if not store["reader"]:
            flags.append("reader not implemented")
        if store["duplicates"]:
            flags.append(f"{store['duplicates']} duplicate")
        rows.append(
            [
                store["provider"],
                store["origin"],
                store["root"],
                f"{store['listed']} listed / {store['candidates']} candidates / "
                f"{store['skipped']} skipped",
                ", ".join(flags),
            ]
        )
    _table(rows, "  ")
    if not rows:
        _out("  none")

    _out("")
    _out("providers")
    _table(
        [
            [
                p["name"],
                _count(p["stores"], "store"),
                _count(p["sessions"], "session")
                + (f" ({p['offline']:,} offline)" if p["offline"] else ""),
                (p["installed"] or f"{p['binary']} {p['tool']}")
                + ("" if p["resumable"] else " (not resumable)"),
            ]
            for p in status["providers"]
        ],
        "  ",
    )

    if status["skipped_stores"]:
        _out("")
        _out("skipped stores")
        _table(
            [
                [s["provider"], s["reason"] + ("" if s["mounted"] else ", not mounted"), s["root"]]
                for s in status["skipped_stores"]
            ],
            "  ",
        )

    _out("")
    _out("drive letters")
    _print_letters(status)

    _out("")
    _out("tools")
    _table(
        [
            [
                t["tool"],
                t["detail"],
                (t["path"] or "") + (" (wrapper)" if t["wrapper"] else ""),
            ]
            for t in status["tools"]
        ],
        "  ",
    )

    if status["no_reader"]:
        _out("")
        _out("installed tools with no reader")
        _table([[t["tool"], t["path"]] for t in status["no_reader"]], "  ")

    _out("")
    _out(f"warnings: {len(status['warnings'])}")
    for warning in status["warnings"]:
        _out(f"  {warning}")
    return 0


# How a volume's state reads (``api.volumes``).
_STATES = {"mounted": "mounted", "attached": "attached, not mounted", "absent": "not attached"}
# For a volume that is not mounted and whose id no scan has recorded: whether
# its device is attached cannot be looked up.
_NO_VOLUME_ID = "not mounted"


def _print_letters(status: dict) -> None:
    """The drive letters of a status report: the resolved ones and how, the
    letters remembered for drives that are not mounted, the unresolved."""
    rows = [
        [
            row["letter"],
            "->",
            row["mount"],
            f"{row['how']}, {_count(row['paths'], 'path')}"
            + (f", recorded on {row['source_mount']}" if row["source_mount"] else ""),
        ]
        for row in status["letters"]
    ]
    rows += [
        [volume["letter"], "->", volume["path"], "as last resolved; the drive is not mounted"]
        for volume in status["volumes"]
        if volume["letter"] and volume["state"] != "mounted"
    ]
    rows += [
        [
            row["letter"],
            "->",
            "unresolved",
            f"{row['reason']}, {_count(row['paths'], 'path')}, "
            f"recorded on {row['source_mount']}"
            + (f", candidates: {', '.join(row['candidates'])}" if row["candidates"] else ""),
        ]
        for row in status["unresolved_letters"]
    ]
    _table(rows, "  ")
    if not rows:
        _out("  none recorded")


def _mount(args: argparse.Namespace) -> int:
    volume = api.find_volume(args.drive)
    if volume["state"] != "mounted":
        _err(f"mounting {volume['label']}… (the desktop may ask for your password)")
    progress = None
    if sys.stderr.isatty():

        def progress(label: str, dirs: int) -> None:
            _progress(f"scanning {label}… {dirs:,} dirs")

    result = api.mount_drive(args.drive, progress=progress)
    if progress is not None and result["scan"]["walk"] is not None:
        _progress()
    _out(f"mounted at {result['mount_point']}")
    _print_scan(result["scan"])
    return 0


def _forget(args: argparse.Namespace) -> int:
    volume = api.find_volume(args.drive)
    what = (
        f"{volume['label']} ({volume['path']}): {_count(volume['stores'], 'store')}, "
        f"{_count(volume['sessions'], 'session')}"
    )
    if not args.yes and volume["state"] != "mounted":  # a mounted one is refused below
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            _err(f"forget {what}: not a terminal to ask on; pass --yes")
            return 1
        if not _ask(f"Forget {what}?"):
            _err("nothing was forgotten")
            return 1
    done = api.forget_drive(volume["path"])
    _out(
        f"forgot {done['label']} ({done['path']}): {_count(done['stores'], 'store')}, "
        f"{_count(done['sessions'], 'session')}"
    )
    return 0


def _map(args: argparse.Namespace) -> int:
    if args.remove is not None:
        if args.letter is not None:
            _err("map: --remove takes the letter alone")
            return 2
        letter = api.remove_letter(args.remove)
        _out(f"{letter} is no longer set by hand")
        return 0
    if args.letter is None:
        _print_letters(api.status())
        return 0
    if args.dir is None:
        _err(f"map: say which directory {args.letter} stands for, or use --remove")
        return 2
    # ``~`` is expanded here, as for every folder this command line takes.
    letter, directory = api.set_letter(args.letter, os.path.expanduser(args.dir))
    _out(f"{letter} -> {directory}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    progress = None
    if sys.stderr.isatty():

        def progress(label: str, dirs: int) -> None:
            _progress(f"walking {label}… {dirs:,} dirs")

    result = api.verify(progress=progress)
    if progress is not None:
        _progress()
    dirs = sum(walk["dirs"] for walk in result["walk"].values())
    _out(
        f"verify: walked {_count(len(result['walk']), 'mount')}, {_count(dirs, 'dir')}, "
        f"{result['seconds']:.2f} s; {_count(result['stores'], 'store')}, "
        f"{_count(result['sessions'], 'session')} on disk"
    )
    for line in result["nested"]:
        _out(f"note: store inside another store, not listed by design: {line}")
    for line in result["stale"]:
        _out(f"stale: {line}")
    if result["stale"]:
        _out(
            f"{_count(len(result['stale']), 'candidate')} changed on disk after the last "
            f"scan; run `{PROG} scan`"
        )
    for line in result["differences"]:
        _out(f"DIFFERENT: {line}")
    if result["differences"]:
        _out(f"verify: {_count(len(result['differences']), 'difference')}")
        return 1
    _out("verify: the index matches the disks")
    return 0


# Said below a printed command whose text is not the command: a folder's name
# can hold characters that are not shown (they are printed as spaces).
_NOT_AS_SHOWN = (
    "note: the folder's name holds characters that cannot be shown; they are printed as "
    "spaces, so the line above is not exactly what runs"
)


def _as_shown(*texts: str) -> bool:
    """Whether every text is printed as it is: nothing in it is replaced on
    the way out."""
    return all(textsafe.clean(text) == text for text in texts)


def _print_plan(plan: LaunchPlan) -> None:
    """A launch plan as a line a shell could run, quoted for one
    (``shlex``), and its note. When the line cannot be printed as it is,
    that is said too."""
    line = f"cd {shlex.quote(plan.cwd)} && {shlex.join(plan.command)}"
    _out(line)
    if not _as_shown(line):
        _out(_NOT_AS_SHOWN)
    if plan.note:
        _out(f"note: {plan.note}")


def _resume(ref: Session | str, here: bool, dry_run: bool, index: Index | None = None) -> int:
    if here and not dry_run:
        # exec replaces this process: say what runs before it does.
        try:
            _print_plan(api.open_session(ref, here=True, dry_run=True, index=index))
            sys.stdout.flush()
        except api.LaunchFailed:
            pass  # the real call below fails the same way, and notifies
    plan = api.open_session(ref, here=here, dry_run=dry_run, index=index)
    if not here or dry_run:
        _print_plan(plan)
    return 0


def _open(args: argparse.Namespace) -> int:
    return _resume(args.key, args.here, args.dry_run)


def _folder_line(plan: dict) -> str:
    """The line of a dry run that says which folder the copy is made for,
    and that it was chosen when it was."""
    recorded = plan["cwd"] or "not recorded"
    chosen = plan["chosen_folder"]
    if plan["cwd_local"] is None:  # an import that uses no folder, and found none
        return f"project folder: {recorded} ({'not found; ' if plan['cwd'] else ''}none is needed)"
    if chosen is None:
        return f"project folder: {recorded} -> {plan['cwd_local']}"
    line = f"project folder: {recorded} -> {plan['cwd_local']} (chosen with --to"
    if plan["cwd_local"] != chosen:  # the folder's real path is what is used
        line += f" {chosen}"
    return line + ")"


# What ``import`` adds to the engine's reason when the session's own folder is
# not there to import into (``importer.NoFolder``): the way out is this
# command's flag, which another front end does not have.
_TO_HINT = "use --to DIR to import it into a folder you choose"

# The same for every other refusal that asking in another way gets past
# (``ApiError.remedy``): the engine's reason names no flag, since the TUI
# shows it too; what this command line calls the way out is added here.
_REMEDIES = {
    api.REMEDY_KEYS: "; `list --keys` shows the keys",
    api.REMEDY_FOLDER: " (--in DIR)",
    api.REMEDY_FORCE: " (--force)",
    api.REMEDY_OVERWRITE: " (--force replaces it)",
    api.REMEDY_EXPERIMENTAL: "; --experimental imports it all the same",
}


def _reason(exc: api.ApiError) -> str:
    """An engine refusal as this command line words it: the reason, and the
    flag that gets past it when there is one."""
    return exc.reason + _REMEDIES.get(exc.remedy, "")


def _import(args: argparse.Namespace) -> int:
    try:
        return _import_into(args)
    except api.NoFolder as exc:
        _err(f"{exc.reason}; {_TO_HINT}")
        return 1


def _import_into(args: argparse.Namespace) -> int:
    # ``~`` is expanded here: the engine takes an absolute path and nothing else.
    target = os.path.expanduser(args.to) if args.to is not None else None
    index = api.load_index()
    if args.experimental:
        # Said before anything is copied, and in a dry run too. Only when
        # the flag is what makes this import possible.
        options = api.import_options(args.key, index=index, target=target)
        if options.experimental:
            _err(options.warning)
    if args.dry_run:
        plan = api.import_plan(args.key, index=index, target=target, experimental=args.experimental)
        if plan["already"]:
            _out(f"already imported: {plan['already']}")
            _out("dry run: nothing was written")
            return 0
        _out(f"import {plan['source_key']} -> {plan['native_key']}")
        _out(_folder_line(plan))
        for copy in plan["copies"]:
            size = _size(copy["bytes"])
            if copy["dir"]:
                _out(
                    f"copy {copy['from']}{os.sep} -> {copy['to']}{os.sep} "
                    f"({_count(copy['files'], 'file')}, {size})"
                )
            else:
                _out(f"copy {copy['from']} -> {copy['to']} ({size})")
        for rewrite in plan["rewrites"]:
            _out(f"rewrite {rewrite['file']}: {rewrite['what']}")
            for change in rewrite["changes"] or ["nothing to change"]:
                _out(f"  {change}")
        for append in plan["appends"]:
            _out(f"append to {append['file']}: {append['what']}")
        _out(
            f"free space on {plan['free_on']}: {_size(plan['free'])}; "
            f"{_size(plan['needed'])} needed"
        )
        _out("dry run: nothing was written")
        return 0
    progress = None
    if sys.stderr.isatty():

        def progress(done: int, total: int) -> None:
            share = 100 * done // total if total else 100
            _progress(f"copying… {share}% of {_size(total)}")

    try:
        report = api.import_report(
            args.key,
            progress=progress,
            index=index,
            target=target,
            experimental=args.experimental,
        )
    finally:
        if progress is not None:
            _progress()
    if report.created:
        _err(
            f"imported {_count(len(report.files), 'item')} "
            f"({_size(report.bytes)}) into {report.session.store}"
        )
    else:
        _err("already imported; nothing was copied")
    _out(report.session.key)
    if args.open:
        return _resume(report.session, False, False)
    return 0


def _print_undo(report: api.UnimportReport) -> None:
    """What an undo removed and left, or with a dry run would: a line each."""
    removed, left = ("would remove", "would leave") if report.dry_run else ("removed", "left")
    for text in report.differences:
        _out(f"differs from the import record: {text}")
    for path in report.removed:
        _out(f"{removed} {path}")
    for path, why in report.left:
        flag = ": --force" if why == api.LEFT_FOR_A_FORCED_UNDO else ""
        _out(f"{left} {path} ({why}{flag})")


def _unimport(args: argparse.Namespace) -> int:
    # Planned first, without touching anything: every refusal comes from
    # here, before a question is asked.
    planned = api.unimport(args.key, force=args.force, dry_run=True)
    name = planned.native_key or f"{planned.provider}:{planned.id}"
    what = (
        f"{_count(len(planned.removed), 'file or directory', 'files and directories')} "
        f"({_size(planned.bytes)})"
    )
    if args.dry_run:
        _print_undo(planned)
        _out("dry run: nothing was removed")
        return 0
    if not args.yes:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            _err(f"unimport {name}: not a terminal to ask on; pass --yes")
            return 1
        used = " It was used since the import." if planned.differences else ""
        if not _ask(f"Remove the imported copy {name}, {what}?{used}"):
            _err("nothing was removed")
            return 1
    report = api.unimport(args.key, force=args.force)
    _print_undo(report)
    _err(
        f"removed the imported copy {name}: "
        f"{_count(len(report.removed), 'file or directory', 'files and directories')}"
        + (f"; {len(report.left)} left in place" if report.left else "")
    )
    return 0


def _show(args: argparse.Namespace) -> int:
    index = api.load_index()
    session = api.find_session(args.key, index=index)
    label = next(
        (p.label for p in api.all_providers() if p.name == session.provider), session.provider
    )
    rows: list[list[object]] = [
        ["title", session.title or "-"],
        ["provider", label],
        ["where", index.where(session)],
        ["cwd", session.cwd or "-"],
    ]
    if session.cwd_local and session.cwd_local != session.cwd:
        rows.append(["local", session.cwd_local])
    rows += [
        ["started", session.started or "-"],
        ["updated", session.updated or "-"],
        ["messages", "-" if session.messages is None else session.messages],
        ["key", session.key],
    ]
    turns = api.transcript(session)  # refused before anything is printed, when it is
    _table(rows)
    last = None
    for turn in turns:
        if turn.role == "tool":
            if last != "tool":
                _out("")
            _out(f"[tool: {turn.text}]")
        else:
            _out("")
            _out(f"{turn.role}:")
            _out(turn.text, multiline=True)
        last = turn.role
    return 0


def _export(args: argparse.Namespace) -> int:
    if args.output is not None:
        # ``~`` and a relative path are this command's to resolve: the engine
        # takes an absolute path and nothing else.
        path = os.path.abspath(os.path.expanduser(args.output))
        size = api.export_file(args.key, path, fmt=args.format, force=args.force)
        _err(f"wrote {path} ({_size(size)})")
        return 0
    # Refused before anything is printed, when it is; then piece by piece.
    pieces = api.export_transcript(args.key, fmt=args.format)
    for piece in pieces:
        if args.format == "json":
            _json_line(piece)
        else:
            _out(piece, multiline=True)
    return 0


# The last line of ``continue --dry-run``: what a real run does beyond
# opening a window.
_FIRST_MESSAGE = "starting it sends a first message to the model"


def _continue(args: argparse.Namespace) -> int:
    # ``~`` is expanded here: the engine takes an absolute path and nothing else.
    folder = os.path.expanduser(args.folder) if args.folder is not None else None
    if args.dry_run:
        plan = api.continue_session(
            args.key, tool=args.tool, folder=folder, here=args.here, dry_run=True
        )
        command = shlex.join(plan.command)
        _out(f"folder: {plan.cwd}")
        _out(f"command: {command}")
        if not _as_shown(plan.cwd, command):
            _out(_NOT_AS_SHOWN)
        _out(f"transcript: {plan.handoff}")
        _out(_FIRST_MESSAGE)
        return 0
    if args.here:
        # exec replaces this process: say what runs before it does.
        try:
            _print_plan(
                api.continue_session(
                    args.key, tool=args.tool, folder=folder, here=True, dry_run=True
                )
            )
            sys.stdout.flush()
        except api.ApiError:
            pass  # the real call below fails the same way, and says why
    plan = api.continue_session(args.key, tool=args.tool, folder=folder, here=args.here)
    if not args.here:
        _print_plan(plan)
        _err(f"the transcript was written to {plan.handoff}")
    return 0


def _tui(args: argparse.Namespace | None = None) -> int:
    """Start the TUI and return its exit status (1, with one line that says
    how to get Textual, when that is missing).

    The one place outside ``tui/`` that imports it, and only when the TUI is
    what was asked for: every other command runs without Textual installed.
    """
    from . import tui

    return tui.run()


def _on_a_terminal() -> bool:
    """Whether a person is at both ends: the TUI needs to read keys and to
    draw."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):  # a stream that was closed or replaced
        return False


def _quiet_stdout() -> None:
    """After a broken pipe: point stdout at nothing, so the flush at exit
    does not raise again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


_COMMANDS = {
    "tui": _tui,
    "scan": _scan,
    "list": _list,
    "status": _status,
    "verify": _verify,
    "open": _open,
    "import": _import,
    "unimport": _unimport,
    "show": _show,
    "export": _export,
    "continue": _continue,
    "mount": _mount,
    "forget": _forget,
    "map": _map,
}


INTERRUPTED = 130  # exit status after Ctrl-C: 128 + SIGINT, as a shell reports it
_TERMINATING = ("SIGTERM", "SIGHUP")  # ended like Ctrl-C: cleanly, with 128 + the signal


class _Signalled(BaseException):
    """SIGTERM or SIGHUP arrived while a command ran. Not an ``Exception``:
    nothing that catches errors to carry on may swallow it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextlib.contextmanager
def _signals() -> Iterator[None]:
    """For the length of a command, turn SIGTERM and SIGHUP into an
    exception raised in the main thread, the way Ctrl-C is one already.

    Left alone, either ends the process on the spot and no ``finally`` runs:
    an import would leave its staging directory and whatever it had already
    put in place. Only the first signal is raised; one that arrives while
    the command is unwinding is ignored, so a roll back is not cut short.
    The handlers that were there come back when the command is over.
    """

    def raise_it(signum: int, frame: object) -> None:
        if not seen:
            seen.append(signum)
            raise _Signalled(signum)

    seen: list[int] = []
    before = {}
    for name in _TERMINATING:
        signum = getattr(signal, name, None)  # SIGHUP is not on every platform
        if signum is None:
            continue
        try:
            before[signum] = signal.signal(signum, raise_it)
        except (ValueError, OSError):
            pass  # not the main thread, or a signal this platform does not let us handle
    try:
        yield
    finally:
        for signum, handler in before.items():
            signal.signal(signum, handler)


def _stopped(text: str) -> None:
    """The one line after an interrupt, on a line of its own."""
    if sys.stderr.isatty():
        _progress()  # whatever progress line was showing
    _err(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; the exit status is 0, 1 (failure), 2 (usage) or,
    after an interrupt, 130 (Ctrl-C) or 128 + the signal (SIGTERM, SIGHUP)."""
    command = None
    debugging = False
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if args.debug:
            logs.debug()
            debugging = True
        if args.command is None and not _on_a_terminal():
            parser.print_help()
            return 2
        command = args.command or "tui"  # no command on a terminal: the TUI
        if command == "tui":
            # Not inside ``_signals``: the app has the terminal, and what a
            # signal does to it stays what it is under the launcher script.
            return _tui(args)
        with _signals():
            code = _COMMANDS[command](args)
            sys.stdout.flush()
        return code
    except BrokenPipeError:
        _quiet_stdout()  # the reader (head, less) went away: not an error
        return 0
    except api.ApiError as exc:
        _err(_reason(exc))
        return 1
    except api.MountsError as exc:
        _err(exc)
        return 1
    except KeyboardInterrupt:
        _stopped("interrupted")
        return INTERRUPTED
    except _Signalled as exc:
        _stopped(f"terminated ({signal.Signals(exc.signum).name})")
        return 128 + exc.signum
    except Exception as exc:
        # Not a failure anyone planned for. The traceback goes to the log
        # (and, with --debug, to stderr through the log's copy there).
        logs.unexpected(exc, f"`{PROG} {command}`" if command else PROG)
        where = logs.written()
        details = f" (details in {where})" if where is not None else ""
        _err(f"internal error: {type(exc).__name__}: {exc}{details}")
        return 1
    finally:
        if debugging:
            logs.debug(False)  # for this command only, should the process run another
