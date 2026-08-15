"""Tests for `sample.seed.generators.posts`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that
matter — determinism, OP presence, count plausibility, solved/unanswered/
cross-reference rates within slack-y bands, monotonic timestamps,
cluster-anchor floor, author validity, and at-most-one-solution-per-topic —
and skip the rest. Stochastic rates use `medium` scale where the law of
large numbers actually has a chance.
"""

from __future__ import annotations

import unittest
from collections import defaultdict

from sample.seed.generators.categories import generate_categories
from sample.seed.generators.posts import Post, generate_posts
from sample.seed.generators.tags import generate_tags
from sample.seed.generators.timeline import make_timeline
from sample.seed.generators.topics import generate_topics
from sample.seed.generators.users import generate_users
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int, scale: str = "tiny") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


def _bake(seed: int, scale: str = "tiny"):
    """Run all upstream generators and return what `generate_posts` needs."""
    spec = _spec(seed, scale=scale)
    cats = generate_categories(spec)
    tgs = generate_tags(spec)
    users = generate_users(spec)
    tl = make_timeline(spec, total_topics=spec.scale_targets()["topics"])
    topics = generate_topics(spec, cats, tgs, users, tl)
    return spec, cats, tgs, users, tl, topics


class PostsTests(unittest.TestCase):
    def test_determinism(self) -> None:
        """Same inputs -> identical `list[Post]` (full dataclass equality)."""
        spec, _cats, _tgs, users, tl, topics = _bake(42)
        a = generate_posts(spec, topics, users, tl)
        b = generate_posts(spec, topics, users, tl)
        self.assertEqual(a, b)

    def test_every_topic_has_op(self) -> None:
        """Each topic has exactly one post with post_number == 1 + parent None."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="small")
        posts = generate_posts(spec, topics, users, tl)
        ops_by_topic: dict[int, list[Post]] = defaultdict(list)
        for p in posts:
            if p.post_number == 1:
                ops_by_topic[p.topic_id].append(p)
        topic_ids = {t.id for t in topics}
        self.assertEqual(set(ops_by_topic), topic_ids)
        for tid, ops in ops_by_topic.items():
            with self.subTest(topic_id=tid):
                self.assertEqual(len(ops), 1)
                self.assertIsNone(ops[0].parent_post_id)

    def test_count_plausible(self) -> None:
        """Total posts in [scale_target * 0.6, scale_target * 1.4] (Poisson noise)."""
        for scale in ("small", "medium"):
            with self.subTest(scale=scale):
                spec, _cats, _tgs, users, tl, topics = _bake(42, scale=scale)
                posts = generate_posts(spec, topics, users, tl)
                target = spec.scale_targets()["posts"]
                self.assertGreaterEqual(
                    len(posts),
                    int(target * 0.6),
                    f"too few posts: {len(posts)} vs target {target}",
                )
                self.assertLessEqual(
                    len(posts),
                    int(target * 1.4),
                    f"too many posts: {len(posts)} vs target {target}",
                )

    def test_solved_rate_in_band(self) -> None:
        """Help & Hints solved fraction in [0.05, 0.30] at medium scale."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="medium")
        posts = generate_posts(spec, topics, users, tl)
        # Help & Hints topics with at least one reply.
        help_topic_ids = {t.id for t in topics if t.category == "Help & Hints"}
        replies_by_topic: dict[int, int] = defaultdict(int)
        solutions_by_topic: dict[int, int] = defaultdict(int)
        for p in posts:
            if p.topic_id not in help_topic_ids:
                continue
            if p.post_number >= 2:
                replies_by_topic[p.topic_id] += 1
            if p.is_accepted_solution:
                solutions_by_topic[p.topic_id] += 1
        # Topics with at least one reply form the denominator (the rule only
        # fires on topics that have something to mark).
        eligible = {tid for tid in help_topic_ids if replies_by_topic[tid] >= 1}
        self.assertGreater(
            len(eligible),
            0,
            "no eligible Help & Hints topics with replies; can't measure rate",
        )
        solved = sum(1 for tid in eligible if solutions_by_topic[tid] >= 1)
        rate = solved / len(eligible)
        self.assertGreaterEqual(
            rate, 0.05, f"solved rate {rate:.2%} below 5%"
        )
        self.assertLessEqual(
            rate, 0.30, f"solved rate {rate:.2%} above 30%"
        )

    def test_unanswered_rate_in_band(self) -> None:
        """Fraction of topics with exactly 1 post in [0.05, 0.20] at medium."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="medium")
        posts = generate_posts(spec, topics, users, tl)
        post_count_by_topic: dict[int, int] = defaultdict(int)
        for p in posts:
            post_count_by_topic[p.topic_id] += 1
        unanswered = sum(
            1 for t in topics if post_count_by_topic[t.id] == 1
        )
        rate = unanswered / len(topics)
        self.assertGreaterEqual(
            rate, 0.05, f"unanswered rate {rate:.2%} below 5%"
        )
        self.assertLessEqual(
            rate, 0.20, f"unanswered rate {rate:.2%} above 20%"
        )

    def test_cross_reference_rate_in_band(self) -> None:
        """Fraction of replies with non-None quote_target_id in [0.10, 0.30]."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="medium")
        posts = generate_posts(spec, topics, users, tl)
        replies = [p for p in posts if p.post_number >= 2]
        self.assertGreater(len(replies), 0, "no replies generated; can't measure")
        with_quote = sum(1 for p in replies if p.quote_target_id is not None)
        rate = with_quote / len(replies)
        self.assertGreaterEqual(
            rate, 0.10, f"cross-ref rate {rate:.2%} below 10%"
        )
        self.assertLessEqual(
            rate, 0.30, f"cross-ref rate {rate:.2%} above 30%"
        )

    def test_child_timestamps_post_date_parents(self) -> None:
        """Every reply's `created_at` strictly exceeds its parent's `created_at`."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="small")
        posts = generate_posts(spec, topics, users, tl)
        by_id = {p.id: p for p in posts}
        for p in posts:
            if p.parent_post_id is None:
                continue
            parent = by_id[p.parent_post_id]
            with self.subTest(post_id=p.id):
                self.assertGreater(p.created_at, parent.created_at)

    def test_all_authors_valid(self) -> None:
        """Every `author_username` matches a user in the input list."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="small")
        posts = generate_posts(spec, topics, users, tl)
        usernames = {u.username for u in users}
        for p in posts:
            with self.subTest(post_id=p.id):
                self.assertIn(p.author_username, usernames)

    def test_at_most_one_solution_per_topic(self) -> None:
        """No topic has 2+ posts with `is_accepted_solution=True`."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="medium")
        posts = generate_posts(spec, topics, users, tl)
        solutions_by_topic: dict[int, int] = defaultdict(int)
        for p in posts:
            if p.is_accepted_solution:
                solutions_by_topic[p.topic_id] += 1
        for tid, count in solutions_by_topic.items():
            with self.subTest(topic_id=tid):
                self.assertLessEqual(count, 1)

    def test_cluster_anchors_have_minimum_replies(self) -> None:
        """First N topics (cluster anchors) each have >=4 posts (1 OP + 3 replies)."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="tiny")
        posts = generate_posts(spec, topics, users, tl)
        post_count_by_topic: dict[int, int] = defaultdict(int)
        for p in posts:
            post_count_by_topic[p.topic_id] += 1
        anchor_count = len(crown_of_brine.CLUSTER_TAG_COMBINATIONS)
        for t in topics[:anchor_count]:
            with self.subTest(topic_id=t.id):
                self.assertGreaterEqual(
                    post_count_by_topic[t.id],
                    4,
                    f"cluster-anchor topic {t.id} has "
                    f"{post_count_by_topic[t.id]} posts; expected >=4",
                )

    def test_op_authors_match_topic_authors(self) -> None:
        """OP author == topic.author_username."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="small")
        posts = generate_posts(spec, topics, users, tl)
        topic_author_by_id = {t.id: t.author_username for t in topics}
        for p in posts:
            if p.post_number != 1:
                continue
            with self.subTest(topic_id=p.topic_id):
                self.assertEqual(
                    p.author_username, topic_author_by_id[p.topic_id]
                )

    def test_post_ids_unique_and_sequential(self) -> None:
        """Post IDs are 1..N with no gaps and no duplicates."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="small")
        posts = generate_posts(spec, topics, users, tl)
        ids = [p.id for p in posts]
        self.assertEqual(ids, list(range(1, len(posts) + 1)))

    def test_post_numbers_per_topic_sequential(self) -> None:
        """Within a topic, post_number is 1, 2, 3, … with no gaps."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="small")
        posts = generate_posts(spec, topics, users, tl)
        nums_by_topic: dict[int, list[int]] = defaultdict(list)
        for p in posts:
            nums_by_topic[p.topic_id].append(p.post_number)
        for tid, nums in nums_by_topic.items():
            with self.subTest(topic_id=tid):
                self.assertEqual(nums, list(range(1, len(nums) + 1)))

    def test_op_never_has_solution_marker(self) -> None:
        """Solution marker only ever lands on a reply, never the OP."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="medium")
        posts = generate_posts(spec, topics, users, tl)
        for p in posts:
            if p.post_number == 1:
                with self.subTest(topic_id=p.topic_id):
                    self.assertFalse(p.is_accepted_solution)

    def test_solution_only_in_help_and_hints(self) -> None:
        """No accepted-solution outside the Help & Hints category."""
        spec, _cats, _tgs, users, tl, topics = _bake(42, scale="medium")
        posts = generate_posts(spec, topics, users, tl)
        cat_by_topic = {t.id: t.category for t in topics}
        for p in posts:
            if p.is_accepted_solution:
                with self.subTest(post_id=p.id):
                    self.assertEqual(
                        cat_by_topic[p.topic_id], "Help & Hints"
                    )

    def test_returns_post_instances(self) -> None:
        """Output is a list of `Post` dataclass instances."""
        spec, _cats, _tgs, users, tl, topics = _bake(42)
        posts = generate_posts(spec, topics, users, tl)
        self.assertTrue(all(isinstance(p, Post) for p in posts))

    def test_post_dataclass_is_frozen(self) -> None:
        """`Post` is frozen — guards against accidental mutation downstream."""
        spec, _cats, _tgs, users, tl, topics = _bake(42)
        posts = generate_posts(spec, topics, users, tl)
        with self.assertRaises(Exception):
            posts[0].body = "mutated"  # type: ignore[misc]

    def test_empty_topics_raises(self) -> None:
        """An empty topics list raises `ValueError`."""
        spec, _cats, _tgs, users, tl, _topics = _bake(42)
        with self.assertRaises(ValueError):
            generate_posts(spec, [], users, tl)

    def test_empty_users_raises(self) -> None:
        """An empty users list raises `ValueError`."""
        spec, _cats, _tgs, _users, tl, topics = _bake(42)
        with self.assertRaises(ValueError):
            generate_posts(spec, topics, [], tl)

    def test_seed_varies(self) -> None:
        """Different seeds yield a different total post count or post 0 author."""
        spec_a, _ca, _ta, ua, tl_a, topics_a = _bake(42)
        spec_b, _cb, _tb, ub, tl_b, topics_b = _bake(99)
        a = generate_posts(spec_a, topics_a, ua, tl_a)
        b = generate_posts(spec_b, topics_b, ub, tl_b)
        # Different seeds should produce visibly different bakes — either
        # total length or the first reply's author differs.
        differs = (len(a) != len(b)) or any(
            x.author_username != y.author_username
            for x, y in zip(a, b)
            if x.post_number >= 2
        )
        self.assertTrue(
            differs,
            "expected seeds 42 and 99 to produce visibly different post lists",
        )


if __name__ == "__main__":
    unittest.main()
