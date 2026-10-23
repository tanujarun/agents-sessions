"""The Textual app: widgets, screens and the wiring between keys, workers and
the engine.

Rules this module keeps:

- No engine logic. Every list, scan, import, undo, mount, launch and
  transcript is a call on ``self.engine`` (``tui/engine.py``), made in a
  thread; the thread hands its result back with ``_ui``. An engine
  exception becomes a status line message. What touches a disk for the
  folder input (a listing, a look whether a folder is there) runs in a
  thread too.
- Quitting waits for nothing that only reads. Such a call runs in a daemon
  thread (``Reader``); an import and an undo run in a thread the process
  waits for, and the program leaves when they have finished.
- Nothing is drawn that ``view.clean`` has not seen: every text comes from
  a ``view`` function or goes through ``set_message``, every ``Static`` is
  made with ``markup=False``, and the two inputs are ``SafeInput``.
- No view logic. What a row says, which columns fit, what a key means: all of
  it is in ``tui/view.py``.
- No styling. Colours, borders and spacing are in ``app.tcss``; this module
  only names things (ids, classes, component classes) for it to style, and
  reads the spacing it needs for fitting back from the computed styles.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import random
import re
import signal
import threading
import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from functools import partial
from itertools import accumulate
from typing import TYPE_CHECKING, Any, ClassVar

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.geometry import Region, Size
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Static
from textual.worker import Worker

from . import crt, eva, view
from .crt import CrtHorizontal, CrtMixin, CrtStatic, CrtVertical, Raster
from .engine import Engine

if TYPE_CHECKING:
    from textual.dom import DOMNode
    from textual.timer import Timer

    from ..index import Index
    from ..model import Session
    from ..search import Corpus, Found, Progress

Clock = Callable[[], datetime]

THEME = "ansi-dark"
"""Textual's ANSI theme: every colour is one of the terminal's own sixteen
(or its default foreground/background), so the app follows the terminal."""

# The engine decides: a full walk when the drives changed or the last one is
# over a day old.
STARTUP_SCAN_DEEP = None
PROGRESS_INTERVAL = 0.1  # seconds between progress updates sent to the UI
VERDICT_DELAY = 0.15  # seconds a row must stay selected before import_options runs
CLOCK_INTERVAL = 30.0  # seconds between refreshes of the ages on screen
PAGER_BATCH = 50  # turns handed to the pager at once
PAGER_SLICE = 0.01  # seconds of wrapping before the pager yields to input
PAGER_PAUSE = 0.001  # seconds the keyboard gets between two slices
STATIC_INTERVAL = 0.4  # seconds between two pictures of a lost signal's static
DELIBERATION = 0.25  # seconds the MAGI deliberate on a new target (motion)
PREVIEW_DELAY = 0.3  # seconds a row must stay selected before its transcript is read
PREVIEW_SECONDS = 1.5  # at most this long reading one transcript for the readout
PREVIEW_MAX_TURNS = 5000  # and at most this many turns
PREVIEW_TURNS = 40  # the last things said (no tool turns) kept for the communication log
PREVIEW_CACHE = 48  # transcripts whose reading is kept
SEARCH_DELAY = 0.08  # seconds the typing must pause before what was said is searched
SEARCH_PROGRESS = 0.4  # seconds between two searches while the text is still being read
RELATED_MAX = 5  # other sessions of the same folder listed at most
BOOT_LIMIT = 2.0  # seconds the boot log waits for the index before it gives way
_STATIC = random.Random()  # noqa: S311 (the noise of lost signals and rolling digits)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _version() -> str:
    from .. import __version__

    return __version__


class UiCall(Message):
    """A callback a worker thread wants run on the UI thread. Posting a
    message is thread-safe and never blocks, also while the app is closing."""

    def __init__(self, callback: Callable[[], object]) -> None:
        super().__init__()
        self.callback = callback


class _UiCalls:
    """Mixin for a screen: ``_ui(fn, *args)`` from any thread."""

    if TYPE_CHECKING:
        # What the screen this is mixed into provides.
        def post_message(self, message: Message) -> bool: ...

    def _ui(self, function: Callable[..., object], *args: object) -> None:
        self.post_message(UiCall(partial(function, *args)))

    async def on_ui_call(self, message: UiCall) -> None:
        message.stop()
        result = message.callback()
        if inspect.isawaitable(result):
            await result


class Reader(Worker[None]):
    """A worker for an engine call that only reads, run in a daemon thread.

    Textual runs a thread worker in the event loop's pool, and the
    interpreter waits for the threads of that pool when the program ends:
    quitting took as long as the slowest read from a foreign drive. Nothing
    waits for a daemon thread. What changes a store (an import, an undo)
    stays a thread worker of Textual's, so that it is finished first."""

    def __init__(self, node: DOMNode, read: Callable[[], object], group: str) -> None:
        super().__init__(node, read, name=f"read-{group}", group=group, exit_on_error=False)
        self._read = read

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        def settle(error: BaseException | None) -> None:
            if done.done():  # cancelled: its screen, or the app, has gone
                return
            if error is None:
                done.set_result(None)
            else:
                done.set_exception(error)

        def read() -> None:
            error: BaseException | None = None
            try:
                self._read()
            except Exception as exc:  # the worker's to report, as for any worker
                error = exc
            try:
                loop.call_soon_threadsafe(settle, error)
            except RuntimeError:
                pass  # the loop is closed: the program is on its way out

        threading.Thread(target=read, name=self.name, daemon=True).start()
        await done


def read(node: DOMNode, function: Callable[[], object], group: str) -> None:
    """Start ``function`` as a ``Reader`` of ``node``."""
    node.workers.add_worker(Reader(node, function, group), exclusive=False)


class _ClosesOnce(Screen):
    """A screen that a key or a click closes. Two of them can be on their
    way before the first is handled (a key that repeats, a second press
    because the first seemed lost), and closing twice would take the screen
    below away too."""

    _gone = False  # closed, or closing

    def close(self) -> None:
        if self._gone or self.app.screen is not self:
            return
        self._gone = True
        self.dismiss()


class SafeInput(Input):
    """An ``Input`` that draws its value through ``view.clean``: whatever is
    put into it (a paste, a name read from a disk) reaches the terminal as
    text, never as an escape sequence. The value itself is left as it is.

    Textual's ``Input`` draws ``_value`` and measures with
    ``_position_to_cell``; these two are the whole of what it shows."""

    @property
    def _value(self) -> Text:
        return Text(view.clean(self.value), no_wrap=True, overflow="ignore", end="")

    def _position_to_cell(self, position: int) -> int:
        return cell_len(view.clean(self.value[:position]))

    def show_end(self) -> None:
        """Put the cursor after the last character and scroll to it. For a
        value that was set while the input had no width yet: scrolling to
        the cursor did nothing then."""
        self.cursor_position = len(self.value)
        self.scroll_to(x=self.max_scroll_x, animate=False, force=True)


# --- the session list ---------------------------------------------------------


