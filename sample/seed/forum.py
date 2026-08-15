"""Forum aggregate — the JSON-serialisable bake-result.

The CLI's `init --dry-run` runs every Phase-1 generator in dependency order
and bundles the result into a `Forum`. `Forum` is intentionally a thin
container: no logic, just the fields the JSON dump needs. Phase 3's live-
Discourse pipeline will reuse it as the input shape for the Discourse API
client.

Why a dedicated dataclass instead of a plain dict: it documents the schema
in one place, plays nicely with `dataclasses.asdict` for JSON serialisation,
and is `frozen=True` so a mid-pipeline mutation can't sneak past type checks.
The fields are typed as `list[X]` (not `Sequence`) so `asdict` knows how to
walk them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .generators.posts import Post
from .generators.topics import Topic
from .generators.users import User


@dataclass(frozen=True)
class Forum:
    """Bundle of every artefact a Phase-1 bake produces.

    Attributes:
        seed: integer seed used to build the bake; echoed back so a JSON
            file is self-describing.
        scale: scale preset key (`tiny`, `small`, `medium`, `large`).
        product_name: stable identifier of the product universe used (e.g.
            `crown-of-brine`). Pinned in the JSON so a future re-bake can
            assert it's the same universe.
        categories: result of `generate_categories(spec)`.
        tags: result of `generate_tags(spec)`.
        users: result of `generate_users(spec)`.
        topics: result of `generate_topics(...)`.
        posts: result of `generate_posts(...)`.
    """

    seed: int
    scale: str
    product_name: str
    categories: list[str]
    tags: list[str]
    users: list[User]
    topics: list[Topic]
    posts: list[Post]


@dataclass(frozen=True)
class ForumExtension:
    """Bundle of artefacts an `extend` run produces (Sits 15–18).

    The extension does NOT produce new categories, tags, or users — it
    re-uses the base bake's. Only fresh `topics` + `posts` are emitted,
    with ids that pick up after the base's last id and timestamps that
    fall AFTER the base's last post timestamp. The base spec
    (`base_seed`, `base_scale`, `base_product_name`) is echoed back so
    the JSON dump can document which forum these artefacts extend.

    `new_posts` is heterogeneous: it carries both posts that belong to
    extension topics (their `topic_id` references `new_topics`) AND
    appended replies attached to base topics (their `topic_id`
    references topics in the base bake). Callers distinguish by
    checking set membership against `{t.id for t in new_topics}`.

    Attributes:
        base_seed: integer seed of the base bake this extension extends.
        base_scale: scale preset of the base bake (drives reply density).
        base_product_name: stable identifier of the product universe
            (e.g. `crown-of-brine`).
        extend_seed: integer seed used to drive the extension's RNG.
            Same `(base_seed, base_scale, base_product_name,
            extend_seed, add_topics_n, add_replies_n)` always yields a
            bit-for-bit identical extension.
        add_topics_n: number of new topics generated (Sit 15).
        add_replies_n: number of appended replies attached to BASE
            topics (Sit 16). Replies attach to base topics only, not
            to extension topics — the modes are intentionally
            orthogonal so the visualizer's "new activity on existing
            threads" signal stays distinct from "fresh threads".
        release_burst_version: when set (Sit 17), names the
            `game-version` tag the extension's burst cluster is
            anchored on (e.g. `"remaster"`). Triggers an additional
            10-20 topics + 50-100 replies dated within a 7-day window
            around a fictional release date. The burst's topics + their
            replies are mixed into `new_topics` / `new_posts`; callers
            distinguish via the version tag membership.
        new_topics: combines `--add-topics` topics (if any) and
            release-burst topics (if any). Ids run continuously from
            `max(base.topics.id) + 1`. Titles are deduped against base
            titles in both passes.
        new_posts: posts produced for extension topics, replies on
            burst topics, and appended replies on base topics — all in
            one heterogeneous list. Ids share the same namespace
            (continuous from `max(base.posts.id) + 1`) but their
            `topic_id` distinguishes them.

    `new_topics` and `new_posts` are the only side of the bake that
    needs pushing to a live Discourse — base entities are presumed
    already-pushed.
    """

    base_seed: int
    base_scale: str
    base_product_name: str
    extend_seed: int
    add_topics_n: int
    add_replies_n: int
    release_burst_version: Optional[str]
    new_topics: list[Topic]
    new_posts: list[Post]
