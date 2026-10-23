"""The cathode-ray screen: how the monitors in Central Dogma redraw.

The screens in the series are not animated the way a modern interface is.
Nothing slides or fades: a picture is *painted*. A raster band sweeps down
over static and leaves the new picture behind it, the old one still showing
below until the band gets there; text is printed a character at a time
behind a block cursor; a screen that arrives comes in interlaced, the even
lines first. That is what this module does to every widget that draws
through ``CrtMixin``.

One ``Raster`` belongs to each screen and keeps time for all of it, so a
sweep is one band crossing every panel at once, as the gun of one tube
would. Nothing here runs when motion is off (``eva.motion_enabled``): the
mixin then hands the lines on untouched.
"""

from __future__ import annotations

import math
import random
import time
from typing import TYPE_CHECKING, Any

from rich.segment import Segment
from rich.style import Style
from textual.containers import Horizontal, Vertical
from textual.dom import NoScreen
from textual.geometry import Region
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from . import eva

if TYPE_CHECKING:
    from textual.screen import Screen

NOISE = "▓▒░▚▞╳┼·"  # noqa: RUF001 (what the band paints where it passes)
SWEEP_FRAMES = 9  # a sweep crosses the screen in this many frames
TYPE_STEP = 9  # cells a line types in per frame
TYPE_SLANT = 3  # each line starts this many cells after the one above
CURSOR = "█"
BAND = 2  # lines of static at the head of the band


