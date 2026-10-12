"""Pure view logic for the TUI: what a row says, how columns fit, which rows
a filter keeps, where the selection lands, what a key means.

Standard library only (plus the engine's ``textsafe``) and no I/O, so every
decision here is unit-tested on the system Python. Nothing in this module
knows about Textual, and nothing here is a colour, a border or a spacing
value: those are in ``app.tcss``.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any, Protocol

from .. import textsafe

APP_TITLE = "agent-sessions"
ELLIPSIS = "…"
EMPTY = "-"

ALL = "all"
"""Name of the tab that lists every provider."""

PROVIDER_ORDER = ("claude", "codex", "grok", "antigravity", "gemini")
"""Tab order of the known providers (SPEC "TUI"); others follow, then All."""

NARROW_WIDTH = 80
"""Below this many columns the PROJECT column is dropped."""

DETAIL_MIN_WIDTH = 60
DETAIL_MIN_HEIGHT = 20
"""Below either, the detail pane is dropped too."""

PAGER_MAX_TURNS = 2000
PAGER_MAX_TURN_CHARS = 20000
"""Transcript pager limits: turns loaded, characters shown of one turn."""


class SessionLike(Protocol):
    """The fields of an engine ``Session`` that the view reads."""

    id: str
    key: str
    provider: str
    title: str
    cwd: str | None
    cwd_local: str | None
    cwd_exists: bool
    started: str | None
    updated: str | None
    messages: int | None
    store: str
    origin: str
    imported_as: str | None
    note: str | None


@dataclass(frozen=True)
class Verdict:
    """What the engine answered about importing a session (the fields of its
    ``ImportOptions``), as the view reads it. ``known`` is False when asking
    failed; ``reason`` then says why."""

    possible: bool = False
    reason: str = ""
    needs_folder: bool = False  # it would work into a folder the user chooses
    experimental: bool = False  # it works only as an experimental import
    warning: str = ""  # what is shown before an experimental import
    known: bool = True


# --- display width ----------------------------------------------------------


def char_width(ch: str) -> int:
    """Terminal cells one character takes: 0 for combining marks, joiners and
    control characters, 2 for wide (CJK, most emoji), else 1."""
    if ch < "\x7f":
        return 1 if ch >= " " else 0
    category = unicodedata.category(ch)
    if category in ("Mn", "Me", "Cf", "Cc"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def text_width(text: str) -> int:
    """Terminal cells a string takes."""
    if text.isascii():
        return len(text)
    return sum(char_width(ch) for ch in text)


def clean(text: object) -> str:
    """One printable line: control characters (newline, tab, escape) and the
    characters that reorder text become spaces, so no value can break a row
    or move the cursor. The rule itself is ``textsafe.clean``."""
    return textsafe.clean(text)


def _take(text: str, width: int) -> str:
    """The longest prefix of ``text`` that fits in ``width`` cells."""
    if text.isascii():
        return text[: max(width, 0)]
    used = 0
    for position, ch in enumerate(text):
        used += char_width(ch)
        if used > width:
            return text[:position]
    return text


def clip(text: str, width: int) -> str:
    """``text`` cut to at most ``width`` cells; a cut ends in an ellipsis. A
    wide character that would straddle the limit is dropped whole."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    return _take(text, width - 1) + ELLIPSIS


def pad(text: str, width: int, align: str = "left") -> str:
    """``text`` clipped and padded to exactly ``width`` cells."""
    text = clip(text, width)
    space = " " * max(width - text_width(text), 0)
    return space + text if align == "right" else text + space


# --- time -------------------------------------------------------------------


def parse_stamp(stamp: object) -> datetime | None:
    """An ISO 8601 time as an aware datetime; None when absent or malformed."""
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def age_text(stamp: object, now: datetime) -> str:
    """``now``, ``5m``, ``3h``, ``2d``, ``3w``, ``2mo``, ``1y``. The steps are
    the ones `agent-sessions list` prints: seconds under a minute are ``now``,
    days up to 13, weeks up to 59 days, months up to a year."""
    then = parse_stamp(stamp)
    if then is None:
        return EMPTY
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


def local_time(stamp: object, tz: tzinfo | None = None) -> str:
    """``2026-10-10 17:58`` in the local zone (or ``tz``); ``-`` when unknown."""
    moment = parse_stamp(stamp)
    if moment is None:
        return EMPTY
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


# --- rows -------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """What the list shows of one session."""

    key: str
    provider: str
    title: str
    project: str
    stamp: str | None  # the time AGE is counted from
    where: str
    foreign: bool
    imported: bool
    haystack: str  # casefolded title, project and cwd, for the filter


def project_name(cwd: str | None) -> str:
    """The folder a session ran in: the last segment of its recorded cwd,
    whichever separator it was written with."""
    parts = [part for part in re.split(r"[\\/]+", cwd or "") if part]
    return clean(parts[-1]) if parts else EMPTY


def make_row(session: SessionLike, where: str) -> Row:
    """The row for a session; ``where`` is the engine's WHERE text."""
    title = clean(session.title).strip() or EMPTY
    project = project_name(session.cwd)
    haystack = "\n".join((title, project, session.cwd or "")).casefold()
    return Row(
        key=session.key,
        provider=session.provider,
        title=title,
        project=project,
        stamp=session.updated or session.started,
        where=clean(where),
        foreign=session.origin != "native",
        imported=bool(session.imported_as),
        haystack=haystack,
    )


def matches(row: Row, needle: str) -> bool:
    """Substring match over title, project and cwd, ignoring case."""
    needle = needle.strip().casefold()
    return not needle or needle in row.haystack


def filter_rows(rows: Iterable[Row], provider: str | None = None, needle: str = "") -> list[Row]:
    """The rows of one provider (``None`` or ``ALL``: every provider) that
    match the filter, in the order given."""
    everyone = provider is None or provider == ALL
    return [row for row in rows if (everyone or row.provider == provider) and matches(row, needle)]