class SessionList(CrtMixin, ScrollView, can_focus=True):
    """The list of sessions, drawn line by line: only the visible rows are
    formatted, so thousands of rows cost the same as twenty."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "session-list--row",  # a native row
        "session-list--foreign",  # a row from another drive
        "session-list--cursor",  # the selected row (added to one of the above)
        "session-list--cell",  # padding of every cell = the column spacing
        "session-list--empty",  # the "No sessions found" line
        "session-list--number",  # the row number in front of a row (eva)
        "session-list--mark",  # the selection mark after it
        "session-list--age",  # the AGE cell
        "session-list--internal",  # LOCATION of a session in this system's store
        "session-list--external",  # ... on another drive, mounted
        "session-list--imported",  # ... on another drive, with a copy here
        "session-list--lost",  # ... on a drive that is not mounted: SIGNAL LOST
        "session-list--ghost",  # the afterglow where the selection was (motion)
        "session-list--track-grid",  # the lifespan track where nothing ran
        "session-list--track-bar",  # a session's span on it
        "session-list--track-head",  # its last activity
        "session-list--meter-on",  # the message meter's lit units
        "session-list--meter-off",  # and its unlit ones
        "session-list--meter-count",  # the count after it
        "session-list--section",  # a heading of a search: SESSIONS, MESSAGES
        "session-list--hit-title",  # a message's session, in front of its snippet
        "session-list--hit-text",  # the snippet
        "session-list--hit-match",  # the text searched for in it
        "session-list--hit-role-user",  # the speaker plate
        "session-list--hit-role-assistant",
    }

    class Highlighted(Message):
        """The selection moved, or the rows were replaced."""

        def __init__(self, key: str | None) -> None:
            super().__init__()
            self.key = key

    class Pressed(Message):
        """A row was clicked; ``double`` on the second click of a double-click."""

        def __init__(self, key: str, double: bool) -> None:
            super().__init__()
            self.key = key
            self.double = double

    class Scrolled(Message):
        """The rows moved under the leader line."""

    class Fitted(Message):
        """The columns changed; ``header`` is the new header line."""

        def __init__(self, header: str) -> None:
            super().__init__()
            self.header = header

    def __init__(self, clock: Clock = _utc_now, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.clock = clock
        self.rows: list[view.Row] = []
        self._numbers: list[int] = []  # rows up to and with each, headings not counted
        self.cursor = -1
        self.columns: list[view.Column] = []
        self.widths: dict[str, int] = {}
        self.provider_labels: dict[str, str] = {}
        self.show_provider = False
        self.show_project = True
        self.empty_text = ""
        self._pad = (0, 0)
        self._header: str | None = None
        self._fit_width = -1
        self.motion = False  # set by the screen from the app (``eva.motion_enabled``)
        self._ghost: tuple[int, float] | None = None  # (row, until) of the afterglow
        self.track = 0  # cells of lifespan track after TITLE; 0: none
        self.span = eva.track_span([], clock())
        self.peak = 0  # the most messages among the rows (the meter's scale)

    @property
    def number_width(self) -> int:
        """Cells in front of every row for its number: none when narrow."""
        return eva.GUTTER if self.size.width >= eva.GUTTER_MIN_WIDTH else 0

    @property
    def current(self) -> view.Row | None:
        return self.rows[self.cursor] if 0 <= self.cursor < len(self.rows) else None

    def number(self, index: int) -> int:
        """A row's place among the rows, headings not counted, from 0."""
        return self._numbers[index] - 1 if 0 <= index < len(self._numbers) else index

    @property
    def top(self) -> int:
        return int(self.scroll_offset.y)

    def set_rows(
        self,
        rows: list[view.Row],
        key: str | None = None,
        *,
        show_provider: bool = False,
        provider_labels: dict[str, str] | None = None,
        widths: dict[str, int] | None = None,
        empty_text: str = "",
        from_top: bool = False,
    ) -> None:
        """Replace the rows, keeping the selection on ``key`` when it is
        still listed and near its old place when it is not (``from_top``:
        at the first row, for a list that is not the one shown before)."""
        self.rows = rows
        self._numbers = list(accumulate(view.selectable(row) for row in rows))
        self.span = eva.track_span(rows, self.clock())
        self.peak = max((row.messages or 0 for row in rows), default=0)
        self.cursor = view.select_index(rows, key, 0 if from_top else self.cursor)
        self.show_provider = show_provider
        self.provider_labels = provider_labels or {}
        self.widths = widths or {}
        self.empty_text = empty_text
        self.fit()
        self._show_cursor()
        self.refresh()
        self._announce()

    def fit(self) -> None:
        """Work the columns out again for the current width."""
        self._fit_width = self.size.width
        self.virtual_size = Size(self.size.width, len(self.rows))
        padding = self.get_component_styles("session-list--cell").padding
        self._pad = (padding.left, padding.right)

        def columns(room: int) -> list[view.Column]:
            return view.fit_columns(
                room,
                self.widths,
                show_provider=self.show_provider,
                show_project=self.show_project,
                cell_pad=padding.left + padding.right,
            )

        room = self.size.width - self.number_width
        self.columns = columns(room)
        # A title column with room to spare gives it to the lifespan track and
        # the message meter (eva.track_cells, eva.meter).
        title = next((column for column in self.columns if column.name == "title"), None)
        spare = (title.width if title else 0) - eva.TITLE_ROOM - eva.TRACK_TAIL
        self.track = spare if spare >= eva.TRACK_MIN and self.number_width else 0
        if self.track:
            self.columns = columns(room - self.track - eva.TRACK_TAIL)
        cells = view.format_header(self.columns, *self._pad)
        if self.track:
            cut = sum(self._pad) + self.columns[0].width
            tail = "  " + "MESSAGES".ljust(eva.METER_UNITS + 5) + " "
            cells = cells[:cut] + eva.track_ruler(self.span, self.track) + tail + cells[cut:]
        header = " NO".ljust(self.number_width) * bool(self.number_width) + cells
        if header != self._header:
            self._header = header
            self.post_message(self.Fitted(header))
        self.refresh()

    def select(self, index: int, step: int = 1) -> None:
        """Select a row; a heading passes the selection on in the direction
        of ``step``."""
        if not self.rows:
            return
        index = view.nearest_selectable(self.rows, min(max(index, 0), len(self.rows) - 1), step)
        if index >= 0 and index != self.cursor:
            if self.motion and 0 <= self.cursor < len(self.rows):
                # The row it leaves glows for a moment, as a phosphor does.
                self._ghost = (self.cursor, time.monotonic() + eva.FRAME * 3)
                self.set_timer(eva.FRAME * 3, self.refresh)
            self.cursor = index
            self._show_cursor()
            self.refresh()
            self._announce()

    def move(self, move: str) -> None:
        target = view.move_index(self.cursor, len(self.rows), move, page=self.size.height - 1)
        step = -1 if move in ("up", "pageup", "end") else 1
        self.select(target, step)

    def _announce(self) -> None:
        row = self.current
        self.post_message(self.Highlighted(row.key if row else None))

    def _show_cursor(self) -> None:
        """Scroll just enough for the selected row to be visible."""
        top = view.scroll_for(self.cursor, self.top, self.size.height, len(self.rows))
        if top != self.top:
            self.scroll_to(y=top, animate=False, force=True, immediate=True)

    def on_resize(self, event: events.Resize) -> None:
        self.fit()
        self._show_cursor()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self.post_message(self.Scrolled())

    def render_lines(self, crop: Region) -> list[Strip]:
        # The content gets a column narrower when the scrollbar appears, and
        # no Resize says so: check before drawing.
        if self.size.width != self._fit_width:
            self.fit()
        return super().render_lines(crop)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        offset = event.get_content_offset(self)
        index = -1 if offset is None else offset.y + self.top
        if not 0 <= index < len(self.rows) or not view.selectable(self.rows[index]):
            return
        self.select(index)
        self.post_message(self.Pressed(self.rows[index].key, event.chain == 2))

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not self.rows:
            return self._empty_line(y, width)
        index = y + self.top
        if not 0 <= index < len(self.rows):
            return Strip.blank(width, self.rich_style)
        row = self.rows[index]
        if row.kind == "section":
            return self._section_line(row, width)
        names = ["session-list--foreign" if row.foreign else "session-list--row"]
        selected = index == self.cursor
        if selected:
            names.append("session-list--cursor")
        elif self._ghost is not None and self._ghost[0] == index:
            if time.monotonic() < self._ghost[1]:
                names.append("session-list--ghost")
            else:
                self._ghost = None
        style = self.get_component_rich_style(*names)
        segments: list[Segment] = []
        if self.number_width:
            number = self.get_component_rich_style(*names, "session-list--number")
            mark = self.get_component_rich_style(*names, "session-list--mark")
            text = eva.gutter_text(self.number(index), selected)
            state = eva.signal(row)
            sign = self.get_component_rich_style(*names, f"session-list--{state}")
            segments += [
                Segment(text[:3], style if selected else number),
                Segment(text[3:4], mark),
                Segment(eva.signal_mark(state, _STATIC if self.motion else None), sign),
                Segment(text[4:], style),
            ]
        cells = view.row_cells(
            eva.drawn_row(row), self.columns, self.clock(), self.provider_labels, *self._pad
        )
        if row.kind == "hit":
            segments += self._hit_segments(row, cells, names, selected, style)
            return Strip(segments).adjust_cell_length(width, style)
        for name, text in cells:
            if name == "where" and not selected:
                cell = self.get_component_rich_style(*names, f"session-list--{eva.signal(row)}")
            elif name == "age" and not selected:
                cell = self.get_component_rich_style(*names, "session-list--age")
            else:
                cell = style
            segments.append(Segment(text, cell))
            if name == "title" and self.track:
                segments += self._track_segments(row, names, selected, style)
        return Strip(segments).adjust_cell_length(width, style)

    def _section_line(self, row: view.Row, width: int) -> Strip:
        """A heading of a search, ruled to the edge."""
        style = self.get_component_rich_style("session-list--section")
        text = view.clip(f"{'─' * 2} {row.title} ", width)
        text += "─" * max(width - view.text_width(text), 0)
        return Strip([Segment(text, style)]).adjust_cell_length(width, self.rich_style)

    def _hit_segments(
        self,
        row: view.Row,
        cells: list[tuple[str, str]],
        names: list[str],
        selected: bool,
        style: Style,
    ) -> list[Segment]:
        """A message: its session's title, then the snippet over the
        columns up to AGE (and the lifespan track), then AGE (the message's)
        and the columns after it."""

        def look(name: str) -> Style:
            return style if selected else self.get_component_rich_style(*names, name)

        names_order = [name for name, _ in cells]
        age_at = names_order.index("age") if "age" in names_order else len(cells)
        title_at = names_order.index("title") if "title" in names_order else -1
        out: list[Segment] = []
        pad_left, pad_right = self._pad
        span = sum(
            view.text_width(text) for at, (_, text) in enumerate(cells) if title_at < at < age_at
        )
        if self.track:
            span += self.track + eva.TRACK_TAIL
        if title_at >= 0:
            # The snippet is what a message row is for: the title gives it room.
            title = view.text_width(cells[title_at][1]) - pad_left - pad_right
            kept = min(title, max(view.MIN_TITLE_WIDTH, (title + span) // 3))
            span += title - kept
            text = " " * pad_left + view.pad(row.title, kept) + " " * pad_right
            out.append(Segment(text, look("session-list--hit-title")))
        room = span - pad_left - pad_right
        used = min(pad_left, span)
        out.append(Segment(" " * used, style))
        match = self.get_component_rich_style(*names, "session-list--hit-match")
        for kind, text in view.hit_pieces(row.hit, room):
            out.append(
                Segment(text, match if kind == "match" else look(f"session-list--hit-{kind}"))
            )
            used += view.text_width(text)
        out.append(Segment(" " * max(span - used, 0), style))
        for at in range(age_at, len(cells)):
            name, text = cells[at]
            if name == "where" and not selected:
                cell = self.get_component_rich_style(*names, f"session-list--{eva.signal(row)}")
            elif name == "age" and not selected:
                cell = self.get_component_rich_style(*names, "session-list--age")
            else:
                cell = style
            out.append(Segment(text, cell))
        return out

    def _track_segments(
        self, row: view.Row, names: list[str], selected: bool, style: Style
    ) -> list[Segment]:
        """The lifespan track and the message meter after a row's title."""

        def look(name: str) -> Style:
            return style if selected else self.get_component_rich_style(*names, name)

        out = [
            Segment(text, look(f"session-list--track-{kind}"))
            for kind, text in eva.track_cells(row, self.span, self.track)
        ]
        lit, unlit = eva.meter(row.messages, self.peak)
        out += [
            Segment("  ", style),
            Segment(lit, look("session-list--meter-on")),
            Segment(unlit, look("session-list--meter-off")),
            Segment(" ", style),
            Segment(eva.meter_count(row.messages), look("session-list--meter-count")),
            Segment(" ", style),
        ]
        return out

    def _empty_line(self, y: int, width: int) -> Strip:
        """One centred line in the middle of the empty list."""
        if not self.empty_text or y != (self.size.height - 1) // 2:
            return Strip.blank(width, self.rich_style)
        style = self.get_component_rich_style("session-list--empty")
        text = view.clip(self.empty_text, width)
        left = max((width - view.text_width(text)) // 2, 0)
        return Strip(
            [Segment(" " * left, self.rich_style), Segment(text, style)]
        ).adjust_cell_length(width, self.rich_style)


class MagiPanel(CrtStatic):
    """The MAGI verdict on the selected session: three units, each voting on
    one question (``eva.magi_votes``). Drawn as the screens draw it, three
    panels round the word MAGI (``eva.magi_shape``), where there is room;
    one line a unit where there is not."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "magi--unit",
        "magi--subject",
        "magi--note",
        "magi--approve",
        "magi--reject",
        "magi--pending",
        "magi--conditional",
        "magi--none",
        "magi--fill-approve",
        "magi--fill-reject",
        "magi--fill-pending",
        "magi--fill-conditional",
        "magi--fill-none",
        "magi--name",
        "magi--label",
        "magi--word-approve",
        "magi--word-reject",
        "magi--word-pending",
        "magi--word-none",
        "magi--blank",
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.votes: list[eva.Vote] = []
        self._until = 0.0
        self.roomy = False  # set by the screen: room for the three panels

    def deliberate(self) -> None:
        """A new target: for a moment every unit is still deliberating."""
        if not getattr(self.app, "motion", False):
            return
        self._until = time.monotonic() + DELIBERATION
        self.set_timer(DELIBERATION, lambda: self.show(self.votes))

    def show(self, votes: list[eva.Vote]) -> None:
        self.votes = votes
        if time.monotonic() < self._until:
            votes = [eva.deliberating(vote) for vote in votes]
        width = max(self.size.width, 1)
        text = Text(no_wrap=True, overflow="crop")
        shaped = (self.roomy and width >= eva.MAGI_SHAPE_WIDTH) and eva.magi_shape(votes, width)
        if shaped:
            for number, line in enumerate(shaped):
                if number:
                    text.append("\n")
                for kind, piece in line:
                    text.append(piece, self.get_component_rich_style(f"magi--{kind}"))
            self.update(text)
            return
        for number, vote in enumerate(votes):
            if number:
                text.append("\n")
            for part, piece in eva.magi_line(vote, width):
                kind = vote.state if part == "verdict" else part
                text.append(piece, self.get_component_rich_style(f"magi--{kind}"))
        self.update(text)

    def on_resize(self, event: events.Resize) -> None:
        if self.votes:
            self.show(self.votes)


class StatePlate(CrtStatic):
    """The target's state, as the 内部 INTERNAL plate beside the screens'
    clocks: kanji on a filled plate, the English, hazard slashes."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "plate--internal",
        "plate--external",
        "plate--imported",
        "plate--lost",
        "plate--none",
        "plate--word",
        "plate--slash",
    }

    state = "none"

    def show(self, state: str) -> None:
        self.state = state
        kanji, english, slashes = eva.plate_parts(state, max(self.size.width, 1))
        text = Text(no_wrap=True, overflow="crop")
        text.append(kanji, self.get_component_rich_style(f"plate--{state}"))
        text.append(english, self.get_component_rich_style("plate--word"))
        text.append(slashes, self.get_component_rich_style("plate--slash"))
        self.update(text)

    def on_resize(self, event: events.Resize) -> None:
        self.show(self.state)


class SegmentClock(CrtStatic):
    """Time since the target was last active, in seven-segment numerals
    (``eva.segments``) under ELAPSED SINCE LAST CONTACT. A new target rolls
    its digits before they settle; the colons blink with the seconds."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"clock--digits", "clock--units", "clock--rolling"}

    def __init__(self, clock: Clock = _utc_now, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.clock = clock
        self.stamp: str | None = None
        self._rolls = 0
        self._colon = True
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        if getattr(self.app, "motion", False):
            self.set_interval(1.0, self._second)
        self.draw()

    def show(self, stamp: str | None) -> None:
        changed = stamp != self.stamp
        self.stamp = stamp
        if changed and getattr(self.app, "motion", False) and stamp is not None:
            self._rolls = 6
            if self._timer is None:
                self._timer = self.set_interval(eva.FRAME, self._roll)
        self.draw()

    def _roll(self) -> None:
        self._rolls -= 1
        if self._rolls <= 0 and self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.draw()

    def _second(self) -> None:
        self._colon = not self._colon
        self.draw()

    def draw(self) -> None:
        digits = eva.clock_text(eva.elapsed(self.stamp, self.clock()))
        rolling = self._rolls > 0
        if rolling:
            digits = eva.rolled(digits, _STATIC)
        if not self._colon:
            digits = digits.replace(":", " ")
        style = self.get_component_rich_style("clock--rolling" if rolling else "clock--digits")
        text = Text(no_wrap=True, overflow="crop")
        for line in eva.segments(digits):
            text.append(line + "\n", style)
        units = "".join(
            unit.center(width) for unit, width in zip(eva.CLOCK_UNITS, (11, 10, 10), strict=True)
        )
        text.append(units, self.get_component_rich_style("clock--units"))
        self.update(text)


class SubjectColumn(CrtMixin, Widget):
    """The providers as the screens list their subjects: numbered plates,
    the selected one framed with its number in segments, each over a trace
    of the days it was active (``eva.subject_lines``). A click selects."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "subject--frame",
        "subject--digits",
        "subject--name",
        "subject--count",
        "subject--label",
        "subject--number",
        "subject--plate",
        "subject--trace",
        "subject--trace-active",
        "subject--rule",
        "subject--cursor",
        "subject--legend",
        "subject--internal",
        "subject--external",
        "subject--lost",
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lines: list[eva.SubjectLine] = []
        self._args: tuple = ((), 0, {})

    def show(self, tabs: list[view.Tab], active: int, traces: dict[str, str]) -> None:
        self._args = (tabs, active, traces)
        lines = eva.subject_lines(tabs, active, traces, self.size.width or eva.SUBJECT_WIDTH)
        grew = len(lines) != len(self.lines)
        self.lines = lines
        self.refresh(layout=grew)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.lines)

    def on_resize(self, event: events.Resize) -> None:
        self.show(*self._args)

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not 0 <= y < len(self.lines):
            return Strip.blank(width, self.rich_style)
        segments = [
            Segment(text, self.get_component_rich_style(f"subject--{kind}"))
            for kind, text in self.lines[y].parts
        ]
        return Strip(segments).adjust_cell_length(width, self.rich_style)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        offset = event.get_content_offset(self)
        if offset is None or not 0 <= offset.y < len(self.lines):
            return
        tab = self.lines[offset.y].tab
        if tab >= 0:
            self.post_message(TabLabel.Pressed(tab))


class LinePanel(CrtMixin, Widget):
    """A panel drawn from ``(kind, text)`` lines made in ``eva``; each kind
    is the component class ``<PREFIX>--<kind>``. Its height is its lines."""

    PREFIX = ""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lines: list[list[tuple[str, str]]] = []

    def set_lines(self, lines: list[list[tuple[str, str]]]) -> None:
        grew = len(lines) != len(self.lines)
        self.lines = lines
        self.refresh(layout=grew)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self.lines)

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not 0 <= y < len(self.lines):
            return Strip.blank(width, self.rich_style)
        segments = [
            Segment(text, self.get_component_rich_style(f"{self.PREFIX}--{kind}"))
            for kind, text in self.lines[y]
        ]
        return Strip(segments).crop(0, width).adjust_cell_length(width, self.rich_style)


class ActivityCells(LinePanel):
    """A cell a day for the selected subject (``eva.activity_lines``)."""

    PREFIX = "activity"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "activity--title",
        "activity--note",
        "activity--cell-0",
        "activity--cell-1",
        "activity--cell-2",
        "activity--cell-3",
        "activity--cell-4",
        "activity--cell-x",
    }


class DriveBoard(LinePanel):
    """A plate a drive (``eva.drive_lines``)."""

    PREFIX = "drives"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "drives--title",
        "drives--number",
        "drives--name",
        "drives--blank",
        "drives--online",
        "drives--standby",
        "drives--lost",
        "drives--note",
        "drives--count",
    }


class RelatedPanel(LinePanel):
    """Other sessions from the target's folder (``eva.related_lines``)."""

    PREFIX = "related"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "related--title",
        "related--note",
        "related--number",
        "related--provider",
        "related--text",
        "related--age",
    }


class SyncGraph(LinePanel):
    """The target's turns over its time (``eva.sync_lines``)."""

    PREFIX = "sync"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "sync--title",
        "sync--note",
        "sync--grid",
        "sync--trace",
        "sync--user",
    }


