# Visualize: frontend / runtime (`static/graph.js`, `static/graph.css`)

JS-side reference for the visualizer runtime: view modes, label LOD, detail panels, click routing, and the runtime knobs the user can twiddle.

**Companion docs**:
- `docs/analysis/visualize-build.md` — the Python build pipeline (graph load, algorithms, output layout, color palette).
- `docs/analysis/rel-clusters-algorithm.md` — relationship-type bucket derivation.

## View modes + drill-down

Two reachable JS-side `viewMode` values, driven by `switchView(mode, opts)`:

| Mode | Default? | Nodes on canvas | What clicks do |
|---|---|---|---|
| `node` | **yes** — opens here | individual ~16k nodes + edges; existing filter/search/focus machinery | `#view-toggle-group` → `category`, or `Reset & Refit` → resets filters + re-fits Node view |
| `category` | no | ~7–12 super-category super-nodes (from `GRAPH_META.typeCounts` + `categoryEdges`); aggregate edges between them | single-click super-node opens the rich category detail panel (composition, hubs, bridges); the panel's **"Drill into <Cat>"** button does the legacy click-to-drill (`drillIntoCategory` → pre-check `.type-cb` → `switchView('node')`). Single-click an aggregate edge opens the **category-edge** detail panel. |

**Init at first paint** (line near the bottom of the DOMContentLoaded handler) does **not** call `switchView('node')` — it runs `applyFilters()` → `network.fit({animation: false})` → `applyLabelMode()` directly. The synchronous fit is essential: viewport-aware label LOD (below) reads `network.getViewPosition()` / `getScale()`, and an animated fit at init leaves those at PyVis defaults until `animationFinished` fires (no `zoom` events). Toggling Category→Node later goes through `switchView('node')`, which uses **deferred animated fit** wrapped in `runAfterPaint` so the loading overlay paints before the synchronous re-render blocks the main thread (see [Loading overlay](#loading-overlay)).

**Orphaned `entityType` view.** A third `viewMode === 'entityType'` (entity-type super-nodes within one super-category, fed by `GRAPH_META.entityTypeCounts` + `entityTypeEdges`) was the drill target between Category and Node, but it's degenerate when a category contains a single entity type — common for the structural categories — and the `category-mode` CSS disables filter checkboxes there, leaving the user with nothing useful to do. `drillIntoCategory` was repointed at Node view in commit `a36ed5a`. The `buildEntityTypeView`, `drillIntoEntityType`, and the `entityType` branches of `switchView` / `renderBreadcrumb` / the click handler are still present in `graph.js` but unreachable from the UI; the Python-side `entityTypeCounts` / `entityTypeEdges` payload is still emitted. Remove together if you want the cleanup.

**View toggle** lives in the canvas toolbar (top-right) as the leftmost element — a segmented control `#view-toggle-group` (`Category` / `Node` halves). Was previously in the control-panel header; relocated so it's adjacent to the canvas it controls and stays visible when the control panel is collapsed. The `#toolbar > button` selectors are scoped to direct children so the segmented control's inner buttons don't inherit the toolbar's individual rounded-rect styling. Clicking the active half is a no-op; the `Node` half is `.active` at first paint.

`Reset & Refit` resets every filter (search, sliders, type/rel checkboxes, community lock) and returns to Node view + re-fits the canvas.

### Async-boundary invariant

`switchView('node')` defers its heavy block via `runAfterPaint(fn)` (two nested `requestAnimationFrame`) so the loading overlay paints before the synchronous re-render blocks the main thread. **Any helper that calls `switchView('node')` and then needs the new DataSet must queue its post-switch work via `runAfterPaint` too.** The pattern is in `drillToNode`, `filterToCommunity`, `filterToRelType`, and the `data-edge-id` branch of the `#detail-content` delegated handler. Calling `selectNodes` / `focus` / `showNodeDetail` / `showEdgeDetail` immediately after `switchView('node')` silently no-ops because the DataSet hasn't been hydrated yet.

### Breadcrumb

A right-aligned `<div id="breadcrumb">` above the toolbar renders the current drill path:

- `category` mode → `All` (current, inert).
- `node` mode with exactly one category checkbox checked → `All › <Category>`, with `<Category>` as the current (inert) leaf.
- `node` mode otherwise → `All › Nodes` (Nodes is the current leaf).

Segments are click targets; `.current` is inert. Re-rendered from inside `switchView` and at the end of `applyFilters` so filter-box changes update the leaf.

When a cluster lock is active (`communityFilter !== null`), a chip is appended after the breadcrumb segments — see [Cluster lock + breadcrumb chip](#cluster-lock--breadcrumb-chip).

## Label LOD

`labelMode` JS state, one of `'hubs' | 'all' | 'none'`. Default `'hubs'`. The toolbar control is a `<select id="label-mode-select">` so all three options are visible on click.

Three runtime knobs live on a `labelSettings` object, configurable from the **Label density** collapsible section in the control-panel sidebar and overridable via URL params (`?hubLabels=N&viewportThreshold=N&allCap=N`):

- `labelSettings.hubLabels` (default = `GRAPH_META.hubLabelCount`, i.e. the `--hub-label-count` baked into the build, default 100) — top-N by degree get static labels in Hubs mode.
- `labelSettings.viewportThreshold` (default 60) — Hubs-mode "label everything in viewport" gate.
- `labelSettings.allCap` (default 200) — All-mode hard cap on labels (top-N by degree when more would qualify).
- `FIT_ANIM_LABEL_DELAY = 450` ms (still hardcoded — tuned to the `network.fit` animation duration + 50 ms buffer).

| Mode | Static labels |
|---|---|
| `hubs` (default) | super-nodes + global `isHub` PLUS, when ≤ `viewportThreshold` non-hidden nodes are inside the canvas viewport, **all of them** |
| `all` | super-nodes + every visible-in-viewport node, capped at top-`allCap` by degree (`_topNByDegree`) when more would qualify |
| `none` | super-nodes only |

Slider behaviour:
- Each slider live-applies on every `input` tick (debounced 100 ms). The hub-count slider also calls `recomputeIsHub()` first — single pass over `originalNodes` (~5 ms on the canonical corpus) to update each node's `isHub` flag, then `applyLabelMode()`.
- URL-param overrides at init: if the user passed `?hubLabels=N` and N ≠ the build-time value, `recomputeIsHub()` runs once before first paint so the very first render reflects the override. `_bindLodSlider` extends the slider's `max` if the URL value exceeds the HTML default, so the slider stays addressable.
- A **Reset to defaults** button restores `DEFAULT_LABEL_SETTINGS` and re-runs both helpers.

The Label density section also surfaces a live `N labels · M nodes in viewport` readout — `inViewport` is hoisted out of the mode branches in `applyLabelMode()` so the count is available even in `none` mode. Updates on every label-mode pass.

**Cursor-hover labelling is universal** across all three modes (`hoverNode`/`blurNode` handlers). If the hovered non-super node's static `font.size` is 0, it's bumped to 11 on hover and reset on blur. Skipped if the static logic already shows the label.

`blurNode` guards the `nodeData.update` with `if (!nodeData.get(id)) return;` — vis-network's `DataSet.update({id, ...})` *inserts* a new minimal node when the id isn't found, so a delayed blur on a real-node id after `switchView('category')` cleared the DataSet would otherwise spawn a phantom orphan node.

**`_inViewportNonHidden()`** computes the in-viewport non-hidden node IDs by reading `network.getViewPosition()` + `getScale()` against `network.getPositions()`. ~1 ms on the canonical corpus. **`_topNByDegree(idSet, n)`** is the All-mode cap helper.

**Triggers for `applyLabelMode()`**:
- Mode change (dropdown `change` handler).
- Filter change (end of `applyFilters`).
- Focus enter (`focusOnNode`) — schedules `setTimeout(applyLabelMode, FIT_ANIM_LABEL_DELAY)` after `network.fit`. Same for `_exitFocus()` (called from the breadcrumb focus-mode-chip × or the Focus button's × in the active state).
- Zoom + dragEnd via `scheduleLabelUpdate` (debounced 150 ms). vis.js fires both during user gestures.
- Init (synchronous, post-`network.fit({animation: false})`).

`applyLabelMode()` writes `font.size` (0 or 11) per node and calls `nodeData.update()`. Hover override is a separate per-node update path that doesn't go through this function.

## Per-node + per-edge fields the JS reads

Build-time `_add_nodes_to_net` / `_process_edges` (see `visualize-build.md` → Build seams) attach these:

### Per-node

- `isHub: bool` — true for the top-N nodes by degree. N is `--hub-label-count` at build time, but runtime-mutable via the **Hub labels (top-N)** slider in the Label density section + the `?hubLabels=N` URL param. `recomputeIsHub()` re-derives the flag in a single pass over `originalNodes` and writes back via `nodeData.update`.
- `isSuperNode: bool` — only present on category/entity-type super-nodes built JS-side; never on graph-derived nodes.
- `isEntityTypeNode: bool` — only on `__type__...` super-nodes; vestigial (the click branch that reads it is unreachable now that `drillIntoCategory` skips Entity Type view).
- `isArticulation: bool` — true for nodes whose removal disconnects part of the graph (Tarjan's articulation points; computed at build time).
- `community: int` — Louvain community ID (≥ 0). Sorted by community size descending: 0 is the largest. `GRAPH_META.communitySizes[community]` gives the size.

- `topicIds: list[str]` — deduped, first-mention-order list of topic ids the entity was extracted from. Resolved at build time via the two-hop join `kv_store_text_chunks.json` → `kv_store_full_docs.json`. See `visualize-build.md` → Topic provenance pre-pass. Hub entities can carry 100+ ids. `_unionAndRankTopicIds(node.topicIds, connEdges)` combines the node's own ids with every incident edge's `topicIds` and ranks by incidence count — fixes the User-/Tag-/Category-style case where the node's own `source_id` only points at the first-definition chunk while per-topic provenance lives on incident edges.
- `fullDescription: string` — same per-phrase contract as edges (see below). LightRAG's `<SEP>` separator is normalized to `\x1f` at build time (`visualize.py:_add_nodes_to_net`), so the JS panel splits on `\x1f` into one `<p class="desc-phrase">` per merged-extraction phrase. Phrase order does NOT align with `topicIds` order — LightRAG sorts descriptions by `(timestamp, -length)` independently of `source_id`. The panel surfaces an `N phrases · K topics` annotation reporting multiplicity, never a per-phrase mapping.

### Per-edge

- `fullDescription: string` — raw `\x1f`-separated description payload from the graphml (the build pre-normalizes LightRAG's `<SEP>` to `\x1f`). The PyVis `title` attribute carries a truncated, `\n`-joined plain-text form for the hover tooltip; `fullDescription` keeps the original separators so the JS edge-detail panel can split per phrase into a `<li>` list.
- `relCategory: string` — the rel-type bucket assigned by `_classify_edge`; drives both the edge color and the "Filter to <rel>" panel button.
- `edgeWeight: float` — surfaced for the panel (vis.js consumes `width`, but we want the original weight separately for display).
- `topicIds: list[str]` — same shape as the node-level field. Edges typically have 1–3 source topics; the union across all edges plus the node-side union is what `GRAPH_META.topicIndex` covers.

## Edge interactions

### Click → detail panel

`network.on('click')` dispatches by `viewMode` and click target:

| viewMode | nodes / edges clicked | Action |
|---|---|---|
| `node` | edge | `showEdgeDetail(params.edges)` — concrete-edge panel with rel-chip, full description, Filter-to-rel button. |
| `category` | edge (super-edge) | `showCategoryEdgeDetail(catA, catB)` — aggregate-edge panel; categories extracted from the `__cat__`-prefixed endpoint ids. |
| `category` | super-node (`isSuperNode && !isEntityTypeNode`) | `showCategoryDetail(nodeId)` — opens the rich category panel; **drilling is no longer the default click**, it lives behind the panel's "Drill into <Cat>" button. |
| `node` | node | `showNodeDetail(nodeId)`. |

`showEdgeDetail` accepts an **array** of edge IDs (vis.js can return multiple at intersection points; this corpus is 1:1 per node-pair, but the contract holds for parallel edges in future corpora). For each real edge:

- Source/Target endpoints render as `.conn-item[data-node-id]` spans — clickable via the existing `#detail-content` delegated handler that calls `showNodeDetail(targetId)`.
- Rel-category chip colored from `GRAPH_META.relationshipColors`.
- `fullDescription` split on `\x1f` into a `<ul class="edge-phrase-list">`; single-phrase edges show one `<li>`.
- "Filter to <rel>" button calls `filterToRelType(relCat)` which scopes the rel-cb to that single bucket and re-runs `applyFilters`.

### Selection highlight

`applyEdgeSelection(edgeIds)` bumps width × 2.5 and color opacity → 1.0 on each selected edge, snapshotting originals into `selectedEdgeBackup[id]`. `clearEdgeSelection()` restores from snapshot. Called automatically before any new edge selection, on transition into `showNodeDetail` / `showCategoryDetail`, and from `closeDetailPanel()`. Implemented this way because the Python-side edge color uses `highlight = base color`, so vis.js's built-in selected styling is visually identical to non-selected — the JS-side override is the only way to make the selected edge actually pop.

`applyNodeSelection(nodeIds)` is the parallel for nodes: bumps `borderWidth` (and `borderWidthSelected`) by +4 and overrides `color.border` to `#ffffff`, snapshotting originals into `selectedNodeBackup[id]`. `clearNodeSelection()` restores. Called from `showNodeDetail` and `showCategoryDetail` (single-node selection); cleared from `showEdgeDetail`, `showCategoryEdgeDetail`, `showClusterDetail`, and `closeDetailPanel`. Same root cause as edges — vis.js's built-in node selection ring is too subtle on the dark theme against already-saturated category colors. The white border + thicker stroke makes the selected node findable in a 400+ node graph without overriding the category color (background stays). `Object.assign({}, origColor, {border: '#ffffff'})` preserves vis.js's nested `highlight` / `hover` sub-objects on `color`. Each `*Detail` function pairs with `applyNodeSelection` or `clearNodeSelection` so a node panel always shows a halo and an edge / cluster / category-edge panel never carries a stale one over from a prior node click.

The node detail panel exposes the halo via two affordances: the existing **click-to-recenter title** (subtle), and an explicit **`Recenter` button** in the action row between Focus 2-hop and `Find path to...` (discoverable). Both call `recenterOnNode(nodeId)` which selects + animates `network.focus`. The category detail panel mirrors the action-row button. Finding the selected node in a busy canvas is now a single-button affordance instead of "scan the labels until you spot the matching one" (the original UX complaint that motivated the halo).

### Inline rel-chips on connection rows

Every connection row in the node detail panel renders a tiny rel-type chip after the node label (e.g. `Posted`, `Configures`). Each chip is **clickable** — `onclick="event.stopPropagation();filterToRelType(relCat)"` — scopes the canvas to that rel-bucket. The `event.stopPropagation()` prevents the click from also triggering the row's `data-node-id` navigation handler.

### `closeDetailPanel()`

Single helper called from the panel close button, the empty-canvas click branch, `switchView`, `drillToSuperCat`, `drillToEntityType`, and `unselect-btn`. Centralizes panel-hide + edge-highlight cleanup so a missed call site can't leave a stale highlight on the canvas.

### Unified nav history

Detail-panel back/forward arrows traverse a single mixed history. `navHistory` entries are `{kind, payload}` where `kind ∈ {'node', 'category', 'edge', 'cluster', 'cat-edge'}` and payload is:
- `'node'` / `'category'` → node ID string
- `'edge'` → edge-ID array
- `'cluster'` → community ID number
- `'cat-edge'` → `"catA||catB"` string (canonical alphabetical sort)

`navPush(entry)` accepts either an object or a legacy bare nodeId (auto-promoted to `{kind: 'node', payload: nodeId}`). Same-target consecutive pushes coalesce so the stack doesn't fill with duplicates from re-renders. `navGoTo(idx)` dispatches by `kind` to the matching `show*Detail` function.

The reopen-detail-button (`#reopen-detail-btn`) and reopen via `lastDetailKind` track all five kinds; both check `lastDetailKind` first and fall back to `lastDetailNodeId` for the `node` / `category` paths.

### Delegated `#detail-content` click router

Routes by closest matching attribute:
- elements with `data-node-id` → `drillToNode` (or `showCategoryDetail` for super-node IDs in Category view, or `showNodeDetail` in Node view)
- elements with `data-edge-id` → `switchView('node')` + `runAfterPaint(showEdgeDetail([edgeId]))`

The async wrapping is mandatory — see the [async-boundary invariant](#async-boundary-invariant) above.

## Search

`#search-input` is debounced 200 ms and passes through `applyFilters`. Match predicate covers three node fields (label, `fullDescription`, `entityType`) — matches in any field count as `searchOk = true`, the node lands in `matchIds` (full opacity), and its 1-hop neighbors come in via `neighborIds` (dimmed). The same predicate the table-view search has used since day one, brought to parity on the canvas. `entityType` matches catch typo'd or rare types that aren't in the entity-type checkbox list.

**Search-context strict gate.** Neighbors of search matches must still pass the active type / degree / community / time-window filters — `applyFilters` builds a `passableNonSearch` set during the main loop (every gate except search) and the neighbor expansion checks `passableNonSearch.has(nid)` before adding to `neighborIds`. Without this gate, unchecking a type didn't actually hide that type's nodes when they sat 1 hop from a match, contradicting the legend (`Topic: 0`) and the `Nodes` stat. Now the legend, the stat, and the canvas all agree.

**`Show 1-hop context (dim)` toggle** under `#search-results` (state: `searchShowContext`, default `true`). Unchecked → the neighbor expansion is skipped entirely; only matches show. The Nodes stat exposes the dim contribution explicitly: `Nodes: 1 / 16,502 (+3 dim)` when there are dim neighbors, plain when there aren't. `#search-results:empty { display: none }` collapses the otherwise-reserved 16 px slot when there's no search yet, eliminating the visible gap between input and toggle. The toggle does **not** interact with `Focus 1-hop / 2-hop` (those write `node.hidden` directly via `focusOnNode`, not through `applyFilters`); the two are mutually exclusive — the first thing `applyFilters` does is `focusState = null`, so any filter / search edit exits focus.

**Pin neighbor strict gate.** When a node is pinned (clicked → `pinnedId`), its 1-hop `pinnedNeighbors` are also gated by `passableNonSearch` — same rule as search-context neighbors. Only the explicitly clicked node is sticky beyond filters (its visibility surfaces as the `(+1 pinned)` stat suffix). Pin neighbors that pass type / degree / community / time-window appear dimmed and contribute a `(+N pin-context)` suffix; ones that fail the active filters are hidden, matching the legend / stat counts and avoiding the surprise where unchecking a type left the pin's halo glowing in the pre-filter shape.

## Time-window slider + per-entity chip

A "Time window" collapsible section sits directly below Search in the control panel sidebar (rendered conditionally — only present when `GRAPH_META.timeBounds` is non-null AND spans ≥2 months). Two stacked `<input type="range">` over a shared `.time-slider-track`; each `<input>` has `pointer-events: none`, each thumb gets `pointer-events: auto`, JS clamps the lower thumb at ≤ upper. Step = 1 month-bin. A `.time-slider-fill` div drawn between the thumbs visualizes the active range. `_updateTimeFill(tw)` writes left/width percentages on every `input` event (live, before the debounced `applyFilters` fires).

**Thumb stacking on overlap.** When `lo === hi`, both thumbs occupy the same pixel and only the topmost (later in DOM = the right thumb by default) captures clicks — without intervention the user could drag the left thumb to meet the right and never grab the left back. Fix: a `pointerdown` handler raises whichever thumb the user just touched (`z-index: 3`) and demotes the other (`z-index: 1`). Last-touched = on top, so dragging a thumb leaves it grabbable for the return trip. Symmetric for both thumbs.

`_readTimeWindow()` returns `{lo, hi, bMin, bMax, active}` from the two slider DOM values; `active = (lo > bMin) || (hi < bMax)` so the cheap full-range default short-circuits all gating.

`_edgePassTime(e, tw)` is "any-overlap": passes when `e.tM >= tw.lo && e.tm <= tw.hi`. Edges with no `tm`/`tM` (build couldn't resolve any source-topic createdAt) pass any window — no signal, no penalty.

Composition with other filters:

- **Edges**: time gate ANDs into the existing `endpointsVisible && weightOk && relOk` predicate.
- **Nodes**: when `tw.active`, a precomputed `nodeTimeOk: Set<id>` collects every endpoint of an in-window edge in one O(E) pass at the top of `applyFilters`. `_nodePassTime(n)` returns `!nodeTimeOk || nodeTimeOk.has(n.id)` (null-safe full-range short-circuit). Isolated nodes (no edges) follow the existing visibility cascade — if no incident edge passes, they hide.
- **`passableNonSearch`**: includes time gate, so search-context + pin-neighbor expansions hide neighbors that fall outside the window. Symmetric with the type / community gates.
- **Min Degree slider's `degMaxNoThreshold`** + **type-row leave-one-out `M_type`** + **rel-row leave-one-out `relM`** all also AND time in. Without that, leaving the time slider narrowed but unchecking a type would show a `+M` gain pill that overstated what would actually return on re-check.
- **Reset & Refit**: clears the slider thumbs back to `bMin`/`bMax` (full range).
- **Slider movement exits focus mode**: free, via the existing `focusState = null` at the top of `applyFilters`.

The drop pill (`#time-drop-pill`) shows `−N` (count of edges hidden by the current window) and is clickable to reset to full range. Pattern mirrors the Min Degree + Min Edge Weight drop pills.

## Detail-panel: Query / Stats split-button

A single `[Query] [▾]` split-button in `showNodeDetail` and `showEdgeDetail` action rows, mirroring the existing Copy split-button shape. Primary click copies the natural-language `query` command (default — most users want to ask the GraphRAG backend a question first; the structural-SQL `stats` path is the analytical drill-down). The ▾ menu (`#qs-menu`) offers `Copy as Query` + `Copy as Stats command`.

Bridges the visualizer to the existing `query` (LightRAG) and `stats` (DuckDB analytics) tools without re-typing the entity name across tool boundaries.

Command builders live near the time-window helpers:

- `_buildStatsCommandForNode(node)` — entity-type-aware default. Topic → `SELECT post_number, username, length(plain_text), plain_text FROM posts WHERE topic_id = N` (id from `node.topicIds[0]`). User → `SELECT … FROM posts WHERE username = '<label>'`. Tag → join `topic_tags` to `topics`. Category → `SELECT … FROM topics WHERE category_name = '<label>'`. Everything else (Component / Issue / Document / Model / etc.) falls through to the existing `stats search "<label>"` subcommand — plain-text ILIKE that works for any entity name.
- `_buildStatsCommandForEdge(edge)` — `SELECT … FROM posts WHERE plain_text ILIKE '%a%' AND plain_text ILIKE '%b%'`. Free-text co-occurrence; doesn't always land (LLM-extracted entities can be paraphrased in prose) but is the most defensible default.
- `_buildQueryCommandForNode(node)` — `query "<DATA>" "Tell me about <label>."`.
- `_buildQueryCommandForEdge(edge)` — `query "<DATA>" "How does <a> connect to <b>?"`.

Quoting: `_shQuote` wraps shell args in `"…"` and escapes `\` + `"`. `_sqlEscape` doubles `'` for SQL string literals. Path comes from `GRAPH_META.dataDir` (shipped at build time by `visualize._format_data_dir_for_meta`: relative to cwd when the data dir lives under it — so committed fixture HTML stays portable — absolute otherwise so user-local builds paste-and-run from any cwd).

Rendering: `_renderStatsQuerySplit({stats, query}, anchorPrefix)` emits a `.copy-split` wrapping two buttons: `.statsq-primary` carries `data-qs-cmd` (the query command, copied on click) and `.statsq-toggle` carries both `data-qs-query` + `data-qs-stats` + `data-qs-anchor`. The toggle's inline `onclick="openQsMenu(this)"` opens `#qs-menu`, which on item click reads its own data-attrs (set when the menu opens) and copies the chosen command. The `#detail-content` delegated click router has a `.statsq-primary` branch — matched BEFORE `data-node-id` so the button (sitting in `.detail-actions`, no row data) doesn't fall through to a node-row click. Each button's `title="Copy: …"` previews the full command on hover.

The data-attribute approach (vs inline `onclick="copyToClipboard('…')"`) avoids escaping the SQL's literal `"` characters into a multi-quoted attribute. `_attrEsc(s)` (escapes `&`/`<`/`>`/`"`) is the helper for the data-attr value.

Anchor IDs are sanitized via `[^a-zA-Z0-9-]/g → '_'` since entity labels can contain spaces, dots, quotes — those would break HTML id semantics.

**Per-entity time-range chip**. `_buildTimeRangeChip(node, connEdgeIds)` is called from `showNodeDetail` and inserted at the end of `.detail-meta`. Sources both the node's own `topicIds` and every incident edge's `topicIds` (mirrors `_unionAndRankTopicIds`), resolves each via `GRAPH_META.topicIndex[tid].createdAt`, and emits `Active YYYY-MM – YYYY-MM`. Single-month ranges collapse to `Active YYYY-MM`. When ≥4 incident edges span ≥2 quarters AND one quarter holds ≥25% of edges (and ≥3 absolute), a `· peaked YYYY-Qn (N edges)` suffix is appended. Below those thresholds the "peak" is noise. Returns `''` when the union has no resolvable timestamp — silent skip on isolated/unprovenanced nodes.

Helpers: `_tsToYearMonth(unixSec)` and `_quarterFromMonthBin(bin)` are inverses of the build-side `_month_bin_label` / `_ts_to_month_bin`. Same epoch (2018-01).

## "At a Glance" panel

A collapsible control-panel section (`<h4 data-section="glance">`) populated by `updateGlancePanel(visibleIds)`, called from the end of `applyFilters`. The section body holds a `#stats` block at the top, rendered as a flex row of `.stat-block` cells (small uppercase label on top, tabular-nums value below — easier to scan than running prose; values like `Nodes 8,835 / 16,502`, `Edges 13,300 / 24,715`, `In viewport 1,234`). The `In viewport` cell is layered on top by `applyLabelMode` (Node view only) so zoom + pan recompute it without a full filter pass. `expandNeighborhood` adds an inline orange `+5 expanded` chip that's wiped on the next render. The remaining body is three sub-lists, each top-5 by count:

- **Top entities by degree** within the visible set (one pass over `visibleIds`, running min-heap-of-5).
- **Top super-categories** by visible-node count.
- **Top relationship buckets** by visible-edge count (one pass over `edgeData.get()` filtered by `!ed.hidden`).

Each row is clickable: entities → `showNodeDetail`, super-categories → `drillToSuperCat`, rel-buckets → `filterToRelType`. In Category view (where `applyFilters` early-returns) the panel shows a placeholder; same when no nodes match the current filter.

When `communityFilter !== null`, a tinted blue banner is prepended to the body — `Showing: Cluster #N · k members · click for full summary`. Clicking opens the cluster detail panel.

`#stats` writers (`setStats`, `appendStatsNote`, `setStatsViewportCount`) only touch the `.stat-block` cells / inline notes so the cluster banner stays intact across filter / focus / category-view writes.

### Rel-type `visible (total)` counter

Each rel-filter row shows two numbers: a **visible** count and a **total** count. Initial render has both equal (e.g. `3425 (3425)`), collapsed to a single number via the `.equal` CSS class which hides the parenthesized total.

When the user unchecks an entity-type filter, the `applyFilters` JS pass recomputes how many edges of each rel-bucket have **both endpoints** whose entity type is currently selected (entity-type gate only — degree, weight, search, and the rel-type checkbox itself are deliberately ignored). The `.rel-count-visible` span updates live; the `.rel-count-total` becomes visible when `visible != total`.

## Graph algorithms (computed at runtime, JS)

### Personalized PageRank (`personalizedPageRank(seedId, opts)`)

Power iteration on `neighborIndex`. Defaults: α=0.15, 30 iterations, top-K=10. ~10 ms on the canonical corpus. Used by `showNodeDetail` to populate the **"Related"** section above the connections list. 1-hop neighbours of the seed are tagged with a small `1-hop` chip so the user can tell when PPR is surfacing a structurally close 2- or 3-hop node beyond the direct neighbourhood.

### Yen's k-shortest simple paths (`findKShortestPaths(fromId, toId, K=3)`)

Replaces the older single-shortest-path BFS. Uses `bfsShortestPath(from, to, blockedNodes, blockedEdges)` as a primitive; the spur-node loop blocks each prefix-sharing edge to force diverse alternatives. Default K=3.

`highlightPaths(paths)` accepts an array, colours each route distinctly (yellow / cyan / orange via `_PATH_COLORS`, first-path-wins on shared edges), and renders one panel block per path.

**Path-clear bug fix**: `pathsHighlighted` boolean gets set in `highlightPaths`. `applyFilters` checks the flag at the end of its edge-update step and, when set, restores each visible edge's original `color` + `width` from `originalEdgeById` and resets node `borderWidth`. Was: "Clear paths" only re-ran `applyFilters` which only touched `hidden`, leaving the path-style overrides in place.

## Detail panel additions

In addition to the description and connections list (existing), the node detail panel surfaces:

- **Hub rank** — `degreeRank[nodeId]` is computed once at startup via a sort over `originalNodes`. Rendered as `Degree: 247 [#3 of 16,502 · top 1%]` in the meta line. Top-N suffixes at 1% / 5% / 10% thresholds.
- **Lonely indicator** — `X of Y connections visible` in orange in the meta line, when `hiddenCount > 0`.
- **Cut-node badge** — when `n.isArticulation` is set.
- **Cluster badge** — `Cluster #N (k nodes)`. Clickable to invoke `filterToCommunity(N)`.
- **Related (PPR)** section — top-10 by personalized PageRank, rendered above the per-category connections list.
- **Copy split-button** — `[Copy][▾]`. Left half copies the entity label (legacy single-click behavior). Right half opens a small floating menu with three formats:
  - **Copy name** — same as left-click.
  - **Copy one-liner** — share-ready prose (e.g. `Permission System (Topic) — 247 connections, mostly Component (32) + Issue (18). "First sentence of description"`).
  - **Copy as Markdown** — full panel as a take-home document (header, meta, description, PPR Related, connections grouped by category, top-25/cat with `… +N more` truncation).

  Implementation lives in `renderCopySplit(kind, payload, anchorId)`; the menu is a single shared `<div id="copy-menu">` lazy-created on first use, positioned under the toggle button (clamped to viewport edges), closed on outside click or Escape. Format dispatcher: `getCopyText(format, kind, payload)` for `kind ∈ {'node','edge','cluster','category','cat-edge'}`.
- **Click-to-recenter title** — the `<h3>` has class `.detail-title`; click invokes `recenterOnNode(nodeId)` which selects + animates `network.focus`. Same for category panel.

Edge panel:
- **Copy split-button** — same widget; default copies `<source> -> <target>`, Markdown emits the full edge with phrase-split description.
- **Click-to-recenter title** — `recenterOnEdge(edgeId)` selects the edge and `network.fit({nodes: [from, to]})` to bring both endpoints into view.

`window.showNodeDetail`, `window.recenterOnNode`, `window.recenterOnEdge`, `window.copyToClipboard`, `window.filterToCommunity`, `window.filterToRelType`, `window.showAndFocusNode`, `window.showClusterDetail`, `window.recenterOnCluster`, `window.showCategoryEdgeDetail`, `window.filterToTwoCategories`, `window.drillToNode`, `window.openCopyMenu`, `window.getCopyText` are all exposed for inline `onclick` use across the detail panels and the glance rows.

### Cluster summary panel (`showClusterDetail`)

Triggered by clicking the **Cluster #N** badge in a node panel, the **Cluster #N** banner in the glance panel, or the breadcrumb chip's label area. Renders five sections:

1. **Header** — `Cluster #N`, member count, rank `#N of 1,604` with top-X% suffix, optional cut-node count chip.
2. **Action row** — Lock/Unlock toggle (active when `communityFilter === N`), Zoom-to-fit (`recenterOnCluster` → `network.fit({nodes: [members]})`), Copy split-button.
3. **Composition** — entity-type histogram across cluster members (mini bars, % of cluster).
4. **Top members (within-cluster degree)** — top-10 by internal degree (counted only among edges where both endpoints are in the cluster). Click → drill into the entity.
5. **Top relationships (within cluster)** — rel-bucket histogram for internal edges, top-8.
6. **Bridges to other clusters** — total bridge-edge count + top-5 external counterparts (each clickable).

Single pass over `originalNodes` + `originalEdges` per cluster click, ~5 ms on the canonical corpus. Not cached (clusters can be re-clicked freely).

### Cluster lock + breadcrumb chip

`communityFilter` JS state, `null` or a community ID, ANDed into `applyFilters` so a node passes only if `communityFilter === null || n.community === communityFilter`.

`filterToCommunity(communityId)` toggles: re-clicking the same cluster's badge unlocks; clicking a different one swaps the lock; passing `null` clears. After every change it refreshes the open detail panel (`'node'` → `showNodeDetail`, `'cluster'` → `showClusterDetail`) so the badge `.active` state and the breadcrumb chip stay in sync. When called from Category view, the refresh is queued via `runAfterPaint` (see [async-boundary invariant](#async-boundary-invariant)).

When locked, `renderBreadcrumb` appends a chip after the existing segments:

```html
<span class="cluster-lock-chip">
  <span class="cluster-lock-chip-label" data-action="open-cluster">Cluster #N (size)</span>
  <span class="cluster-lock-chip-close" data-action="unlock">×</span>
</span>
```

Clicking the **label** opens the cluster detail panel; clicking the **×** unlocks. The chip is the only canvas-scope indicator that a lock is active; before this, the lock was invisible state and the only way to clear it was Reset & Refit (which also wipes search/sliders/checkboxes).

### Focus mode + breadcrumb chip + active button × pattern

`focusOnNode(nodeId, hops)` does its own BFS-driven `node.hidden = true/false` write and stores `focusState = {nodeId, hops, memberCount}`. While focus is active, two surfaces signal the state:

1. **Breadcrumb focus-mode-chip** (amber, distinguishable from the blue cluster-lock chip):
   ```html
   <span class="focus-mode-chip">
     <span class="focus-mode-chip-label" data-action="open-focus">Focus 1-hop · ivluu (24)</span>
     <span class="focus-mode-chip-close" data-action="exit-focus">×</span>
   </span>
   ```
   Label opens the focused node's detail panel; × calls `_exitFocus()`. Replaces the old `#exit-focus-btn` toolbar button (users overlooked it).
2. **Focus 1/2-hop buttons in the node detail panel** flip to `.action-btn.active` (amber) with a trailing `&times;` when `focusState` is on for THAT node + hops; click in active state calls `_exitFocus()` instead of re-applying focus. Mirrors the cluster `Cluster #N` badge's lock/unlock pattern, single discoverable surface inside the panel.

To keep the panel's button state in sync with `focusState`, three call sites re-render an open node panel via `showNodeDetail`:
- `focusOnNode` (after enter — button picks up active state).
- `_exitFocus` (after exit — button drops active state).
- `applyFilters` end-of-pass, when `hadFocus` flag was true at top-of-pass — covers the implicit-exit case (toggling a filter exits focus). Deferred via `setTimeout(0)` to avoid re-entrancy with listeners that fire during the rebuild.

`_exitFocus()` early-returns when `focusState === null` so the breadcrumb-chip × and the panel-button × are both idempotent.

### Category detail panel (`showCategoryDetail`)

Driven by `_categoryStats(cat)` — a session-cached helper that walks `originalNodes` + `originalEdges` once per category to produce: top-10 hubs by within-category degree, entity-type histogram, and per-other-category sorted concrete bridges.

Sections:
1. **Header** — category badge + name, member count + entity-type count (omitted when there's only one entity type).
2. **Action row** (at top) — `Drill into <Cat>` (the legacy click-to-drill, now explicit), Copy split-button.
3. **Composition by entity type** — mini bar histogram (e.g. `API: 4,200 (65%)`). **Skipped when the category contains a single entity type** (a 100%-only bar wastes vertical space).
4. **Top members (within-category degree)** — top-10 hub rows, each with a tiny entity-type chip; clickable to drill (`drillToNode` switches to Node view + opens entity detail).
5. **Inter-category edges** — each connected category as a row with its edge count, followed by its **top-3 indented bridge entities** under each pair (`› Permission System — 47 edges`). Bridges are clickable; turns aggregate edges into actionable navigation rather than opaque numbers.

Super-nodes also get a hover tooltip via `_categoryHoverText(cat)` (plain text with newlines: name + member count, top-3 hub labels, entity-type count). Lets the user scan all 12 super-nodes by hover before deciding which to drill into.

### Category-edge detail panel (`showCategoryEdgeDetail`)

Triggered by clicking an aggregate edge in Category view (e.g. `Component ↔ Issue: 3,200 edges`). Driven by `_categoryEdgeStats(catA, catB)` — single pass over `originalEdges` filtered by endpoint super-categories, session-cached under sorted `"catA||catB"` key.

Sections:
1. **Header** — dual entity-badge `catA ↔ catB` (or `catA (internal)` for self-edges), title, meta line: `N edges · total weight · avg · K rel-types`.
2. **Action row** (at top) — `Filter to both` (sets entity-type checkboxes to just these two and switches to Node view, via `filterToTwoCategories`), `Drill into <catA>`, `Drill into <catB>`, Copy split-button.
3. **Relationship types** — rel-bucket histogram across the inter-category edge set (clickable to `filterToRelType`).
4. **Top concrete bridges (by weight)** — top-10 rows, `<from> ↔ <to> [rel-chip] w=N`. Endpoint spans are clickable to drill into either side; clicking elsewhere on the row opens the concrete edge in `showEdgeDetail` (after switching to Node view).
5. **From <catA>** / **From <catB>** — top-5 contributing entities per side; clickable to drill. (For self-edges, a single "Top contributors" section.)

## Per-phrase description split

LightRAG joins per-chunk descriptions of the same entity / edge with `<SEP>`; the build (see `visualize-build.md` → "<SEP> normalization at load time") rewrites these to `\x1f` so the JS can split. `showNodeDetail` and `showEdgeDetail` emit one `<p class="desc-phrase">` per phrase, with a thin left-accent border per block plus a dashed `border-top` separator on `.desc-phrase + .desc-phrase` so multi-paragraph LLM-summarized phrases don't visually melt into the next phrase.

Each phrase may itself contain `\n\n` paragraph breaks (LLM summary output). Those render as nested `<p>` children inside the `.desc-phrase` wrapper so within-phrase spacing is correct and the dashed separator only fires at actual phrase boundaries.

When `descPhrases.length > 1`, a `<div class="desc-meta">` annotation (`N phrases · K topics`) is prepended; `K` is `node.topicIds.length` (the union the build attaches), reporting multiplicity, not a per-phrase mapping. Single-phrase descriptions render as plain prose without the annotation.

Markdown copy (`_mdNode`): single-phrase descriptions render as plain prose under `## Description`; multi-phrase descriptions emit one `### Phrase N` H3 subsection per phrase (subsections survive every Markdown renderer unchanged, unlike loose-list 4-space continuation, which can break in some renderers).

## Source topics (provenance) — node Connections + residual section

Both `showNodeDetail` and `showEdgeDetail` close with a Source topics surface. The closure looks different per panel:

- **Edge panel** — `_renderSourceTopics(e.topicIds)` emits the full list (per-edge provenance is already correct as-shipped from the build).
- **Node panel** — provenance is integrated into the Connections list **per Topic-typed neighbor row**. Each Topic connection row gets a `&#9656;` (`▸`) **`.conn-expand` button** that toggles a sibling `.conn-topic-detail` block populated from `GRAPH_META.topicIndex[cn.topicIds[0]]` (date · posts · started by · 2-line excerpt + Open-topic-preview button). When a group has ≥2 expandable rows, a per-group `.conn-expand-all` toggle (`Expand all` / `Collapse all`) sits to the right of the heading inside `.conn-group-header`. Implementations: `_toggleConnExpand(btn)` and `_toggleAllConnExpand(btn)` in `graph.js`.
- **Node panel residual** — after the per-row provenance, a `<details class="source-topics-residual">` wraps **only** the topics that aren't already named as Topic neighbors above (`residualTopicIds = unionRanked − topicIdsAlreadyShown`). Header: `Source topics — not shown above (N)` when there's overlap, plain `Source topics (N)` otherwise. Open by default when there's no overlap (residual is the only provenance signal), collapsed when there is. Hidden entirely when residual is empty — common for User / Tag / Category nodes whose Topic neighbors already cover every chunk.

Inputs:
- `node.topicIds` / `e.topicIds` — per-payload deduped lists from the build (see `visualize-build.md` → Topic provenance pre-pass).
- `GRAPH_META.topicIndex[tid]` — `{title, createdAt, postCount, excerpt, firstPostBy}` lookup; built at viz time from `<data-dir>/topics/<tid>.json`.

`_unionAndRankTopicIds(nodeTopicIds, connEdges)` is the union helper: counts incidence of each tid across the node's own `topicIds` plus every incident edge's `topicIds`, returns the ids sorted by count desc with insertion-order tie-break. This solves the structural quirk where a User entity's `source_id` only marks first-definition (1 chunk) while its actual per-topic provenance lives on incident `Posted` edges.

Each Source-topics row renders:
```
[<a href="javascript:openTopicModal(tid)">Title</a>]  YYYY-MM-DD · N posts · by <username>
  Excerpt of the first post (line-clamped to 2 lines via -webkit-line-clamp).
```

Cap at 10 rows (`_SOURCE_TOPICS_CAP`); longer lists end with a small italic `+ N more topic(s)` line. Click opens the topic preview modal (no `file://` JSON fetch — title + excerpt come from `topicIndex`).

Markdown copy: `_mdNode` renders Topic neighbors as `[Title](../topics/N.json) (#N)` with a 2-space-italic continuation for date/posts/author; the residual Source topics section is dropped entirely when `residualTopicIds.length === 0`.

## Filter sidebar — leave-one-out counts + pills

Each `.type-filter` and `.rel-filter` row shares a unified count layout:

```html
<span class="row-count [equal]" data-total="N">
  <span class="row-count-visible">L</span>
  <span class="row-count-total">(N)</span>
</span>
<span class="gain-pill" style="display:none">+M</span>
```

- **L** = visible-now (passes ALL filters, including this row's checkbox state).
- **N** = graph total (fixed; `data-total` carries it).
- **M** = leave-one-out count: visible if THIS row's filter were ignored, all OTHER filters honoured.

When `L == N` the `.equal` class hides the parens (avoids "X (X)"). The `.gain-pill` shows only when the row's checkbox is OFF AND `M > 0`; click it to re-enable the filter and recover `M` items.

`applyFilters()` computes `typeL`, `typeM`, `relL`, `relM`, `degMaxNoThreshold`, `weightMaxNoThreshold` in single passes over `allNodes` / `allEdges`. The slider rows (`Min Degree`, `Min Edge Weight`) get their own `slider-count` span (`L (M)` format) and `drop-pill` (`−drop` chip when the threshold has hidden items; click to reset to the slider's minimum).

A persistent `#filter-legend` div at the bottom of the control panel explains the format.

When the row's checkbox is unchecked, CSS dims the row's count-cluster (`opacity: 0.55`) to make the disabled state visually obvious.

## Loading overlay

Full-canvas spinner + message (`#loading-overlay`) shown while the main thread is blocked on the heavy initial `nodeData.add(window.GRAPH_DATA.nodes)` (~1-3 s for 16k nodes) and during `Category → Node` toggles in `switchView` (same heavy re-render path). The spinner uses a transform-based `@keyframes spin` with `will-change: transform`, so the browser composites it on a separate thread — keeps animating while JS is busy.

`switchView('node')` wraps its synchronous block in `runAfterPaint(fn)` (two nested `requestAnimationFrame`) so the overlay paints before the blocking work begins. `hideLoading()` runs at the end of init + at the end of the deferred block.

This deferral is also the source of the [async-boundary invariant](#async-boundary-invariant) — any helper that needs the new DataSet immediately after `switchView('node')` must queue its work via `runAfterPaint` too.

## Tooltip rendering

Vis-network 9.1.2 (the version pyvis bundles) renders string titles via text-content escaping — `<b>` and `<br>` show as literal characters. Passing an `HTMLElement` would render HTML correctly but **breaks node click events** in this build (a regression that's reproducible: HTMLElement-as-title nodes don't fire `click`, only edges do). So the visualizer keeps every title as a plain string with `\n` line breaks; CSS `white-space: pre-wrap` honours them.

Real-node tooltips (built in `visualize.py`) format as: `NodeId / SuperCat · entityType · deg N / blank line / truncated description`. Edge tooltips: `relCategory / phrase 1 / phrase 2 / phrase 3 [/ …]`. Super-node tooltips (`_categoryHoverText` in `graph.js`): `cat · N members / Top hubs: A, B, C / N entity types`. Aggregate-edge tooltips: `catA ↔ catB / N edges / total weight N`.

The tooltip CSS (`div.vis-tooltip`) caps width at 360px, dark theme, `pointer-events: none !important` so the box doesn't intercept clicks at the cursor position.

If you want HTML/colored tooltips back later, the path is a custom hover-overlay (manage our own `<div>` from `hoverNode` / `blurNode` events, ignore vis-network's tooltip system entirely). Don't try to pass HTMLElements to vis-network 9.1.2 — that's the trap.
