"""The Evangelion (NERV / MAGI) presentation layer: pure helpers, no Textual.

The look follows what the screens in the series do, not just their colours:

- colour is a signal, never decoration: orange is the system speaking (frames,
  labels), green is nominal or live, red is alert, cyan is external data;
- state is shown by identical, numbered units that change colour (PHASE 10 to
  17, PARTITION #1 to #5): rows are numbered, providers are numbered plates;
- English and kanji are paired on labels (内部 INTERNAL, 警告 WARNING);
- a decision is a MAGI vote: three units, each 承認 (approve) or 否決 (reject);
- a crisis takes the screen over: a full-width band with hazard stripes;
- subjects are plates in a column, each with a trace beside it; a label is
  tied to what it names by a leader line; the one reading that matters is
  drawn huge, in seven-segment numerals;
- the screen is a cathode-ray tube: pictures are painted, never eased in
  (``crt.py`` does the painting; the shapes are here).

Everything here is a pure function of its arguments so it is unit-tested
without Textual; ``app.py`` draws it and ``app.tcss`` colours it.
"""

from __future__ import annotations

import dataclasses
import math
import os
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol

from .. import fmt
from . import view

# Switch every transition off: AGENT_SESSIONS_MOTION=0 (the tests do; so may a
# slow terminal or anyone who wants a still screen).
MOTION_ENV = "AGENT_SESSIONS_MOTION"

BRAND = "NERV"
BRAND_SUB = "特務機関"  # "special agency": the line under NERV's own logo
SITE = "CENTRAL DOGMA ─ SESSION MONITOR"
BOARD_TITLE = "SESSION LOG ─ 記録"
DETAIL_TITLE = "DATA"
MAGI_TITLE = "MAGI ─ 審議"
HELP_TITLE = "操作マニュアル OPERATION MANUAL"
PAGER_TITLE = "通信記録 COMMUNICATION LOG"

# The three MAGI units and what each one rules on for the selected session.
MAGI_UNITS = (("MELCHIOR·1", "RESUME"), ("BALTHASAR·2", "IMPORT"), ("CASPER·3", "DRIVE"))
APPROVE = "承認"  # approve
REJECT = "否決"  # reject
PENDING = "審議中"  # under deliberation (the MAGI screen's own word)
CONDITIONAL = "条件付"  # approved with a condition
NOT_APPLICABLE = "──"

# How a prompt is announced: routine choices in an orange frame, anything
# that cannot be taken back, or that talks to a model, under a red band.
DANGER_MODES = frozenset({"warn", "undo", "undo_used", "start"})
WARNING_LABEL = "警告 WARNING"
CAUTION_LABEL = "注意 CAUTION"

GUTTER_MIN_WIDTH = 80  # below this the row numbers give their room to TITLE
GUTTER = 6  # "001▶▌ ": number, selection mark, signal mark
MAGI_MIN_WIDTH = 110  # below this the MAGI panel gives its room to the data


def motion_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether transitions run: on unless ``AGENT_SESSIONS_MOTION`` is
    ``0``, ``false``, ``no`` or ``off``."""
    value = (os.environ if environ is None else environ).get(MOTION_ENV, "")
    return value.strip().lower() not in ("0", "false", "no", "off")


# --- the list -----------------------------------------------------------------


def tab_number(tab: view.Tab) -> str:
    """A subject's number: its digit key in two digits (All is 00)."""
    return f"{tab.digit or 0:0>2}"[-2:]


def row_number(index: int) -> str:
    """A row's number as the screens number their units: three digits."""
    return f"{index + 1:03d}" if index < 999 else f"{index + 1}"[-3:]


def gutter_text(index: int, selected: bool) -> str:
    """The cells in front of a row: its number, and the selection mark."""
    return f"{row_number(index)}{'▶' if selected else ' '} "


def signal(row: view.Row) -> str:
    """What a row's LOCATION reads as: ``internal`` (this system's store),
    ``external`` (another drive, mounted), ``imported`` (another drive, with
    a copy here) or ``lost`` (its drive is not mounted: SIGNAL LOST)."""
    if getattr(row, "offline", False):
        return "lost"
    if not row.foreign:
        return "internal"
    return "imported" if row.imported else "external"


