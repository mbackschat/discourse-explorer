"""Tests for `pipeline.extend_forum` — Sits 15 + 16 + 17.

Pragmatic posture (sample-subtree TDD relaxed): cover the structural
invariants the `extend` flow must satisfy. These are the things that
break Discourse / break the parent scraper / break determinism if we
get them wrong:

- `len(new_topics) == add_topics_n` (Sit 15)
- new topic ids start AFTER `max(base.topics.id)`; new post ids start
  AFTER `max(base.posts.id)`
- every new post's `created_at` > every base post's `created_at` — the
  whole point of "extend": new content lives in the future, not
  retconned into the past
- new topics reference only base.categories / base.tags / base.users
- titles dedupe AGAINST base titles (Discourse rejects duplicates)
- determinism: same `(base_spec, add_topics_n, add_replies_n,
  release_burst_version, extend_seed)` -> bit-for-bit identical
  extension
- distinct `extend_seed` values produce distinct extensions
- Sit 16: appended replies attach to BASE topics only, post_number
  continues cleanly per-topic, multi-reply ordering on the same topic
  is monotonic
- Sit 17: release-burst cluster has ≥10 topics within a 7-day window;
  every burst topic carries the version tag plus one of
  `[bug, feature-request, hint-needed]`; the burst is self-contained
  (its replies attach to its own topics, not base topics).
"""

from __future__ import annotations

import dataclasses
import unittest

from sample.seed.forum import ForumExtension
from sample.seed.pipeline import build_forum, extend_forum
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _base_spec(seed: int = 42, scale: str = "tiny") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


class ExtendForumStructuralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _base_spec()
        cls.base = build_forum(cls.spec)
        cls.ext = extend_forum(
            cls.spec, add_topics_n=5, extend_seed=7
        )

    def test_extension_dataclass_shape(self) -> None:
        """`extend_forum` returns a `ForumExtension` echoing all base IDs."""
        self.assertIsInstance(self.ext, ForumExtension)
        self.assertEqual(self.ext.base_seed, 42)
        self.assertEqual(self.ext.base_scale, "tiny")
        # Default `base_product_name` resolution: falls back to the
        # imported product module's last segment (`crown_of_brine`,
        # underscored). The CLI passes the dashed slug explicitly via
        # `--product`; covered separately in `test_cli`.
        self.assertEqual(self.ext.base_product_name, "crown_of_brine")
        self.assertEqual(self.ext.extend_seed, 7)
        self.assertEqual(self.ext.add_topics_n, 5)
        self.assertEqual(self.ext.add_replies_n, 0)
        self.assertIsNone(self.ext.release_burst_version)

    def test_topic_count_matches_request(self) -> None:
        self.assertEqual(len(self.ext.new_topics), 5)

    def test_posts_include_one_op_per_topic_plus_replies(self) -> None:
        """Every topic has at least the OP; total posts >= topic count."""
        self.assertGreaterEqual(len(self.ext.new_posts), len(self.ext.new_topics))
        op_count = sum(1 for p in self.ext.new_posts if p.post_number == 1)
        self.assertEqual(op_count, len(self.ext.new_topics))

    def test_topic_ids_strictly_after_base(self) -> None:
        """New topic ids occupy the range `(max(base) , ...]`."""
        base_max = max(t.id for t in self.base.topics)
        for t in self.ext.new_topics:
            self.assertGreater(t.id, base_max, t)

    def test_post_ids_strictly_after_base(self) -> None:
        """New post ids occupy the range `(max(base), ...]`."""
        base_max = max(p.id for p in self.base.posts)
        for p in self.ext.new_posts:
            self.assertGreater(p.id, base_max, p)

    def test_no_id_collisions_within_extension(self) -> None:
        """Extension's own ids are unique (sanity)."""
        topic_ids = [t.id for t in self.ext.new_topics]
        self.assertEqual(len(topic_ids), len(set(topic_ids)))
        post_ids = [p.id for p in self.ext.new_posts]
        self.assertEqual(len(post_ids), len(set(post_ids)))

    def test_post_topic_id_references_extension_topics(self) -> None:
        """Every `Post.topic_id` resolves to an extension topic."""
        ext_topic_ids = {t.id for t in self.ext.new_topics}
        for p in self.ext.new_posts:
            self.assertIn(p.topic_id, ext_topic_ids, p)

    def test_parent_post_id_references_extension_post_or_none(self) -> None:
        """`Post.parent_post_id` is either None (OP) or an extension post."""
        ext_post_ids = {p.id for p in self.ext.new_posts}
        for p in self.ext.new_posts:
            if p.parent_post_id is None:
                self.assertEqual(p.post_number, 1, p)
            else:
                self.assertIn(p.parent_post_id, ext_post_ids, p)

    def test_timestamps_strictly_after_base(self) -> None:
        """Every new post's `created_at` > every base post's `created_at`."""
        base_end = max(p.created_at for p in self.base.posts)
        for p in self.ext.new_posts:
            self.assertGreater(p.created_at, base_end, p)

    def test_only_base_categories_used(self) -> None:
        """Extension topics only reference base categories."""
        base_cats = set(self.base.categories)
        for t in self.ext.new_topics:
            self.assertIn(t.category, base_cats, t)

    def test_only_base_tags_used(self) -> None:
        """Extension topics only reference tags from the base tag set."""
        base_tags = set(self.base.tags)
        for t in self.ext.new_topics:
            self.assertTrue(set(t.tags).issubset(base_tags), t)

    def test_only_base_authors_used(self) -> None:
        """Topic + post authors are all base users."""
        base_users = {u.username for u in self.base.users}
        for t in self.ext.new_topics:
            self.assertIn(t.author_username, base_users, t)
        for p in self.ext.new_posts:
            self.assertIn(p.author_username, base_users, p)

    def test_titles_do_not_collide_with_base(self) -> None:
        """Discourse rejects duplicate topic titles — extension dedupes."""
        base_titles = {t.title for t in self.base.topics}
        for t in self.ext.new_topics:
            self.assertNotIn(t.title, base_titles, t)


