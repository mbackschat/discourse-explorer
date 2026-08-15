# Relationship clustering algorithm (`rel_clusters.py`)

Deep reference for how edge keywords get classified into the visualizer's relationship-type legend. Covers both cluster modes, the prune-and-split balancer, cache schema, and the (now-fixed) size-rank reorder bug.

## Two-layer architecture

Mirrors the entity-type model: Discourse-universal **pins** + corpus-derived **clusters**.

### Pinned layer (always present)

`config.STRUCTURAL_REL_PINS` is a tuple of `StructuralRelPin(id, display_name, keywords_csv)`:

| `id` | `display_name` | Keywords (canonical CSV) |
|---|---|---|
| `user_posted` | `Posted` | `posted, authored, participated` |
| `topic_tagged` | `Tagged` | `tagged, tag, labeled` |
| `topic_in_category` | `Categorized` | `posted in, category, section` |

These are emitted by `query.py`'s Pass 1 on every topic (`_topic_to_custom_kg`). At classification time, any edge keyword matching one of these strings bypasses the clustering pipeline and routes straight to its pin bucket. Pins always appear in `ClusterMap.buckets` regardless of cache state — the legend shows them even if the corpus is tiny.

The display name (e.g. `Posted`) is the past-tense Discourse UI verb for that relationship type. See `docs/discourse/DISCOURSE_TERMINOLOGY.md`.

### Discovered layer

Remaining (LLM-extracted, free-form) keywords feed one of two modes, auto-selected by `OPENAI_API_KEY`:

#### `llm-cluster` mode (OpenAI configured)

1. **Embed** every unique keyword via `text-embedding-3-*` (batched, `_EMBED_BATCH = 2048`).
2. **K-means** at `cache_k = 50` (default) using `scipy.cluster.vq.kmeans2(seed=42, minit="++")`. Produces 50 base centroids. `effective_k` is clamped to `min(cache_k, len(keywords))` for tiny corpora.
3. **Hierarchical merge** to the requested display `N` via `scipy.cluster.hierarchy.linkage(method="ward")` + `fcluster(t=N, criterion="maxclust")`. Ward on L2-normalized embeddings behaves like cosine distance since `||a-b||² = 2 - 2cos(a,b)` is monotone in cosine distance.
4. **Prune-and-split pass** (see below) — fixes imbalance and drops absurdly tiny buckets.
5. **LLM labeling** — one batched call to `gpt-4.1-mini` with the top-20 most-frequent member keywords per super-cluster. Free-form PascalCase labels. The prompt explicitly tells the LLM to avoid Discourse-primitive labels (`Posted`/`Tagged`/`Categorized`) since those are reserved for the pin layer.

#### `token-cluster` mode (no OpenAI)

1. Tokenize each keyword (letters + digits, length ≥ 2, stop-word filter).
2. Top-N most-frequent tokens become bucket seeds. Skip tokens that are stem-substrings of an already-picked seed (avoids `error` + `errors` both becoming buckets).
3. Route each keyword to the bucket whose seed has the most overlap with its tokens.
4. Bucket names = capitalized seed tokens.

No prune-and-split (the discrete token frequency ladder naturally produces flatter distributions than embedding clustering in dense semantic regions).

## Prune-and-split balancer

Fires after Ward merge in `llm-cluster` mode. Replaces an earlier single-centroid-move rebalancer that ping-ponged on dense embedding regions (moving a centroid A→B made B the new biggest; next iteration moved one B→A; 30 moves with zero net size change).

### The problem

Ward merge on `cache_k = 50` centroids down to `N = 10` super-clusters tends to produce one "vacuum cleaner" bucket that absorbs dense embedding regions. On a typical Discourse corpus, a single bucket can end up with 40–97% of all discovered edges while the other 9 are nearly empty (with some legitimately tiny at 2–30 edges). Users see an uninformative legend dominated by one generic label.

### The algorithm

```
while any(bucket too small) or (max / median > balance_threshold):
    if any bucket too small:
        drain smallest tiny bucket → mid-tier peers (NOT biggest)
        k-means-2 split biggest → one half keeps biggest's slot,
                                   other half takes drained slot
    elif max / median > balance_threshold:
        drain smallest non-tiny bucket → mid-tier peers (NOT biggest)
        k-means-2 split biggest → same as above
    else:
        break
```

Strict `N` cap preserved — never creates or destroys slots. One k-means-2 split is the primitive for both branches, so the biggest bucket shrinks ~50% per iteration on dense lumps.

### The subtle bug (don't drain into biggest)

Initial implementation drained a tiny bucket's centroids to their *overall-nearest* peers by embedding distance. In dense semantic regions, the biggest bucket is usually the nearest peer for any given centroid. Draining inflated biggest *before* the split, so the subsequent k-means-2 started from a larger bucket than before — net distribution got *worse*.

