"""Tag generator with axis-aware cluster reservation.

The forum needs ~25-35 tags drawn from a ~60-tag pool spread across seven
axes (status, game-version, type, subject, platform, engine, mod-tool). Two
constraints fight a naive uniform draw:

1. **Pain-point clusters** (`CLUSTER_TAG_COMBINATIONS`) require specific tag
   *combinations* to all be present together — otherwise `/forum-report`'s
   pain-points audit can't surface them. We reserve those before sampling.
2. **Every axis must contribute ≥1 tag** so per-axis filtering / faceting in
   the visualizer doesn't end up with empty axes.

Reservation handles both: the union of `CORE_TAGS_BY_AXIS` values plus all
tags appearing in any `CLUSTER_TAG_COMBINATIONS` entry covers every axis by
construction (the cluster combos in `crown_of_brine` are designed so every
non-core axis receives ≥1 cluster tag), so we don't need a separate
"top-up each axis" pass — reservation gives us the floor for free.

The remainder up to the target count is sampled without replacement from the
non-reserved tags across all axes.

Determinism: uses `spec.rng("tags")`. Output is sorted so the result is
human-readable and deterministic regardless of internal set/dict ordering.
"""

from __future__ import annotations

from ..universe import GenerationSpec


# Target count window for the generated tag set. Chosen to leave room over the
# ~16-tag reserved set (the union of CORE_TAGS_BY_AXIS + CLUSTER_TAG_COMBINATIONS
# in crown_of_brine) without exhausting the ~60-tag pool.
_TARGET_LO = 25
_TARGET_HI = 35


def generate_tags(spec: GenerationSpec) -> list[str]:
    """Return a deterministic 25–35 tag set with cluster combos reserved.

    Same `spec` -> same `list[str]`. Output is sorted.
    """
    product = spec.product
    rng = spec.rng("tags")

    pool_by_axis: dict[str, list[str]] = product.TAG_POOL_BY_AXIS
    core_by_axis: dict[str, list[str]] = product.CORE_TAGS_BY_AXIS
    cluster_combos: list[list[str]] = product.CLUSTER_TAG_COMBINATIONS

    # Reserved set: every CORE tag + every tag named by any cluster combo.
    # We keep this as a set for O(1) membership; we sort at the end so output
    # ordering doesn't depend on set iteration order.
    reserved: set[str] = set()
    for axis_core in core_by_axis.values():
        reserved.update(axis_core)
    for combo in cluster_combos:
        reserved.update(combo)

    # Full universe of tags across all axes — sorted so the candidate ordering
    # we hand to the rng is deterministic regardless of dict iteration.
    all_tags: list[str] = sorted(
        {tag for axis_tags in pool_by_axis.values() for tag in axis_tags}
    )

    # Sanity: every reserved tag must exist in the axis pool. Catches a typo
    # in CLUSTER_TAG_COMBINATIONS (e.g. a tag not actually part of any axis).
    unknown = reserved - set(all_tags)
    if unknown:
        raise ValueError(
            f"reserved tags not present in TAG_POOL_BY_AXIS: {sorted(unknown)}"
        )

    target = rng.pick_int(_TARGET_LO, _TARGET_HI)
    if target < len(reserved):
        # Defensive: would only fire if the product constants drift past 25
        # reserved tags. Surfacing it as an error is preferable to silently
        # truncating the cluster invariant the design doc relies on.
        raise ValueError(
            f"target tag count {target} is below reserved size {len(reserved)}; "
            "increase _TARGET_LO or shrink reservations"
        )

    extras_needed = target - len(reserved)
    optional_pool = sorted(set(all_tags) - reserved)
    extras = rng.pick_n(optional_pool, extras_needed)

    return sorted(reserved | set(extras))