def select_index(rows: Sequence[Row], key: str | None, previous: int = 0) -> int:
    """Index of the row with ``key``. When it is gone, the row now at the old
    position (clamped), so the selection stays where the eye was. ``-1`` for
    an empty list."""
    if not rows:
        return -1
    if key is not None:
        for index, row in enumerate(rows):
            if row.key == key:
                return index
    return min(max(previous, 0), len(rows) - 1)


def move_index(index: int, count: int, move: str, page: int = 1) -> int:
    """The selection after a movement: ``up``, ``down``, ``pageup``,
    ``pagedown``, ``home``, ``end``. Clamped; ``-1`` for an empty list."""
    if count <= 0:
        return -1
    page = max(page, 1)
    target = {
        "up": index - 1,
        "down": index + 1,
        "pageup": index - page,
        "pagedown": index + page,
        "home": 0,
        "end": count - 1,
    }.get(move, index)
    return min(max(target, 0), count - 1)


def scroll_for(index: int, top: int, height: int, count: int) -> int:
    """The first visible row after the smallest scroll that shows ``index``."""
    if height <= 0 or count <= 0:
        return 0
    if index < top:
        top = index
    elif index >= top + height:
        top = index - height + 1
    return min(max(top, 0), max(count - height, 0))


# --- provider tabs ----------------------------------------------------------


@dataclass(frozen=True)
class Tab:
    name: str  # provider name, or ALL
    label: str
    count: int
    digit: str  # the key that selects it; "" when there is none


def build_tabs(providers: Iterable[tuple[str, str]], rows: Sequence[Row]) -> list[Tab]:
    """One tab per provider in the SPEC order, unknown providers after in the
    order given, then All. ``providers`` is ``[(name, label)]``. Digits ``1``
    to ``9`` select the provider tabs, ``0`` selects All."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.provider] = counts.get(row.provider, 0) + 1
    labels: dict[str, str] = {}
    for name, label in providers:
        labels.setdefault(name, label or name)
    ordered = [name for name in PROVIDER_ORDER if name in labels]
    ordered += [name for name in labels if name not in PROVIDER_ORDER]
    tabs = [
        Tab(name, labels[name], counts.get(name, 0), str(n) if n <= 9 else "")
        for n, name in enumerate(ordered, 1)
    ]
    tabs.append(Tab(ALL, "All", len(rows), "0"))
    return tabs


def tab_for_digit(tabs: Iterable[Tab], digit: str) -> int | None:
    """Index of the tab a digit key selects, or None."""
    for index, tab in enumerate(tabs):
        if tab.digit and tab.digit == digit:
            return index
    return None


def cycle_tab(index: int, step: int, count: int) -> int:
    """The tab ``step`` places on, wrapping round."""
    return (index + step) % count if count else 0


def _tab_label(tab: Tab, level: int, active: bool) -> str:
    digit = f"{tab.digit} " if tab.digit else ""
    if level == 0 or (active and level <= 3):
        return f"{digit}{tab.label} {tab.count}"
    if active:
        return f"{digit}{tab.label}"
    if level == 1:
        return f"{digit}{tab.label}"
    if level == 2:
        return f"{digit}{tab.label[:3]}"
    return tab.digit or tab.label[:1]


def fit_tab_labels(tabs: Sequence[Tab], active: int, available: int, extra: int = 0) -> list[str]:
    """Tab texts that fit in ``available`` cells when every tab also takes
    ``extra`` cells of its own (padding, margin). Tried in turn: full
    (``1 Claude 41``), without counts, three-letter names, digits only, each
    with the active tab in full; last, the active tab loses its count too."""
    labels: list[str] = []
    for level in range(5):
        labels = [_tab_label(tab, level, index == active) for index, tab in enumerate(tabs)]
        if sum(text_width(label) + extra for label in labels) <= available:
            break
    return labels


# --- columns ----------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str  # "title" | "provider" | "project" | "age" | "where"
    title: str
    width: int


AGE_WIDTH = 4  # "11mo"
MIN_TITLE_WIDTH = 16
MAX_SIDE_WIDTH = 24  # cap on PROJECT, WHERE and PROVIDER

_HEADINGS = {
    "title": "TITLE",
    "provider": "PROVIDER",
    "project": "PROJECT",
    "age": "AGE",
    "where": "WHERE",
}
_ORDER = ("title", "provider", "project", "age", "where")
_DROP_ORDER = ("project", "where", "provider", "age")


def column_widths(
    rows: Collection[Row], provider_labels: Mapping[str, str] | None = None
) -> dict[str, int]:
    """Natural widths of the PROJECT, WHERE and PROVIDER columns for a set of
    rows: the widest value, at least the heading, at most ``MAX_SIDE_WIDTH``."""
    provider_labels = provider_labels or {}

    def widest(values: Iterable[str], heading: str) -> int:
        longest = max((text_width(value) for value in values), default=0)
        return min(max(longest, len(heading)), MAX_SIDE_WIDTH)

    return {
        "project": widest({row.project for row in rows}, _HEADINGS["project"]),
        "where": widest({row.where for row in rows}, _HEADINGS["where"]),
        "provider": widest(
            {provider_labels.get(row.provider, row.provider) for row in rows}, _HEADINGS["provider"]
        ),
    }


def fit_columns(
    width: int,
    widths: Mapping[str, int] | None = None,
    *,
    show_provider: bool = False,
    show_project: bool = True,
    cell_pad: int = 0,
) -> list[Column]:
    """The columns that fit in ``width`` cells, left to right, TITLE taking
    what the others leave. ``cell_pad`` is the padding every cell carries
    (left plus right). When TITLE would get less than ``MIN_TITLE_WIDTH``,
    columns go in this order: PROJECT, WHERE, PROVIDER, AGE. TITLE is never
    dropped, however narrow."""
    widths = widths or {}
    sides = {"age": AGE_WIDTH, "where": widths.get("where", len(_HEADINGS["where"]))}
    if show_project:
        sides["project"] = min(
            widths.get("project", len(_HEADINGS["project"])),
            max(width // 4, len(_HEADINGS["project"])),
        )
    if show_provider:
        sides["provider"] = widths.get("provider", len(_HEADINGS["provider"]))

    def title_width() -> int:
        return width - cell_pad - sum(size + cell_pad for size in sides.values())

    for name in _DROP_ORDER:
        if title_width() >= MIN_TITLE_WIDTH:
            break
        sides.pop(name, None)
    sides["title"] = max(title_width(), 1)
    return [Column(name, _HEADINGS[name], sides[name]) for name in _ORDER if name in sides]


def _cells(
    values: Mapping[str, str], columns: Iterable[Column], pad_left: int, pad_right: int
) -> str:
    return "".join(
        " " * pad_left + pad(values.get(column.name, ""), column.width) + " " * pad_right
        for column in columns
    )


def format_header(columns: Sequence[Column], pad_left: int = 0, pad_right: int = 0) -> str:
    """The column header line."""
    return _cells({column.name: column.title for column in columns}, columns, pad_left, pad_right)


def format_row(
    row: Row,
    columns: Iterable[Column],
    now: datetime,
    provider_labels: Mapping[str, str] | None = None,
    pad_left: int = 0,
    pad_right: int = 0,
) -> str:
    """One list line: every cell clipped with an ellipsis and padded to its
    column, by display width."""
    provider_labels = provider_labels or {}
    values = {
        "title": row.title,
        "project": row.project,
        "provider": provider_labels.get(row.provider, row.provider),
        "age": age_text(row.stamp, now),
        "where": row.where,
    }
    return _cells(values, columns, pad_left, pad_right)


# --- layout for small terminals ---------------------------------------------


@dataclass(frozen=True)
class Layout:
    show_project: bool
    show_detail: bool
    hints: tuple[str, ...]


HINTS_FULL = (
    "↑↓ move  ←→ provider  Enter resume  o resume, keep open  i import  t import into folder"
    "  v view",
    "c continue  u undo import  m mount  / filter  r rescan  R full rescan  ? help  q quit",
)
HINTS_MEDIUM = (  # the same keys in the 80 columns of a standard terminal
    "↑↓ move  ←→ provider  Enter resume  o keep open  i import  t into folder",
    "v view  c continue  u undo  m mount  / filter  r R rescan  ? help  q quit",
)
HINTS_SHORT = (
    "Enter resume  i import  c continue  v view  / filter  ? help  q quit",
    "Enter resume  i import  v view  / filter  ? help  q quit",
    "Enter resume  / filter  ? help  q quit",
    "Enter resume  ? help  q quit",
    "? help  q quit",
)


def hint_lines(width: int, roomy: bool = True) -> tuple[str, ...]:
    """Key hints for a width: two lines with every key when they fit and the
    terminal is tall enough (``roomy``), in full or in shorter words, else
    the longest one-line version that fits."""
    for lines in (HINTS_FULL, HINTS_MEDIUM) if roomy else ():
        if all(text_width(line) <= width for line in lines):
            return lines
    for line in HINTS_SHORT:
        if text_width(line) <= width:
            return (line,)
    return (HINTS_SHORT[-1],)


def layout_for(width: int, height: int, hint_gutter: int = 0) -> Layout:
    """What a terminal of this size shows (SPEC: below 80 columns PROJECT
    goes first, then the detail pane). ``hint_gutter`` is the padding round
    the hint text."""
    show_detail = width >= DETAIL_MIN_WIDTH and height >= DETAIL_MIN_HEIGHT
    return Layout(
        show_project=width >= NARROW_WIDTH,
        show_detail=show_detail,
        hints=hint_lines(width - hint_gutter, roomy=show_detail),
    )


# --- status line ------------------------------------------------------------


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}" if count == 1 else f"{count:,} {word}s"


def short_count(count: int) -> str:
    """``512``, ``41k``, ``1.2M``."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.1f}M"


