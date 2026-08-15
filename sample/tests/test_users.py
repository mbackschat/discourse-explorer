"""Tests for `sample.seed.generators.users`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that
matter — determinism, role mix, uniqueness, the Pareto power law, the
display-name pairing, scale validation — and skip the rest.
"""

from __future__ import annotations

import unittest

from sample.seed.generators.users import User, generate_users
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec, SCALE_PRESETS


def _spec(seed: int, scale: str = "tiny") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


class UsersTests(unittest.TestCase):
    def test_determinism(self) -> None:
        """Same seed twice -> identical `list[User]` (full dataclass equality)."""
        a = generate_users(_spec(42))
        b = generate_users(_spec(42))
        self.assertEqual(a, b)

    def test_count_matches_scale(self) -> None:
        """Roster size equals `spec.scale_targets()["users"]` for every scale."""
        for scale in SCALE_PRESETS:
            with self.subTest(scale=scale):
                spec = _spec(42, scale=scale)
                users = generate_users(spec)
                self.assertEqual(len(users), spec.scale_targets()["users"])

    def test_usernames_unique(self) -> None:
        """No two users share a username, even at the largest scale."""
        users = generate_users(_spec(42, scale="large"))
        self.assertEqual(
            len({u.username for u in users}),
            len(users),
            "usernames are not unique",
        )

    def test_role_mix(self) -> None:
        """Exactly 1 admin + 1 moderator + rest regulars (tiny = 10 users)."""
        users = generate_users(_spec(42, scale="tiny"))
        roles = [u.role for u in users]
        self.assertEqual(roles.count("admin"), 1)
        self.assertEqual(roles.count("moderator"), 1)
        self.assertEqual(roles.count("regular"), len(users) - 2)
        # Position pinning: admin and moderator occupy fixed indices so
        # downstream generators can rely on it.
        self.assertEqual(users[0].role, "admin")
        self.assertEqual(users[1].role, "moderator")

    def test_pareto_invariant(self) -> None:
        """Top 20% of users hold a heavy share of total activity weight.

        We use `medium` (80 users) — small samples are too noisy to assert
        the textbook 80/20 reliably.

        Threshold note: the canonical "80/20" Pareto principle (alpha=1.16)
        is a property of the continuous Lorenz curve, not of finite i.i.d.
        samples. Empirically, finite Pareto draws at alpha=1.16 land the
        top-20% share between roughly 0.50 and 0.95 with a median near
        0.65–0.70 (see sweep over 1000 seeds at n=80). We therefore assert
        ≥ 0.50: comfortably above what a uniform (broken-Pareto) draw
        would yield (~0.20 for the top quintile), while robust enough to
        not flag legitimate Pareto samples on adversarial seeds.
        """
        users = generate_users(_spec(42, scale="medium"))
        weights = sorted((u.activity_weight for u in users), reverse=True)
        top_count = max(1, len(weights) // 5)  # top 20%
        top_share = sum(weights[:top_count]) / sum(weights)
        self.assertGreaterEqual(
            top_share,
            0.50,
            f"top 20% hold only {top_share:.2%} of weight; "
            "distribution looks flatter than Pareto",
        )

    def test_display_name_pairs_username(self) -> None:
        """Every user's display_name is the title-cased rendering of its username."""
        users = generate_users(_spec(42, scale="medium"))
        for u in users:
            with self.subTest(username=u.username):
                expected_parts = []
                for part in u.username.split("_"):
                    expected_parts.append(
                        "-".join(seg.capitalize() for seg in part.split("-"))
                    )
                expected = " ".join(expected_parts)
                self.assertEqual(u.display_name, expected)

    def test_seed_varies(self) -> None:
        """Different seeds yield a different role-0 (admin) username."""
        a = generate_users(_spec(42))
        b = generate_users(_spec(99))
        self.assertNotEqual(
            a[0].username,
            b[0].username,
            "expected seeds 42 and 99 to produce different admin usernames",
        )

    def test_scale_below_min_raises(self) -> None:
        """Scales with users < 3 are rejected with `ValueError`."""
        # Inject a one-off bogus scale via SCALE_PRESETS to exercise the guard.
        original = SCALE_PRESETS.get("__pareto_test_micro__")
        SCALE_PRESETS["__pareto_test_micro__"] = {"users": 2, "topics": 1, "posts": 1}
        try:
            spec = GenerationSpec(
                seed=42,
                scale="__pareto_test_micro__",
                product=crown_of_brine,
            )
            with self.assertRaises(ValueError):
                generate_users(spec)
        finally:
            if original is None:
                del SCALE_PRESETS["__pareto_test_micro__"]
            else:
                SCALE_PRESETS["__pareto_test_micro__"] = original

    def test_user_dataclass_is_frozen(self) -> None:
        """`User` is frozen — guards against accidental mutation downstream."""
        users = generate_users(_spec(42))
        with self.assertRaises(Exception):
            # `dataclasses.FrozenInstanceError` is the specific type, but
            # `Exception` keeps the test robust if Python ever changes it.
            users[0].username = "mutated"  # type: ignore[misc]

    def test_activity_weights_positive(self) -> None:
        """All weights are strictly positive — required for weighted draws."""
        users = generate_users(_spec(42, scale="medium"))
        for u in users:
            with self.subTest(username=u.username):
                self.assertGreater(u.activity_weight, 0.0)

    def test_returns_user_instances(self) -> None:
        """Output is a list of `User` dataclass instances (not dicts/tuples)."""
        users = generate_users(_spec(42))
        self.assertTrue(all(isinstance(u, User) for u in users))


if __name__ == "__main__":
    unittest.main()
