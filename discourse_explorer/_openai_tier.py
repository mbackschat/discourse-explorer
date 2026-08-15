"""OpenAI rate-limit probe + concurrency recommender.

Purpose: before committing to a long `--index` run, know what OpenAI will
actually let us do. We don't care about the tier *label* ("Tier 3") — we care
about the per-model RPM and TPM ceilings, because those determine how far we
can safely bump `llm_model_max_async` without stuttering.

The probe sends a 1-token ping to the specified chat model and reads the
`x-ratelimit-limit-*` headers in the response. Those headers are the ground
truth for your account + model combination, and they're free to observe
(the ping itself costs a fraction of a cent).

Usage (inline, from query.py `--detect-limits`):
    from discourse_explorer._openai_tier import probe_and_recommend
    rec = probe_and_recommend("gpt-4.1-mini")
    print(rec)

Returns something like:
    {
        "model": "gpt-4.1-mini",
        "rpm": 5000,
        "tpm": 2000000,
        "recommended": {
            "llm_model_max_async": 16,
            "max_parallel_insert": 4,
        },
        "tier_hint": "Tier 3 (≥5000 RPM)",
    }
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error


def probe_rate_limits(model: str, api_key: str | None = None) -> dict[str, int]:
    """Ping the chat completions endpoint and read rate-limit headers.

    Returns {"rpm": int, "tpm": int}. Raises on auth/network errors.
    Cost: ~$0.0001 (one 1-token completion).
    """
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        # 429 can still carry useful rate-limit headers; read them.
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        if "x-ratelimit-limit-requests" not in headers:
            raise RuntimeError(f"OpenAI probe failed: {e.code} {e.reason}") from e

    try:
        return {
            "rpm": int(headers["x-ratelimit-limit-requests"]),
            "tpm": int(headers["x-ratelimit-limit-tokens"]),
        }
    except KeyError as e:
        raise RuntimeError(
            f"OpenAI response missing rate-limit headers: {sorted(headers)}"
        ) from e


def recommend_concurrency(
    rpm: int,
    tpm: int,
    avg_tokens_per_call: int = 2500,
    avg_latency_s: float = 2.0,
) -> dict[str, int]:
    """Derive safe concurrency settings from observed rate limits.

    Philosophy:
      - Leave 50% RPM headroom — don't stutter at peaks.
      - Leave 50% TPM headroom.
      - `llm_model_max_async` = simultaneous in-flight calls. Each worker
        completes ~(60/avg_latency_s) calls/min, so peak RPM with N workers
        is N × 60 / avg_latency_s; peak TPM is N × avg_tokens × 60 / latency.
      - Cap at 32 — beyond that LightRAG's internal queuing and your OpenAI
        connection pool become the bottleneck, not the API.

    Defaults (avg_tokens_per_call=2500, avg_latency_s=2.0) reflect real
    measurements on gpt-4.1-mini doing entity extraction with gleaning=1.
    Tune if you switch models or observe very different latency.
    """
    # Effective calls/min per worker = 60 / latency
    calls_per_min_per_worker = 60.0 / avg_latency_s
    # RPM bound: N × calls_per_min_per_worker ≤ rpm × 0.5
    rpm_bound = max(1, int(rpm * 0.5 / calls_per_min_per_worker))
    # TPM bound: N × (avg_tokens × calls_per_min_per_worker) ≤ tpm × 0.5
    tpm_bound = max(1, int(tpm * 0.5 / (avg_tokens_per_call * calls_per_min_per_worker)))
    llm_max_async = max(4, min(rpm_bound, tpm_bound, 32))

    # Parallel inserts: modestly below llm_max_async (they share the LLM budget).
    # LightRAG's per-doc cascade saturates quickly, so a cap of 4 is plenty.
    max_parallel_insert = max(2, min(llm_max_async // 4, 4))

    return {
        "llm_model_max_async": llm_max_async,
        "max_parallel_insert": max_parallel_insert,
    }


def _tier_hint(rpm: int) -> str:
    """Best-guess tier label from observed RPM. Not authoritative."""
    if rpm >= 30000:
        return "Tier 5 (≥30000 RPM)"
    if rpm >= 10000:
        return "Tier 4 (≥10000 RPM)"
    if rpm >= 5000:
        return "Tier 3 (≥5000 RPM)"
    if rpm > 500:
        return "Tier 2+ (>500 RPM)"
    return "Tier 1 or 2 (≤500 RPM)"


def probe_and_recommend(model: str, api_key: str | None = None) -> dict:
    """One-shot: probe + recommend. Use this from the CLI / SKILL."""
    limits = probe_rate_limits(model, api_key)
    rec = recommend_concurrency(limits["rpm"], limits["tpm"])
    return {
        "model": model,
        "rpm": limits["rpm"],
        "tpm": limits["tpm"],
        "recommended": rec,
        "tier_hint": _tier_hint(limits["rpm"]),
    }
