"""Tests for `sample.seed.generators.categories`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that matter
and skip the rest.
"""

from __future__ import annotations

import unittest

from sample.seed.generators.categories import generate_categories
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int) -> GenerationSpec:
    return GenerationSpec(seed=seed, scale="tiny", product=crown_of_brine)


class CategoriesTests(unittest.TestCase):
    def test_determinism(self) -> None:
        """Same seed twice -> identical output (including order)."""
        a = generate_categories(_spec(42))
        b = generate_categories(_spec(42))
        self.assertEqual(a, b)

    def test_core_categories_present(self) -> None:
        """The three core categories are always in the result."""
        result = generate_categories(_spec(42))
        self.assertLessEqual(
            {"Announcements", "Help & Hints", "Bug Reports"},
            set(result),
        )

    def test_seed_varies_optional_portion(self) -> None:
        """Different seeds change at least the optional portion of the draw."""
        a = generate_categories(_spec(42))
        b = generate_categories(_spec(99))
        # Strip the (always-identical) core prefix and compare what's left.
        core = set(crown_of_brine.CORE_CATEGORIES)
        a_optional = [c for c in a if c not in core]
        b_optional = [c for c in b if c not in core]
        self.assertNotEqual(
            a_optional,
            b_optional,
            "expected at least one differing optional category between seeds 42 and 99",
        )

    def test_count_in_range(self) -> None:
        """Result size is in [5, 7] (3 core + 2..4 optional)."""
        for seed in (42, 99, 1, 2026):
            result = generate_categories(_spec(seed))
            self.assertGreaterEqual(
                len(result), 5, f"seed={seed}: too few categories ({len(result)})"
            )
            self.assertLessEqual(
                len(result), 7, f"seed={seed}: too many categories ({len(result)})"
            )

    def test_no_duplicate_categories(self) -> None:
        """Optional draw never re-introduces a core category."""
        result = generate_categories(_spec(42))
        self.assertEqual(len(result), len(set(result)))


if __name__ == "__main__":
    unittest.main()
