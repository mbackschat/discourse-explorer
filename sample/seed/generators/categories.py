"""Category generator.

Returns the product's `CORE_CATEGORIES` plus 2–4 randomly-drawn optional
categories from the rest of `CATEGORY_POOL`. Result count therefore lands in
[5, 7] (3 core + 2..4 optional).
"""

from __future__ import annotations

from ..universe import GenerationSpec


def generate_categories(spec: GenerationSpec) -> list[str]:
    """Return CORE_CATEGORIES + 2–4 optional categories drawn from the pool.

    Determinism: uses `spec.rng("categories")`. Same seed -> same list.
    """
    product = spec.product
    core = list(product.CORE_CATEGORIES)
    optional = [c for c in product.CATEGORY_POOL if c not in core]
    rng = spec.rng("categories")
    k = rng.pick_int(2, 4)
    extras = rng.pick_n(optional, k)
    return core + extras
