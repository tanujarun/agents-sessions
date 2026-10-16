"""The Evangelion (NERV / MAGI) presentation layer: pure helpers, no Textual.

The look follows what the screens in the series do, not just their colours:

- colour is a signal, never decoration: orange is the system speaking (frames,
  labels), green is nominal or live, red is alert, cyan is external data;
- state is shown by identical, numbered units that change colour (PHASE 10 to
  17, PARTITION #1 to #5): rows are numbered, providers are numbered plates;
- English and kanji are paired on labels (内部 INTERNAL, 警告 WARNING);
- a decision is a MAGI vote: three units, each 承認 (approve) or 否決 (reject);
- a crisis takes the screen over: a full-width band with hazard stripes;
- motion is stepped, never eased: hard cuts, a scan line that wipes a panel
  in, an afterglow where the selection was, flicker when a screen arrives.

Everything here is a pure function of its arguments so it is unit-tested
without Textual; ``app.py`` draws it and ``app.tcss`` colours it.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from . import view

# Switch every transition off: AGENT_SESSIONS_MOTION=0 (the tests do; so may a
# slow terminal or anyone who wants a still screen).
MOTION_ENV = "AGENT_SESSIONS_MOTION"

BRAND = "NERV"
BRAND_SUB = "特務機関"  # "special agency": the line under NERV's own logo
BOARD_TITLE = "SESSION LOG ─ 記録"
DETAIL_TITLE = "DATA"
MAGI_TITLE = "MAGI ─ 審議"
HELP_TITLE = "操作 KEYS"
PAGER_TITLE = "記録 TRANSCRIPT"

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

GUTTER_MIN_WIDTH = 100  # below this the row numbers give their room to TITLE
GUTTER = 5  # "001▶ "
MAGI_MIN_WIDTH = 110  # below this the MAGI panel gives its room to the data


def motion_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether transitions run: on unless ``AGENT_SESSIONS_MOTION`` is
    ``0``, ``false``, ``no`` or ``off``."""
    value = (os.environ if environ is None else environ).get(MOTION_ENV, "")
    return value.strip().lower() not in ("0", "false", "no", "off")


# --- the list -----------------------------------------------------------------


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


# --- key hints ------------------------------------------------------------------------


def hint_spans(line: str) -> list[tuple[str, str]]:
    """A key-hint line as ``(kind, text)`` pieces, ``kind`` being ``key``,
    ``action`` or ``gap``: hints are separated by two spaces, and the first
    word of each is its key. The text is unchanged; only its parts are
    named, so the key can be drawn as a plate."""
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


def flicker_frames() -> Sequence[str]:
    """The steps of a screen that arrives, as a cathode screen catching:
    ``off``, ``on``, ``dim``, ``on`` (each one frame of ``FRAME`` seconds;
    the stylesheet says what off and dim look like)."""
    return ("off", "on", "dim", "on")


FRAME = 0.035  # seconds per step of a transition


__all__ = [
    "APPROVE",
    "BOARD_TITLE",
    "BRAND",
    "BRAND_SUB",
    "CAUTION_LABEL",
    "CONDITIONAL",
    "DANGER_MODES",
    "DETAIL_TITLE",
    "FRAME",
    "GUTTER",
    "GUTTER_MIN_WIDTH",
    "HELP_TITLE",
    "MAGI_MIN_WIDTH",
    "MAGI_TITLE",
    "MAGI_UNITS",
    "MOTION_ENV",
    "NOT_APPLICABLE",
    "PAGER_TITLE",
    "PENDING",
    "REJECT",
    "WARNING_LABEL",
    "Vote",
    "drawn_row",
    "flicker_frames",
    "gutter_text",
    "hint_spans",
    "magi_line",
    "magi_votes",
    "motion_enabled",
    "reveal_step",
    "row_number",
    "severity",
    "shown_where",
    "signal",
    "stripe_line",
    "stripe_parts",
]
