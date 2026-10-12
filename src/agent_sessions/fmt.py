"""Small values as a listing shows them: an age, a byte count, a project's
name, a local time; and the one clock the engine stamps its files with.

One copy for every front end. Each function takes what a cache or a store
may hold, a value of the wrong type or a time no calendar has included, and
gives ``EMPTY`` for what it cannot place: none of them raises. What they
return is plain text, not yet made safe to show (``textsafe`` does that on
the way out).

Standard library only; imports nothing from this package.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo

EMPTY = "-"  # what stands in a listing for a value that is not known

_SEPARATORS = re.compile(r"[\\/]+")  # a folder as Windows or as Linux wrote it
_UNITS = ("kB", "MB", "GB")


def utc_now() -> str:
    """The time now, to the second: ``2026-10-13T05:07:41Z``."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_stamp(stamp: object) -> datetime | None:
    """An ISO 8601 time as an aware datetime; None when it is absent or not
    one. A time without a zone is taken as UTC, as ``parse_ts`` takes it."""
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def age(stamp: object, now: datetime | None = None) -> str:
    """How long ago ``stamp`` was: ``now``, ``5m``, ``3h``, ``2d``, ``3w``,
    ``2mo``, ``1y``. Seconds under a minute are ``now`` (a time in the
    future too), days up to 13, weeks up to 59 days, months up to a year.
    ``EMPTY`` for anything that is not a time."""
    then = parse_stamp(stamp)
    if then is None:
        return EMPTY
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        seconds = max((now - then).total_seconds(), 0)
    except (OverflowError, ValueError, TypeError):
        return EMPTY
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
    """``2026-10-10 17:58`` in the local zone (or ``tz``); ``EMPTY`` for
    anything that is not a time, and for one the zone cannot express (the
    last day of the year 9999, east of Greenwich)."""
    moment = parse_stamp(stamp)
    if moment is None:
        return EMPTY
    try:
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, ValueError, OSError):
        return EMPTY


def size(count: object) -> str:
    """A byte count in decimal units, one decimal from a kilobyte up:
    ``812 B``, ``116.7 kB``, ``123.7 MB``, ``5000.0 GB``. The unit is the
    one in which the rounded number stays below 1000 (999,950 bytes are
    ``1.0 MB``, not ``1000.0 kB``). Less than nothing counts as nothing;
    ``EMPTY`` for what is not a number."""
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return EMPTY if count is not None else "0 B"
    try:
        number = max(int(count), 0)
    except (OverflowError, ValueError):  # infinity, not a number
        return EMPTY
    if number < 1000:
        return f"{number} B"
    value, unit = number / 1000, _UNITS[0]
    for larger in _UNITS[1:]:
        if round(value, 1) < 1000:
            break
        value, unit = value / 1000, larger
    return f"{value:.1f} {unit}"


def project(cwd: object) -> str:
    """The folder a session ran in: the last segment of its recorded
    working directory, whichever separator it was written with. ``EMPTY``
    when there is none."""
    parts = [part for part in _SEPARATORS.split(cwd) if part] if isinstance(cwd, str) else []
    return parts[-1] if parts else EMPTY


__all__ = ["EMPTY", "age", "local_time", "parse_stamp", "project", "size", "utc_now"]