Fix: exclude the biggest from drain targets. Centroids migrate to mid-tier peers. Edge case: all buckets empty except target + biggest → fall back to biggest so no centroid is orphaned. (Doesn't happen on realistic corpora.)

### Edge-voting metric

Bucket sizes during balance are computed by **simulating `_classify_edge`'s per-edge Counter vote**, not by summing `keyword_counts` per bucket. This matters because:

- `keyword_counts[kw]` is an occurrence count: if edge E has keywords `"error, cause, reporting"`, it contributes +1 to each of those.
- `_classify_edge` votes once per edge: same edge contributes +1 to the bucket winning the most votes.

Keyword-occurrence bulk and edge-count-per-bucket are different metrics, and the sidebar shows edge counts. Balancing against occurrences doesn't move the numbers the user actually sees. `_rebalance_buckets` (in an earlier commit) made this mistake — fix was to pre-build `edge_centroid_lists` and simulate the vote.

### Configuration

| Flag | Default | Effect |
|---|---|---|
| `--balance-threshold F` | `4.0` | Trigger the imbalance branch when `max_size / median_size > F`. Pass `inf` to disable. |
| `--min-bucket-pct F` | `0.5` | Drop buckets smaller than `F %` of total edges. Pass `0` to disable. |

Per-N clustering re-derives when either flag value differs from the cached run — cheap (same one LLM relabel call).

## Cache schema v3

On disk at `<data-dir>/visualize/cache/rel-clusters.json`. Mode-tagged so load/validate can route correctly.

### `llm-cluster` shape

```json
{
  "version": 3,
  "mode": "llm-cluster",
  "embedding_model": "text-embedding-3-large",
  "embedding_dim": 3072,
  "cache_k": 50,
  "centroids": [[...3072 floats...], ...],
  "keyword_to_centroid": {"error cause": 17, ...},
  "clusterings": {
    "12": {
      "bucket_names": ["ComponentIntegration", "IssueResolution", ...],
      "centroid_to_bucket_idx": [0, 1, 0, 2, ...],
      "balance_threshold": 4.0,
      "min_bucket_pct": 0.5
    }
  }
}
```

`clusterings` is a dict keyed by requested `N`. Adding a new N only re-runs the hierarchical merge + prune-and-split + one LLM relabel — the centroids are reused.

### `token-cluster` shape

```json
{
  "version": 3,
  "mode": "token-cluster",
  "token_counts": {"error": 1420, "version": 800, ...},
  "clusterings": {
    "12": {
      "bucket_names": ["Error", "Version", ...],
      "keyword_to_bucket_idx": {"error cause": 0, ...}
    }
  }
}
```

### No color data

Bucket colors are NOT stored in the cache. `visualize.py` paints bucket names from `_PALETTE` at render time via `_assign_colors(pinned_names, discovered_names)`. Keeping colors out of the cache means the palette can change without invalidating the data.

### Invalidation rules

| Trigger | What's invalidated |
|---|---|
| Schema version changed | Everything (full rebuild) |
| Mode changed (llm ↔ token) | Everything |
| Embedding model changed (llm-cluster only) | Everything |
| Embedding dim changed | Everything |
| `cache_k` grew above cached value | Everything |
| `balance_threshold` or `min_bucket_pct` differ from cached N | Only that N's clustering (cheap) |
| `--regenerate-keyword-clusters` | Everything (forced) |

### Skip-if-identical write

`_save_cache` compares the new payload (minus `generated_at` timestamp) to what's on disk. Identical → skip the write. Keeps mtime stable across no-op runs and avoids rewriting the 3 MB JSON for cache-hit viz runs.

## Removed: post-LLM size-rank reorder (and why)

Earlier versions re-numbered bucket indices after LLM labeling so palette slot 0 always went to the biggest bucket (stable "red = biggest" across corpora).

Why it was removed: the reorder is a *renaming* that should be semantically equivalent to the original partition. But `_classify_edge` tie-breaks on bucket index (`min(votes, key=lambda x: (-count, index))` — smaller index wins ties). Changing the indices re-routed tied-vote edges to different buckets. Net: the classification post-reorder differed from what the rebalancer had just produced. Diagnosed via DIAGs printed before and after the reorder showing identical totals but different size multisets.

Fix: don't reorder. `visualize.py` sorts the legend by edge count at render time anyway, so display order is unaffected. Only palette slot-assignment loses "biggest = first color," which is cosmetic.

## Progress output

During a full cache build:

```
[1/3] Embedding 11,254 unique keywords (text-embedding-3-large, 3072d, 6 batches)...
  Embedded 2,048/11,254...
  ...
[1/3] Embedding complete in 109.2s
[2/3] Running k-means at cache_k=50 (seed=42, minit='++')...
[2/3] K-means complete in 15.3s (50/50 centroids populated; largest=524, smallest=75)
[3/3] Writing cache header to disk...

[a] Hierarchical merge: 50 centroids → 10 super-clusters (Ward linkage)...
[a] Merge complete in 0.01s
[a'] Prune-and-split: 6 iterations, 4 tiny-bucket replacement(s), 1 imbalance split(s).
     Max/median ratio 71.1x → 2.7x, biggest 12,836 → 3,985
[b] Labeling 10 super-clusters via gpt-4.1-mini (one batched call, ~$0.0003)...
[b] Labels returned in 1.4s: VersionManagement, ConfigurationSetup, ...
```

Each stage prints elapsed wall-clock time. LLM labeling cost is estimated per cluster count (`N × $0.000032`).

## Cost summary

First-time build on an 11k-keyword corpus:

| Step | Cost | One-time? |
|---|---|---|
| Embedding (6 batches × ~2000 keywords) | ~$0.003 (`-large`) / ~$0.0005 (`-small`) | yes — cached |
| K-means | free | yes — cached |
| Prune-and-split | free | yes — re-runs when flags change |
| LLM labeling (per N) | ~$0.0003 | recomputed when N changes |

Subsequent `--max-rel-types` changes reuse the cached centroids: one `$0.0003` relabel per new N. Already-seen N is a pure cache hit (no API calls, no disk write on the rel-clusters cache).
