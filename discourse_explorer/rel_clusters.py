"""Edge-keyword → relationship-bucket mapping.

Hybrid design, mirrors the entity-type model (Discourse-universal pins +
corpus-derived content types):

- **Pinned layer** — Discourse-universal relations defined in
  `config.STRUCTURAL_REL_PINS` and authored by Pass 1 in `query.py` via
  `STRUCTURAL_REL_KEYWORDS`. Keywords that match those canonical strings
  bypass clustering entirely and go straight to their fixed bucket.

- **Discovered layer** — the remaining (LLM-extracted, free-form) keywords
  get clustered into `max_rel_types` buckets via one of two modes
  auto-selected by `OPENAI_API_KEY`:

  - **llm-cluster** (OpenAI): embed every unique keyword
    (`text-embedding-3-*`), k-means at `cache_k=50` once, then
    hierarchically merge centroids down to the requested display N at viz
    time. Free-form LLM labels per merged super-cluster (one batched call
    per new N, ~$0.0004).

  - **token-cluster** (no OpenAI): top-N most-frequent keyword *tokens*
    (after stop-word filtering + stem-overlap dedup) become bucket
    centers; keywords routed via substring/token overlap. Pure-Python,
    deterministic, free.

Both modes share a cache at `<data-dir>/visualize/cache/rel-clusters.json`
whose schema is mode-tagged. Cache stores raw analysis output (centroids
or token counts) plus per-N derived clusterings, so changing
`--max-rel-types` is free up to `cache_k` (llm mode) or always (token
mode). Pins are not cached — they come from `config.STRUCTURAL_REL_PINS`
at load time. Colors are not stored either — `visualize.py` paints names
from a palette at render time.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import networkx as nx
import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.cluster.vq import kmeans2

from discourse_explorer.config import (
    STRUCTURAL_REL_PINS,
    RuntimeConfig,
)

_CACHE_SCHEMA = 3
_DEFAULT_MAX_REL_TYPES = 12
_DEFAULT_CACHE_K = 50
_RANDOM_SEED = 42
_EMBED_BATCH = 2048
_SAMPLES_PER_CLUSTER = 20
# Post-Ward rebalance: if the largest discovered bucket exceeds
# `threshold × median`, peel off its most-outlying centroids and fold
# them into the nearest non-largest bucket until balance is within the
# threshold. Respects the N cap — never creates new buckets. Disable
# with `--balance-threshold inf`.
_DEFAULT_BALANCE_THRESHOLD = 4.0
# Minimum bucket size as a fraction of total edges (%). Buckets below
# this are dropped and their slot reused by splitting the biggest
# bucket via k-means. Avoids absurdly tiny "2-edge DataMigration"
# legend entries. User-tunable via `--min-bucket-pct`.
_DEFAULT_MIN_BUCKET_PCT = 0.5

_OTHER_BUCKET_NAME = "Other"


def _build_pin_keyword_map() -> dict[str, int]:
    """keyword (lowercased) → pin bucket index in `STRUCTURAL_REL_PINS` order."""
    out: dict[str, int] = {}
    for idx, pin in enumerate(STRUCTURAL_REL_PINS):
        for kw in pin.keywords_csv.split(","):
            kw = kw.strip().lower()
            if kw:
                out[kw] = idx
    return out


_PIN_KEYWORD_TO_PIN_IDX: dict[str, int] = _build_pin_keyword_map()


# Token-cluster stop-word filter. Noise removal, not a vocabulary.
_TOKEN_STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "in", "on", "for", "with", "by", "from",
    "and", "or", "as", "at", "is", "are", "be", "this", "that", "it", "its",
    "via", "into", "out", "up", "down", "over", "under", "between",
    "type", "thing", "stuff", "item",
})

_TOKENIZE_RE = re.compile(r"[a-z][a-z0-9]+")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    """A relationship-type bucket — vocabulary only, no presentation data.

    `origin` tells `visualize.py` whether this is a Discourse pin, a
    discovered cluster, or the `Other` sink — which lets it section the
    legend into "Forum primitives" vs "Discovered themes" and assign
    colors in the correct pin-first, discovery-second order.
    """
    name: str
    origin: str  # "pinned" | "discovered" | "other"


@dataclass(frozen=True)
class ClusterMap:
    """What `visualize.py` consumes.

    `buckets` layout (indices 0..N-1):
      [pinned...][discovered sorted by size, largest first][Other]

    `keyword_to_bucket_idx` maps every keyword the cache/pins recognize to a
    position in `buckets`. Unknown keywords → `other_idx`.
    """
    buckets: list[Bucket]
    keyword_to_bucket_idx: dict[str, int]
    mode: str  # "llm-cluster" | "token-cluster" | "empty"

    @property
    def other_idx(self) -> int:
        return len(self.buckets) - 1

    @property
    def pinned_names(self) -> list[str]:
        return [b.name for b in self.buckets if b.origin == "pinned"]

    @property
    def discovered_names(self) -> list[str]:
        return [b.name for b in self.buckets if b.origin == "discovered"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def harvest_keywords(G: nx.Graph) -> dict[str, int]:
    """Return {keyword_lowercased: occurrence_count} across every edge."""
    counts: dict[str, int] = {}
    for _, _, data in G.edges(data=True):
        kws = data.get("keywords") or ""
        if not kws.strip():
            continue
        for kw in kws.replace("<SEP>", ",").split(","):
            kw = kw.strip().lower()
            if kw:
                counts[kw] = counts.get(kw, 0) + 1
    return counts


def harvest_edge_keywords(G: nx.Graph) -> list[list[str]]:
    """Return one list of lowercased keywords per edge (order matches
    `G.edges()`). Used by the rebalancer to simulate `_classify_edge`'s
    per-edge bucket voting, which is what the visualizer's sidebar counts
    reflect — balancing against plain keyword-occurrence counts
    over-weights edges that happen to have many same-bucket keywords.
    """
    out: list[list[str]] = []
    for _, _, data in G.edges(data=True):
        kws = data.get("keywords") or ""
        if not kws.strip():
            out.append([])
            continue
        row = [kw.strip().lower() for kw in kws.replace("<SEP>", ",").split(",")]
        out.append([kw for kw in row if kw])
    return out


def load_or_build(
    rc: RuntimeConfig,
    keyword_counts: dict[str, int],
    *,
    edge_keywords: list[list[str]] | None = None,
    max_rel_types: int = _DEFAULT_MAX_REL_TYPES,
    cache_k: int = _DEFAULT_CACHE_K,
    balance_threshold: float = _DEFAULT_BALANCE_THRESHOLD,
    min_bucket_pct: float = _DEFAULT_MIN_BUCKET_PCT,
    force_rebuild: bool = False,
) -> ClusterMap:
    """Return a ClusterMap with `len(STRUCTURAL_REL_PINS)` pin buckets + up
    to `max_rel_types` discovered buckets + Other.

    Auto-selects discovery mode by `rc.is_openai`. Loads the cache when
    valid; rebuilds on schema/model mismatch or when `cache_k` exceeds the
    cached value, or when the cached N-clustering was built with a
    different `balance_threshold`. `force_rebuild` always wipes from scratch.
    """
    cache_path = rc.paths().data_dir / "visualize" / "cache" / "rel-clusters.json"

    if not keyword_counts:
        return ClusterMap(
            buckets=[Bucket(_OTHER_BUCKET_NAME, "other")],
            keyword_to_bucket_idx={},
            mode="empty",
        )

    pinned_keyword_counts = {
        kw: keyword_counts[kw]
        for kw in keyword_counts if kw in _PIN_KEYWORD_TO_PIN_IDX
    }
    discoverable_keyword_counts = {
        kw: keyword_counts[kw]
        for kw in keyword_counts if kw not in _PIN_KEYWORD_TO_PIN_IDX
    }

    cache_data = None if force_rebuild else _load_cache(cache_path)
    cache_data = _validate_cache(cache_data, rc, cache_k)

    # Keyword-signature check: invalidate the cache when the underlying
    # graph's discoverable keyword set has drifted (e.g., after a re-index
    # against fresh content). Without this, _validate_cache only checks
    # schema/mode/embedding-model/cache_k and would silently reuse a
    # cache whose centroids cover almost none of the current keywords —
    # leaving most edges in the Other bucket. Skipped for empty-mode
    # caches (no keywords to compare against).
    if cache_data is not None and cache_data.get("mode") != "empty":
        expected_sig = _keyword_signature(discoverable_keyword_counts)
        cached_sig = cache_data.get("keyword_signature")
        if cached_sig != expected_sig:
            print(f"  Keyword cluster cache built on different keyword "
                  f"set (sig {cached_sig or 'missing'} -> {expected_sig}); "
                  f"rebuilding.")
            cache_data = None

    if cache_data is None:
        if not discoverable_keyword_counts:
            print(f"  Keyword clusters: no discoverable keywords "
                  f"(all matched Discourse pins); skipping discovery.")
            cache_data = _empty_cache(rc)
        elif rc.is_openai:
            print(f"  Keyword clusters: building llm-cluster cache "
                  f"({len(discoverable_keyword_counts):,} discoverable "
                  f"keywords after pinning {len(pinned_keyword_counts):,}; "
                  f"embed={rc.openai_embed_model}/{rc.openai_embed_dim}d, "
                  f"cache_k={cache_k}). One-time ~$0.001-$0.005.")
            cache_data = asyncio.run(
                _build_llm_cache(rc, discoverable_keyword_counts, cache_k))
        else:
            print(f"  Keyword clusters: building token-cluster cache "
                  f"({len(discoverable_keyword_counts):,} discoverable "
                  f"keywords after pinning {len(pinned_keyword_counts):,}; "
                  f"no OpenAI key — local token-frequency analysis, free).")
            cache_data = _build_token_cache(discoverable_keyword_counts)

    # Per-N cache is invalidated when balance_threshold changed, since a
    # different threshold produces different centroid-to-bucket assignments
    # (and thus different bucket names/members). Only relevant for
    # llm-cluster mode — token-cluster doesn't rebalance.
    if (cache_data["mode"] == "llm-cluster"
            and str(max_rel_types) in cache_data["clusterings"]):
        cluster_entry = cache_data["clusterings"][str(max_rel_types)]
        cached_thresh = cluster_entry.get("balance_threshold")
        cached_min_pct = cluster_entry.get("min_bucket_pct")
        invalid = (
            cached_thresh is None
            or float(cached_thresh) != float(balance_threshold)
            or cached_min_pct is None
            or float(cached_min_pct) != float(min_bucket_pct)
        )
        if invalid:
            print(f"  Keyword clusters: N={max_rel_types} cached with "
                  f"(balance_threshold={cached_thresh}, "
                  f"min_bucket_pct={cached_min_pct}) but current run "
                  f"requests ({balance_threshold}, {min_bucket_pct}); "
                  f"re-deriving.")
            del cache_data["clusterings"][str(max_rel_types)]

    if cache_data["mode"] != "empty" and str(max_rel_types) not in cache_data["clusterings"]:
        if cache_data["mode"] == "llm-cluster":
            print(f"  Keyword clusters: deriving N={max_rel_types} from "
                  f"cached centroids (one LLM relabel call, ~$0.0004)...")
            asyncio.run(
                _ensure_llm_clustering(rc, cache_data, max_rel_types,
                                       discoverable_keyword_counts,
                                       edge_keywords=edge_keywords,
                                       balance_threshold=balance_threshold,
                                       min_bucket_pct=min_bucket_pct))
        else:
            print(f"  Keyword clusters: deriving N={max_rel_types} from "
                  f"cached token counts (free)...")
            _ensure_token_clustering(cache_data, max_rel_types,
                                     discoverable_keyword_counts)
    elif cache_data["mode"] != "empty":
        print(f"  Keyword clusters: cache hit for N={max_rel_types} "
              f"({cache_data['mode']} mode).")

    _save_cache(cache_path, cache_data)
    return _to_cluster_map(cache_data, max_rel_types)


# ---------------------------------------------------------------------------
# Cache I/O + validation
# ---------------------------------------------------------------------------


def _empty_cache(rc: RuntimeConfig) -> dict:
    return {
        "version": _CACHE_SCHEMA,
        "mode": "empty",
        "clusterings": {},
    }


def _keyword_signature(discoverable_keywords: dict[str, int]) -> str:
    """Stable hex digest of the discoverable keyword set, used to invalidate
    the rel-clusters cache when the underlying graph's keyword distribution
    drifts. Mirrors the structural-signature pattern in `visualize.py`'s
    layout cache. Sorted-keys + newline-join means the digest is stable
    against insertion order and isn't sensitive to occurrence counts (a
    keyword's bucket assignment doesn't depend on its frequency)."""
    import hashlib
    keys = sorted(discoverable_keywords.keys())
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def _load_cache(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  Keyword cluster cache unreadable "
              f"({type(e).__name__}); rebuilding.", file=sys.stderr)
        return None


def _validate_cache(
    cache: Optional[dict],
    rc: RuntimeConfig,
    cache_k: int,
) -> Optional[dict]:
    if cache is None:
        return None
    if cache.get("version") != _CACHE_SCHEMA:
        print(f"  Keyword cluster cache schema mismatch "
              f"(expected {_CACHE_SCHEMA}, got {cache.get('version')}); "
              f"rebuilding.")
        return None

    cached_mode = cache.get("mode")
    desired_mode = "llm-cluster" if rc.is_openai else "token-cluster"
    if cached_mode == "empty":
        cache.setdefault("clusterings", {})
        return cache
    if cached_mode != desired_mode:
        print(f"  Keyword cluster cache mode changed "
              f"({cached_mode} → {desired_mode}); rebuilding.")
        return None

    if desired_mode == "llm-cluster":
        if cache.get("embedding_model") != rc.openai_embed_model:
            print(f"  Keyword cluster cache embedding model changed "
                  f"({cache.get('embedding_model')} → "
                  f"{rc.openai_embed_model}); rebuilding.")
            return None
        if int(cache.get("embedding_dim", 0)) != int(rc.openai_embed_dim):
            print(f"  Keyword cluster cache embedding dim mismatch; "
                  f"rebuilding.")
            return None
        if int(cache.get("cache_k", 0)) < cache_k:
            print(f"  Keyword cluster cache_k "
                  f"({cache.get('cache_k')}) < requested ({cache_k}); "
                  f"rebuilding to grow base clustering.")
            return None

    cache.setdefault("clusterings", {})
    return cache


def _save_cache(path: Path, cache_data: dict) -> None:
    """Write the cache, skipping if nothing meaningful changed.

    `generated_at` is a bookkeeping field that would churn on every run if
    we stamped it unconditionally, so we only refresh it (and write) when
    the rest of the payload differs from what's already on disk. This
    turns cache-hit viz runs into zero-write operations for rel-clusters.json
    (same pattern as the data.js skip-if-identical gate in visualize.py).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    def _without_timestamp(d: dict) -> dict:
        return {k: v for k, v in d.items() if k != "generated_at"}

    if _without_timestamp(existing) == _without_timestamp(cache_data):
        # Preserve the prior timestamp so the field on disk stays stable
        # too — a second no-op run won't show a "modified" mtime.
        return

    cache_data["generated_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    path.write_text(json.dumps(cache_data, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# llm-cluster mode (operates on discoverable keywords only)
# ---------------------------------------------------------------------------


async def _build_llm_cache(
    rc: RuntimeConfig,
    keyword_counts: dict[str, int],
    cache_k: int,
) -> dict:
    import time
    keywords = sorted(keyword_counts.keys())
    effective_k = min(cache_k, len(keywords))
    total_batches = (len(keywords) + _EMBED_BATCH - 1) // _EMBED_BATCH

    print(f"    [1/3] Embedding {len(keywords):,} unique keywords "
          f"({rc.openai_embed_model}, {rc.openai_embed_dim}d, "
          f"{total_batches} batch{'es' if total_batches != 1 else ''})...",
          flush=True)
    t0 = time.perf_counter()
    embeddings = await _embed_keywords(rc, keywords)
    print(f"    [1/3] Embedding complete in {time.perf_counter() - t0:.1f}s "
          f"({embeddings.shape[0]:,} × {embeddings.shape[1]}d).",
          flush=True)

    print(f"    [2/3] Running k-means at cache_k={effective_k} "
          f"(seed={_RANDOM_SEED}, minit='++')...", flush=True)
    t0 = time.perf_counter()
    centroids, labels = kmeans2(
        embeddings, k=effective_k, seed=_RANDOM_SEED,
        minit="++", missing="warn",
    )
    # Report cluster size distribution so abnormal skew is visible.
    counts_per_centroid = np.bincount(labels, minlength=effective_k)
    nonempty = int((counts_per_centroid > 0).sum())
    print(f"    [2/3] K-means complete in {time.perf_counter() - t0:.1f}s "
          f"({nonempty}/{effective_k} centroids populated; "
          f"largest={int(counts_per_centroid.max()):,}, "
          f"smallest={int(counts_per_centroid[counts_per_centroid > 0].min()):,}).",
          flush=True)
    print(f"    [3/3] Writing cache header to disk (centroids + "
          f"keyword-to-centroid map for {len(keywords):,} keywords)...",
          flush=True)

    return {
        "version": _CACHE_SCHEMA,
        "mode": "llm-cluster",
        "embedding_model": rc.openai_embed_model,
        "embedding_dim": rc.openai_embed_dim,
        "cache_k": int(effective_k),
        "keyword_signature": _keyword_signature(keyword_counts),
        "centroids": centroids.tolist(),
        "keyword_to_centroid": {
            kw: int(labels[i]) for i, kw in enumerate(keywords)
        },
        "clusterings": {},
    }


async def _embed_keywords(
    rc: RuntimeConfig, keywords: list[str],
) -> np.ndarray:
    """Batched OpenAI embedding. Returns L2-normalized (N, D) float32."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    vectors: list[list[float]] = []
    for start in range(0, len(keywords), _EMBED_BATCH):
        batch = keywords[start:start + _EMBED_BATCH]
        resp = await client.embeddings.create(
            model=rc.openai_embed_model, input=batch,
        )
        vectors.extend(d.embedding for d in resp.data)
        print(f"      Embedded {min(start + len(batch), len(keywords)):,}/"
              f"{len(keywords):,}...", flush=True)
    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def _prune_and_split(
    *,
    centroid_to_bucket_idx: list[int],
    centroids: np.ndarray,
    keyword_to_centroid: dict,
    edge_keywords: list[list[str]],
    balance_threshold: float,
    min_bucket_pct: float,
    max_iters: int = 30,
) -> tuple[list[int], dict]:
    """Unified bucket-cleanup pass.

    Per iteration:
      - Compute per-bucket edge count (simulated by `_classify_edge`'s
        per-edge voting on the current centroid→bucket map).
      - If any bucket is below `min_bucket_pct × total_edges`: drop the
        smallest tiny bucket (reassign its centroids to the nearest
        non-tiny peer by embedding distance), then split the biggest
        bucket into two via k-means to fill the freed slot. Net: bucket
        count stays at N, a semantically-thin group is replaced by a
        split of the dominant theme.
      - Else if `max / median > balance_threshold`: split the biggest
        bucket anyway, placing one half into the smallest bucket's slot
        (merge tiny into nearest peer first). Drives max/median down.
      - Else: done.

    Respects the strict N cap — never creates or destroys bucket slots;
    only reshuffles which centroids live where. Returns the updated
    mapping plus a stats dict for progress reporting.
    """
    import statistics
    centroid_to_bucket_idx = list(centroid_to_bucket_idx)
    if not centroid_to_bucket_idx:
        return centroid_to_bucket_idx, {
            "iterations": 0, "prunes": 0, "extra_splits": 0,
            "before_max": 0, "after_max": 0,
            "before_ratio": 1.0, "after_ratio": 1.0,
        }
    n = max(centroid_to_bucket_idx) + 1

    # Pre-build edge → centroid-list. Edges whose only keywords are pins
    # (not in keyword_to_centroid) contribute an empty list and don't
    # count toward any discovered bucket — matches real viz behavior.
    edge_centroid_lists: list[list[int]] = []
    for kwlist in edge_keywords:
        cids: list[int] = []
        for kw in kwlist:
            cid = keyword_to_centroid.get(kw)
            if cid is not None:
                cids.append(int(cid))
        edge_centroid_lists.append(cids)

    def bucket_sizes() -> list[int]:
        """Edge counts per bucket via Counter vote over each edge's
        discoverable keywords. Matches `_classify_edge` semantics."""
        sizes = [0] * n
        for cids in edge_centroid_lists:
            if not cids:
                continue
            votes: dict[int, int] = {}
            for cid in cids:
                b = centroid_to_bucket_idx[cid]
                votes[b] = votes.get(b, 0) + 1
            best = min(votes.items(), key=lambda x: (-x[1], x[0]))[0]
            sizes[best] += 1
        return sizes

    def bucket_means() -> np.ndarray:
        sums = np.zeros((n, centroids.shape[1]), dtype=centroids.dtype)
        counts = [0] * n
        for ci, b in enumerate(centroid_to_bucket_idx):
            sums[b] += centroids[ci]
            counts[b] += 1
        for i in range(n):
            if counts[i]:
                sums[i] /= counts[i]
        return sums

    def ratio(sizes: list[int]) -> float:
        nonzero = [s for s in sizes if s > 0]
        if len(nonzero) < 2:
            return 1.0
        return max(nonzero) / statistics.median(nonzero)

    initial_sizes = bucket_sizes()
    initial_max = max(initial_sizes)
    initial_ratio = ratio(initial_sizes)

    prunes = 0
    extra_splits = 0
    iterations = 0

    def drain(target_bucket: int) -> None:
        """Reassign every centroid in `target_bucket` to its nearest
        mid-tier peer (excluding both the target bucket and the current
        biggest bucket). Afterwards the target is empty and ready to be
        refilled by a split. Excluding biggest is critical — without it,
        the tiny bucket's centroids drift toward the biggest (often
        their nearest peer), and the subsequent split starts from a
        bucket that's now *larger* than before. Net-worse outcome.
        """
        means = bucket_means()
        sizes = bucket_sizes()
        biggest = max(range(n), key=lambda i: sizes[i]) if any(sizes) else -1
        for ci in [c for c, b in enumerate(centroid_to_bucket_idx) if b == target_bucket]:
            best_b, best_d = -1, float("inf")
            for b in range(n):
                if b == target_bucket or b == biggest:
                    continue
                if not any(bb == b for bb in centroid_to_bucket_idx):
                    continue
                d = float(np.linalg.norm(centroids[ci] - means[b]))
                if d < best_d:
                    best_d, best_b = d, b
            if best_b >= 0:
                centroid_to_bucket_idx[ci] = best_b
            # If no mid-tier peer exists (edge case: all buckets empty
            # except target + biggest), fall back to biggest so no
            # centroid is orphaned. Typical corpora won't hit this.
            elif biggest >= 0:
                centroid_to_bucket_idx[ci] = biggest

    def split_biggest_into(free_slot: int) -> bool:
        """K-means-2 the biggest bucket's centroids. Relabels half as
        `free_slot`, the other half stays in the biggest's original slot.
        Returns False if the biggest has fewer than 2 centroids.
        """
        sizes = bucket_sizes()
        biggest = max(range(n), key=lambda i: sizes[i])
        big_members = [ci for ci, b in enumerate(centroid_to_bucket_idx) if b == biggest]
        if len(big_members) < 2:
            return False
        big_pts = centroids[big_members]
        _, sub = kmeans2(big_pts, k=2, seed=_RANDOM_SEED, minit="++", missing="warn")
        # Put sub==1 into free_slot, sub==0 stays in biggest.
        # Guard: if one sub got 0 members, kmeans2 is degenerate; skip.
        subs_present = set(int(s) for s in sub)
        if len(subs_present) < 2:
            return False
        for ci, s in zip(big_members, sub):
            if int(s) == 1:
                centroid_to_bucket_idx[ci] = free_slot
        return True

    for _it in range(max_iters):
        iterations += 1
        sizes = bucket_sizes()
        total = sum(sizes)
        if total == 0:
            break
        min_bucket_edges = max(1, int(min_bucket_pct / 100.0 * total))
        nonzero_buckets = [b for b, s in enumerate(sizes) if s > 0]
        if len(nonzero_buckets) < 2:
            break

        tiny_buckets = [b for b in nonzero_buckets if sizes[b] < min_bucket_edges]
        cur_ratio = ratio(sizes)
        needs_balance = (
            balance_threshold != float("inf") and cur_ratio > balance_threshold
        )

        if tiny_buckets:
            # Replace smallest tiny bucket with a split of the biggest.
            victim = min(tiny_buckets, key=lambda b: sizes[b])
            drain(victim)
            if not split_biggest_into(victim):
                break
            prunes += 1
        elif needs_balance:
            # No tiny buckets but max/median still exceeds threshold.
            # Sacrifice the smallest existing bucket to host a split of
            # the biggest — reduces max and raises median in one step.
            victim = min(nonzero_buckets, key=lambda b: sizes[b])
            # If victim IS the biggest, there's no peer — bail.
            if sizes[victim] >= max(sizes):
                break
            drain(victim)
            if not split_biggest_into(victim):
                break
            extra_splits += 1
        else:
            break

    final_sizes = bucket_sizes()
    stats = {
        "iterations": iterations,
        "prunes": prunes,
        "extra_splits": extra_splits,
        "before_max": int(initial_max),
        "after_max": int(max(final_sizes)),
        "before_ratio": float(initial_ratio),
        "after_ratio": float(ratio(final_sizes)),
    }
    return centroid_to_bucket_idx, stats


async def _ensure_llm_clustering(
    rc: RuntimeConfig,
    cache_data: dict,
    n: int,
    keyword_counts: dict[str, int],
    edge_keywords: list[list[str]] | None = None,
    balance_threshold: float = _DEFAULT_BALANCE_THRESHOLD,
    min_bucket_pct: float = _DEFAULT_MIN_BUCKET_PCT,
) -> None:
    import time
    cache_k = int(cache_data["cache_k"])
    if n > cache_k:
        raise ValueError(
            f"max_rel_types={n} exceeds cached cache_k={cache_k}. "
            f"Re-run with `--cache-k {max(n, _DEFAULT_CACHE_K)} "
            f"--regenerate-keyword-clusters` to grow the base clustering."
        )

    centroids = np.asarray(cache_data["centroids"], dtype=np.float32)
    keyword_to_centroid = cache_data["keyword_to_centroid"]

    if n >= cache_k:
        print(f"      [a] Every centroid becomes its own bucket "
              f"(N={n} >= cache_k={cache_k}).", flush=True)
        centroid_to_bucket_idx = list(range(cache_k))
    else:
        print(f"      [a] Hierarchical merge: {cache_k} centroids → "
              f"{n} super-clusters (Ward linkage)...", flush=True)
        t0 = time.perf_counter()
        Z = linkage(centroids, method="ward")
        flat = fcluster(Z, t=n, criterion="maxclust")
        unique = sorted(set(int(x) for x in flat))
        relabel = {old: new for new, old in enumerate(unique)}
        centroid_to_bucket_idx = [relabel[int(x)] for x in flat]
        print(f"      [a] Merge complete in {time.perf_counter() - t0:.2f}s.",
              flush=True)

    # Rebalance: if the biggest bucket dominates the rest, peel off its
    # most-outlying centroids and fold them into peer buckets. Strict N
    # cap preserved (no new buckets). Disabled with threshold=inf.
    if balance_threshold != float("inf") or min_bucket_pct > 0:
        centroid_to_bucket_idx, stats = _prune_and_split(
            centroid_to_bucket_idx=centroid_to_bucket_idx,
            centroids=centroids,
            keyword_to_centroid=keyword_to_centroid,
            edge_keywords=edge_keywords or [],
            balance_threshold=balance_threshold,
            min_bucket_pct=min_bucket_pct,
        )
        print(f"      [a'] Prune-and-split: {stats['iterations']} iterations, "
              f"{stats['prunes']} tiny-bucket replacement(s), "
              f"{stats['extra_splits']} imbalance split(s). "
              f"Max/median ratio {stats['before_ratio']:.1f}x → "
              f"{stats['after_ratio']:.1f}x, biggest "
              f"{stats['before_max']:,} → {stats['after_max']:,}.",
              flush=True)

    actual_n = max(centroid_to_bucket_idx) + 1
    super_members: list[list[tuple[str, int]]] = [
        [] for _ in range(actual_n)
    ]
    for kw, cid in cache_data["keyword_to_centroid"].items():
        bidx = centroid_to_bucket_idx[int(cid)]
        super_members[bidx].append((kw, keyword_counts.get(kw, 0)))
    for members in super_members:
        members.sort(key=lambda x: -x[1])

    samples = [
        [kw for kw, _ in members[:_SAMPLES_PER_CLUSTER]]
        for members in super_members
    ]
    sizes = [sum(c for _, c in members) for members in super_members]

    print(f"      [b] Labeling {actual_n} super-clusters via "
          f"gpt-4.1-mini (one batched call, ~${0.000032 * actual_n:.4f})...",
          flush=True)
    t0 = time.perf_counter()
    bucket_names = await _label_super_clusters(samples)
    print(f"      [b] Labels returned in {time.perf_counter() - t0:.1f}s: "
          f"{', '.join(bucket_names)}", flush=True)

    # Size-rank order → bucket index 0 = largest discovered bucket. The
    # visualizer assigns palette colors in this order, so the dominant
    # bucket keeps a stable color across reruns even if its name shifts.
    # NOTE: previously we reordered bucket indices by size here so the
    # palette's slot-0 always lined up with the biggest bucket. That
    # rename changed tie-break outcomes in `_classify_edge` (which uses
    # `min(..., key=lambda x: (-count, bucket_index))`), re-routing
    # tied-vote edges to different buckets and measurably shifting the
    # distribution from what the rebalancer produced. Solution: don't
    # rename. The visualizer sorts the legend by edge count at render
    # time anyway, so display order is unaffected; only palette
    # slot-assignment loses "biggest = first color." Minor.

    cache_data["clusterings"][str(n)] = {
        "bucket_names": bucket_names,
        "centroid_to_bucket_idx": centroid_to_bucket_idx,
        "balance_threshold": balance_threshold,
        "min_bucket_pct": min_bucket_pct,
    }


async def _label_super_clusters(samples: list[list[str]]) -> list[str]:
    """One batched LLM call: PascalCase label per super-cluster."""
    from openai import AsyncOpenAI

    n = len(samples)
    blocks = "\n\n".join(
        f"Cluster {i}:\n  " + ", ".join(samples[i])
        for i in range(n) if samples[i]
    )
    prompt = (
        f"Below are {n} clusters of edge keywords from a forum knowledge "
        f"graph. For each cluster, give one short PascalCase label "
        f"(1-2 words) that captures its semantic theme. Labels should be "
        f"distinct, specific enough to be useful as UI filter categories, "
        f"and derived from the keyword evidence itself — no generic "
        f"labels like Cluster1. Do NOT reuse Discourse-primitive labels "
        f"(Posted, Tagged, Categorized); those are reserved for the "
        f"structural-pin layer upstream.\n\n"
        f"Output ONLY a JSON object on one line, no markdown fences, no "
        f"prose:\n"
        f'  {{"0": "Label0", "1": "Label1", ...}}\n\n'
        f"Clusters:\n\n{blocks}"
    )

    client = AsyncOpenAI()
    resp = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  LLM cluster labeling returned invalid JSON: {e}",
              file=sys.stderr)
        labels = {}

    result: list[str] = []
    for i in range(n):
        pick = labels.get(str(i)) or labels.get(i)
        if not isinstance(pick, str) or not pick.strip():
            pick = f"Cluster{i}"
            print(f"  Warning: cluster {i} got no LLM label; "
                  f"using {pick!r}.", file=sys.stderr)
        result.append(pick.strip())
    return result


# ---------------------------------------------------------------------------
# token-cluster mode (operates on discoverable keywords only)
# ---------------------------------------------------------------------------


def _tokenize(keyword: str) -> list[str]:
    return [
        t for t in _TOKENIZE_RE.findall(keyword.lower())
        if t not in _TOKEN_STOPWORDS
    ]


def _build_token_cache(keyword_counts: dict[str, int]) -> dict:
    token_counts: dict[str, int] = {}
    for kw, cnt in keyword_counts.items():
        for t in set(_tokenize(kw)):
            token_counts[t] = token_counts.get(t, 0) + cnt
    return {
        "version": _CACHE_SCHEMA,
        "mode": "token-cluster",
        "keyword_signature": _keyword_signature(keyword_counts),
        "token_counts": token_counts,
        "clusterings": {},
    }


def _ensure_token_clustering(
    cache_data: dict,
    n: int,
    keyword_counts: dict[str, int],
) -> None:
    token_counts: dict[str, int] = cache_data["token_counts"]

    seeds: list[str] = []
    for tok, _cnt in sorted(token_counts.items(), key=lambda x: -x[1]):
        if any(tok in s or s in tok for s in seeds):
            continue
        seeds.append(tok)
        if len(seeds) >= n:
            break

    bucket_names = [s.capitalize() for s in seeds]
    actual_n = len(seeds)

    keyword_to_bucket_idx: dict[str, int] = {}
    for kw in keyword_counts:
        toks = _tokenize(kw)
        if not toks:
            continue
        best = None
        for i, seed in enumerate(seeds):
            if any(seed == t or seed in t or t in seed for t in toks):
                if best is None or i < best:
                    best = i
        if best is not None:
            keyword_to_bucket_idx[kw] = best

    sizes = [0] * actual_n
    for kw, bidx in keyword_to_bucket_idx.items():
        sizes[bidx] += keyword_counts.get(kw, 0)

    size_rank = sorted(range(actual_n), key=lambda i: -sizes[i])
    old_to_new = {old: new for new, old in enumerate(size_rank)}
    keyword_to_bucket_idx = {
        kw: old_to_new[bidx] for kw, bidx in keyword_to_bucket_idx.items()
    }
    bucket_names = [bucket_names[i] for i in size_rank]

    cache_data["clusterings"][str(n)] = {
        "bucket_names": bucket_names,
        "keyword_to_bucket_idx": keyword_to_bucket_idx,
    }


# ---------------------------------------------------------------------------
# ClusterMap construction
# ---------------------------------------------------------------------------


def _to_cluster_map(cache_data: dict, n: int) -> ClusterMap:
    """Assemble the final ClusterMap: [pins] + [discovered] + [Other].

    Discovered keyword lookups get their indices shifted by
    `len(STRUCTURAL_REL_PINS)` so they line up with the prepended pins.
    """
    pin_buckets = [Bucket(pin.display_name, "pinned") for pin in STRUCTURAL_REL_PINS]
    pin_offset = len(pin_buckets)

    if cache_data["mode"] == "empty" or str(n) not in cache_data.get("clusterings", {}):
        discovered_buckets: list[Bucket] = []
        discovered_keyword_to_idx: dict[str, int] = {}
    else:
        clustering = cache_data["clusterings"][str(n)]
        discovered_buckets = [
            Bucket(name, "discovered") for name in clustering["bucket_names"]
        ]
        if cache_data["mode"] == "llm-cluster":
            c2b = clustering["centroid_to_bucket_idx"]
            discovered_keyword_to_idx = {
                kw: int(c2b[int(cid)]) + pin_offset
                for kw, cid in cache_data["keyword_to_centroid"].items()
            }
        else:
            discovered_keyword_to_idx = {
                kw: int(bidx) + pin_offset
                for kw, bidx in clustering["keyword_to_bucket_idx"].items()
            }

    # Pins take precedence on any overlap.
    keyword_to_bucket_idx: dict[str, int] = {}
    keyword_to_bucket_idx.update(discovered_keyword_to_idx)
    keyword_to_bucket_idx.update(_PIN_KEYWORD_TO_PIN_IDX)

    buckets = (
        pin_buckets
        + discovered_buckets
        + [Bucket(_OTHER_BUCKET_NAME, "other")]
    )

    return ClusterMap(
        buckets=buckets,
        keyword_to_bucket_idx=keyword_to_bucket_idx,
        mode=cache_data["mode"],
    )