class ExtendForumDeterminismTests(unittest.TestCase):
    def test_same_args_yield_identical_extension(self) -> None:
        """Bit-for-bit identical output across two runs with the same args."""
        spec = _base_spec()
        ext_a = extend_forum(spec, add_topics_n=5, extend_seed=7)
        ext_b = extend_forum(spec, add_topics_n=5, extend_seed=7)
        self.assertEqual(
            dataclasses.asdict(ext_a), dataclasses.asdict(ext_b)
        )

    def test_different_extend_seeds_diverge(self) -> None:
        """Distinct `extend_seed` values produce distinct extensions."""
        spec = _base_spec()
        ext_a = extend_forum(spec, add_topics_n=5, extend_seed=7)
        ext_b = extend_forum(spec, add_topics_n=5, extend_seed=8)
        # At minimum titles must differ across some topic; relax to "any
        # field on any topic differs" for robustness against rare seed
        # collisions on the title front.
        self.assertNotEqual(
            [dataclasses.asdict(t) for t in ext_a.new_topics],
            [dataclasses.asdict(t) for t in ext_b.new_topics],
        )

    def test_different_base_seeds_diverge(self) -> None:
        """Same `extend_seed` against different bases yields different content."""
        ext_a = extend_forum(
            _base_spec(seed=42), add_topics_n=5, extend_seed=7
        )
        ext_b = extend_forum(
            _base_spec(seed=99), add_topics_n=5, extend_seed=7
        )
        # Different base seeds produce different categories / users / tags,
        # so the extension's topic/post fields must differ in at least one
        # place — assert via dict comparison on new_topics.
        self.assertNotEqual(
            [dataclasses.asdict(t) for t in ext_a.new_topics],
            [dataclasses.asdict(t) for t in ext_b.new_topics],
        )


class ExtendForumValidationTests(unittest.TestCase):
    def test_negative_add_topics_raises(self) -> None:
        with self.assertRaises(ValueError):
            extend_forum(_base_spec(), add_topics_n=-1, extend_seed=7)

    def test_negative_add_replies_raises(self) -> None:
        with self.assertRaises(ValueError):
            extend_forum(_base_spec(), add_replies_n=-1, extend_seed=7)

    def test_zero_total_extension_raises(self) -> None:
        """`extend_forum` with zero topics AND zero replies is a no-op error."""
        with self.assertRaises(ValueError):
            extend_forum(
                _base_spec(),
                add_topics_n=0,
                add_replies_n=0,
                extend_seed=7,
            )

    def test_zero_add_topics_with_replies_is_valid(self) -> None:
        """Sit 16: replies-only mode is a legitimate extension."""
        ext = extend_forum(
            _base_spec(),
            add_topics_n=0,
            add_replies_n=5,
            extend_seed=7,
        )
        self.assertEqual(len(ext.new_topics), 0)
        self.assertEqual(len(ext.new_posts), 5)