def bytes_text(count: float | None) -> str:
    """``812 B``, ``3.1 kB``, ``12.0 MB``, ``1.4 GB``."""
    count = max(int(count or 0), 0)
    if count < 1000:
        return f"{count} B"
    size = float(count)
    for unit in ("kB", "MB", "GB"):
        size /= 1000
        if size < 1000 or unit == "GB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} GB"


def drive_count(mounts: Iterable[Mapping[str, Any]] | None) -> int:
    """Drives in a status report: mounted entries, one per label (``/`` and
    ``/home`` are both ``Linux``)."""
    return len(
        {
            mount.get("label") or mount.get("path")
            for mount in mounts or ()
            if mount.get("mounted", True)
        }
    )


def summary_variants(
    drives: int, sessions: int, last_scan: object, now: datetime, offline: int = 0
) -> list[str]:
    """The right-hand status text, longest first, for ``status_parts`` to
    pick from: ``5 drives · 63 sessions · scanned 2m ago``. ``offline`` is
    how many of the sessions are on drives that are not mounted; when there
    are any, the count reads ``136 sessions · 29 offline``."""
    if parse_stamp(last_scan) is None:
        scanned = "never scanned"
    else:
        age = age_text(last_scan, now)
        scanned = "scanned just now" if age == "now" else f"scanned {age} ago"
    total = plural(sessions, "session")
    count = f"{total} · {offline:,} offline" if offline else total
    variants = [f"{plural(drives, 'drive')} · {count} · {scanned}", f"{count} · {scanned}", count]
    return variants + [total] if offline else variants


def status_parts(
    left: str, variants: Sequence[str], width: int, gap: int = 2, keep_left: bool = False
) -> tuple[str, str]:
    """Share the status line between the left text (the app name, or a
    message) and the longest right text that still fits beside it. When none
    fits, a message wins the whole line (``keep_left``); otherwise the
    shortest right text does."""
    left = clean(left)
    for right in variants:
        if text_width(left) + gap + text_width(right) <= width:
            return left, right
    if keep_left or not variants:
        return clip(left, width), ""
    return "", clip(variants[-1], width)


