# Visualize: build pipeline (`visualize.py`)

Python-side reference for the visualizer build: how the graphml is loaded, what algorithms run, how output files + caches work, and the seams that future features should extend.

**Companion docs**:
- `docs/analysis/visualize-frontend.md` — the runtime / JS side (view modes, label LOD, detail panels, click routing).
- `docs/analysis/rel-clusters-algorithm.md` — how the relationship-type buckets shown in the legend are computed.

## Output layout

All artifacts land under `<data-dir>/visualize/`. Two browser-loaded files at the top, build-time-only caches under `cache/`.

```
<data-dir>/visualize/
├── graph.html              # open in a browser
├── data.js                 # node + edge payload, loaded by graph.html
└── cache/                  # build-time-only inputs; never loaded by the browser
    ├── layout.json         # cached spring-layout positions (first run)
    └── rel-clusters.json   # cached keyword→bucket clustering (first run)
```

**Invariant: the browser never loads anything from `cache/`.** That's the point of the subdirectory — makes the role split visible in a directory listing. Moving `graph.html` out of `visualize/` yields an empty network because `data.js` is loaded as a sibling via `<script src="data.js">`.

| File | Size (16k-node graph) | Browser? | Rebuilt when… |
|---|---|---|---|
| `graph.html` | ~70 KB | yes | content changes (skip-if-identical gate) |
| `data.js` | ~22 MB | yes | content changes (skip-if-identical gate) |
| `cache/layout.json` | ~2 MB | no | graph structure changes (sha256 of nodes+edges differs) |
| `cache/rel-clusters.json` | ~3 MB | no | embedding model/dim/schema/mode/cache_k differs, or `--regenerate-keyword-clusters` |

### Sharing

Copy or zip the whole `visualize/` folder — moving just `graph.html` opens to an empty network. `data.js` must stay adjacent.

## Color palette

Single source of truth: `_PALETTE` in `visualize.py` (16 hex colors). `_OTHER_COLOR = "#888888"` (gray, reserved for the `OTHER_BUCKET = "Other"` sink).

### `_assign_colors(pinned_names, discovered_names)`

```python
def _assign_colors(pinned_names, discovered_names) -> dict[str, str]:
    colors = {}
    for i, name in enumerate(pinned_names):
        colors[name] = _PALETTE[i % len(_PALETTE)]
    offset = len(pinned_names)
    for i, name in enumerate(discovered_names):
        colors[name] = _PALETTE[(offset + i) % len(_PALETTE)]
    colors["Other"] = _OTHER_COLOR
    return colors
```

Pins always get palette slots `0..N-1` in their declared order, so the same pin name gets the same color across any corpus. Discovered buckets continue in slots `N..N+M-1`. Palette wraps at 16.

Applied twice per viz run:

- **Entity types** (`_build_entity_color_map(data_dir)`): reads `<data-dir>/config/entity_types.json`, splits into `structural: true` (pinned) + `structural: false` (discovered), paints. Structural types (User / Topic / Category / Tag) always occupy the first four palette slots.
- **Relationship types**: `rel_clusters.load_or_build(...)` returns a `ClusterMap` whose `buckets` have `.origin` of `"pinned"` / `"discovered"` / `"other"`. `_assign_colors` is called with `cluster_map.pinned_names` + `cluster_map.discovered_names`.

Same palette used for both — entity and rel types never appear in the same legend section, so color collisions don't matter. The two filter blocks have separate headings anyway.

### Why colors aren't stored in config/cache

- `entity_types.json` (schema v2): holds names + `structural` flags only. Legacy `color` fields in older files are silently ignored.
- `rel-clusters.json`: no bucket color data. Palette is applied at render time.

Keeping colors out of data files means the palette can evolve without invalidating vocabulary or cluster caches.

## Edge coloring