class ExtendForumAddRepliesTests(unittest.TestCase):
    """Sit 16: `extend --add-replies M` structural invariants."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _base_spec()
        cls.base = build_forum(cls.spec)
        # Replies-only extension at a count high enough to exercise the
        # multi-reply-per-topic path (10 replies across ~30 base topics
        # ≈ 0-2 replies per topic; some topics get multiple).
        cls.ext = extend_forum(
            cls.spec, add_topics_n=0, add_replies_n=10, extend_seed=7
        )

    def test_reply_count_matches_request(self) -> None:
        self.assertEqual(len(self.ext.new_posts), 10)
        self.assertEqual(self.ext.add_replies_n, 10)

    def test_no_new_topics_in_replies_only_mode(self) -> None:
        self.assertEqual(len(self.ext.new_topics), 0)
        self.assertEqual(self.ext.add_topics_n, 0)

    def test_replies_attach_to_base_topics_only(self) -> None:
        """Every appended reply's `topic_id` references a base topic."""
        base_topic_ids = {t.id for t in self.base.topics}
        for p in self.ext.new_posts:
            self.assertIn(p.topic_id, base_topic_ids, p)

    def test_reply_post_ids_strictly_after_base(self) -> None:
        base_max = max(p.id for p in self.base.posts)
        for p in self.ext.new_posts:
            self.assertGreater(p.id, base_max, p)

    def test_reply_timestamps_strictly_after_base(self) -> None:
        base_end = max(p.created_at for p in self.base.posts)
        for p in self.ext.new_posts:
            self.assertGreater(p.created_at, base_end, p)

    def test_reply_post_numbers_continue_per_topic(self) -> None:
        """post_number for each appended reply continues after the topic's max."""
        base_max_pn: dict[int, int] = {}
        for p in self.base.posts:
            current = base_max_pn.get(p.topic_id, 0)
            if p.post_number > current:
                base_max_pn[p.topic_id] = p.post_number
        for reply in self.ext.new_posts:
            self.assertGreater(
                reply.post_number, base_max_pn[reply.topic_id], reply
            )

    def test_multiple_replies_same_topic_monotonic(self) -> None:
        """If multiple replies hit the same topic, ts + post_number ascend."""
        by_topic: dict[int, list] = {}
        for p in self.ext.new_posts:
            by_topic.setdefault(p.topic_id, []).append(p)
        for tid, reps in by_topic.items():
            if len(reps) < 2:
                continue
            sorted_reps = sorted(reps, key=lambda r: r.post_number)
            for i in range(1, len(sorted_reps)):
                self.assertGreater(
                    sorted_reps[i].created_at,
                    sorted_reps[i - 1].created_at,
                    f"topic {tid}: reply ts not monotonic",
                )

    def test_authors_drawn_from_base_users(self) -> None:
        usernames = {u.username for u in self.base.users}
        for p in self.ext.new_posts:
            self.assertIn(p.author_username, usernames, p)

    def test_parent_post_id_resolves_to_base_or_extension_post(self) -> None:
        """parent_post_id points to a post in base OR an earlier appended reply."""
        valid_ids = {p.id for p in self.base.posts} | {
            p.id for p in self.ext.new_posts
        }
        for p in self.ext.new_posts:
            self.assertIsNotNone(
                p.parent_post_id,
                "appended reply must have a parent (it's a reply, not an OP)",
            )
            self.assertIn(p.parent_post_id, valid_ids, p)

    def test_replies_distributed_across_multiple_topics(self) -> None:
        """N=10 replies should land on at least 2 distinct base topics."""
        topic_ids_hit = {p.topic_id for p in self.ext.new_posts}
        self.assertGreater(len(topic_ids_hit), 1)


