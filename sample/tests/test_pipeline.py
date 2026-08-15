"""Tests for `sample.seed.pipeline.build_forum` + `push_forum`.

Pipeline tests lock in three contracts the CLI relies on:

1. `build_forum(spec)` (no body_provider) returns a Forum with all fields
   populated and structural integrity matching the underlying generators.
2. `build_forum(spec, body_provider=...)` actually injects body strings
   from the callable — proving the Sit-11 wire is in place independent of
   the live LLM smoke test.
3. `push_forum(forum, client)` (Sit 14) walks the Forum in the right order
   and forwards the right arguments to a `DiscourseClient` — verified
   against a hand-built MagicMock so we don't need a live Discourse stack
   for the unit tests.

These run alongside the existing per-generator suites; they exist to
exercise the assembly + body-injection seam, not to re-test the generators
themselves.
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import MagicMock

from sample.seed.forum import Forum
from sample.seed.generators.categories import generate_categories
from sample.seed.generators.posts import Post, generate_posts
from sample.seed.generators.tags import generate_tags
from sample.seed.generators.timeline import make_timeline
from sample.seed.generators.topics import Topic, generate_topics
from sample.seed.generators.users import User, generate_users
from sample.seed.pipeline import PushResult, build_forum, push_forum
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int = 42, scale: str = "tiny") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


class PipelineDefaultProviderTests(unittest.TestCase):
    """No body_provider -> placeholder bodies, all fields populated."""

    def test_build_forum_populates_all_fields(self) -> None:
        forum = build_forum(_spec())
        self.assertEqual(forum.seed, 42)
        self.assertEqual(forum.scale, "tiny")
        # Default `product_name` is the module's last segment (no slug
        # passed). Tests for the dashed-slug behaviour live in test_cli.
        self.assertEqual(forum.product_name, "crown_of_brine")
        self.assertGreater(len(forum.categories), 0)
        self.assertGreater(len(forum.tags), 0)
        self.assertGreater(len(forum.users), 0)
        self.assertGreater(len(forum.topics), 0)
        self.assertGreater(len(forum.posts), 0)

    def test_build_forum_matches_direct_generator_outputs(self) -> None:
        """Forum fields agree with what each generator returns standalone.

        Re-running every generator with the same spec must produce the
        same lists pipeline assembled — no hidden re-ordering, no extra
        massaging in the pipeline layer.
        """
        spec = _spec()
        cats = generate_categories(spec)
        tags = generate_tags(spec)
        users = generate_users(spec)
        timeline = make_timeline(
            spec, total_topics=spec.scale_targets()["topics"]
        )
        topics = generate_topics(spec, cats, tags, users, timeline)
        posts = generate_posts(spec, topics, users, timeline)

        forum = build_forum(spec)

        self.assertEqual(forum.categories, cats)
        self.assertEqual(forum.tags, tags)
        self.assertEqual(forum.users, users)
        self.assertEqual(forum.topics, topics)
        self.assertEqual(forum.posts, posts)

    def test_product_name_override(self) -> None:
        """`product_name=` argument flows through to `Forum.product_name`."""
        forum = build_forum(_spec(), product_name="crown-of-brine")
        self.assertEqual(forum.product_name, "crown-of-brine")


class PipelineBodyProviderTests(unittest.TestCase):
    """Body provider's return value reaches every post body verbatim."""

    def test_fixed_body_provider_applies_to_every_post(self) -> None:
        forum = build_forum(
            _spec(), body_provider=lambda topic, post: "fixed body"
        )
        bodies = {p.body for p in forum.posts}
        self.assertEqual(bodies, {"fixed body"})

    def test_provider_sees_topic_and_post_in_progress(self) -> None:
        """Provider receives the topic + a `PostInProgress` snapshot.

        Asserts the contract: the provider can read post-level structural
        decisions (post_number, author_username, parent_post_id,
        created_at, quote_target_id) without `id`, since `id` is assigned
        only after the body is set.
        """
        seen: list[tuple[int, int, str]] = []

        def provider(topic, post) -> str:
            # `post` is a PostInProgress, not a Post — has no `id`.
            self.assertFalse(hasattr(post, "id"))
            self.assertEqual(post.topic_id, topic.id)
            seen.append((topic.id, post.post_number, post.author_username))
            return f"body for topic={topic.id} post={post.post_number}"

        forum = build_forum(_spec(), body_provider=provider)

        # Every post got a unique body that round-trips the (topic, post)
        # the provider was called with.
        for p in forum.posts:
            self.assertEqual(
                p.body, f"body for topic={p.topic_id} post={p.post_number}"
            )
        # Provider was called once per generated post.
        self.assertEqual(len(seen), len(forum.posts))
        # Each (topic_id, post_number) was unique.
        counts = Counter((tid, pn) for tid, pn, _ in seen)
        self.assertTrue(all(v == 1 for v in counts.values()))

    def test_body_provider_does_not_shift_structure(self) -> None:
        """Same spec + different body_providers → identical structure.

        The body provider must not influence reply counts, parent links,
        timestamps, or any other structural decision. Two runs with two
        different providers should differ ONLY on `body`.
        """
        forum_a = build_forum(
            _spec(), body_provider=lambda t, p: "BODY-A"
        )
        forum_b = build_forum(
            _spec(), body_provider=lambda t, p: "BODY-B"
        )

        # Same number of posts; same per-post structural fields (id,
        # topic_id, post_number, parent_post_id, author_username,
        # created_at, is_accepted_solution, quote_target_id).
        self.assertEqual(len(forum_a.posts), len(forum_b.posts))
        for pa, pb in zip(forum_a.posts, forum_b.posts):
            self.assertEqual(pa.id, pb.id)
            self.assertEqual(pa.topic_id, pb.topic_id)
            self.assertEqual(pa.post_number, pb.post_number)
            self.assertEqual(pa.parent_post_id, pb.parent_post_id)
            self.assertEqual(pa.author_username, pb.author_username)
            self.assertEqual(pa.created_at, pb.created_at)
            self.assertEqual(
                pa.is_accepted_solution, pb.is_accepted_solution
            )
            self.assertEqual(pa.quote_target_id, pb.quote_target_id)
            # Bodies differ — proving the provider was actually consulted.
            self.assertEqual(pa.body, "BODY-A")
            self.assertEqual(pb.body, "BODY-B")


