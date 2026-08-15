# Visualizer cold-build performance

How to make `discourse-explorer visualize` faster on cold builds (when the layout cache is invalid). Catalogues the levers, their pros + cons, and a recommended sequence — conservative first, transformational only if needed.

> Numbers below are from the canonical 1.3K-topic corpus (16K nodes, 25K edges). Smaller corpora scale roughly linearly.

Status: **proposed, not started.**

## Baseline (today, sequential, networkx-only)

Cold build measured at **378 s end-to-end** for a 16K-node corpus. Phase breakdown:

| phase | function | cost | notes |
|---|---|---|---|
| graphml load | `_load_graph` | ~5-10 s | I/O + networkx parse |
| topic-provenance pre-pass | `_load_chunk_to_topic` | ~50 ms | JSON reads |
| rel-cluster build | `_rel_clusters.load_or_build` | ~2-30 s (uncached) | k-means + Ward; cached as `rel-clusters.json` after first run |
| node metadata | `_compute_node_metadata` | ~1-2 s | iterate nodes + topic-id resolve |
| **articulation points** | `_compute_articulation_points` | ~1 s | Tarjan O(V+E), single-pass |
| **Louvain communities** | `_compute_louvain_communities` | **~30-60 s** | pure-Python `networkx.algorithms.community.louvain_communities` |
| **spring layout** | `_compute_layout` | **~360 s** | `networkx.spring_layout(iterations=50)`, pure-Python force-directed |
| pyvis serialization | `_add_nodes_to_net` + `_process_edges` | ~10-20 s | builds in-memory `Network` object |
| data.js write | `_write_data_payload` | ~1-2 s | JSON dump (~23 MB) |
| pyvis HTML | `net.save_graph` | ~2-5 s | template render |

**Layout dominates: 95% of total time.** Louvain is the only other phase worth attacking; everything else is sub-10-second.

## Cache lifecycle

`layout.json` is keyed by a structural signature of the graph (node set + edge set hash). Subsequent visualize runs against the same graph hit the cache and finish in **~10-20 s** total. Cache invalidates on any graph mutation:
- `--index` (full re-build)
- `--canonicalize-only` (Pass 4 — collapses ~700 nodes on first run)
- Manual graphml edits

So the cold-build cost only hits at indexing-time boundaries. The question is how much that hurts.

## Options