def reveal_step(height: int, frames: int = 6) -> int:
    """Rows a wiping scan line uncovers per frame: the whole panel in about
    ``frames`` frames, whatever its height."""
    return max(1, -(-max(height, 1) // max(frames, 1)))


SIGNAL_MARKS = {"internal": "▌", "external": "▌", "imported": "▌", "lost": "░"}
STATIC = "░▒▓"


def signal_mark(state: str, rng: random.Random | None = None) -> str:
    """The one cell after a row's number that shows where it lives; a lost
    signal is static, and with ``rng`` the static moves."""
    if state == "lost" and rng is not None:
        return rng.choice(STATIC)
    return SIGNAL_MARKS.get(state, " ")


def leader_cell(y: int, row: int, target: int, width: int) -> str:
    """Line ``y`` of the leader from the selected ``row`` (entering at the
    left) to the ``target`` line (leaving at the right): out, along, in."""
    width = max(width, 1)
    bend = width // 2
    low, high = min(row, target), max(row, target)
    if not low <= y <= high:
        return " " * width
    if row == target:
        return "━" * width
    if y == row:
        corner = "┓" if row < target else "┛"
        return ("━" * bend + corner).ljust(width)
    if y == target:
        corner = "┗" if row < target else "┏"
        return (" " * bend + corner).ljust(width, "━")
    return (" " * bend + "┃").ljust(width)


# --- prompts --------------------------------------------------------------------


def severity(mode: str) -> str:
    """``danger`` for a prompt whose answer cannot be taken back or sends a
    message to a model, ``caution`` for the rest."""
    return "danger" if mode in DANGER_MODES else "caution"


STRIPE = "▚▚▚  "  # one period of a hazard stripe; it marches by shifting


def stripe_parts(width: int, phase: int = 0, label: str = "") -> tuple[str, str, str]:
    """A hazard band ``width`` cells wide as ``(stripes, label, stripes)``:
    diagonal stripes shifted by ``phase`` (the march), the label cut into
    the middle when it fits (else the label part is empty)."""
    width = max(width, 0)
    shift = phase % len(STRIPE)
    body = (STRIPE * (width // len(STRIPE) + 2))[shift : shift + width]
    tag = f" {label} " if label else ""
    size = view.text_width(tag)
    if not tag or size + 4 > width:
        return body, "", ""
    left = (width - size) // 2
    return body[:left], tag, body[left + size : width]


def stripe_line(width: int, phase: int = 0, label: str = "") -> str:
    """``stripe_parts`` as one line of text."""
    return "".join(stripe_parts(width, phase, label))


EMERGENCY_LABEL = "非常事態 EMERGENCY"


def ticks_parts(width: int, label: str = "") -> tuple[str, str, str]:
    """The dark beat of a hazard band: black, a tick an eighth of the way in
    from each side, the label in the middle (``stripe_parts``' shape)."""
    width = max(width, 0)
    body = [" "] * width
    for x in (width // 8, width - 1 - width // 8):
        if 0 <= x < width:
            body[x] = "▮"
    line = "".join(body)
    tag = f" {label} " if label else ""
    size = view.text_width(tag)
    if not tag or size + 4 > width:
        return line, "", ""
    left = (width - size) // 2
    return line[:left], tag, line[left + size :]


SIGNAL_LOST = "SIGNAL LOST"


def drawn_row(row: view.Row) -> view.Row:
    """The row as the list draws it (``shown_where``); the row itself, and
    every prompt and message made from it, keep the engine's words."""
    if not getattr(row, "offline", False):
        return row
    return dataclasses.replace(row, where=shown_where(row))


def shown_where(row: view.Row) -> str:
    """LOCATION as the list draws it: a drive that is not mounted reads
    SIGNAL LOST, as a lost camera feed does on the screens."""
    if getattr(row, "offline", False):
        drive = view.drive_of(row.where)
        return f"{drive} {SIGNAL_LOST}" if drive else SIGNAL_LOST
    return row.where


# --- the MAGI panel ---------------------------------------------------------------


class _Session(Protocol):
    origin: str
    launchable: bool
    imported_as: str | None
    offline: bool


@dataclass(frozen=True)
class Vote:
    unit: str  # MELCHIOR·1, …
    subject: str  # RESUME, IMPORT, DRIVE
    verdict: str  # one of APPROVE, REJECT, PENDING, CONDITIONAL, NOT_APPLICABLE
    state: str  # approve | reject | pending | conditional | none: the colour
    note: str = ""  # why, in a word or two


def magi_votes(session: _Session | None, verdict: view.Verdict | None) -> list[Vote]:
    """The three votes for the selected session, from what the engine said
    about it: can it be resumed, can it be imported, is its drive there."""
    if session is None:
        return [Vote(unit, subject, NOT_APPLICABLE, "none") for unit, subject in MAGI_UNITS]
    offline = bool(getattr(session, "offline", False))
    native = session.origin == "native"
    (u1, s1), (u2, s2), (u3, s3) = MAGI_UNITS

    if native and session.launchable:
        resume = Vote(u1, s1, APPROVE, "approve", "READY")
    elif not native and session.imported_as:
        resume = Vote(u1, s1, APPROVE, "approve", "VIA COPY")
    elif native:
        resume = Vote(u1, s1, REJECT, "reject", "NO TOOL")
    elif offline:
        resume = Vote(u1, s1, REJECT, "reject", "NO SIGNAL")
    else:
        resume = Vote(u1, s1, REJECT, "reject", "IMPORT FIRST")

    if native:
        imp = Vote(u2, s2, NOT_APPLICABLE, "none", "INTERNAL")
    elif session.imported_as:
        imp = Vote(u2, s2, APPROVE, "approve", "DONE")
    elif offline:
        imp = Vote(u2, s2, REJECT, "reject", "NO SIGNAL")
    elif verdict is None:
        imp = Vote(u2, s2, PENDING, "pending")
    elif not verdict.known:
        imp = Vote(u2, s2, REJECT, "reject", "UNKNOWN")
    elif verdict.possible:
        imp = Vote(u2, s2, APPROVE, "approve", "READY")
    elif verdict.needs_folder:
        imp = Vote(u2, s2, CONDITIONAL, "conditional", "FOLDER")
    elif verdict.experimental:
        imp = Vote(u2, s2, CONDITIONAL, "conditional", "EXPERIMENTAL")
    else:
        imp = Vote(u2, s2, REJECT, "reject", "REFUSED")

    if offline:
        drive = Vote(u3, s3, REJECT, "reject", "SIGNAL LOST")
    else:
        drive = Vote(u3, s3, APPROVE, "approve", "INTERNAL" if native else "EXTERNAL")
    return [resume, imp, drive]


def deliberating(vote: Vote) -> Vote:
    """The vote while the unit is still deliberating (a unit with nothing to
    rule on stays out of it)."""
    if vote.state == "none":
        return vote
    return dataclasses.replace(vote, verdict=PENDING, state="pending", note="")


def magi_line(vote: Vote, width: int) -> list[tuple[str, str]]:
    """One vote as ``(part, text)`` pieces for the panel: ``unit``,
    ``subject``, ``verdict``, ``note``; fitted to ``width`` cells (the note
    goes first, then the subject)."""
    unit, subject = f"{vote.unit:<12}", f"{vote.subject:<7}"
    plate = f" {vote.verdict} "
    note = f" {vote.note}" if vote.note else ""
    parts = [("unit", unit), ("subject", subject), ("verdict", plate), ("note", note)]
    for drop in ("note", "subject"):
        if sum(view.text_width(text) for _, text in parts) <= width:
            break
        parts = [part for part in parts if part[0] != drop]
    return parts


# --- the subject column: providers as numbered plates --------------------------------

SUBJECT_TITLE = "SUBJECT ─ 被験体"
TARGET_TITLE = "TARGET ─ 標的"
SUBJECT_MIN_WIDTH = 160  # below this the providers are a strip of plates over the list
TARGET_MIN_WIDTH = 135  # below this the target readout goes under the list
TARGET_TALL = 34  # below this many lines the readout drops its clock
SUBJECT_WIDTH = 24
TRACE_DAYS = 18  # days of activity a subject's trace shows, today at the right
TRACE = "▁▂▃▄▅▆▇█"

# Seven-segment numerals, three lines high, as on the screens' countdowns.
SEGMENTS = {
    "0": ("┏━┓", "┃ ┃", "┗━┛"),
    "1": ("  ╻", "  ┃", "  ╹"),
    "2": ("╺━┓", "┏━┛", "┗━╸"),
    "3": ("╺━┓", " ━┫", "╺━┛"),
    "4": ("╻ ╻", "┗━┫", "  ╹"),
    "5": ("┏━╸", "┗━┓", "╺━┛"),
    "6": ("┏━╸", "┣━┓", "┗━┛"),
    "7": ("╺━┓", "  ┃", "  ╹"),
    "8": ("┏━┓", "┣━┫", "┗━┛"),
    "9": ("┏━┓", "┗━┫", "╺━┛"),
    ":": (" ", "▪", "▪"),
    " ": ("   ", "   ", "   "),
}


def segments(text: str, gap: int = 1) -> tuple[str, str, str]:
    """``text`` (digits, ``:`` and spaces) in seven-segment numerals: three
    lines of equal width."""
    rows = ["", "", ""]
    for number, char in enumerate(text):
        glyph = SEGMENTS.get(char, SEGMENTS[" "])
        for line in range(3):
            rows[line] += (" " * gap if number else "") + glyph[line]
    return rows[0], rows[1], rows[2]


def activity(stamps: Sequence[str | None], now: datetime, days: int = TRACE_DAYS) -> list[int]:
    """Sessions per day over the last ``days`` days, oldest first: a day is
    counted by the time each session was last active (``Row.stamp``)."""
    counts = [0] * days
    today = now.astimezone(UTC).date()
    for stamp in stamps:
        when = fmt.parse_stamp(stamp)
        if when is None:
            continue
        back = (today - when.astimezone(UTC).date()).days
        if 0 <= back < days:
            counts[days - 1 - back] += 1
    return counts


def trace(counts: Sequence[int], peak: int | None = None) -> str:
    """Counts as a bar trace: ``·`` for an empty day, the bars scaled to
    ``peak`` (the largest count by default)."""
    top = max(peak if peak is not None else max(counts, default=0), 1)
    out = []
    for count in counts:
        if count <= 0:
            out.append("·")
        else:
            out.append(TRACE[min(len(TRACE) - 1, (count * len(TRACE) - 1) // top)])
    return "".join(out)


@dataclass(frozen=True)
class SubjectLine:
    """One line of the subject column: ``(style, text)`` pieces, and the
    tab a click on it selects (-1: none)."""

    parts: tuple[tuple[str, str], ...]
    tab: int = -1


def subject_lines(
    tabs: Sequence[view.Tab], active: int, traces: dict[str, str], width: int
) -> list[SubjectLine]:
    """The subject column: the selected provider as a framed plate with its
    number in segments, every other one as a numbered line over its trace,
    All under a rule, then the legend of the signal marks."""
    width = max(width, 12)
    inner = width - 2
    lines: list[SubjectLine] = []

    def put(index: int, *parts: tuple[str, str]) -> None:
        lines.append(SubjectLine(tuple(parts), index))

    providers = [(i, tab) for i, tab in enumerate(tabs) if tab.name != view.ALL]
    total = [(i, tab) for i, tab in enumerate(tabs) if tab.name == view.ALL]
    for index, tab in providers:
        number = tab_number(tab)
        name = view.clip(tab.label.upper(), max(width - 9, 4))
        bars = (traces.get(tab.name) or "·" * TRACE_DAYS)[-max(inner - 2, 1) :]
        if index == active:
            digits = segments(number)
            room = inner - 3 - len(digits[0])
            put(index, ("frame", "┏" + "━" * inner + "┓"))
            put(
                index,
                ("frame", "┃ "),
                ("digits", digits[0]),
                ("frame", "  "),
                ("name", view.clip(name, room).ljust(room)),
                ("frame", "┃"),
            )
            put(
                index,
                ("frame", "┃ "),
                ("digits", digits[1]),
                ("frame", "  "),
                ("count", f"{tab.count:03d}".ljust(room)),
                ("frame", "┃"),
            )
            put(
                index,
                ("frame", "┃ "),
                ("digits", digits[2]),
                ("frame", "  "),
                ("label", "SESSIONS".ljust(room)),
                ("frame", "┃"),
            )
            put(index, ("frame", "┃ "), ("trace-active", bars.ljust(inner - 2)), ("frame", " ┃"))
            put(index, ("frame", "┗" + "━" * inner + "┛"))
        else:
            count = f"{tab.count:03d}"
            gap = max(width - 2 - len(number) - 1 - view.text_width(name) - len(count) - 1, 1)
            put(
                index,
                ("number", f" {number} "),
                ("plate", name),
                ("frame", " " * gap),
                ("count", count),
                ("frame", " "),
            )
            put(index, ("frame", "    "), ("trace", bars))
    for index, tab in total:
        put(-1, ("rule", " " + "─" * (width - 2)))
        count = f"{tab.count:03d}"
        label = tab_number(tab)
        name = "ALL"
        if index == active:
            put(index, ("cursor", f" {label} {name}".ljust(width - len(count) - 1) + count + " "))
        else:
            gap = max(width - 2 - len(label) - 1 - len(name) - len(count) - 1, 1)
            put(
                index,
                ("number", f" {label} "),
                ("plate", name),
                ("frame", " " * gap),
                ("count", count),
                ("frame", " "),
            )
    put(-1)
    put(
        -1,
        ("frame", " "),
        ("internal", "▌"),
        ("legend", "INTERNAL  "),
        ("external", "▌"),
        ("legend", "EXTERNAL"),
    )
    put(-1, ("frame", " "), ("lost", "░"), ("legend", "SIGNAL LOST"))
    return lines


# --- the target readout ---------------------------------------------------------------

PLATES = {
    "internal": ("内部", "INTERNAL"),
    "external": ("外部", "EXTERNAL"),
    "imported": ("外部", "EXTERNAL · COPY HERE"),
    "lost": ("信号途絶", "SIGNAL LOST"),
    "none": ("待機", "STANDBY"),
}
CLOCK_LABEL = "ELAPSED SINCE LAST CONTACT"
CLOCK_UNITS = ("DAY", "HOUR", "MIN")


def plate_parts(state: str, width: int) -> tuple[str, str, str]:
    """The readout's state plate: ``(kanji, english, slashes)``, the hazard
    slashes filling the line out to ``width``."""
    kanji, english = PLATES.get(state, PLATES["none"])
    kanji = f" {kanji} "
    english = f" {english} "
    used = view.text_width(kanji) + view.text_width(english) + 1
    return kanji, english, " " + "╱" * max(width - used, 0)  # noqa: RUF001


def elapsed(stamp: object, now: datetime) -> tuple[int, int, int] | None:
    """Days, hours and minutes since ``stamp``; None when it is no time.
    A time in the future is no time at all past."""
    then = fmt.parse_stamp(stamp)
    if then is None:
        return None
    seconds = max(int((now - then).total_seconds()), 0)
    days, rest = divmod(seconds, 86400)
    return min(days, 999), rest // 3600, rest % 3600 // 60


def clock_text(parts: tuple[int, int, int] | None) -> str:
    """The countdown's digits: ``DDD:HH:MM`` (dashes become blanks)."""
    if parts is None:
        return "   :  :  "
    days, hours, minutes = parts
    return f"{days:03d}:{hours:02d}:{minutes:02d}"


def rolled(text: str, rng: random.Random) -> str:
    """``text`` with every digit replaced by a random one: a counter that
    has not settled yet."""
    return "".join(rng.choice("0123456789") if char.isdigit() else char for char in text)


# --- the MAGI as the screens draw it ------------------------------------------------------

RESOLUTION = "決議"
PASSED = "可決"
REJECTED = "否決"
MAGI_SHAPE_WIDTH = 38  # the three panels need this many cells
MAGI_SHAPE_HEIGHT = 12


def resolution(votes: Sequence[Vote]) -> tuple[str, str]:
    """The MAGI's resolution: ``(word, state)``. Still deliberating while a
    vote is pending; passed when the votes cast approve (a conditional
    approval counts) more often than they reject."""
    if any(vote.state == "pending" for vote in votes):
        return PENDING, "pending"
    cast = [vote for vote in votes if vote.state != "none"]
    yes = sum(1 for vote in cast if vote.state in ("approve", "conditional"))
    if not cast:
        return NOT_APPLICABLE, "none"
    return (PASSED, "approve") if yes * 2 > len(cast) else (REJECTED, "reject")


def _box(vote: Vote, width: int) -> list[list[tuple[str, str]]]:
    """One MAGI unit as six lines: a frame filled with its verdict."""
    fill = f"fill-{vote.state}"
    inner = width - 2
    out = [[(fill, "┌" + "─" * inner + "┐")]]
    for text in (vote.unit, vote.subject, vote.verdict, vote.note):
        text = view.clip(text, inner)
        pad = inner - view.text_width(text)
        left = pad // 2
        out.append([(fill, "│" + " " * left + text + " " * (pad - left) + "│")])
    out.append([(fill, "└" + "─" * inner + "┘")])
    return out


def magi_shape(votes: Sequence[Vote], width: int) -> list[list[tuple[str, str]]]:
    """The MAGI as three panels round their name, BALTHASAR·2 above,
    CASPER·3 and MELCHIOR·1 below, the resolution between them: lines of
    ``(style, text)`` pieces, ``MAGI_SHAPE_WIDTH`` cells wide at most."""
    by_unit = {vote.unit.split("·")[0]: vote for vote in votes}
    melchior, balthasar, casper = (
        by_unit.get("MELCHIOR"),
        by_unit.get("BALTHASAR"),
        by_unit.get("CASPER"),
    )
    if melchior is None or balthasar is None or casper is None:
        return []
    side, gap, top_width = 15, 8, 19
    body = side * 2 + gap
    left = max((width - body) // 2, 0)
    lines: list[list[tuple[str, str]]] = []
    offset = left + (body - top_width) // 2
    for row in _box(balthasar, top_width):
        lines.append([("blank", " " * offset), *row])
    word, state = resolution(votes)
    middle = {
        1: ("name", "MAGI"),
        3: ("label", RESOLUTION),
        4: (f"word-{state}", f" {word} "),
    }
    for number, (a, b) in enumerate(zip(_box(casper, side), _box(melchior, side), strict=True)):
        style, text = middle.get(number, ("blank", ""))
        pad = gap - view.text_width(text)
        lines.append(
            [
                ("blank", " " * left),
                *a,
                ("blank", " " * (pad // 2)),
                (style, text),
                ("blank", " " * (pad - pad // 2)),
                *b,
            ]
        )
    return lines


# --- the boot log ------------------------------------------------------------------------

BOOT_TITLE = "MAGI SYSTEM BOOT ─ 起動"


@dataclass(frozen=True)
class BootFacts:
    """What the boot log reports once the index is read."""

    sessions: int
    drives: int
    lost: int
    subjects: tuple[tuple[str, int], ...] = ()  # (label, sessions) per provider


def boot_lines(version: str, facts: BootFacts | None) -> list[tuple[str, str]]:
    """What the boot log prints, as ``(state, text)``: ``head``, ``line``,
    ``ok``, ``wait``, ``lost`` and ``done``. Without ``facts`` (the index is
    still being read) it stops at the line that waits for it; with them it
    says what was found: drives, sessions per subject, signals lost."""
    dots = "." * 22
    lines = [
        ("head", f"NERV 特務機関  {BOOT_TITLE}"),
        ("line", f"AGENT-SESSIONS  Ver.{version}"),
        ("line", ""),
        ("ok", f"MELCHIOR-1 {dots[:16]} ONLINE"),
        ("ok", f"BALTHASAR-2 {dots[:15]} ONLINE"),
        ("ok", f"CASPER-3 {dots[:18]} ONLINE"),
        ("line", ""),
    ]
    if facts is None:
        lines.append(("wait", f"INDEX {dots} READING"))
        return lines
    lines.append(("ok", f"INDEX {dots} {facts.sessions:>4} SESSIONS"))
    lines.append(("ok", f"DRIVES {dots[:21]} {facts.drives:>4} MOUNTED"))
    lines.append(("lost" if facts.lost else "ok", f"SIGNAL LOST {dots[:16]} {facts.lost:>4}"))
    for number, (label, count) in enumerate(facts.subjects, 1):
        name = f"SUBJECT {number:02d} {label.upper()} "
        lines.append(("line", f"{name}{'.' * max(27 - len(name), 1)} {count:>4}"))
    lines.append(("line", ""))
    lines.append(("done", "ALL SYSTEMS NOMINAL ─ 起動完了"))
    return lines


# --- the lifespan track and the message meter (the list) ------------------------------------

TRACK_MIN = 24  # cells of track below which the list has none
TITLE_ROOM = 52  # what TITLE keeps when a track takes the rest
METER_UNITS = 10
TRACK_TAIL = 2 + METER_UNITS + 1 + 4 + 1  # "  ▮▮▮▮▯▯▯▯▯▯ 1234 " after the track
TRACK_MAX_DAYS = 90  # the track spans at most this many days back


@dataclass(frozen=True)
class Span:
    """The time the track draws, ``start`` to ``end`` (now), on a log scale
    of age: the last hours take as much room as the weeks before them."""

    start: datetime
    end: datetime

    def column(self, when: datetime, width: int) -> int:
        oldest = max((self.end - self.start).total_seconds() / 3600, 1.0)
        age = min(max((self.end - when).total_seconds() / 3600, 0.0), oldest)
        part = 1 - math.log1p(age) / math.log1p(oldest)
        return min(max(int(part * (width - 1) + 0.5), 0), width - 1)


TRACK_MARKS = (
    ("1H", timedelta(hours=1)),
    ("6H", timedelta(hours=6)),
    ("1D", timedelta(days=1)),
    ("3D", timedelta(days=3)),
    ("1W", timedelta(weeks=1)),
    ("2W", timedelta(weeks=2)),
    ("1MO", timedelta(days=30)),
    ("2MO", timedelta(days=60)),
)


def track_span(rows: Sequence[view.Row], now: datetime) -> Span:
    """From the earliest start among ``rows`` (at most ``TRACK_MAX_DAYS``
    back, at least a day) to now."""
    earliest = now - timedelta(days=1)
    floor = now - timedelta(days=TRACK_MAX_DAYS)
    for row in rows:
        when = fmt.parse_stamp(row.started or row.stamp)
        if when is not None and when < earliest:
            earliest = when
    return Span(max(earliest, floor), now)


def _marks(span: Span) -> list[tuple[str, datetime]]:
    return [(label, span.end - ago) for label, ago in TRACK_MARKS if span.end - ago > span.start]


def track_cells(row: view.Row, span: Span, width: int) -> list[tuple[str, str]]:
    """A row's lifespan on the track, ``width`` cells: ``(kind, text)``
    pieces, ``grid`` (the empty track, dotted at each mark of the ruler),
    ``bar`` (from its start to its last activity), ``head`` (the last
    activity)."""
    width = max(width, 1)
    cells = ["grid"] * width
    text = [" "] * width
    for _label, when in _marks(span):
        text[span.column(when, width)] = "·"
    end = fmt.parse_stamp(row.stamp)
    begin = fmt.parse_stamp(row.started) or end
    if end is not None and begin is not None:
        a, b = span.column(min(begin, end), width), span.column(end, width)
        for x in range(a, b):
            cells[x], text[x] = "bar", "━"
        cells[b], text[b] = "head", "▮"
        if begin < span.start:
            cells[0], text[0] = "bar", "◂"
    out: list[tuple[str, str]] = []
    for kind, char in zip(cells, text, strict=True):
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + char)
        else:
            out.append((kind, char))
    return out


def track_ruler(span: Span, width: int) -> str:
    """The track's heading: the marks (1H, 6H, 1D, …) where they fall,
    NOW at its right end; a mark that would touch another is left out."""
    width = max(width, 1)
    line = [" "] * width
    line[width - 3 :] = "NOW"[-min(width, 3) :]
    for label, when in _marks(span):
        x = span.column(when, width)
        x = min(max(x - len(label) // 2, 0), width - len(label))
        if all(c == " " for c in line[max(x - 1, 0) : x + len(label) + 1]):
            line[x : x + len(label)] = label
    return "".join(line)[:width]


def meter(messages: int | None, peak: int, units: int = METER_UNITS) -> tuple[str, str]:
    """A message count as identical units: ``(lit, unlit)``, on a log scale
    against ``peak`` (the most messages among the rows)."""
    if not messages or messages <= 0:
        return "", "▯" * units
    top = max(peak, messages, 1)
    lit = max(1, min(units, math.ceil(units * math.log1p(messages) / math.log1p(top))))
    return "▮" * lit, "▯" * (units - lit)


def meter_count(messages: int | None) -> str:
    """The count after the meter, four cells."""
    if messages is None:
        return "  --"
    return f"{messages:>4}" if messages < 10000 else f"{messages // 1000:>3}k"


# --- the left column: activity cells and the drive board ---------------------------------------

ACTIVITY_TITLE = "ACTIVITY ─ 活動"
DRIVES_TITLE = "DRIVES ─ 記憶装置"
WEEKDAYS = "MTWTFSS"


def activity_grid(stamps: Sequence[str | None], now: datetime, weeks: int) -> list[list[int]]:
    """Sessions per day for the last ``weeks`` weeks as seven rows (Monday
    first) of ``weeks`` columns, this week at the right; days still to
    come are -1."""
    weeks = max(weeks, 1)
    grid = [[0] * weeks for _ in range(7)]
    today = now.astimezone(UTC).date()
    monday = today - timedelta(days=today.weekday())
    first = monday - timedelta(weeks=weeks - 1)
    for day in range(7):
        for week in range(weeks):
            if first + timedelta(days=week * 7 + day) > today:
                grid[day][week] = -1
    for stamp in stamps:
        when = fmt.parse_stamp(stamp)
        if when is None:
            continue
        date = when.astimezone(UTC).date()
        offset = (date - first).days
        if 0 <= offset < weeks * 7 and date <= today:
            grid[offset % 7][offset // 7] += 1
    return grid


def activity_lines(grid: list[list[int]], width: int, label: str) -> list[list[tuple[str, str]]]:
    """The activity cells: a heading, then a row a weekday, a cell a day,
    lit in four steps (``cell-0`` to ``cell-4``; ``cell-x`` for days to
    come)."""
    peak = max((count for row in grid for count in row), default=0)
    weeks = len(grid[0]) if grid else 0
    total = sum(count for row in grid for count in row if count > 0)
    lines: list[list[tuple[str, str]]] = [
        [("title", view.clip(f" {ACTIVITY_TITLE}", width))],
        [("note", view.clip(f" {label} · {weeks}W · {total:03d}", width))],
    ]
    for day, row in enumerate(grid):
        parts: list[tuple[str, str]] = [("note", f" {WEEKDAYS[day]} ")]
        for count in row:
            if count < 0:
                parts.append(("cell-x", "  "))
                continue
            level = 0 if count == 0 else 1 + min(3, (count * 4 - 1) // max(peak, 1))
            parts.append((f"cell-{level}", "■ " if level else "· "))
        lines.append(parts)
    return lines


@dataclass(frozen=True)
class Drive:
    """One drive on the board."""

    label: str
    fstype: str
    device: str
    letter: str
    state: str  # mounted | attached | absent
    sessions: int


DRIVE_STATES = {
    "mounted": ("online", "ONLINE"),
    "attached": ("standby", "STANDBY"),
    "absent": ("lost", "信号途絶"),
}


def drive_lines(drives: Sequence[Drive], width: int) -> list[list[tuple[str, str]]]:
    """The drive board: a plate a drive, numbered as the screens number
    their units (D-01 …): its name and state, then its filesystem, letter
    and the sessions it holds."""
    width = max(width, 16)
    lines: list[list[tuple[str, str]]] = [[("title", view.clip(f" {DRIVES_TITLE}", width))]]
    for number, drive in enumerate(drives, 1):
        kind, word = DRIVE_STATES.get(drive.state, ("lost", drive.state.upper()))
        plate = f" {word} "
        name_room = width - 6 - view.text_width(plate) - 1
        name = view.clip(drive.label.upper(), max(name_room, 3))
        gap = max(width - 6 - view.text_width(name) - view.text_width(plate), 1)
        lines.append(
            [("number", f" D-{number:02d} "), ("name", name), ("blank", " " * gap), (kind, plate)]
        )
        facts = " · ".join(part for part in (drive.fstype, drive.letter) if part)
        tail = f"{drive.sessions:03d} SES"
        gap = max(width - 6 - view.text_width(facts) - len(tail), 1)
        lines.append([("blank", "      "), ("note", facts), ("blank", " " * gap), ("count", tail)])
    return lines


# --- the right column: related sessions, the sync graph, the communication log --------------------

RELATED_TITLE = "RELATED ─ 関連"
SYNC_TITLE = "SYNC GRAPH ─ 同期"
COMM_TITLE = "COMMUNICATION LOG ─ 通信"


@dataclass(frozen=True)
class Related:
    """Another session from the same folder."""

    number: str  # its subject's number, "01"
    provider: str
    title: str
    age: str


def related_lines(
    related: Sequence[Related], folder: str, width: int, limit: int
) -> list[list[tuple[str, str]]]:
    """The other sessions that ran in the same folder, any provider, newest
    first: subject number, provider, title, age."""
    head = f" {RELATED_TITLE} · {len(related):02d}"
    lines: list[list[tuple[str, str]]] = [[("title", view.clip(head, width))]]
    if not related:
        lines.append([("note", view.clip(f" NONE IN {folder}" if folder else " NONE", width))])
        return lines
    for item in related[:limit]:
        provider = view.clip(item.provider.upper(), 8).ljust(8)
        age = f"{item.age:>4}"
        room = max(width - 4 - 9 - 1 - len(age) - 1, 1)
        lines.append(
            [
                ("number", f" {item.number} "),
                ("provider", provider + " "),
                ("text", view.pad(item.title, room)),
                ("age", " " + age),
            ]
        )
    if len(related) > limit:
        lines.append([("note", f" +{len(related) - limit} MORE")])
    return lines


@dataclass(frozen=True)
class Preview:
    """What was read of a transcript for the readout."""

    turns: tuple[tuple[str, str, str | None], ...]  # the last few: (role, first line, ts)
    beats: tuple[tuple[str, str | None], ...]  # every turn read: (role, ts)
    total: int
    capped: bool = False
    error: str | None = None


SYNC_ROWS = 3


def sync_lines(preview: Preview | None, width: int) -> list[list[tuple[str, str]]]:
    """The sync graph: the session's turns over its time, the replies as a
    bar trace three lines high, the user's turns as marks under it, the
    span and the turn count in the heading."""
    width = max(width, 10)
    if preview is None:
        return [[("title", view.clip(f" {SYNC_TITLE}", width))], [("note", " RECEIVING…")]]
    times = [(role, fmt.parse_stamp(ts)) for role, ts in preview.beats]
    stamped = [(role, when) for role, when in times if when is not None]
    if len(stamped) < 2:
        head = f" {SYNC_TITLE} · {preview.total} TURNS"
        return [[("title", view.clip(head, width))], [("note", " NO TIMING IN THIS SESSION")]]
    first, last = stamped[0][1], stamped[-1][1]
    for _role, when in stamped:
        first, last = min(first, when), max(last, when)
    span = Span(first, last if last > first else first + timedelta(seconds=1))
    inner = width - 2
    replies = [0] * inner
    users = [0] * inner
    for role, when in stamped:
        x = span.column(when, inner)
        if role == "user":
            users[x] += 1
        else:
            replies[x] += 1
    peak = max(replies) or 1
    seconds = int((last - first).total_seconds())
    hours, minutes = divmod(seconds // 60, 60)
    took = f"{hours}H{minutes:02d}M" if hours else f"{minutes}M"
    more = "+" if preview.capped else ""
    head = f" {SYNC_TITLE} · {preview.total}{more} TURNS · {took}"
    lines: list[list[tuple[str, str]]] = [[("title", view.clip(head, width))]]
    levels = len(TRACE) * SYNC_ROWS
    for row in range(SYNC_ROWS - 1, -1, -1):
        text = []
        for count in replies:
            height = 0 if not count else max(1, count * levels // peak)
            fill = height - row * len(TRACE)
            text.append(" " if fill <= 0 else TRACE[min(fill, len(TRACE)) - 1])
        lines.append([("grid", " │"), ("trace", "".join(text))])
    marks = "".join("▲" if count else "─" for count in users)
    lines.append([("grid", " └"), ("user", marks)])
    return lines


def comm_lines(
    preview: Preview | None, width: int, rows: int, tz: tzinfo | None = None
) -> list[list[tuple[str, str]]]:
    """The last turns of the session, newest at the bottom, as many as
    ``rows`` lines hold: a speaker plate, the time, the first line."""
    width = max(width, 12)
    lines: list[list[tuple[str, str]]] = [[("title", view.clip(f" {COMM_TITLE}", width))]]
    if preview is None:
        lines.append([("note", " RECEIVING…")])
        return lines
    if preview.error:
        lines.append([("lost", view.clip(f" 信号途絶 {preview.error}", width))])
        return lines
    if not any(role != "tool" for role, _text, _ts in preview.turns):
        lines.append([("note", " NO TRANSMISSIONS")])
        return lines
    body: list[list[tuple[str, str]]] = []
    for role, text, ts in preview.turns:
        if role == "tool":
            continue  # the log is what was said; the sync graph counts the tools
        plate = {"user": "USER", "tool": "TOOL"}.get(role, "AI")
        when = view.local_time(ts, tz)[-5:] if ts else "--:--"
        lead = f"▐{plate:<4}▌ {when} "
        room = max(width - 1 - view.text_width(lead), 1)
        body.append([("blank", " "), (f"role-{role}", lead), ("text", view.pad(text, room))])
    return lines + body[-max(rows - 1, 1) :]


# --- key hints ------------------------------------------------------------------------


def hint_spans(line: str) -> list[tuple[str, str]]:
    """A key-hint line as ``(kind, text)`` pieces, ``kind`` being ``key``,
    ``action`` or ``gap``: hints are separated by two spaces, and the first
    word of each is its key. The text is unchanged; only its parts are
    named, so the key can be drawn apart."""
    spans: list[tuple[str, str]] = []
    for number, item in enumerate(line.split("  ")):
        if not item:
            continue
        if number and spans:
            spans.append(("gap", "  "))
        key, sep, action = item.partition(" ")
        spans.append(("key", key))
        if sep:
            spans.append(("action", sep + action))
    return spans


FRAME = 0.035  # seconds per step of a transition


__all__ = [
    "ACTIVITY_TITLE",
    "APPROVE",
    "BOARD_TITLE",
    "BOOT_TITLE",
    "BRAND",
    "BRAND_SUB",
    "CAUTION_LABEL",
    "CLOCK_LABEL",
    "CLOCK_UNITS",
    "COMM_TITLE",
    "CONDITIONAL",
    "DANGER_MODES",
    "DETAIL_TITLE",
    "DRIVES_TITLE",
    "DRIVE_STATES",
    "EMERGENCY_LABEL",
    "FRAME",
    "GUTTER",
    "GUTTER_MIN_WIDTH",
    "HELP_TITLE",
    "MAGI_MIN_WIDTH",
    "MAGI_SHAPE_HEIGHT",
    "MAGI_SHAPE_WIDTH",
    "MAGI_TITLE",
    "MAGI_UNITS",
    "METER_UNITS",
    "MOTION_ENV",
    "NOT_APPLICABLE",
    "PAGER_TITLE",
    "PASSED",
    "PENDING",
    "PLATES",
    "REJECT",
    "REJECTED",
    "RELATED_TITLE",
    "RESOLUTION",
    "SEGMENTS",
    "SIGNAL_MARKS",
    "SITE",
    "STATIC",
    "SUBJECT_MIN_WIDTH",
    "SUBJECT_TITLE",
    "SUBJECT_WIDTH",
    "SYNC_ROWS",
    "SYNC_TITLE",
    "TARGET_MIN_WIDTH",
    "TARGET_TALL",
    "TARGET_TITLE",
    "TITLE_ROOM",
    "TRACE_DAYS",
    "TRACK_MARKS",
    "TRACK_MAX_DAYS",
    "TRACK_MIN",
    "TRACK_TAIL",
    "WARNING_LABEL",
    "WEEKDAYS",
    "BootFacts",
    "Drive",
    "Preview",
    "Related",
    "Span",
    "SubjectLine",
    "Vote",
    "activity",
    "activity_grid",
    "activity_lines",
    "boot_lines",
    "clock_text",
    "comm_lines",
    "deliberating",
    "drawn_row",
    "drive_lines",
    "elapsed",
    "gutter_text",
    "hint_spans",
    "leader_cell",
    "magi_line",
    "magi_shape",
    "magi_votes",
    "meter",
    "meter_count",
    "motion_enabled",
    "plate_parts",
    "related_lines",
    "resolution",
    "reveal_step",
    "rolled",
    "row_number",
    "segments",
    "severity",
    "shown_where",
    "signal",
    "signal_mark",
    "stripe_line",
    "stripe_parts",
    "subject_lines",
    "sync_lines",
    "tab_number",
    "ticks_parts",
    "trace",
    "track_cells",
    "track_ruler",
    "track_span",
]