class CommLog(LinePanel):
    """The target's last turns (``eva.comm_lines``); it takes the height it
    is given, and shows as many as fit."""

    PREFIX = "comm"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "comm--title",
        "comm--note",
        "comm--lost",
        "comm--blank",
        "comm--role-user",
        "comm--role-assistant",
        "comm--role-tool",
        "comm--text",
    }

    def __init__(self, tz: tzinfo | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tz = tz
        self.preview: eva.Preview | None = None

    def show(self, preview: eva.Preview | None) -> None:
        self.preview = preview
        self.lines = eva.comm_lines(preview, self.size.width, self.size.height, self.tz)
        self.refresh()

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return max(container.height, 1)

    def on_resize(self, event: events.Resize) -> None:
        self.show(self.preview)


class Leader(CrtMixin, Widget):
    """The leader line from the selected row to the target readout, as the
    screens tie a label to what it names. It redraws (flashes) when the
    selection moves."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"leader--line", "leader--flash"}

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ends: tuple[int, int] | None = None  # (row, target) in this widget's lines
        self._flash_until = 0.0

    def point(self, row: int | None, target: int | None, flash: bool = False) -> None:
        ends = None if row is None or target is None else (row, target)
        if flash and ends != self.ends and getattr(self.app, "motion", False):
            self._flash_until = time.monotonic() + eva.FRAME * 3
            self.set_timer(eva.FRAME * 3, self.refresh)
        self.ends = ends
        self.refresh()

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if self.ends is None:
            return Strip.blank(width, self.rich_style)
        row, target = self.ends
        text = eva.leader_cell(y, row, target, width)
        if not text.strip():
            return Strip.blank(width, self.rich_style)
        name = "leader--flash" if time.monotonic() < self._flash_until else "leader--line"
        return Strip([Segment(text, self.get_component_rich_style(name))]).adjust_cell_length(
            width, self.rich_style
        )


class BootLog(CrtStatic):
    """The boot log over the main screen while the index is first read
    (motion only): lines print one a frame; any key or the end takes it
    away and the screen is swept in (``eva.boot_lines``)."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "boot--head",
        "boot--line",
        "boot--ok",
        "boot--wait",
        "boot--lost",
        "boot--done",
    }

    def __init__(self, version: str, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.version = version
        self.facts: eva.BootFacts | None = None
        self.shown = 0
        self.finished = False
        self._timer: Timer | None = None
        self._ends = 0.0

    def start(self) -> None:
        self._timer = self.set_interval(eva.FRAME, self._next)
        self._ends = time.monotonic() + BOOT_LIMIT

    def _next(self) -> None:
        lines = eva.boot_lines(self.version, self.facts)
        if self.shown < len(lines):
            self.shown += 1
        elif self.facts is not None or time.monotonic() > self._ends:
            self.finish()
            return
        text = Text(no_wrap=True, overflow="crop")
        for state, line in lines[: self.shown]:
            text.append(line + "\n", self.get_component_rich_style(f"boot--{state}"))
        if self.shown < len(lines) or self.facts is None:
            text.append(crt.CURSOR, self.get_component_rich_style("boot--ok"))
        self.update(text)

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        if self._timer is not None:
            self._timer.stop()
        screen = self.screen
        self.remove()
        raster = getattr(screen, "raster", None)
        if raster is not None:
            raster.sweep()


class HazardBand(CrtStatic):
    """The band over and under a prompt that cannot be taken back: diagonal
    stripes with 警告 WARNING cut into them (``eva.stripe_parts``)."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"hazard--stripe", "hazard--label"}

    def draw(self, width: int, phase: int) -> None:
        if phase // 8 % 2:  # every other beat the band goes dark but for its ticks
            left, label, right = eva.ticks_parts(width, eva.EMERGENCY_LABEL)
        else:
            left, label, right = eva.stripe_parts(width, phase, eva.WARNING_LABEL)
        stripe = self.get_component_rich_style("hazard--stripe")
        text = Text(left, stripe, no_wrap=True, overflow="crop")
        text.append(label, self.get_component_rich_style("hazard--label"))
        text.append(right, stripe)
        self.update(text)


class KeyHints(CrtStatic):
    """The key hints, each key in the system's orange; the words are
    ``view.hint_lines``' own."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"hints--key", "hints--action"}

    def show(self, lines: Iterable[str]) -> None:
        text = Text(no_wrap=True, overflow="ellipsis")
        for number, line in enumerate(lines):
            if number:
                text.append("\n")
            for kind, piece in eva.hint_spans(line):
                style = "hints--key" if kind == "key" else "hints--action"
                text.append(piece, self.get_component_rich_style(style))
        self.update(text)


class TabLabel(CrtStatic):
    """One provider tab."""

    class Pressed(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(self, tab: view.Tab, index: int) -> None:
        name = re.sub(r"[^A-Za-z0-9_-]", "-", tab.name)
        super().__init__(tab.label, markup=False, id=f"tab-{name}", classes="tab")
        self.index = index

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Pressed(self.index))


# --- data for the main screen ---------------------------------------------------


@dataclass
class Snapshot:
    """Everything the main screen shows, read from the engine in one go."""

    index: Index | None = None
    sessions: dict[str, Session] = field(default_factory=dict)  # key -> Session
    rows: list[view.Row] = field(default_factory=list)  # newest first, as the engine lists
    providers: list[tuple[str, str]] = field(default_factory=list)  # [(name, label)]
    drives: int = 0
    offline: int = 0  # sessions whose drive is not mounted
    last_scan: str | None = None
    mounts: list[dict] = field(default_factory=list)  # status()["mounts"]


@dataclass
class Question:
    """What the prompt on screen, or the folder input, is about: one session,
    and what the steps before this one settled."""

    key: str
    title: str
    where: str
    stay: bool = False  # asked with ``o``: the TUI stays open after a resume
    verdict: view.Verdict = field(default_factory=view.Verdict)  # what its import can be
    then_open: bool = False  # the warning: resume the copy once it is imported
    target: str | None = None  # the warning: the folder chosen before it
    purpose: str = ""  # the folder input: what the folder is for (view.FOLDER_LABELS)
    tools: tuple[tuple[str, str], ...] = ()  # continue: the tools, as (name, label)
    tool: int = 0  # continue: the one chosen
    folder: str = ""  # continue: the folder to suggest, then the one accepted
    files: int = 0  # undo: the files a dry run would remove
    size: int = 0  # undo: their bytes
    differ: int = 0  # undo: the files that differ in a copy that was used since


def build_snapshot(engine: Engine) -> Snapshot:
    """Read the index through the engine. Called in a worker thread."""
    index = engine.load_index()
    sessions = list(engine.list_sessions(index=index))
    rows = [view.make_row(session, engine.where(session, index)) for session in sessions]
    status = engine.status(index) or {}
    return Snapshot(
        index=index,
        sessions={session.key: session for session in sessions},
        rows=rows,
        providers=list(engine.providers()),
        drives=view.drive_count(status.get("mounts")),
        offline=sum(1 for session in sessions if session.offline),
        last_scan=status.get("last_quick"),
        mounts=[mount for mount in status.get("mounts") or () if isinstance(mount, dict)],
    )


# --- main screen ------------------------------------------------------------------


class MainScreen(_UiCalls, Screen):
    """Central Dogma's session monitor: the status line; the subjects (the
    providers) in a column, or a strip of plates when the terminal is
    narrower; the session log; a leader line to the target readout (state
    plate, clock, data, MAGI); the prompts and the key hints."""

    COMPONENT_CLASSES: ClassVar[set[str]] = set(crt.SCREEN_CLASSES)

    def __init__(self, engine: Engine, clock: Clock = _utc_now, tz: tzinfo | None = None) -> None:
        super().__init__(id="main")
        self.engine = engine
        self.clock = clock
        self.tz = tz
        self.snapshot = Snapshot()
        self.loaded = False
        self.tabs: list[view.Tab] = []
        self.tab_index = 0
        self.tab_name: str | None = None
        self.needle = ""
        self.home = self._engine_home()  # what ``~`` in the folder input means
        self.mode = "list"  # "list", or what has the keys: a table of view.KEYMAPS
        self._question: Question | None = None  # what the prompt or the folder input asks
        # Counts key presses and clicks: what a worker answers to a question
        # asked before the last one is dropped, not shown.
        self._asked = 0
        # (session key, "prompt" | "import" | "folder", stay, _asked): a key
        # that waits for the import verdict of its row.
        self._asking: tuple[str, str, bool, int] | None = None
        self._completed = ""  # the folder input's text as a completion left it
        self._selected: dict[str, str] = {}  # tab name -> selected session key
        self._message = ""
        self._message_error = False
        self._activity = ""
        self._scan: dict | None = None  # {"deep": bool, "cancel": Event}
        self._mount: dict | None = None  # {"drive": str, "cancel": Event, "scanning": bool}
        self._importing = False
        self._undoing = False
        self._opening = False
        self._continuing = False
        self._starting = ""  # what the status line says while a continue starts
        self.motion = False  # the app's (eva.motion_enabled), read on mount
        self._stripe_phase = 0  # the march of a hazard band
        self._stripe_timer: Timer | None = None
        self._blink_timer: Timer | None = None
        self._verdicts: dict[str, view.Verdict] = {}
        self._verdict_pending: set[str] = set()
        self._verdict_timer: Timer | None = None
        # The exit status, once asked to quit while an import or an undo runs.
        self._leaving: int | None = None
        self.raster = Raster(self)
        self._previews: OrderedDict[str, eva.Preview] = OrderedDict()
        self._preview_wanted: str | None = None
        self._preview_timer: Timer | None = None
        self._by_folder: dict[str, list[Session]] = {}
        self._extras: tuple = ()
        self._related_limit = RELATED_MAX
        self._traces: dict[str, str] = {}
        self._target_key: str | None = None
        # Search: the sessions' text (read in a thread once a search wants
        # it), how far that reading is, and the last answer, with the
        # (text, tab) it answers.
        self._corpus: Corpus | None = None
        self._corpus_failed = False
        self._corpus_busy = False
        self._corpus_again = False
        self._corpus_stop = threading.Event()
        self._reading: view.Reading | None = None
        self._found: Found | None = None
        self._found_for: tuple[str, str] | None = None
        self._shown_for: tuple[str, str] | None = None  # what the rows on screen answer
        self._search_timer: Timer | None = None
        self._search_asked = 0

    def _engine_home(self) -> str:
        """The engine's home (``AGENT_SESSIONS_HOME`` can move it); this
        user's own for an engine that does not say."""
        try:
            return str(self.engine.home())
        except Exception:
            return os.path.expanduser("~")

    # --- widgets ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with CrtHorizontal(id="statusbar"):
            yield CrtStatic(eva.BRAND, id="brand", markup=False)
            yield CrtStatic(eva.BRAND_SUB, id="brand-sub", markup=False)
            yield CrtStatic(eva.SITE, id="brand-site", markup=False)
            yield CrtStatic(view.APP_TITLE, id="status-message", markup=False)
            yield CrtStatic("", id="status-summary", markup=False)
        yield CrtHorizontal(id="tabs")
        with Horizontal(id="deck"):
            with CrtVertical(id="subjects-box"):
                yield SubjectColumn(id="subjects")
                yield ActivityCells(id="activity")
                yield DriveBoard(id="drives")
            with CrtVertical(id="board"):
                yield CrtStatic("", id="header", markup=False)
                yield SessionList(clock=self.clock, id="list")
            yield Leader(id="leader")
            with CrtVertical(id="target"):
                yield StatePlate("", id="target-plate", markup=False)
                yield CrtStatic(eva.CLOCK_LABEL, id="target-clock-label", markup=False)
                yield SegmentClock(clock=self.clock, id="target-clock", markup=False)
                with CrtVertical(id="detail"):
                    with CrtHorizontal(classes="detail-row", id="detail-row-main"):
                        for number, name in enumerate(view.DETAIL_FIELDS[:3], 1):
                            yield CrtStatic(
                                f"{number:02d} {name.upper()}",
                                classes="detail-label",
                                markup=False,
                            )
                            yield CrtStatic(
                                view.EMPTY,
                                id=f"detail-{name}",
                                classes="detail-value",
                                markup=False,
                            )
                    for number, name in enumerate(view.DETAIL_FIELDS[3:], 4):
                        with CrtHorizontal(classes="detail-row", id=f"detail-row-{name}"):
                            yield CrtStatic(
                                f"{number:02d} {name.upper()}",
                                classes="detail-label",
                                markup=False,
                            )
                            yield CrtStatic(
                                view.EMPTY,
                                id=f"detail-{name}",
                                classes="detail-value",
                                markup=False,
                            )
                yield MagiPanel("", id="magi", markup=False)
                yield RelatedPanel(id="related")
                yield SyncGraph(id="sync")
                yield CommLog(tz=self.tz, id="comm")
        yield HazardBand("", id="stripe-top", markup=False)
        yield CrtStatic("", id="prompt", markup=False)
        yield HazardBand("", id="stripe-bottom", markup=False)
        with CrtHorizontal(id="folderbar"):
            yield CrtStatic("", id="folder-label", markup=False)
            # The text it opens with is a start to type after, not to replace.
            yield SafeInput(id="folder", select_on_focus=False)
        with CrtHorizontal(id="filterbar"):
            yield CrtStatic("/", id="filter-label", markup=False)
            yield SafeInput(
                id="filter", placeholder="session name, project, folder, or words said in it"
            )
        yield KeyHints("", id="hints", markup=False)
        if getattr(self.app, "motion", False):
            yield BootLog(_version(), id="boot", markup=False)

    @property
    def session_list(self) -> SessionList:
        return self.query_one("#list", SessionList)

    @property
    def filter_input(self) -> SafeInput:
        return self.query_one("#filter", SafeInput)

    @property
    def folder_input(self) -> SafeInput:
        return self.query_one("#folder", SafeInput)

    def on_mount(self) -> None:
        self.motion = bool(getattr(self.app, "motion", False))
        self.session_list.motion = self.motion
        self.query_one("#board").border_title = eva.BOARD_TITLE
        self.query_one("#subjects-box").border_title = eva.SUBJECT_TITLE
        self.query_one("#target").border_title = eva.TARGET_TITLE
        self.query_one("#detail").border_title = eva.DETAIL_TITLE
        self.query_one("#magi").border_title = eva.MAGI_TITLE
        boot = self.query("#boot")
        if boot:
            boot.first(BootLog).start()
        for name in ("#stripe-top", "#stripe-bottom"):
            self.query_one(name).display = False
        self.query_one("#prompt").display = False
        self.query_one("#folderbar").display = False
        self.query_one("#filterbar").display = False
        self.session_list.focus()
        self._apply_layout()
        self.set_interval(CLOCK_INTERVAL, self._tick)
        if self.motion:
            self.set_interval(STATIC_INTERVAL, self._static)
        self._set_activity(f"loading{view.ELLIPSIS}")
        self._read(self._load_thread, group="load")

    def on_unmount(self) -> None:
        self._stop_reading()

    def _stop_reading(self) -> None:
        """Ask a scan, and the scan after a mount, to stop: a thread cannot
        be killed, and the next thing it finds out should be that nobody is
        waiting."""
        for job in (self._scan, self._mount):
            if job is not None:
                job["cancel"].set()
        self._corpus_stop.set()

    def _read(self, function: Callable[..., object], *args: object, group: str) -> None:
        """Start an engine call that only reads (``Reader``)."""
        read(self, partial(function, *args), group)

    def _write(self, function: Callable[..., object], *args: object, group: str) -> None:
        """Start an engine call that changes a store: an import, an undo.
        The process does not end before it has."""
        self.run_worker(partial(function, *args), thread=True, group=group, exit_on_error=False)

    # --- leaving ---------------------------------------------------------

    def leave(self, return_code: int = 0) -> None:
        """Quit (``q``, Ctrl-C, SIGTERM, SIGHUP). At once when nothing is
        being changed: every read is asked to stop and none is waited for.
        An import or an undo under way is finished, or rolled back, first:
        the status line says so, and until then no key does anything."""
        if self._leaving is not None:
            return
        self._stop_reading()
        writing = "import" if self._importing else "undo" if self._undoing else None
        if writing is None:
            self.app.exit(return_code=return_code)
            return
        self._leaving = return_code
        self._close_prompt()
        self.pop_until_active()  # the pager or the help would hide the status line
        self.set_message(view.finishing_text(writing))

    def _left(self) -> bool:
        """After an import or an undo: go, when quitting waited for it."""
        if self._leaving is None:
            return False
        self.app.exit(return_code=self._leaving)
        return True

    def _static(self) -> None:
        """A lost signal's mark is static, and static moves."""
        listing = self.session_list
        shown = listing.rows[listing.top : listing.top + listing.size.height]
        if any(row.offline for row in shown):
            listing.refresh()

    def _tick(self) -> None:
        self.session_list.refresh()
        self.query_one("#target-clock", SegmentClock).draw()
        self._render_status()

    # --- status line -----------------------------------------------------

    def set_message(self, text: str, error: bool = False) -> None:
        """Show a message on the left of the status line until the next key."""
        self._message = view.clean(text)
        self._message_error = error
        self._render_status()

    def clear_message(self) -> None:
        if self._message:
            self.set_message("")

    def _set_activity(self, text: str) -> None:
        """Show a running job's progress in place of the summary."""
        self._activity = text
        self._render_status()

    def _render_status(self) -> None:
        left = self.query_one("#status-message", Static)
        right = self.query_one("#status-summary", Static)
        room = self.size.width - sum(
            widget.styles.gutter.width + widget.styles.margin.width
            for widget in (left, right, self.query_one("#statusbar"))
        )
        # The site's name gives its room to anything the line has to say.
        busy = bool(self._message or self._activity or self._starting)
        self.query_one("#brand-site").display = self.size.width >= eva.TARGET_MIN_WIDTH and not busy
        for name in ("#brand", "#brand-sub", "#brand-site"):
            brand = self.query_one(name)
            if brand.display:
                room -= brand.outer_size.width or (
                    view.text_width(str(brand.render())) + brand.styles.gutter.width
                )
        activity = self._activity or self._starting
        if activity:
            variants = [activity]
        else:
            variants = view.summary_variants(
                self.snapshot.drives,
                len(self.snapshot.rows),
                self.snapshot.last_scan,
                self.clock(),
                self.snapshot.offline,
            )
        shown, summary = view.status_parts(
            self._message or view.APP_TITLE, variants, room, gap=0, keep_left=bool(self._message)
        )
        left.update(shown)
        left.set_class(bool(self._message) and self._message_error, "-error")
        left.set_class(bool(self._message) and not self._message_error, "-info")
        right.update(summary)
        right.set_class(bool(activity), "-busy")
        self._blink(bool(activity))

    def _blink(self, on: bool) -> None:
        """A job under way blinks its line, as the screens' status lamps do."""
        if not self.motion:
            return
        right = self.query_one("#status-summary")
        if on and self._blink_timer is None:
            self._blink_timer = self.set_interval(0.45, lambda: right.toggle_class("-blink"))
        elif not on and self._blink_timer is not None:
            self._blink_timer.stop()
            self._blink_timer = None
            right.remove_class("-blink")

    # --- layout ----------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        """Decide what a terminal of this size shows (``view.layout_for``)."""
        hints = self.query_one("#hints", KeyHints)
        layout = view.layout_for(
            self.size.width, self.size.height, hints.styles.gutter.width + hints.styles.margin.width
        )
        width, height = self.size.width, self.size.height
        side = layout.show_detail and width >= eva.TARGET_MIN_WIDTH
        wide = side and width >= eva.SUBJECT_MIN_WIDTH
        self.query_one("#detail").display = layout.show_detail
        self.query_one("#target").display = layout.show_detail
        self.query_one("#leader").display = side
        self.query_one("#subjects-box").display = wide
        self.query_one("#tabs").display = not wide
        self.query_one("#magi").display = width >= eva.MAGI_MIN_WIDTH
        for name in ("#target-plate", "#target-clock-label", "#target-clock"):
            self.query_one(name).display = side and (
                name == "#target-plate" or height >= eva.TARGET_TALL
            )
        self.query_one("#brand-sub").display = width >= eva.MAGI_MIN_WIDTH
        self.query_one("#brand").display = width >= 50
        self.set_class(side, "-side")
        if not side:
            for name in ("#related", "#sync", "#comm"):
                self.query_one(name).display = False
        self.set_class(wide, "-wide")
        self.set_class(layout.show_detail and not side, "-stacked")
        hints.show(layout.hints)
        self.set_class(not layout.show_project, "-narrow")
        self.set_class(not layout.show_detail, "-compact")
        if self.session_list.show_project != layout.show_project:
            self.session_list.show_project = layout.show_project
            self.session_list.fit()
        self._render_tabs()
        self._render_status()
        self._render_prompt()
        self.call_after_refresh(self._point_leader)
        self.call_after_refresh(self._fit_extras)

    @on(SessionList.Fitted)
    def _header_changed(self, message: SessionList.Fitted) -> None:
        self.query_one("#header", Static).update(message.header)

    # --- tabs ------------------------------------------------------------

    async def _rebuild_tabs(self) -> None:
        container = self.query_one("#tabs")
        await container.remove_children()
        await container.mount(*[TabLabel(tab, index) for index, tab in enumerate(self.tabs)])

    def _render_tabs(self) -> None:
        container = self.query_one("#tabs")
        labels = list(container.query(TabLabel))
        if not labels or len(labels) != len(self.tabs):
            return
        extra = labels[0].styles.gutter.width + labels[0].styles.margin.width
        room = self.size.width - container.styles.gutter.width - container.styles.margin.width
        texts = view.fit_tab_labels(self.tabs, self.tab_index, room, extra)
        for index, (label, text) in enumerate(zip(labels, texts, strict=False)):
            label.update(text)
            label.set_class(index == self.tab_index, "-active")
        self.query_one("#subjects", SubjectColumn).show(self.tabs, self.tab_index, self._traces)
        self._render_column()

    def select_tab(self, index: int) -> None:
        if not self.tabs:
            return
        self.tab_index = index % len(self.tabs)
        self.tab_name = self.tabs[self.tab_index].name
        self._render_tabs()
        self._refresh_rows(from_top=True)
        self.raster.sweep()

    @on(TabLabel.Pressed)
    def _tab_pressed(self, message: TabLabel.Pressed) -> None:
        self._close_prompt()
        self.clear_message()
        self.select_tab(message.index)

    # --- rows and selection ----------------------------------------------

    async def _apply_snapshot(self, snapshot: Snapshot, select_key: str | None = None) -> None:
        """Show a fresh reading of the index, keeping tab and selection."""
        first = not self.loaded
        names = [tab.name for tab in self.tabs]
        self.snapshot = snapshot
        self.loaded = True
        self._verdicts.clear()
        self._by_folder = {}
        for session in sorted(
            snapshot.sessions.values(), key=lambda s: s.updated or s.started or "", reverse=True
        ):
            if session.cwd:
                self._by_folder.setdefault(session.cwd, []).append(session)
        self._previews.clear()
        self._traces = {
            name: eva.trace(
                eva.activity(
                    [row.stamp for row in snapshot.rows if row.provider == name], self.clock()
                )
            )
            for name, _label in snapshot.providers
        }
        self.tabs = view.build_tabs(snapshot.providers, snapshot.rows)
        new_names = [tab.name for tab in self.tabs]
        self.tab_index = new_names.index(self.tab_name) if self.tab_name in new_names else 0
        self.tab_name = self.tabs[self.tab_index].name
        if new_names != names:
            await self._rebuild_tabs()
        if self._question is not None and self._question.key not in snapshot.sessions:
            self._close_prompt()
        self._render_tabs()
        if self._corpus is not None:
            self._start_corpus()  # what changed since it was read
        self._refresh_rows(select_key)
        self._render_status()
        boot = self.query("#boot")
        if first and boot:
            boot.first(BootLog).facts = eva.BootFacts(
                sessions=len(snapshot.rows),
                drives=snapshot.drives,
                lost=snapshot.offline,
                subjects=tuple((tab.label, tab.count) for tab in self.tabs if tab.name != view.ALL),
            )
        elif not first and not boot:
            self.raster.sweep()

    def _refresh_rows(
        self, select_key: str | None = None, from_top: bool = False, reset: bool = False
    ) -> None:
        """Show the rows of the current tab and filter. The selection goes to
        ``select_key``, else stays on its session, else returns to the one
        this tab had; ``from_top`` (a tab switch) starts at the first row
        when none of those is listed. ``reset`` (a new search): the same row
        when it is still listed, else the first one; never another row of
        the selected session, which may be far down among the messages."""
        provider = self.tab_name or view.ALL
        rows = view.filter_rows(self.snapshot.rows, provider, self.needle)
        searching = self._searching()
        if searching:
            listed = {row.key: row for row in view.filter_rows(self.snapshot.rows, provider)}
            rows = view.search_rows(rows, listed, self._found, self._reading)
            if self._found_for != (self.needle.strip(), provider):
                self._schedule_search()
        current = self.session_list.current
        key = select_key or (
            view.row_ident(current)
            if current and current.kind != "section" and self._listed_under(current, provider)
            else self._selected.get(provider)
        )
        if reset and not any(view.row_ident(row) == key for row in rows):
            key, from_top = None, True
        self.session_list.set_rows(
            rows,
            key,
            show_provider=provider == view.ALL,
            provider_labels=dict(self.snapshot.providers),
            widths=view.column_widths(
                [eva.drawn_row(row) for row in rows], dict(self.snapshot.providers)
            ),
            empty_text=view.empty_text(self.loaded, self.needle, messages=searching),
            from_top=from_top,
        )
        entries = sum(1 for row in rows if view.selectable(row))
        self.query_one("#board").border_subtitle = f"{entries:03d} ENTRIES"
        tab = self.tabs[self.tab_index] if self.tabs else None
        self.query_one("#board").border_title = (
            f"{eva.BOARD_TITLE} ─ {eva.tab_number(tab)} {escape(tab.label.upper())}"
            if tab is not None
            else eva.BOARD_TITLE
        )

    @staticmethod
    def _listed_under(row: view.Row, provider: str) -> bool:
        return provider == view.ALL or row.provider == provider

    def current_session(self) -> Session | None:
        row = self.session_list.current
        return self.snapshot.sessions.get(row.key) if row else None

    @on(SessionList.Highlighted)
    def _highlighted(self, message: SessionList.Highlighted) -> None:
        if message.key is not None and self.tab_name is not None:
            self._selected[self.tab_name] = message.key
        self._render_detail()
        self._schedule_verdict()
        self._point_leader(flash=True)

    @on(SessionList.Scrolled)
    def _list_scrolled(self, message: SessionList.Scrolled) -> None:
        self._point_leader()

    def _point_leader(self, flash: bool = False) -> None:
        """Tie the selected row to the target readout."""
        leader = self.query_one("#leader", Leader)
        if not leader.display:
            return
        listing = self.session_list
        row = listing.cursor - listing.top
        target = self.query_one("#target-plate").region.y - leader.region.y
        if not self.loaded or listing.current is None or not 0 <= row < listing.size.height:
            leader.point(None, None)
            return
        leader.point(listing.region.y - leader.region.y + row, target, flash)

    @on(SessionList.Pressed)
    def _row_pressed(self, message: SessionList.Pressed) -> None:
        """A click selects (the list has done that); a double-click is Enter.
        Either takes a pending prompt away first."""
        if self._leaving is not None:
            return
        self._close_prompt()
        self.clear_message()
        current = self.session_list.current
        if message.double and current is not None and current.key == message.key:
            self.do_resume()

    def on_click(self, event: events.Click) -> None:
        # A click anywhere else takes a pending prompt away too. One in the
        # folder input only moves its cursor.
        bar = self.query_one("#folderbar")
        if event.widget is None or bar not in event.widget.ancestors_with_self:
            self._close_prompt()

    # --- detail pane -----------------------------------------------------

    def _render_detail(self) -> None:
        session = self.current_session()
        verdict = self._verdicts.get(session.key) if session else None
        for name, value in view.detail_fields(session, verdict, self.tz).items():
            self.query_one(f"#detail-{name}", Static).update(value)
        cursor = self.session_list.cursor
        self.query_one("#detail").border_title = (
            f"{eva.DETAIL_TITLE} ─ SESSION {eva.row_number(self.session_list.number(cursor))}"
            if session is not None
            else eva.DETAIL_TITLE
        )
        self.query_one("#target").border_title = (
            f"{eva.TARGET_TITLE} {eva.row_number(self.session_list.number(cursor))}"
            if session is not None
            else eva.TARGET_TITLE
        )
        row = self.session_list.current
        self.query_one("#target-plate", StatePlate).show(eva.signal(row) if row else "none")
        self.query_one("#target-clock", SegmentClock).show(row.stamp if row else None)
        self.query_one("#magi", MagiPanel).show(eva.magi_votes(session, verdict))
        key = session.key if session is not None else None
        if key != self._target_key:
            # A new target: the readout prints itself, as the screens' text does.
            self._target_key = key
            values = list(self.query("#detail .detail-value"))
            self._render_related()
            self._schedule_preview()
            self.call_after_refresh(self._fit_extras)
            magi = self.query_one("#magi", MagiPanel)
            magi.deliberate()
            magi.show(magi.votes)
            self.raster.type_in(self.query_one("#target-plate"), *values)

    # --- the panels round the log: activity, drives, related, sync, comm ------

    def _render_column(self) -> None:
        """The left column under the subjects: the selected subject's days,
        and the drive board."""
        if not self.query_one("#subjects-box").display:
            return
        tab = self.tabs[self.tab_index] if self.tabs else None
        name = tab.name if tab else view.ALL
        cells = self.query_one("#activity", ActivityCells)
        width = cells.size.width or eva.SUBJECT_WIDTH
        stamps = [
            row.stamp for row in self.snapshot.rows if name == view.ALL or row.provider == name
        ]
        grid = eva.activity_grid(stamps, self.clock(), max((width - 3) // 2, 1))
        cells.set_lines(eva.activity_lines(grid, width, (tab.label if tab else "ALL").upper()))
        self._render_drives()

    def _drives(self) -> list[eva.Drive]:
        """The drives of the last status report, one a label, with the
        sessions listed on each."""
        counts: dict[str, int] = {}
        for session in self.snapshot.sessions.values():
            counts[session.mount] = counts.get(session.mount, 0) + 1
        drives: dict[str, eva.Drive] = {}
        for mount in self.snapshot.mounts:
            label = view.clean(str(mount.get("label") or mount.get("path") or "?"))
            held = counts.get(str(mount.get("path")), 0)
            known = drives.get(label)
            if known is not None:
                drives[label] = dataclasses.replace(known, sessions=known.sessions + held)
                continue
            drives[label] = eva.Drive(
                label=label,
                fstype=view.clean(str(mount.get("fstype") or "")),
                device=view.clean(str(mount.get("device") or "")),
                letter=view.clean(str(mount.get("letter") or "")),
                state=str(mount.get("state") or ("mounted" if mount.get("mounted") else "absent")),
                sessions=held,
            )
        return list(drives.values())

    def _render_drives(self, room: int | None = None) -> None:
        board = self.query_one("#drives", DriveBoard)
        drives = self._drives()
        if room is not None:
            drives = drives[: max((room - 1) // 2, 0)]
        board.set_lines(eva.drive_lines(drives, board.size.width or eva.SUBJECT_WIDTH))

    def _render_related(self) -> None:
        panel = self.query_one("#related", RelatedPanel)
        session = self.current_session()
        if session is None:
            panel.set_lines([])
            return
        digits = {tab.name: eva.tab_number(tab) for tab in self.tabs}
        labels = dict(self.snapshot.providers)
        now = self.clock()
        related = [
            eva.Related(
                digits.get(other.provider, "--"),
                view.clean(labels.get(other.provider, other.provider)),
                view.shown_title(other.title, other.id),
                view.age_text(other.updated or other.started, now),
            )
            for other in self._by_folder.get(session.cwd or "", [])
            if other.key != session.key
        ]
        folder = view.clean(session.cwd or "")
        panel.set_lines(
            eva.related_lines(related, folder, panel.size.width or 44, self._related_limit)
        )

    def _fit_extras(self) -> None:
        """Give the space the column and the readout have left to the panels
        that fit in it, in order: the activity cells, then the drives on the
        left; the MAGI's shape, the communication log, the sync graph, the
        related sessions on the right. A panel that does not fit is hidden."""
        if not self.is_attached:
            return
        box = self.query_one("#subjects-box")
        if box.display:
            room = box.content_size.height - self.query_one("#subjects").outer_size.height
            cells = self.query_one("#activity", ActivityCells)
            show_cells = room >= len(cells.lines) + 1 and bool(cells.lines)
            room -= (len(cells.lines) + 1) * show_cells
            drives = len(self._drives())
            show_drives = room >= 4 and drives > 0
            self._set_extra(cells, show_cells)
            self._set_extra(self.query_one("#drives"), show_drives)
            if show_drives:
                self._render_drives(room - 1)
        target = self.query_one("#target")
        if not (target.display and self.has_class("-side")):
            return
        fixed = sum(
            widget.outer_size.height
            for widget in target.children
            if widget.display and widget.id not in ("magi", "related", "sync", "comm")
        )
        room = target.content_size.height - fixed
        magi = self.query_one("#magi", MagiPanel)
        shaped = room >= eva.MAGI_SHAPE_HEIGHT + 1 + 8
        room -= (eva.MAGI_SHAPE_HEIGHT if shaped else 3) + 1
        comm = room >= 7
        room -= 7 * comm
        sync = comm and room >= eva.SYNC_ROWS + 3
        room -= (eva.SYNC_ROWS + 3) * sync
        related = sync and room >= 4
        self._related_limit = max(min(room - 2, RELATED_MAX), 1)
        state = (shaped, comm, sync, related, self._related_limit)
        if state == self._extras:
            return
        self._extras = state
        magi.roomy = shaped
        magi.show(magi.votes)
        self._set_extra(self.query_one("#comm"), comm)
        self._set_extra(self.query_one("#sync"), sync)
        self._set_extra(self.query_one("#related"), related)
        self._render_related()
        self._schedule_preview()

    @staticmethod
    def _set_extra(widget: Widget, show: bool) -> None:
        if widget.display != show:
            widget.display = show

    def _schedule_preview(self) -> None:
        """Read the selected session's transcript for the sync graph and the
        communication log once the selection has settled on it."""
        if self._preview_timer is not None:
            self._preview_timer.stop()
            self._preview_timer = None
        session = self.current_session()
        wanted = self.query_one("#comm").display or self.query_one("#sync").display
        key = session.key if session is not None and wanted else None
        self._preview_wanted = key
        preview = self._previews.get(key) if key else None
        self._show_preview(preview)
        if key is None or preview is not None:
            return
        self._preview_timer = self.set_timer(PREVIEW_DELAY, partial(self._start_preview, key))

    def _start_preview(self, key: str) -> None:
        self._preview_timer = None
        session = self.snapshot.sessions.get(key)
        if session is None or key != self._preview_wanted:
            return
        self._read(self._preview_thread, key, session, self.snapshot.index, group="preview")

    def _preview_thread(self, key: str, session: Session, index: Index | None) -> None:
        turns: deque[tuple[str, str, str | None]] = deque(maxlen=PREVIEW_TURNS)
        beats: list[tuple[str, str | None]] = []
        total, capped, error = 0, False, None
        deadline = time.monotonic() + PREVIEW_SECONDS
        try:
            for turn in self.engine.transcript(session.key, index=index):
                if self._preview_wanted != key:
                    return  # the selection moved on
                role, text, ts = (tuple(turn) + (None, None, None))[:3]
                role = role if role in ("user", "assistant", "tool") else "assistant"
                stamp = ts if isinstance(ts, str) else None
                lines = view.clean("" if text is None else str(text)[:400]).strip().splitlines()
                if role != "tool":  # the log keeps what was said; the graph counts all
                    turns.append((role, lines[0] if lines else view.EMPTY, stamp))
                beats.append((role, stamp))
                total += 1
                if total >= PREVIEW_MAX_TURNS or time.monotonic() > deadline:
                    capped = True
                    break
        except Exception as exc:
            error = view.error_text(exc)
        preview = eva.Preview(tuple(turns), tuple(beats), total, capped, error)
        self._ui(self._preview_done, key, preview)

    def _preview_done(self, key: str, preview: eva.Preview) -> None:
        self._previews[key] = preview
        while len(self._previews) > PREVIEW_CACHE:
            self._previews.popitem(last=False)
        if key == self._preview_wanted:
            self._show_preview(preview)
            self.raster.type_in(self.query_one("#comm"), stagger=0)

    def _show_preview(self, preview: eva.Preview | None) -> None:
        sync = self.query_one("#sync", SyncGraph)
        sync.set_lines(eva.sync_lines(preview, sync.size.width or 44))
        self.query_one("#comm", CommLog).show(preview)

    def _schedule_verdict(self) -> None:
        """Ask the engine what an import of the selected foreign session can
        be, once the selection has settled on it."""
        if self._verdict_timer is not None:
            self._verdict_timer.stop()
            self._verdict_timer = None
        session = self.current_session()
        if (
            session is None
            or session.origin == "native"
            or session.imported_as
            or session.key in self._verdicts
            or session.key in self._verdict_pending
        ):
            return
        self._verdict_timer = self.set_timer(
            VERDICT_DELAY, partial(self._start_verdict, session.key)
        )

    def _start_verdict(self, key: str) -> None:
        self._verdict_timer = None
        session = self.snapshot.sessions.get(key)
        if session is None or key in self._verdict_pending:
            return
        self._verdict_pending.add(key)
        self._read(self._verdict_thread, key, session, self.snapshot.index, group="verdict")

    def _verdict_thread(self, key: str, session: Session, index: Index | None) -> None:
        try:
            verdict = view.make_verdict(self.engine.import_options(session, index=index))
        except Exception as exc:  # shown in the detail pane, never fatal
            verdict = view.failed_verdict(exc)
        self._ui(self._verdict_done, key, index, verdict)

    def _verdict_done(self, key: str, index: Index | None, verdict: view.Verdict) -> None:
        self._verdict_pending.discard(key)
        if index is not self.snapshot.index:
            self._schedule_verdict()  # the index moved on; ask again if still selected
            return
        self._verdicts[key] = verdict
        self._render_detail()
        if self._asking is not None and self._asking[0] == key:
            _key, what, stay, asked = self._asking
            self._asking = None
            session = self._still_wanted(asked, key)
            if session is not None:
                self._answer_import(session, verdict, what, stay)

    def _ask_import(self, session: Session, what: str, stay: bool = False) -> None:
        """Enter (``what`` is ``prompt``), ``i`` (``import``) or ``t``
        (``folder``) on a foreign row: each starts from what the engine says
        the import can be. That is known already when the row was selected
        for a moment; otherwise the key waits for the answer, and the next
        key press forgets it. An answer that failed is asked for again."""
        verdict = self._verdicts.get(session.key)
        if verdict is not None and verdict.known:
            self._answer_import(session, verdict, what, stay)
            return
        self._verdicts.pop(session.key, None)
        self._asking = (session.key, what, stay, self._asked)
        if self._verdict_timer is not None:
            self._verdict_timer.stop()
            self._verdict_timer = None
        self._start_verdict(session.key)

    def _answer_import(
        self, session: Session, verdict: view.Verdict, what: str, stay: bool
    ) -> None:
        if not verdict.known:  # asking failed: the reason, and nothing to choose from
            self.set_message(verdict.reason, error=True)
            return
        question = self._about(session, stay=stay, verdict=verdict)
        step = "prompt" if what == "prompt" else view.import_step(verdict, what)
        if step == "prompt":
            self._open_prompt(view.foreign_mode(verdict), question)
        elif step == "refuse":
            self.set_message(verdict.reason or "this session cannot be imported", error=True)
        elif step == "folder":
            self._open_folder(question, "import")
        else:  # "warn" or "confirm": the question of that name
            self._open_prompt(step, question)

    # --- keys ------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self.app.screen is not self:
            return  # on its way when the pager or the help came up: theirs, or nobody's
        if self._leaving is not None:
            event.stop()
            event.prevent_default()
            return
        for boot in self.query(BootLog):
            boot.finish()  # a key ends the boot log, and does what it does
        mode = "filter" if self.focused is self.filter_input else self.mode
        action = view.key_action(mode, event.key, event.character)
        if action is None:
            if mode in view.MODAL_MODES:
                event.stop()
                event.prevent_default()
            return
        event.stop()
        event.prevent_default()
        self.clear_message()
        self._forget()
        getattr(self, f"do_{action}")(event)

    def _forget(self) -> None:
        """A key was pressed, or something clicked: whatever a worker still
        has to answer to an earlier key is not wanted any more."""
        self._asked += 1
        self._asking = None

    def _still_wanted(self, asked: int, key: str) -> Session | None:
        """The session a worker's answer is about, when the answer is still
        wanted: nothing was pressed since, no prompt came up and nothing is
        being typed, and the row is still the selected one."""
        if asked != self._asked or self.mode != "list" or isinstance(self.focused, Input):
            return None
        session = self.current_session()
        return session if session is not None and session.key == key else None

    def do_cursor_up(self, event: events.Key | None = None) -> None:
        self.session_list.move("up")

    def do_cursor_down(self, event: events.Key | None = None) -> None:
        self.session_list.move("down")

    def do_cursor_pageup(self, event: events.Key | None = None) -> None:
        self.session_list.move("pageup")

    def do_cursor_pagedown(self, event: events.Key | None = None) -> None:
        self.session_list.move("pagedown")

    def do_cursor_home(self, event: events.Key | None = None) -> None:
        self.session_list.move("home")

    def do_cursor_end(self, event: events.Key | None = None) -> None:
        self.session_list.move("end")

    def do_tab_prev(self, event: events.Key | None = None) -> None:
        self.select_tab(view.cycle_tab(self.tab_index, -1, len(self.tabs)))

    def do_tab_next(self, event: events.Key | None = None) -> None:
        self.select_tab(view.cycle_tab(self.tab_index, 1, len(self.tabs)))

    def do_tab_digit(self, event: events.Key) -> None:
        index = view.tab_for_digit(self.tabs, event.character or "")
        if index is not None:
            self.select_tab(index)

    def do_quit(self, event: events.Key | None = None) -> None:
        self.leave()

    def do_help(self, event: events.Key | None = None) -> None:
        self._show(HelpScreen())

    def _show(self, screen: Screen) -> None:
        """Put the help or the pager over this screen. Nothing when this
        screen is not the one on top: one of them is open already."""
        if self.app.screen is self:
            self.app.push_screen(screen)

    def do_escape(self, event: events.Key | None = None) -> None:
        """Esc: stop a full rescan, else clear the filter, else stop a quick
        scan, or the scan that follows a mount."""
        if self._scan is not None and self._scan["deep"]:
            self._cancel_scan()
        elif self.needle:
            self.do_filter_clear()
        elif self._scan is not None:
            self._cancel_scan()
        elif self._mount is not None and self._mount["scanning"]:
            self._mount["cancel"].set()
            self.set_message(f"stopping the scan{view.ELLIPSIS}")

    # --- filter ----------------------------------------------------------

    def do_filter(self, event: events.Key | None = None) -> None:
        self.query_one("#filterbar").display = True
        self.filter_input.focus()

    def do_filter_accept(self, event: events.Key | None = None) -> None:
        self.query_one("#filterbar").display = bool(self.needle)
        self.session_list.focus()

    def do_filter_clear(self, event: events.Key | None = None) -> None:
        self.filter_input.value = ""
        self._set_needle("")
        self.query_one("#filterbar").display = False
        self.session_list.focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, message: Input.Changed) -> None:
        self._set_needle(message.value)

    def _set_needle(self, needle: str) -> None:
        if needle != self.needle:
            self.needle = needle
            if len(needle.strip()) >= view.SEARCH_MIN:
                self._start_corpus()
            self._refresh_rows(reset=self._searching())

    def do_filter_enter(self, event: events.Key | None = None) -> None:
        """Enter in the search box: a message opens at its place, as a
        search result does in a chat app; anything else leaves the box for
        the list."""
        row = self.session_list.current
        self.do_filter_accept()
        if row is not None and row.kind == "hit":
            self._view_hit(row)

    # --- search: what was said ------------------------------------------

    def _searching(self) -> bool:
        """Whether the list shows what was said as well as the names."""
        return self._corpus is not None and len(self.needle.strip()) >= view.SEARCH_MIN

    def _start_corpus(self) -> None:
        """Have the sessions' text read, for a search: all of it the first
        time, after that what changed."""
        if self._corpus is None:
            if self._corpus_failed:
                return
            try:
                self._corpus = self.engine.corpus()
            except Exception as exc:
                self._corpus_failed = True
                self.set_message(view.error_text(exc), error=True)
                return
            self._reading = view.Reading()
        if not self.loaded:
            return  # the first snapshot starts it
        if self._corpus_busy:
            self._corpus_again = True
            return
        self._corpus_busy = True
        self._corpus_again = False
        sessions = list(self.snapshot.sessions.values())
        self._corpus.keep(self.snapshot.sessions.keys())
        self._read(self._corpus_thread, self._corpus, sessions, self.snapshot.index, group="corpus")

    def _corpus_thread(self, corpus: Corpus, sessions: list[Session], index: Index | None) -> None:
        last = 0.0

        def progress(state: Progress) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last >= SEARCH_PROGRESS:
                last = now
                self._ui(self._corpus_progress, state, False)

        state = corpus.read(
            sessions,
            partial(self.engine.transcript, index=index),
            progress,
            stop=self._corpus_stop.is_set,
        )
        self._ui(self._corpus_progress, state, True)

    def _corpus_progress(self, state: Progress, finished: bool) -> None:
        if not finished and state.total == 0:
            return
        self._reading = view.Reading(state.done, state.total, state.failed, state.full, finished)
        if finished:
            self._corpus_busy = False
            if self._corpus_again:
                self._start_corpus()
        if self._searching():
            self._found_for = None  # more text to search: ask again
            self._refresh_rows()

    def _schedule_search(self) -> None:
        """Search what was said once the typing pauses."""
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(SEARCH_DELAY, self._start_search)

    def _start_search(self) -> None:
        self._search_timer = None
        if not self._searching() or self._corpus is None:
            return
        provider = self.tab_name or view.ALL
        keys = (
            None
            if provider == view.ALL
            else {row.key for row in self.snapshot.rows if row.provider == provider}
        )
        self._search_asked += 1
        needle = self.needle.strip()
        self._read(
            self._search_thread,
            self._corpus,
            needle,
            provider,
            keys,
            self._search_asked,
            group="search",
        )

    def _search_thread(
        self, corpus: Corpus, needle: str, provider: str, keys: set[str] | None, asked: int
    ) -> None:
        def stale() -> bool:
            return asked != self._search_asked

        found = corpus.search(needle, keys, stop=stale)
        if not stale():
            self._ui(self._search_done, needle, provider, found, asked)

    def _search_done(self, needle: str, provider: str, found: Found, asked: int) -> None:
        if asked != self._search_asked or not self._searching():
            return
        new = self._shown_for != (needle, provider)
        self._found, self._found_for = found, (needle, provider)
        self._shown_for = (needle, provider)
        self._refresh_rows(reset=new)  # a new search starts at its first result

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if event.widget is self.filter_input:
            self._close_prompt()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if event.widget is self.filter_input:
            # The filter box left by a click: an empty one has nothing to show.
            if not self.needle:
                self.query_one("#filterbar").display = False
        elif (
            self.mode == "folder"
            and self.focused is not self.folder_input
            and self.app.app_focus  # the terminal lost the focus: it comes back
        ):
            # The folder input left by a click: its question goes with it.
            self._close_prompt()

    # --- prompts (the lines over the key hints) ---------------------------

    def _about(self, session: Session, **facts: Any) -> Question:
        """A question about the selected session."""
        row = self.session_list.current
        where = row.where if row is not None and row.key == session.key else ""
        return Question(session.key, view.shown_title(session.title, session.id), where, **facts)

    def _open_prompt(self, mode: str, question: Question) -> None:
        """Show a prompt; ``mode`` names its keys (``view.KEYMAPS``)."""
        self._hide_folder()
        self.mode = mode
        self._question = question
        self._render_prompt()
        self.query_one("#prompt").display = True
        self._announce_prompt()

    def _render_prompt(self) -> None:
        """Word the prompt for the width there is."""
        question = self._question
        if question is None or self.mode in ("list", "folder"):
            return
        prompt = self.query_one("#prompt", Static)
        room = self.size.width - prompt.styles.gutter.width - prompt.styles.margin.width
        prompt.update("\n".join(self._prompt_lines(self.mode, question, room)))
        danger = eva.severity(self.mode) == "danger"
        prompt.set_class(danger, "-danger")
        prompt.set_class(not danger, "-caution")
        self._draw_stripes()

    def _draw_stripes(self) -> None:
        """The hazard band over and under a prompt that cannot be taken back."""
        danger = self._question is not None and eva.severity(self.mode) == "danger"
        show = danger and self.mode not in ("list", "folder") and self.size.height >= 12
        for name, phase in (
            ("#stripe-top", self._stripe_phase),
            ("#stripe-bottom", -self._stripe_phase),
        ):
            band = self.query_one(name, HazardBand)
            band.display = show
            if show:
                band.draw(self.size.width, phase)

    def _announce_prompt(self) -> None:
        """A prompt arrives: a danger band marches, a caution frame flashes."""
        if not self.motion:
            return
        prompt = self.query_one("#prompt")
        prompt.add_class("-flash")
        self.set_timer(eva.FRAME * 2, lambda: prompt.remove_class("-flash"))
        if eva.severity(self.mode) == "danger" and self._stripe_timer is None:
            self._stripe_timer = self.set_interval(0.12, self._march)

    def _march(self) -> None:
        if self._question is None or eva.severity(self.mode) != "danger":
            if self._stripe_timer is not None:
                self._stripe_timer.stop()
                self._stripe_timer = None
            return
        self._stripe_phase += 1
        self._draw_stripes()

    @staticmethod
    def _prompt_lines(mode: str, question: Question, room: int) -> list[str]:
        if mode == "mount":
            return [view.mount_prompt(question.where, room)]
        if mode == "warn":
            return view.warn_prompt(question.verdict.warning, room)
        if mode in ("tool", "start"):
            label = question.tools[question.tool][1]
            if mode == "tool":
                return [view.tool_prompt(label, room)]
            return [view.start_prompt(label, question.folder, room)]
        if mode == "undo":
            return [view.undo_prompt(question.files, question.size, room)]
        if mode == "undo_used":
            return [view.undo_used_prompt(question.differ, room)]
        if mode == "confirm":
            return [view.confirm_prompt(question.title, question.where, room)]
        return view.foreign_prompt(question.where, question.verdict, room)

    def _close_prompt(self) -> None:
        """Take the prompt or the folder input away, with its question."""
        self._forget()
        if self.mode == "list":
            return
        self.mode = "list"
        self._question = None
        self.query_one("#prompt").display = False
        self._draw_stripes()
        self._hide_folder()

    def _take_prompt(self) -> Question | None:
        question = self._question
        self._close_prompt()
        return question

    def do_prompt_cancel(self, event: events.Key | None = None) -> None:
        self._close_prompt()

    def do_prompt_import_resume(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_import(question.key, then_open=True, stay=question.stay)

    def do_prompt_import_only(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_import(question.key, then_open=False, stay=True)

    def do_prompt_warn_resume(self, event: events.Key | None = None) -> None:
        self._warn(then_open=True)

    def do_prompt_warn_only(self, event: events.Key | None = None) -> None:
        self._warn(then_open=False)

    def _warn(self, then_open: bool) -> None:
        """An experimental import asks a second time, with its warning."""
        question = self._question
        if question is not None:
            question.then_open = then_open
            self._open_prompt("warn", question)

    def do_warn_yes(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_import(
                question.key,
                then_open=question.then_open,
                stay=question.stay or not question.then_open,
                target=question.target,
                experimental=True,
            )

    def do_prompt_folder(self, event: events.Key | None = None) -> None:
        if self._question is not None:
            self._open_folder(self._question, "import")

    def do_prompt_continue(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_continue(question.key)

    def do_prompt_view(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._view(question.key)

    def do_prompt_mount(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        session = self.snapshot.sessions.get(question.key) if question is not None else None
        if question is not None and session is not None:
            self._start_mount(session, view.drive_of(question.where))

    def do_confirm_yes(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_import(question.key, then_open=False, stay=True)

    def do_confirm_no(self, event: events.Key | None = None) -> None:
        self._close_prompt()

    # --- the folder input (in the prompt's place) --------------------------

    def _open_folder(self, question: Question, purpose: str) -> None:
        """Ask for a folder: to import into, or to start a tool in."""
        question.purpose = purpose
        self.query_one("#prompt").display = False
        for name in ("#stripe-top", "#stripe-bottom"):
            self.query_one(name).display = False
        self.mode = "folder"
        self._question = question
        text = (question.folder if purpose == "continue" else "") or view.FOLDER_START
        if not view.showable(text):
            # Never into the input: it would be drawn, and typed after, as it is.
            text = view.FOLDER_START
            self.set_message(view.FOLDER_UNSHOWABLE)
        self._completed = text
        self.query_one("#folder-label", Static).update(view.FOLDER_LABELS[purpose])
        box = self.folder_input
        box.value = text
        self.query_one("#folderbar").display = True
        box.focus()
        # The bar has a width only once it is laid out: a long folder shows
        # its end, where typing goes on, from then.
        self.call_after_refresh(box.show_end)

    def _hide_folder(self) -> None:
        bar = self.query_one("#folderbar")
        if bar.display:
            bar.display = False
            if self.focused is None or self.focused is self.folder_input:
                self.session_list.focus()

    def do_folder_cancel(self, event: events.Key | None = None) -> None:
        self._close_prompt()

    def do_folder_stay(self, event: events.Key | None = None) -> None:
        """Shift-Tab: the focus stays in the folder input."""

    @on(Input.Changed, "#folder")
    def _folder_changed(self, message: Input.Changed) -> None:
        # Typing takes the last answer away; a completion's own change
        # leaves the names it has just shown.
        if message.value != self._completed:
            self.clear_message()

    def do_folder_complete(self, event: events.Key | None = None) -> None:
        """Tab: complete the folder's last name. The listing is a worker's."""
        self._read(self._complete_thread, self._asked, self.folder_input.value, group="folder")

    def _complete_thread(self, asked: int, text: str) -> None:
        try:
            completion = view.complete_folder(text, self.engine.list_folder, self.home)
        except Exception as exc:  # not the listing's own failure: that one is in the answer
            completion = view.Completion(text, error=view.error_text(exc))
        self._ui(self._completed_folder, asked, text, completion)

    def _completed_folder(self, asked: int, text: str, completion: view.Completion) -> None:
        box = self.folder_input
        if asked != self._asked or self.mode != "folder" or box.value != text:
            return  # typed on, or gone, while the folder was listed
        if completion.error:
            self.set_message(completion.error, error=True)
            return
        self._completed = completion.text
        box.value = completion.text
        box.cursor_position = len(completion.text)
        said = view.completion_message(completion)
        if said:
            self.set_message(said)

    def do_folder_accept(self, event: events.Key | None = None) -> None:
        """Enter: take the folder when it is one. Looked at in a worker."""
        text = self.folder_input.value
        path = view.expand_folder(text.strip(), self.home)
        self._read(self._folder_thread, self._asked, text, path, group="folder")

    def _folder_thread(self, asked: int, text: str, path: str) -> None:
        folder = ""
        try:
            # One spelling first (``a/link/../b`` is ``a/b``): what is looked
            # at here is the path that is used afterwards.
            wanted = os.path.normpath(path) if path and os.path.isabs(path) else ""
            if wanted and os.path.isdir(wanted):
                folder = wanted
        except (OSError, ValueError):  # a NUL in the text
            folder = ""
        self._ui(self._folder_known, asked, text, path, folder)

    def _folder_known(self, asked: int, text: str, path: str, folder: str) -> None:
        question = self._question
        if asked != self._asked or self.mode != "folder" or question is None:
            return
        if self.folder_input.value != text:
            return
        if not folder:
            self.set_message(view.folder_error(path), error=True)  # and the input stays
        elif question.purpose == "continue":
            question.folder = folder
            self._open_prompt("start", question)
        elif question.verdict.experimental:
            question.target, question.then_open = folder, False
            self._open_prompt("warn", question)
        else:
            self._close_prompt()
            self._start_import(question.key, then_open=False, stay=True, target=folder)

    # --- resume ----------------------------------------------------------

    def do_resume(self, event: events.Key | None = None, stay: bool = False) -> None:
        """Enter: a native session resumes; a foreign one resumes its native
        copy when it has one, else asks what to do: about its drive when
        that is not mounted, else about the import. A message found by a
        search opens the transcript at its place instead."""
        row = self.session_list.current
        if row is not None and row.kind == "hit":
            self._view_hit(row)
            return
        session = self.current_session()
        if session is None:
            return
        if session.origin == "native":
            self._start_open(session.key, session.title, stay)
        elif session.imported_as:
            self._start_open(session.imported_as, session.title, stay)
        elif session.offline:
            self._ask_volume(session, at_once=False)
        else:
            self._ask_import(session, "prompt", stay)

    def do_resume_stay(self, event: events.Key | None = None) -> None:
        self.do_resume(stay=True)

    def _start_refusal(self, session: Session, what: str) -> None:
        """For a session whose drive is not mounted: ask the engine to
        ``view`` or ``import`` it and show what it answers in the status
        line. Nothing is started: the engine refuses both."""
        self._read(self._refusal_thread, session, what, self.snapshot.index, group="refusal")

    def _refusal_thread(self, session: Session, what: str, index: Index | None) -> None:
        reason = ""
        try:
            if what == "import":
                options = self.engine.import_options(session, index=index)
                reason = "" if options.possible else options.reason or ""
            else:
                next(iter(self.engine.transcript(session.key, index=index)), None)
        except Exception as exc:
            reason = view.error_text(exc)
        self._ui(self.set_message, reason or view.clean(session.note) or "drive not mounted", True)

    # --- mount -------------------------------------------------------------

    def _ask_volume(self, session: Session, at_once: bool) -> None:
        """Enter or ``m`` on a session whose drive is not mounted: is the
        drive there to be mounted at all? Then the prompt (Enter), or the
        mount ``at_once`` (``m``)."""
        self._read(
            self._volume_thread,
            self._asked,
            session.key,
            session.mount,
            self.snapshot.index,
            at_once,
            group="volume",
        )

    def _volume_thread(
        self, asked: int, key: str, mount: str, index: Index | None, at_once: bool
    ) -> None:
        try:
            state = view.volume_state(self.engine.volumes(index=index), mount)
        except Exception as exc:
            self._ui(self._volume_known, asked, key, None, view.error_text(exc), at_once)
            return
        self._ui(self._volume_known, asked, key, state, None, at_once)

    def _volume_known(
        self, asked: int, key: str, state: str | None, error: str | None, at_once: bool
    ) -> None:
        session = self._still_wanted(asked, key)
        if session is None:
            return
        question = self._about(session)
        drive = view.drive_of(question.where)
        if error:
            self.set_message(error, error=True)
        elif state == view.VOLUME_ABSENT:
            self.set_message(view.not_attached_text(drive), error=True)
        elif at_once:
            self._start_mount(session, drive)
        else:
            self._open_prompt("mount", question)

    def do_mount(self, event: events.Key | None = None) -> None:
        """``m``: mount the drive of the selected session."""
        session = self.current_session()
        if session is None:
            return
        if not session.offline:
            self.set_message(view.DRIVE_IS_MOUNTED)
        elif self._mount is not None:  # said at once, without asking about the drive again
            self.set_message(view.already_mounting_text(self._mount["drive"]))
        else:
            self._ask_volume(session, at_once=True)

    def _job(self) -> str | None:
        """The job under way that ends in a scan, if any: one at a time."""
        if self._scan is not None:
            return "scan"
        if self._importing:
            return "import"
        if self._mount is not None:
            return "mount"
        return "undo" if self._undoing else None

    def _start_mount(self, session: Session, drive: str) -> None:
        if self._mount is not None:
            self.set_message(view.already_mounting_text(self._mount["drive"]))
            return
        running = self._job()
        if running is not None:
            self.set_message(view.busy_text(running, "mount"))
            return
        cancel = threading.Event()
        self._mount = {"drive": drive, "cancel": cancel, "scanning": False}
        self._set_activity(view.mount_activity_text(drive))
        self._read(self._mount_thread, session.mount, drive, cancel, group="mount")

    def _mount_thread(self, mount: str, drive: str, cancel: threading.Event) -> None:
        last = [0.0]

        def progress(label: str, dirs: int) -> None:  # of the scan that follows the mount
            now = time.monotonic()
            if now - last[0] >= PROGRESS_INTERVAL:
                last[0] = now
                self._ui(self._mount_scanning, view.scan_progress_text(label, dirs))

        try:
            self.engine.mount_drive(mount, progress=progress, cancel=cancel)
            snapshot = build_snapshot(self.engine)
        except Exception as exc:
            self._ui(self._mount_done, drive, None, view.error_text(exc))
            return
        self._ui(self._mount_done, drive, snapshot, None)

    def _mount_scanning(self, text: str) -> None:
        if self._mount is not None:
            self._mount["scanning"] = True
            self._set_activity(text)

    async def _mount_done(self, drive: str, snapshot: Snapshot | None, error: str | None) -> None:
        self._mount = None
        self._set_activity("")
        if snapshot is not None:
            await self._apply_snapshot(snapshot)
        if error:
            self.set_message(error, error=True)
        else:
            self.set_message(view.mounted_text(drive))

    def _start_open(self, key: str, title: str, stay: bool) -> None:
        if self._opening:
            return
        self._opening = True
        self._read(
            self._open_thread,
            key,
            view.shown_title(title, key),
            stay,
            self.snapshot.index,
            group="open",
        )

    def _open_thread(self, key: str, title: str, stay: bool, index: Index | None) -> None:
        try:
            plan = self.engine.open_session(key, index=index)
        except Exception as exc:
            self._ui(self._open_failed, view.error_text(exc))
            return
        self._ui(self._opened, title, getattr(plan, "note", None), stay)

    def _open_failed(self, reason: str) -> None:
        self._opening = False
        self.set_message(reason, error=True)

    def _opened(self, title: str, note: str | None, stay: bool) -> None:
        self._opening = False
        if not stay:
            self.app.exit(result=note or None, return_code=0)
            return
        self.set_message(f"opened {title}" + (f" · {note}" if note else ""))

    # --- import ----------------------------------------------------------

    def do_import(self, event: events.Key | None = None) -> None:
        """``i``: import the selected foreign session, after a y/n; into a
        folder that is asked for when it needs one; after its warning when
        the import is an experimental one."""
        self._import_key("import")

    def do_import_folder(self, event: events.Key | None = None) -> None:
        """``t``: import the selected foreign session into a folder that is
        asked for."""
        self._import_key("folder")

    def _import_key(self, what: str) -> None:
        session = self.current_session()
        if session is None:
            return
        if session.origin == "native":
            self.set_message("already on Linux; nothing to import")
        elif session.imported_as:
            self.set_message("already imported; Enter resumes the Linux copy")
        elif session.offline:
            self._start_refusal(session, "import")
        else:
            self._ask_import(session, what)

    def _start_import(
        self,
        key: str,
        then_open: bool,
        stay: bool,
        target: str | None = None,
        experimental: bool = False,
    ) -> None:
        session = self.snapshot.sessions.get(key)
        if session is None:
            return
        running = self._job()
        if running is not None:
            self.set_message(view.busy_text(running, "import"))
            return
        self._importing = True
        self._set_activity(view.import_progress_text(0, 0))
        self._write(
            self._import_thread,
            key,
            view.shown_title(session.title, key),
            then_open,
            stay,
            target,
            experimental,
            self.snapshot.index,
            group="import",
        )

    def _import_thread(
        self,
        key: str,
        title: str,
        then_open: bool,
        stay: bool,
        target: str | None,
        experimental: bool,
        index: Index | None,
    ) -> None:
        last = [0.0]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] >= PROGRESS_INTERVAL:
                last[0] = now
                self._ui(self._set_activity, view.import_progress_text(done, total))

        try:
            copy = self.engine.import_session(
                key, progress=progress, index=index, target=target, experimental=experimental
            )
        except Exception as exc:
            self._ui(self._import_done, title, None, None, False, None, view.error_text(exc), stay)
            return
        new_key = getattr(copy, "key", None)
        if self._leaving is not None:
            # Asked to quit meanwhile: the copy is in place, and nothing more
            # is read or started.
            self._ui(self._import_done, title, new_key or key, None, False, None, None, stay)
            return
        snapshot: Snapshot | None = None
        opened = False
        note: str | None = None
        error: str | None = None
        try:
            # The engine has indexed the copy; read the index again to list it.
            snapshot = build_snapshot(self.engine)
        except Exception as exc:
            error = view.error_text(exc)
        if then_open and new_key is not None:
            try:
                plan = self.engine.open_session(new_key, index=snapshot.index if snapshot else None)
                opened, note = True, getattr(plan, "note", None)
            except Exception as exc:
                error = view.error_text(exc)
        self._ui(self._import_done, title, new_key or key, snapshot, opened, note, error, stay)

    async def _import_done(
        self,
        title: str,
        new_key: str | None,
        snapshot: Snapshot | None,
        opened: bool,
        note: str | None,
        error: str | None,
        stay: bool,
    ) -> None:
        """``new_key`` is None when the import itself failed (``error`` says
        why); otherwise the copy exists and ``error`` is what went wrong after
        (reading the index again, or the launch)."""
        self._importing = False
        self._set_activity("")
        if self._left():
            return
        if snapshot is not None:
            await self._apply_snapshot(snapshot, select_key=new_key)
        if new_key is None:
            self.set_message(error or "", error=True)
        elif opened and not stay:
            self.app.exit(result=note or None, return_code=0)
        elif error:
            self.set_message(f"imported {title}, but: {error}", error=True)
        else:
            text = f"imported and opened {title}" if opened else f"imported {title}"
            self.set_message(text + (f" · {note}" if note else ""))

    # --- undo an import ---------------------------------------------------

    def do_undo(self, event: events.Key | None = None) -> None:
        """``u``: take back the import of the selected session, its native
        copy or the session it was made from. Planned first, as a dry run:
        every refusal comes from there, before a question is asked."""
        session = self.current_session()
        if session is not None:
            self._read(
                self._undo_plan_thread,
                self._asked,
                session.key,
                self.snapshot.index,
                group="undo",
            )

    def _undo_plan_thread(self, asked: int, key: str, index: Index | None) -> None:
        try:
            report = self.engine.unimport(key, dry_run=True, index=index)
        except Exception as exc:
            kind, differ = view.undo_refusal(exc)
            self._ui(self._undo_planned, asked, key, kind, (0, 0, differ), view.error_text(exc))
            return
        self._ui(self._undo_planned, asked, key, "", view.undo_counts(report), "")

    def _undo_planned(
        self, asked: int, key: str, refusal: str, counts: tuple[int, int, int], reason: str
    ) -> None:
        """The dry run's answer: what would go (``refusal`` is empty), or
        which refusal it was (``view.undo_refusal``) and its ``reason``."""
        session = self._still_wanted(asked, key)
        if session is None:
            return
        files, size, differ = counts
        if not refusal:
            self._open_prompt("undo", self._about(session, files=files, size=size))
        elif refusal == "used":  # a forced undo gets past this one: ask
            self._open_prompt("undo_used", self._about(session, differ=differ))
        else:  # nothing recorded is an answer; anything else went wrong
            self.set_message(reason, error=refusal != "none")

    def do_undo_yes(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_undo(question.key, force=False)

    def do_undo_force_yes(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is not None:
            self._start_undo(question.key, force=True)

    def _start_undo(self, key: str, force: bool) -> None:
        running = self._job()
        if running is not None:
            self.set_message(view.busy_text(running, "undo"))
            return
        self._undoing = True
        self._write(self._undo_thread, key, force, self.snapshot.index, group="undo")

    def _undo_thread(self, key: str, force: bool, index: Index | None) -> None:
        try:
            report = self.engine.unimport(key, force=force, index=index)
        except Exception as exc:
            self._ui(self._undo_done, None, None, view.error_text(exc))
            return
        files, _size, left = view.undo_counts(report)
        snapshot: Snapshot | None = None
        error: str | None = None
        if self._leaving is None:  # asked to quit meanwhile: nothing more is read
            try:
                # The engine has scanned; read the index again to list what is left.
                snapshot = build_snapshot(self.engine)
            except Exception as exc:
                error = view.error_text(exc)
        self._ui(self._undo_done, view.undo_message(files, left), snapshot, error)

    async def _undo_done(
        self, removed: str | None, snapshot: Snapshot | None, error: str | None
    ) -> None:
        """``removed`` is None when the undo itself failed (``error`` says
        why); otherwise the copy is gone and ``error`` is what went wrong
        when the index was read again."""
        self._undoing = False
        if self._left():
            return
        if snapshot is not None:
            await self._apply_snapshot(snapshot)
        if removed is None:
            self.set_message(error or "", error=True)
        elif error:
            self.set_message(f"{removed}, but: {error}", error=True)
        else:
            self.set_message(removed)

    # --- continue as a new session -----------------------------------------

    def do_continue(self, event: events.Key | None = None) -> None:
        """``c``: carry the selected session on as a new session of a tool."""
        session = self.current_session()
        if session is not None:
            self._start_continue(session.key)

    def _start_continue(self, key: str) -> None:
        self._read(
            self._continue_options_thread, self._asked, key, self.snapshot.index, group="continue"
        )

    def _continue_options_thread(self, asked: int, key: str, index: Index | None) -> None:
        try:
            options = self.engine.continue_options(key, index=index)
            tools = tuple((str(name), view.clean(label)) for name, label in options.tools)
            answer = (bool(options.possible), view.clean(options.reason), tools)
            start = (view.tool_start(tools, options.default_tool), options.folder or "")
        except Exception as exc:
            self._ui(self._continue_asked, asked, key, (False, view.error_text(exc), ()), (0, ""))
            return
        self._ui(self._continue_asked, asked, key, answer, start)

    def _continue_asked(
        self,
        asked: int,
        key: str,
        answer: tuple[bool, str, tuple[tuple[str, str], ...]],
        start: tuple[int, str],
    ) -> None:
        session = self._still_wanted(asked, key)
        if session is None:
            return
        possible, reason, tools = answer
        # A row this list shows as offline is not started, whatever a
        # listing that is behind the engine made it answer.
        if not possible or not tools or session.offline:
            self.set_message(reason or view.clean(session.note) or "cannot be continued", True)
            return
        tool, folder = start
        self._open_prompt("tool", self._about(session, tools=tools, tool=tool, folder=folder))

    def do_tool_prev(self, event: events.Key | None = None) -> None:
        self._turn_tool(-1)

    def do_tool_next(self, event: events.Key | None = None) -> None:
        self._turn_tool(1)

    def _turn_tool(self, step: int) -> None:
        question = self._question
        if question is not None:
            question.tool = view.cycle_tab(question.tool, step, len(question.tools))
            self._render_prompt()

    def do_tool_accept(self, event: events.Key | None = None) -> None:
        if self._question is not None:
            self._open_folder(self._question, "continue")

    def do_start_yes(self, event: events.Key | None = None) -> None:
        question = self._take_prompt()
        if question is None or self._continuing:
            return
        name, label = question.tools[question.tool]
        self._continuing = True
        # The transcript is exported before the tool starts: said meanwhile.
        self._starting = view.starting_text(label)
        self._render_status()
        self._read(
            self._continue_thread,
            question.key,
            name,
            label,
            question.folder,
            self.snapshot.index,
            group="continue",
        )

    def _continue_thread(
        self, key: str, tool: str, label: str, folder: str, index: Index | None
    ) -> None:
        try:
            self.engine.continue_session(key, tool=tool, folder=folder, index=index)
        except Exception as exc:
            self._ui(self._continued, label, view.error_text(exc))
            return
        self._ui(self._continued, label, None)

    def _continued(self, label: str, error: str | None) -> None:
        self._continuing = False
        self._starting = ""
        self._render_status()
        if error:
            self.set_message(error, error=True)
        else:
            self.set_message(view.started_text(label))

    # --- view ------------------------------------------------------------

    def do_view(self, event: events.Key | None = None) -> None:
        row = self.session_list.current
        if row is not None and row.kind == "hit":
            self._view_hit(row)
            return
        session = self.current_session()
        if session is not None:
            self._view(session.key)

    def _view_hit(self, row: view.Row) -> None:
        """The transcript, at the message a search found, its matches lit."""
        self._view(row.key, row.hit.turn, self.needle.strip())

    def _view(self, key: str | None, turn: int | None = None, needle: str = "") -> None:
        session = self.snapshot.sessions.get(key) if key is not None else None
        if key is None or session is None:
            return
        if session.offline:
            self._start_refusal(session, "view")
            return
        title = view.shown_title(session.title, key)
        self._show(
            TranscriptScreen(
                self.engine,
                key,
                title,
                self.snapshot.index,
                tz=self.tz,
                max_turns=None if turn is None else max(view.PAGER_MAX_TURNS, turn + 1),
                anchor=turn,
                needle=needle,
            )
        )

    # --- load and scan -----------------------------------------------------

    def _load_thread(self) -> None:
        try:
            snapshot = build_snapshot(self.engine)
        except Exception as exc:
            self._ui(self._loaded, None, view.error_text(exc))
            return
        self._ui(self._loaded, snapshot, None)

    async def _loaded(self, snapshot: Snapshot | None, error: str | None) -> None:
        """The cached index is on screen; now bring it up to date."""
        self._set_activity("")
        if snapshot is not None:
            await self._apply_snapshot(snapshot)
        else:
            self.loaded = True
            self._refresh_rows()
            self.set_message(error or "", error=True)
        self.start_scan(STARTUP_SCAN_DEEP, announce=False)

    def do_rescan(self, event: events.Key | None = None) -> None:
        self.start_scan(False)

    def do_full_rescan(self, event: events.Key | None = None) -> None:
        self.start_scan(True)

    def start_scan(self, deep: bool | None, announce: bool = True) -> None:
        running = self._job()
        if running is not None:
            self.set_message(view.busy_text(running, "scan"))
            return
        cancel = threading.Event()
        self._scan = {"deep": bool(deep), "cancel": cancel}
        self._set_activity(f"scanning{view.ELLIPSIS}")
        self._read(self._scan_thread, deep, cancel, announce, group="scan")

    def _cancel_scan(self) -> None:
        if self._scan is not None:
            self._scan["cancel"].set()
            self.set_message(f"stopping the scan{view.ELLIPSIS}")

    def _scan_thread(self, deep: bool | None, cancel: threading.Event, announce: bool) -> None:
        last = [0.0]

        def progress(label: str, dirs: int) -> None:
            now = time.monotonic()
            if now - last[0] >= PROGRESS_INTERVAL:
                last[0] = now
                self._ui(self._set_activity, view.scan_progress_text(label, dirs))

        try:
            summary = self.engine.scan(deep=deep, progress=progress, cancel=cancel)
            snapshot = build_snapshot(self.engine)
        except Exception as exc:
            self._ui(self._scan_done, None, None, view.error_text(exc), announce)
            return
        self._ui(self._scan_done, summary, snapshot, None, announce)

    async def _scan_done(
        self, summary: dict | None, snapshot: Snapshot | None, error: str | None, announce: bool
    ) -> None:
        self._scan = None
        self._set_activity("")
        if snapshot is not None:
            await self._apply_snapshot(snapshot)
        if error:
            self.set_message(error, error=True)
        elif announce or (summary or {}).get("cancelled"):
            self.set_message(view.scan_message(summary))


# --- help overlay -------------------------------------------------------------------


class HelpScreen(_ClosesOnce, ModalScreen):
    """The key help; ``?``, ``q``, Esc, Enter or a click closes it."""

    COMPONENT_CLASSES: ClassVar[set[str]] = set(crt.SCREEN_CLASSES)

    def __init__(self) -> None:
        super().__init__()
        self.raster = Raster(self)

    def compose(self) -> ComposeResult:
        with CrtVertical(id="help"):
            yield CrtStatic("Keys", id="help-title", markup=False)
            yield CrtStatic("", id="help-text", markup=False)
            yield CrtStatic("Esc closes", id="help-hint", markup=False)

    def on_mount(self) -> None:
        box = self.query_one("#help")
        box.border_title = eva.HELP_TITLE
        self._fill()
        self.raster.interlace()

    def on_resize(self, event: events.Resize) -> None:
        self._fill()

    def _fill(self) -> None:
        box = self.query_one("#help")
        text = self.query_one("#help-text", Static)
        room = self.size.width - sum(
            widget.styles.gutter.width + widget.styles.margin.width for widget in (box, text)
        )
        # One column is kept back for the scrollbar a short terminal needs.
        text.update("\n".join(view.help_lines(max(room - 1, 1))))

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        action = view.key_action("help", event.key, event.character)
        box = self.query_one("#help")
        if action == "close":
            self.close()
        elif action == "up":
            box.scroll_up(animate=False)
        elif action == "down":
            box.scroll_down(animate=False)
        elif action == "pageup":
            box.scroll_page_up(animate=False)
        elif action == "pagedown":
            box.scroll_page_down(animate=False)

    def on_click(self, event: events.Click) -> None:
        self.close()


# --- transcript pager ----------------------------------------------------------------


class TranscriptView(CrtMixin, ScrollView, can_focus=True):
    """A transcript drawn line by line. Turns arrive in batches and are
    wrapped a slice at a time, each slice from a timer of its own: a key is
    answered between two slices, however long the session is.

    Every turn keeps the lines it was last wrapped into. After a resize the
    turns on screen are wrapped for the new width first, then the others,
    down and up from there; until its turn comes, a turn shows the lines it
    had, cut to the new width."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "transcript--role-user",
        "transcript--role-assistant",
        "transcript--role-tool",
        "transcript--text",
        "transcript--note",
        "transcript--blank",
        "transcript--match",  # the text a search was for
    }

    def __init__(self, tz: tzinfo | None = None, needle: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tz = tz
        self.needle = needle.strip()
        self._folded = self.needle.lower()
        self.matches: list[int] = []  # the turns that say ``needle``
        self.place: int | None = None  # the one of them the view went to last
        self._anchor: int | None = None  # a turn to bring to the top once it is wrapped
        self.turns: list[tuple] = []
        self.pending = 0  # turns not wrapped for the current width yet
        self._blocks: list[list[tuple[str, str]]] = []  # the lines of every turn
        self._wrapped_for: list[int] = []  # the width each turn's lines are for; 0: none yet
        self._starts: list[int] = []  # first line of every turn
        self._height = 0  # lines in all
        self._width = 0
        self._down = 0  # the next turn to wrap going down from the top of the view
        self._up = -1  # and going up
        self._ahead = 0  # lines wrapped below the top of the view since the width changed
        self._upward = False  # whether the next turn is one above, when both ways are open
        self._timer: Timer | None = None
        self._stopped = False
        self._rewrap_due = False

    @property
    def lines(self) -> list[tuple[str, str]]:
        """Every line there is, as ``(kind, text)``."""
        return [line for block in self._blocks for line in block]

    @property
    def settled(self) -> bool:
        """Whether every turn is wrapped for the width the view has now."""
        return not self.pending and self._width == self.size.width

    def add_turns(self, turns: Iterable[tuple]) -> None:
        before = len(self.turns)
        self.turns.extend(turns)
        added = len(self.turns) - before
        if self._folded:
            self.matches += [
                index
                for index in range(before, len(self.turns))
                if self.turns[index][0] != "tool"
                and self._folded
                in str(self.turns[index][1] or "")[: view.PAGER_MAX_TURN_CHARS].lower()
            ]
        self._blocks.extend([] for _ in range(added))
        self._wrapped_for.extend([0] * added)
        self._starts.extend([self._height] * added)
        self.pending += added
        if self._anchor is not None and before <= self._anchor < len(self.turns):
            self._aim(self._anchor)  # it has arrived: wrap from there
            if self._anchor in self.matches:
                self.place = self.matches.index(self._anchor)
        if self._timer is None:
            self._wrap_some()

    def go_to_turn(self, index: int) -> None:
        """Bring a turn to the top of the view: now when it is wrapped, else
        as soon as it is (it is wrapped first, and then what follows it)."""
        self._anchor = index
        if index in self.matches:
            self.place = self.matches.index(index)
        if index < len(self.turns):
            self._aim(index)
            self._jump()
            if self._timer is None:
                self._wrap_some()

    def drop_anchor(self) -> None:
        """The reader scrolled: the view stays where they put it."""
        self._anchor = None

    def step_match(self, step: int) -> int | None:
        """Go to the next (``step`` 1) or previous (-1) turn that says the
        needle, from the last one gone to or from the top of the view; past
        the last, round to the first. The turn, or None when none says it."""
        if not self.matches:
            return None
        here = self.matches[self.place] if self.place is not None else self._top_turn()
        if step > 0:
            at = bisect_right(self.matches, here)
            if self.place is None and here in self.matches:
                at -= 1  # the top turn itself is the first one forward
        else:
            at = bisect_left(self.matches, here) - 1
        at %= len(self.matches)
        self.go_to_turn(self.matches[at])
        return self.matches[at]

    def _aim(self, index: int) -> None:
        """Wrap from ``index`` down first, then up and down in turn."""
        self._down, self._up, self._ahead, self._upward = index, index - 1, 0, False

    def _jump(self) -> None:
        """Scroll to the anchor once it, and a screenful after it (or all
        there is), is wrapped for this width."""
        anchor, width = self._anchor, self._width
        if anchor is None or width <= 0 or anchor >= len(self.turns):
            return
        lines, index = 0, anchor
        while index < len(self.turns) and lines < self.size.height:
            if self._wrapped_for[index] != width:
                return
            lines += len(self._blocks[index])
            index += 1
        self._anchor = None
        self.scroll_to(y=self._starts[anchor], animate=False, force=True, immediate=True)
        self.refresh()

    def stop(self) -> None:
        """No more wrapping: the pager is closing."""
        self._stopped = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_unmount(self) -> None:
        self.stop()

    def on_resize(self, event: events.Resize) -> None:
        self._rewrap()

    def render_lines(self, crop: Region) -> list[Strip]:
        # A scrollbar that appears narrows the content without a Resize.
        if self.size.width != self._width and not self._rewrap_due:
            self._rewrap_due = True
            self.call_later(self._rewrap)
        return super().render_lines(crop)

    def _top_turn(self) -> int:
        """The turn the first line on screen belongs to; -1 without turns.
        Where no line is yet, the first turn that will put one there."""
        y = int(self.scroll_offset.y)
        if y < self._height:
            return bisect_right(self._starts, y) - 1
        return min(bisect_left(self._starts, y), len(self.turns) - 1)

    def _rewrap(self) -> None:
        """The width has changed: every turn is to be wrapped again, the
        ones on screen first."""
        self._rewrap_due = False
        width = self.size.width
        if width == self._width:
            return
        self._width = width
        self.pending = sum(1 for wrapped_for in self._wrapped_for if wrapped_for != width)
        anchor = self._anchor
        top = (
            anchor if anchor is not None and anchor < len(self.turns) else max(self._top_turn(), 0)
        )
        self._aim(top)
        if self._timer is None:
            self._wrap_some()

    def _next_turn(self) -> tuple[int, bool]:
        """The turn to wrap next, and whether it lies above the place the
        wrapping started from: down from there until a screenful is done,
        then up and down in turn."""
        width, count = self._width, len(self.turns)
        while self._down < count and self._wrapped_for[self._down] == width:
            self._down += 1
        while self._up >= 0 and self._wrapped_for[self._up] == width:
            self._up -= 1
        below, above = self._down < count, self._up >= 0
        upward = above and (not below or (self._ahead >= self.size.height and self._upward))
        self._upward = not upward
        if upward:
            self._up -= 1
            return self._up + 1, True
        self._down += 1
        return self._down - 1, False

    def _wrap_some(self) -> None:
        """Wrap turns for ``PAGER_SLICE`` seconds; a timer comes back for
        the rest, so that the keys waiting are handled first. The line at
        the top of the view stays the same line of the same turn."""
        self._timer = None
        if self._stopped or self._width <= 0 or not self.pending:
            return
        y = int(self.scroll_offset.y)
        top = self._top_turn()
        inside = y - self._starts[top] if top >= 0 else 0
        before = len(self._blocks[top]) if top >= 0 else 0
        deadline = time.monotonic() + PAGER_SLICE
        while self.pending:
            index, above = self._next_turn()
            role, text, ts = self.turns[index]
            self._blocks[index] = view.transcript_lines(role, text, ts, self._width, self.tz)
            self._wrapped_for[index] = self._width
            self.pending -= 1
            if not above:
                self._ahead += len(self._blocks[index])
            if time.monotonic() > deadline:
                break
        self._starts, self._height = [], 0
        for block in self._blocks:
            self._starts.append(self._height)
            self._height += len(block)
        self.virtual_size = Size(self._width, self._height)
        if top >= 0:
            after = len(self._blocks[top])
            if after != before:  # wrapped again itself: as far into it as before
                inside = inside * after // before if before else 0
            target = self._starts[top] + min(inside, max(after - 1, 0))
            if target != y:
                self.scroll_to(y=target, animate=False, force=True)
        self.refresh()
        self._jump()
        if self.pending:
            self._timer = self.set_timer(PAGER_PAUSE, self._wrap_some)

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        index = y + int(self.scroll_offset.y)
        if not 0 <= index < self._height:
            return Strip.blank(width, self.rich_style)
        turn = bisect_right(self._starts, index) - 1
        kind, text = self._blocks[turn][index - self._starts[turn]]
        style = self.get_component_rich_style(f"transcript--{kind}")
        if kind != "text" or not self._folded or self._folded not in text.lower():
            return Strip([Segment(text, style)]).adjust_cell_length(width, self.rich_style)
        lit = self.get_component_rich_style("transcript--match")
        segments = [
            Segment(part, lit if hit else style)
            for hit, part in view.match_pieces(text, self.needle)
        ]
        return Strip(segments).adjust_cell_length(width, self.rich_style)


class TranscriptScreen(_UiCalls, _ClosesOnce):
    """The built-in pager: one session's transcript, read in a thread."""

    COMPONENT_CLASSES: ClassVar[set[str]] = set(crt.SCREEN_CLASSES)

    def __init__(
        self,
        engine: Engine,
        key: str,
        title: str,
        index: Index | None = None,
        tz: tzinfo | None = None,
        max_turns: int | None = None,
        anchor: int | None = None,
        needle: str = "",
    ) -> None:
        """``anchor``: the turn to open at (a message a search found);
        ``needle``: the text the search was for, lit wherever it is."""
        super().__init__(id="pager")
        self.engine = engine
        self.key = key
        self.session_title = title
        self.index = index
        self.tz = tz
        self.max_turns = view.PAGER_MAX_TURNS if max_turns is None else max_turns
        self.turn_count = 0
        self.reading = True
        self.start_turn = anchor
        self.needle = needle.strip()
        self._capped = False
        self._error: str | None = None
        self.raster = Raster(self)

    def compose(self) -> ComposeResult:
        yield CrtStatic(self.session_title, id="pager-title", markup=False)
        yield TranscriptView(tz=self.tz, needle=self.needle, id="pager-body")
        with CrtHorizontal(id="pager-footer"):
            hints = view.PAGER_SEARCH_HINTS if self.needle else view.PAGER_HINTS
            yield CrtStatic(hints, id="pager-hints", markup=False)
            yield CrtStatic("", id="pager-status", markup=False)

    @property
    def body(self) -> TranscriptView:
        return self.query_one("#pager-body", TranscriptView)

    def on_mount(self) -> None:
        self.body.border_title = eva.PAGER_TITLE
        self.body.focus()
        if self.start_turn is not None:
            self.body.go_to_turn(self.start_turn)
        self._render_status()
        self.raster.interlace()
        read(self, self._load_thread, "transcript")

    def on_unmount(self) -> None:
        self._gone = True  # the reader stops at its next turn

    def _render_status(self, capped: bool | None = None, error: str | None = None) -> None:
        if capped is not None:
            self._capped, self._error = capped, error
        status = self.query_one("#pager-status", Static)
        text = view.pager_status(self.turn_count, self.reading, self._capped, self._error)
        if self.needle:
            body = self.body
            text = f"{view.match_status(body.place, len(body.matches))} · {text}"
        status.update(text)
        status.set_class(bool(self._error), "-error")

    def _load_thread(self) -> None:
        batch: list[tuple] = []
        count, last = 0, time.monotonic()
        capped = False
        error: str | None = None
        try:
            for turn in self.engine.transcript(self.key, index=self.index):
                if self._gone:
                    return
                if count >= self.max_turns:
                    capped = True
                    break
                batch.append(tuple(turn)[:3])
                count += 1
                now = time.monotonic()
                if len(batch) >= PAGER_BATCH or now - last >= PROGRESS_INTERVAL:
                    self._ui(self._append, batch)
                    batch, last = [], now
        except Exception as exc:
            error = view.error_text(exc)
        self._ui(self._append, batch)
        self._ui(self._finished, capped, error)

    def _append(self, batch: list[tuple]) -> None:
        if batch:
            self.turn_count += len(batch)
            self.body.add_turns(batch)
            self._render_status()

    def _finished(self, capped: bool, error: str | None) -> None:
        self.reading = False
        self._render_status(capped, error)

    def on_key(self, event: events.Key) -> None:
        action = view.key_action("pager", event.key, event.character)
        if action is None:
            return
        event.stop()
        event.prevent_default()
        body = self.body
        if action in ("next_match", "previous_match"):
            body.step_match(1 if action == "next_match" else -1)
            self._render_status()
            return
        body.drop_anchor()
        if action == "close":
            body.stop()
            self.close()
        elif action == "up":
            body.scroll_up(animate=False)
        elif action == "down":
            body.scroll_down(animate=False)
        elif action == "pageup":
            body.scroll_page_up(animate=False)
        elif action == "pagedown":
            body.scroll_page_down(animate=False)
        elif action == "home":
            body.scroll_home(animate=False)
        elif action == "end":
            body.scroll_end(animate=False)


# --- the app ---------------------------------------------------------------------------


class SessionsApp(App):
    """``engine`` is anything with the methods of ``tui.engine.Engine``."""

    CSS_PATH = "app.tcss"
    TITLE = view.APP_TITLE
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True)
    ]

    def __init__(
        self,
        engine: Engine | None = None,
        clock: Clock = _utc_now,
        tz: tzinfo | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.engine = engine if engine is not None else Engine()
        self.clock = clock
        self.tz = tz
        self.theme = THEME
        self.motion = eva.motion_enabled()
        self._signals: list[int] = []

    def get_default_screen(self) -> Screen:
        return MainScreen(self.engine, clock=self.clock, tz=self.tz)

    def leave(self, return_code: int = 0) -> None:
        """Quit, from whichever screen: what is under way decides how
        (``MainScreen.leave``)."""
        stack = self.screen_stack
        if stack and isinstance(stack[0], MainScreen):
            stack[0].leave(return_code)
        else:
            self.exit(return_code=return_code)

    async def action_quit(self) -> None:
        """Ctrl-C."""
        self.leave()

    def on_mount(self) -> None:
        """Leave through the front door on SIGTERM and SIGHUP too, so the
        terminal is put back as it was. (Where the platform or the thread
        cannot take a handler, there is nothing to do.)"""
        if self.is_headless:
            return
        loop = asyncio.get_running_loop()
        for name in ("SIGTERM", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                loop.add_signal_handler(number, partial(self.leave, 128 + number))
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            self._signals.append(number)

    def on_unmount(self) -> None:
        loop = asyncio.get_running_loop()
        for number in self._signals:
            loop.remove_signal_handler(number)
        self._signals.clear()


__all__ = [
    "HelpScreen",
    "MainScreen",
    "Reader",
    "SafeInput",
    "SessionList",
    "SessionsApp",
    "Snapshot",
    "TabLabel",
    "TranscriptScreen",
    "TranscriptView",
    "UiCall",
    "build_snapshot",
]
