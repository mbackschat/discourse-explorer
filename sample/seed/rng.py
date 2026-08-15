"""Deterministic RNG wrapper.

All randomness in the sample seeder flows through `Rng` instances. There is no
module-level `random.choice` (or equivalent) anywhere in the subtree — every
generator must accept an `Rng` explicitly so that a given seed produces the
same forum across machines.

The wrapper is intentionally thin around `random.Random`; we keep only the
methods generators actually need, with names that read at the call site.
"""

from __future__ import annotations

import random
from typing import Sequence, TypeVar

T = TypeVar("T")


class Rng:
    """Wraps a single `random.Random` and exposes the operations the seeders need.

    The seed accepts either an int or a string. Strings are useful for derived
    streams (see `GenerationSpec.rng(*salt)`) so that, e.g., the `categories`
    generator and the `users` generator don't influence each other when both
    sample from the same base seed.
    """

    def __init__(self, seed: int | str) -> None:
        self._seed = seed
        self._random = random.Random(seed)

    @property
    def seed(self) -> int | str:
        return self._seed

    def pick_one(self, seq: Sequence[T]) -> T:
        """Return one uniformly-chosen item from `seq`."""
        if not seq:
            raise ValueError("pick_one called with empty sequence")
        return self._random.choice(list(seq))

    def pick_n(self, seq: Sequence[T], k: int) -> list[T]:
        """Return `k` distinct items from `seq` (sample without replacement)."""
        items = list(seq)
        if k < 0:
            raise ValueError(f"pick_n: k must be non-negative, got {k}")
        if k > len(items):
            raise ValueError(
                f"pick_n: k={k} exceeds sequence length {len(items)}"
            )
        return self._random.sample(items, k)

    def pick_int(self, lo: int, hi: int) -> int:
        """Return an integer in `[lo, hi]` (both endpoints inclusive)."""
        if lo > hi:
            raise ValueError(f"pick_int: lo={lo} > hi={hi}")
        return self._random.randint(lo, hi)

    def weighted(self, items: Sequence[T], weights: Sequence[float]) -> T:
        """Return one item from `items`, picked with the given weights."""
        items_list = list(items)
        weights_list = list(weights)
        if not items_list:
            raise ValueError("weighted called with empty items")
        if len(items_list) != len(weights_list):
            raise ValueError(
                f"weighted: items ({len(items_list)}) and weights "
                f"({len(weights_list)}) must have the same length"
            )
        return self._random.choices(items_list, weights=weights_list, k=1)[0]

    def shuffle(self, seq: Sequence[T]) -> list[T]:
        """Return a new shuffled list (does not mutate the input)."""
        items = list(seq)
        self._random.shuffle(items)
        return items

    def pareto(self, alpha: float) -> float:
        """Draw from a Pareto distribution with shape parameter `alpha`.

        Used by `generators/users.py` to assign Pareto-distributed activity
        weights (≈ classic 80/20 power law at `alpha = 1.16`). Wrapping
        `paretovariate` here keeps generators from reaching into `_random`
        directly — the RNG surface stays hermetic.
        """
        if alpha <= 0:
            raise ValueError(f"pareto: alpha must be positive, got {alpha}")
        return self._random.paretovariate(alpha)

    def poisson(self, mean: float) -> int:
        """Draw a non-negative integer from a Poisson distribution with given mean.

        Used by `generators/posts.py` to pick the per-topic reply count where
        the mean is derived from the scale preset (`posts/topics - 1`). We
        sample inter-arrival times via `expovariate(1/mean)` and count how
        many fit in a unit interval — the standard expovariate-based Poisson
        sampler. Adding this here keeps generators from poking `_random`
        directly so the RNG surface stays hermetic (mirrors `pareto`).

        A `mean <= 0` short-circuits to 0 — Poisson(0) is degenerate and the
        post generator wants 0 replies in that pathological case rather than
        an exception.
        """
        if mean <= 0:
            return 0
        # Expovariate-sum algorithm: a Poisson(mean) is the count of arrivals
        # in unit time of a Poisson process with rate `mean`. Draw exponential
        # inter-arrival times with rate `mean` and count how many fit in 1
        # unit of time. Equivalent to `numpy.random.poisson(mean)` but free of
        # the numpy dep. `expovariate(lambd)` interprets `lambd` as the rate,
        # so we pass `mean` directly.
        count = 0
        elapsed = 0.0
        while True:
            elapsed += self._random.expovariate(mean)
            if elapsed >= 1.0:
                return count
            count += 1
