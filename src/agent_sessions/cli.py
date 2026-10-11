"""argparse entry point; all subcommands."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from . import __version__, api
from .mounts import MountsError
from .providers import all_providers

PROG = "agent-sessions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Find, list and resume saved AI coding-agent sessions.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("tui", help="open the TUI")

    p = sub.add_parser("scan", help="refresh index; print summary")
    p.add_argument("--deep", action="store_true", help="walk every mount completely")
    p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("list", help="sessions, newest first")
    p.add_argument("--provider", metavar="P", help="only this provider",
                   choices=[provider.name for provider in all_providers()])
    p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("open", help="native: resume. foreign: import first")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true", help="print the command, do not run it")

    p = sub.add_parser("import", help="import; optionally resume after")
    p.add_argument("key")
    p.add_argument("--open", action="store_true", help="resume after import")

    p = sub.add_parser("show", help="plain-text transcript to stdout")
    p.add_argument("key")

    p = sub.add_parser("status", help="mounts, stores, counts per provider, warnings")
    p.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("verify", help="compare index against a brute-force walk")
    return parser


# --- output -----------------------------------------------------------------

def _out(text: str) -> None:
    """Print; a path with bytes that are not UTF-8 is escaped, not fatal."""
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(encoding, "backslashreplace").decode(encoding) + "\n")


def _json(value) -> None:
    _out(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _table(rows, indent: str = "") -> None:
    """Print rows of cells in aligned columns."""
    rows = [[str(cell) for cell in row] for row in rows]
    if not rows:
        return
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths)]
        _out((indent + "  ".join(cells)).rstrip())


def _count(n: int, word: str, plural: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (plural or word + 's')}"


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


def _walk_rows(mounts, walks) -> list[list[str]]:
    rows = []
    for mount in mounts:
        walk = walks.get(mount["path"]) if walks else None
        if not walk:
            continue
        rows.append([mount["label"], mount["path"], _count(walk["dirs"], "dir"),
                     f"{walk['pruned']:,} pruned", f"{walk['errors']:,} unreadable",
                     f"{walk['seconds']:.2f} s"])
    return rows


# --- commands ---------------------------------------------------------------

def _scan(args) -> int:
    progress = None
    if sys.stderr.isatty() and not args.json:
        def progress(label, dirs):
            sys.stderr.write(f"\r\033[Kscanning {label}… {dirs:,} dirs")
            sys.stderr.flush()
    summary = api.scan(deep=True if args.deep else None, progress=progress)
    if progress is not None and summary["walk"] is not None:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    if args.json:
        _json(summary)
        return 0
    _out(f"scan: {summary['mode']}, {_count(len(summary['mounts']), 'mount')}, "
         f"{summary['seconds']:.2f} s")
    _table(_walk_rows(summary["mounts"], summary["walk"]), "  ")
    extra = []
    if summary["stores_not_mounted"]:
        extra.append(f"{summary['stores_not_mounted']} more on drives not mounted")
    if summary["skipped_stores"]:
        extra.append(f"{summary['skipped_stores']} skipped")
    _out(f"stores: {summary['stores']}" + (f" ({', '.join(extra)})" if extra else ""))
    _table([[name, _count(row["stores"], "store"), _count(row["sessions"], "session")]
            for name, row in sorted(summary["by_provider"].items())], "  ")
    _out(f"sessions: {summary['sessions']:,} listed of "
         f"{_count(summary['candidates'], 'candidate')} "
         f"({summary['parsed']:,} parsed, {summary['cached']:,} from cache)")
    if summary["warnings"]:
        _out(f"warnings: {len(summary['warnings'])} (see `{PROG} status`)")
    return 0


def _list(args) -> int:
    index = api.load_index()
    sessions = api.list_sessions(args.provider, index=index)
    if args.json:
        _json([s.to_dict() for s in sessions])
        return 0
    if index.last_quick is None:
        print(f"{PROG}: no index yet; run `{PROG} scan`", file=sys.stderr)
    now = datetime.now(timezone.utc)
    _table([[_age(s.updated or s.started, now), index.where(s), s.provider,
             s.title or "-", _project(s.cwd)] for s in sessions])
    return 0


def _status(args) -> int:
    status = api.status()
    if args.json:
        _json(status)
        return 0
    _out(f"index: {status['cache']}")
    _out(f"last scan: {status['last_quick'] or 'never'}; "
         f"last deep scan: {status['last_deep'] or 'never'}")

    _out("\nmounts")
    rows = []
    for mount in status["mounts"]:
        walk = mount["walk"]
        if not mount["mounted"]:
            detail = "not mounted"
        elif walk:
            detail = (f"{_count(walk['dirs'], 'dir')}, {walk['pruned']:,} pruned, "
                      f"{walk['errors']:,} unreadable, {walk['seconds']:.2f} s")
        else:
            detail = "not walked yet"
        rows.append([mount["letter"] or "", mount["label"], mount["path"],
                     mount["fstype"], detail])
    _table(rows, "  ")
    for mount in status["mounts"]:
        for path in (mount["walk"] or {}).get("error_paths", []):
            _out(f"  unreadable: {path}")

    _out("\nstores")
    rows = []
    for store in status["stores"]:
        flags = []
        if not store["present"]:
            flags.append("not mounted")
        if not store["reader"]:
            flags.append("reader not implemented")
        if store["duplicates"]:
            flags.append(f"{store['duplicates']} duplicate")
        rows.append([store["provider"], store["origin"], store["root"],
                     f"{store['listed']} listed / {store['candidates']} candidates / "
                     f"{store['skipped']} skipped", ", ".join(flags)])
    _table(rows, "  ")
    if not rows:
        _out("  none")

    _out("\nproviders")
    _table([[p["name"], _count(p["stores"], "store"), _count(p["sessions"], "session"),
             (p["installed"] or f"{p['binary']} not installed")
             + ("" if p["resumable"] else " (not resumable)")]
            for p in status["providers"]], "  ")

    if status["skipped_stores"]:
        _out("\nskipped stores")
        _table([[s["provider"], s["reason"] + ("" if s["mounted"] else ", not mounted"),
                 s["root"]] for s in status["skipped_stores"]], "  ")

    _out("\ndrive letters")
    rows = [[row["letter"], "->", row["mount"],
             f"{row['how']}, {_count(row['paths'], 'path')}"
             + (f", recorded on {row['source_mount']}" if row["source_mount"] else "")]
            for row in status["letters"]]
    rows += [[row["letter"], "->", "unresolved",
              f"{row['reason']}, {_count(row['paths'], 'path')}, "
              f"recorded on {row['source_mount']}"
              + (f", candidates: {', '.join(row['candidates'])}" if row["candidates"] else "")]
             for row in status["unresolved_letters"]]
    _table(rows, "  ")
    if not rows:
        _out("  none recorded")

    if status["no_reader"]:
        _out("\ninstalled tools with no reader")
        _table([[t["tool"], t["path"]] for t in status["no_reader"]], "  ")

    _out(f"\nwarnings: {len(status['warnings'])}")
    for warning in status["warnings"]:
        _out(f"  {warning}")
    return 0


def _verify(args) -> int:
    progress = None
    if sys.stderr.isatty():
        def progress(label, dirs):
            sys.stderr.write(f"\r\033[Kwalking {label}… {dirs:,} dirs")
            sys.stderr.flush()
    result = api.verify(progress=progress)
    if progress is not None:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
    dirs = sum(walk["dirs"] for walk in result["walk"].values())
    _out(f"verify: walked {_count(len(result['walk']), 'mount')}, {_count(dirs, 'dir')}, "
         f"{result['seconds']:.2f} s; {_count(result['stores'], 'store')}, "
         f"{_count(result['sessions'], 'session')} on disk")
    for line in result["nested"]:
        _out(f"note: store inside another store, not listed by design: {line}")
    for line in result["stale"]:
        _out(f"stale: {line}")
    if result["stale"]:
        _out(f"{_count(len(result['stale']), 'candidate')} changed on disk after the last "
             f"scan; run `{PROG} scan`")
    for line in result["differences"]:
        _out(f"DIFFERENT: {line}")
    if result["differences"]:
        _out(f"verify: {_count(len(result['differences']), 'difference')}")
        return 1
    _out("verify: the index matches the disks")
    return 0


def _not_implemented(args: argparse.Namespace) -> int:
    print(f"{PROG}: {args.command}: not implemented", file=sys.stderr)
    return 1


_COMMANDS = {"scan": _scan, "list": _list, "status": _status, "verify": _verify}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        return _COMMANDS.get(args.command, _not_implemented)(args)
    except MountsError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
