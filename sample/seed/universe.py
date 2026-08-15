"""Generation spec — the (seed, scale, product) tuple that controls a bake.

A `GenerationSpec` is the one immutable handle generators take. It owns the
single source of randomness via `rng(*salt)`: each generator asks for its own
derived stream so that adding/removing a generator doesn't shift the output of
every other generator (which would happen if they all shared one global RNG).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from .rng import Rng


# Per-scale targets for users, topics, and posts. Costs in the design doc are
# attached to (seed, scale, product) — these are the structural counts.
SCALE_PRESETS: dict[str, dict[str, int]] = {
    "tiny": {"users": 10, "topics": 30, "posts": 120},
    "small": {"users": 30, "topics": 150, "posts": 700},
    "medium": {"users": 80, "topics": 500, "posts": 2500},
    "large": {"users": 200, "topics": 2000, "posts": 12000},
}


@dataclass(frozen=True)
class GenerationSpec:
    """Immutable bake parameters.

    Attributes:
        seed: integer seed; combined with per-generator salt to produce a
            derived `Rng` (so generators don't influence each other).
        scale: one of `SCALE_PRESETS` keys.
        product: imported product module (e.g. `crown_of_brine`) — the spec
            does not import a specific product so the universe abstraction
            stays swappable.
    """

    seed: int
    scale: str
    product: ModuleType

    def __post_init__(self) -> None:
        if self.scale not in SCALE_PRESETS:
            raise ValueError(
                f"unknown scale {self.scale!r}; "
                f"expected one of {sorted(SCALE_PRESETS)}"
            )

    def scale_targets(self) -> dict[str, int]:
        """Return the user/topic/post counts for this spec's scale."""
        # Return a copy so callers can't mutate the shared preset dict.
        return dict(SCALE_PRESETS[self.scale])

    def rng(self, *salt: str) -> Rng:
        """Return a per-component `Rng` derived from `seed` + `salt`.

        Generators should each ask for their own salted stream
        (`spec.rng("categories")`, `spec.rng("users")`, …) so that adding or
        re-ordering generators doesn't change output of unrelated ones.
        """
        salt_str = ":".join(salt)
        return Rng(f"{self.seed}:{salt_str}")
