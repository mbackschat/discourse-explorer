"""Tests for `sample.seed.generators.tags`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that matter —
determinism, count range, cluster + core preservation, axis coverage,
no-duplicates, sorted output — and skip the rest.
"""

from __future__ import annotations

import unittest

from sample.seed.generators.tags import generate_tags
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int) -> GenerationSpec:
    return GenerationSpec(seed=seed, scale="tiny", product=crown_of_brine)


class TagsTests(unittest.TestCase):
    def test_determinism(self) -> None:
        """Same seed twice -> identical output (including order)."""
        a = generate_tags(_spec(42))
        b = generate_tags(_spec(42))
        self.assertEqual(a, b)

    def test_all_cluster_combos_present(self) -> None:
        """Every tag in every CLUSTER_TAG_COMBINATIONS entry is in the result."""
        result = set(generate_tags(_spec(42)))
        for combo in crown_of_brine.CLUSTER_TAG_COMBINATIONS:
            with self.subTest(combo=combo):
                self.assertLessEqual(
                    set(combo),
                    result,
                    f"cluster combo {combo} not fully present in result",
                )

    def test_all_core_tags_present(self) -> None:
        """Union of CORE_TAGS_BY_AXIS values is a subset of the result."""
        result = set(generate_tags(_spec(42)))
        core: set[str] = set()
        for axis_core in crown_of_brine.CORE_TAGS_BY_AXIS.values():
            core.update(axis_core)
        self.assertLessEqual(core, result)

    def test_count_in_range(self) -> None:
        """Result size is in [25, 35]."""
        for seed in (42, 99, 1, 2026):
            with self.subTest(seed=seed):
                result = generate_tags(_spec(seed))
                self.assertGreaterEqual(
                    len(result), 25, f"seed={seed}: too few tags ({len(result)})"
                )
                self.assertLessEqual(
                    len(result), 35, f"seed={seed}: too many tags ({len(result)})"
                )

    def test_every_axis_contributes(self) -> None:
        """For each axis, ≥1 tag from TAG_POOL_BY_AXIS[axis] appears in the result."""
        result = set(generate_tags(_spec(42)))
        for axis, pool in crown_of_brine.TAG_POOL_BY_AXIS.items():
            with self.subTest(axis=axis):
                self.assertTrue(
                    set(pool) & result,
                    f"axis {axis!r} contributed no tags to the result",
                )

    def test_seed_varies(self) -> None:
        """Different seeds produce different results, but invariants still hold."""
        a = generate_tags(_spec(42))
        b = generate_tags(_spec(99))
        self.assertNotEqual(a, b, "seeds 42 and 99 produced identical tag sets")

        # Both seeds must still satisfy the cluster + core invariants.
        core: set[str] = set()
        for axis_core in crown_of_brine.CORE_TAGS_BY_AXIS.values():
            core.update(axis_core)
        for label, result in (("seed=42", a), ("seed=99", b)):
            with self.subTest(label=label):
                result_set = set(result)
                self.assertLessEqual(
                    core,
                    result_set,
                    f"{label}: core tags missing from result",
                )
                for combo in crown_of_brine.CLUSTER_TAG_COMBINATIONS:
                    self.assertLessEqual(
                        set(combo),
                        result_set,
                        f"{label}: cluster {combo} not fully present",
                    )

    def test_no_duplicates(self) -> None:
        """The reserved + extras combination doesn't double-count any tag."""
        result = generate_tags(_spec(42))
        self.assertEqual(len(result), len(set(result)))

    def test_sorted(self) -> None:
        """Output is sorted so it's human-readable + order-deterministic."""
        result = generate_tags(_spec(42))
        self.assertEqual(result, sorted(result))


if __name__ == "__main__":
    unittest.main()
