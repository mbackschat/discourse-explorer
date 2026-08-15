"""User generator with Pareto activity weighting.

The forum needs a user roster with two qualities the design doc relies on:

1. **Pareto activity** — 80% of posts come from 20% of users. We give every
   user a Pareto-distributed `activity_weight` (`alpha = 1.16`, ≈ classic
   80/20 power law). Topic + post generators in later sits draw authors
   weighted by this field, which is how the heavy-tail signal actually
   propagates into `stats users`.
2. **Role mix** — exactly 1 admin + 1 moderator + the rest regulars. Roles
   are pinned to deterministic positions (user[0] = admin, user[1] = mod) so
   downstream generators / tests can assert against fixed indices without
   re-discovering who's who from a `role` filter.

Username construction draws `<adjective>_<noun>` pairs from `USERNAME_PARTS`
without replacement; the 20 × 30 = 600-pair pool comfortably covers the
`large` scale's 200 users. A numeric suffix is appended on the rare collision
(would only fire if a future product extends counts past pool capacity)
rather than failing, so the generator stays robust to product-constant drift.

Determinism: uses `spec.rng("users")`. Output is ordered by user-creation
order (admin first, moderator second, regulars after) so tests can pin
specific indices.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..universe import GenerationSpec


# Shape parameter for the Pareto draw on activity weight. `alpha = 1.16` is
# the canonical 80/20 power law — top 20% of draws hold ~80% of the total
# mass at the population scale. Lower alpha = heavier tail.
_PARETO_ALPHA = 1.16

# Minimum user count: we need at least 1 admin + 1 moderator + 1 regular for
# the role mix to be meaningful. Fewer users means the spec's scale preset is
# malformed.
_MIN_USERS = 3


@dataclass(frozen=True)
class User:
    """A generated forum user.

    Attributes:
        username: lowercase snake_case identifier (e.g. `salty_gull`).
        display_name: title-cased rendering of the username
            (e.g. `Salty Gull`). Always paired with `username`.
        role: one of `"admin"`, `"moderator"`, `"regular"`.
        activity_weight: positive float, Pareto-distributed. Relative weight
            for author draws — does not need to sum to 1 across the roster.
    """

    username: str
    display_name: str
    role: str
    activity_weight: float


def _username_to_display(username: str) -> str:
    """Render a snake_case username as a title-cased display name.

    `salty_gull` -> `Salty Gull`. Hyphens inside a noun (e.g. `bilge-rat`)
    are preserved as word separators so `salty_bilge-rat` becomes
    `Salty Bilge-Rat`.
    """
    parts = username.split("_")
    titled_parts = []
    for part in parts:
        # Hyphenated nouns: title-case each segment so `bilge-rat` -> `Bilge-Rat`.
        titled_parts.append("-".join(seg.capitalize() for seg in part.split("-")))
    return " ".join(titled_parts)


def generate_users(spec: GenerationSpec) -> list[User]:
    """Return a deterministic `list[User]` for `spec`.

    Same `spec` -> same list (full dataclass equality). Roles assigned by
    position: index 0 = admin, index 1 = moderator, the rest = regular.
    Activity weights drawn from `Pareto(alpha=1.16)`.
    """
    target = spec.scale_targets()["users"]
    if target < _MIN_USERS:
        raise ValueError(
            f"generate_users: scale target {target} below minimum {_MIN_USERS} "
            f"(need at least 1 admin + 1 moderator + 1 regular)"
        )

    product = spec.product
    rng = spec.rng("users")

    parts = product.USERNAME_PARTS
    adjectives: list[str] = sorted(parts["adjectives"])
    nouns: list[str] = sorted(parts["nouns"])
    if not adjectives or not nouns:
        raise ValueError(
            "generate_users: USERNAME_PARTS must define non-empty "
            "'adjectives' and 'nouns' pools"
        )

    # Build the full pair pool deterministically (sorted inputs above), then
    # shuffle via the salted RNG so seed controls the order.
    all_pairs: list[tuple[str, str]] = [(a, n) for a in adjectives for n in nouns]
    shuffled_pairs = rng.shuffle(all_pairs)

    used_usernames: set[str] = set()
    usernames: list[str] = []
    pair_idx = 0
    while len(usernames) < target:
        if pair_idx < len(shuffled_pairs):
            adj, noun = shuffled_pairs[pair_idx]
            pair_idx += 1
            base = f"{adj}_{noun}"
        else:
            # Defensive: only reachable if a future scale pushes user count
            # past the 600-pair pool. We loop the pool again with the
            # numeric-suffix path below, which guarantees uniqueness.
            adj, noun = shuffled_pairs[pair_idx % len(shuffled_pairs)]
            pair_idx += 1
            base = f"{adj}_{noun}"

        if base not in used_usernames:
            used_usernames.add(base)
            usernames.append(base)
            continue

        # Collision: rare. Try `<base>_2`, `_3`, … until unique. A growing
        # suffix is preferred over re-drawing because it keeps the username
        # readable and the generator finite even in adversarial cases.
        suffix = 2
        while True:
            candidate = f"{base}_{suffix}"
            if candidate not in used_usernames:
                used_usernames.add(candidate)
                usernames.append(candidate)
                break
            suffix += 1

    # Roles: pinned positions so downstream generators / tests can rely on
    # `users[0].role == "admin"`. Exactly one of each privileged role.
    roles: list[str] = ["admin", "moderator"] + ["regular"] * (target - 2)

    # One Pareto draw per user. Order matters for determinism — by drawing in
    # the same loop we keep the per-user RNG advance bound to that user's
    # position in the list.
    users: list[User] = []
    for username, role in zip(usernames, roles):
        weight = rng.pareto(_PARETO_ALPHA)
        users.append(
            User(
                username=username,
                display_name=_username_to_display(username),
                role=role,
                activity_weight=weight,
            )
        )

    return users