def scan_progress_text(label: object, dirs: int) -> str:
    """``scanning Zephyro… 41k dirs``."""
    return f"scanning {clean(label)}{ELLIPSIS} {short_count(int(dirs or 0))} dirs"


def import_progress_text(done: float | None, total: float | None) -> str:
    """``importing… 3.1 MB of 12.0 MB``."""
    if not total:
        return f"importing{ELLIPSIS} {bytes_text(done)}"
    return f"importing{ELLIPSIS} {bytes_text(done)} of {bytes_text(total)}"


_JOBS = {  # a job that ends in a scan: (what is running, what is asked for)
    "scan": ("a scan", "rescan"),
    "import": ("an import", "import"),
    "mount": ("a mount", "mount"),
    "undo": ("an undo", "undo"),
}


def busy_text(running: str, wanted: str) -> str:
    """Why a job is not started while another runs (one at a time: each ends
    in a scan): ``a scan is running; import when it has finished``."""
    article = _JOBS[running][0]
    if running == wanted:
        return f"{article} is already running"
    return f"{article} is running; {_JOBS[wanted][1]} when it has finished"


def scan_message(summary: Mapping[str, Any] | None) -> str:
    """One line for a finished scan."""
    summary = summary or {}
    name = "full rescan" if summary.get("mode") == "deep" else "rescan"
    if summary.get("cancelled"):
        return f"{name} cancelled"
    text = f"{name}: {plural(int(summary.get('sessions') or 0), 'session')}"
    seconds = summary.get("seconds")
    if isinstance(seconds, (int, float)):
        text += f", {seconds:.1f} s"
    warnings = summary.get("warnings") or []
    if warnings:
        text += f", {plural(len(warnings), 'warning')}"
    return text


def error_text(exc: BaseException) -> str:
    """An engine failure in plain words: its ``reason`` when it has one, else
    its message, else the kind of error."""
    reason = getattr(exc, "reason", None)
    text = clean(reason if isinstance(reason, str) and reason else str(exc)).strip()
    return text or type(exc).__name__


# --- detail pane ------------------------------------------------------------

DETAIL_FIELDS = ("id", "messages", "updated", "cwd", "local", "store", "note", "import")


def make_verdict(options: object) -> Verdict:
    """The verdict in an engine's ``ImportOptions``, read field by field."""
    return Verdict(
        possible=bool(getattr(options, "possible", False)),
        reason=clean(getattr(options, "reason", "") or "").strip(),
        needs_folder=bool(getattr(options, "needs_folder", False)),
        experimental=bool(getattr(options, "experimental", False)),
        warning=str(getattr(options, "warning", "") or ""),  # cleaned where it is wrapped
    )


def failed_verdict(exc: BaseException) -> Verdict:
    """The verdict when asking the engine failed."""
    return Verdict(reason=error_text(exc), known=False)


def verdict_text(session: SessionLike, verdict: Verdict | None) -> str:
    """The import line of the detail pane. ``verdict`` is the engine's
    answer for a foreign session, or None while it is being worked out."""
    if session.origin == "native":
        return EMPTY
    if session.imported_as:
        return "already imported; Enter resumes the Linux copy"
    if verdict is None:
        return f"checking{ELLIPSIS}"
    reason = clean(verdict.reason or "").strip()
    if not verdict.known:  # the question itself failed
        return f"unknown: {reason}" if reason else "unknown"
    if verdict.possible:
        return "possible"
    if verdict.needs_folder and verdict.experimental:
        return "experimental only, into a folder you choose"
    if verdict.needs_folder:
        return "possible into a folder you choose"
    if verdict.experimental:
        return "experimental only"
    return f"not possible: {reason}" if reason else "not possible"


def detail_fields(
    session: SessionLike | None, verdict: Verdict | None = None, tz: tzinfo | None = None
) -> dict[str, str]:
    """The detail pane for a session, one value per ``DETAIL_FIELDS`` name;
    all ``-`` when nothing is selected."""
    if session is None:
        return dict.fromkeys(DETAIL_FIELDS, EMPTY)
    found = "(exists)" if session.cwd_exists else "(not found)"
    local = f"{clean(session.cwd_local)}   {found}" if session.cwd_local else found
    return {
        "id": clean(session.id) or EMPTY,
        "messages": EMPTY if session.messages is None else f"{session.messages:,}",
        "updated": local_time(session.updated or session.started, tz),
        "cwd": clean(session.cwd) or EMPTY,
        "local": local,
        "store": clean(session.store) or EMPTY,
        "note": clean(session.note) or EMPTY,
        "import": verdict_text(session, verdict),
    }


# --- prompts ----------------------------------------------------------------


def drive_of(where: str) -> str:
    """The drive part of a WHERE text (``C: · imported`` -> ``C:``)."""
    return where.split(" · ")[0].strip() or "another drive"


def fit_first(variants: Sequence[str], width: int | None) -> str:
    """The first text that fits in ``width`` cells; the last one, clipped,
    when none does. ``None`` for the width means no limit."""
    if width is None:
        return variants[0]
    for text in variants:
        if text_width(text) <= width:
            return text
    return clip(variants[-1], width)


def clip_left(text: str, width: int) -> str:
    """``text`` cut to at most ``width`` cells from the left, so that its end
    stays (what tells one path from another); a cut starts with an ellipsis."""
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    return ELLIPSIS + _take(text[::-1], width - 1)[::-1]


def _lead(drive: str) -> str:
    """A drive in front of what is said of it; a drive letter brings its own
    colon (``C:``, ``Zephyro:``)."""
    return drive if drive.endswith(":") else f"{drive}:"