def _patch_push_delay():
    """Patcher zeroing `_PUSH_REQUEST_DELAY_SECONDS` for unit tests.

    Otherwise each unit test would block for ~150 × 0.4s = 60s on
    `time.sleep` calls. The delay is a live-stack rate-limit work-
    around; against a MagicMock there's nothing to rate-limit.
    """
    from unittest.mock import patch as _patch

    return _patch("sample.seed.pipeline._PUSH_REQUEST_DELAY_SECONDS", 0)


def _build_mock_client(
    *,
    category_id_seq: tuple[int, ...] = (10, 11, 12, 13, 14, 15, 16),
    user_id_seq: tuple[int, ...] = tuple(range(100, 300)),
    topic_id_seq: tuple[int, ...] = tuple(range(1000, 1500)),
) -> MagicMock:
    """Construct a `DiscourseClient`-shaped mock with deterministic ids.

    The mock returns a fresh id from each pre-baked sequence on each call —
    enough variety that test assertions can distinguish between categories,
    users, and topics without coupling to specific values. `create_post`
    just returns a stub dict (we don't track post ids in `PushResult`).
    """
    client = MagicMock()
    client.create_category.side_effect = list(category_id_seq)
    client.create_user.side_effect = list(user_id_seq)

    def _create_topic(**kwargs):
        # Pop one id off the sequence per call.
        topic_id = _create_topic.ids.pop(0)
        return {"topic_id": topic_id, "id": topic_id * 10, "post_number": 1}

    _create_topic.ids = list(topic_id_seq)
    client.create_topic.side_effect = _create_topic

    client.create_post.return_value = {
        "id": 9999,
        "post_number": 2,
    }
    return client


def _tiny_forum() -> Forum:
    """Build the smallest deterministic Forum for push tests."""
    spec = _spec()  # tiny scale, seed=42
    return build_forum(spec, product_name="crown-of-brine")