class Raster:
    """The time base of one screen's effects.

    ``sweep``: a band crosses the whole screen top to bottom. ``interlace``:
    even lines, then odd lines, then the picture. ``type_in``: the given
    widgets print themselves. Widgets read the state when they draw
    (``CrtMixin.render_lines``); the raster only refreshes them each frame
    until nothing is moving."""

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        self.kind: str | None = None
        self.started = 0.0
        self.step = 1
        self.frames = 0
        self.typing: dict[int, tuple[float, float]] = {}  # id(widget) -> (start, end)
        self.rng = random.Random()  # noqa: S311 (static, not secrets)
        self._timer: Timer | None = None
        self._styles: dict[str, Style] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.screen.app, "motion", False))

    def frame(self, since: float | None = None) -> int:
        start = self.started if since is None else since
        return math.floor((time.monotonic() - start) / eva.FRAME)

    # --- starting -------------------------------------------------------------

    def sweep(self) -> None:
        """A raster band crosses the screen; the new picture behind it."""
        if not self.enabled:
            return
        height = max(self.screen.size.height, 1)
        self.kind = "sweep"
        self.step = eva.reveal_step(height, SWEEP_FRAMES)
        self.frames = -(-height // self.step) + 2
        self.started = time.monotonic()
        self._run()

    def interlace(self) -> None:
        """The screen comes in on alternate lines."""
        if not self.enabled:
            return
        self.kind = "interlace"
        self.frames = 3
        self.started = time.monotonic()
        self._run()

    def type_in(self, *widgets: Widget, stagger: float = eva.FRAME) -> None:
        """Each widget prints its lines, a few cells a frame, each one
        ``stagger`` seconds after the one before."""
        if not self.enabled:
            return
        now = time.monotonic()
        for number, widget in enumerate(widgets):
            start = now + number * stagger
            width = max(widget.size.width + widget.styles.gutter.width, 1)
            height = max(widget.size.height + widget.styles.gutter.height, 1)
            frames = -(-(width + TYPE_SLANT * height) // TYPE_STEP)
            self.typing[id(widget)] = (start, start + frames * eva.FRAME)
        self._run()

    def typing_frame(self, widget: Widget) -> int | None:
        times = self.typing.get(id(widget))
        if times is None or time.monotonic() >= times[1]:
            return None
        return self.frame(times[0])

    def stop(self) -> None:
        self.kind = None
        self.typing.clear()
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _run(self) -> None:
        if self._timer is None:
            self._timer = self.screen.set_interval(eva.FRAME, self._tick)
        self._tick()

    def _tick(self) -> None:
        if self.kind is not None and self.frame() >= self.frames:
            self.kind = None
        now = time.monotonic()
        for key in [key for key, (_start, end) in self.typing.items() if now >= end]:
            del self.typing[key]
        for widget in self.screen.query(Widget):
            if isinstance(widget, CrtMixin):
                widget.refresh()
        if self.kind is None and not self.typing and self._timer is not None:
            self._timer.stop()
            self._timer = None

    # --- painting -------------------------------------------------------------

    def style(self, name: str) -> Style:
        """``crt--noise``, ``crt--bloom``, ``crt--old`` from the screen's CSS."""
        style = self._styles.get(name)
        if style is None:
            try:
                style = self.screen.get_component_rich_style(name)
            except KeyError:
                style = Style()
            self._styles[name] = style
        return style

    def noise(self, width: int) -> Strip:
        text = "".join(self.rng.choice(NOISE) for _ in range(width))
        return Strip([Segment(text, self.style("crt--noise"))], width)

    def bloom(self, strip: Strip) -> Strip:
        """A line just painted: brighter than it will stay."""
        return Strip(
            list(Segment.apply_style(strip, post_style=self.style("crt--bloom"))),
            strip.cell_length,
        )

    def decay(self, strip: Strip) -> Strip:
        """A line of the old picture waiting for the band."""
        return Strip(
            list(Segment.apply_style(strip, post_style=self.style("crt--old"))),
            strip.cell_length,
        )


class CrtMixin:
    """Draws a widget's lines through its screen's ``Raster``."""

    _crt_seen: dict[int, Strip]

    def render_lines(self, crop: Region) -> list[Strip]:
        strips: list[Strip] = super().render_lines(crop)  # type: ignore[misc]
        widget: Any = self
        try:
            screen = widget.screen
        except NoScreen:  # taken off its screen while a repaint was on its way: drawn plain
            return strips
        raster: Raster | None = getattr(screen, "raster", None)
        if raster is None or not raster.enabled:
            return strips
        seen = self.__dict__.setdefault("_crt_seen", {})
        typing = raster.typing_frame(widget)
        if raster.kind is None and typing is None:
            for offset, strip in enumerate(strips):
                seen[crop.y + offset] = strip
            return strips
        top = widget.region.y
        out: list[Strip] = []
        for offset, strip in enumerate(strips):
            y = crop.y + offset
            if typing is not None:
                strip = _typed(raster, strip, typing, y, crop.x)
            out.append(_painted(raster, strip, seen.get(y), top + y, crop.width))
        return out


def _typed(raster: Raster, strip: Strip, frame: int, y: int, x: int) -> Strip:
    shown = (frame + 1) * TYPE_STEP - TYPE_SLANT * y - x
    width = strip.cell_length
    if shown >= width:
        return strip
    blank = Style(bgcolor=_ground(strip))
    if shown <= 0:
        return Strip.blank(width, blank)
    head = strip.crop(0, shown)
    cursor = Segment(CURSOR, raster.style("crt--bloom"))
    return Strip([*head, cursor], shown + 1).extend_cell_length(width, blank).crop(0, width)


def _painted(raster: Raster, strip: Strip, old: Strip | None, row: int, width: int) -> Strip:
    if raster.kind == "sweep":
        band = (raster.frame() + 1) * raster.step
        if row >= band:
            if old is not None and old.cell_length == strip.cell_length:
                return raster.decay(old)
            return Strip.blank(strip.cell_length, Style(bgcolor=_ground(strip)))
        if row >= band - min(raster.step, BAND):
            return raster.noise(strip.cell_length)
        if row >= band - 2 * raster.step:
            return raster.bloom(strip)
        return strip
    if raster.kind == "interlace":
        frame = raster.frame()
        if frame == 0:
            return raster.bloom(strip) if row % 2 == 0 else Strip.blank(width, Style())
        if frame == 1 and row % 2:
            return raster.bloom(strip)
    return strip


def _ground(strip: Strip) -> Any:
    """The background a line is drawn on (for the cells not yet painted)."""
    for segment in strip:
        if segment.style is not None and segment.style.bgcolor is not None:
            return segment.style.bgcolor
    return None


class CrtStatic(CrtMixin, Static):
    """A ``Static`` that draws through the raster."""


class CrtVertical(CrtMixin, Vertical):
    """A frame (border, title) that draws through the raster."""


class CrtHorizontal(CrtMixin, Horizontal):
    """A row of widgets whose own ground draws through the raster."""


# A screen that owns a Raster declares these (its CSS colours them).
SCREEN_CLASSES = frozenset({"crt--noise", "crt--bloom", "crt--old"})

__all__ = [
    "CURSOR",
    "NOISE",
    "SCREEN_CLASSES",
    "CrtHorizontal",
    "CrtMixin",
    "CrtStatic",
    "CrtVertical",
    "Raster",
]