# The choices of a prompt, each as (key, long, shorter, shortest wording).
_CONTINUE = ("[c]", "continue as new", "continue", "new")
_VIEW = ("[v]", "view", "view", "view")
_CANCEL = ("[Esc]", "cancel", "", "")
_CHOICES = {
    "foreign": (
        ("[i]", "import and resume", "import+resume", "resume"),
        ("[I]", "import only", "import", "import"),
        ("[t]", "into another folder", "folder", "folder"),
        _CONTINUE,
        _VIEW,
        _CANCEL,
    ),
    "foreign_folder": (
        ("[t]", "import into another folder", "import into folder", "folder"),
        _CONTINUE,
        _VIEW,
        _CANCEL,
    ),
    "foreign_experimental": (
        ("[i]", "import and resume (experimental)", "import+resume (exp.)", "resume"),
        ("[I]", "import only (experimental)", "import (exp.)", "import"),
        _CONTINUE,
        _VIEW,
        _CANCEL,
    ),
    "foreign_none": (_CONTINUE, _VIEW, _CANCEL),
    "mount": (("[m]", "mount", "mount", "mount"), _CANCEL),
}


def _choice_line(mode: str, lead: str, width: int | None) -> str:
    """The choices of a prompt on one line: in full after ``lead``, then in
    shorter words, then without the lead, last the keys alone, so that no
    choice is cut off on a narrow terminal."""
    choices = _CHOICES[mode]

    def worded(level: int, gap: str) -> str:
        return gap.join(f"{choice[0]} {choice[level]}".rstrip() for choice in choices)

    return fit_first(
        (
            lead + worded(1, "  "),
            lead + worded(2, "  "),
            worded(3, " "),
            " ".join(choice[0] for choice in choices),
        ),
        width,
    )


def foreign_mode(verdict: Verdict) -> str:
    """Which prompt Enter on a foreign row opens, by what its import can be:
    the name of its key table and of its choices."""
    if verdict.possible:
        return "foreign"
    if verdict.needs_folder:
        return "foreign_folder"
    if verdict.experimental:
        return "foreign_experimental"
    return "foreign_none"


def foreign_prompt(where: str, verdict: Verdict, width: int | None = None) -> list[str]:
    """The foreign-row prompt, one or two lines: when the session cannot be
    imported as it is, the reason; then the choices that apply (``On C: [i]
    import and resume  [I] import only …``)."""
    mode = foreign_mode(verdict)
    lead = f"On {_lead(drive_of(where))} " if mode == "foreign" else ""
    lines = [_choice_line(mode, lead, width)]
    reason = clean(verdict.reason or "").strip()
    if reason and not verdict.possible:
        lines.insert(0, reason if width is None else clip(reason, width))
    return lines


def import_step(verdict: Verdict, key: str) -> str:
    """What ``i`` (``key`` is ``import``) or ``t`` (``folder``) on a foreign
    row leads to: ``refuse`` (the reason is shown), ``folder`` (the folder
    input), ``warn`` (the warning of an experimental import) or ``confirm``
    (the y/n)."""
    if not (verdict.possible or verdict.needs_folder or verdict.experimental):
        return "refuse"
    if key == "folder" or verdict.needs_folder:
        return "folder"
    return "warn" if verdict.experimental else "confirm"


def confirm_prompt(title: str, where: str, width: int | None = None) -> str:
    drive = drive_of(where)
    return fit_first(
        (
            f'Import "{clip(title, 40)}" from {drive} to Linux?  [y] yes  [n] no',
            f"Import from {drive} to Linux?  [y] yes  [n] no",
            "Import?  [y] yes  [n] no",
            "Import? [y] [n]",
        ),
        width,
    )


WARNING_LINES = 3
"""How many lines of an experimental import's warning a prompt shows."""


def warning_lines(warning: str, width: int | None, limit: int = WARNING_LINES) -> list[str]:
    """A warning wrapped to ``width`` cells, at most ``limit`` lines; one
    that is longer ends in an ellipsis."""
    lines = [line for line in wrap_text(warning, width or 0) if line.strip()]
    if len(lines) <= limit:
        return lines
    last = lines[limit - 1] + ELLIPSIS
    return lines[: limit - 1] + [last if not width else clip(last, width)]


def warn_prompt(warning: str, width: int | None = None) -> list[str]:
    """The second question of an experimental import: its warning, then
    ``Import anyway? y/n``."""
    question = fit_first(("Import anyway? y/n", "Import? y/n"), width)
    return [*warning_lines(warning, width), question]


# --- drives that are not mounted ----------------------------------------------

DRIVE_IS_MOUNTED = "this session's drive is mounted"
VOLUME_ABSENT = "absent"
"""The ``state`` of a row of the engine's ``volumes``: the device is not there."""


def volume_state(volumes: Iterable[Mapping[str, Any]] | None, mount: str | None) -> str | None:
    """The state of the volume at a mount path in the engine's ``volumes``
    (``mounted``, ``attached``, ``absent``); None when it is not listed."""
    for volume in volumes or ():
        if volume.get("path") == mount:
            state = volume.get("state")
            return state if isinstance(state, str) else None
    return None


def mount_prompt(where: str, width: int | None = None) -> str:
    """``C: is not mounted: [m] mount  [Esc] cancel``."""
    return _choice_line("mount", f"{drive_of(where)} is not mounted: ", width)


def mount_activity_text(drive: str) -> str:
    """``mounting Zephyro: answer the password prompt…``."""
    return f"mounting {_lead(drive)} answer the password prompt{ELLIPSIS}"


def mounted_text(drive: str) -> str:
    return f"{drive} mounted"


def already_mounting_text(drive: str) -> str:
    return f"already mounting {drive}"


def not_attached_text(drive: str) -> str:
    return f"{drive} is not attached"


# --- a folder the user types ----------------------------------------------------

FOLDER_LABELS = {"import": "Import into:", "continue": "Start in:"}
"""The label of the folder input, by what the folder is for."""

