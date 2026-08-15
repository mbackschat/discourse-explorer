"""Tests for `sample.seed.content.blocklist`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that matter
— the loader is non-empty + idempotent, the matcher catches a known
positive case + ignores a known negative case, and every actually-generated
artefact (usernames, titles, categories, tag names) survives the filter.
"""

from __future__ import annotations

import unittest

from sample.seed.content.blocklist import check, load_blocklist
from sample.seed.generators.categories import generate_categories
from sample.seed.generators.tags import generate_tags
from sample.seed.generators.timeline import make_timeline
from sample.seed.generators.topics import generate_topics
from sample.seed.generators.users import generate_users
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int = 42, scale: str = "medium") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


class LoadBlocklistTests(unittest.TestCase):
    def test_at_least_100_entries(self) -> None:
        """The hand-curated list has ≥100 entries (sanity floor)."""
        self.assertGreaterEqual(
            len(load_blocklist()),
            100,
            "blocklist.txt looks too small — re-check the curation",
        )

    def test_idempotent(self) -> None:
        """Two calls return the same (cached) frozenset object."""
        self.assertIs(load_blocklist(), load_blocklist())

    def test_entries_are_lowercased(self) -> None:
        """Every entry is lowercase — matching is case-insensitive at compile."""
        for entry in load_blocklist():
            with self.subTest(entry=entry):
                self.assertEqual(entry, entry.lower())


class CheckTests(unittest.TestCase):
    def test_positive_case_monkey_island(self) -> None:
        """`Monkey Island` is a canonical hit — case-insensitive multi-word."""
        self.assertEqual(check("Monkey Island spoiler"), ["monkey island"])

    def test_negative_case_universe_phrase(self) -> None:
        """A universe-allowed phrase has zero hits."""
        self.assertEqual(check("the cartographer's riddle"), [])

    def test_empty_input(self) -> None:
        """An empty body has no hits."""
        self.assertEqual(check(""), [])

    def test_word_boundary_substring_safe(self) -> None:
        """A substring inside another word doesn't count as a hit.

        `Mario` is on the list. The word `marionette` contains those letters
        but isn't the franchise — `\\b` boundaries should keep it from
        firing. This guards against the regex collapsing into substring
        match if someone later "simplifies" the pattern.
        """
        self.assertEqual(check("a marionette show"), [])
        # Sanity: the bare word still hits.
        self.assertIn("mario", check("Mario Kart"))


class GeneratedArtefactsClearBlocklistTests(unittest.TestCase):
    """Every generator output must clear the blocklist post-filter.

    These tests are the real value of Sit 7: they assert the design-doc
    invariant ("no real names anywhere") empirically against the actually-
    generated forum at `seed=42, scale="medium"`. If a future product-
    constant edit slips a real name in, one of these tests fires.
    """

    @classmethod
    def setUpClass(cls) -> None:
        spec = _spec()
        cls.spec = spec
        cls.categories = generate_categories(spec)
        cls.tags = generate_tags(spec)
        cls.users = generate_users(spec)
        cls.timeline = make_timeline(
            spec, total_topics=spec.scale_targets()["topics"]
        )
        cls.topics = generate_topics(
            spec, cls.categories, cls.tags, cls.users, cls.timeline
        )

    def test_all_usernames_clear(self) -> None:
        for u in self.users:
            with self.subTest(username=u.username):
                self.assertEqual(check(u.username), [])
                self.assertEqual(check(u.display_name), [])

    def test_all_topic_titles_clear(self) -> None:
        for t in self.topics:
            with self.subTest(title=t.title):
                self.assertEqual(check(t.title), [])

    def test_all_category_names_clear(self) -> None:
        for cat in self.categories:
            with self.subTest(category=cat):
                self.assertEqual(check(cat), [])

    def test_all_tag_names_clear(self) -> None:
        # Tag names are kebab-case slugs (`tide-engine`, `voice-acting`),
        # not full English. Still must clear the blocklist — if a future
        # tag drift introduces e.g. `nintendo-port`, this test catches it.
        for tag in self.tags:
            with self.subTest(tag=tag):
                self.assertEqual(check(tag), [])


if __name__ == "__main__":
    unittest.main()
