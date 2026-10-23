"""search.py: the corpus that finds a session by what was said in it. What
it reads (and rereads, and leaves alone), what a search finds and in which
order, its bounds, and that a snippet is safe to show and keeps the match in
it."""

import sys
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: F401  (state, cache and home in temp dirs)

from agent_sessions import search, textsafe
from agent_sessions.search import Corpus, Progress


@dataclass
class S:
    key: str
    updated: str | None = "2026-10-01T00:00:00Z"
    messages: int | None = 3
    files: list = field(default_factory=list)
    offline: bool = False


def turns(*said):
    """``("user", "text", "2026-…")`` tuples, as ``transcript`` gives them."""
    return [tuple(turn) for turn in said]


class ReadTest(unittest.TestCase):
    def test_reads_every_session_once_newest_first_and_again_when_it_changed(self):
        old, new = S("a", updated="2026-10-01T00:00:00Z"), S("b", updated="2026-10-05T00:00:00Z")
        asked = []

        def transcript(key):
            asked.append(key)
            return turns(("user", f"text of {key}", None))

        corpus = Corpus()
        self.assertEqual(corpus.read([old, new], transcript), Progress(2, 2, 0, False))
        self.assertEqual(asked, ["b", "a"])
        self.assertEqual(corpus.read([old, new], transcript), Progress(0, 0, 0, False))
        new.messages = 4  # the session grew
        corpus.read([old, new], transcript)
        self.assertEqual(asked, ["b", "a", "b"])

    def test_an_offline_session_is_not_read_and_an_unreadable_one_counts_as_failed(self):
        def transcript(key):
            if key == "broken":
                raise OSError("gone")
            if key == "deep":
                raise RecursionError
            return turns(("user", "fine", None))

        corpus = Corpus()
        sessions = [S("ok"), S("broken"), S("deep"), S("lost", offline=True)]
        self.assertEqual(corpus.read(sessions, transcript), Progress(3, 3, 2, False))
        self.assertEqual(len(corpus), 1)
        self.assertIn("ok", corpus)

    def test_progress_after_every_session_and_stop_between_two(self):
        seen = []
        corpus = Corpus()
        sessions = [S(str(n), updated=f"2026-10-0{n}T00:00:00Z") for n in range(1, 6)]
        state = corpus.read(
            sessions,
            lambda key: turns(("user", key, None)),
            progress=seen.append,
            stop=lambda: len(seen) >= 2,
        )
        self.assertEqual(seen, [Progress(1, 5), Progress(2, 5)])
        self.assertEqual(state, Progress(2, 5))

    def test_tool_turns_are_not_kept_and_a_turn_is_cut_where_the_pager_cuts_it(self):
        corpus = Corpus(turn_chars=10)
        corpus.read(
            [S("a")],
            lambda key: turns(
                ("tool", "needle", None),
                ("user", "0123456789needle", None),
                ("assistant", None, None),
                ("assistant", 5, None),
                ("odd",),
            ),
        )
        self.assertEqual(corpus.search("needle").total, 0)
        self.assertEqual(corpus.search("0123").total, 1)

    def test_the_budget_stops_the_reading(self):
        corpus = Corpus(max_chars=10)
        sessions = [S("a", updated="2026-10-02T00:00:00Z"), S("b")]
        state = corpus.read(sessions, lambda key: turns(("user", "x" * 8, None)))
        self.assertTrue(state.full)
        self.assertEqual(state.done, 1)
        self.assertEqual(len(corpus), 1)
        self.assertEqual(corpus.chars, 8)

    def test_keep_forgets_the_sessions_that_are_gone(self):
        corpus = Corpus()
        corpus.read([S("a"), S("b")], lambda key: turns(("user", "hello", None)))
        corpus.keep({"b"})
        self.assertEqual((len(corpus), "a" in corpus), (1, False))