FOLDER_START = "~/"
"""What the folder input holds when it opens with no folder to suggest."""

COMPLETIONS_SHOWN = 6
"""How many names the status line shows when several folders match."""

_SEPARATORS = tuple(sep for sep in (os.sep, os.altsep) if sep)


@dataclass(frozen=True)
class Completion:
    """What Tab makes of the text in the folder input."""

    text: str  # the text afterwards; as it was when there is nothing to add
    matches: tuple[str, ...] = ()  # the folders that match, in order
    error: str = ""  # why the folder could not be listed


def expand_folder(text: str, home: str) -> str:
    """The path a typed folder means: ``~`` and ``~/…`` are the home
    directory, anything else is taken as it was typed."""
    if text == "~":
        return home
    if text[:1] == "~" and text[1:2] in _SEPARATORS:
        return home.rstrip("".join(_SEPARATORS)) + text[1:]
    return text


def complete_folder(
    text: str, list_folder: Callable[[str], Iterable[tuple[str, bool]]], home: str
) -> Completion:
    """Complete the last name of a typed folder, as Tab does in a shell.

    ``list_folder(directory)`` gives the entries of a directory as ``(name,
    is a directory)`` and may raise ``OSError``; it is the only I/O, and the
    caller's. Only directories count, and hidden ones only when the name
    being typed starts with a dot. One match is completed and gets a
    separator; several are completed as far as they agree, and are named in
    ``matches``. Nothing is listed for an empty text or a relative one."""
    if text == "~":
        return Completion(text + os.sep)
    cut = max(text.rfind(sep) for sep in _SEPARATORS) + 1
    head, typed = text[:cut], text[cut:]
    directory = expand_folder(head, home)
    if not head or not os.path.isabs(directory):
        return Completion(text)
    try:
        entries = list(list_folder(directory))
    except OSError as exc:
        return Completion(text, error=f"cannot list {clean(directory)}: {exc.strerror or exc}")
    names = sorted(
        name
        for name, is_dir in entries
        if is_dir and name.startswith(typed) and (typed[:1] == "." or name[:1] != ".")
    )
    if len(names) == 1:
        return Completion(head + names[0] + os.sep, tuple(names))
    if not names:
        return Completion(text)
    # As far as they agree: sorted, so the first and the last differ soonest.
    first, last = names[0], names[-1]
    agreed = next(
        (n for n, (a, b) in enumerate(zip(first, last, strict=False)) if a != b), len(first)
    )
    return Completion(head + first[:agreed], tuple(names))


def completions_text(names: Sequence[str], shown: int = COMPLETIONS_SHOWN) -> str:
    """The first few of the folders that match, for the status line:
    ``alpha  alps  alto  … 4 more``."""
    text = "  ".join(clean(name) for name in names[:shown])
    more = len(names) - shown
    return f"{text}  {ELLIPSIS} {more:,} more" if more > 0 else text


def folder_error(path: str) -> str:
    """What Enter answers for a text that is not a folder."""
    return f"not an existing folder: {clean(path)}" if path else "type a folder"


# --- continue as a new session ----------------------------------------------------


def tool_start(tools: Sequence[tuple[str, str]], default: str | None) -> int:
    """Where the choice of a tool starts: at the engine's default, else at
    the first one."""
    for index, (name, _label) in enumerate(tools):
        if name == default:
            return index
    return 0


TOOL_MARKS = ("\u2039", "\u203a")
"""The single angle quotation marks round the tool that is chosen."""


def tool_prompt(label: str, width: int | None = None) -> str:
    """The first step of a continue: ``Continue with:``, the chosen tool
    between ``TOOL_MARKS``, then ``←→ change  Enter next  Esc cancel``."""
    choice = f"{TOOL_MARKS[0]} {clean(label)} {TOOL_MARKS[1]}"
    return fit_first(
        (
            f"Continue with: {choice}   ←→ change  Enter next  Esc cancel",
            f"{choice}  ←→ change  Enter next  Esc cancel",
            f"{choice} ←→ Enter Esc",
            choice,
        ),
        width,
    )


MIN_FOLDER_WIDTH = 12
"""A folder is not shown in fewer cells than this: it would say nothing."""


def start_prompt(label: str, folder: str, width: int | None = None) -> str:
    """``Start Claude in /home/me/site and send a first message to the
    model? y/n``; on a narrow terminal the folder keeps its end, then goes."""
    tool, folder = clean(label), clean(folder)
    question = "and send a first message to the model? y/n"
    variants = [f"Start {tool} in {folder} {question}"]
    if width is not None:
        room = width - text_width(f"Start {tool} in  {question}")
        if room >= MIN_FOLDER_WIDTH:
            variants.append(f"Start {tool} in {clip_left(folder, room)} {question}")
    variants += [
        f"Start {tool} {question}",
        f"Start {tool} and send a first message? y/n",
        f"Start {tool}? y/n",
        "Start? y/n",
    ]
    return fit_first(variants, width)


def started_text(label: str) -> str:
    return f"started {clean(label)} in a new window"


# --- undo an import ---------------------------------------------------------------


def undo_refusal(exc: BaseException) -> tuple[str, int]:
    """Which refusal of an undo this is, from what the engine marked on it
    (``UndoFailed.no_record``, ``.differences``) and never from its text:
    ``("none", 0)`` when no import is recorded, ``("used", n)`` when the copy
    was used since the import and ``n`` of its files differ, else
    ``("other", 0)``."""
    differences = getattr(exc, "differences", None)
    if isinstance(differences, (list, tuple)) and differences:
        return "used", len(differences)
    if getattr(exc, "no_record", False) is True:
        return "none", 0
    return "other", 0


