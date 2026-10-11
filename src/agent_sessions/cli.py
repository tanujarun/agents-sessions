"""argparse entry point; all subcommands."""

from __future__ import annotations

import argparse
import sys

from . import __version__

PROG = "agent-sessions"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Find, list and resume saved AI coding-agent sessions.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("scan", help="refresh index; print summary")
    p.add_argument("--deep", action="store_true", help="walk every mount completely")
    p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("list", help="sessions, newest first")
    p.add_argument("--provider", metavar="P", help="only this provider")
    p.add_argument("--json", action="store_true", help="JSON output")

    p = sub.add_parser("menu", help="TSV rows for the menu")
    p.add_argument("provider")

    p = sub.add_parser("open", help="native: resume. foreign: chooser")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true", help="print the command, do not run it")

    p = sub.add_parser("import", help="import; optionally resume after")
    p.add_argument("key")
    p.add_argument("--open", action="store_true", help="resume after import")

    p = sub.add_parser("show", help="plain-text transcript to stdout")
    p.add_argument("key")

    sub.add_parser("status", help="mounts, stores, counts per provider, warnings")
    sub.add_parser("verify", help="compare index against a brute-force walk")
    return parser


def _not_implemented(args: argparse.Namespace) -> int:
    print(f"{PROG}: {args.command}: not implemented", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return _not_implemented(args)
