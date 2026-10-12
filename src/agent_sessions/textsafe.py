"""The one place that makes text from a session store safe to show.

Every byte of a store on another drive is hostile: a title, a working
directory, a file name or a line of a transcript can carry terminal escape
sequences, or characters that reorder or hide what is printed. Everything the
CLI, the TUI and the desktop notification show goes through here.

What is neutralised (``UNSAFE``):

- the C0 controls U+0000-U+001F (escape, bell, backspace, carriage return, …),
- DEL U+007F and the C1 controls U+0080-U+009F (U+009B is an 8-bit CSI),
- the bidirectional controls U+061C, U+200E, U+200F, U+202A-U+202E and
  U+2066-U+2069, which reorder the text around them,
- U+2028 and U+2029, which some terminals take as line breaks, and U+FEFF.

Replace, not remove, decided once for every caller: each such character
becomes one space. A removed character would join its neighbours into a word
that was never in the data (``a\\x1bb`` would read ``ab``), and a one-for-one
replacement keeps the length of a value, so a column cut by character count
stays where it was.

``clean`` gives one line; ``clean(text, multiline=True)`` keeps ``\\n`` and
``\\t`` for transcript text. ``json_escape`` is for ``--json`` output, which
must stay exact data: there the same characters are written as ``\\uXXXX``
escapes, which a JSON parser turns back into the original string.

Standard library only; imports nothing from this package.
"""

from __future__ import annotations

import re

_RANGES = (
    (0x0000, 0x001F),  # C0 controls
    (0x007F, 0x009F),  # DEL and the C1 controls
    (0x061C, 0x061C),  # Arabic letter mark
    (0x200E, 0x200F),  # left-to-right and right-to-left marks
    (0x2028, 0x2029),  # line and paragraph separators
    (0x202A, 0x202E),  # bidirectional embeddings and overrides
    (0x2066, 0x2069),  # bidirectional isolates
    (0xFEFF, 0xFEFF),  # byte order mark, a zero-width space anywhere else
)
# Every code point ``clean`` replaces.
UNSAFE = frozenset(chr(point) for first, last in _RANGES for point in range(first, last + 1))
REPLACEMENT = " "

_ONE_LINE = {ord(ch): REPLACEMENT for ch in UNSAFE}
_MULTILINE = {point: to for point, to in _ONE_LINE.items() if chr(point) not in "\n\t"}
# json.dumps escapes the C0 controls itself; these are what it leaves as they are.
_JSON_RAW = re.compile("[" + "".join(sorted(ch for ch in UNSAFE if ch > "\x1f")) + "]")


def clean(text: object, multiline: bool = False) -> str:
    """``text`` with every character of ``UNSAFE`` replaced by a space; None
    gives an empty string, anything else that is not text goes through
    ``str``. With ``multiline`` the line feed and the tab are kept."""
    text = "" if text is None else str(text)
    if multiline:
        return text.translate(_MULTILINE)
    if text.isprintable():  # the common case: nothing to replace
        return text
    return text.translate(_ONE_LINE)


def json_escape(document: str) -> str:
    """A JSON document made safe to print: DEL, the C1 controls and the
    other ``UNSAFE`` characters become ``\\uXXXX`` escapes. In JSON they can
    only stand inside a string, where the escape means the same character, so
    the document parses to exactly the same value."""
    return _JSON_RAW.sub(lambda found: f"\\u{ord(found.group()):04x}", document)


__all__ = ["REPLACEMENT", "UNSAFE", "clean", "json_escape"]
