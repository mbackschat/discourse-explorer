"""Tests for `sample.seed.generators.topics`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that
matter — determinism, count, set-membership of categories/tags/authors,
timeline alignment, cluster-combo anchoring, no unfilled template slots,
and one canonical title snapshot — and skip the rest.
"""

from __future__ import annotations

import unittest

from sample.seed.generators.categories import generate_categories
from sample.seed.generators.tags import generate_tags
from sample.seed.generators.timeline import make_timeline
from sample.seed.generators.topics import Topic, generate_topics
from sample.seed.generators.users import generate_users
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int, scale: str = "tiny") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


def _bake(seed: int, scale: str = "tiny"):
    """Run all upstream generators and return everything `generate_topics` needs."""
    spec = _spec(seed, scale=scale)
    cats = generate_categories(spec)
    tgs = generate_tags(spec)
    users = generate_users(spec)
    tl = make_timeline(spec, total_topics=spec.scale_targets()["topics"])
    return spec, cats, tgs, users, tl


class TopicsTests(unittest.TestCase):
    def test_determinism(self) -> None:
        """Same inputs -> identical `list[Topic]` (full dataclass equality)."""
        spec, cats, tgs, users, tl = _bake(42)
        a = generate_topics(spec, cats, tgs, users, tl)
        b = generate_topics(spec, cats, tgs, users, tl)
        self.assertEqual(a, b)

    def test_count_matches_scale(self) -> None:
        """Length equals `spec.scale_targets()["topics"]` for every scale."""
        for scale in ("tiny", "small", "medium"):
            with self.subTest(scale=scale):
                spec, cats, tgs, users, tl = _bake(42, scale=scale)
                topics = generate_topics(spec, cats, tgs, users, tl)
                self.assertEqual(
                    len(topics), spec.scale_targets()["topics"]
                )

    def test_categories_valid(self) -> None:
        """Every assigned category is in the input categories list."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        cats_set = set(cats)
        for t in topics:
            with self.subTest(topic_id=t.id):
                self.assertIn(t.category, cats_set)

    def test_tags_valid(self) -> None:
        """Every tag in every topic is in the input tag list."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        tags_set = set(tgs)
        for t in topics:
            for tag in t.tags:
                with self.subTest(topic_id=t.id, tag=tag):
                    self.assertIn(tag, tags_set)

    def test_authors_valid(self) -> None:
        """Every author_username matches a user in the input users list."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        usernames = {u.username for u in users}
        for t in topics:
            with self.subTest(topic_id=t.id):
                self.assertIn(t.author_username, usernames)

    def test_timestamps_respect_timeline(self) -> None:
        """`topic.created_at == timeline.timestamp_for_topic(idx)` for every idx."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        for idx, t in enumerate(topics):
            with self.subTest(idx=idx):
                self.assertEqual(t.created_at, tl.timestamp_for_topic(idx))

    def test_cluster_combos_anchored(self) -> None:
        """Every CLUSTER_TAG_COMBINATIONS entry is a subset of some topic's tags."""
        spec, cats, tgs, users, tl = _bake(42)
        topics = generate_topics(spec, cats, tgs, users, tl)
        all_tag_sets = [set(t.tags) for t in topics]
        for combo in crown_of_brine.CLUSTER_TAG_COMBINATIONS:
            with self.subTest(combo=combo):
                self.assertTrue(
                    any(set(combo) <= ts for ts in all_tag_sets),
                    f"cluster combo {combo} missing from any topic's tag set",
                )

    def test_topics_zero_title_snapshot(self) -> None:
        """Canonical title pin for `seed=42, scale='tiny'`.

        snapshot — bump intentionally if title generation changes.
        """
        spec, cats, tgs, users, tl = _bake(42)
        topics = generate_topics(spec, cats, tgs, users, tl)
        self.assertEqual(
            topics[0].title,
            "Crown of Brine: Reborn crashes on Android during the lighthouse arc",
        )

    def test_no_unfilled_slots(self) -> None:
        """No `{...}` survives in any title — catches templates that name an absent vocab key."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        for t in topics:
            with self.subTest(topic_id=t.id):
                self.assertNotIn("{", t.title)
                self.assertNotIn("}", t.title)

    def test_seed_varies(self) -> None:
        """Same upstream inputs except seed -> at least one topic title differs."""
        spec_a, cats_a, tgs_a, users_a, tl_a = _bake(42)
        spec_b, cats_b, tgs_b, users_b, tl_b = _bake(99)
        a = generate_topics(spec_a, cats_a, tgs_a, users_a, tl_a)
        b = generate_topics(spec_b, cats_b, tgs_b, users_b, tl_b)
        # Compare title-by-title: at minimum one must differ. We compare
        # over the same length window (both runs use the tiny scale = 30).
        self.assertEqual(len(a), len(b))
        differs = any(x.title != y.title for x, y in zip(a, b))
        self.assertTrue(
            differs,
            "expected seeds 42 and 99 to produce at least one differing title",
        )

    def test_topic_ids_sequential(self) -> None:
        """IDs are 1, 2, 3, … with no gaps and no duplicates."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        self.assertEqual([t.id for t in topics], list(range(1, len(topics) + 1)))

    def test_topics_chronological(self) -> None:
        """Topic timestamps are sorted ascending (timeline guarantee)."""
        spec, cats, tgs, users, tl = _bake(42, scale="small")
        topics = generate_topics(spec, cats, tgs, users, tl)
        timestamps = [t.created_at for t in topics]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_topic_dataclass_is_frozen(self) -> None:
        """`Topic` is frozen — guards against accidental mutation downstream."""
        spec, cats, tgs, users, tl = _bake(42)
        topics = generate_topics(spec, cats, tgs, users, tl)
        with self.assertRaises(Exception):
            topics[0].title = "mutated"  # type: ignore[misc]

    def test_returns_topic_instances(self) -> None:
        """Output is a list of `Topic` dataclass instances."""
        spec, cats, tgs, users, tl = _bake(42)
        topics = generate_topics(spec, cats, tgs, users, tl)
        self.assertTrue(all(isinstance(t, Topic) for t in topics))

    def test_invalid_category_raises(self) -> None:
        """Passing a category not in CATEGORY_POOL raises `ValueError`."""
        spec, cats, tgs, users, tl = _bake(42)
        with self.assertRaises(ValueError):
            generate_topics(
                spec, cats + ["NotARealCategory"], tgs, users, tl
            )

    def test_invalid_tag_raises(self) -> None:
        """Passing a tag not in TAG_POOL_BY_AXIS raises `ValueError`."""
        spec, cats, tgs, users, tl = _bake(42)
        with self.assertRaises(ValueError):
            generate_topics(spec, cats, tgs + ["not-a-real-tag"], users, tl)

    def test_empty_users_raises(self) -> None:
        """An empty user list raises `ValueError`."""
        spec, cats, tgs, _users, tl = _bake(42)
        with self.assertRaises(ValueError):
            generate_topics(spec, cats, tgs, [], tl)

    def test_undersized_timeline_raises(self) -> None:
        """A timeline shorter than the scale target raises `ValueError`."""
        spec, cats, tgs, users, _tl = _bake(42)
        # Build a timeline sized for fewer topics than the spec needs.
        small_tl = make_timeline(spec, total_topics=5)
        with self.assertRaises(ValueError):
            generate_topics(spec, cats, tgs, users, small_tl)


if __name__ == "__main__":
    unittest.main()