class ExtendForumMixedModeTests(unittest.TestCase):
    """Sit 16: `--add-topics N --add-replies M` interaction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _base_spec()
        cls.base = build_forum(cls.spec)
        cls.ext = extend_forum(
            cls.spec,
            add_topics_n=3,
            add_replies_n=7,
            extend_seed=7,
        )

    def test_counts_partitioned(self) -> None:
        """new_posts splits into (posts on new topics) + (replies on base topics)."""
        ext_topic_ids = {t.id for t in self.ext.new_topics}
        to_new = [p for p in self.ext.new_posts if p.topic_id in ext_topic_ids]
        to_base = [
            p for p in self.ext.new_posts if p.topic_id not in ext_topic_ids
        ]
        self.assertEqual(len(self.ext.new_topics), 3)
        # Each new topic has at least an OP.
        self.assertGreaterEqual(len(to_new), 3)
        # Exactly add_replies_n appended replies on base topics.
        self.assertEqual(len(to_base), 7)

    def test_post_ids_unique_across_both_flavours(self) -> None:
        ids = [p.id for p in self.ext.new_posts]
        self.assertEqual(len(ids), len(set(ids)))

    def test_post_ids_all_strictly_after_base(self) -> None:
        base_max = max(p.id for p in self.base.posts)
        for p in self.ext.new_posts:
            self.assertGreater(p.id, base_max, p)


class ExtendForumReleaseBurstTests(unittest.TestCase):
    """Sit 17: `extend --release-burst <version>` — load-bearing invariants only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = _base_spec()
        cls.base = build_forum(cls.spec)
        cls.ext = extend_forum(
            cls.spec, release_burst_version="remaster", extend_seed=7
        )

    def test_density_and_tag_signal(self) -> None:
        """Plan's integration test rolled up: ≥10 topics in a 7-day window,
        every one tagged `<version>` + one of [bug, feature-request, hint-needed]."""
        timestamps = sorted(t.created_at for t in self.ext.new_topics)
        span = timestamps[-1] - timestamps[0]
        self.assertGreaterEqual(len(self.ext.new_topics), 10)
        self.assertLessEqual(len(self.ext.new_topics), 20)
        self.assertLessEqual(span.total_seconds(), 7 * 24 * 3600)
        type_tags = {"bug", "feature-request", "hint-needed"}
        for t in self.ext.new_topics:
            self.assertIn("remaster", t.tags, t)
            self.assertTrue(set(t.tags) & type_tags, t)
        # And echo back on the dataclass.
        self.assertEqual(self.ext.release_burst_version, "remaster")

    def test_burst_is_self_contained_and_post_base(self) -> None:
        """All replies on burst topics (not base); all timestamps + ids
        strictly after base; reply count in [50, 100]."""
        burst_topic_ids = {t.id for t in self.ext.new_topics}
        base_post_id_max = max(p.id for p in self.base.posts)
        base_end = max(p.created_at for p in self.base.posts)
        for p in self.ext.new_posts:
            self.assertIn(p.topic_id, burst_topic_ids, p)
            self.assertGreater(p.id, base_post_id_max, p)
            self.assertGreater(p.created_at, base_end, p)
        op_count = sum(1 for p in self.ext.new_posts if p.post_number == 1)
        reply_count = len(self.ext.new_posts) - op_count
        self.assertEqual(op_count, len(self.ext.new_topics))
        self.assertGreaterEqual(reply_count, 50)
        self.assertLessEqual(reply_count, 100)

    def test_determinism_and_invalid_version(self) -> None:
        """Same args → identical extension; unknown version raises."""
        spec = _base_spec()
        ext_a = extend_forum(
            spec, release_burst_version="remaster", extend_seed=7
        )
        ext_b = extend_forum(
            spec, release_burst_version="remaster", extend_seed=7
        )
        self.assertEqual(
            dataclasses.asdict(ext_a), dataclasses.asdict(ext_b)
        )
        with self.assertRaises(ValueError):
            extend_forum(
                spec, release_burst_version="not-a-real-version", extend_seed=7
            )

    def test_composes_with_other_modes(self) -> None:
        """`--add-topics 3 --add-replies 4 --release-burst remaster` runs
        all three; ids unique, timestamps after base, appended-replies
        attach to base topics."""
        ext = extend_forum(
            self.spec,
            add_topics_n=3,
            add_replies_n=4,
            release_burst_version="remaster",
            extend_seed=7,
        )
        # Topic count = 3 add-topics + (10-20) burst topics.
        self.assertGreaterEqual(len(ext.new_topics), 13)
        self.assertLessEqual(len(ext.new_topics), 23)
        # Post ids unique across all flavours.
        post_ids = [p.id for p in ext.new_posts]
        self.assertEqual(len(post_ids), len(set(post_ids)))
        # Exactly 4 appended replies on base topics.
        base_topic_ids = {t.id for t in self.base.topics}
        appended = [p for p in ext.new_posts if p.topic_id in base_topic_ids]
        self.assertEqual(len(appended), 4)


class ExtendForumDeterminismRepliesTests(unittest.TestCase):
    def test_same_args_yield_identical_replies_only_extension(self) -> None:
        spec = _base_spec()
        ext_a = extend_forum(spec, add_replies_n=10, extend_seed=7)
        ext_b = extend_forum(spec, add_replies_n=10, extend_seed=7)
        self.assertEqual(
            dataclasses.asdict(ext_a), dataclasses.asdict(ext_b)
        )

    def test_same_args_yield_identical_mixed_extension(self) -> None:
        spec = _base_spec()
        ext_a = extend_forum(
            spec, add_topics_n=3, add_replies_n=7, extend_seed=7
        )
        ext_b = extend_forum(
            spec, add_topics_n=3, add_replies_n=7, extend_seed=7
        )
        self.assertEqual(
            dataclasses.asdict(ext_a), dataclasses.asdict(ext_b)
        )

    def test_different_extend_seeds_diverge_replies(self) -> None:
        spec = _base_spec()
        ext_a = extend_forum(spec, add_replies_n=10, extend_seed=7)
        ext_b = extend_forum(spec, add_replies_n=10, extend_seed=8)
        self.assertNotEqual(
            [dataclasses.asdict(p) for p in ext_a.new_posts],
            [dataclasses.asdict(p) for p in ext_b.new_posts],
        )


if __name__ == "__main__":
    unittest.main()
