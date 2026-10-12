"""blake3.py: the one-chunk BLAKE3 the Grok import names long group
directories with (docs/FORMATS.md "Phase 2 findings", Q1)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: F401  (puts src on the path)

from agent_sessions import blake3

# The BLAKE3 team's own test vectors:
#   https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/master/test_vectors/test_vectors.json
# (fetched 2026-10-13, 31,922 bytes, SHA-256 dcb91ea8…db32f624). The input of
# a case is ``input_len`` bytes of the repeating pattern 0, 1, …, 250, that is
# byte ``i % 251``; the value here is the first 32 bytes (64 hex digits) of
# its ``hash`` field, which the file gives as extended output. Every input
# length the file lists up to 1,024 is here; its next one is 1,025.
OFFICIAL = (
    (0, "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"),
    (1, "2d3adedff11b61f14c886e35afa036736dcd87a74d27b5c1510225d0f592e213"),
    (2, "7b7015bb92cf0b318037702a6cdd81dee41224f734684c2c122cd6359cb1ee63"),
    (3, "e1be4d7a8ab5560aa4199eea339849ba8e293d55ca0a81006726d184519e647f"),
    (4, "f30f5ab28fe047904037f77b6da4fea1e27241c5d132638d8bedce9d40494f32"),
    (5, "b40b44dfd97e7a84a996a91af8b85188c66c126940ba7aad2e7ae6b385402aa2"),
    (6, "06c4e8ffb6872fad96f9aaca5eee1553eb62aed0ad7198cef42e87f6a616c844"),
    (7, "3f8770f387faad08faa9d8414e9f449ac68e6ff0417f673f602a646a891419fe"),
    (8, "2351207d04fc16ade43ccab08600939c7c1fa70a5c0aaca76063d04c3228eaeb"),
    (63, "e9bc37a594daad83be9470df7f7b3798297c3d834ce80ba85d6e207627b7db7b"),
    (64, "4eed7141ea4a5cd4b788606bd23f46e212af9cacebacdc7d1f4c6dc7f2511b98"),
    (65, "de1e5fa0be70df6d2be8fffd0e99ceaa8eb6e8c93a63f2d8d1c30ecb6b263dee"),
    (127, "d81293fda863f008c09e92fc382a81f5a0b4a1251cba1634016a0f86a6bd640d"),
    (128, "f17e570564b26578c33bb7f44643f539624b05df1a76c81f30acd548c44b45ef"),
    (129, "683aaae9f3c5ba37eaaf072aed0f9e30bac0865137bae68b1fde4ca2aebdcb12"),
    (1023, "10108970eeda3eb932baac1428c7a2163b0e924c9a9e25b35bba72b28f70bd11"),
    (1024, "42214739f095a406f3fc83deb889744ac00df831c10daa55189b5d121c855af7"),
)

# Seen on this machine, outside any test of ours: Grok names its memory
# folders ``<slug>-<first 8 hex of BLAKE3(path)>`` (FORMATS Q1), the hash
# over the path exactly as recorded.
OBSERVED = (
    ("/home/tanuj", "db4bfc44"),  # ~/.grok/memory-v2/workspaces/tanuj-db4bfc44
    ("/home/tanuj/Work", "6b15ba01"),  # …/work-6b15ba01
    ("F:\\AI Projects\\Website Works", "32f235e1"),  # Windows: memory/website-works-32f235e1
)


def pattern(length: int) -> bytes:
    return bytes(index % 251 for index in range(length))


class OfficialVectorsTest(unittest.TestCase):
    def test_every_listed_input_length_up_to_one_chunk(self):
        self.assertEqual(
            [length for length, _ in OFFICIAL],
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 63, 64, 65, 127, 128, 129, 1023, 1024],
        )
        for length, expected in OFFICIAL:
            with self.subTest(length=length):
                self.assertEqual(blake3.hexdigest(pattern(length)), expected)
                self.assertEqual(blake3.digest(pattern(length)), bytes.fromhex(expected))

    def test_the_digest_is_32_bytes(self):
        for length in (0, 1, 64, 1024):
            self.assertEqual(len(blake3.digest(pattern(length))), blake3.DIGEST_SIZE)
        self.assertEqual(blake3.DIGEST_SIZE, 32)

    def test_bytes_like_input(self):
        expected = blake3.hexdigest(pattern(65))
        self.assertEqual(blake3.hexdigest(bytearray(pattern(65))), expected)
        self.assertEqual(blake3.hexdigest(memoryview(pattern(65))), expected)


class LimitsTest(unittest.TestCase):
    def test_more_than_one_chunk_is_refused_not_hashed_wrongly(self):
        self.assertEqual(blake3.MAX_INPUT, 1024)
        for length in (1025, 2048, 100_000):
            with self.assertRaises(ValueError) as caught:
                blake3.digest(pattern(length))
            self.assertIn("more than the 1024", str(caught.exception))

    def test_text_is_not_bytes(self):
        for value in ("/home/user", None, 7, ["a"]):
            with self.assertRaises(TypeError):
                blake3.digest(value)


class ObservedNamesTest(unittest.TestCase):
    def test_the_three_folder_names_grok_made_on_this_machine(self):
        for path, first8 in OBSERVED:
            with self.subTest(path=path):
                self.assertEqual(blake3.hexdigest(path.encode("utf-8"))[:8], first8)

    def test_the_hash_is_over_the_exact_bytes(self):
        base = blake3.hexdigest(b"/home/tanuj")
        for other in (b"/home/tanuj/", b"/home/Tanuj", b"/home/tanuj\n"):
            self.assertNotEqual(blake3.hexdigest(other), base)


if __name__ == "__main__":
    unittest.main()
