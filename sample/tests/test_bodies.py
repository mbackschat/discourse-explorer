"""Tests for `sample.seed.content.bodies.generate_body`.

Sit 10. All scenarios use mock LLMs — no live provider dependency, so the
suite stays fast and offline. Covers:

* Cache hit short-circuits the LLM (mock raises if called).
* Cache miss writes through (cache populated, body returned).
* Blocklist hit triggers retry with a stronger system note; retry success
  caches the second response.
* Blocklist hard-fail raises `BlocklistViolation` after exhausting retries;
  cache stays empty.
* LLM unavailable (raise) → deterministic template fallback, NOT cached.
* Empty/whitespace LLM response → template fallback, NOT cached.

Mock LLM convention: a tiny inline class that records every prompt it sees
and returns scripted responses in order. Easier to read than `MagicMock`
chained side_effect lists when there's an explicit retry-prompt assertion.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from sample.seed.content import bodies as bodies_module
from sample.seed.content.bodies import (
    BlocklistViolation,
    build_prompt,
    generate_body,
)
from sample.seed.content.cache import Cache


# --- Test doubles ---------------------------------------------------------


@dataclass
class _StubTopic:
    """Minimal stand-in for `generators.topics.Topic`.

    `generate_body` only reads `id`, `title`, `category`, and `tags` — so
    skip the full dataclass with its frozen + datetime fields and use a
    lightweight stub.
    """

    id: int
    title: str
    category: str
    tags: list[str]


@dataclass
class _StubPost:
    """Minimal stand-in for `generators.posts.Post`."""

    topic_id: int
    post_number: int


class _ScriptedLLM:
    """Returns scripted strings in order; records every prompt it received.

    `responses` may contain strings (returned verbatim) or `Exception`
    instances (raised). Once exhausted, raises `IndexError` so a test that
    over-calls fails loudly rather than silently looping.
    """

    def __init__(self, responses: Iterable):
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        if not self._responses:
            raise IndexError(
                f"_ScriptedLLM exhausted after {self.calls} call(s); "
                f"test asked for one too many."
            )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _ExplodingLLM:
    """Raises if `generate` is ever called — proves the LLM was untouched."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        raise AssertionError(
            "LLM.generate was called but the test expected a cache hit."
        )


class _StubBlocklist:
    """Substitute for the module-level `blocklist` argument.

    `hits_for(text)` is the response oracle: tests register what `check`
    should return for given inputs. Default = `[]` (no hits).
    """

    def __init__(self, oracle: Optional[dict[str, list[str]]] = None) -> None:
        self.oracle = dict(oracle or {})
        self.calls: list[str] = []

    def check(self, text: str) -> list[str]:
        self.calls.append(text)
        return list(self.oracle.get(text, []))


# --- Fixtures -------------------------------------------------------------


def _make_topic_post(post_number: int = 1):
    topic = _StubTopic(
        id=42,
        title="The {item} chapter is unfair",
        category="Help & Hints",
        tags=["hint-needed", "ch3"],
    )
    post = _StubPost(topic_id=42, post_number=post_number)
    return topic, post


# --- Tests ----------------------------------------------------------------


class CacheHitTests(unittest.TestCase):
    """Cache hit must short-circuit the LLM entirely."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_hit_returns_cached_body_without_invoking_llm(self) -> None:
        topic, post = _make_topic_post(post_number=1)

        # Pre-populate the cache the same way generate_body would.
        cache = Cache(self.cache_path)
        cache.set(topic.id, post.post_number, "the cached OP body")

        llm = _ExplodingLLM()  # any call asserts
        blocklist = _StubBlocklist()

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        self.assertEqual(result, "the cached OP body")
        self.assertEqual(llm.calls, 0)
        # Blocklist isn't consulted on a hit either — the cached body was
        # already vetted when it was first written.
        self.assertEqual(blocklist.calls, [])


class CacheMissTests(unittest.TestCase):
    """Cache miss should call the LLM, vet the result, and persist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_miss_calls_llm_and_writes_through(self) -> None:
        topic, post = _make_topic_post(post_number=1)
        cache = Cache(self.cache_path)
        clean = "I was stuck on this for an hour too. Try the lever first."
        llm = _ScriptedLLM([clean])
        blocklist = _StubBlocklist()  # default = no hits for any text

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        self.assertEqual(result, clean)
        self.assertEqual(llm.calls, 1)
        # Write-through: a fresh Cache reading the same path finds the entry.
        reloaded = Cache(self.cache_path)
        self.assertEqual(reloaded.get(topic.id, post.post_number), clean)

    def test_miss_strips_whitespace_before_caching(self) -> None:
        topic, post = _make_topic_post(post_number=2)
        cache = Cache(self.cache_path)
        # Provider may return trailing newlines; the cached body shouldn't
        # carry them.
        llm = _ScriptedLLM(["   answer body\n\n"])
        blocklist = _StubBlocklist()

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        self.assertEqual(result, "answer body")
        self.assertEqual(cache.get(topic.id, post.post_number), "answer body")


