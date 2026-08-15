"""Blocklist loader and post-filter for the sample seeder.

Every body, title, username, tag, or category produced by the generators must
clear this filter before going on the wire. The list lives in
`sample/seed/blocklist.txt` (hand-curated, ~200 entries) and covers real game
franchises, studios, engines, and iconic characters across genres — content
an LLM is most likely to reach for. Hardware/OS platforms (Amiga, Steam Deck,
…) are explicitly NOT in the blocklist; see `sample/CLAUDE.md`.

Two functions are exposed:

- `load_blocklist()` — read the file once and cache the lowercased term set.
- `check(text)` — return the sorted (lowercased) blocklist hits in `text`.
  Word-boundary case-insensitive regex match. Multi-word terms are escaped
  via `re.escape` so embedded punctuation (apostrophes, ampersands, hyphens)
  doesn't trip up the regex compiler.

Word boundaries (`\\b`) match between a word character and a non-word
character. They behave correctly for ASCII multi-word terms like
`"Monkey Island"` (the inner space is a non-word char, the outer letters
match the boundary). They do NOT fire reliably for terms beginning or
ending with punctuation (`'D&D'`, `"Ren'Py"`); for those the boundary still
anchors against the alphabetic edge of the term, which is what we want.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# `blocklist.txt` lives one directory up from this module (`sample/seed/`),
# next to the generators it filters. Anchoring the path off `__file__` keeps
# the loader independent of the caller's CWD — important because the CLI
# (`sample/seed/__main__.py`) doesn't necessarily run from the project root.
_BLOCKLIST_PATH = Path(__file__).resolve().parent.parent / "blocklist.txt"


@lru_cache(maxsize=1)
def load_blocklist() -> frozenset[str]:
    """Return the lowercased blocklist as a frozen set.

    Reads `sample/seed/blocklist.txt` once, ignores comment lines (leading
    `#`) and blank lines, lowercases each entry, and caches the result. The
    return type is `frozenset` so callers can treat it as immutable; the
    `lru_cache` makes repeat calls free.

    Idempotency: `load_blocklist() is load_blocklist()` — the cache hands
    back the same frozen object every call.
    """
    if not _BLOCKLIST_PATH.exists():
        raise FileNotFoundError(
            f"blocklist file not found: {_BLOCKLIST_PATH}"
        )
    entries: set[str] = set()
    for line in _BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        entries.add(stripped.lower())
    return frozenset(entries)


@lru_cache(maxsize=1)
def _compiled_pattern() -> re.Pattern[str]:
    """Compile a single combined regex over the full blocklist.

    One alternation pattern is dramatically faster than iterating every term
    against the input — for a 250-term list and a typical post body that's
    one regex pass instead of 250. Sorted-by-length-descending so a longer
    term wins over a shorter substring match (e.g. `"The Witcher"` beats
    `"Witcher"` when both happen to be in the list — currently only the
    short form is, but the ordering keeps future additions safe).

    Each term is wrapped with `\\b` anchors and `re.escape`d so embedded
    punctuation in terms like `"D&D"` or `"Counter-Strike"` doesn't break
    the regex compile.
    """
    terms = sorted(load_blocklist(), key=len, reverse=True)
    if not terms:
        # Defensive: a regex `(?:)` matches everywhere; we'd rather match
        # nowhere when the list is empty (a missing/empty file shouldn't
        # silently flag every body as a hit). Use a pattern guaranteed not
        # to match by anchoring at start AND end against an impossible char.
        return re.compile(r"(?!x)x")
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def check(text: str) -> list[str]:
    """Return sorted, deduplicated blocklist hits found in `text`.

    Each hit is lowercased so the caller gets a stable representation
    regardless of the casing in the input. Multi-word terms with embedded
    punctuation (`"Counter-Strike"`, `"Ren'Py"`) are matched correctly.

    An empty input or a no-hit input returns `[]`. Order is sorted
    alphabetically — callers that want to surface the first hit can index
    `[0]` after sorting.
    """
    if not text:
        return []
    hits: set[str] = set()
    for match in _compiled_pattern().finditer(text):
        hits.add(match.group(0).lower())
    return sorted(hits)
