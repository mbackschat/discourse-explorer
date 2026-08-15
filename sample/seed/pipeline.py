"""Forum-build pipeline — single entry point that runs every generator.

Sit 11 lifts the generator-orchestration sequence out of `cli.py` and into
its own module so future code paths (the live-Discourse `init`, `extend`,
fixture-snapshot tooling, …) share one spine. The CLI now calls
`build_forum(spec, body_provider=...)`; tests can call it directly with no
process-boundary detour.

Generator dependency order is fixed and load-bearing for determinism:

    categories -> tags -> users -> timeline -> topics -> posts

Each generator asks `spec.rng(<salt>)` for its own derived stream, so the
order matters only for what each generator gets to consume — not for the
randomness used inside it. Same `(seed, scale, product)` plus same
`body_provider` -> bit-for-bit identical `Forum`.

The optional `body_provider` is forwarded verbatim to `generate_posts`.
Default `None` keeps the placeholder body behaviour from Sit 6 — important
because every test that exercises `build_forum` would otherwise need to
mock or skip the LLM round-trip.

Sit 14 adds the live-push spine — `push_forum(forum, client)` — that takes
a fully-built `Forum` and POSTs it to a running Discourse instance via the
Sit 13 `DiscourseClient`. The push is the one-way mirror of `build_forum`:
deterministic structure in, real Discourse-side ids out, captured in a
`PushResult` so callers (`cli.init`, integration tests) can summarise what
landed without re-querying the server.

## Push order + role-handling decisions (Sit 14)

The push order — categories → users → topics-with-OPs → replies, in topic
chronological order — was chosen so every later request can refer to the
ids returned by an earlier one without holding extra state. Tags are
auto-created by Discourse when first attached to a topic, so the explicit
`create_tag` call is skipped (it's a documented no-op anyway, see
`discourse_api.py`).

**Role handling — Sit 14.1 revision: every seeded user is promoted to
moderator immediately after creation.** The Sit 14 plan said "all users push
as regular Discourse users — labels are demo-side metadata"; that worked
for the dry-run path but DEADLOCKED the live push within the first ~5
topics. Discourse's per-user rate limits (`max_topics_in_first_day=3`,
`max_topics_per_day=20`, `rate_limit_create_post=5`, etc.) all check via
`RateLimiter#rate_unlimited?`, which short-circuits to true when
`user.staff?` is true (verified against `lib/rate_limiter.rb` +
`app/models/user.rb` in bitnamilegacy/discourse 3.4.6). Promoting every
seeded user to moderator therefore bypasses every per-user rate cap —
present or future, named or unnamed — without us having to enumerate
Discourse's full rate-limit surface. Cosmetic side effect: each user wears
a moderator badge in the UI. Acceptable on a fixture forum; would not be
on a production import tool.

The seeded `User.role` field still distinguishes admin / moderator /
regular as design intent (the visualizer's activity diagnostics still see
the right Pareto distribution). The grant changes only the in-Discourse
role; it doesn't reach back into the seeder's data model.

**The bitnami admin is NOT pushed.** The container created an admin user
from the `DISCOURSE_USERNAME` env var; that admin is the API caller, not
a seeded forum user. Seeded usernames come from the nautical
adjective + noun pool in `crown_of_brine.USERNAME_PARTS`, which is
extremely unlikely to collide with the admin's username (the bitnami
default is typically `user` or whatever the operator picked).

**Email derivation: `f"{username}@sample.local"`.** Discourse requires a
unique email per user; we mint a deterministic one per seeded user. The
`.local` TLD is RFC-6762-reserved and won't accept real mail — that's
fine for a localhost demo, since the test stack effectively skips email
validation (bitnami's defaults treat the admin-created `active=True`
flag as bypassing the verification round-trip).

**Password is a single shared placeholder.** Every seeded user gets
`_FIXTURE_PASSWORD` (declared below). This is fixture data on a localhost
container — anyone with shell access to the host can read the seeder
source anyway. The password complexity satisfies Discourse's default
`min_password_length` (10) without making the test harness compute a
unique password per user.

## What's deliberately deferred

Two seeder-side metadata fields don't translate to Discourse for Sit 14:

* `Post.is_accepted_solution` — Discourse marks accepted solutions via the
  separate `discourse-solved` plugin (`PUT /solution.json`). Plugin isn't
  installed by default on bitnami; the seeder's marker is preserved in the
  Forum JSON for `--dry-run` artifacts and future fixture replay.
* `Post.quote_target_id` — Discourse renders quotes from BBCode markup in
  the post body (`[quote="user, post:N, topic:M"]…[/quote]`). Rewriting
  the bodies would tie the seeder to a specific Discourse markdown dialect
  and re-introduce LLM-generated content into the post-cache invalidation
  surface. The cross-reference signal is still visible to the visualizer
  via the seeder JSON.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from .discourse_api import DiscourseAPIError, DiscourseClient
from .forum import Forum, ForumExtension
from .generators.categories import generate_categories
from .generators.posts import (
    BodyProvider,
    Post,
    PostInProgress,
    _placeholder_body,
    generate_posts,
)
from .generators.tags import generate_tags
from .generators.timeline import Timeline, make_timeline
from .generators.topics import (
    Topic,
    _dedupe_titles,
    _fill_template,
    generate_topics,
)
from .generators.users import User, generate_users
from .universe import GenerationSpec


# ---------------------------------------------------------------------------
# push_forum constants (Sit 14)
# ---------------------------------------------------------------------------

# Single shared placeholder password for every seeded user. This is fixture
# data on a localhost demo — see module docstring for the full rationale.
# Length 25 ≥ Discourse's default `min_password_length` of 10.
_FIXTURE_PASSWORD = "sample-seeder-password-1234"

# Email-domain suffix for synthetic users. `.local` is RFC-6762-reserved
# and won't deliver actual mail; that's the point — these are synthetic
# accounts on a test forum.
_FIXTURE_EMAIL_DOMAIN = "sample.local"

# Per-request delay (seconds) for every POST in the push.  Discourse's
# Rack-middleware IP rate-limiter caps unauthenticated bursts; this
# delay keeps a tiny-scale push (~150 POSTs) under that limit without
# triggering the 429-Retry-After cooldown.  Patched to 0 in unit tests
# (`tests/test_pipeline._patch_push_delays`) so the mock-driven suite
# doesn't pay the wall-clock cost.
_PUSH_REQUEST_DELAY_SECONDS = 0.4

# Discourse site settings the push tunes BEFORE its main loop.  The
# default `rate_limit_create_post=5` (seconds between posts by the same
# user) makes a Pareto-distributed seeder push (heavy posters author
# 20+ adjacent posts) take 10+ minutes at tiny scale.  We lower it to
# `1` for the duration of the push.  Critically, this setting controls
# *write-rate* only — it does NOT affect what the parent project's
# scraper reads back, so tuning it doesn't violate the spec's "don't
# change what the scraper sees" rule.  A real forum operator would
# tweak this same setting before any bulk import.
_PUSH_RATE_LIMIT_SETTINGS: dict[str, int] = {
    # Seconds between posts by the same user. Default 5; we lower to 1.
    "rate_limit_create_post": 1,
    # Seconds between topics by the same user. Default 5; same reasoning.
    "rate_limit_create_topic": 1,
    # New users default to 30s between posts (`rate_limit_new_user_create_post`).
    # All our seeded users are "new" by Discourse's reckoning (TL0 — joined
    # moments ago), so this setting drives the bottleneck even after
    # lowering `rate_limit_create_post`. Lower to 1.
    "rate_limit_new_user_create_post": 1,
    # Default rejects duplicate post bodies within 5 minutes
    # (`unique_posts_mins`). Our seeded bodies are deterministic per
    # (topic, post_number) so collisions are unlikely, but the default
    # also forbids empty posts which the placeholder body skirts. Setting
    # to 0 disables the dedup window and avoids surprises with future
    # placeholder-body changes.
    "unique_posts_mins": 0,
    # Sit 14.1 follow-up: Discourse's "new user, first day" content-policy
    # caps. Defaults are aggressively low — `max_topics_in_first_day=3`
    # and `max_replies_in_first_day=10` per new user, on a 24h rolling
    # window keyed off the *user's* `User.created_at` (i.e. NOW from the
    # seeder's perspective — the post `created_at` backdate is irrelevant
    # to this check). With 10 seeded users at tiny scale and Pareto-
    # weighted authoring, the heaviest posters trip these within the
    # first ~5 topics they author. Raise to comfortably exceed any
    # plausible scale so the per-user content policy never bites.
    #
    # NOTE: setting names go `<noun>_in_first_day`, NOT
    # `max_first_day_<noun>` — this was the source of a Sit-14.1
    # verification re-roll. The wrong-name variant silently 404'd via
    # `set_site_setting` (admin endpoint just no-ops on unknown keys),
    # so the seeder thought it had tuned them but Discourse kept the
    # default of 3.
    "max_topics_in_first_day": 1000,
    "max_replies_in_first_day": 1000,
    # `max_topics_per_day` is the GLOBAL per-user-per-day cap regardless
    # of trust level or first-day status — default 20, which matches the
    # exact failure point observed during Sit 14.1 verification (push
    # stalled at 20 topics, no other diagnostic). Without this tweak, the
    # first-day check passes but this layer kicks in immediately after.
    "max_topics_per_day": 1000,
}


def build_forum(
    spec: GenerationSpec,
    *,
    body_provider: Optional[BodyProvider] = None,
    product_name: Optional[str] = None,
) -> Forum:
    """Run every generator in dependency order and return a `Forum`.

    `product_name` is the user-facing slug (e.g. `"crown-of-brine"`) echoed
    back on `Forum.product_name`. If `None`, falls back to the imported
    product module's last-segment name (`crown_of_brine`) — fine for tests
    and REPL use, but the CLI passes the dashed slug through to keep the
    JSON dump aligned with the `--product` flag.

    The `body_provider` is forwarded verbatim to `generate_posts`. `None`
    yields placeholder bodies (matches the Sit-6 contract); any callable
    matching `Callable[[Topic, PostInProgress], str]` substitutes
    LLM-backed (or fixture / test) bodies.
    """
    categories = generate_categories(spec)
    tags = generate_tags(spec)
    users = generate_users(spec)
    timeline = make_timeline(
        spec, total_topics=spec.scale_targets()["topics"]
    )
    topics = generate_topics(spec, categories, tags, users, timeline)
    posts = generate_posts(
        spec, topics, users, timeline, body_provider=body_provider
    )

    resolved_name = (
        product_name
        if product_name is not None
        else spec.product.__name__.rsplit(".", 1)[-1]
    )

    return Forum(
        seed=spec.seed,
        scale=spec.scale,
        product_name=resolved_name,
        categories=categories,
        tags=tags,
        users=users,
        topics=topics,
        posts=posts,
    )


# ---------------------------------------------------------------------------
# push_forum (Sit 14) — wire `Forum` into a live Discourse via the API client
# ---------------------------------------------------------------------------


@dataclass
class PushResult:
    """What landed on the live Discourse instance.

    Returned by `push_forum` so callers can summarise (`cli.init`'s stdout
    line) or assert (integration tests) without re-querying the server.

    Attributes:
        category_ids: name → Discourse category id, in push order.
        user_ids: username → Discourse user id, in push order.
        topic_ids: seeded `Topic.id` → Discourse `topic_id`, in push order.
        post_count: total posts created (OPs + replies).
        errors: non-fatal issues we noted (e.g., a post that hit a 422 from
            the body-content rules and was skipped). Empty when the push
            was clean. Note: a fatal error raises through; this list is for
            the soft-skip path only.
    """

    category_ids: dict[str, int] = field(default_factory=dict)
    user_ids: dict[str, int] = field(default_factory=dict)
    topic_ids: dict[int, int] = field(default_factory=dict)
    post_count: int = 0
    errors: list[str] = field(default_factory=list)


def _email_for_username(username: str) -> str:
    """Return the synthetic email Discourse uses for `username`.

    Single source of truth so the push and any future verifier agree.
    """
    return f"{username}@{_FIXTURE_EMAIL_DOMAIN}"


def push_forum(forum: Forum, client: DiscourseClient) -> PushResult:
    """Push `forum` into the running Discourse instance via `client`.

    Order: categories → users → topics (with OPs) → replies, where the
    topics + replies loop walks `forum.topics` chronologically (the seeder
    already sorts them by `id`, which equals chronological order because
    the timeline is sorted ascending). Tags are auto-created by Discourse
    when first attached to a topic — no explicit tag-creation pass.

    See module docstring for the role-handling and accepted-solution
    decisions. Behaviour summary:

    * Every seeded user is `grant_moderator`'d immediately after creation
      (Sit 14.1 — see module docstring for the rate-limit-bypass rationale).
    * Email = `<username>@sample.local`; password = the shared fixture
      placeholder.
    * The bitnami admin (the API caller) is NOT in `forum.users`; we
      assume zero overlap with seeded usernames.
    * `Post.is_accepted_solution` and `Post.quote_target_id` are seeder-
      side metadata; not pushed to Discourse for Sit 14.

    Rate limiting: BEFORE the main loop, the push lowers Discourse's
    `rate_limit_create_post` / `rate_limit_create_topic` from 5 → 1 and
    raises the per-IP request budget; this is the same admin tweak a
    real forum operator would make ahead of a bulk import. With those
    settings, a fixed ~0.4s per-request delay clears every Discourse
    rate limiter cleanly. The settings tweak does NOT affect what the
    parent project's scraper reads on the resulting forum.

    Returns a `PushResult` with the id mappings + counts. Fatal errors
    (non-2xx that aren't soft-skipped) propagate as `DiscourseAPIError`.
    """
    result = PushResult()

    # 0. Tune rate-limit settings so the push can run at a reasonable
    # pace. Site settings are reset on `make -C sample nuke`; for a long-
    # lived forum a future Sit could revert them post-push, but for the
    # localhost test stack the defaults are restored on the next nuke
    # anyway. Errors here are NON-fatal — the seeder still works at the
    # default rate limits, just slowly. We record the failure so the
    # final summary surfaces it.
    for setting_name, setting_value in _PUSH_RATE_LIMIT_SETTINGS.items():
        try:
            client.set_site_setting(setting_name, setting_value)
        except DiscourseAPIError as exc:
            result.errors.append(
                f"failed to tune site setting {setting_name}: "
                f"{exc.status} — {exc.body[:160]}"
            )

    def _request_throttle() -> None:
        """Sleep `_PUSH_REQUEST_DELAY_SECONDS` between every push request."""
        if _PUSH_REQUEST_DELAY_SECONDS > 0:
            time.sleep(_PUSH_REQUEST_DELAY_SECONDS)

    # 1. Categories. Capture name → id; topics need this to set the
    # `category_id` on POST /posts.json.
    for category_name in forum.categories:
        _request_throttle()
        cat_id = client.create_category(category_name)
        result.category_ids[category_name] = cat_id

    # 2. Users. `display_name` becomes the Discourse `name` field;
    # `username` is the login + handle. Email is derived deterministically;
    # password is shared. Every seeded user is promoted to moderator
    # immediately after creation — see `grant_moderator` docstring +
    # `live-push-walkthrough.md` for the full rationale. Briefly: staff
    # users (admin OR mod) bypass `RateLimiter.rate_unlimited?` for every
    # per-user cap (`max_topics_in_first_day`, `max_topics_per_day`,
    # `rate_limit_create_post`, etc.), which removes the entire
    # whack-a-mole surface from the seeder. Cosmetic cost: each user
    # carries a moderator badge in the UI; acceptable on a fixture forum.
    for user in forum.users:
        _request_throttle()
        uid = client.create_user(
            user.username,
            _FIXTURE_PASSWORD,
            _email_for_username(user.username),
            name=user.display_name,
            active=True,
            approved=True,
        )
        result.user_ids[user.username] = uid
        # Promote BEFORE any topic/post is authored as this user — the
        # rate-limit bypass only applies once `staff?` is true.
        _request_throttle()
        try:
            client.grant_moderator(uid)
        except DiscourseAPIError as exc:
            # Non-fatal: if the grant fails (network blip, etc.), the
            # subsequent topic POSTs by this user will hit the per-user
            # caps and surface as soft-skipped 429 errors. Record the
            # failure so a maintainer can correlate.
            result.errors.append(
                f"failed to grant moderator to user {user.username!r} "
                f"(id={uid}): {exc.status} — {exc.body[:160]}"
            )

    # 3. Topics + posts in chronological order.
    #
    # Group posts by topic for fast lookup. The seeder generates posts in
    # post_number order within each topic (and the global `posts` list is
    # built as `[topic1_op, topic1_replies..., topic2_op, ...]`), so a single
    # pass groups them deterministically.
    posts_by_topic: dict[int, list] = {}
    for post in forum.posts:
        posts_by_topic.setdefault(post.topic_id, []).append(post)

    # Sort each topic's posts by post_number so OP is first and replies
    # follow in generation order. Defensive — already true today.
    for topic_id in posts_by_topic:
        posts_by_topic[topic_id].sort(key=lambda p: p.post_number)

    # forum.topics is already sorted chronologically (id == chronological
    # order via the sorted timeline). Sorting by created_at as well is a
    # safety net for hypothetical future changes.
    chronological_topics = sorted(
        forum.topics, key=lambda t: (t.created_at, t.id)
    )

    for topic in chronological_topics:
        topic_posts = posts_by_topic.get(topic.id, [])
        if not topic_posts:
            # Defensive: the post generator always emits at least an OP per
            # topic, so this should never fire. Skip with a recorded error
            # rather than KeyError-ing on the next index.
            result.errors.append(
                f"topic {topic.id} ({topic.title!r}) has no OP; skipped"
            )
            continue

        op_post = topic_posts[0]
        if op_post.post_number != 1:
            result.errors.append(
                f"topic {topic.id}: first post has post_number "
                f"{op_post.post_number}, expected 1"
            )

        category_id = result.category_ids.get(topic.category)
        if category_id is None:
            # Same defensive posture: a topic referencing a category that
            # didn't get created is a generator-side bug, not a push-side
            # one. Record and skip the topic so the push continues.
            result.errors.append(
                f"topic {topic.id}: category {topic.category!r} "
                f"missing from category_ids; skipped"
            )
            continue

        _request_throttle()
        topic_response = client.create_topic(
            title=topic.title,
            raw=op_post.body,
            category_id=category_id,
            tags=list(topic.tags) if topic.tags else None,
            created_at=topic.created_at,
            author_username=topic.author_username,
        )
        discourse_topic_id = topic_response["topic_id"]
        result.topic_ids[topic.id] = discourse_topic_id
        result.post_count += 1  # the OP

        # Build a post_number → seeded post lookup so reply parents map
        # cleanly. Within a topic, the seeder's `post_number` IS the
        # Discourse `post_number` because Discourse assigns `post_number`
        # in arrival order starting at 1 — and we POST replies in the same
        # order the seeder generated them.
        seeded_by_post_number: dict[int, object] = {
            p.post_number: p for p in topic_posts
        }

        for post in topic_posts[1:]:  # replies only
            # Map parent_post_id (a global seeded post id) to the seeder's
            # post_number within this topic — which equals Discourse's
            # post_number, since we author replies in order.
            reply_to_post_number: Optional[int] = None
            if post.parent_post_id is not None:
                # Find the parent within this topic's seeded posts.
                parent = next(
                    (
                        p
                        for p in topic_posts
                        if p.id == post.parent_post_id
                    ),
                    None,
                )
                # Pass `reply_to_post_number` only when the parent isn't
                # the OP — Discourse implicitly treats every reply with no
                # `reply_to_post_number` as a reply to the OP.
                if parent is not None and parent.post_number != 1:
                    reply_to_post_number = parent.post_number
                # Sanity-check the lookup table agrees.
                if (
                    parent is not None
                    and seeded_by_post_number.get(parent.post_number) is not parent
                ):
                    # Should never trigger; if it does, log and continue.
                    result.errors.append(
                        f"topic {topic.id} post {post.post_number}: "
                        f"parent post_number lookup mismatch"
                    )

            _request_throttle()
            try:
                client.create_post(
                    topic_id=discourse_topic_id,
                    raw=post.body,
                    reply_to_post_number=reply_to_post_number,
                    created_at=post.created_at,
                    author_username=post.author_username,
                )
                result.post_count += 1
            except DiscourseAPIError as exc:
                # Soft-skip a single post if Discourse rejects it (e.g.
                # body too short after a regen, anti-spam heuristic). The
                # rest of the push continues; the error is recorded so
                # `cli.init`'s summary surfaces it.
                result.errors.append(
                    f"topic {topic.id} post {post.post_number}: "
                    f"{exc.status} — {exc.body[:200]}"
                )

    return result


# ---------------------------------------------------------------------------
# extend_forum (Sit 15) — produce a deterministic ForumExtension
# ---------------------------------------------------------------------------


# How many days of "post-base" timeline window the extension spans. Topics
# get uniform-random offsets in `[1, _EXTENSION_WINDOW_DAYS]` (so timestamps
# are strictly AFTER `base_end_ts`, never equal). 30 days is enough headroom
# for a few extension batches without crossing into a hypothetical "next
# year's" release-event territory; tunable later if `--add-topics` grows.
_EXTENSION_WINDOW_DAYS = 30

# Sit 16 — appended-reply timing. Each appended reply gets `created_at =
# previous-on-this-topic + uniform(MIN, MAX)` minutes. The window mirrors
# `generate_posts._REPLY_DELTA_{MIN,MAX}_MINUTES` so the extension's
# inter-reply gaps look like the base's. The values are duplicated rather
# than imported because `generate_posts` exposes them as private constants;
# diverging is acceptable since the base generator's window is deliberately
# wide already (5 minutes ↔ 10 days).
_APPENDED_REPLY_DELTA_MIN_MINUTES = 5
_APPENDED_REPLY_DELTA_MAX_MINUTES = 14400

# Sit 16 — parent-pick policy for appended replies, mirroring
# `generate_posts._REPLY_TO_OP_PROBABILITY`. 80% of appended replies reply
# directly to the OP; 20% reply to a randomly-picked earlier post on the
# same topic (chosen from base + extension posts visible at the time).
_APPENDED_REPLY_TO_OP_PROBABILITY = 0.80


# Sit 17 — release-burst constants. The burst is a tight cluster of
# topics + replies all dated within a 7-day window centred on a fictional
# in-fiction release date, tagged with `<version>` plus a weighted draw
# from `[bug, feature-request, hint-needed]` so the cluster reads as
# "release-day chatter". Counts are drawn from the extend_seed rng
# within the [10, 20] / [50, 100] ranges so distinct seeds produce
# distinct-but-bounded burst sizes.
_RELEASE_BURST_WINDOW_DAYS = 7

# Release date offset (in days) past `base_end_ts`. Picked uniformly so
# the burst doesn't always sit immediately on top of the base end. Lower
# bound = half the burst window (so the burst's leading edge is strictly
# after the base) plus a small buffer.
_RELEASE_BURST_OFFSET_MIN_DAYS = 7
_RELEASE_BURST_OFFSET_MAX_DAYS = 23

# Topic + reply count windows.
_RELEASE_BURST_TOPICS_MIN = 10
_RELEASE_BURST_TOPICS_MAX = 20
_RELEASE_BURST_REPLIES_MIN = 50
_RELEASE_BURST_REPLIES_MAX = 100

# Per-burst-topic category bias. Weighted toward release-relevant
# categories (bug reports, help threads, announcements). Other base
# categories fall back to the default weight so the bias degrades
# gracefully if a seed dropped one of the biased categories from the
# pool. Numbers are integer to mirror `topics._category_weights`.
_RELEASE_BURST_CATEGORY_WEIGHTS: dict[str, int] = {
    "Bug Reports": 4,
    "Help & Hints": 3,
    "Announcements": 2,
}
_RELEASE_BURST_CATEGORY_FALLBACK_WEIGHT = 1

# Per-burst-topic "type"-axis tag bias. The version tag is mandatory
# (drawn from the `--release-burst` argument); each topic also gets ONE
# of these to make the cluster read as a release-day issue stream. Sums
# to 100 by convention but `rng.weighted` doesn't require it.
_RELEASE_BURST_TYPE_TAG_WEIGHTS: dict[str, int] = {
    "bug": 60,
    "feature-request": 25,
    "hint-needed": 15,
}

# Probability a burst topic carries a third (extra) tag drawn from its
# category's affinity axes. 50% gives some topics a richer tag set
# without making every burst topic look identical.
_RELEASE_BURST_EXTRA_TAG_PROBABILITY = 0.50


def _make_extension_timeline(
    extend_spec: GenerationSpec,
    base_forum: Forum,
    add_topics_n: int,
) -> Timeline:
    """Build a `Timeline` whose topic timestamps fall after the base bake's.

    Used by `extend_forum`. The timeline's `base_epoch` is set to the
    base bake's last post timestamp, so `timestamp_for_topic(idx) =
    base_epoch + offsets[idx] days` lands strictly after the base. Offsets
    are drawn uniformly from `[1, _EXTENSION_WINDOW_DAYS]` (no burst
    biasing — extension topics aren't anchored on the base's release
    events).

    `Timeline` requires `release_events` to satisfy the same validator as
    the init path (≥3 distinct days in `[0, 365]`). We pass through the
    product's `RELEASE_EVENTS` unchanged; they're not consumed by
    `generate_topics`/`generate_posts` and exist on the timeline only for
    `is_burst_window` callers that don't run on this path. The base epoch
    being a year ahead of the original puts those events in
    "next year's calendar" — semantically harmless for our usage.
    """
    if not base_forum.posts:
        # `build_forum` always emits at least 1 post per topic and at least
        # one topic at every scale, so this branch is defensive.
        raise ValueError(
            "extend_forum: base forum has no posts; cannot derive end timestamp"
        )
    if add_topics_n <= 0:
        raise ValueError(
            f"extend_forum: add_topics_n must be positive, got {add_topics_n}"
        )

    base_end_ts = max(p.created_at for p in base_forum.posts)
    rng = extend_spec.rng("extension-timeline")

    offsets = sorted(
        rng.pick_int(1, _EXTENSION_WINDOW_DAYS) for _ in range(add_topics_n)
    )

    return Timeline(
        base_epoch=base_end_ts,
        total_topics=add_topics_n,
        release_events=tuple(extend_spec.product.RELEASE_EVENTS),
        _offsets=tuple(offsets),
    )


def _generate_appended_replies(
    base_forum: Forum,
    extend_spec: GenerationSpec,
    *,
    add_replies_n: int,
    post_id_start: int,
    body_provider: Optional[BodyProvider] = None,
) -> list[Post]:
    """Sit 16 — append `add_replies_n` replies to BASE topics.

    Topics are picked with replacement, weighted by existing reply count
    (more-active topics keep getting replies — matches "thread revival on
    a busy topic" semantics). Authors are picked weighted by
    `User.activity_weight` so the Pareto distribution that drove the
    original bake also drives the appended replies.

    Per-topic clock: each base topic carries a "next available timestamp"
    initialised to `max(base_end_ts, max(p.created_at for p in topic.posts))`
    — i.e., strictly after the global base end so the extension invariant
    holds even for topics whose latest post pre-dates `base_end_ts`. Each
    appended reply advances its topic's clock by `uniform(MIN, MAX)` minutes.

    Parent-pick policy mirrors `generate_posts`: 80% reply to the OP, 20%
    reply to a randomly-chosen earlier post on the same topic (drawing from
    base posts plus any earlier appended replies on that topic — appended
    replies authored within this same call ARE valid parents).

    `quote_target_id` is set to `None` for appended replies. Quote
    cross-references for the visualizer are still surfaced via base posts'
    existing fields; we keep this code path simple.

    Determinism: same `(base_forum, extend_spec, add_replies_n,
    post_id_start, body_provider)` -> identical output. The
    `extend_spec.rng("appended-replies")` stream advances exactly once per
    decision (topic, author, parent-roll, parent-pick-or-OP, delta) so a
    future change in any one branch doesn't shift unrelated draws.
    """
    if add_replies_n <= 0:
        return []
    if not base_forum.topics:
        raise ValueError(
            "appended replies: base forum has no topics to attach to"
        )

    base_end_ts = max(p.created_at for p in base_forum.posts)
    rng = extend_spec.rng("appended-replies")

    # Group base posts by topic for fast per-topic lookup. Sort each list by
    # post_number so `topic_posts[0]` is the OP.
    posts_by_topic: dict[int, list[Post]] = {}
    for p in base_forum.posts:
        posts_by_topic.setdefault(p.topic_id, []).append(p)
    for tid in posts_by_topic:
        posts_by_topic[tid].sort(key=lambda p: p.post_number)

    # Topics + activity weights (= existing post count). Skip topics with
    # zero base posts — `build_forum` always emits at least an OP per topic
    # so this is defensive.
    candidate_topics = [
        t for t in base_forum.topics if posts_by_topic.get(t.id)
    ]
    if not candidate_topics:
        raise ValueError(
            "appended replies: every base topic has zero posts (impossible)"
        )
    weights = [
        len(posts_by_topic[t.id]) for t in candidate_topics
    ]

    user_list = list(base_forum.users)
    user_weights = [u.activity_weight for u in user_list]
    if not any(w > 0 for w in user_weights):
        raise ValueError(
            "appended replies: every user has zero activity_weight"
        )

    # Per-topic running state: next post_number to use, and the topic's
    # clock (must be ≥ base_end_ts to keep the post-base invariant).
    next_post_number: dict[int, int] = {
        t.id: max(p.post_number for p in posts_by_topic[t.id]) + 1
        for t in candidate_topics
    }
    topic_clock: dict[int, "datetime"] = {
        t.id: max(
            max(p.created_at for p in posts_by_topic[t.id]),
            base_end_ts,
        )
        for t in candidate_topics
    }
    # Track appended replies per-topic so a later reply on the same topic
    # can pick an earlier appended reply as a parent (matches in-base
    # "reply to a recent reply" semantics).
    appended_by_topic: dict[int, list[Post]] = {
        t.id: [] for t in candidate_topics
    }

    new_replies: list[Post] = []
    next_post_id = post_id_start

    for _ in range(add_replies_n):
        topic = rng.weighted(candidate_topics, weights)
        author = rng.weighted(user_list, user_weights)

        # Parent-pick: full visible post list = base posts on this topic
        # plus appended replies authored earlier in this same loop.
        topic_posts: list[Post] = (
            posts_by_topic[topic.id] + appended_by_topic[topic.id]
        )
        op = topic_posts[0]
        non_op = topic_posts[1:]
        parent_choice_roll = rng.pick_int(0, 99)
        if (
            parent_choice_roll
            >= int(_APPENDED_REPLY_TO_OP_PROBABILITY * 100)
            and non_op
        ):
            parent = rng.pick_one(non_op)
        else:
            parent = op

        # Advance the topic's clock; reply timestamp is strictly after the
        # previous reply on this topic AND strictly after base_end_ts (the
        # max() in the topic_clock initialisation guarantees the latter).
        delta_minutes = rng.pick_int(
            _APPENDED_REPLY_DELTA_MIN_MINUTES,
            _APPENDED_REPLY_DELTA_MAX_MINUTES,
        )
        new_ts = topic_clock[topic.id] + timedelta(minutes=delta_minutes)
        topic_clock[topic.id] = new_ts

        post_number = next_post_number[topic.id]
        next_post_number[topic.id] = post_number + 1

        # Body via the provided callable or a placeholder. The
        # PostInProgress snapshot mirrors what generate_posts builds so a
        # caller's body_provider sees the same shape across init and
        # extend.
        in_progress = PostInProgress(
            topic_id=topic.id,
            post_number=post_number,
            parent_post_id=parent.id,
            author_username=author.username,
            created_at=new_ts,
            quote_target_id=None,
        )
        body = (
            body_provider(topic, in_progress)
            if body_provider is not None
            else _placeholder_body(topic.id, post_number)
        )

        reply = Post(
            id=next_post_id,
            topic_id=topic.id,
            post_number=post_number,
            parent_post_id=parent.id,
            author_username=author.username,
            body=body,
            created_at=new_ts,
            is_accepted_solution=False,
            quote_target_id=None,
        )
        new_replies.append(reply)
        appended_by_topic[topic.id].append(reply)
        next_post_id += 1

    return new_replies


def _generate_release_burst(
    base_forum: Forum,
    extend_spec: GenerationSpec,
    *,
    version: str,
    topic_id_offset: int,
    post_id_start: int,
    seen_titles: dict[str, int],
    body_provider: Optional[BodyProvider] = None,
) -> tuple[list[Topic], list[Post]]:
    """Sit 17 — generate a release-burst cluster (topics + replies).

    Returns `(burst_topics, burst_posts)` where every artefact is dated
    within ±`_RELEASE_BURST_WINDOW_DAYS / 2` days of a fictional release
    date that itself sits 7-23 days past the base bake's last timestamp.

    Topics:
        * Count drawn via `extend_spec.rng("release-burst-topics")` in
          `[_RELEASE_BURST_TOPICS_MIN, _RELEASE_BURST_TOPICS_MAX]`.
        * Categories weighted by `_RELEASE_BURST_CATEGORY_WEIGHTS` toward
          the Bug Reports / Help & Hints / Announcements trio (the natural
          fit for release-day chatter); other base categories take the
          fallback weight so the bias doesn't crowd them out entirely.
        * Tags = {`<version>`, one of `[bug, feature-request,
          hint-needed]` per `_RELEASE_BURST_TYPE_TAG_WEIGHTS`, optional
          extra from the chosen category's affinity axes (50%)}.
        * Titles slot-filled via `_fill_template` (re-uses init's machinery).
        * Authors picked via `User.activity_weight` Pareto.
        * Timestamps drawn uniformly from `[release_date - half_window,
          release_date + half_window]` then sorted; ids assigned in
          chronological order.

    Replies:
        * Count drawn via `extend_spec.rng("release-burst-posts")` in
          `[_RELEASE_BURST_REPLIES_MIN, _RELEASE_BURST_REPLIES_MAX]`.
        * Distributed across burst topics via Pareto-weighted random
          picks — one or two topics carry most of the chatter (matches
          "the bug thread on the new patch blows up while the hint
          thread stays small"), tail topics get 0-1 replies.
        * Per-reply timestamp drawn uniformly in `[topic.created_at +
          60s, release_date + half_window]` then sorted ascending per
          topic so post_number ordering is monotonic in time.
        * Parent-pick: 80% reply to OP, 20% to an earlier reply on the
          same topic (mirrors `generate_posts`).
        * `quote_target_id=None` (matches Sit 16 appended-replies).

    Determinism: same `(base_forum, extend_spec, version, topic_id_offset,
    post_id_start, seen_titles, body_provider)` -> bit-for-bit identical
    output. Two distinct `extend_spec.rng(...)` streams are used
    (`"release-burst-topics"` for the topic-construction draws and
    `"release-burst-posts"` for the per-reply draws) so a future change
    to one doesn't shift the other.
    """
    if not version:
        raise ValueError(
            "release-burst: version must be a non-empty string"
        )
    product = extend_spec.product
    valid_versions = product.TAG_POOL_BY_AXIS.get("game-version", [])
    if version not in valid_versions:
        raise ValueError(
            f"release-burst: version {version!r} not in game-version axis "
            f"{sorted(valid_versions)}"
        )
    # `game-version` axis is fully reserved by `generators/tags.py` (every
    # one of game-1/game-2/game-3/remaster always lands in `base.tags`),
    # so the version tag is guaranteed present in `base_forum.tags`. Defensive
    # check in case the reservation rule changes.
    if version not in base_forum.tags:
        raise ValueError(
            f"release-burst: version tag {version!r} not in base.tags — "
            "tag generator dropped a core game-version reservation"
        )
    if not base_forum.posts:
        raise ValueError("release-burst: base forum has no posts")
    if not base_forum.users:
        raise ValueError("release-burst: base forum has no users")

    rng_topics = extend_spec.rng("release-burst-topics")
    rng_posts = extend_spec.rng("release-burst-posts")

    base_end_ts = max(p.created_at for p in base_forum.posts)
    offset_days = rng_topics.pick_int(
        _RELEASE_BURST_OFFSET_MIN_DAYS, _RELEASE_BURST_OFFSET_MAX_DAYS
    )
    release_date = base_end_ts + timedelta(days=offset_days)
    half_window = timedelta(days=_RELEASE_BURST_WINDOW_DAYS / 2)
    window_start = release_date - half_window
    window_end = release_date + half_window
    half_window_minutes = int(half_window.total_seconds() // 60)

    topic_count = rng_topics.pick_int(
        _RELEASE_BURST_TOPICS_MIN, _RELEASE_BURST_TOPICS_MAX
    )
    reply_count = rng_posts.pick_int(
        _RELEASE_BURST_REPLIES_MIN, _RELEASE_BURST_REPLIES_MAX
    )

    cat_list: list[str] = list(base_forum.categories)
    cat_weights = [
        _RELEASE_BURST_CATEGORY_WEIGHTS.get(
            c, _RELEASE_BURST_CATEGORY_FALLBACK_WEIGHT
        )
        for c in cat_list
    ]

    user_list: list[User] = list(base_forum.users)
    user_weights = [u.activity_weight for u in user_list]
    if not any(w > 0 for w in user_weights):
        raise ValueError(
            "release-burst: every user has zero activity_weight"
        )
    username_by_user = {u.username: u for u in user_list}

    type_tag_pool = sorted(_RELEASE_BURST_TYPE_TAG_WEIGHTS)
    type_tag_weights = [
        _RELEASE_BURST_TYPE_TAG_WEIGHTS[t] for t in type_tag_pool
    ]

    # --- Build burst topics, sort by ts, assign ids in chronological order ---
    raw_topics: list[tuple[datetime, str, str, list[str], str]] = []
    for _ in range(topic_count):
        category = rng_topics.weighted(cat_list, cat_weights)
        templates = product.TITLE_TEMPLATES_BY_CATEGORY[category]
        template = rng_topics.pick_one(sorted(templates))
        title = _fill_template(template, rng_topics, product, base_forum.tags)
        type_tag = rng_topics.weighted(type_tag_pool, type_tag_weights)

        # Optional extra tag from the chosen category's affinity axes.
        # Exclude every `game-version` tag other than the one we asked for
        # so the cluster's version signal stays unambiguous (no burst
        # topic for `game-1` also carrying `remaster`, which would dilute
        # the visualizer's tag-density signal).
        affinity_axes = product.TAG_AFFINITY_BY_CATEGORY.get(category, [])
        extra_pool: set[str] = set()
        for axis in affinity_axes:
            extra_pool.update(product.TAG_POOL_BY_AXIS.get(axis, []))
        extra_pool &= set(base_forum.tags)
        extra_pool -= {version, type_tag}
        extra_pool -= set(product.TAG_POOL_BY_AXIS.get("game-version", []))
        extra_candidates = sorted(extra_pool)
        # Roll always happens so a future reshuffle doesn't shift the
        # subsequent rng advances when the candidate pool is empty.
        extra_roll = rng_topics.pick_int(0, 99)
        extra_tag: Optional[str] = None
        if (
            extra_candidates
            and extra_roll < int(_RELEASE_BURST_EXTRA_TAG_PROBABILITY * 100)
        ):
            extra_tag = rng_topics.pick_one(extra_candidates)

        burst_tags = sorted({version, type_tag} | ({extra_tag} if extra_tag else set()))
        author = rng_topics.weighted(user_list, user_weights)

        # Timestamp uniform in [release_date - half_window, release_date + half_window].
        delta_minutes = rng_topics.pick_int(
            -half_window_minutes, half_window_minutes
        )
        ts = release_date + timedelta(minutes=delta_minutes)
        raw_topics.append((ts, title, category, burst_tags, author.username))

    raw_topics.sort(key=lambda r: r[0])

    pre_dedup_topics: list[Topic] = [
        Topic(
            id=idx + 1 + topic_id_offset,
            title=title,
            category=category,
            tags=tags,
            author_username=author_username,
            created_at=ts,
        )
        for idx, (ts, title, category, tags, author_username) in enumerate(
            raw_topics
        )
    ]
    burst_topics = _dedupe_titles(pre_dedup_topics, seen_titles=seen_titles)

    # --- Distribute replies across burst topics, then assemble Post objects ---
    pareto_weights = [rng_posts.pareto(1.16) for _ in burst_topics]

    # Per-reply assignment: (topic_index, ts). Each reply lands within
    # [topic.created_at + 60s, window_end] so the 7-day-window invariant
    # holds for replies too. Late-window topics get a vanishingly small
    # span; we floor the upper offset to lo+60s so `pick_int` never sees
    # `lo > hi`.
    reply_records: list[tuple[int, datetime]] = []
    for _ in range(reply_count):
        topic_idx = rng_posts.weighted(
            list(range(len(burst_topics))), pareto_weights
        )
        topic = burst_topics[topic_idx]
        earliest = topic.created_at + timedelta(seconds=60)
        latest_candidate = window_end
        if latest_candidate <= earliest:
            latest_candidate = earliest + timedelta(seconds=60)
        span_seconds = max(
            1, int((latest_candidate - earliest).total_seconds())
        )
        offset_seconds = rng_posts.pick_int(0, span_seconds)
        ts = earliest + timedelta(seconds=offset_seconds)
        reply_records.append((topic_idx, ts))

    replies_by_topic_idx: dict[int, list[datetime]] = {}
    for topic_idx, ts in reply_records:
        replies_by_topic_idx.setdefault(topic_idx, []).append(ts)
    for topic_idx in replies_by_topic_idx:
        replies_by_topic_idx[topic_idx].sort()

    # Assemble: emit OP per topic in topic-id order, then per-topic
    # replies (the test suite doesn't require strict global chronological
    # order across the post list, but per-topic post_number must be
    # monotonic in `created_at` — sorting per-topic above guarantees that).
    burst_posts: list[Post] = []
    next_post_id = post_id_start
    posts_by_topic_id: dict[int, list[Post]] = {}

    for topic in burst_topics:
        op_author = username_by_user[topic.author_username]
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
        burst_posts.append(op)
        posts_by_topic_id[topic.id] = [op]
        next_post_id += 1

    for topic_idx, topic in enumerate(burst_topics):
        sorted_reply_ts = replies_by_topic_idx.get(topic_idx, [])
        for ts in sorted_reply_ts:
            existing = posts_by_topic_id[topic.id]
            op = existing[0]
            non_op = existing[1:]
            parent_choice_roll = rng_posts.pick_int(0, 99)
            if (
                parent_choice_roll
                >= int(_APPENDED_REPLY_TO_OP_PROBABILITY * 100)
                and non_op
            ):
                parent = rng_posts.pick_one(non_op)
            else:
                parent = op
            author = rng_posts.weighted(user_list, user_weights)
            post_number = len(existing) + 1

            in_progress = PostInProgress(
                topic_id=topic.id,
                post_number=post_number,
                parent_post_id=parent.id,
                author_username=author.username,
                created_at=ts,
                quote_target_id=None,
            )
            body = (
                body_provider(topic, in_progress)
                if body_provider is not None
                else _placeholder_body(topic.id, post_number)
            )
            reply = Post(
                id=next_post_id,
                topic_id=topic.id,
                post_number=post_number,
                parent_post_id=parent.id,
                author_username=author.username,
                body=body,
                created_at=ts,
                is_accepted_solution=False,
                quote_target_id=None,
            )
            burst_posts.append(reply)
            posts_by_topic_id[topic.id].append(reply)
            next_post_id += 1

    return burst_topics, burst_posts


def extend_forum(
    base_spec: GenerationSpec,
    *,
    add_topics_n: int = 0,
    add_replies_n: int = 0,
    release_burst_version: Optional[str] = None,
    extend_seed: int,
    body_provider: Optional[BodyProvider] = None,
    base_product_name: Optional[str] = None,
) -> ForumExtension:
    """Run the base bake, then produce post-base topics + appended replies.

    Composable: pass `add_topics_n=N` (Sit 15), `add_replies_n=M`
    (Sit 16), `release_burst_version="<version>"` (Sit 17), or any
    combination. At least one must be active — an extension that
    produces nothing is a programming error, not a meaningful no-op.

    Mode interactions are intentionally orthogonal:
      * appended replies attach to BASE topics only, never to the
        extension's own new topics. The visualizer's "thread revival"
        signal stays distinct from the "fresh thread" signal.
      * the release-burst cluster is self-contained: its replies attach
        to its own burst topics, not to base topics or `--add-topics`
        topics. The 7-day-window density signal would otherwise be
        diluted by replies on unrelated threads.

    Release-burst mode (Sit 17): when `release_burst_version` is set
    (must be one of `product.TAG_POOL_BY_AXIS["game-version"]`), the
    extension also emits a tight cluster of 10-20 topics + 50-100
    replies dated within a 7-day window centred on a fictional release
    date 7-23 days past the base bake's last timestamp. Every burst
    topic carries the `<version>` tag plus a weighted draw from
    `[bug, feature-request, hint-needed]`.

    Determinism: same `(base_spec, add_topics_n, add_replies_n,
    release_burst_version, extend_seed, body_provider)` -> bit-for-bit
    identical `ForumExtension`. The base forum is re-baked from
    `base_spec` (no live state read) so the function is fully offline.

    Reused base entities: `base_forum.categories`, `base_forum.tags`,
    and `base_forum.users` flow into the extension unchanged. The
    extension does NOT generate fresh categories/users/tags.

    Id continuity: extension topic ids start at
    `max(base.topics.id) + 1`. Extension post ids start at
    `max(base.posts.id) + 1` and run continuously across both flavours
    (posts for new topics first, then appended replies on base topics)
    so the JSON dump's `new_posts` list has unique ids end-to-end.

    Title dedup (`add_topics_n` only): extension topic titles are
    deduped against base titles — a base title appearing in the
    extension would trigger Discourse's `title_has_already_been_used`
    rejection.

    Reply density: extension topics use `base_spec`'s scale via
    `_mean_replies_per_topic`. Appended replies use a fixed Poisson-
    free per-call schedule (`add_replies_n` total replies, distributed
    across base topics weighted by their existing post count), so the
    `--scale` flag doesn't affect appended-reply count.

    `body_provider` is forwarded to BOTH generation passes (extension
    topic posts AND appended replies). `None` keeps placeholder bodies.

    `base_product_name` echoes through to
    `ForumExtension.base_product_name`; `None` falls back to the
    imported product module's last segment, same convention as
    `build_forum`.
    """
    if add_topics_n < 0:
        raise ValueError(
            f"extend_forum: add_topics_n must be non-negative, got {add_topics_n}"
        )
    if add_replies_n < 0:
        raise ValueError(
            f"extend_forum: add_replies_n must be non-negative, got {add_replies_n}"
        )
    has_burst = bool(release_burst_version)
    if (
        add_topics_n + add_replies_n <= 0
        and not has_burst
    ):
        raise ValueError(
            "extend_forum: at least one of add_topics_n, add_replies_n, "
            "or release_burst_version must be active — an extension with "
            "none of them produces nothing"
        )

    base = build_forum(
        base_spec,
        body_provider=None,  # we never need bodies for the base re-bake
        product_name=base_product_name,
    )

    extend_spec = GenerationSpec(
        seed=extend_seed,
        scale=base_spec.scale,
        product=base_spec.product,
    )

    base_topic_max_id = max((t.id for t in base.topics), default=0)
    base_post_max_id = max((p.id for p in base.posts), default=0)
    seen_titles_from_base: dict[str, int] = {t.title: 1 for t in base.topics}

    new_topics: list = []
    new_posts: list = []

    if add_topics_n > 0:
        ext_timeline = _make_extension_timeline(
            extend_spec, base, add_topics_n
        )
        new_topics = generate_topics(
            extend_spec,
            base.categories,
            base.tags,
            base.users,
            ext_timeline,
            count=add_topics_n,
            id_offset=base_topic_max_id,
            seen_titles=seen_titles_from_base,
            skip_cluster_anchors=True,
        )
        new_posts = generate_posts(
            extend_spec,
            new_topics,
            base.users,
            ext_timeline,
            body_provider=body_provider,
            id_offset=base_post_max_id,
            skip_cluster_anchors=True,
        )

    if has_burst:
        # `seen_titles` for burst dedup includes BOTH base titles and any
        # `--add-topics` titles produced just above so a mixed-mode run
        # can't introduce a duplicate Discourse-side. Pass a fresh dict
        # — `_dedupe_titles` copies internally but we want to be explicit
        # about not aliasing the per-run snapshot.
        burst_seen_titles = {
            **seen_titles_from_base,
            **{t.title: 1 for t in new_topics},
        }
        burst_topic_id_offset = base_topic_max_id + len(new_topics)
        burst_post_id_start = (
            (max(p.id for p in new_posts) if new_posts else base_post_max_id)
            + 1
        )
        burst_topics, burst_posts = _generate_release_burst(
            base,
            extend_spec,
            version=release_burst_version,  # type: ignore[arg-type]
            topic_id_offset=burst_topic_id_offset,
            post_id_start=burst_post_id_start,
            seen_titles=burst_seen_titles,
            body_provider=body_provider,
        )
        new_topics = new_topics + burst_topics
        new_posts = new_posts + burst_posts

    if add_replies_n > 0:
        # Continue post-id numbering after whatever the topic-extension
        # + release-burst produced (or after base if both were skipped).
        next_post_id = (
            (max(p.id for p in new_posts) if new_posts else base_post_max_id)
            + 1
        )
        appended = _generate_appended_replies(
            base,
            extend_spec,
            add_replies_n=add_replies_n,
            post_id_start=next_post_id,
            body_provider=body_provider,
        )
        new_posts = new_posts + appended

    return ForumExtension(
        base_seed=base_spec.seed,
        base_scale=base_spec.scale,
        base_product_name=base.product_name,
        extend_seed=extend_seed,
        add_topics_n=add_topics_n,
        add_replies_n=add_replies_n,
        release_burst_version=release_burst_version,
        new_topics=new_topics,
        new_posts=new_posts,
    )


# ---------------------------------------------------------------------------
# push_extension (Sit 15.1) — wire `ForumExtension` into a live Discourse
# ---------------------------------------------------------------------------


def push_extension(
    extension: ForumExtension,
    base_forum: Forum,
    client: DiscourseClient,
) -> PushResult:
    """Push `extension` into a live Discourse instance.

    Unlike `push_forum`, this does NOT create categories or users —
    they're presumed already-pushed by the original `init`. Instead it:

    1. GETs `/categories.json` to map base category names → live ids.
    2. GETs `/c/<slug>/<id>.json?page=N` for each base category to map
       base topic titles → live topic ids (so appended replies on
       Sit-16 base topics land on the right thread).
    3. Walks `extension.new_topics` chronologically: creates each new
       topic + its OP, then its replies, exactly like `push_forum`'s
       per-topic loop.
    4. Walks the appended replies (posts whose `topic_id` references a
       BASE topic, not an extension topic) in chronological order and
       POSTs each one against the resolved live topic id.

    Rate-limit handling matches `push_forum`: site settings are tuned
    upfront; per-request throttle stays at `_PUSH_REQUEST_DELAY_SECONDS`;
    seeded users were `grant_moderator`'d during init so per-user caps
    don't bite. The bitnami admin (the API caller) is the same one that
    pushed the base; we re-run `_PUSH_RATE_LIMIT_SETTINGS` defensively
    in case someone nuked-and-rebuilt the stack between init and extend.

    Returns a `PushResult` whose `topic_ids` carries BOTH the resolved
    base-topic mapping (so a follow-up scrape can correlate seeder
    ids → live ids) AND the new extension topics, distinguishable via
    set membership against `{t.id for t in extension.new_topics}`.
    """
    result = PushResult()

    # 0. Tune rate-limit settings (defensive — same reasoning as push_forum;
    # cheap if they're already at the tuned values).
    for setting_name, setting_value in _PUSH_RATE_LIMIT_SETTINGS.items():
        try:
            client.set_site_setting(setting_name, setting_value)
        except DiscourseAPIError as exc:
            result.errors.append(
                f"failed to tune site setting {setting_name}: "
                f"{exc.status} — {exc.body[:160]}"
            )

    def _request_throttle() -> None:
        if _PUSH_REQUEST_DELAY_SECONDS > 0:
            time.sleep(_PUSH_REQUEST_DELAY_SECONDS)

    # 1. Resolve category names → live ids. Walk all categories on the
    # forum once; the seeder's category set is small (≤8 at tiny scale)
    # so the lookup is cheap. We assume names are unique within the
    # parent's flat category space (no nested same-name categories in
    # the bitnami test stack).
    _request_throttle()
    live_categories = client.list_categories()
    cat_name_to_id: dict[str, int] = {}
    cat_id_to_slug: dict[int, str] = {}
    for cat in live_categories:
        if not isinstance(cat, dict):
            continue
        name = cat.get("name")
        cid = cat.get("id")
        slug = cat.get("slug")
        if isinstance(name, str) and isinstance(cid, int):
            cat_name_to_id[name] = cid
            if isinstance(slug, str):
                cat_id_to_slug[cid] = slug

    for cat_name in base_forum.categories:
        cid = cat_name_to_id.get(cat_name)
        if cid is None:
            raise DiscourseAPIError(
                url=f"{client.base_url}/categories.json",
                status=200,
                body=(
                    f"base category {cat_name!r} not found on live forum — "
                    "did `init` finish? did the stack get nuked since?"
                ),
            )
        result.category_ids[cat_name] = cid

    # 2. Build the title → live topic id map ONLY for categories the
    # base bake actually used (any category created by the bitnami
    # default — Staff, General — is irrelevant since the seeder never
    # writes into them).
    base_title_to_live_id: dict[str, int] = {}
    needs_topic_lookup = any(
        p.topic_id not in {t.id for t in extension.new_topics}
        for p in extension.new_posts
    )
    if needs_topic_lookup:
        for cat_name in base_forum.categories:
            cid = result.category_ids[cat_name]
            slug = cat_id_to_slug.get(cid)
            if slug is None:
                # Defensive — list_categories returned a category without
                # a slug. Skip rather than fall over; the resulting map
                # may miss some base topics but the per-reply lookup
                # surfaces a clear error.
                continue
            _request_throttle()
            try:
                live_topics = client.list_topics_in_category(cid, slug)
            except DiscourseAPIError as exc:
                result.errors.append(
                    f"failed to list topics in category {cat_name!r}: "
                    f"{exc.status} — {exc.body[:160]}"
                )
                continue
            for lt in live_topics:
                title = lt.get("title")
                tid = lt.get("id")
                if isinstance(title, str) and isinstance(tid, int):
                    base_title_to_live_id[title] = tid

    # 3. Push extension topics + their posts in chronological order.
    # Reuse `push_forum`'s per-topic logic shape: OP first, then replies,
    # mapping `parent_post_id` (a global seeded id) to a live
    # `reply_to_post_number` via the per-topic post-number lookup.
    extension_topic_ids = {t.id for t in extension.new_topics}
    posts_by_topic: dict[int, list[Post]] = {}
    for post in extension.new_posts:
        posts_by_topic.setdefault(post.topic_id, []).append(post)
    for tid in posts_by_topic:
        posts_by_topic[tid].sort(key=lambda p: p.post_number)

    chronological_extension_topics = sorted(
        extension.new_topics, key=lambda t: (t.created_at, t.id)
    )

    for topic in chronological_extension_topics:
        topic_posts = posts_by_topic.get(topic.id, [])
        if not topic_posts:
            result.errors.append(
                f"extension topic {topic.id} ({topic.title!r}) has no OP; skipped"
            )
            continue
        op_post = topic_posts[0]
        if op_post.post_number != 1:
            result.errors.append(
                f"extension topic {topic.id}: first post post_number "
                f"{op_post.post_number}, expected 1"
            )

        category_id = result.category_ids.get(topic.category)
        if category_id is None:
            result.errors.append(
                f"extension topic {topic.id}: category {topic.category!r} "
                "missing from live forum; skipped"
            )
            continue

        _request_throttle()
        try:
            topic_response = client.create_topic(
                title=topic.title,
                raw=op_post.body,
                category_id=category_id,
                tags=list(topic.tags) if topic.tags else None,
                created_at=topic.created_at,
                author_username=topic.author_username,
            )
        except DiscourseAPIError as exc:
            result.errors.append(
                f"extension topic {topic.id} ({topic.title!r}): "
                f"create_topic failed {exc.status} — {exc.body[:200]}"
            )
            continue
        live_topic_id = topic_response["topic_id"]
        result.topic_ids[topic.id] = live_topic_id
        result.post_count += 1  # the OP

        for post in topic_posts[1:]:
            reply_to_post_number: Optional[int] = None
            if post.parent_post_id is not None:
                parent = next(
                    (p for p in topic_posts if p.id == post.parent_post_id),
                    None,
                )
                if parent is not None and parent.post_number != 1:
                    reply_to_post_number = parent.post_number
            _request_throttle()
            try:
                client.create_post(
                    topic_id=live_topic_id,
                    raw=post.body,
                    reply_to_post_number=reply_to_post_number,
                    created_at=post.created_at,
                    author_username=post.author_username,
                )
                result.post_count += 1
            except DiscourseAPIError as exc:
                result.errors.append(
                    f"extension topic {topic.id} post {post.post_number}: "
                    f"{exc.status} — {exc.body[:200]}"
                )

    # 4. Push appended replies on BASE topics. The seeder's `topic_id`
    # references the base bake's id, so we look up the live topic via
    # title. `reply_to_post_number` is intentionally None — the seeded
    # `parent_post_id` typically points at a base post, but we don't
    # have base posts' live `post_number` here without an extra GET per
    # topic. Discourse renders a post with no `reply_to_post_number` as
    # "reply to OP", which preserves the parent-as-OP semantics for the
    # 80% case (matching `_APPENDED_REPLY_TO_OP_PROBABILITY`). The 20%
    # non-OP-reply signal is a soft loss in live mode; the dry-run JSON
    # still records the precise parent for fixture replay.
    base_topic_id_to_title: dict[int, str] = {t.id: t.title for t in base_forum.topics}
    for post in extension.new_posts:
        if post.topic_id in extension_topic_ids:
            continue  # handled in step 3
        title = base_topic_id_to_title.get(post.topic_id)
        if title is None:
            result.errors.append(
                f"appended reply (seeded post {post.id}): base topic "
                f"{post.topic_id} not found in base bake; skipped"
            )
            continue
        live_topic_id = base_title_to_live_id.get(title)
        if live_topic_id is None:
            result.errors.append(
                f"appended reply (seeded post {post.id}): base topic "
                f"{title!r} not found on live forum; skipped"
            )
            continue
        _request_throttle()
        try:
            client.create_post(
                topic_id=live_topic_id,
                raw=post.body,
                created_at=post.created_at,
                author_username=post.author_username,
            )
            result.post_count += 1
        except DiscourseAPIError as exc:
            result.errors.append(
                f"appended reply (seeded post {post.id} on {title!r}): "
                f"{exc.status} — {exc.body[:200]}"
            )

    return result