def undo_counts(report: object) -> tuple[int, int, int]:
    """``(files, bytes, left)`` of an engine's ``UnimportReport``: the files
    it removes (everything it removes, for a report that does not count
    them), their size, and how many things stay in place."""
    files = getattr(report, "files", None)
    if not isinstance(files, int):
        files = len(getattr(report, "removed", None) or ())
    return files, int(getattr(report, "bytes", 0) or 0), len(getattr(report, "left", None) or ())


def undo_prompt(files: int, size: float, width: int | None = None) -> str:
    """``Remove the imported copy (12 files, 3.1 MB)? y/n``."""
    return fit_first(
        (
            f"Remove the imported copy ({plural(files, 'file')}, {bytes_text(size)})? y/n",
            "Remove the imported copy? y/n",
            "Remove the copy? y/n",
            "Remove? y/n",
        ),
        width,
    )


def undo_used_prompt(differ: int, width: int | None = None) -> str:
    """The question for a copy that was used since its import: removing it
    loses what was added."""
    files = f"{plural(differ, 'file')} {'differs' if differ == 1 else 'differ'}"
    return fit_first(
        (
            f"The copy was used since the import ({files}). Remove it and lose its new turns? y/n",
            "The copy was used since the import. Remove it and lose its new turns? y/n",
            "Used since the import. Remove it and lose its new turns? y/n",
            "Used since the import. Remove it? y/n",
            "Remove it? y/n",
        ),
        width,
    )


def undo_message(files: int, left: int = 0) -> str:
    """``removed 12 files, 1 left in place``."""
    return f"removed {plural(files, 'file')}" + (f", {left:,} left in place" if left else "")


# --- keys -------------------------------------------------------------------

_LIST_KEYS = {
    "up": "cursor_up",
    "k": "cursor_up",
    "down": "cursor_down",
    "j": "cursor_down",
    "pageup": "cursor_pageup",
    "pagedown": "cursor_pagedown",
    "home": "cursor_home",
    "g": "cursor_home",
    "end": "cursor_end",
    "G": "cursor_end",
    "left": "tab_prev",
    "h": "tab_prev",
    "shift+tab": "tab_prev",
    "right": "tab_next",
    "l": "tab_next",
    "tab": "tab_next",
    "enter": "resume",
    "o": "resume_stay",
    "i": "import",
    "t": "import_folder",
    "c": "continue",
    "u": "undo",
    "m": "mount",
    "v": "view",
    "/": "filter",
    "r": "rescan",
    "R": "full_rescan",
    "?": "help",
    "q": "quit",
    "escape": "escape",
}
_LIST_KEYS.update({str(digit): "tab_digit" for digit in range(10)})

# What every foreign-row prompt offers, whatever the import can be.
_FOREIGN_KEYS = {"c": "prompt_continue", "v": "prompt_view", "escape": "prompt_cancel"}


def _yes_no(yes: str) -> dict[str, str]:
    """The keys of a y/n question whose ``y`` means the action ``yes``."""
    return {"y": yes, "Y": yes, "n": "confirm_no", "N": "confirm_no", "escape": "confirm_no"}


KEYMAPS = {
    "list": _LIST_KEYS,
    # A prompt takes over the keys. The foreign-row prompt is one of four,
    # by what the import can be (``foreign_mode``): possible as it is, ...
    "foreign": {
        "i": "prompt_import_resume",
        "I": "prompt_import_only",
        "t": "prompt_folder",
        **_FOREIGN_KEYS,
    },
    # ... possible into a folder the user chooses, ...
    "foreign_folder": {"t": "prompt_folder", **_FOREIGN_KEYS},
    # ... only as an experimental import (its warning comes first), ...
    "foreign_experimental": {
        "i": "prompt_warn_resume",
        "I": "prompt_warn_only",
        **_FOREIGN_KEYS,
    },
    # ... or not at all.
    "foreign_none": dict(_FOREIGN_KEYS),
    "mount": {"m": "prompt_mount", "escape": "prompt_cancel"},
    # The y/n questions: import, an experimental import after its warning,
    # start a tool, remove an imported copy, remove one that was used since.
    "confirm": _yes_no("confirm_yes"),
    "warn": _yes_no("warn_yes"),
    "start": _yes_no("start_yes"),
    "undo": _yes_no("undo_yes"),
    "undo_used": _yes_no("undo_force_yes"),
    # Continue as new, first step: the tool.
    "tool": {
        "left": "tool_prev",
        "right": "tool_next",
        "enter": "tool_accept",
        "escape": "prompt_cancel",
    },
    # While the folder input has the keyboard it takes every printable key
    # and the ones that edit; Shift-Tab must not carry the focus away.
    "folder": {
        "escape": "folder_cancel",
        "enter": "folder_accept",
        "tab": "folder_complete",
        "shift+tab": "folder_stay",
    },
    # While the filter box has the keyboard it takes every printable key.
    "filter": {
        "escape": "filter_clear",
        "enter": "filter_accept",
        "tab": "filter_accept",
        "up": "cursor_up",
        "down": "cursor_down",
        "pageup": "cursor_pageup",
        "pagedown": "cursor_pagedown",
    },
    "help": {
        "escape": "close",
        "enter": "close",
        "q": "close",
        "?": "close",
        "up": "up",
        "k": "up",
        "down": "down",
        "j": "down",
        "pageup": "pageup",
        "pagedown": "pagedown",
    },
    "pager": {
        "q": "close",
        "escape": "close",
        "up": "up",
        "k": "up",
        "down": "down",
        "j": "down",
        "pageup": "pageup",
        "pagedown": "pagedown",
        "space": "pagedown",
        "home": "home",
        "g": "home",
        "end": "end",
        "G": "end",
    },
}

MODAL_MODES = (
    "foreign",
    "foreign_folder",
    "foreign_experimental",
    "foreign_none",
    "mount",
    "confirm",
    "warn",
    "start",
    "undo",
    "undo_used",
    "tool",
)
"""The prompts: modes in which a key with no meaning is swallowed, not
passed on. (Not ``folder``: the keys that edit its text must reach it.)"""


