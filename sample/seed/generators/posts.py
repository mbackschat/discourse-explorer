"""Post generator with Poisson reply trees + cross-references.

Posts are the leaves of the forum: every topic owns an OP (post #1) plus a
Poisson-distributed tail of replies. The design doc's "Injected forum
dynamics" section asks for four structural signals on top of the raw count:

1. **Reply density tracks the scale preset.** The mean reply count per topic
   is `(scale_targets()["posts"] / scale_targets()["topics"]) - 1` (subtract
   one because the OP is already counted in `posts`). Drawing per-topic
   counts from a Poisson with that mean keeps total post counts close to
   the preset target while admitting natural variance.
2. **Cluster threads are discussion-worthy.** The first
   `len(CLUSTER_TAG_COMBINATIONS)` topics — the Sit-5 cluster anchors —
   force `replies >= 3` so the pain-point clusters always look like
   pain-points, not orphan one-offs. The cluster-anchor minimum overrides
   the unanswered-rule below.
3. **Realistic backlog.** Roughly 10% of *non-anchor* topics end up with
   zero replies (lore questions, obscure mod issues that never got an
   answer). Drives `stats unanswered`.
4. **Solved markers.** Roughly 15% of Help & Hints topics with at least one
   reply have one (randomly-chosen) reply marked `is_accepted_solution`.
   Drives the `accepted_answer` field.
5. **Cross-references.** Roughly 20% of replies (post_number >= 2) carry a
   `quote_target_id` pointing to a previously-generated post (any topic).
   Drives the graph-density signal in the visualizer.

Reply trees are not flat: 80% of replies parent to the OP, 20% parent to a
random earlier reply in the same topic. Author selection is weighted by
`User.activity_weight` (Pareto), and the OP author may reply to themselves —
that's how forums actually look.

Timestamps strictly post-date the parent: every reply is generated with a
delta in `[5, 14400]` minutes (5 min to 10 days) after its parent's
timestamp, accumulated through the reply tree so children always come after
ancestors. We do NOT cap on the topic's neighbour-in-time — replies routinely
land after the next topic was created, which is realistic.

Body content defaults to a placeholder string. Sit 11 added an optional
`body_provider` callable: `generate_posts` invokes it once per post (after
all structural decisions for that post are made) to obtain the body. The
provider receives a frozen `PostInProgress` snapshot with everything except
the not-yet-known final `id` — so the body generator (`content/bodies.py`)
sees `topic_id`, `post_number`, `parent_post_id`, `author_username`,
`created_at`, etc. The provider does NOT influence reply counts, parent
links, timestamps, or any other structural decision; it only picks the
body string.

Determinism: uses `spec.rng("posts")`. Same `(spec, topics, users, timeline)`
plus same `body_provider` -> identical `list[Post]`. The default (no
`body_provider`) keeps the placeholder behaviour from Sit 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

from ..universe import GenerationSpec
from .timeline import Timeline
from .topics import Topic
from .users import User


# Probability a reply parents to the OP rather than a random earlier reply.
# 80/20 keeps most threads shallow (Discourse-like) while still producing
# enough sub-threads to give the visualizer's reply graph some depth.
_REPLY_TO_OP_PROBABILITY = 0.80

# Probability a reply carries a `quote_target_id` referencing an earlier
# post. 20% matches the design doc's cross-reference rate.
_QUOTE_PROBABILITY = 0.20

# Probability a non-anchor topic ends up with zero replies, overriding the
# Poisson draw. Cluster-anchor topics ignore this rule (see module docstring).
_UNANSWERED_PROBABILITY = 0.10

# Probability a Help & Hints topic with >=1 reply has one of those replies
# marked `is_accepted_solution`. Other categories never carry the marker.
_SOLVED_PROBABILITY = 0.15

# Cluster-anchor topics are forced to have at least this many replies on top
# of the OP — a cluster with only an OP can't carry the discussion signal
# `/forum-report` looks for.
_CLUSTER_ANCHOR_MIN_REPLIES = 3

# Reply timestamp delta bounds, in minutes, relative to the parent post's
# timestamp. 5 minutes is a fast back-and-forth; 10 days (14400 min) is the
# long-tail "thread revives weeks later" case.
_REPLY_DELTA_MIN_MINUTES = 5
_REPLY_DELTA_MAX_MINUTES = 14400

# Help & Hints category name. Used to gate the solved-marker rule. We could
# import the constant from the product module, but the category string is a
# stable contract across the seeder so the duplication is acceptable.
_HELP_AND_HINTS_CATEGORY = "Help & Hints"


@dataclass(frozen=True)
class Post:
    """A generated forum post.

    Attributes:
        id: globally unique 1-indexed sequential ID across all posts in the
            forum.
        topic_id: the `Topic.id` this post belongs to.
        post_number: per-topic 1-indexed position. `1` is the OP, `>=2` are
            replies in the order they were generated for the topic.
        parent_post_id: `None` for the OP; otherwise the `Post.id` of the
            parent post (either the OP or a random earlier reply in the same
            topic).
        author_username: a username from the input user list. The OP's
            author may reply to itself (some authors do that IRL).
        body: placeholder string for Phase 1. Phase 2 swaps this for
            LLM-generated content via `content/bodies.py`.
        created_at: timestamp; for OP equals the topic's `created_at`, for
            replies equals `parent.created_at + delta` where delta is in
            `[5, 14400]` minutes.
        is_accepted_solution: True for at most one reply per Help & Hints
            topic; always False for the OP and for non-Help & Hints topics.
        quote_target_id: optional `Post.id` of an earlier post (any topic)
            this post quotes. ~20% of replies carry one; OPs never do.
    """

    id: int
    topic_id: int
    post_number: int
    parent_post_id: Optional[int]
    author_username: str
    body: str
    created_at: datetime
    is_accepted_solution: bool
    quote_target_id: Optional[int]


@dataclass(frozen=True)
class PostInProgress:
    """Pre-finalisation snapshot of a post passed to a `body_provider`.

    Carries everything the body generator needs to compose a prompt — except
    the global `id` (assigned only after the body is set) and the
    `is_accepted_solution` flag (decided in a 2nd pass once all replies
    exist). The fields mirror `Post` 1:1 so a provider can rely on the same
    attribute names whether it inspects the in-progress object here or a
    finalised `Post` elsewhere.

    Use a frozen dataclass — mutation by a misbehaving provider would be a
    bug, and freezing surfaces it immediately rather than silently shifting
    later structural decisions.
    """

    topic_id: int
    post_number: int
    parent_post_id: Optional[int]
    author_username: str
    created_at: datetime
    quote_target_id: Optional[int]


# A `BodyProvider` is a callable receiving the `Topic` and a
# `PostInProgress` snapshot, returning the post's body string. `None` is the
# default in `generate_posts` so existing callers (and the structural test
# suite) keep getting placeholder bodies without dragging an LLM into the
# loop. The real provider is wired by `cli.py` via `pipeline.build_forum`.
BodyProvider = Callable[[Topic, "PostInProgress"], str]


def _validate_inputs(
    spec: GenerationSpec,
    topics: Sequence[Topic],
    users: Sequence[User],
    timeline: Timeline,
) -> None:
    """Raise `ValueError` on any malformed input — generator-hygiene rule."""
    if not topics:
        raise ValueError("generate_posts: topics must be non-empty")
    if not users:
        raise ValueError("generate_posts: users must be non-empty")
    if timeline.total_topics < len(topics):
        raise ValueError(
            f"generate_posts: timeline sized for {timeline.total_topics} "
            f"topics, but {len(topics)} topics were passed"
        )

    targets = spec.scale_targets()
    if targets["topics"] <= 0 or targets["posts"] <= 0:
        raise ValueError(
            f"generate_posts: scale {spec.scale!r} has non-positive targets "
            f"{targets}"
        )

    usernames = {u.username for u in users}
    for t in topics:
        if t.author_username not in usernames:
            raise ValueError(
                f"generate_posts: topic {t.id} author "
                f"{t.author_username!r} not in user list"
            )


def _placeholder_body(topic_id: int, post_number: int) -> str:
    """Return the placeholder body string used in Phase 1.

    Phase 2 swaps this for `content/bodies.generate_body(...)` — the
    placeholder is intentionally trivial so the structural test suite has
    something stable to assert against without dragging an LLM into the loop.
    """
    return f"<placeholder body for topic {topic_id} post {post_number}>"


def _mean_replies_per_topic(spec: GenerationSpec) -> float:
    """Return the Poisson mean reply count for this scale.

    Subtract 1 because `posts` in the scale preset includes the OP — we want
    the mean of the *reply tail*, not of total posts.
    """
    targets = spec.scale_targets()
    return max(0.0, (targets["posts"] / targets["topics"]) - 1.0)


def _reply_count_for_topic(
    rng,
    is_cluster_anchor: bool,
    poisson_mean: float,
) -> int:
    """Pick a per-topic reply count.

    Order of operations matters and is encoded here once so the test
    invariants line up with the implementation:

    1. Poisson draw with the scale-derived mean.
    2. Cluster anchors force `>= _CLUSTER_ANCHOR_MIN_REPLIES`.
    3. Unanswered rule (10%) overrides to 0 for non-anchor topics ONLY.

    The unanswered roll is drawn AFTER the Poisson so removing/changing the
    cluster-anchor behaviour doesn't shift subsequent rng advances.
    """
    base = rng.poisson(poisson_mean)
    # Unanswered roll always drawn so removing/changing the cluster-anchor
    # branch doesn't shift subsequent rng advances. The roll is honored only
    # for non-anchor topics — cluster anchors enforce their floor below
    # regardless.
    unanswered_roll = rng.pick_int(0, 99)
    if is_cluster_anchor:
        return max(base, _CLUSTER_ANCHOR_MIN_REPLIES)
    if unanswered_roll < int(_UNANSWERED_PROBABILITY * 100):
        return 0
    return base


def generate_posts(
    spec: GenerationSpec,
    topics: Sequence[Topic],
    users: Sequence[User],
    timeline: Timeline,
    body_provider: Optional[BodyProvider] = None,
    *,
    id_offset: int = 0,
    skip_cluster_anchors: bool = False,
) -> list[Post]:
    """Return a deterministic `list[Post]` covering every topic.

    Per topic: one OP plus a Poisson-distributed tail of replies. Reply
    parents, authors, timestamps, accepted-solution flags, and cross-
    references are all drawn from `spec.rng("posts")`.

    Body content is the placeholder string by default. Pass `body_provider`
    to substitute LLM-generated bodies (or any other strategy): for each
    post the callable receives `(topic, post_in_progress)` and returns the
    body string. The provider is invoked AFTER all structural decisions for
    that post are finalised, so it sees consistent author / parent /
    timestamp values and cannot influence them.

    With `id_offset=K` (default 0), post ids start at `K + 1` instead of 1
    — used by `extend` so extension post ids don't collide with base post
    ids. The offset propagates automatically through `parent_post_id` and
    `quote_target_id` because both reference posts already produced in this
    same call (which all carry the offset).

    With `skip_cluster_anchors=True`, the cluster-anchor reply-floor pass
    is suppressed: the first `len(CLUSTER_TAG_COMBINATIONS)` topics are
    NOT forced above `_CLUSTER_ANCHOR_MIN_REPLIES`. Used by `extend` (Sit
    15+): cluster anchors are an init-only "first N topics" reservation
    pattern, so the extension's topics must NOT inherit the floor.

    Same `(spec, topics, users, timeline, body_provider, id_offset,
    skip_cluster_anchors)` -> identical output (full dataclass equality
    across the list).
    """
    _validate_inputs(spec, topics, users, timeline)

    product = spec.product
    cluster_anchor_count = (
        0 if skip_cluster_anchors else len(product.CLUSTER_TAG_COMBINATIONS)
    )
    poisson_mean = _mean_replies_per_topic(spec)

    rng = spec.rng("posts")

    user_list: list[User] = list(users)
    user_weights = [u.activity_weight for u in user_list]
    if not any(w > 0 for w in user_weights):
        raise ValueError(
            "generate_posts: every user has zero activity_weight; "
            "weighted author draw cannot proceed"
        )
    username_by_user = {u.username: u for u in user_list}

    posts: list[Post] = []
    next_post_id = 1 + id_offset

    for topic_idx, topic in enumerate(topics):
        is_cluster_anchor = topic_idx < cluster_anchor_count

        # OP (post #1) always emitted first. Author = topic.author_username
        # so the topic's author and OP author agree (this is what the data
        # model expects downstream).
        op_author = username_by_user.get(topic.author_username)
        if op_author is None:
            raise ValueError(
                f"generate_posts: topic {topic.id} author "
                f"{topic.author_username!r} not in user list"
            )
        op_in_progress = PostInProgress(
            topic_id=topic.id,
            post_number=1,
            parent_post_id=None,
            author_username=op_author.username,
            created_at=topic.created_at,
            quote_target_id=None,
        )
        op_body = (
            body_provider(topic, op_in_progress)
            if body_provider is not None
            else _placeholder_body(topic.id, 1)
        )
        op = Post(
            id=next_post_id,
            topic_id=topic.id,
            post_number=1,
            parent_post_id=None,
            author_username=op_author.username,
            body=op_body,
            created_at=topic.created_at,
            is_accepted_solution=False,
            quote_target_id=None,
        )
        posts.append(op)
        next_post_id += 1

        topic_posts: list[Post] = [op]

        reply_count = _reply_count_for_topic(
            rng, is_cluster_anchor, poisson_mean
        )

        for reply_idx in range(reply_count):
            post_number = reply_idx + 2  # 1 = OP, replies start at 2
            # Parent: 80% OP, 20% earlier reply (if any exist). For the very
            # first reply there's only the OP, so the 20% branch has no
            # earlier reply to point at and we silently fall back to the OP
            # (still consumes the rng draw, keeping advances stable).
            parent_choice_roll = rng.pick_int(0, 99)
            if (
                parent_choice_roll >= int(_REPLY_TO_OP_PROBABILITY * 100)
                and len(topic_posts) > 1
            ):
                parent = rng.pick_one(topic_posts[1:])
            else:
                parent = op

            # Quote target: ~20% chance to reference an earlier post (ANY
            # topic). Roll always happens so removing the feature later
            # doesn't shift subsequent draws.
            quote_roll = rng.pick_int(0, 99)
            if quote_roll < int(_QUOTE_PROBABILITY * 100) and posts:
                quote_target = rng.pick_one(posts)
                quote_target_id: Optional[int] = quote_target.id
            else:
                quote_target_id = None

            # Author: weighted by activity. OP author can also reply (no
            # exclusion list — some authors really do reply to themselves).
            author = rng.weighted(user_list, user_weights)

            # Timestamp: parent.created_at + delta minutes. Always > parent
            # by construction.
            delta_minutes = rng.pick_int(
                _REPLY_DELTA_MIN_MINUTES, _REPLY_DELTA_MAX_MINUTES
            )
            created_at = parent.created_at + timedelta(minutes=delta_minutes)

            reply_in_progress = PostInProgress(
                topic_id=topic.id,
                post_number=post_number,
                parent_post_id=parent.id,
                author_username=author.username,
                created_at=created_at,
                quote_target_id=quote_target_id,
            )
            reply_body = (
                body_provider(topic, reply_in_progress)
                if body_provider is not None
                else _placeholder_body(topic.id, post_number)
            )
            reply = Post(
                id=next_post_id,
                topic_id=topic.id,
                post_number=post_number,
                parent_post_id=parent.id,
                author_username=author.username,
                body=reply_body,
                created_at=created_at,
                is_accepted_solution=False,  # solved flag set in a 2nd pass
                quote_target_id=quote_target_id,
            )
            posts.append(reply)
            topic_posts.append(reply)
            next_post_id += 1

        # Solved marker (Help & Hints only; ~15%; only if the topic actually
        # has a reply to mark). Rolled per-topic AFTER all replies exist so
        # the choice draw is decoupled from the per-reply parent/author/
        # quote draws above.
        if (
            topic.category == _HELP_AND_HINTS_CATEGORY
            and len(topic_posts) >= 2  # at least one reply
        ):
            solved_roll = rng.pick_int(0, 99)
            if solved_roll < int(_SOLVED_PROBABILITY * 100):
                solution_pick_idx = rng.pick_int(1, len(topic_posts) - 1)
                solution_post = topic_posts[solution_pick_idx]
                # Replace by a new frozen Post with the flag flipped — the
                # frozen dataclass means we can't mutate, and we also have
                # to update both the local `topic_posts` and the global
                # `posts` list at the same index.
                marked = Post(
                    id=solution_post.id,
                    topic_id=solution_post.topic_id,
                    post_number=solution_post.post_number,
                    parent_post_id=solution_post.parent_post_id,
                    author_username=solution_post.author_username,
                    body=solution_post.body,
                    created_at=solution_post.created_at,
                    is_accepted_solution=True,
                    quote_target_id=solution_post.quote_target_id,
                )
                topic_posts[solution_pick_idx] = marked
                # Find and replace in the global list. The post was appended
                # in order so we can index from the end (latest topic's
                # posts are at the tail).
                for global_idx in range(len(posts) - 1, -1, -1):
                    if posts[global_idx].id == marked.id:
                        posts[global_idx] = marked
                        break

    return posts