class SearchTest(unittest.TestCase):
    def corpus(self):
        corpus = Corpus()
        corpus.add(
            "a",
            (),
            turns(
                ("user", "How do I fix the Window Tags plugin?", "2026-10-01T10:00:00Z"),
                ("tool", "Read", None),
                ("assistant", "The window tags service\nreads a socket.", "2026-10-01T10:01:00Z"),
            ),
        )
        corpus.add("b", (), turns(("user", "window tags again", "2026-10-03T09:00:00Z")))
        corpus.add("c", (), turns(("user", "nothing here", "2026-10-04T09:00:00Z")))
        return corpus

    def test_finds_the_messages_newest_first_ignoring_case(self):
        found = self.corpus().search("  WINDOW tags ")
        self.assertEqual((found.total, found.sessions), (3, 2))
        self.assertEqual(
            [(hit.key, hit.turn) for hit in found.hits], [("b", 0), ("a", 2), ("a", 0)]
        )
        self.assertEqual(found.hits[2].match, "Window Tags")
        self.assertEqual(found.hits[2].role, "user")

    def test_too_short_a_needle_finds_nothing(self):
        self.assertEqual(self.corpus().search("w"), search.Found())
        self.assertEqual(self.corpus().search("   "), search.Found())

    def test_keys_limit_the_sessions_searched(self):
        found = self.corpus().search("window tags", keys={"a"})
        self.assertEqual({hit.key for hit in found.hits}, {"a"})

    def test_per_session_and_limit(self):
        corpus = Corpus()
        said = [("user", "word", f"2026-10-01T10:{n:02d}:00Z") for n in range(9)]
        corpus.add("a", (), turns(*said))
        corpus.add("b", (), turns(("user", "word", "2026-09-01T10:00:00Z")))
        found = corpus.search("word", per_session=3)
        self.assertEqual((found.total, found.sessions, len(found.hits)), (10, 2, 4))
        self.assertEqual([hit.turn for hit in found.hits[:3]], [8, 7, 6])
        self.assertEqual(len(corpus.search("word", limit=2).hits), 2)

    def test_stop_gives_up_with_what_was_found(self):
        found = self.corpus().search("window", stop=lambda: True)
        self.assertEqual(found.total, 0)

    def test_the_snippet_is_the_line_of_the_match(self):
        hit = self.corpus().search("socket").hits[0]
        self.assertEqual((hit.before, hit.match, hit.after), ("reads a ", "socket", "."))

    def test_a_long_line_is_cut_at_a_word_before_the_match_and_after_it(self):
        corpus = Corpus()
        text = " ".join(f"word{n}" for n in range(30)) + " NEEDLE " + "tail " * 100
        corpus.add("a", (), turns(("user", text, None)))
        hit = corpus.search("needle").hits[0]
        self.assertTrue(hit.before.startswith("…word"))
        self.assertLessEqual(len(hit.before), search.CONTEXT + 1)
        self.assertEqual(hit.match, "NEEDLE")
        self.assertTrue(hit.after.endswith("…"))
        self.assertLessEqual(len(hit.after), search.SNIPPET + 1)

    def test_a_snippet_is_safe_to_show(self):
        corpus = Corpus()
        corpus.add("a", (), turns(("user", "\x1b]0;PWNED\x07 evil ‮ needle \x9b31m", None)))
        hit = corpus.search("needle").hits[0]
        for part in (hit.before, hit.match, hit.after):
            self.assertEqual(textsafe.clean(part), part)

    def test_a_text_whose_length_lower_case_changes(self):
        corpus = Corpus()
        corpus.add("a", (), turns(("user", "İstanbul needle here", None)))
        hit = corpus.search("needle").hits[0]
        self.assertEqual((hit.match, hit.after), ("needle", " here"))
        self.assertEqual(hit.count, 1)

    def test_search_while_another_thread_reads(self):
        corpus = Corpus()
        sessions = [S(f"s{n}", updated=f"2026-10-01T00:{n % 60:02d}:00Z") for n in range(300)]
        done = threading.Event()

        def reader():
            corpus.read(sessions, lambda key: turns(("user", f"needle {key}", None)))
            done.set()

        thread = threading.Thread(target=reader)
        thread.start()
        while not done.is_set():
            corpus.search("needle")
        thread.join()
        self.assertEqual(corpus.search("needle").total, 300)


if __name__ == "__main__":
    unittest.main()