def key_action(mode: str, key: str, character: str | None = None) -> str | None:
    """The action a key press means in a mode, or None. ``key`` is the key's
    name (``enter``, ``shift+tab``, ``question_mark``), ``character`` what it
    types, if anything; a typed character wins, so ``G`` and ``?`` need no
    knowledge of how a toolkit names them."""
    table = KEYMAPS.get(mode, {})
    if character and character in table:
        return table[character]
    return table.get(key)


HELP = (
    ("↑ ↓ k j", "move the selection"),
    ("PgUp PgDn Home End g G", "move by page, to the top, to the end"),
    ("← → h l Tab Shift-Tab", "switch provider"),
    ("1-9, 0", "provider by number; 0 is All"),
    ("Enter", "resume in a new terminal window and quit; a session on another drive asks first"),
    ("o", "the same, and stay here"),
    ("i", "import a session from another drive to Linux (asks y/n)"),
    ("t", "import it into a folder you choose (Tab completes, ~ is your home folder)"),
    ("c", "continue as a new session: choose the tool and the folder; asks before it starts"),
    ("u", "undo an import: remove the imported copy (asks y/n)"),
    ("m", "mount the drive of an offline session (the desktop asks for the password)"),
    ("v", "view the transcript (q or Esc returns)"),
    ("/", "filter by title, project and folder; Esc clears"),
    ("r", "rescan the known stores"),
    ("R", "rescan every drive; Esc cancels"),
    ("?", "this help"),
    ("q, Ctrl-C", "quit"),
    ("Mouse", "click a provider or a row, wheel scrolls, double-click resumes"),
)


def help_lines(width: int | None = None) -> list[str]:
    """The help overlay as plain lines no wider than ``width``: keys and
    action side by side when there is room, the action under its keys (and
    wrapped) when there is not."""
    keys_width = max(text_width(keys) for keys, _ in HELP)
    gap = 3
    if width is None:
        return [f"{pad(keys, keys_width)}{' ' * gap}{action}" for keys, action in HELP]
    lines: list[str] = []
    room = width - keys_width - gap
    if room >= 24:
        for keys, action in HELP:
            for n, part in enumerate(wrap_line(action, room)):
                lines.append(f"{pad(keys if n == 0 else '', keys_width)}{' ' * gap}{part}")
        return lines
    for keys, action in HELP:
        lines.append(clip(keys, width))
        lines.extend("  " + part for part in wrap_line(action, max(width - 2, 1)))
    return lines


# --- transcript pager -------------------------------------------------------

ROLE_LABELS = {"user": "User", "assistant": "Assistant", "tool": "Tool"}


def _split_wide(word: str, width: int) -> tuple[str, str]:
    """Cut a word that is wider than a line: (what fits, the rest). Always
    takes at least one character so wrapping ends."""
    head = _take(word, width) or word[:1]
    return head, word[len(head) :]


def wrap_line(line: str, width: int) -> list[str]:
    """One paragraph wrapped to ``width`` cells at spaces; a word longer than
    a line is cut. Leading indentation stays on the first line."""
    if width <= 0 or text_width(line) <= width:
        return [line]
    out: list[str] = []
    current, used = "", 0
    for token in re.findall(r"\s+|\S+", line):
        size = text_width(token)
        if used + size <= width:
            current, used = current + token, used + size
            continue
        if token.isspace():
            if current.strip():
                out.append(current.rstrip())
            current, used = "", 0
            continue
        if current.strip():
            out.append(current.rstrip())
            current, used = "", 0
        elif used + 1 > width:
            current, used = "", 0  # indentation as wide as the line: drop it
        while used + text_width(token) > width:
            head, token = _split_wide(token, width - used)
            out.append(current + head)
            current, used = "", 0
        current, used = current + token, used + text_width(token)
    if current.strip() or not out:
        out.append(current.rstrip())
    return out


def wrap_text(text: str, width: int) -> list[str]:
    """Text wrapped to ``width`` cells, keeping its own line breaks."""
    lines: list[str] = []
    for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.extend(wrap_line(clean(line.expandtabs(4)).rstrip(), width))
    return lines


def transcript_lines(
    role: str,
    text: object,
    ts: object,
    width: int,
    tz: tzinfo | None = None,
    max_chars: int = PAGER_MAX_TURN_CHARS,
) -> list[tuple[str, str]]:
    """One turn as pager lines, each ``(kind, text)``: a ``role-<role>``
    heading, wrapped ``text`` lines, a closing ``blank``. A tool turn is its
    heading alone (the tool's name)."""
    role = role if role in ROLE_LABELS else "assistant"
    text = "" if text is None else str(text)
    when = local_time(ts, tz) if ts else ""
    if role == "tool":
        heading = f"{ROLE_LABELS[role]}  {clip(clean(text).strip() or EMPTY, max(width - 6, 1))}"
        return [(f"role-{role}", clip(heading, width)), ("blank", "")]
    heading = f"{ROLE_LABELS[role]}  {when}".rstrip()
    lines = [(f"role-{role}", clip(heading, width))]
    extra = len(text) - max_chars
    if extra > 0:
        text = text[:max_chars]
    lines += [("text", line) for line in wrap_text(text.strip("\n"), width)]
    if extra > 0:
        lines.append(("note", clip(f"[{ELLIPSIS} {extra:,} more characters not shown]", width)))
    lines.append(("blank", ""))
    return lines


def pager_status(turns: int, loading: bool, capped: bool, error: str | None = None) -> str:
    """The pager's status line."""
    if error:
        return f"{plural(turns, 'turn')} · stopped: {error}"
    if capped:
        return f"showing first {plural(turns, 'turn')}"
    if loading:
        return f"loading{ELLIPSIS} {plural(turns, 'turn')}"
    return plural(turns, "turn") if turns else "no turns in this session"


PAGER_HINTS = "↑↓ PgUp PgDn Home End scroll  q back"
