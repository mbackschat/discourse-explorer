"""LLM-backed post-body generator with caching + blocklist post-filter.

Phase 2 — Sit 10. Sits between `generators/posts.py` (which decides the reply
tree, authors, and timestamps) and the LLM provider (`content/llm.py`). The
generator is the single place where:

1. A cache lookup short-circuits the LLM on a re-run.
2. The provider is invoked with a deterministic prompt.
3. Outputs are filtered through `content/blocklist.check` — a real-name leak
   (Mario, Zelda, …) is treated as a generation bug, retried up to three
   times with an explicit avoidance note, then escalated by raising
   `BlocklistViolation`.
4. A failed/empty/erroring provider falls back to a deterministic template
   body so the seeder can still produce a complete forum offline. Template
   bodies are intentionally NOT cached — a future run with a working LLM
   should populate the slot properly.

The split keeps `posts.py` blind to provider configuration and lets the
content layer evolve (different prompts per category, multi-shot retries,
…) without touching the structural generator.

Determinism
-----------
The prompt builder is a pure function of `(topic, post, parent_body)`. The
cache key is `(topic_id, post_number)`. As long as the same `(seed, scale,
product, provider)` tuple is replayed, the prompt is identical and a hot
cache returns the exact prior body. The `rng` argument is currently unused
but reserved — future work may introduce minor variation across retries
(temperature shift, alternate phrasing) and that needs a deterministic
stream, not a fresh `random` instance.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol

from . import blocklist as blocklist_module


# Maximum number of LLM retries triggered by a blocklist hit before the
# generator gives up and raises. Counted as RETRIES — total LLM calls in the
# worst-case blocklist-hit path is `1 + _MAX_BLOCKLIST_RETRIES = 4` (initial
# attempt + 3 retries). A 4th still-banned response raises.
_MAX_BLOCKLIST_RETRIES = 3


class BlocklistViolation(Exception):
    """Raised when the LLM keeps surfacing blocklisted terms after retries.

    Treated as a generator-hygiene failure rather than a content fallback —
    the alternative would be silently shipping real-franchise leaks. The
    exception carries the offending hits from the final attempt so callers
    can log + report which terms the model wouldn't drop.
    """

    def __init__(self, hits: Iterable[str], attempts: int) -> None:
        self.hits = sorted(set(hits))
        self.attempts = attempts
        super().__init__(
            f"LLM body generation produced blocklisted terms after "
            f"{attempts} attempt(s); offending: {self.hits}"
        )


class _LLMLike(Protocol):
    """Structural interface for objects passed as `llm`.

    Matches `content.llm.Provider` but redeclared locally so this module
    doesn't import the provider scaffolding (and so test mocks don't have to
    implement the full `Provider` protocol).
    """

    def generate(self, prompt: str) -> str:  # pragma: no cover - protocol stub
        ...


def _format_tags(tags: Iterable[str]) -> str:
    """Render a tag list for the prompt: comma-joined, lowercased, sorted.

    Sorted + lowercased makes the prompt deterministic regardless of the
    order the topic generator stored tags in (Sit 5 sorts them, but encoding
    that assumption into the prompt builder also is cheap insurance).
    """
    cleaned = sorted({t.strip().lower() for t in tags if t and t.strip()})
    return ", ".join(cleaned) if cleaned else "(none)"


def build_prompt(
    topic,
    post,
    parent_body: Optional[str] = None,
    extra_system_note: Optional[str] = None,
) -> str:
    """Compose the LLM prompt for a single post.

    The output is a deterministic function of its inputs — same topic, post,
    parent body, and note → byte-for-byte identical prompt. That's load-
    bearing for cache-key stability.

    Sections:
    - System framing (forum role, tone constraints).
    - Topic context: title, category, tags.
    - Post role: OP vs reply, parent excerpt for replies.
    - Optional `extra_system_note`: appended on retries to steer the model
      away from blocklisted content.
    """
    is_op = post.post_number == 1
    role = "starting a new topic" if is_op else "replying to an earlier post"
    tag_list = _format_tags(getattr(topic, "tags", []) or [])

    lines: list[str] = []
    lines.append(
        "You are a member of a fictional fan-community forum. Write a single "
        "post in plain prose, two to four short paragraphs, conversational "
        "tone, no markdown headings, no signature. Do not mention real game "
        "franchises, studios, engines, or characters — everything in this "
        "forum is set in the universe described by the topic context."
    )
    if extra_system_note:
        lines.append(extra_system_note)
    lines.append("")
    lines.append("Topic context:")
    lines.append(f"- Title: {topic.title}")
    lines.append(f"- Category: {topic.category}")
    lines.append(f"- Tags: {tag_list}")
    lines.append("")
    lines.append(f"You are {role}.")
    if not is_op and parent_body:
        # Truncate the parent body so a long thread doesn't blow the prompt
        # budget on a 16k-token recursive quote chain. 600 chars is enough
        # context for a coherent reply without ballooning cost.
        excerpt = parent_body.strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:600].rstrip() + "..."
        lines.append("Parent post excerpt (you are replying to this):")
        lines.append(excerpt)
        lines.append("")
    lines.append("Write the post body now. Output only the body text.")
    return "\n".join(lines)


def _template_fallback(topic, post) -> str:
    """Deterministic body used when the LLM is unavailable.

    Returned without writing to the cache so a future run with a working
    LLM populates the slot properly. The text is intentionally bland so a
    grep for `[placeholder body — LLM unavailable]` flags every fallback
    in the forum at once.
    """
    if post.post_number == 1:
        return f"{topic.title}\n\n[placeholder body — LLM unavailable]"
    return f"Re: {topic.title}\n\n[placeholder body — LLM unavailable]"


def generate_body(
    topic,
    post,
    rng,
    llm: _LLMLike,
    cache,
    blocklist=blocklist_module,
    parent_body: Optional[str] = None,
) -> str:
    """Return the body for `post` under `topic`, hitting cache + LLM as needed.

    Order of operations:

    1. Cache hit on `(topic.id, post.post_number)` → return verbatim. The
       `llm` is NOT touched.
    2. Cache miss → build prompt + call `llm.generate`.
       - If the call raises OR returns an empty/whitespace string, return a
         deterministic template body. The template is NOT cached so a later
         run with a working LLM will populate the slot.
       - If the call returns a string, run it through `blocklist.check`:
         * No hits → write to cache + return.
         * Hits → re-prompt with a per-retry avoidance note up to
           `_MAX_BLOCKLIST_RETRIES` times. After the final retry still trips,
           raise `BlocklistViolation` with the offending terms. The cache is
           untouched on a hard failure so the next run gets a clean attempt.

    `rng` is currently unused but accepted in the signature so callers
    (`generators/posts.py`) don't need a separate code path once retry-time
    perturbation lands.
    """
    cached = cache.get(topic.id, post.post_number)
    if cached is not None:
        return cached

    base_prompt = build_prompt(topic, post, parent_body=parent_body)
    extra_note: Optional[str] = None
    last_hits: list[str] = []

    # Attempt count: 1 initial + up to _MAX_BLOCKLIST_RETRIES retries.
    total_attempts = 1 + _MAX_BLOCKLIST_RETRIES
    for attempt in range(1, total_attempts + 1):
        prompt = (
            base_prompt
            if extra_note is None
            else build_prompt(
                topic, post, parent_body=parent_body, extra_system_note=extra_note
            )
        )
        try:
            raw = llm.generate(prompt)
        except Exception:
            # LLM unavailable / errored — return template body, do NOT cache.
            return _template_fallback(topic, post)

        if not raw or not raw.strip():
            # Empty/whitespace from the provider counts as unavailable for
            # body purposes. Don't retry an empty response — the prompt isn't
            # the issue, the provider is. Drop to the template fallback.
            return _template_fallback(topic, post)

        body = raw.strip()
        hits = blocklist.check(body)
        if not hits:
            cache.set(topic.id, post.post_number, body)
            return body

        # Hit. Stage a retry with a stronger system note that names the
        # offending term(s) so the model has a concrete steer.
        last_hits = hits
        extra_note = (
            f"Avoid mentioning these terms or their franchises: "
            f"{', '.join(hits)}. Use only names from the topic context."
        )

    # Exhausted retries with banned content still present.
    raise BlocklistViolation(last_hits, attempts=total_attempts)
