"""BLAKE3 of a short input: the plain hash, one chunk, nothing else.

What it is for: Grok names the directory that holds the sessions of a
working directory after the path, and when that name would be too long it
uses the first 16 hex digits of the path's BLAKE3 instead (docs/FORMATS.md
"Phase 2 findings", Q1). An import has to compute the same name, the
standard library has no BLAKE3, and the engine uses nothing else.

What it is: the unkeyed hash with the default 32-byte output, for an input
of up to 1,024 bytes (``MAX_INPUT``). That is one chunk of the algorithm, so
the tree that joins chunks is not here; a longer input raises ``ValueError``
and is never hashed wrongly. The digests are those of the BLAKE3 team's
published test vectors for every input length they list up to 1,024
(tests/test_blake3.py).

What it is not: the keyed hash, key derivation, extended output, a streaming
interface, fast, or constant in time. Do not use it for anything secret, or
for anything but a short path.

Standard library only.
"""

from __future__ import annotations

import struct

MAX_INPUT = 1024  # bytes: one chunk
DIGEST_SIZE = 32

_BLOCK = 64
_IV = (
    0x6A09E667,
    0xBB67AE85,
    0x3C6EF372,
    0xA54FF53A,
    0x510E527F,
    0x9B05688C,
    0x1F83D9AB,
    0x5BE0CD19,
)
# How the message words are reordered after each round.
_PERMUTATION = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)
_ROUNDS = 7
_CHUNK_START, _CHUNK_END, _ROOT = 1, 2, 8  # flags of a compression
_MASK = 0xFFFFFFFF


def _rotr(value: int, count: int) -> int:
    return ((value >> count) | (value << (32 - count))) & _MASK


def _g(state: list[int], a: int, b: int, c: int, d: int, mx: int, my: int) -> None:
    """The quarter round, on four words of the state and two of the message."""
    state[a] = (state[a] + state[b] + mx) & _MASK
    state[d] = _rotr(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & _MASK
    state[b] = _rotr(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b] + my) & _MASK
    state[d] = _rotr(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & _MASK
    state[b] = _rotr(state[b] ^ state[c], 7)


def _compress(chaining: list[int], block: bytes, length: int, flags: int) -> list[int]:
    """One block into the chaining value; returns the next one. ``block`` is
    64 bytes, zero-filled after its ``length``. The chunk counter is 0: there
    is one chunk."""
    words = list(struct.unpack("<16I", block))
    state = [*chaining, *_IV[:4], 0, 0, length, flags]
    for _round in range(_ROUNDS):
        _g(state, 0, 4, 8, 12, words[0], words[1])  # the columns
        _g(state, 1, 5, 9, 13, words[2], words[3])
        _g(state, 2, 6, 10, 14, words[4], words[5])
        _g(state, 3, 7, 11, 15, words[6], words[7])
        _g(state, 0, 5, 10, 15, words[8], words[9])  # the diagonals
        _g(state, 1, 6, 11, 12, words[10], words[11])
        _g(state, 2, 7, 8, 13, words[12], words[13])
        _g(state, 3, 4, 9, 14, words[14], words[15])
        words = [words[index] for index in _PERMUTATION]
    return [state[index] ^ state[index + 8] for index in range(8)]


def digest(data: bytes) -> bytes:
    """The 32-byte BLAKE3 hash of ``data``. Raises ``ValueError`` for more
    than ``MAX_INPUT`` bytes and ``TypeError`` for anything but bytes."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("blake3.digest takes bytes")
    data = bytes(data)
    if len(data) > MAX_INPUT:
        raise ValueError(
            f"{len(data)} bytes is more than the {MAX_INPUT} this BLAKE3 hashes (one chunk)"
        )
    # An empty input is one empty block.
    blocks = [data[start : start + _BLOCK] for start in range(0, len(data), _BLOCK)] or [b""]
    chaining = list(_IV)
    for number, block in enumerate(blocks):
        flags = _CHUNK_START if number == 0 else 0
        if number == len(blocks) - 1:
            flags |= _CHUNK_END | _ROOT
        chaining = _compress(chaining, block.ljust(_BLOCK, b"\0"), len(block), flags)
    return struct.pack("<8I", *chaining)


def hexdigest(data: bytes) -> str:
    """``digest`` as 64 lower-case hex digits."""
    return digest(data).hex()


__all__ = ["DIGEST_SIZE", "MAX_INPUT", "digest", "hexdigest"]
