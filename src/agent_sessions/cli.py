"""argparse entry point; all subcommands."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TextIO

from . import __version__, api, logs, textsafe
from .mounts import MountsError
from .providers import all_providers

if TYPE_CHECKING:
    from .index import Index
    from .launch import LaunchPlan
    from .model import Session

PROG = "agent-sessions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Find, list and resume saved AI coding-agent sessions.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--debug", action="store_true", help="log more, and to stderr as well as to the log file"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("tui", help="open the TUI")

    p = sub.add_parser("scan", help="refresh index; print summary")
    p.add_argument("--deep", action="store_true", help="walk every mount completely")
    p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("list", help="sessions, newest first")
    p.add_argument(
        "--provider",
        metavar="P",
        help="only this provider",
        choices=[provider.name for provider in all_providers()],
    )
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--keys", action="store_true", help="add the session key as a last column")
    p.add_argument(
        "--mounted",
        action="store_true",
        help="leave out the sessions of drives that are not mounted",
    )

    key_help = "a session key (`list --keys`), a unique start of one, or a session id"
    p = sub.add_parser("open", help="native: resume. foreign: import first")
    p.add_argument("key", help=key_help)
    p.add_argument("--dry-run", action="store_true", help="print the command, do not run it")
    p.add_argument(
        "--here", action="store_true", help="resume in this terminal instead of a new window"
    )

    p = sub.add_parser("import", help="import; optionally resume after")
    p.add_argument("key", help=key_help)
    p.add_argument("--open", action="store_true", help="resume after import")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be copied and rewritten, write nothing",
    )

    p = sub.add_parser("show", help="plain-text transcript to stdout")
    p.add_argument("key", help=key_help)

    p = sub.add_parser("status", help="mounts, stores, counts per provider, warnings")
    p.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("verify", help="compare index against a brute-force walk")

    drive_help = "a remembered drive: its label, drive letter (C:), mount path or volume id"
    p = sub.add_parser(
        "mount",
        help="mount a remembered drive, then scan",
        description=(
            "Mount a drive whose sessions are listed as offline, then scan it. The mount is "
            "`udisksctl mount`; when it needs a password, the desktop asks for it."
        ),
    )
    p.add_argument("drive", help=drive_help)

    p = sub.add_parser(
        "forget",
        help="drop a drive that is not mounted from the index",
        description=(
            "Drop a remembered drive that is not mounted, and with it its sessions, from the "
            "index. Nothing on the drive is touched; a scan finds it again once it is mounted."
        ),
    )
    p.add_argument("drive", help=drive_help)
    p.add_argument("--yes", action="store_true", help="do not ask")

    p = sub.add_parser(
        "map",
        help="drive letters: print them, or set one by hand",
        description=(
            "With no argument, print the drive letters and what each stands for. "
            "`map D: DIR` says by hand that D: is DIR; `map --remove D:` takes that back."
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


def _size(count: int) -> str:
    """A byte count in decimal units: ``812 B``, ``116.7 kB``, ``123.7 MB``."""
    size = float(count)
    for unit in ("B", "kB", "MB"):
        if size < 1000:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"


def _age(stamp: str | None, now: datetime) -> str:
    if not stamp:
        return "-"
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    seconds = max((now - then).total_seconds(), 0)
    days = int(seconds // 86400)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if days < 1:
        return f"{int(seconds // 3600)}h"
    if days < 14:
        return f"{days}d"
    if days < 60:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _project(cwd: str | None) -> str:
    """The folder a session ran in: the last segment of its recorded cwd."""
    parts = [p for p in re.split(r"[\\/]+", cwd or "") if p]
    return parts[-1] if parts else "-"


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
    now = datetime.now(UTC)
    _table(
        [
            [
                _age(s.updated or s.started, now),
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
    letter, directory = api.set_letter(args.letter, args.dir)
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


def _print_plan(plan: LaunchPlan) -> None:
    """A launch plan as a line a shell could run, and its note."""
    _out(f"cd {shlex.quote(plan.cwd)} && {shlex.join(plan.command)}")
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


def _import(args: argparse.Namespace) -> int:
    index = api.load_index()
    if args.dry_run:
        plan = api.import_plan(args.key, index=index)
        if plan["already"]:
            _out(f"already imported: {plan['already']}")
            _out("dry run: nothing was written")
            return 0
        _out(f"import {plan['source_key']} -> {plan['native_key']}")
        _out(f"project folder: {plan['cwd']} -> {plan['cwd_local']}")
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
        report = api.import_report(args.key, progress=progress, index=index)
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


def _show(args: argparse.Namespace) -> int:
    index = api.load_index()
    session = api.find_session(args.key, index=index)
    label = next((p.label for p in all_providers() if p.name == session.provider), session.provider)
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


def _not_implemented(args: argparse.Namespace) -> int:
    _err(f"{args.command}: not implemented")
    return 1


def _quiet_stdout() -> None:
    """After a broken pipe: point stdout at nothing, so the flush at exit
    does not raise again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


_COMMANDS = {
    "scan": _scan,
    "list": _list,
    "status": _status,
    "verify": _verify,
    "open": _open,
    "import": _import,
    "show": _show,
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
        if args.command is None:
            parser.print_help()
            return 2
        command = args.command
        if args.debug:
            logs.debug()
            debugging = True
        with _signals():
            code = _COMMANDS.get(args.command, _not_implemented)(args)
            sys.stdout.flush()
        return code
    except BrokenPipeError:
        _quiet_stdout()  # the reader (head, less) went away: not an error
        return 0
    except api.ApiError as exc:
        _err(exc.reason)
        return 1
    except MountsError as exc:
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
