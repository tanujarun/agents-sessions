"""The Textual app: widgets, screens and the wiring between keys, workers and
the engine.

Rules this module keeps:

- No engine logic. Every list, scan, import, launch and transcript is a call
  on ``self.engine`` (``tui/engine.py``), made in a thread worker; the worker
  hands its result back with ``_ui``. An engine exception becomes a status
  line message.
- No view logic. What a row says, which columns fit, what a key means: all of
  it is in ``tui/view.py``.
- No styling. Colours, borders and spacing are in ``app.tcss``; this module
  only names things (ids, classes, component classes) for it to style, and
  reads the spacing it needs for fitting back from the computed styles.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import signal
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial

from rich.segment import Segment
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Input, Static
from textual.worker import get_current_worker

from . import view
from .engine import Engine

THEME = "ansi-dark"
"""Textual's ANSI theme: every colour is one of the terminal's own sixteen
(or its default foreground/background), so the app follows the terminal."""

STARTUP_SCAN_DEEP = None  # the engine decides: a full walk when the drives changed or the last one is over a day old
PROGRESS_INTERVAL = 0.1  # seconds between progress updates sent to the UI
VERDICT_DELAY = 0.15  # seconds a row must stay selected before can_import runs
CLOCK_INTERVAL = 30.0  # seconds between refreshes of the ages on screen
PAGER_BATCH = 50  # turns handed to the pager at once
PAGER_SLICE = 0.01  # seconds of wrapping before the pager yields to input


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UiCall(Message):
    """A callback a worker thread wants run on the UI thread. Posting a
    message is thread-safe and never blocks, also while the app is closing."""

    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback


class _UiCalls:
    """Mixin for a screen: ``_ui(fn, *args)`` from any thread."""

    def _ui(self, function, *args) -> None:
        self.post_message(UiCall(partial(function, *args)))

    async def on_ui_call(self, message: UiCall) -> None:
        message.stop()
        result = message.callback()
        if inspect.isawaitable(result):
            await result


# --- the session list ---------------------------------------------------------


class SessionList(ScrollView, can_focus=True):
    """The list of sessions, drawn line by line: only the visible rows are
    formatted, so thousands of rows cost the same as twenty."""

    COMPONENT_CLASSES = {
        "session-list--row",  # a native row
        "session-list--foreign",  # a row from another drive
        "session-list--cursor",  # the selected row (added to one of the above)
        "session-list--cell",  # padding of every cell = the column spacing
        "session-list--empty",  # the "No sessions found" line
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

    class Fitted(Message):
        """The columns changed; ``header`` is the new header line."""

        def __init__(self, header: str) -> None:
            super().__init__()
            self.header = header

    def __init__(self, clock=_utc_now, **kwargs) -> None:
        super().__init__(**kwargs)
        self.clock = clock
        self.rows: list[view.Row] = []
        self.cursor = -1
        self.columns: list[view.Column] = []
        self.widths: dict[str, int] = {}
        self.provider_labels: dict[str, str] = {}
        self.show_provider = False
        self.show_project = True
        self.empty_text = ""
        self._pad = (0, 0)
        self._header = None
        self._fit_width = -1

    @property
    def current(self) -> view.Row | None:
        return self.rows[self.cursor] if 0 <= self.cursor < len(self.rows) else None

    @property
    def top(self) -> int:
        return int(self.scroll_offset.y)

    def set_rows(
        self,
        rows,
        key=None,
        *,
        show_provider=False,
        provider_labels=None,
        widths=None,
        empty_text="",
        from_top=False,
    ) -> None:
        """Replace the rows, keeping the selection on ``key`` when it is
        still listed and near its old place when it is not (``from_top``:
        at the first row, for a list that is not the one shown before)."""
        self.rows = rows
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
        self.columns = view.fit_columns(
            self.size.width,
            self.widths,
            show_provider=self.show_provider,
            show_project=self.show_project,
            cell_pad=padding.left + padding.right,
        )
        header = view.format_header(self.columns, *self._pad)
        if header != self._header:
            self._header = header
            self.post_message(self.Fitted(header))
        self.refresh()

    def select(self, index: int) -> None:
        if not self.rows:
            return
        index = min(max(index, 0), len(self.rows) - 1)
        if index != self.cursor:
            self.cursor = index
            self._show_cursor()
            self.refresh()
            self._announce()

    def move(self, move: str) -> None:
        self.select(view.move_index(self.cursor, len(self.rows), move, page=self.size.height - 1))

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

    def render_lines(self, crop):
        # The content gets a column narrower when the scrollbar appears, and
        # no Resize says so: check before drawing.
        if self.size.width != self._fit_width:
            self.fit()
        return super().render_lines(crop)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        offset = event.get_content_offset(self)
        index = -1 if offset is None else offset.y + self.top
        if not 0 <= index < len(self.rows):
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
        names = ["session-list--foreign" if row.foreign else "session-list--row"]
        if index == self.cursor:
            names.append("session-list--cursor")
        style = self.get_component_rich_style(*names)
        text = view.format_row(row, self.columns, self.clock(), self.provider_labels, *self._pad)
        return Strip([Segment(text, style)]).adjust_cell_length(width, style)

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


class TabLabel(Static):
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

    index: object = None
    sessions: dict = field(default_factory=dict)  # key -> Session
    rows: list = field(default_factory=list)  # newest first, as the engine lists
    providers: list = field(default_factory=list)  # [(name, label)]
    drives: int = 0
    last_scan: str | None = None


def build_snapshot(engine) -> Snapshot:
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
        last_scan=status.get("last_quick"),
    )


# --- main screen ------------------------------------------------------------------


class MainScreen(_UiCalls, Screen):
    """Status line, provider tabs, the list, the detail pane, key hints."""

    def __init__(self, engine, clock=_utc_now, tz=None) -> None:
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
        self.mode = "list"  # "list" | "foreign" | "confirm"
        self._prompt_key: str | None = None
        self._prompt_stay = False
        self._prompt_about = ("", "")  # (title, where) of the prompt's session
        self._selected: dict[str, str] = {}  # tab name -> selected session key
        self._message = ""
        self._message_error = False
        self._activity = ""
        self._scan: dict | None = None  # {"deep": bool, "cancel": Event}
        self._importing = False
        self._opening = False
        self._verdicts: dict[str, tuple] = {}
        self._verdict_pending: set[str] = set()
        self._verdict_timer = None

    # --- widgets ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="statusbar"):
            yield Static(view.APP_TITLE, id="status-message", markup=False)
            yield Static("", id="status-summary", markup=False)
        yield Horizontal(id="tabs")
        yield Static("", id="header", markup=False)
        yield SessionList(clock=self.clock, id="list")
        with Vertical(id="detail"):
            with Horizontal(classes="detail-row", id="detail-row-main"):
                for name in view.DETAIL_FIELDS[:3]:
                    yield Static(name, classes="detail-label", markup=False)
                    yield Static(
                        view.EMPTY, id=f"detail-{name}", classes="detail-value", markup=False
                    )
            for name in view.DETAIL_FIELDS[3:]:
                with Horizontal(classes="detail-row", id=f"detail-row-{name}"):
                    yield Static(name, classes="detail-label", markup=False)
                    yield Static(
                        view.EMPTY, id=f"detail-{name}", classes="detail-value", markup=False
                    )
        yield Static("", id="prompt", markup=False)
        with Horizontal(id="filterbar"):
            yield Static("/", id="filter-label", markup=False)
            yield Input(id="filter", placeholder="title, project or folder")
        yield Static("", id="hints", markup=False)

    @property
    def session_list(self) -> SessionList:
        return self.query_one("#list", SessionList)

    @property
    def filter_input(self) -> Input:
        return self.query_one("#filter", Input)

    def on_mount(self) -> None:
        self.query_one("#prompt").display = False
        self.query_one("#filterbar").display = False
        self.session_list.focus()
        self._apply_layout()
        self.set_interval(CLOCK_INTERVAL, self._tick)
        self._set_activity(f"loading{view.ELLIPSIS}")
        self._work(self._load_thread, group="load")

    def on_unmount(self) -> None:
        # A scan in a thread cannot be killed; ask it to stop so quitting
        # does not wait for a walk of every drive.
        if self._scan is not None:
            self._scan["cancel"].set()

    def _work(self, function, *args, group: str = "default"):
        return self.run_worker(
            partial(function, *args), thread=True, group=group, exit_on_error=False
        )

    def _tick(self) -> None:
        self.session_list.refresh()
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
        if self._activity:
            variants = [self._activity]
        else:
            variants = view.summary_variants(
                self.snapshot.drives, len(self.snapshot.rows), self.snapshot.last_scan, self.clock()
            )
        shown, summary = view.status_parts(
            self._message or view.APP_TITLE, variants, room, gap=0, keep_left=bool(self._message)
        )
        left.update(shown)
        left.set_class(bool(self._message) and self._message_error, "-error")
        left.set_class(bool(self._message) and not self._message_error, "-info")
        right.update(summary)
        right.set_class(bool(self._activity), "-busy")

    # --- layout ----------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        """Decide what a terminal of this size shows (``view.layout_for``)."""
        hints = self.query_one("#hints", Static)
        layout = view.layout_for(
            self.size.width, self.size.height, hints.styles.gutter.width + hints.styles.margin.width
        )
        self.query_one("#detail").display = layout.show_detail
        hints.update("\n".join(layout.hints))
        self.set_class(not layout.show_project, "-narrow")
        self.set_class(not layout.show_detail, "-compact")
        if self.session_list.show_project != layout.show_project:
            self.session_list.show_project = layout.show_project
            self.session_list.fit()
        self._render_tabs()
        self._render_status()
        self._render_prompt()

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
        for index, (label, text) in enumerate(zip(labels, texts)):
            label.update(text)
            label.set_class(index == self.tab_index, "-active")

    def select_tab(self, index: int) -> None:
        if not self.tabs:
            return
        self.tab_index = index % len(self.tabs)
        self.tab_name = self.tabs[self.tab_index].name
        self._render_tabs()
        self._refresh_rows(from_top=True)

    @on(TabLabel.Pressed)
    def _tab_pressed(self, message: TabLabel.Pressed) -> None:
        self._close_prompt()
        self.clear_message()
        self.select_tab(message.index)

    # --- rows and selection ----------------------------------------------

    async def _apply_snapshot(self, snapshot: Snapshot, select_key: str | None = None) -> None:
        """Show a fresh reading of the index, keeping tab and selection."""
        names = [tab.name for tab in self.tabs]
        self.snapshot = snapshot
        self.loaded = True
        self._verdicts.clear()
        self.tabs = view.build_tabs(snapshot.providers, snapshot.rows)
        new_names = [tab.name for tab in self.tabs]
        self.tab_index = new_names.index(self.tab_name) if self.tab_name in new_names else 0
        self.tab_name = self.tabs[self.tab_index].name
        if new_names != names:
            await self._rebuild_tabs()
        if self._prompt_key is not None and self._prompt_key not in snapshot.sessions:
            self._close_prompt()
        self._render_tabs()
        self._refresh_rows(select_key)
        self._render_status()

    def _refresh_rows(self, select_key: str | None = None, from_top: bool = False) -> None:
        """Show the rows of the current tab and filter. The selection goes to
        ``select_key``, else stays on its session, else returns to the one
        this tab had; ``from_top`` (a tab switch) starts at the first row
        when none of those is listed."""
        provider = self.tab_name or view.ALL
        rows = view.filter_rows(self.snapshot.rows, provider, self.needle)
        if not self.loaded:
            empty = ""
        elif self.needle.strip():
            empty = f'No sessions match "{self.needle.strip()}"'
        else:
            empty = "No sessions found"
        current = self.session_list.current
        key = select_key or (
            current.key
            if current and self._listed_under(current, provider)
            else self._selected.get(provider)
        )
        self.session_list.set_rows(
            rows,
            key,
            show_provider=provider == view.ALL,
            provider_labels=dict(self.snapshot.providers),
            widths=view.column_widths(rows, dict(self.snapshot.providers)),
            empty_text=empty,
            from_top=from_top,
        )

    @staticmethod
    def _listed_under(row: view.Row, provider: str) -> bool:
        return provider == view.ALL or row.provider == provider

    def current_session(self):
        row = self.session_list.current
        return self.snapshot.sessions.get(row.key) if row else None

    @on(SessionList.Highlighted)
    def _highlighted(self, message: SessionList.Highlighted) -> None:
        if message.key is not None and self.tab_name is not None:
            self._selected[self.tab_name] = message.key
        self._render_detail()
        self._schedule_verdict()

    @on(SessionList.Pressed)
    def _row_pressed(self, message: SessionList.Pressed) -> None:
        """A click selects (the list has done that); a double-click is Enter.
        Either takes a pending prompt away first."""
        self._close_prompt()
        self.clear_message()
        current = self.session_list.current
        if message.double and current is not None and current.key == message.key:
            self.do_resume()

    def on_click(self, event: events.Click) -> None:
        # A click anywhere else takes a pending prompt away too.
        self._close_prompt()

    # --- detail pane -----------------------------------------------------

    def _render_detail(self) -> None:
        session = self.current_session()
        verdict = self._verdicts.get(session.key) if session else None
        for name, value in view.detail_fields(session, verdict, self.tz).items():
            self.query_one(f"#detail-{name}", Static).update(value)

    def _schedule_verdict(self) -> None:
        """Ask the engine whether the selected foreign session can be
        imported, once the selection has settled on it."""
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
        self._work(self._verdict_thread, key, session, self.snapshot.index, group="verdict")

    def _verdict_thread(self, key, session, index) -> None:
        try:
            possible, reason = self.engine.can_import(session, index=index)
            verdict = (bool(possible), reason or "")
        except Exception as exc:  # shown in the detail pane, never fatal
            verdict = (None, view.error_text(exc))
        self._ui(self._verdict_done, key, index, verdict)

    def _verdict_done(self, key, index, verdict) -> None:
        self._verdict_pending.discard(key)
        if index is not self.snapshot.index:
            self._schedule_verdict()  # the index moved on; ask again if still selected
            return
        self._verdicts[key] = verdict
        self._render_detail()

    # --- keys ------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Input):
            mode = "filter"
        else:
            mode = self.mode
        action = view.key_action(mode, event.key, event.character)
        if action is None:
            if mode in view.MODAL_MODES:
                event.stop()
                event.prevent_default()
            return
        event.stop()
        event.prevent_default()
        self.clear_message()
        getattr(self, f"do_{action}")(event)

    def do_cursor_up(self, event=None) -> None:
        self.session_list.move("up")

    def do_cursor_down(self, event=None) -> None:
        self.session_list.move("down")

    def do_cursor_pageup(self, event=None) -> None:
        self.session_list.move("pageup")

    def do_cursor_pagedown(self, event=None) -> None:
        self.session_list.move("pagedown")

    def do_cursor_home(self, event=None) -> None:
        self.session_list.move("home")

    def do_cursor_end(self, event=None) -> None:
        self.session_list.move("end")

    def do_tab_prev(self, event=None) -> None:
        self.select_tab(view.cycle_tab(self.tab_index, -1, len(self.tabs)))

    def do_tab_next(self, event=None) -> None:
        self.select_tab(view.cycle_tab(self.tab_index, 1, len(self.tabs)))

    def do_tab_digit(self, event) -> None:
        index = view.tab_for_digit(self.tabs, event.character or "")
        if index is not None:
            self.select_tab(index)

    def do_quit(self, event=None) -> None:
        self.app.exit(return_code=0)

    def do_help(self, event=None) -> None:
        self.app.push_screen(HelpScreen())

    def do_escape(self, event=None) -> None:
        """Esc: stop a full rescan, else clear the filter, else stop a quick scan."""
        if self._scan is not None and self._scan["deep"]:
            self._cancel_scan()
        elif self.needle:
            self.do_filter_clear()
        elif self._scan is not None:
            self._cancel_scan()

    # --- filter ----------------------------------------------------------

    def do_filter(self, event=None) -> None:
        self.query_one("#filterbar").display = True
        self.filter_input.focus()

    def do_filter_accept(self, event=None) -> None:
        self.query_one("#filterbar").display = bool(self.needle)
        self.session_list.focus()

    def do_filter_clear(self, event=None) -> None:
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
            self._refresh_rows()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, Input):
            self._close_prompt()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        # The filter box left by a click: an empty one has nothing to show.
        if isinstance(event.widget, Input) and not self.needle:
            self.query_one("#filterbar").display = False

    # --- prompts (the line over the key hints) ----------------------------

    def _open_prompt(self, mode: str, session, stay: bool = False) -> None:
        row = self.session_list.current
        self.mode = mode
        self._prompt_key = session.key
        self._prompt_stay = stay
        self._prompt_about = (view.clean(session.title) or session.id, row.where if row else "")
        self._render_prompt()
        self.query_one("#prompt").display = True

    def _render_prompt(self) -> None:
        """Word the prompt for the width there is."""
        if self.mode == "list":
            return
        prompt = self.query_one("#prompt", Static)
        room = self.size.width - prompt.styles.gutter.width - prompt.styles.margin.width
        title, where = self._prompt_about
        if self.mode == "foreign":
            prompt.update(view.foreign_prompt(where, room))
        else:
            prompt.update(view.confirm_prompt(title, where, room))

    def _close_prompt(self) -> None:
        if self.mode == "list":
            return
        self.mode = "list"
        self._prompt_key = None
        self.query_one("#prompt").display = False

    def _take_prompt(self) -> tuple[str | None, bool]:
        key, stay = self._prompt_key, self._prompt_stay
        self._close_prompt()
        return key, stay

    def do_prompt_cancel(self, event=None) -> None:
        self._close_prompt()

    def do_prompt_import_resume(self, event=None) -> None:
        key, stay = self._take_prompt()
        self._start_import(key, then_open=True, stay=stay)

    def do_prompt_import_only(self, event=None) -> None:
        key, _stay = self._take_prompt()
        self._start_import(key, then_open=False, stay=True)

    def do_prompt_view(self, event=None) -> None:
        key, _stay = self._take_prompt()
        self._view(key)

    def do_confirm_yes(self, event=None) -> None:
        key, _stay = self._take_prompt()
        self._start_import(key, then_open=False, stay=True)

    def do_confirm_no(self, event=None) -> None:
        self._close_prompt()

    # --- resume ----------------------------------------------------------

    def do_resume(self, event=None, stay: bool = False) -> None:
        """Enter: a native session resumes; a foreign one resumes its native
        copy when it has one, else asks what to do."""
        session = self.current_session()
        if session is None:
            return
        if session.origin == "native":
            self._start_open(session.key, session.title, stay)
        elif session.imported_as:
            self._start_open(session.imported_as, session.title, stay)
        else:
            self._open_prompt("foreign", session, stay)

    def do_resume_stay(self, event=None) -> None:
        self.do_resume(stay=True)

    def _start_open(self, key: str, title: str, stay: bool) -> None:
        if self._opening:
            return
        self._opening = True
        self._work(
            self._open_thread,
            key,
            view.clean(title) or key,
            stay,
            self.snapshot.index,
            group="open",
        )

    def _open_thread(self, key, title, stay, index) -> None:
        try:
            plan = self.engine.open_session(key, index=index)
        except Exception as exc:
            self._ui(self._open_failed, view.error_text(exc))
            return
        self._ui(self._opened, title, getattr(plan, "note", None), stay)

    def _open_failed(self, reason: str) -> None:
        self._opening = False
        self.set_message(reason, error=True)

    def _opened(self, title: str, note, stay: bool) -> None:
        self._opening = False
        if not stay:
            self.app.exit(result=note or None, return_code=0)
            return
        self.set_message(f"opened {title}" + (f" · {note}" if note else ""))

    # --- import ----------------------------------------------------------

    def do_import(self, event=None) -> None:
        """``i``: import the selected foreign session, after a y/n."""
        session = self.current_session()
        if session is None:
            return
        if session.origin == "native":
            self.set_message("already on Linux; nothing to import")
            return
        if session.imported_as:
            self.set_message("already imported; Enter resumes the Linux copy")
            return
        verdict = self._verdicts.get(session.key)
        if verdict is not None and verdict[0] is False:
            self.set_message(verdict[1] or "this session cannot be imported", error=True)
            return
        self._open_prompt("confirm", session)

    def _start_import(self, key, then_open: bool, stay: bool) -> None:
        session = self.snapshot.sessions.get(key)
        if session is None:
            return
        if self._importing:
            self.set_message("an import is already running")
            return
        if self._scan is not None:
            self.set_message("a scan is running; import when it has finished")
            return
        self._importing = True
        self._set_activity(view.import_progress_text(0, 0))
        self._work(
            self._import_thread,
            key,
            view.clean(session.title) or key,
            then_open,
            stay,
            self.snapshot.index,
            group="import",
        )

    def _import_thread(self, key, title, then_open, stay, index) -> None:
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if now - last[0] >= PROGRESS_INTERVAL:
                last[0] = now
                self._ui(self._set_activity, view.import_progress_text(done, total))

        try:
            copy = self.engine.import_session(key, progress=progress, index=index)
        except Exception as exc:
            self._ui(self._import_done, title, None, None, False, None, view.error_text(exc), stay)
            return
        new_key = getattr(copy, "key", None)
        snapshot, opened, note, error = None, False, None, None
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

    async def _import_done(self, title, new_key, snapshot, opened, note, error, stay) -> None:
        """``new_key`` is None when the import itself failed (``error`` says
        why); otherwise the copy exists and ``error`` is what went wrong after
        (reading the index again, or the launch)."""
        self._importing = False
        self._set_activity("")
        if snapshot is not None:
            await self._apply_snapshot(snapshot, select_key=new_key)
        if new_key is None:
            self.set_message(error, error=True)
        elif opened and not stay:
            self.app.exit(result=note or None, return_code=0)
        elif error:
            self.set_message(f"imported {title}, but: {error}", error=True)
        else:
            text = f"imported and opened {title}" if opened else f"imported {title}"
            self.set_message(text + (f" · {note}" if note else ""))

    # --- view ------------------------------------------------------------

    def do_view(self, event=None) -> None:
        session = self.current_session()
        if session is not None:
            self._view(session.key)

    def _view(self, key) -> None:
        session = self.snapshot.sessions.get(key)
        if session is None:
            return
        self.app.push_screen(
            TranscriptScreen(
                self.engine, key, view.clean(session.title) or key, self.snapshot.index, tz=self.tz
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

    async def _loaded(self, snapshot, error) -> None:
        """The cached index is on screen; now bring it up to date."""
        self._set_activity("")
        if snapshot is not None:
            await self._apply_snapshot(snapshot)
        else:
            self.loaded = True
            self._refresh_rows()
            self.set_message(error, error=True)
        self.start_scan(STARTUP_SCAN_DEEP, announce=False)

    def do_rescan(self, event=None) -> None:
        self.start_scan(False)

    def do_full_rescan(self, event=None) -> None:
        self.start_scan(True)

    def start_scan(self, deep: bool | None, announce: bool = True) -> None:
        if self._scan is not None:
            self.set_message("a scan is already running")
            return
        if self._importing:
            self.set_message("an import is running; rescan when it has finished")
            return
        cancel = threading.Event()
        self._scan = {"deep": bool(deep), "cancel": cancel}
        self._set_activity(f"scanning{view.ELLIPSIS}")
        self._work(self._scan_thread, deep, cancel, announce, group="scan")

    def _cancel_scan(self) -> None:
        if self._scan is not None:
            self._scan["cancel"].set()
            self.set_message(f"stopping the scan{view.ELLIPSIS}")

    def _scan_thread(self, deep, cancel, announce) -> None:
        last = [0.0]

        def progress(label, dirs):
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

    async def _scan_done(self, summary, snapshot, error, announce) -> None:
        self._scan = None
        self._set_activity("")
        if snapshot is not None:
            await self._apply_snapshot(snapshot)
        if error:
            self.set_message(error, error=True)
        elif announce or (summary or {}).get("cancelled"):
            self.set_message(view.scan_message(summary))


# --- help overlay -------------------------------------------------------------------


class HelpScreen(ModalScreen):
    """The key help; ``?``, ``q``, Esc, Enter or a click closes it."""

    def compose(self) -> ComposeResult:
        with Vertical(id="help"):
            yield Static("Keys", id="help-title", markup=False)
            yield Static("", id="help-text", markup=False)
            yield Static("Esc closes", id="help-hint", markup=False)

    def on_mount(self) -> None:
        self._fill()

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
            self.dismiss()
        elif action == "up":
            box.scroll_up(animate=False)
        elif action == "down":
            box.scroll_down(animate=False)
        elif action == "pageup":
            box.scroll_page_up(animate=False)
        elif action == "pagedown":
            box.scroll_page_down(animate=False)

    def on_click(self, event: events.Click) -> None:
        self.dismiss()


# --- transcript pager ----------------------------------------------------------------


class TranscriptView(ScrollView, can_focus=True):
    """A transcript drawn line by line. Turns arrive in batches and are
    wrapped a slice at a time, so neither a long session nor a resize holds
    the keyboard up."""

    COMPONENT_CLASSES = {
        "transcript--role-user",
        "transcript--role-assistant",
        "transcript--role-tool",
        "transcript--text",
        "transcript--note",
        "transcript--blank",
    }

    def __init__(self, tz=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tz = tz
        self.turns: list[tuple] = []
        self.lines: list[tuple[str, str]] = []
        self._starts: list[int] = []  # first line of every wrapped turn
        self._width = 0
        self._anchor: int | None = None  # turn to scroll back to after a re-wrap
        self._wrapping = False
        self._rewrap_due = False

    def add_turns(self, turns) -> None:
        self.turns.extend(turns)
        if not self._wrapping:
            self._wrap_some()

    def on_resize(self, event: events.Resize) -> None:
        self._rewrap()

    def render_lines(self, crop):
        # A scrollbar that appears narrows the content without a Resize.
        if self.size.width != self._width and not self._rewrap_due:
            self._rewrap_due = True
            self.call_later(self._rewrap)
        return super().render_lines(crop)

    def _rewrap(self) -> None:
        """Wrap everything again when the width has changed, and come back
        to the turn that was at the top."""
        self._rewrap_due = False
        width = self.size.width
        if width == self._width:
            return
        if self._starts and self._anchor is None:
            top = int(self.scroll_offset.y)
            self._anchor = max(bisect_right(self._starts, top) - 1, 0)
        self._width = width
        self.lines, self._starts = [], []
        self.virtual_size = Size(width, 0)
        if not self._wrapping:
            self._wrap_some()

    def _wrap_some(self) -> None:
        """Wrap pending turns for ``PAGER_SLICE`` seconds, then come back."""
        self._wrapping = False
        if self._width <= 0:
            return
        deadline = time.monotonic() + PAGER_SLICE
        while len(self._starts) < len(self.turns):
            role, text, ts = self.turns[len(self._starts)]
            self._starts.append(len(self.lines))
            self.lines.extend(view.transcript_lines(role, text, ts, self._width, self.tz))
            if time.monotonic() > deadline:
                break
        self.virtual_size = Size(self._width, len(self.lines))
        if self._anchor is not None and self._anchor < len(self._starts):
            self.scroll_to(y=self._starts[self._anchor], animate=False, force=True)
            self._anchor = None
        self.refresh()
        if len(self._starts) < len(self.turns):
            self._wrapping = True
            self.call_later(self._wrap_some)

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        index = y + int(self.scroll_offset.y)
        if not 0 <= index < len(self.lines):
            return Strip.blank(width, self.rich_style)
        kind, text = self.lines[index]
        style = self.get_component_rich_style(f"transcript--{kind}")
        return Strip([Segment(text, style)]).adjust_cell_length(width, self.rich_style)


class TranscriptScreen(_UiCalls, Screen):
    """The built-in pager: one session's transcript, read in a worker."""

    def __init__(
        self, engine, key: str, title: str, index=None, tz=None, max_turns: int | None = None
    ) -> None:
        super().__init__(id="pager")
        self.engine = engine
        self.key = key
        self.session_title = title
        self.index = index
        self.tz = tz
        self.max_turns = view.PAGER_MAX_TURNS if max_turns is None else max_turns
        self.turn_count = 0
        self.reading = True
        self._gone = False

    def compose(self) -> ComposeResult:
        yield Static(self.session_title, id="pager-title", markup=False)
        yield TranscriptView(tz=self.tz, id="pager-body")
        with Horizontal(id="pager-footer"):
            yield Static(view.PAGER_HINTS, id="pager-hints", markup=False)
            yield Static("", id="pager-status", markup=False)

    @property
    def body(self) -> TranscriptView:
        return self.query_one("#pager-body", TranscriptView)

    def on_mount(self) -> None:
        self.body.focus()
        self._render_status()
        self.run_worker(self._load_thread, thread=True, group="transcript", exit_on_error=False)

    def on_unmount(self) -> None:
        self._gone = True

    def _render_status(self, capped: bool = False, error: str | None = None) -> None:
        status = self.query_one("#pager-status", Static)
        status.update(view.pager_status(self.turn_count, self.reading, capped, error))
        status.set_class(bool(error), "-error")

    def _load_thread(self) -> None:
        worker = get_current_worker()
        batch: list[tuple] = []
        count, last = 0, time.monotonic()
        capped, error = False, None
        try:
            for turn in self.engine.transcript(self.key, index=self.index):
                if self._gone or worker.is_cancelled:
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

    def _append(self, batch) -> None:
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
        if action == "close":
            self._gone = True
            self.dismiss()
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
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", show=False, priority=True)]

    def __init__(self, engine=None, clock=_utc_now, tz=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.engine = engine if engine is not None else Engine()
        self.clock = clock
        self.tz = tz
        self.theme = THEME
        self._signals: list[int] = []

    def get_default_screen(self) -> Screen:
        return MainScreen(self.engine, clock=self.clock, tz=self.tz)

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
                loop.add_signal_handler(number, partial(self.exit, return_code=128 + number))
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
    "SessionList",
    "SessionsApp",
    "Snapshot",
    "TabLabel",
    "TranscriptScreen",
    "TranscriptView",
    "UiCall",
    "build_snapshot",
]
