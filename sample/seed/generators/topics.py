"""Topic generator with cluster anchoring + slot-filled titles.

Topics are the unit of forum activity: a title, a category, a small set of
tags, an author, and a creation timestamp. The design doc's "Injected forum
dynamics" section asks for two structural signals that simple uniform
sampling would erase:

1. **Pain-point clusters survive subset draws.** Every entry of
   `CLUSTER_TAG_COMBINATIONS` must show up as the tag set of at least one
   topic — otherwise `/forum-report` can't surface the cluster. We pin the
   first `len(CLUSTER_TAG_COMBINATIONS)` topics as "anchor" topics whose
   tags are exactly the combo (sorted) and whose category is biased toward
   the most natural fit (a combo containing `bug` -> Bug Reports, etc.).
2. **Categories have visibly different tones.** Bug-report titles read like
   bug reports; lore-theory titles read like lore theories. We reach this
   by giving every category in `CATEGORY_POOL` its own template list, and
   weighting the per-topic category draw 3:1 toward the three core
   categories (Announcements / Help & Hints / Bug Reports) so the basic
   Q&A skeleton dominates regardless of how the tag draws fall.

After the cluster anchors, remaining topics:

- pick a category by weighted draw (3× weight for core, 1× for optional);
- pick one of that category's templates;
- slot-fill from `LORE_VOCAB` + `GAME_TITLES` + `MOD_TOOLS` + `{tag}`
  (drawn from the generated tag list) + `{platform}` (rendered via
  `PLATFORM_DISPLAY_NAMES`);
- draw 1–4 tags from the union of axes listed in
  `TAG_AFFINITY_BY_CATEGORY[category]`, intersected with the actually
  generated tag list;
- pick an author via `weighted` over `User.activity_weight`;
- timestamp from `timeline.timestamp_for_topic(idx)`.

Determinism: uses `spec.rng("topics")`. Output is ordered by `id` (1-indexed,
also chronological because the timeline is sorted ascending).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from ..universe import GenerationSpec
from .timeline import Timeline
from .users import User


# Weight applied to the three CORE_CATEGORIES in the per-topic category
# draw. Optional categories get weight 1. 3:1 keeps the Q&A skeleton
# (Announcements + Help & Hints + Bug Reports) dominant in the topic mix
# without crowding out the optional categories entirely.
_CORE_CATEGORY_WEIGHT = 3
_OPTIONAL_CATEGORY_WEIGHT = 1

# Tag-count window per topic. 1 tag = a casual short post; 4 = a heavily-
# tagged bug report or pain-point anchor. The cluster-combo anchors can
# exceed 4 (combos have up to 4 tags themselves) — this range only governs
# the non-anchor topics.
_MIN_TAGS_PER_TOPIC = 1
_MAX_TAGS_PER_TOPIC = 4

# Categories that are most natural for clusters containing certain tags.
# Used by `_pick_anchor_category` to bias the cluster-anchor topic into a
# category whose templates fit the combo. Order matters: earliest match
# wins, so "Bug Reports" beats "Modding & Fan Projects" when a combo
# contains both `bug` and `compass-editor`.
_ANCHOR_CATEGORY_PREFERENCES: list[tuple[str, str]] = [
    ("bug", "Bug Reports"),
    ("compass-editor", "Modding & Fan Projects"),
    ("doubloon-sdk", "Modding & Fan Projects"),
    ("modded", "Modding & Fan Projects"),
    ("voice-acting", "Voice Cast Talk"),
    ("localization", "Translations & Localization"),
    ("puzzle-design", "Lore & Theories"),
    ("lore", "Lore & Theories"),
    ("theory", "Lore & Theories"),
    ("speedrun", "Speedruns & Challenges"),
    ("fan-art", "Show & Tell"),
    ("fan-fiction", "Show & Tell"),
    ("hint-needed", "Help & Hints"),
    ("walkthrough", "Help & Hints"),
]


@dataclass(frozen=True)
class Topic:
    """A generated forum topic.

    Attributes:
        id: 1-indexed sequential ID. Also chronological because the timeline
            is sorted ascending.
        title: slot-filled template result. Contains no unfilled `{...}`
            placeholders (the generator validates).
        category: one of the categories returned by `generate_categories`.
        tags: subset of the generated tag list, length 1–4 for non-anchor
            topics. Cluster-anchor topics carry exactly the combo's tags
            (length up to 4). Sorted for deterministic equality.
        author_username: a username from the generated user list.
        created_at: timestamp from `timeline.timestamp_for_topic(idx)` where
            `idx = id - 1`.
    """

    id: int
    title: str
    category: str
    tags: list[str] = field()
    author_username: str = field()
    created_at: datetime = field()


def _pick_anchor_category(
    combo: Sequence[str], available: Sequence[str]
) -> str:
    """Pick the most natural category for a cluster combo.

    Walks `_ANCHOR_CATEGORY_PREFERENCES` in order; returns the first matching
    (tag-in-combo, category) pair where the category is in `available`. Falls
    back to the first available category otherwise — defensive, only fires if
    the seed happened to pick zero matching optional categories.
    """
    available_set = set(available)
    for tag, preferred in _ANCHOR_CATEGORY_PREFERENCES:
        if tag in combo and preferred in available_set:
            return preferred
    # Fallback: first available category (sorted for determinism, but the
    # caller passes them in generator order which is already deterministic).
    return available[0]


def _platform_display(tag: str, product) -> str:
    """Render a platform tag for `{platform}` slot-fill.

    Looks up `PLATFORM_DISPLAY_NAMES` if defined; falls back to a plain
    title-case rendering otherwise. A missing entry is a soft failure — we
    don't want a future product without a display map to fail at title
    generation time.
    """
    overrides = getattr(product, "PLATFORM_DISPLAY_NAMES", {})
    if tag in overrides:
        return overrides[tag]
    # Plain title-casing per hyphen-segment, then space-join. Mirrors the
    # display-name logic in `users.py`.
    return " ".join(seg.capitalize() for seg in tag.split("-"))


def _fill_template(
    template: str,
    rng,
    product,
    generated_tags: Sequence[str],
) -> str:
    """Fill every `{slot}` in `template`.

    Slots are pulled from `LORE_VOCAB`, `GAME_TITLES`, `MOD_TOOLS`, the
    generated tag list (for `{tag}`), and the platform-tag axis (for
    `{platform}`). Each slot draw is one rng call against a sorted list so
    determinism survives a future dict-ordering change in product constants.
    """
    lore = product.LORE_VOCAB
    games = list(product.GAME_TITLES)
    mod_tools = list(product.MOD_TOOLS)
    platform_tags = sorted(product.TAG_POOL_BY_AXIS["platform"])
    # Plain `format_map` would crash on a missing key — but we want the
    # generator to surface a malformed template explicitly via a clear
    # error rather than a KeyError. So we do the substitution explicitly.
    result = template
    # Process slots until none remain. A bounded loop guards against
    # accidentally introducing a slot whose fill value re-introduces a
    # `{...}` (it shouldn't — vocab strings don't contain braces — but the
    # bound is cheap insurance).
    max_iters = 16
    for _ in range(max_iters):
        if "{" not in result:
            break
        # Find the next `{slot}`.
        start = result.find("{")
        end = result.find("}", start + 1)
        if end == -1:
            break
        slot = result[start + 1 : end]
        if slot == "puzzle":
            value = rng.pick_one(sorted(lore["puzzles"]))
        elif slot == "chapter":
            value = rng.pick_one(sorted(lore["chapters"]))
        elif slot == "location":
            value = rng.pick_one(sorted(lore["locations"]))
        elif slot == "character_archetype":
            value = rng.pick_one(sorted(lore["character_archetypes"]))
        elif slot == "item":
            value = rng.pick_one(sorted(lore["items"]))
        elif slot == "verb":
            value = rng.pick_one(sorted(lore["verbs"]))
        elif slot == "asset_type":
            value = rng.pick_one(sorted(lore["asset_types"]))
        elif slot == "game":
            value = rng.pick_one(games)
        elif slot == "tool":
            value = rng.pick_one(mod_tools)
        elif slot == "platform":
            value = _platform_display(rng.pick_one(platform_tags), product)
        elif slot == "tag":
            if not generated_tags:
                raise ValueError(
                    "topic-title generation: '{tag}' slot requested but the "
                    "generated tag list is empty"
                )
            value = rng.pick_one(sorted(generated_tags))
        else:
            raise ValueError(
                f"topic-title generation: unknown slot {{{slot}}} in "
                f"template {template!r}"
            )
        result = result[:start] + value + result[end + 1 :]
    if "{" in result:
        raise ValueError(
            f"topic-title generation: unfilled slots remain in {result!r} "
            f"(from template {template!r})"
        )
    return result


def _draw_tags_for_category(
    category: str,
    rng,
    product,
    generated_tags: Sequence[str],
) -> list[str]:
    """Draw 1–4 tags for a non-anchor topic in `category`.

    Uses `TAG_AFFINITY_BY_CATEGORY[category]` as the axis whitelist. The
    candidate pool is the union of those axes' tag lists, intersected with
    the actually-generated tag list (some optional tags didn't survive the
    Sit-2 random subset draw). Result is sorted for deterministic equality.
    """
    affinity_axes = product.TAG_AFFINITY_BY_CATEGORY.get(category)
    if not affinity_axes:
        raise ValueError(
            f"topic-tag draw: TAG_AFFINITY_BY_CATEGORY missing entry for "
            f"category {category!r}"
        )
    tag_pool_by_axis = product.TAG_POOL_BY_AXIS
    candidate_pool: set[str] = set()
    for axis in affinity_axes:
        if axis not in tag_pool_by_axis:
            raise ValueError(
                f"topic-tag draw: affinity axis {axis!r} for category "
                f"{category!r} not present in TAG_POOL_BY_AXIS"
            )
        candidate_pool.update(tag_pool_by_axis[axis])
    candidate_pool &= set(generated_tags)
    candidates = sorted(candidate_pool)
    if not candidates:
        # Defensive: if the seed happened to drop every tag from the
        # affinity axes, fall back to the full generated tag list rather
        # than failing — this is a draw-distribution problem, not a
        # constants problem.
        candidates = sorted(generated_tags)
    k_max = min(_MAX_TAGS_PER_TOPIC, len(candidates))
    k = rng.pick_int(_MIN_TAGS_PER_TOPIC, k_max)
    drawn = rng.pick_n(candidates, k)
    return sorted(drawn)


def _category_weights(
    categories: Sequence[str], product
) -> list[int]:
    """Return per-category integer weights for the topic-category draw.

    Core categories get `_CORE_CATEGORY_WEIGHT`, optional ones get
    `_OPTIONAL_CATEGORY_WEIGHT`. Integer weights play nicely with
    `rng.weighted` and make the 3:1 ratio human-readable.
    """
    core = set(product.CORE_CATEGORIES)
    return [
        _CORE_CATEGORY_WEIGHT if cat in core else _OPTIONAL_CATEGORY_WEIGHT
        for cat in categories
    ]


def _validate_inputs(
    spec: GenerationSpec,
    categories: Sequence[str],
    tags: Sequence[str],
    users: Sequence[User],
    timeline: Timeline,
    *,
    count: Optional[int] = None,
) -> int:
    """Validate generator inputs; return the topic count to generate.

    `count=None` (default) defers to `spec.scale_targets()["topics"]` — the
    `init` flow uses this. Sit 15's `extend --add-topics N` flow passes an
    explicit `count` because the extension's topic count is decoupled from
    the scale preset.

    Raises `ValueError` on any mismatch — generator-hygiene rule.
    """
    total = (
        count
        if count is not None
        else spec.scale_targets()["topics"]
    )
    if total <= 0:
        raise ValueError(
            f"generate_topics: total {total} must be positive"
        )
    if not categories:
        raise ValueError("generate_topics: categories must be non-empty")
    if not tags:
        raise ValueError("generate_topics: tags must be non-empty")
    if not users:
        raise ValueError("generate_topics: users must be non-empty")
    if timeline.total_topics < total:
        raise ValueError(
            f"generate_topics: timeline sized for {timeline.total_topics} "
            f"topics, need {total}"
        )

    product = spec.product
    pool = set(product.CATEGORY_POOL)
    for cat in categories:
        if cat not in pool:
            raise ValueError(
                f"generate_topics: category {cat!r} not in CATEGORY_POOL"
            )
        if cat not in product.TITLE_TEMPLATES_BY_CATEGORY:
            raise ValueError(
                f"generate_topics: TITLE_TEMPLATES_BY_CATEGORY missing "
                f"entry for {cat!r}"
            )
        if not product.TITLE_TEMPLATES_BY_CATEGORY[cat]:
            raise ValueError(
                f"generate_topics: TITLE_TEMPLATES_BY_CATEGORY[{cat!r}] "
                f"is empty"
            )
        if cat not in product.TAG_AFFINITY_BY_CATEGORY:
            raise ValueError(
                f"generate_topics: TAG_AFFINITY_BY_CATEGORY missing "
                f"entry for {cat!r}"
            )

    all_pool_tags = {
        t for axis_tags in product.TAG_POOL_BY_AXIS.values() for t in axis_tags
    }
    for tag in tags:
        if tag not in all_pool_tags:
            raise ValueError(
                f"generate_topics: tag {tag!r} not in TAG_POOL_BY_AXIS"
            )

    return total


def generate_topics(
    spec: GenerationSpec,
    categories: Sequence[str],
    tags: Sequence[str],
    users: Sequence[User],
    timeline: Timeline,
    *,
    count: Optional[int] = None,
    id_offset: int = 0,
    seen_titles: Optional[dict[str, int]] = None,
    skip_cluster_anchors: bool = False,
) -> list[Topic]:
    """Return a deterministic `list[Topic]`.

    With `count=None` (default), generates `spec.scale_targets()["topics"]`
    topics — the `init` path. With `count=N`, generates exactly N topics —
    the `extend --add-topics N` path (Sit 15).

    With `id_offset=K` (default 0), topic ids start at `K + 1` instead of
    `1`. Used by `extend` so extension topic ids don't collide with base
    topic ids; the unused-by-init default keeps the existing behaviour.

    With `seen_titles=<dict>`, the title-dedup pass treats those titles as
    already taken (suffixing `(2)`, `(3)`, … to any new title that collides).
    `extend` passes the base forum's title set so extension topics never
    duplicate a base title — Discourse rejects duplicate titles. The dict is
    NOT mutated in-place; a copy is taken so the caller's snapshot stays
    intact.

    With `skip_cluster_anchors=True`, the cluster-anchor pre-pass is
    skipped entirely — every emitted topic draws its category / tags via
    the regular weighted path. Used by `extend` (Sit 15+): cluster anchors
    are a "first N topics of the forum" reservation pattern; the
    extension's topics are by definition NOT the first, and the base
    bake's anchors already exist in `seen_titles`. Skipping also lets
    `extend --add-topics N` accept N < `len(CLUSTER_TAG_COMBINATIONS)`
    without tripping the "more combos than topics" guard.

    The first `len(CLUSTER_TAG_COMBINATIONS)` topics (init path) anchor
    the design-doc pain-point clusters (their tag sets equal the combos
    exactly). Remaining topics draw category, template, slot fills, tags,
    author, and timestamp via the rules in this module's docstring.

    Same `(spec, categories, tags, users, timeline, count, id_offset,
    seen_titles, skip_cluster_anchors)` -> identical output (full
    dataclass equality across the list).
    """
    total = _validate_inputs(
        spec, categories, tags, users, timeline, count=count
    )

    product = spec.product
    rng = spec.rng("topics")

    cluster_combos: list[list[str]] = (
        []
        if skip_cluster_anchors
        else list(product.CLUSTER_TAG_COMBINATIONS)
    )
    if len(cluster_combos) > total:
        raise ValueError(
            f"generate_topics: {len(cluster_combos)} cluster combos exceed "
            f"total topic count {total}; bump scale or shrink combos"
        )

    cat_list: list[str] = list(categories)
    cat_weights = _category_weights(cat_list, product)
    user_list: list[User] = list(users)
    user_weights = [u.activity_weight for u in user_list]
    if not any(w > 0 for w in user_weights):
        raise ValueError(
            "generate_topics: every user has zero activity_weight; "
            "weighted author draw cannot proceed"
        )

    topics: list[Topic] = []

    for combo in cluster_combos:
        idx = len(topics)
        category = _pick_anchor_category(combo, cat_list)
        templates = product.TITLE_TEMPLATES_BY_CATEGORY[category]
        template = rng.pick_one(sorted(templates))
        title = _fill_template(template, rng, product, tags)
        author = rng.weighted(user_list, user_weights)
        # Anchor tags = the combo, intersected with the generated tag list,
        # sorted. We assert intersection completeness because the design doc
        # invariant is "every cluster combo present in the generated tag set"
        # — Sit 2 reserves combo tags before sampling, so intersection is the
        # full combo.
        anchor_tags = sorted(set(combo) & set(tags))
        if set(anchor_tags) != set(combo):
            missing = sorted(set(combo) - set(tags))
            raise ValueError(
                f"generate_topics: cluster combo {combo} contains tags not "
                f"in the generated tag set {missing}; tag generator failed "
                f"its reservation invariant"
            )
        topics.append(
            Topic(
                id=idx + 1 + id_offset,
                title=title,
                category=category,
                tags=anchor_tags,
                author_username=author.username,
                created_at=timeline.timestamp_for_topic(idx),
            )
        )

    while len(topics) < total:
        idx = len(topics)
        category = rng.weighted(cat_list, cat_weights)
        templates = product.TITLE_TEMPLATES_BY_CATEGORY[category]
        template = rng.pick_one(sorted(templates))
        title = _fill_template(template, rng, product, tags)
        topic_tags = _draw_tags_for_category(category, rng, product, tags)
        author = rng.weighted(user_list, user_weights)
        topics.append(
            Topic(
                id=idx + 1 + id_offset,
                title=title,
                category=category,
                tags=topic_tags,
                author_username=author.username,
                created_at=timeline.timestamp_for_topic(idx),
            )
        )

    return _dedupe_titles(topics, seen_titles=seen_titles)


def _dedupe_titles(
    topics: list[Topic],
    *,
    seen_titles: Optional[dict[str, int]] = None,
) -> list[Topic]:
    """Suffix `(2)`, `(3)`, … on duplicate titles in chronological-id order.

    Discourse enforces unique topic titles by default (the
    `title_has_already_been_used` check). The slot-fill templates have a
    finite vocabulary, so at scale `tiny` two topics with seed=42 can land
    on the same title (~5% chance). The fix is deterministic: walk topics
    in input order (which is chronological-id order) and append a
    parenthesised counter to each later occurrence.

    `seen_titles=None` starts from an empty set — the `init` path. `extend`
    seeds it with `{title: 1 for title in base.topics_titles}` so extension
    titles never collide with base titles. The passed dict is copied;
    callers' snapshot is not mutated.
    """
    seen: dict[str, int] = (
        dict(seen_titles) if seen_titles is not None else {}
    )
    deduped: list[Topic] = []
    for t in topics:
        count = seen.get(t.title, 0)
        if count == 0:
            seen[t.title] = 1
            deduped.append(t)
        else:
            seen[t.title] = count + 1
            new_title = f"{t.title} ({count + 1})"
            # Defensive: a deeper collision (rare but possible if a
            # duplicate template happens to land on `Title (2)` already).
            # Bump until clear.
            while new_title in seen:
                count += 1
                new_title = f"{t.title} ({count + 1})"
            seen[new_title] = 1
            deduped.append(
                Topic(
                    id=t.id,
                    title=new_title,
                    category=t.category,
                    tags=t.tags,
                    author_username=t.author_username,
                    created_at=t.created_at,
                )
            )
    return deduped
