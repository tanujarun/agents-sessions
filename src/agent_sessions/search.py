"""Finding a session by what was said in it.

A ``Corpus`` keeps the text of the sessions it has read, in memory, and
answers a search with the messages that contain the text searched for. The
caller hands ``read`` the sessions and the function that gives a session's
turns (``api.transcript``); ``read`` takes the ones it has not seen, or that
changed since (``signature``), newest first. ``search`` may run in another
thread while ``read`` goes on: it looks at the sessions read so far.

Only what the user and the assistant said is searched; a tool turn's text is
the tool's name. A turn is searched as far as the pager shows it
(``TURN_CHARS``), and the corpus stops reading at ``MAX_CHARS`` in all.
Nothing here opens a file or writes one, and the text never leaves the
process: the reading is ``transcript``'s, and so are its bounds.

A match ignores case and finds the text as it is typed, spaces included,
anywhere in a message (``fold``).

Standard library only.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from . import textsafe

MIN_NEEDLE = 2
"""Shorter text is in a message of nearly every session: not searched."""

TURN_CHARS = 20000
"""Characters of one turn that are searched: what the pager shows of it."""

MAX_CHARS = 200_000_000
"""Characters kept in all; the sessions past it are not read."""

PER_SESSION = 25
"""Messages of one session in an answer at most, its newest: one long
session that says a word a thousand times would bury every other."""

LIMIT = 400
"""Messages in an answer at most."""

CONTEXT = 32
"""Characters before a match kept for its snippet."""

SNIPPET = 200
"""Characters after it."""


class SessionLike(Protocol):
    key: str
    updated: str | None
    messages: int | None
    files: list[str]


def signature(session: SessionLike) -> tuple:
    """What changes when a session does: read again when it has."""
    return (session.updated, session.messages, tuple(session.files or ()))


def fold(text: str) -> str:
    """Text as it is matched: lower case. ``str.lower`` keeps the length of
    nearly every text, so a match found in the folded text is at the same
    place in the text itself (``_place`` handles the rest)."""
    return text.lower()


@dataclass(frozen=True)
class Hit:
    """One message that contains the text searched for. The snippet is the
    line of it the match is on, made safe to show: ``before`` the match, the
    ``match`` as the message spells it, and what comes ``after``."""

    key: str
    turn: int  # its place in the transcript as ``transcript`` gives it, tool turns counted
    role: str  # "user" | "assistant"
    ts: str | None
    before: str
    match: str
    after: str
    count: int  # times the text is in the message


@dataclass(frozen=True)
class Found:
    """An answer: the ``hits`` (at most ``LIMIT``, newest first) and how
    many messages, in how many sessions, contain the text in all."""

    hits: tuple[Hit, ...] = ()
    total: int = 0
    sessions: int = 0


@dataclass(frozen=True)
class Progress:
    """How far a ``read`` is: sessions read of those it took on, and the
    ones that could not be read (gone, or on a drive that is not mounted)."""

    done: int = 0
    total: int = 0
    failed: int = 0
    full: bool = False  # MAX_CHARS was reached: the rest are not searched


class _Text(NamedTuple):
    turn: int
    role: str
    ts: str | None
    text: str
    folded: str


class Corpus:
    def __init__(self, turn_chars: int = TURN_CHARS, max_chars: int = MAX_CHARS) -> None:
        self.turn_chars = turn_chars
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._texts: dict[str, tuple[tuple, tuple[_Text, ...]]] = {}
        self._chars: dict[str, int] = {}
        self.full = False

    def __len__(self) -> int:
        return len(self._texts)

    def __contains__(self, key: object) -> bool:
        return key in self._texts

    @property
    def chars(self) -> int:
        with self._lock:
            return sum(self._chars.values())

    def stale(self, sessions: Iterable[SessionLike]) -> list[SessionLike]:
        """The sessions to read: not read yet, or changed since; none whose
        drive is not mounted. Newest first."""
        with self._lock:
            known = {key: sig for key, (sig, _) in self._texts.items()}
        wanted = [
            session
            for session in sessions
            if not getattr(session, "offline", False)
            and known.get(session.key) != signature(session)
        ]
        return sorted(
            wanted, key=lambda session: (session.updated or "", session.key), reverse=True
        )

    def add(self, key: str, sig: tuple, turns: Iterable[Any]) -> bool:
        """Keep the text of one session (``turns`` as ``transcript`` gives
        them) in place of what was kept of it. False, and nothing kept,
        when it would take the corpus past ``max_chars``."""
        texts: list[_Text] = []
        size = 0
        for number, turn in enumerate(turns):
            role, text, ts = (tuple(turn) + (None, None, None))[:3]
            if role not in ("user", "assistant") or not isinstance(text, str) or not text:
                continue
            text = text[: self.turn_chars]
            size += len(text)
            stamp = ts if isinstance(ts, str) else None
            texts.append(_Text(number, role, stamp, text, fold(text)))
        with self._lock:
            if sum(self._chars.values()) - self._chars.get(key, 0) + size > self.max_chars:
                self.full = True
                return False
            self._texts[key] = (sig, tuple(texts))
            self._chars[key] = size
        return True

    def keep(self, keys: Collection[str]) -> None:
        """Forget every session not in ``keys`` (gone since, or undone)."""
        with self._lock:
            for key in [key for key in self._texts if key not in keys]:
                del self._texts[key]
                del self._chars[key]

    def read(
        self,
        sessions: Iterable[SessionLike],
        transcript: Callable[[str], Iterable[Any]],
        progress: Callable[[Progress], None] | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> Progress:
        """Read the ``stale`` sessions with ``transcript(key)``, one at a
        time, newest first; ``progress`` hears after each one. ``stop``
        ends it between two sessions. A session that cannot be read is
        counted as failed and left out."""
        todo = self.stale(sessions)
        done = failed = 0
        for session in todo:
            if (stop is not None and stop()) or self.full:
                break
            try:
                kept = self.add(session.key, signature(session), transcript(session.key))
            except (Exception, RecursionError):
                failed += 1
            else:
                if not kept:
                    break
            done += 1
            if progress is not None:
                progress(Progress(done, len(todo), failed, self.full))
        return Progress(done, len(todo), failed, self.full)

    def search(
        self,
        needle: str,
        keys: Collection[str] | None = None,
        per_session: int = PER_SESSION,
        limit: int = LIMIT,
        stop: Callable[[], bool] | None = None,
    ) -> Found:
        """The messages that contain ``needle`` (``fold``), among the
        sessions in ``keys`` (``None``: all). Newest first; ``stop`` gives
        up between two sessions, with what was found so far."""
        folded = fold(needle.strip())
        if len(folded) < MIN_NEEDLE:
            return Found()
        with self._lock:
            entries = [
                (key, texts)
                for key, (_, texts) in self._texts.items()
                if keys is None or key in keys
            ]
        hits: list[Hit] = []
        total = sessions = 0
        for key, texts in entries:
            if stop is not None and stop():
                break
            found = [text for text in texts if folded in text.folded]
            if not found:
                continue
            total += len(found)
            sessions += 1
            found.sort(key=lambda text: (text.ts or "", text.turn), reverse=True)
            hits.extend(_hit(key, text, folded) for text in found[:per_session])
        hits.sort(key=lambda hit: (hit.ts or "", hit.key, hit.turn), reverse=True)
        return Found(tuple(hits[:limit]), total, sessions)


def _place(text: _Text, folded: str) -> tuple[int, int]:
    """Where the first match is in the text itself: ``(start, end)``."""
    if len(text.folded) == len(text.text):
        start = text.folded.find(folded)
        return start, start + len(folded)
    # Lower case changed a length (a dotted capital I, …): look again.
    found = re.search(re.escape(folded), text.text, re.IGNORECASE)
    return (found.start(), found.end()) if found is not None else (0, 0)


def _hit(key: str, text: _Text, folded: str) -> Hit:
    start, end = _place(text, folded)
    body = text.text
    line_start = body.rfind("\n", 0, start) + 1
    line_end = body.find("\n", end)
    line_end = len(body) if line_end < 0 else line_end
    first = max(line_start, start - CONTEXT)
    if first > line_start:
        space = body.find(" ", first, start)  # begin at a word
        first = space + 1 if space >= 0 else first
    before = body[first:start].lstrip()
    if first > line_start:
        before = "…" + before
    last = min(line_end, end + SNIPPET)
    after = body[end:last].rstrip() + ("…" if last < line_end else "")
    return Hit(
        key=key,
        turn=text.turn,
        role=text.role,
        ts=text.ts,
        before=textsafe.clean(before),
        match=textsafe.clean(body[start:end]),
        after=textsafe.clean(after),
        count=text.folded.count(folded),
    )


__all__ = [
    "LIMIT",
    "MIN_NEEDLE",
    "PER_SESSION",
    "Corpus",
    "Found",
    "Hit",
    "Progress",
    "fold",
    "signature",
]