Each edge is painted in **its rel-bucket color** (not the source node's entity color, which was the old behavior). Rationale: selecting a single rel-type filter with an orange swatch should highlight orange edges on the canvas. The old behavior drew edges in whatever color the source node happened to be — filter selections were visually inconsistent with canvas output.

```python
# visualize.py
edge_color = relationship_colors.get(rel_cat, _OTHER_COLOR)
net.add_edge(u, v, ..., relCategory=rel_cat,
             color={"color": edge_color, ...})
```

Each edge carries its `relCategory` (bucket name as string) so the JS filter logic can hide/show by rel-type without re-computing classification.

## Edge classification: `_classify_edge`

Counter-based vote across the edge's comma-separated keywords:

```python
matches = Counter()
for kw in keywords_str.split(","):
    kw = kw.strip().lower()
    idx = cluster_map.keyword_to_bucket_idx.get(kw)
    if idx is not None:
        matches[idx] += 1
bidx = min(matches.items(), key=lambda x: (-x[1], x[0]))[0]
```

Ties broken by bucket index: pins at `0..pin_count-1` (so pins win on ties), then discovered in cache order. No size-rank reorder is applied to the bucket indices — see `docs/analysis/rel-clusters-algorithm.md` for why.

Edges with no keyword matches land in `OTHER_BUCKET`.

## Legend structure

Two sub-sections per filter block, visually separated by `.filter-group-heading` CSS class (small, uppercase, muted gray, subtle top border):

```
Entity Types
  Forum primitives
    User      336
    Topic    1331
    Category   22
    Tag       139
  Discovered
    Component 6438
    Issue    1495
    ...
    Other    2013

Relationship Types
  Forum primitives
    Posted       3425
    Tagged       1714
    Categorized  1332
  Discovered
    ComponentIntegration    5802
    IssueResolution         3083
    ...
    Other                    198
```

Empty sub-sections collapse entirely (e.g. if a corpus has no discovered entity content types, only Forum primitives shows).

Within each sub-section, buckets are sorted by edge count descending, with `Other` pinned to the bottom.

`_render_filter_section(cls_prefix, pinned, discovered, counts, colors)` emits the HTML for one filter block; called twice per build (entity-types, rel-types). `_filter_row` and `_sub_heading` are the row-level helpers.

## Cache skip-if-identical write gates

Three files have skip gates so cache-hit runs don't touch disk:

### `data.js` (~22 MB)

```python
new_contents = "// Auto-generated...\nwindow.GRAPH_DATA = " + json.dumps(...) + ";\n"
existing = graph_data_path.read_text() if graph_data_path.exists() else None
if existing == new_contents:
    print("Graph data unchanged; skipping write.")
else:
    graph_data_path.write_text(new_contents)
```

Byte-compare. The 22 MB read is disk-cached for repeat runs, way cheaper than the 22 MB write.

### `graph.html` (~70 KB)

Same byte-compare pattern (`_inject_and_write_html`). Keeps mtime stable on cache-hit runs so downstream watchers don't re-trigger.

### `cache/rel-clusters.json` (~3 MB)

Compares everything *except* the `generated_at` timestamp. Only bumps the timestamp (and writes) when the payload actually differs:

```python
def _without_timestamp(d):
    return {k: v for k, v in d.items() if k != "generated_at"}

if _without_timestamp(existing) == _without_timestamp(new):
    return  # skip
```

Stable mtime on no-op runs.

## PyVis override pattern

PyVis's default behavior: `net.save_graph(path)` inlines every node and edge into the HTML as a `new vis.DataSet([...])` literal. On a 16k-node graph that's ~22 MB baked into a single HTML file — slow to load, uncacheable by the browser across runs, re-emitted on every viz invocation.

Our override:

1. Build `net.nodes` and `net.edges` normally.
2. Collect them into `graph_data_payload = {"nodes": list(net.nodes), "edges": list(net.edges)}`.
3. Write that payload to `data.js` as `window.GRAPH_DATA = {...};`.
4. **Empty `net.nodes` and `net.edges`** before calling `save_graph()` so PyVis emits empty DataSet literals.
5. Inject `<script src="data.js"></script>` into the generated HTML.
6. `graph.js`'s `DOMContentLoaded` handler populates the live DataSet objects from `window.GRAPH_DATA` before the init.

Net: `graph.html` drops to ~70 KB (PyVis template + vis-network library + our CSS/JS/panels), data.js carries the ~22 MB separately.

### Why `.js` not `.json`

Browsers block `fetch()` of local `.json` files on `file://` URLs (Chrome's CORS policy). `<script src>` tags are not subject to CORS and load synchronously by default, which is exactly the ordering we need (GRAPH_DATA defined before `graph.js` runs).

Classic JSONP-style workaround for the local-file viewing case. If we ever host the viz behind an HTTP server, `fetch()` of a `.json` would work, but the current pattern is robust to drag-and-drop opening too.

## Layout cache

`cache/layout.json` stores spring-layout positions keyed by **sha256 of graph structure** (sorted node names + sorted edge tuples).

```python
def _graph_signature(G):
    h = hashlib.sha256()
    for n in sorted(G.nodes()):
        h.update(n.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    h.update(b"\x01")
    for src, tgt in sorted((str(s), str(t)) for s, t in G.edges()):
        h.update(src.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
        h.update(tgt.encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return "sha256:" + h.hexdigest()
```

Only the **structure** counts — node names and edge tuples. Attribute changes (e.g. Pass 3 `entity_type` rewrites) don't invalidate the cache. On unchanged structure, viz reuses the cached positions instead of re-running `spring_layout` (which takes ~6 min on a 16k-node graph).

Delete `cache/layout.json` to force a fresh layout; otherwise it's self-invalidating on structural change.

## CLI flags

| Flag | Default | Effect |
|---|---|---|
| `--max-rel-types N` | `12` | Legend bucket count. Cheap to vary — cached centroids reused, one LLM relabel per new N. |
| `--cache-k N` | `50` | Base k-means cluster count (llm-cluster mode). Bumping triggers a full re-embedding. |
| `--balance-threshold F` | `4.0` | Drives prune-and-split imbalance branch. Pass `inf` to disable. |
| `--min-bucket-pct F` | `0.5` | Drops buckets below F% of total edges. Pass `0` to disable. |
| `--regenerate-keyword-clusters` | off | Wipe and rebuild rel-clusters cache from scratch. |
| `--hub-label-count N` | `100` | Top-N hubs whose labels stay visible in the default "Labels: Hubs" mode. Pass `0` to start with no hub labels. Build-time default; the JS-side **Label density** slider + `?hubLabels=N` URL param both override at runtime via `recomputeIsHub()`. |
| `--open` | off | Open the generated HTML in the default browser. |

## Graph algorithms (computed at build time)

Two NetworkX-built-in algorithms run during `visualize.py` graph-load and ship per-node attributes consumed by the JS side:

### Articulation points (`isArticulation: bool`)

`nx.articulation_points(G)` — Tarjan, O(n+m). A node is an articulation point if removing it disconnects part of the graph. Surfaced in the JS detail meta line as a red "Cut node" chip. Canonical corpus: **4,062 / 16,502** nodes are articulation points.

### Louvain communities (`community: int`)

`networkx.algorithms.community.louvain_communities(G, seed=42)` — modularity-based clustering, near-linear. Communities are sorted by size descending; community ID `0` is largest. Sizes ship as `GRAPH_META.communitySizes: list[int]`. Canonical corpus: **1,604 communities**, top-5 are 2,132 / 735 / 652 / 613 / 453 nodes. Surfaced in the JS detail meta as a "Cluster #N (k nodes)" badge.

## Build seams

`build_visualization` is a thin orchestrator — every roadmap feature should extend one of these named seams rather than fattening the main function.

| Seam | Returns | When to extend |
|---|---|---|
| `_load_graph(graphml_path)` | `nx.Graph` | If the graph source changes (e.g. read from a different format). |
| `_compute_node_metadata(G, color_map, hub_label_count, chunk_to_topic)` | `NodeMeta` (categories, entity_types, type_counts, entity_type_counts, degrees, max_degree, hub_ids, topic_ids, referenced_topic_ids) | Per-node facts derived in a single pass. New per-node attributes (e.g. node-level community-membership-summary) plug in here. |
| `_compute_articulation_points(G)` | `set[str]` | Per-node structural booleans. Independent — fine to parallelize with Louvain. |
| `_compute_louvain_communities(G)` | `(node_community, sizes_desc)` | Same shape as articulation points; new community-style algorithms (k-core, betweenness samples) belong here. |
| `_compute_max_weight(G)` | `float` | Edge-weight max for width scaling; one-line generator. |
| `_add_nodes_to_net(net, G, meta, color_map, positions, ...)` | — | The PyVis node-add loop. New per-node payload fields go here. |
| `_process_edges(net, G, meta, rel_cluster_map, rel_colors, max_weight, chunk_to_topic)` | `(rel_type_counts, cat_edges, entity_type_edges, edge_topic_ids)` | Single edge pass: classify + add + view-level aggregates + per-edge topic provenance. New per-edge payload fields, new aggregate views (e.g. time-window) fit here. |
| `_load_chunk_to_topic(graphrag_dir)` / `_build_topic_index(topics_dir, ids)` | `chunk_id → topic_id` map / `tid → {title,…}` map | Topic provenance — see the dedicated section below. Pre-pass runs once at build start. |
| `_render_filter_section(...)` / `_filter_row` / `_sub_heading` | HTML string | Sidebar legend rows. New filter sections are one call. |
| `_render_control_panel(type_filters, rel_filters, max_weight)` | HTML string | The left sidebar. New collapsible sections (time slider, pinned set) extend the f-string template directly. |
| `_write_data_payload(path, net)` | — | Skip-if-identical gate for `data.js`. |
| `_inject_and_write_html(output_path, ...)` | — | Skip-if-identical gate for `graph.html`. |

Constants — `OTHER_BUCKET`, `UNKNOWN_ENTITY_TYPE`, `DESC_SEP` (`\x1f`), `LIGHTRAG_GRAPH_FIELD_SEP` (`<SEP>`), `CAT_EDGE_SEP` (`|`), `TYPE_EDGE_SEP` (`||`), `TOOLTIP_TRUNCATE` — encode the mini-protocol shared with the JS side. Touch them on both ends if you change the contract. `SOURCE_ID_SEP` is an alias for `LIGHTRAG_GRAPH_FIELD_SEP` since LightRAG uses the same literal for both `source_id` chunk lists and merged-description phrase joins.

## `<SEP>` normalization at load time

LightRAG uses the literal string `<SEP>` (its `GRAPH_FIELD_SEP`) for two unrelated jobs in graphml: joining merged per-chunk descriptions on a single entity / edge, and joining chunk-id lists in `source_id`. The visualizer's `DESC_SEP = "\x1f"` is the cleaner internal joiner. **Both `_add_nodes_to_net` and `_process_edges` normalize `<SEP>` → `DESC_SEP` immediately after reading from graphml**, so downstream code only has to know one separator:

```python
description = (data.get("description") or "").replace(LIGHTRAG_GRAPH_FIELD_SEP, DESC_SEP)
tooltip_text = description.replace(DESC_SEP, "\n")  # vis-network honours \n
# title= uses tooltip_text; fullDescription= ships description (DESC_SEP-separated)
```

The JS panel splits `node.fullDescription` on `\x1f` to render per-phrase paragraphs (each `<SEP>`-separated phrase is a distinct chunk extraction; see `visualize-frontend.md` → "Per-phrase description split"). Without this normalization the JS would see literal `<SEP>` text in panel content + tooltips.

**Critical caveat — phrase order does NOT align with chunk order**. LightRAG's `_merge_nodes_then_upsert` (operate.py:1764-1779) deduplicates descriptions by content and sorts the survivors by `(timestamp, -length)`; source_ids go through a separate `merge_source_ids` with different ordering. Don't zip `description.split('<SEP>')` with `source_id.split('<SEP>')` — the i-th phrase is not from the i-th chunk. The doc-comment on `LIGHTRAG_GRAPH_FIELD_SEP` calls this out for future readers.

The `descPhrases` annotation in the JS panel reports multiplicity (`N phrases · K topics`) but never claims a per-phrase mapping. K is the union-of-topicIds count from `_unionAndRankTopicIds`, not a 1:1 attribution.

## Topic provenance pre-pass

Every graphml node + edge carries a `<SEP>`-joined list of chunk IDs in `source_id`. The pre-pass `_load_chunk_to_topic(graphrag_dir)` resolves those chunks to topic ids by joining two LightRAG kv_stores:

- **`<data-dir>/graphrag/kv_store_text_chunks.json`** — `chunk-<hash> → full_doc_id`.
- **`<data-dir>/graphrag/kv_store_full_docs.json`** — `doc-<hash> → file_path: 'topic-NNNN.json'`.

`full_doc_id` comes in two shapes on this corpus:
- `topic-NNNN` (Pass-1 custom_kg seed in `query.py::_topic_to_custom_kg`) — strip prefix.
- `doc-<hash>` (Pass-2 LLM extraction via `ainsert`) — resolve via `kv_store_full_docs.json`.

Both shapes resolve cleanly; the canonical corpus has **100% chunk resolution** (1,723/1,723 referenced chunks → 1,331 distinct topics).

The pre-pass runs **once at build start**, ~50 ms on the canonical corpus, before `_compute_node_metadata` and `_process_edges`. The resulting `chunk_to_topic: dict[str, str]` is plumbed through both:

- `_compute_node_metadata(..., chunk_to_topic)` populates `NodeMeta.topic_ids: dict[str, list[str]]` (per-node, deduped, first-mention-order) and `NodeMeta.referenced_topic_ids: set[str]` (union for the topic-index pass).
- `_process_edges(..., chunk_to_topic)` attaches `topicIds: list[str]` to each edge payload and returns the edge-side topic id union as the fourth tuple element.

After both passes, `_build_topic_index(topics_dir, referenced_topic_ids)` walks each referenced `<data-dir>/topics/<id>.json` once and returns a `{tid: {title, createdAt, postCount, excerpt, firstPostBy}}` map. Shipped as `GRAPH_META.topicIndex` so the JS panel renders rows without `file://` JSON fetches (Chrome blocks those — same reason `data.js` is `.js` not `.json`).

Failure modes: missing `kv_store_*.json` returns `{}` and provenance is silently absent; missing topic JSONs are skipped per-topic. The build still succeeds.

Output payload growth: ~900 KB on the canonical corpus (`data.js` 22.2 → 23.1 MB, plus the topicIndex inline in `graph.html`'s meta script).

## Time-window bounds

The graphml's per-edge / per-node `created_at` is **index time** — every value reflects the indexer run, not the underlying forum post. Useless as a time-window filter on a re-indexed corpus (on the canonical corpus all 24,587 edges land inside a single 11-hour indexing window). The visualizer instead derives source-post time from `GRAPH_META.topicIndex[tid].createdAt` (Discourse's ISO-8601, e.g. `2022-11-04T09:27:09.387Z`).

Pipeline (runs after `_build_topic_index`, before `_write_data_payload`):

1. `_parse_topic_ts(iso) -> int | None` parses ISO-8601 to Unix seconds.
2. `_ts_to_month_bin(ts) -> int` converts to month-bin index since the slider epoch (2018-01). Pre-epoch timestamps clamp to 0.
3. `_bounds_for_topics(topic_ids, topic_to_ts)` returns `(min, max)` Unix seconds over the resolvable subset, or `None` when nothing resolves.
4. The build mutates `net.edges` in place to attach `tm` / `tM` (month-bin endpoints). Edges whose topic ids don't resolve get neither key — the JS treats those as time-pass (no signal, no penalty).
5. `GRAPH_META.timeBounds = {minBin, maxBin, epoch: "2018-01"}` is shipped for the slider extent. `None` when nothing resolves; the JS skips the section entirely.

Per-node bounds are NOT shipped. The JS derives `nodeTimeOk` per slider tick by walking the edges-in-window once, and the per-entity time-range chip computes from `topicIndex[tid].createdAt` over the union of node-side + incident-edge topic ids — both cheap on this corpus, both keep the data.js delta small.

Payload growth: ~440 KB on the canonical corpus (`"tm":N,"tM":M` × 24,587 edges). `data.js` 22.8 → 23.2 MB.

## Canvas label truncation

`_truncate_label` caps the vis-network-rendered `label` at `LABEL_TRUNCATE = 30` chars + a single `…` (U+2026, 1 char wide), so the longest visible label is 31 chars. Topic / Issue / Guide / Document entities frequently carry sentence-length names ("`"JOIN" over different models - output…`") that overlap into mush in dense regions; the cap keeps the canvas readable in Labels: All mode and during viewport-aware Hubs labelling.

Critically, **only `label` is truncated** — `node.id` keeps the full text. Search predicates read both (`n.id` for the truncated tail, `n.label` for cheap-prefix matches); detail panels, copy formats, Markdown emitters, and tooltip bodies all read `node.id || node.label` so they show the full name. The JS bulk-flipped from the old `n.label || n.id` idiom (where the fallback was dead code because `label === id`) to `n.id || n.label` for display sites.

## Coverage diagnostic

Each viz run prints `Edge categorization coverage: X% (n/total fell to Other; mode=…)`. Below 75% means the cached cluster map no longer fits the corpus — usually after a re-index that added many new keyword strings. Fix by re-running with `--regenerate-keyword-clusters`.