| # | option | wall-time savings | dependency cost | risk |
|---|---|---|---|---|
| 1 | `python-louvain` for community detection | ~25-50 s (Louvain only) | new pure-Python dep | low (cosmetic: community colors reshuffle once) |
| 2 | Concurrent articulation + Louvain + layout via `multiprocessing` | ~5-30 s (overlaps Louvain with layout startup) | none (stdlib) | low (pickling overhead, no shared state) |
| 3 | `python-igraph` for spring layout | ~330-340 s (the big one) | new C extension | low-medium (visual diff in node positions; macOS Apple Silicon wheel) |
| 4 | `python-igraph` for Louvain | ~25-50 s (consolidates with #3) | bundled with #3 | low (same as #1: color reshuffle) |
| 5 | `iterations=30` instead of `50` in `nx.spring_layout` | ~140 s (40% faster) | none | low (slightly less polished layout) |

**Combinations:**

- **Conservative bundle (1 + 2 + 5)**: ~165 s total (~6.3 min → ~3 min). Stdlib + one pure-Python dep.
- **Aggressive bundle (3 + 4 + 2)**: ~30-40 s total (~6.3 min → ~30-40 s). Adds python-igraph; ~10× speedup.

## Per-option detail

### Option 1: `python-louvain` for community detection

Replace `networkx.algorithms.community.louvain_communities(G, seed=42)` with `community.best_partition(G, random_state=42)` from the `python-louvain` package (PyPI: `community-louvain`).

**Pros:**
- Pure Python, MIT, well-maintained (predates networkx's port; networkx's algorithm descends from this library).
- ~5× faster than networkx Louvain in practice for graphs in the 10-50K node range.
- No C extension, no platform risk, no compilation.
- Same big-O, same correctness guarantees.

**Cons:**
- New dependency in `pyproject.toml`.
- **Visual community-color shift one time** — modularity has many valid optima; the two libraries produce different valid assignments. Stabilizes after first run; visualizer's community ordering by size means the *shape* of the legend doesn't change, just which color goes where.
- Tests / docs that reference specific community indices invalidate. Project has no such fixtures today (per the "Beyond the layering test, there's no broader test suite" note in `CLAUDE.md`).

### Option 2: Concurrent articulation + Louvain + layout via `multiprocessing`

The three computations are pure functions over the same `G` and produce independent results. Today they run sequentially in `build_visualization`. Wrap them in a `concurrent.futures.ProcessPoolExecutor` so they run in parallel.

**Pros:**
- Stdlib only — no new dependency.
- Truly concurrent (process-level, not GIL-bound).
- Layout dominates so the upper bound on parallelism is `max(layout, Louvain, articulation) + pickle_overhead` — at sequential 6 min layout + 30 s Louvain + 1 s articulation, the concurrent equivalent is ~6 min + ~5-10 s pickling overhead. Saves ~25-30 s.

**Cons:**
- Pickle overhead: `G` is pickled per subprocess (~50 MB serialized for 16K nodes). 3 subprocesses = ~5-10 s setup cost. Net win is real but small.
- Adds complexity in error reporting (subprocess failures need explicit propagation).
- Memory: 3× the RAM during the concurrent window (each subprocess holds its own copy of G). On a 16K-node graph that's ~150 MB extra peak; not a concern on a laptop, would matter on a memory-constrained CI runner.
- **The gain is small unless layout itself is sped up.** With layout at ~360 s, saving 30 s on Louvain is barely visible. With layout at ~30 s (Option 3), saving 30 s on Louvain doubles total wall time wins.

**Combo note:** Option 2 only really pays off when paired with Option 3. Without #3, it's saving 5-7% on a 6-min build.

### Option 3: `python-igraph` for spring layout — the big lever

Replace `nx.spring_layout(G, iterations=50, seed=42)` with `igraph.Graph.layout_fruchterman_reingold(...)` (or `layout_drl` for very large graphs). Convert `nx.Graph` → `igraph.Graph` once at the top of the layout function.

**Pros:**
- **~10-30× faster** — C-backed implementation. Estimated drop: 360 s → 12-36 s for 16K nodes.
- Multiple layout algorithms available (`layout_drl` is built for very large graphs; `layout_kamada_kawai` for smaller dense graphs).
- Same FR algorithm family as networkx — visually compatible enough that it's not a "completely different look."

**Cons:**
- **New C-extension dependency.** `python-igraph` ships pre-built wheels for major platforms including macOS Apple Silicon (was bumpy in 2023, mostly fixed by 2026), but worth a `uv sync` smoke test before relying on it.
- ~15 MB venv footprint.
- **Visual diff in node positions.** Even with same seed, igraph's FR implementation differs from networkx's in initial conditions and convergence. Tendency: more compressed/denser layouts. May need parameter tuning (`niter`, `start_temp`, `grid_size`) to match the current aesthetic.
- **Cache invalidation one-time.** The current `layout.json` (from networkx) is unusable post-switch. First post-switch build pays the (much faster) layout cost again, then cache works fine.
- **`nx.Graph` → `igraph.Graph` conversion.** ~1-2 s per build. Modest but not zero.
- Determinism: seedable in both libraries, but seeded outputs aren't comparable across libraries.

### Option 4: `python-igraph` for Louvain (consolidate with #3)

`igraph.Graph.community_multilevel()` is the C-backed Louvain. If python-igraph is already added for #3, this is essentially free additional speedup.

**Pros:**
- ~30× faster than networkx Louvain (vs python-louvain's ~5×).
- No additional dependency if #3 is taken.
- Same algorithm family as networkx + python-louvain.

**Cons:**
- Same visual shift as Option 1 (community colors reshuffle).
- `resolution` parameter scales differently from networkx — may need retuning if that parameter is ever surfaced as a knob (currently it's not).
- **Replaces the conservative Option 1 if both are considered** — pick one, not both.

### Option 5: lower `iterations` from 50 to 30 in `nx.spring_layout`

A free win: 40% fewer force iterations means 40% less compute time, with a slight visual-quality drop (less converged force-directed positions).

**Pros:**
- **Zero code change beyond a single integer**, no new dependency.
- ~360 s → ~215 s (~145 s saved).
- Often the difference is barely visible to a human.

**Cons:**
- Layout looks slightly less "settled" — some nodes may overlap more than they would at 50 iterations.
- Doesn't address the underlying scaling problem.
- Caching means this only matters on cold builds anyway.

## Recommended sequence

1. **Try Option 5 first** (`iterations=30`). Free, instant, ~40% win on cold builds. If the visual is acceptable, ship it.
2. **If that's not enough**, ship the **conservative bundle** (Options 1 + 2 + 5):
   - Add `python-louvain` (pure Python).
   - Move articulation + Louvain + layout into a `ProcessPoolExecutor` block.
   - Lower iterations to 30.
   - Net: ~165 s on cold builds.
3. **If cold builds still hurt enough that #2 isn't sufficient**, ship the **aggressive bundle** (Options 3 + 4 + 2):
   - Add `python-igraph` (C extension; smoke-test on Apple Silicon).
   - Replace spring_layout with `layout_fruchterman_reingold`.
   - Replace Louvain with `community_multilevel()`.
   - Keep ProcessPoolExecutor wrapper.
   - Net: ~30-40 s on cold builds (10× faster than baseline).

**Important guard**: at every step, verify the visualizer output is acceptable. Spot-check a few high-degree nodes and a few low-degree clusters; confirm legend / community colors are still meaningful. Document the chosen step + observed before/after numbers in the doc rewrite that follows implementation.

## What this isn't about

- **Indexing pipeline** (`query.py` Pass 1-4) — already optimized; Pass 4 is the recent addition. See `multi-pass-indexing.md`.
- **Per-query latency** — the visualizer is build-time only; users open the resulting HTML, no Python runtime required at view time.
- **`data.js` size** — this is the on-disk artifact, separate from build time. Current size cap is 24.3 MB on the canonical corpus (per memory).

## Open questions

- **How often do cold builds happen?** If the answer is "twice a year after a re-index," the conservative bundle is enough. If "weekly during active vocabulary tuning," the aggressive bundle pays for itself fast.
- **Is the visual diff from igraph layout acceptable?** Only one way to find out — try it. If not, settle on conservative bundle.
- **Multi-corpus scaling?** All numbers above are from one corpus. If the project ever indexes a forum 5× larger, the slowest options scale superlinearly while igraph layouts stay near-linear. The decision shifts toward aggressive at that point regardless.