class PushForumTests(unittest.TestCase):
    """Unit tests for `push_forum` against a mocked DiscourseClient."""

    def setUp(self) -> None:
        # Patch the per-request delay to 0. Without this each test would
        # block for ~minute on `time.sleep`. See `_patch_push_delay`.
        delay_patcher = _patch_push_delay()
        delay_patcher.start()
        self.addCleanup(delay_patcher.stop)

    def test_push_returns_pushresult_with_expected_shape(self) -> None:
        forum = _tiny_forum()
        client = _build_mock_client()

        result = push_forum(forum, client)

        self.assertIsInstance(result, PushResult)
        # Every category got an id.
        self.assertEqual(
            len(result.category_ids), len(forum.categories)
        )
        # Every user got an id.
        self.assertEqual(len(result.user_ids), len(forum.users))
        # Every topic got an id.
        self.assertEqual(len(result.topic_ids), len(forum.topics))
        # post_count = OPs + replies = total posts.
        self.assertEqual(result.post_count, len(forum.posts))
        # No errors on the happy path.
        self.assertEqual(result.errors, [])

    def test_push_call_order_categories_users_topics_posts(self) -> None:
        """Categories pushed before users, users before topics, OPs before
        replies. We assert by inspecting `client.method_calls` in order.
        """
        forum = _tiny_forum()
        client = _build_mock_client()

        push_forum(forum, client)

        # Walk the call list and capture the method-name sequence; collapse
        # consecutive identical names so we can assert on the high-level
        # phase order without coupling to per-entity counts.
        names = [c[0] for c in client.method_calls]
        # Phase boundaries appear once each in this sequence.
        # We assert the FIRST `create_category` precedes the FIRST
        # `create_user`, FIRST `create_user` precedes FIRST `create_topic`,
        # FIRST `create_topic` precedes FIRST `create_post`.
        first = {}
        for idx, name in enumerate(names):
            if name not in first:
                first[name] = idx
        self.assertLess(
            first["create_category"], first["create_user"],
            "categories must be pushed before users",
        )
        self.assertLess(
            first["create_user"], first["create_topic"],
            "users must be pushed before topics",
        )
        # `create_post` is only called for replies; topics with zero replies
        # mean no `create_post` ever fires. Tiny scale has plenty of replies.
        if "create_post" in first:
            self.assertLess(
                first["create_topic"], first["create_post"],
                "topics (OPs) must precede replies",
            )

    def test_push_skips_explicit_create_tag(self) -> None:
        """Tags auto-create on topic POST — `create_tag` must NOT be called."""
        forum = _tiny_forum()
        client = _build_mock_client()

        push_forum(forum, client)

        # `create_tag` was never invoked — Discourse handles it lazily.
        client.create_tag.assert_not_called()

    def test_push_passes_correct_category_id_per_topic(self) -> None:
        """Every `create_topic` call uses the id that `create_category`
        returned for that topic's category — no cross-wiring.
        """
        forum = _tiny_forum()
        client = _build_mock_client()

        result = push_forum(forum, client)

        # Build a map from create_topic invocation order back to the seeded
        # topic. The chronological order push_forum uses is sort by
        # (created_at, id), which equals topic.id order on tiny seed=42.
        chronological_topics = sorted(
            forum.topics, key=lambda t: (t.created_at, t.id)
        )
        topic_calls = [
            c for c in client.method_calls if c[0] == "create_topic"
        ]
        self.assertEqual(len(topic_calls), len(chronological_topics))

        for call, topic in zip(topic_calls, chronological_topics):
            kwargs = call.kwargs
            expected_category_id = result.category_ids[topic.category]
            self.assertEqual(
                kwargs["category_id"],
                expected_category_id,
                f"topic {topic.id} ({topic.category!r}) sent wrong category_id",
            )
            self.assertEqual(kwargs["title"], topic.title)
            self.assertEqual(
                kwargs["author_username"], topic.author_username
            )
            # Tags forwarded as a list of strings.
            if topic.tags:
                self.assertEqual(kwargs["tags"], list(topic.tags))

    def test_push_users_use_synthetic_email_and_fixture_password(self) -> None:
        """Email = `<username>@sample.local`; password = the shared fixture."""
        forum = _tiny_forum()
        client = _build_mock_client()

        push_forum(forum, client)

        user_calls = [
            c for c in client.method_calls if c[0] == "create_user"
        ]
        self.assertEqual(len(user_calls), len(forum.users))
        seen_passwords = set()
        for call, user in zip(user_calls, forum.users):
            args = call.args
            kwargs = call.kwargs
            self.assertEqual(args[0], user.username)
            password = args[1]
            seen_passwords.add(password)
            self.assertEqual(args[2], f"{user.username}@sample.local")
            self.assertEqual(kwargs["name"], user.display_name)
            self.assertTrue(kwargs["active"])
            self.assertTrue(kwargs["approved"])
        # All users share the same fixture password.
        self.assertEqual(len(seen_passwords), 1)
        # Length safely exceeds Discourse's default min_password_length (10).
        self.assertGreaterEqual(len(seen_passwords.pop()), 10)

    def test_push_reply_to_post_number_for_non_op_parents(self) -> None:
        """Non-OP parents → reply_to_post_number set; OP parents → None.

        Builds a small hand-crafted forum so we can pin exact wiring without
        depending on the seeder's stochastic reply-tree shape.
        """
        ts = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        users = [
            User("admin_user", "Admin User", "admin", 1.0),
            User("alice_x", "Alice X", "regular", 1.0),
        ]
        topic = Topic(
            id=1,
            title="Test topic",
            category="General",
            tags=["bug"],
            author_username="admin_user",
            created_at=ts,
        )
        op = Post(
            id=1,
            topic_id=1,
            post_number=1,
            parent_post_id=None,
            author_username="admin_user",
            body="OP body, well over twenty characters please.",
            created_at=ts,
            is_accepted_solution=False,
            quote_target_id=None,
        )
        # Reply 1: parents to OP -> reply_to_post_number should be None.
        reply1 = Post(
            id=2,
            topic_id=1,
            post_number=2,
            parent_post_id=1,  # OP id
            author_username="alice_x",
            body="Reply to OP, padded to clear min length.",
            created_at=ts,
            is_accepted_solution=False,
            quote_target_id=None,
        )
        # Reply 2: parents to reply1 (a non-OP) -> reply_to_post_number=2.
        reply2 = Post(
            id=3,
            topic_id=1,
            post_number=3,
            parent_post_id=2,
            author_username="admin_user",
            body="Reply to reply1, also padded out for length.",
            created_at=ts,
            is_accepted_solution=False,
            quote_target_id=None,
        )

        forum = Forum(
            seed=0,
            scale="tiny",
            product_name="test",
            categories=["General"],
            tags=["bug"],
            users=users,
            topics=[topic],
            posts=[op, reply1, reply2],
        )

        client = _build_mock_client()
        push_forum(forum, client)

        post_calls = [
            c for c in client.method_calls if c[0] == "create_post"
        ]
        self.assertEqual(len(post_calls), 2, "two replies expected")

        # First reply parents to OP → reply_to_post_number should be None.
        self.assertIsNone(post_calls[0].kwargs.get("reply_to_post_number"))
        # Second reply parents to reply1 (post_number=2) → reply_to=2.
        self.assertEqual(
            post_calls[1].kwargs.get("reply_to_post_number"), 2
        )
        # Both replies forwarded the right author + topic id (the topic_id
        # came from the mock — 1000 is the first id in the sequence).
        self.assertEqual(post_calls[0].kwargs["topic_id"], 1000)
        self.assertEqual(post_calls[1].kwargs["topic_id"], 1000)
        self.assertEqual(post_calls[0].kwargs["author_username"], "alice_x")
        self.assertEqual(
            post_calls[1].kwargs["author_username"], "admin_user"
        )

    def test_push_does_not_mark_accepted_solution_or_quotes(self) -> None:
        """Sit 14 deliberately defers solved markers + quote_target rewrites.

        Asserts no extra calls beyond the documented client methods —
        in particular, no `mark_solved` or body-rewriting POST that would
        signal the deferred features had silently been wired.
        """
        forum = _tiny_forum()
        client = _build_mock_client()

        push_forum(forum, client)

        # Only these methods should have been touched: the four create_*
        # endpoints, `set_site_setting` for the rate-limit pre-tune, and
        # `grant_moderator` for the per-user staff bypass (Sit 14.1).
        called = {c[0] for c in client.method_calls}
        self.assertLessEqual(
            called,
            {
                "create_category",
                "create_user",
                "create_topic",
                "create_post",
                "set_site_setting",
                "grant_moderator",
            },
            f"unexpected client method calls: {called}",
        )

    def test_push_tunes_rate_limits_before_writes(self) -> None:
        """Push lifts the post rate-limit before pushing categories/users.

        We assert the FIRST `set_site_setting` call precedes the FIRST
        `create_category` call so a slow push doesn't waste minutes on
        the default rate limit.
        """
        forum = _tiny_forum()
        client = _build_mock_client()

        push_forum(forum, client)

        names = [c[0] for c in client.method_calls]
        first = {}
        for idx, name in enumerate(names):
            if name not in first:
                first[name] = idx
        self.assertIn("set_site_setting", first)
        self.assertLess(
            first["set_site_setting"], first["create_category"],
            "rate-limit settings must be tuned before any write",
        )

        # The setting names tuned should include `rate_limit_create_post`
        # at minimum — that's the dominant cost on the default config.
        tuned = {
            c.args[0] for c in client.method_calls
            if c[0] == "set_site_setting"
        }
        self.assertIn("rate_limit_create_post", tuned)


if __name__ == "__main__":
    unittest.main()