class BlocklistRetryTests(unittest.TestCase):
    """First response trips the blocklist; second clean response wins."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_retry_then_success(self) -> None:
        topic, post = _make_topic_post(post_number=2)
        cache = Cache(self.cache_path)

        banned = "Mario was great"
        clean = "the cartographer was helpful"
        llm = _ScriptedLLM([banned, clean])
        # Oracle: only the banned string flags; the clean string is fine.
        blocklist = _StubBlocklist({banned: ["mario"]})

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        self.assertEqual(result, clean)
        self.assertEqual(llm.calls, 2)
        # Cache should hold the clean response — never the banned one.
        self.assertEqual(cache.get(topic.id, post.post_number), clean)
        # The retry prompt must contain a steer naming the offending term so
        # the model has a concrete avoidance target. (Documented contract.)
        retry_prompt = llm.prompts[1]
        self.assertIn("mario", retry_prompt.lower())


class BlocklistHardFailTests(unittest.TestCase):
    """LLM keeps surfacing banned content; generator gives up + raises."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_hard_fail_raises_after_retries_and_leaves_cache_empty(self) -> None:
        topic, post = _make_topic_post(post_number=2)
        cache = Cache(self.cache_path)
        banned = "Mario again"
        # 1 initial + 3 retries = 4 LLM calls; all four return the banned
        # string. The fourth still-banned response triggers the raise.
        llm = _ScriptedLLM([banned, banned, banned, banned])
        blocklist = _StubBlocklist({banned: ["mario"]})

        with self.assertRaises(BlocklistViolation) as cm:
            generate_body(
                topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
            )

        self.assertEqual(llm.calls, 4)
        self.assertEqual(cm.exception.attempts, 4)
        self.assertIn("mario", cm.exception.hits)
        # Cache must be untouched on hard-fail — the next run gets a clean
        # attempt without inheriting a poisoned cached value.
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.get(topic.id, post.post_number))


class LLMUnavailableTests(unittest.TestCase):
    """LLM raises → fall back to template body without caching."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_provider_error_returns_template_body_and_skips_cache(self) -> None:
        topic, post = _make_topic_post(post_number=2)
        cache = Cache(self.cache_path)
        llm = _ScriptedLLM([RuntimeError("provider down")])
        blocklist = _StubBlocklist()

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        # Non-empty template that references the topic title so a grep can
        # identify which posts fell back.
        self.assertTrue(result)
        self.assertIn(topic.title, result)
        self.assertIn("LLM unavailable", result)
        # Cache UNCHANGED so a future run with a working LLM will populate.
        self.assertEqual(len(cache), 0)

    def test_op_template_body_uses_topic_title_directly(self) -> None:
        topic, post = _make_topic_post(post_number=1)
        cache = Cache(self.cache_path)
        llm = _ScriptedLLM([RuntimeError("provider down")])
        blocklist = _StubBlocklist()

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        # OP template starts with the title (no `Re:` prefix).
        self.assertTrue(result.startswith(topic.title))

    def test_empty_response_falls_back_without_caching(self) -> None:
        # An LLM that returns whitespace counts as unavailable; it should
        # NOT be retried (the prompt isn't the issue) and should NOT cache.
        topic, post = _make_topic_post(post_number=2)
        cache = Cache(self.cache_path)
        llm = _ScriptedLLM(["   \n\t  "])
        blocklist = _StubBlocklist()

        result = generate_body(
            topic, post, rng=None, llm=llm, cache=cache, blocklist=blocklist
        )

        self.assertIn("LLM unavailable", result)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(cache), 0)


class PromptShapeTests(unittest.TestCase):
    """`build_prompt` is a pure deterministic function of its inputs."""

    def test_same_inputs_yield_identical_prompt(self) -> None:
        topic, post = _make_topic_post(post_number=1)
        a = build_prompt(topic, post)
        b = build_prompt(topic, post)
        self.assertEqual(a, b)

    def test_op_and_reply_prompts_differ(self) -> None:
        topic, op = _make_topic_post(post_number=1)
        _, reply = _make_topic_post(post_number=3)
        prompt_op = build_prompt(topic, op)
        prompt_reply = build_prompt(topic, reply, parent_body="prior body content")
        self.assertNotEqual(prompt_op, prompt_reply)
        # Reply prompt mentions the parent excerpt; OP prompt doesn't.
        self.assertIn("prior body content", prompt_reply)
        self.assertNotIn("prior body content", prompt_op)


class RealBlocklistIntegrationTests(unittest.TestCase):
    """One end-to-end pass with the real `blocklist` module wired in.

    Default-argument path: `generate_body` uses
    `sample.seed.content.blocklist` when no override is passed. This
    exercises that wiring once so a mistype in the import doesn't escape
    the mock-only happy paths above.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_real_blocklist_passes_clean_body(self) -> None:
        topic, post = _make_topic_post(post_number=1)
        cache = Cache(self.cache_path)
        # Body uses only fictional names from the topic context, no real
        # franchises — should pass the real blocklist on first attempt.
        clean = (
            "I tried the cartographer route last night. The lever in the "
            "third chapter is the trick — the puzzle clicks once you push it."
        )
        llm = _ScriptedLLM([clean])

        result = generate_body(topic, post, rng=None, llm=llm, cache=cache)

        self.assertEqual(result, clean)
        self.assertEqual(cache.get(topic.id, post.post_number), clean)


if __name__ == "__main__":
    unittest.main()
