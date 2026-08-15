# Visualizer Feature Roadmap

Outstanding feature proposals for `discourse_explorer/visualize.py` + `static/graph.js`, ordered by leverage. All entries below are doable against the **existing** indexed graph — no LightRAG re-index required. (Two earlier proposals — sentinel-extraction-only ideas — were dropped because they would have needed `--index --clear`.)

## Already shipped

- ✅ **Full-text description search** — search box now matches `fullDescription` + `entityType` in addition to `label`. One-line change in `applyFilters`. Surfaces entities whose names don't include the query but whose descriptions do.
- ✅ **"At a glance" panel** — control-panel section that summarizes the current visible/filtered set: top 5 entities by degree, top super-categories with counts, top relationship buckets with counts. All clickable.
- ✅ **Viewport-aware label LOD** — Hubs mode shows all labels when ≤60 in viewport; All mode capped at top-200 by degree; cursor-hover label reveal universal. Label-mode picker is a `<select>` so all options are visible at a glance.
- ✅ **Detail panel: clickable edges + selection highlight + unified nav history** — clicking an edge opens the panel with Source→Target, rel-category chip, full description split per phrase, "Filter to <rel>" button. Back/forward arrows traverse a mixed history of nodes, categories, and edges.
- ✅ **Topic provenance (originally #1 below)** — every node + edge panel now shows a "Source topics" section listing the forum topics the entity/relation was extracted from. Title + date + post count + 2-line excerpt + click-to-open the topic JSON. Build-time pre-pass (`_load_chunk_to_topic`) joins `kv_store_text_chunks.json` → `kv_store_full_docs.json` (two-hop, since Pass-2 LLM extraction creates `doc-<hash>` ids that resolve via `file_path` to a topic), then `_compute_node_metadata` and `_process_edges` attach `topicIds` per node/edge. Topic title + excerpt shipped in `GRAPH_META.topicIndex` so JS doesn't need `file://` JSON fetches. 100% chunk resolution on the canonical corpus (16k nodes / 1,729 chunks / 1,331 topics).
- ✅ **Topic provenance — node panel rework + Markdown subsections** (commit `c39751a` and the follow-up). Resolves the swulf-style structural quirk where a User entity's own `source_id` only points at the first-definition chunk while per-topic provenance lives on incident edges. Specifically:
  - `_unionAndRankTopicIds(node.topicIds, connEdges)` unions node + incident-edge topic ids and ranks by incidence count → lands the "Most-relevant-first sort" deferred enhancement at render time (no build-time recomputation).
  - Topic-typed connection rows get a per-row expand `▸` button revealing date · posts · author · excerpt + Open-topic-preview modal inline. Per-group `Expand all` / `Collapse all` toggle on Topic groups with ≥2 expandable rows.
  - Source topics section becomes a residual `<details>` listing only topics not already named as Topic neighbors above. Header diff-aware (`Source topics — not shown above (N)` vs plain `Source topics (N)`); section hidden entirely when residual is empty.
  - Markdown copy includes Source topics as a structured block (residual-aware, with `[Title](../topics/N.json) (#N)` links), and Topic Connection rows render with a 2-space-italic continuation for date/posts/author. Lands the deferred `"Source topics" in the Markdown copy format` enhancement.
- ✅ **Description per-phrase split + multiplicity annotation**. LightRAG concatenates per-chunk descriptions of the same entity / edge with `<SEP>`. The build now normalizes `<SEP>` → `DESC_SEP` (`\x1f`) at load time (visualize.py: `LIGHTRAG_GRAPH_FIELD_SEP` constant); the JS panel splits into one `<p class="desc-phrase">` per phrase with a thin left-accent border and a dashed `border-top` separator on `.desc-phrase + .desc-phrase` for clear phrase boundaries. Multi-paragraph LLM-summarized phrases render as nested `<p>` children inside one `.desc-phrase` wrapper. When N>1, a `.desc-meta` annotation reports `N phrases · K topics` with a `title=` tooltip explaining LightRAG's ordering caveat (descriptions sorted by `(timestamp, -length)` independently of `source_id` — phrase order does NOT map to topic order). Markdown copy emits `### Phrase N` H3 subsections for multi-phrase descriptions (subsections survive every Markdown renderer; loose-list 4-space continuation does not).
- ✅ **Search-context strict gate + toggle**. Search-context neighbors now respect type/degree/community filters (no more leaking unchecked-type nodes through the search-neighbor expansion). New `passableNonSearch` set in `applyFilters` gates the expansion; the Nodes stat exposes the dim contribution as `(+N dim)`. New `Show 1-hop context (dim)` checkbox under the search box (state: `searchShowContext`, default checked) — uncheck for matches-only view.
- ✅ **Markdown polish across all `_md*` emitters**. Top-level meta lines (Type / Category / Degree / Cluster / Cut node / Edges / Total weight / Avg weight / Members / Rank etc.) converted from `·`-joined sentence form to bulleted lists for machine-parseability. Edge-row description sourced from `e.fullDescription` first phrase instead of `e.title.split('|')[0]` (the latter never finds `|` on regular edges and was duplicating the rel-cat into the row text).
- ✅ **Detail-panel: Stats / Query buttons (originally #4 below)**. Two new action-row buttons in `showNodeDetail` and `showEdgeDetail`. Click → copy a copy-paste-ready CLI command to clipboard; toast confirms; `title=` previews the full command on hover. Per-entity-type defaults: Topic → SQL on `posts.topic_id`, User → SQL on `posts.username`, Tag → join on `topic_tags`, Category → SQL on `topics.category_name`, everything else → existing `stats search "<label>"` (plain-text ILIKE). Edges → SQL ANDing both endpoint labels in the same post body (free-text co-occurrence). Query default: `Tell me about <label>` for nodes, `How does <a> connect to <b>?` for edges. `GRAPH_META.dataDir` ships the corpus path (cwd-relative when the data dir is under cwd, absolute otherwise — see `visualize._format_data_dir_for_meta`) so the command pastes-and-runs without the user retyping `--path`. `_attrEsc` + `data-statsq-cmd` attribute + delegated click handler avoids onclick-attribute escaping fragility against the SQL's literal `"` characters.
- ✅ **Time-window slider + per-entity time-range chip (originally #2 below)**. Sidebar collapsible section with a dual-thumb range slider gated on source-post time (topic JSON `created_at`, not the degenerate index-time `created_at` on the graphml). Build-side: `_parse_topic_ts` / `_ts_to_month_bin` / `_bounds_for_topics` derive per-edge `tm`/`tM` (month-bin endpoints since 2018-01) from `topicIndex[tid].createdAt`; `GRAPH_META.timeBounds` ships the slider extent. JS-side: any-overlap predicate (`_edgePassTime`); per-tick `nodeTimeOk` set drives node visibility + composes with type/degree/community via `passableNonSearch` (search-context + pin-neighbor expansions honour the window symmetrically). Per-entity chip in the node detail panel meta line: `Active YYYY-MM – YYYY-MM` over the union of node-side + incident-edge topic ids, with `· peaked YYYY-Qn (N edges)` suffix when ≥4 incident edges span ≥2 quarters and one dominates ≥25%. Reset & Refit clears the slider; drop pill shows `−N` edges hidden and resets on click. Payload: `tm`/`tM` per edge ≈ +440 KB on the canonical corpus (`data.js` 22.8 → 23.2 MB); per-node bounds NOT shipped (cheap to derive JS-side per slider tick).

---

## 1. ~~Topic provenance~~ — shipped (commit `baaf6d2`)

Status: **basic feature shipped.** Steps 1-4 from the original proposal all worked against the existing indexed graph; no re-index was needed.

### What shipped (entry points to read first)

Build-time pipeline (`discourse_explorer/visualize.py`):

- `SOURCE_ID_SEP = "<SEP>"` — module constant for the LightRAG separator that joins chunk-id lists in graphml `source_id` fields.
- `_resolve_topic_id(full_doc_id, docs)` — handles the two `full_doc_id` shapes (`topic-NNNN` direct, `doc-<hash>` via the second kv_store).
- `_load_chunk_to_topic(graphrag_dir)` — pre-pass that builds the `chunk_id → topic_id` map. Reads `kv_store_text_chunks.json` + `kv_store_full_docs.json`. Returns `{}` on missing files (graceful degradation).
- `_topics_for_source_id(source_id, chunk_to_topic)` — splits + dedupes + preserves first-mention order. Used in both node and edge passes.
- `_build_topic_index(topics_dir, topic_ids)` — reads each referenced topic JSON, returns `{tid: {title, createdAt, postCount, excerpt, firstPostBy}}`. Shipped as `GRAPH_META.topicIndex`.
- `NodeMeta` gains `topic_ids: dict[str, list[str]]` and `referenced_topic_ids: set[str]`. `_compute_node_metadata` and `_process_edges` both take `chunk_to_topic` as a parameter; `_process_edges` returns `edge_topic_ids` as the 4th tuple element.

JS-side rendering (`static/graph.js`, `static/graph.css`):

- `_renderSourceTopics(topicIds)` — shared helper called from `showNodeDetail` (after Connections) and `showEdgeDetail` (after the description phrase list). Caps at `_SOURCE_TOPICS_CAP = 10` rows with a "+N more" chip below.
- Each row renders title (link to `../topics/<id>.json` in a new tab) + meta line (date · N posts · by username) + 2-line excerpt clamped via `-webkit-line-clamp: 2`.
- CSS: `.topic-row`, `.topic-row-link`, `.topic-row-meta`, `.topic-row-excerpt`, `.topic-row-more`.

### Key findings (read these before resuming)

- **The roadmap's chunk → topic mapping was a TWO-hop, not one-hop.** `kv_store_text_chunks.json` gives `chunk → full_doc_id`, but `full_doc_id` comes in two shapes: `topic-NNNN` (Pass-1 custom_kg seeds in `query.py::_topic_to_custom_kg`) and `doc-<hash>` (Pass-2 LLM extraction via `ainsert`). The latter resolves via `kv_store_full_docs.json`'s `file_path: 'topic-NNNN.json'`. **100% resolution on the canonical corpus** (1,723 / 1,723 referenced chunks → 1,331 distinct topics → 1,331 / 1,331 topic JSONs on disk).
- **Pass 3 (`aedit_entity` enrichment in `query.py`) does not touch `source_id`** — provenance is preserved across pass 3. No regression.
- **Hub entities reference 100+ topics**, many tangential. The current panel cap at 10 + first-mention order is acceptable but could be improved (see "Most-relevant-first sort" below).
- **`fetch()` of `.json` files is blocked under `file://`** — same constraint that pushed `data.js` over `data.json`. The current "open in new tab" approach sidesteps it (browsers happily *navigate* to a `.json` URL even when they refuse to `fetch()` it). Inline preview needs a JSONP-style shim.
- **Build-time cost on the canonical corpus**: ~50 ms for the chunk-to-topic pre-pass + ~few seconds for `_build_topic_index` (one read per referenced topic JSON). `data.js` grew from 22.2 to 23.1 MB (~900 KB for the per-node/-edge `topicIds` lists + the inline `topicIndex`).

### Deferred enhancements (resume points)

- **Post-level granularity (best-effort)** — *non-destructive*. Each chunk in `kv_store_text_chunks.json` carries its `content` field; substring-matching that against each post's `plain_text` (in the topic JSON) yields candidate post numbers per chunk. Compose with the existing `chunk_to_topic` map and emit `postRefs: list[{topic_id, post_numbers}]` per node/edge. Caveat: LightRAG's chunker splits by token count, not post boundaries, so a chunk spanning posts N + N+1 returns both as candidates — that's the strongest claim possible without re-extraction. **Resume**: extend `_load_chunk_to_topic` to also return `chunk_to_posts: dict[chunk_id, list[int]]`; thread through `NodeMeta` and `_process_edges`; render `→ posts #1, #3` next to the title in `_renderSourceTopics`. ~1-2 hr build extension.
- **Post-level granularity (exact)** — *destructive*. Reconfigure LightRAG's chunker to chunk-per-post and re-run `--index --clear`. Only worth it if best-effort matching turns out to be too noisy. Skip unless empirically motivated.
- ✅ **Most-relevant-first sort** — shipped at render time via `_unionAndRankTopicIds(node.topicIds, connEdges)`. Counts incidence across the node's own + every incident edge's `topicIds`, sorts by count desc with insertion-order tie-break. Lands the "rank by edge-density" goal without a build-time recomputation step. (Was: planned as a build-time post-process in `_compute_node_metadata` / `_process_edges`.)
- ✅ **"Source topics" in the Markdown copy format** — shipped (`_appendMdSourceTopics` in `static/graph.js`). Topic Connection rows in `_mdNode` render as `[Title](../topics/N.json) (#N)` with a 2-space-italic continuation for date/posts/author; the residual Source topics section is dropped entirely when residual = 0 (diff-aware header otherwise).
- **Inline topic detail without leaving the panel** — *non-destructive*. The shipped Topic-row expand UI surfaces the first-post excerpt + Open-topic-preview modal action inline; the modal's "raw JSON" affordance still opens `../topics/<id>.json`. Remaining: embed top-N **posts** (not just first-post excerpt) in `topicIndex`, render multi-post discussion inline on click. Two paths: (a) inline post payload in `topicIndex` (~5-10 MB grow on canonical corpus); (b) lazy-load via per-topic `<data-dir>/visualize/topics/<id>.js` shims. **Resume**: pick (a) or (b) based on payload tolerance. ~2-3 hr either way.
- **Filter by topic (search prefix)** — *non-destructive*. Search syntax `topic:1232` scopes the canvas to nodes/edges whose `topicIds` include that id. **Resume**: extend `applyFilters` predicate; piggyback on the existing search box with a `topic:` prefix. ~1 hr. **See also** the modal-triggered + topic-lock variant under "Payload-neutral follow-ups" below — that one is the more user-discoverable surface; the prefix is the keyboard-power-user variant. Picking just one is fine.

### Payload-neutral follow-ups (post-shipping discoveries)

All three read from the existing per-node / per-edge `topicIds` already shipped in `data.js`. **No build-time changes, no payload growth** — pure JS/UI work on top of what #1 emitted.

- **Filter by topic chip — modal-triggered + lock pattern** — *non-destructive*. Single click in the topic-preview modal scopes the canvas to nodes/edges whose `topicIds` include that `tid`. Mirrors the existing `filterToCommunity` cluster-lock pattern: a `topicFilter` JS state ANDed into `applyFilters`; a "Topic #NNNN — <title>" chip appended to the breadcrumb after `renderBreadcrumb`'s normal segments, with × to unlock; re-clicking the same topic chip toggles off, clicking a different one swaps. **Resume**: introduce `topicFilter` alongside `communityFilter`; extend `applyFilters` predicate; add a chip class + click handler symmetric to `cluster-lock-chip`; wire the modal's primary action. Pairs naturally with the search-prefix variant above (the prefix sets the same `topicFilter` state). ~30 min.
- **Topic-anchored neighborhood walk** — *non-destructive*. Given a topic, render a focused subgraph of every entity whose `topicIds` include `tid` plus every edge whose `topicIds` include `tid` — the *full* topic-extracted subgraph, not just the union of node-side membership. Trigger from the modal as a second action ("Walk this topic's subgraph"). **Resume**: a one-shot variant of `applyFilters` that flips `hidden=false` only on this set, then `network.fit({nodes: [members]})`. Composes with the topic-lock chip above (same set of nodes/edges, plus the camera fit). ~1 hr.
- **Cross-topic shared-entity report** — *non-destructive*. Two-topic picker; set-intersection over node `topicIds` (and optionally edge `topicIds` for shared relations). Surface as a new detail-panel `kind` ("topic-pair") so it slots into `navHistory` alongside cluster / category / cat-edge / node / edge. Useful for "what does this RFC discussion have in common with that bug thread?". **Resume**: pick UI surface (modal with two topic-search inputs, or a "compare to…" button on an active topic-lock chip); compute intersection; render shared entities as a clickable list with per-entity edge counts in each topic. ~1 hr.

> **Why these aren't in the main deferred list above**: the main list captures the original proposal's resume points. These three are *post-ship discoveries* — capabilities that became cheap once `topicIds` was on every node/edge. Worth tracking separately so the distinction (planned vs. unlocked) doesn't get lost.

---

## 2. ~~Time-window slider~~ — shipped

Status: **shipped.** See the bullet under "Already shipped" above for the implementation summary; the original proposal is preserved below for context.

### Key findings

- **Index-time `created_at` on graphml edges/nodes is degenerate as a filter.** On the canonical corpus every value clusters around the indexer run (24,587 edges inside a single 11-hour window). The roadmap caveat predicted this; source-post time via `topicIndex[tid].createdAt` is the only viable signal, and it was already in `GRAPH_META` from the topic-provenance ship.
- **Composition matters more than the slider itself.** Adding the time gate to `passableNonSearch` (alongside type / degree / community) was the difference between a filter that surprised users and one that respected the "derived UI must respect every active filter" principle. Same fix applied to the leave-one-out `M`-counts so the gain pills don't lie when the time slider is narrowed.
- **Any-overlap is the user's mental model.** Edges are extracted from 1-3 source topics; "edge passes if any source topic is in window" matches "show me what was discussed in this period" without surprising hides.

### Original proposal

**What** — every edge in the graphml already carries a `created_at` long (Unix timestamp, set when the edge was first ingested). Two complementary surfaces:

- **Bottom-of-canvas slider** with a date range; only show edges whose `created_at` falls inside the window. Supports range-mode (two thumbs) and play-mode ("animate the graph from 2018 to now").
- **Detail-panel time-range chip** — for the currently inspected node, summarize the time spread of its edges: `Active 2022-06 to 2024-09 · peaked 2023-Q3 (8 edges)`. Tells you whether you're looking at a current concern or a historical artifact, without leaving the panel.

**Why** — this corpus spans 2018-06 to 2026-04. The current view is timeless: everything is shown at once, even though many edges represent stale references to deprecated components. Time-windowing turns a static graph into a temporal one — "what was hot in 2024.06?", "what's been added since the last release?". The per-entity chip answers the same question for one specific node.

**Effort** — small-to-medium. Edge `created_at` is already present and accessible. Plumb it through:
1. visualize.py: extract `data["created_at"]` per edge in the build loop, attach as `createdAt` on `net.add_edge`.
2. JS-side: slider widget, debounced filter that sets `hidden=true` on edges outside the window, updates the visible-edge count.
3. Subtle: nodes whose only edges are filtered out should also dim. Reuse the existing `entityVisibleIds` pattern from `applyFilters`.
4. Per-entity chip: aggregate `min`, `max`, modal-quarter over the node's incident edges; render as a small chip in the detail-meta line. Cheap; reuses the same per-edge `createdAt` field.

**Caveat** — `created_at` is the index time, not the source-post time. They typically agree for first-pass extraction but diverge if you re-index. For source-post timestamps, plumb through topic JSON `created_at` via the same path as #1 (topic provenance) — both features share infrastructure. ~1 day for index-time, ~1.5 days for source-time.

This filter then needs to be honored by other filters if it makes sense for them (thus, reflect and check, ask me when in doubt)

---

## 3. LLM "explain this subgraph"

**What** — select a region (lasso-select on canvas, or "use current filter"), feed the visible nodes + edges through the existing `query.py` LLM path with a prompt like "summarize this subgraph in 5 bullets — what theme do these entities share?". Show the response in a panel.

**Why** — closes the loop between visual exploration and the GraphRAG query backend. The current `query.py` answers free-form questions; the visualizer shows the graph; today there's no bridge. After this feature, you can drill in visually and then ask "what is this cluster about?" without leaving the page.

**Effort** — medium. The Python side is mostly already there:
1. Add a tiny HTTP endpoint OR a CLI command that takes a list of node IDs and runs them through `_get_rag().aquery(...)` with a custom prompt that scopes context to those entities.
2. JS-side: button in the toolbar / detail panel that POSTs the current visible-set IDs and renders the streamed response in a dedicated dialog.

**Caveats** — needs a local Python server (small Flask or stdlib `http.server`-based) since the visualization is currently static-file-only. Or: subprocess-launched with the IDs piped in. Per-use cost: one OpenAI/Ollama call. ~1.5–2 days; keep it behind a config flag.

---

## 4. ~~Detail-panel: open-in-stats / open-in-query~~ — shipped

Status: **shipped.** See the bullet under "Already shipped" above for the implementation summary; the original proposal is preserved below for context.

### Deferred enhancements

- **Split-button with ▾ menu of alternate templates** — first-pass shipped single buttons with one smart default per entity type. If users hit cases where the default isn't right (e.g. a User entity where they'd rather see "topics they started" instead of the all-posts SQL), the `renderCopySplit` pattern already in place for Copy is the natural extension. ~30 min once a real alternate-template list is collected.
- **Modal preview** — the original proposal mentioned "shows it in a modal with one-click copy"; v1 ships clipboard-copy-with-toast + `title=` hover preview, which is lower-friction. Modal becomes interesting only if we also add the local-server "Run + show output" path (joint with #3 LLM explain).

### Original proposal

**What** — two new buttons in the node and edge detail panels:

- **Open in stats** — pre-fills a `discourse-explorer stats` invocation scoped to this entity. For an entity named `RoleManager`: copies `discourse-explorer stats --path <DATA_DIR> sql "SELECT * FROM posts WHERE plain_text ILIKE '%RoleManager%' LIMIT 20"` to clipboard, or shows it in a modal with one-click copy. Variants: per-entity-type sensible defaults (User → activity timeline; Topic → posts in that topic; etc.).
- **Open in query** — pre-fills a question for `discourse-explorer query` or `/query-advisor`: "Tell me about \<entity\>" or "How does \<entity\> connect to \<entity-2\>". Same modal pattern.

**Why** — bridges the visualizer to the existing CLI tooling. Today the graph asks "what is this connected to?", `stats` answers "in which topics? when?", `query` answers "in plain English what does this mean?" — but the user has to manually retype the entity name across tool boundaries. One-click hand-off keeps the entity in scope.

**Effort** — small. No new infrastructure required:
1. JS-side: buttons in the action-row of `showNodeDetail` / `showEdgeDetail` that build a command string from `nodeData.get(nodeId).label` and the runtime data-dir.
2. Modal helper: tiny copy-to-clipboard dialog (one input + Copy button). Reuses the existing panel CSS.
3. Optional: also offer "Run + show output" via the same local-server route that #3 needs (so this benefits from #3's plumbing).

**Effort estimate** — 0.5 days for clipboard-copy variants; ~1 day if the local-server route is wired in.

---

## 5. Multi-set path explorer

**What** — generalize today's pathfinding (single from-node → single to-node) to "any path between **filter set A** and **filter set B**, length ≤ N". E.g., "all paths between nodes in the Modeling category and nodes tagged 2024.06, length ≤ 3". Highlight every such path simultaneously with the existing path-highlight UI.

**Why** — unlocks cross-cutting questions: how does category X interact with category Y? Which entities sit between two clusters? The answers are graph-theoretic but currently inaccessible — the user can only check pairwise.

**Effort** — medium. Algorithm: bidirectional BFS from each set, intersect at common nodes, emit paths up to length N. Cap the result count (e.g., top 50 by total edge weight) to keep the canvas readable. UI: two filter-set selectors in a small modal; rerun on changes.

**Effort estimate** — 2 days (1 for the algorithm + cap heuristic, 1 for UI integration with the existing path-highlight code).

---

## 6. Detail-panel: edge-list sort + filter

**What** — in the node detail panel's connections list, add a small toolbar at the top of the connections section:

- **Sort by**: degree (default — most-connected neighbour first), edge weight, alphabetical, rel-type.
- **Filter chips**: one per rel-type with counts; click to scope the list to a single bucket.
- **Search box**: free-text filter over the visible-list labels.

**Why** — Component nodes in this corpus have hundreds of connections. The current grouped-by-super-category list scrolls forever and surfaces no answer to "what's the *most important* neighbour?" or "which neighbours configure this?". Sort + filter inside the panel turns a long scroll into one or two interactions.

**Effort** — small. The data is already in the panel-build loop (`groups[cat].push({...})`). Adding a thin toolbar above the list + a small JS state object for sort/filter selection is ~half a day. No back-end work.

---

## 7. Minimap + smart relayout

**What** — bottom-right inset showing the full graph as a low-fidelity overview, with a viewport rectangle that you can drag to pan. Pair with: when filtering reduces visible nodes by ≥80%, run a quick force-directed relayout so the visible subset occupies the canvas instead of being scattered at sparse cached positions.

**Why** — pure ergonomics. After heavy filtering, you often see "12 nodes scattered at the corners of a 16k-node layout". Relayout brings them together. The minimap helps when you've zoomed in and lost orientation.

**Effort** — small-to-medium. vis.js doesn't ship a minimap natively; either a separate vis.js network at low detail, OR a custom canvas2d render using cached positions. Smart relayout: trigger `network.setOptions({physics: {enabled: true, ...}})` for ~2 seconds when the visible-set delta crosses the threshold, then disable physics again. ~1.5 days for both.

---

## 8. Pinned set

**What** — let the user explicitly mark a multi-node set as "pinned" (keyboard shortcut, or right-click menu). Pinned nodes stay visible at full opacity through every filter change. A second pin slot would enable side-by-side comparison ("how does set A's neighbourhood overlap with set B's?").

**Why** — current `applyFilters` already pins the *single selected* node and its neighbors (`pinnedId` + `pinnedNeighbors`). This generalizes to a persistent multi-pin that survives filter toggles. Useful for tracking entities of interest while exploring around them.

**Effort** — small. Reuse the existing pinned-node logic: extend `pinnedNeighbors` to be a union over all pinned IDs. UI: a small "pinned" tray at the bottom of the control panel with one row per pinned entity, click to unpin, drag to reorder. ~1 day.

---

## 9. Category view improvements

The Category view today shows 7-12 super-category nodes connected by aggregate edges, all equal-sized, with hover doing nothing and the only action being "drill to Node view." Lots of headroom to make it a proper top-down navigation surface. Tier 1 (super-node sizing by member count, edge-width by count, hover tooltips, beefed-up category detail panel) shipped as part of the Category view rework; the items below are the deferred Tiers 2 and 3.

### 9a. Category card grid (top-down navigation sidebar)

**What** — a dense card grid sidebar visible only in Category view, with one card per super-category. Each card shows: name, member count, top-3 hubs by within-category degree, top-3 entity types, and a "Drill" link. Clicking a card drills to Node view filtered to that category; clicking a hub name navigates straight to that node.

**Why** — turns Category view into a navigation index. Today the user sees a dozen unlabelled super-nodes and has to click each in turn to learn what's inside. Cards expose the "what's in here" answer up front, so users can pick the right drill target in seconds. Particularly valuable on first-time UX.

**Effort** — small-to-medium. The data is all derivable from `originalNodes` filtered by `superCategory`: pre-compute top hubs + entity-type histogram per category once at init (cheap on a 16k-node corpus). Render as a CSS grid in a sibling panel that's hidden in Node view. ~120 lines.

**Surface** — only in Category view; replaces or sits alongside the existing right-side breadcrumb area. Reuses Tier-1's `category-mode` body class for visibility gating.

### 9b. Aggregate-edge tooltips + click → top contributors

**What** — for the aggregate edges between super-category nodes (e.g., the `Component–Issue: 3,200 edges` edge), hover surfaces a tooltip card listing the top-3 concrete edges contributing to the aggregate (`Permission System ↔ #4231 [Resolves]`, `…`). Clicking opens the existing edge detail panel with a list of the top-N contributors, each clickable to navigate.

**Why** — today aggregate edges are opaque: they convey only "these two categories interact a lot." The user can't see *what* is interacting without drilling, which loses the cross-category context. Tooltip + click reveals the dominant flows behind each aggregate edge, surfacing bridge entities directly from the high-level view.

**Effort** — small. The aggregate-to-concrete mapping is a single pass over `originalEdges` filtered by endpoint category pair (one-time precompute cached per edge). vis.js's `title` attribute already supports HTML tooltip rendering for edges. The click path reuses the existing `showEdgeDetail` infrastructure; only need a new "show top contributors" wrapper. ~80 lines.

### 9c. Resurrect the Category → EntityType expand step (Tier 3)

**What** — restore the orphaned entity-type view but route it correctly: clicking a super-node in Category view expands it into its 3-6 entity-type sub-nodes in the same canvas (not a hard view switch). E.g., clicking `Component` reveals API / Service / Component-as-entity-type sub-bubbles, each clickable to drill into Node view. A second click on the parent collapses back.

**Why** — the current category-mode jumps from 12 nodes to 16k nodes in one click, skipping the natural intermediate granularity. The orphaned `entityType` view (still in `graph.js` but unreachable since `drillIntoCategory` was repointed at Node view in commit `a36ed5a`) was halfway there but routed wrong. Restoring it as an inline-expand step gives a proper Category → EntityType → Node hierarchy and lets the user explore at the right zoom level for the question at hand.

**Effort** — medium. The `buildEntityTypeView`, `drillIntoEntityType`, and `entityType` branches of `switchView` / `renderBreadcrumb` already exist in `graph.js`. Repurpose into an "expand-in-place" pattern (super-node + its entity-type children rendered together; non-expanded super-nodes still visible). Layout: nest entity-type children inside the parent super-node's bounding region or attach as orbiting satellites. The existing `category-mode` CSS body class disables filter checkboxes there — needs revisiting since expanded entity-types may want filterable. ~2 days. Doc update: drop the "orphaned `entityType` view" paragraph from `docs/analysis/visualize-frontend.md` once the repurpose lands.

### 9d. Cluster overlay dots on super-nodes (Tier 3)

**What** — for each super-node in Category view, render 3-5 small colored dots representing the top Louvain communities whose members fall inside that super-category. Dot size scales with overlap count; dot color matches the cluster's accent (could reuse the cluster lock's blue palette or generate per-cluster from the Louvain community IDs).

**Why** — surfaces internal heterogeneity. Today `Component` looks like one homogeneous mass of 6,438 members; in reality it spans many distinct Louvain communities. Cluster dots tell the user "this category is actually 4 sub-communities" before they drill in, which redirects exploration to the right granularity.

**Effort** — small. Per-category cluster overlap is a single pass over `originalNodes` grouped by `(superCategory, community)`. Render the dots as small vis.js child nodes anchored to each super-node, OR (simpler) as CSS-positioned overlays drawn on the canvas via the same coordinate-projection pattern used by the focus-mode highlights. ~1 day.

---

## Graph algorithms (deferred)

The following are useful structural / similarity algorithms not yet implemented. The four highest-leverage picks (Louvain communities, personalized PageRank, articulation points, Yen's k-shortest paths) shipped as a separate batch; the items below are the ones that didn't make the cut.

### A. Betweenness centrality

**What** — for each node, the fraction of all-pairs shortest paths that pass through it. Identifies *bridge* nodes that connect otherwise distant parts of the graph (different from articulation points: betweenness is a ranked score; articulation points are binary cut-detector).

**Why** — surfaces "structurally critical" nodes whose importance isn't visible from degree alone. A node with degree 8 sitting between two dense clusters can have higher betweenness than a degree-200 hub deep inside one cluster.

**Effort** — exact computation is `O(n × m)` (~400M ops on this corpus); too slow for build time. NetworkX `betweenness_centrality(G, k=200)` samples 200 random pivots, runs in ~5–10 s and gives a usable approximation. Per-node score shipped with the data.

**Surface** — add a "Bridge: 0.04" badge to the detail panel; possibly a "Top bridges" section in the glance panel; or a viz overlay highlighting top-50 bridges.

### B. K-core decomposition

**What** — assigns each node a `coreNumber` such that the node is part of a maximal subgraph where every node has degree `≥ coreNumber`. Linear-time (NetworkX has `core_number(G)`).

**Why** — "structural backbone" — better than degree at distinguishing densely-embedded nodes from peripheral high-degree nodes. A degree-50 node in a sparse periphery has lower core number than a degree-20 node inside a dense cluster of similarly-connected peers.

**Surface** — replace or augment the current `isHub` flag (which is just top-N by degree). New label-LOD mode "Backbone" shows only nodes with `coreNumber ≥ k`. Detail-panel chip: `Core: 7`.

### C. Adamic-Adar similarity ("find similar entities")

**What** — for two nodes `u, v`, `AA(u,v) = Σ over common neighbours w of 1 / log(degree(w))`. Common-but-popular neighbours count less than rare ones. Cheap on-demand for one query node: O(deg(u) × avg-deg).

**Why** — answers "what other entities are structurally like this one?". Complements personalized PageRank — PPR is "what's relevant to this", AA is "what's structurally a sibling".

**Surface** — "Similar entities" section in the detail panel, top-10 by AA score, clickable to navigate. Particularly useful for spotting near-duplicates / entity-extraction collisions.

### D. Steiner-tree approximation

**What** — given a *set* of nodes (e.g., "all nodes in the Modeling category" + "all 2024.06 issues"), find a minimum-edge subgraph connecting them all. NP-hard exactly; cheap heuristics (shortest-path heuristic, MST-based) within 2× of optimal.

**Why** — generalizes path-finding from "between two nodes" to "the connecting tissue of this set". Pairs naturally with the multi-set path explorer (#5 above) — instead of "all paths between A and B", show "the smallest tree linking A and B together".

**Surface** — toolbar button next to the multi-set path explorer: "Find connecting tree". Highlight tree edges + nodes.

### E. Local clustering coefficient

**What** — per-node `C(v) = (triangles through v) / (deg(v) choose 2)`. Range [0, 1]; 0 = star (no triangles), 1 = clique. NetworkX has `clustering(G)`. Cheap.

**Why** — answers "is this node in a tight community or a star hub?". A degree-50 node with `C = 0.05` is a *connector* (its neighbours don't know each other); same degree with `C = 0.6` is *embedded in a clique*.

**Surface** — detail-panel chip: `Cluster: 0.42`.

## Recommended next step

#1 (topic provenance), #2 (time-window), and #4 (open-in-stats/query) are all shipped. Of the remaining open items, **#3 (LLM explain this subgraph)** is the next big bridge to the GraphRAG backend; it needs a local-server route. **#6 (edge-list sort/filter)** is the cheapest of the open items (~half day, no back-end work) and pays off fastest on highly-connected nodes. **#5 (multi-set path explorer)** generalizes today's pathfinding and is the most graph-theoretic of the lot.
