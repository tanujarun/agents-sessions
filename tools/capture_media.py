#!/usr/bin/env python3
"""Draw the README's screenshots and animation from made-up data.

Runs the real TUI under Textual's headless driver against the test suite's fake
engine, so nothing here reads a store, a drive or a session of the machine it runs on.
The sessions below are invented for the pictures. Screens are exported as SVG
(``App.export_screenshot``), turned into PNG by ``rsvg-convert`` and joined into a GIF by
``ffmpeg``; both tools are needed only for this script, not for the program.

Run it with the project's own Python, from anywhere::

    .venv/bin/python tools/capture_media.py            # writes docs/media/
    .venv/bin/python tools/capture_media.py some/dir
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

import tui_support as ts  # noqa: E402  (also points state and cache at temp directories)

# tui_support switches the transitions off for the test suites; the pictures want them.
os.environ.pop("AGENT_SESSIONS_MOTION", None)

from agent_sessions.tui.app import SessionsApp  # noqa: E402

M = ts.make_session
SESSIONS = [
    M(
        "claude",
        1,
        title="Refactor the invoice parser",
        age=6,
        cwd="/home/user/code/ledger",
        messages=48,
    ),
    M(
        "claude",
        2,
        title="Add dark mode to the settings page",
        age=95,
        cwd="/home/user/code/orbit-web",
        messages=31,
    ),
    M(
        "codex",
        3,
        title="Profile the slow startup path",
        age=40,
        cwd="/home/user/code/ledger",
        messages=22,
    ),
    M(
        "grok",
        4,
        title="Benchmark the directory walk",
        age=3000,
        cwd="/home/user/code/ledger",
        messages=9,
    ),
    M(
        "claude",
        5,
        title="Fix flaky CI on the release branch",
        age=7000,
        cwd="/home/user/code/orbit-web",
        messages=64,
    ),
    M(
        "codex",
        6,
        title="Sketch the plugin API",
        age=9000,
        cwd="/home/user/code/harbor",
        messages=15,
    ),
    M(
        "claude",
        7,
        foreign=True,
        title="Landing page copy pass",
        age=14000,
        cwd="C:\\Work\\bloom-site",
        messages=27,
    ),
    M(
        "codex",
        8,
        foreign=True,
        title="Scraper for the price list",
        age=26000,
        cwd="C:\\Work\\scout",
        messages=18,
    ),
    M(
        "antigravity",
        9,
        foreign=True,
        title="Wireframe the onboarding flow",
        age=60000,
        cwd="C:\\Work\\bloom-site",
        messages=11,
    ),
    M(
        "grok",
        10,
        title="Why is the fan curve ignored",
        age=70000,
        cwd="/home/user/code/harbor",
        messages=7,
    ),
    M(
        "claude",
        11,
        foreign=True,
        title="Invoice export to CSV",
        age=90000,
        cwd="C:\\Work\\ledger",
        messages=33,
        offline=True,
    ),
    M(
        "claude",
        12,
        title="Write the migration guide",
        age=110000,
        cwd="/home/user/code/orbit-web",
        messages=40,
    ),
]
CONVERSATION = [
    ts.Turn(
        "user", "The parser drops the last line item when a PDF has two pages.", ts.stamp(minutes=9)
    ),
    ts.Turn("tool", "Read", None),
    ts.Turn("tool", "Grep", None),
    ts.Turn(
        "assistant",
        "Found it: the page loop stops one row early. A fix and a test follow.",
        ts.stamp(minutes=8),
    ),
    ts.Turn("user", "Good. Run the suite and tell me what else breaks.", ts.stamp(minutes=7)),
    ts.Turn("tool", "Bash", None),
    ts.Turn(
        "assistant",
        "All 212 tests pass; one warning in the date formatter, left alone.",
        ts.stamp(minutes=6),
    ),
]

WIDE = (200, 56)  # the stills: every panel fits
NARROW = (110, 32)  # a smaller window: tabs and readout under the log
ANIMATION = (170, 46)
FRAME_WIDTH = 900  # pixels of an animation frame


def engine() -> "ts.FakeEngine":
    fake = ts.FakeEngine(sessions=list(SESSIONS))
    fake.default_turns = CONVERSATION
    return fake


class Frames:
    """Screenshots taken at the moments the animation shows, with how long each one stays."""

    def __init__(self, app, folder: Path) -> None:
        self.app = app
        self.folder = folder
        self.taken: list[tuple[Path, float]] = []

    def grab(self) -> None:
        path = self.folder / f"{len(self.taken):04d}.svg"
        path.write_text(self.app.export_screenshot())
        self.taken.append((path, time.monotonic()))

    async def watch(self, pilot, seconds: float, every: float = 0.07) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.grab()
            await pilot.pause(every)


def png(svg: Path, width: int) -> Path:
    out = svg.with_suffix(".png")
    subprocess.run(["rsvg-convert", "-w", str(width), str(svg), "-o", str(out)], check=True)
    return out


async def still(name: str, size, keys: list[str], out: Path, width: int = 1800) -> None:
    app = SessionsApp(engine=engine(), clock=lambda: ts.NOW, tz=ts.UTC)
    async with app.run_test(size=size) as pilot:
        await ts.settle(pilot, 3)
        await pilot.pause(3.0)  # the boot sequence plays out
        for key in keys:
            await pilot.press(key)
            await ts.settle(pilot, 2)
            await pilot.pause(1.2)
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / f"{name}.svg"
            svg.write_text(app.export_screenshot())
            shutil.move(png(svg, width), out / f"{name}.png")


async def animation(out: Path) -> None:
    app = SessionsApp(engine=engine(), clock=lambda: ts.NOW, tz=ts.UTC)
    with tempfile.TemporaryDirectory() as tmp:
        frames = Frames(app, Path(tmp))
        async with app.run_test(size=ANIMATION) as pilot:
            await frames.watch(pilot, 2.4)  # boot log and the raster sweep
            await ts.settle(pilot, 2)
            await frames.watch(pilot, 0.6)
            for key, hold in (
                ("down", 0.6),
                ("down", 0.6),
                ("right", 1.2),
                ("right", 1.2),
                ("0", 1.2),
                ("down", 0.6),
            ):
                await pilot.press(key)
                await frames.watch(pilot, hold)
            frames.grab()
        gif(frames.taken, out / "tui.gif")


def gif(taken: list[tuple[Path, float]], target: Path) -> None:
    folder = taken[0][0].parent
    lines = []
    for i, (svg, at) in enumerate(taken):
        lines.append(f"file '{png(svg, FRAME_WIDTH).name}'")
        lines.append(f"duration {(taken[i + 1][1] - at) if i + 1 < len(taken) else 1.5:.3f}")
    lines.append(f"file '{png(taken[-1][0], FRAME_WIDTH).name}'")
    (folder / "frames.txt").write_text("\n".join(lines) + "\n")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(folder / "frames.txt"),
            "-vf",
            "fps=8,split[a][b];[a]palettegen=max_colors=40:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
            "-loop",
            "0",
            str(target),
        ],
        check=True,
    )


async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    await still("tui-all", WIDE, ["0"], out)
    await still("tui-claude", WIDE, ["down", "down", "down"], out)
    await still("tui-narrow", NARROW, [], out, width=1200)
    await animation(out)


if __name__ == "__main__":
    for tool in ("rsvg-convert", "ffmpeg"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is needed to draw the pictures")
    asyncio.run(main(Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs" / "media"))
