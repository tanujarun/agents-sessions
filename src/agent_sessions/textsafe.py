"""The one place that makes text from a session store safe to show.

Every byte of a store on another drive is hostile: a title, a working
directory, a file name or a line of a transcript can carry terminal escape
sequences, or characters that reorder or hide what is printed. Everything the
CLI, the TUI and the desktop notification show goes through here.

What is neutralised (``UNSAFE``):

- the C0 controls U+0000-U+001F (escape, bell, backspace, carriage return, …),
- DEL U+007F and the C1 controls U+0080-U+009F (U+009B is an 8-bit CSI),
- U+2028 and U+2029, which some terminals take as line breaks,
- the lone surrogates U+D800-U+DFFF. They are not characters: JSON can spell
  one (``"\\udc9b"``), and Python uses U+DC80-U+DCFF to stand for a raw byte
  0x80-0xFF, which a stream in the C or POSIX locale writes back as that byte
  (0x9B: the 8-bit CSI again),
- every format character (Unicode category Cf). None of them draws
  anything, and each changes what is seen or hides what is there: the
  bidirectional controls U+061C, U+200E, U+200F, U+202A-U+202E and
  U+2066-U+2069 reorder the text around them; the zero-width characters
  U+200B-U+200D and U+2060, the soft hyphen U+00AD and U+FEFF sit in a word
  unseen; the tag characters U+E0001 and U+E0020-U+E007F spell ASCII text
  that no screen shows. The rest of the tag block, U+E0000-U+E007F, goes
  with them.

``UNSAFE`` lists them as of Unicode 16. A format character that a later
Unicode adds is replaced as well, wherever this Python's ``unicodedata``
knows it as one.

Replace, not remove, decided once for every caller: each such character
becomes one space. A removed character would join its neighbours into a word
that was never in the data (``a\\x1bb`` would read ``ab``), and a one-for-one
replacement keeps the length of a value, so a column cut by character count
stays where it was. It also means that what was invisible leaves a mark: a
word with a zero-width space in it reads as two.

``clean`` gives one line; ``clean(text, multiline=True)`` keeps ``\\n`` and
``\\t`` for transcript text. ``invisible`` counts the characters of a text
that draw nothing, for whoever has to say that some were replaced.
``json_escape`` is for ``--json`` output, which must stay exact data: there
the same characters are written as ``\\uXXXX`` escapes, which a JSON parser
turns back into the original string.

Standard library only; imports nothing from this package.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

_RANGES = (
    (0x0000, 0x001F),  # C0 controls
    (0x007F, 0x009F),  # DEL and the C1 controls
    (0x00AD, 0x00AD),  # soft hyphen
    (0x0600, 0x0605),  # Arabic number signs
    (0x061C, 0x061C),  # Arabic letter mark
    (0x06DD, 0x06DD),  # Arabic end of ayah
    (0x070F, 0x070F),  # Syriac abbreviation mark
    (0x0890, 0x0891),  # Arabic pound and piastre marks above
    (0x08E2, 0x08E2),  # Arabic disputed end of ayah
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x200B, 0x200F),  # zero-width space, non-joiner and joiner; the directional marks
    (0x2028, 0x2029),  # line and paragraph separators
    (0x202A, 0x202E),  # bidirectional embeddings and overrides
    (0x2060, 0x2064),  # word joiner and the invisible operators
    (0x2066, 0x206F),  # bidirectional isolates, deprecated format characters
    (0xD800, 0xDFFF),  # lone surrogates
    (0xFEFF, 0xFEFF),  # byte order mark, a zero-width space anywhere else
    (0xFFF9, 0xFFFB),  # interlinear annotation
    (0x110BD, 0x110BD),  # Kaithi number sign
    (0x110CD, 0x110CD),  # Kaithi number sign above
    (0x13430, 0x1343F),  # Egyptian hieroglyph format controls
    (0x1BCA0, 0x1BCA3),  # shorthand format controls
    (0x1D173, 0x1D17A),  # musical symbol format controls
    (0xE0000, 0xE007F),  # the tag block
)
# Every code point ``clean`` replaces, as of Unicode 16.
UNSAFE = frozenset(chr(point) for first, last in _RANGES for point in range(first, last + 1))
REPLACEMENT = " "
_FORMAT = "Cf"  # the category of a character that only directs how others are shown


def unsafe(ch: str) -> bool:
    """Whether ``clean`` replaces this character."""
    return ch in UNSAFE or unicodedata.category(ch) == _FORMAT


class _Table(dict[int, "int | str"]):
    """A table for ``str.translate`` that is filled as characters are met:
    the first time a code point is asked for it is judged (``unsafe``) and
    the answer kept, so a text is walked by ``translate`` itself and this
    code runs once per distinct character, not once per character."""

    def __init__(self, spell: Callable[[str], str], keep: str = "") -> None:
        super().__init__()
        self._spell = spell  # what an unsafe character becomes
        self._keep = keep  # unsafe characters that stay all the same

    def __missing__(self, point: int) -> int | str:
        ch = chr(point)
        value: int | str = self._spell(ch) if unsafe(ch) and ch not in self._keep else point
        self[point] = value
        return value


def _escape(ch: str) -> str:
    """A character as JSON spells it in ASCII: ``\\uXXXX``, two of them (a
    surrogate pair) beyond U+FFFF. A lone surrogate is spelled as itself,
    ``\\udc9b``, which parses back to the same lone surrogate."""
    point = ord(ch)
    if point <= 0xFFFF:
        return f"\\u{point:04x}"
    point -= 0x10000
    return f"\\u{0xD800 + (point >> 10):04x}\\u{0xDC00 + (point & 0x3FF):04x}"


_C0 = "".join(map(chr, range(0x20)))
_ONE_LINE = _Table(lambda ch: REPLACEMENT)
_MULTILINE = _Table(lambda ch: REPLACEMENT, keep="\n\t")
# json.dumps escapes the C0 controls inside a string itself: those that are
# left in a document are its own line breaks and indentation.
_JSON = _Table(_escape, keep=_C0)
_BEYOND_ASCII = re.compile(r"[^\x00-\x7f]")


def clean(text: object, multiline: bool = False) -> str:
    """``text`` with every unsafe character replaced by a space; None gives
    an empty string, anything else that is not text goes through ``str``.
    With ``multiline`` the line feed and the tab are kept."""
    text = "" if text is None else str(text)
    if text.isprintable():  # the common case: nothing to replace
        return text
    return text.translate(_MULTILINE if multiline else _ONE_LINE)


def invisible(text: object) -> int:
    """How many characters of ``text`` draw nothing and are replaced for
    that reason alone: the format characters and the tag block. Controls,
    line separators and lone surrogates are not counted; a terminal acts on
    those, or shows them."""
    text = "" if text is None else str(text)
    if text.isprintable():
        return 0
    return sum(
        text.count(ch)
        for ch in set(text)
        if unicodedata.category(ch) == _FORMAT or 0xE0000 <= ord(ch) <= 0xE007F
    )


def json_escape(document: str, ascii_only: bool = False) -> str:
    """A JSON document made safe to print: DEL, the C1 controls, lone
    surrogates and the other unsafe characters become ``\\uXXXX`` escapes.
    In JSON they can only stand inside a string, where the escape means the
    same character, so the document parses to exactly the same value. What
    comes out can always be encoded as UTF-8.

    ``ascii_only`` escapes every character beyond ASCII as well: for a
    stream whose encoding is not UTF-8, where a character it cannot encode
    would otherwise be written in a form JSON does not have."""
    if document.isascii() and "\x7f" not in document:  # nothing above the C0 controls to escape
        return document
    document = document.translate(_JSON)
    if ascii_only:
        document = _BEYOND_ASCII.sub(lambda found: _escape(found.group()), document)
    return document


__all__ = ["REPLACEMENT", "UNSAFE", "clean", "invisible", "json_escape", "unsafe"]
