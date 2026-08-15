# Documentation index

Maintainer-facing catalog of `docs/` — per-file blurbs to help pick the right deep-dive. Open the file itself for the actual content.

For project overview + setup, see [`../README.md`](../README.md); for per-tool usage (CLI flags, env vars, examples, end-to-end workflow), see [`MANUAL.md`](MANUAL.md); for codebase invariants, commands, skill triggers, and read-before-editing triggers, see [`../CLAUDE.md`](../CLAUDE.md).

## `analysis/` — maintainer deep-dives

- **[`architecture-map.md`](analysis/architecture-map.md)** — module-by-module map + cross-cutting invariants. Start here when figuring out which file to open, or what gotchas apply across modules before making a change.
- **[`multi-pass-indexing.md`](analysis/multi-pass-indexing.md)** — `query.py` Pass 1/2/3/4 mechanics, the Counter-vote collision Pass 3 solves, `aedit_entity` contract, the deferred-VDB-writes optimization that brings Pass 4 from ~7h → ~150s, `--enrich-only` + `--canonicalize-only` recovery modes, `_split_for_embedding`, `PERSIST_EVERY`, Faiss vs NanoVectorDB.
- **[`entity-name-canonicalization.md`](analysis/entity-name-canonicalization.md)** — Pass 4 deep dive: case-fold + paraphrase merges via `_canonicalize_case_dupes` / `_canonicalize_user_paraphrases` / `_defer_pass4_writes` / `_apply_pass4_writes`, the canonical-pick rule that preserves Pass-1 seeds across cases, the deferred-VDB-writes architecture, observed numbers on the canonical corpus (16.5K → 15.8K nodes, 639 → 0 collision groups, 151s wall clock), and the two bugs caught + fixed during the first canonical run.
- **[`rel-clusters-algorithm.md`](analysis/rel-clusters-algorithm.md)** — `rel_clusters.py` pinned + discovered layers, Ward merge, prune-and-split balancer (incl. the "don't drain into biggest" subtlety and the size-rank reorder bug), edge-voting metric, cache schema v3.
- **[`visualize-build.md`](analysis/visualize-build.md)** — `visualize.py` Python build pipeline: output layout, color palette + `_assign_colors`, edge coloring + `_classify_edge`, legend sub-sections, skip-if-identical write gates (data.js / graph.html / rel-clusters.json), PyVis override, layout cache, CLI flags, build-time graph algorithms (articulation, Louvain), topic-provenance pre-pass (two-hop chunk→topic join via `kv_store_text_chunks.json` + `kv_store_full_docs.json`), `<SEP>` → `DESC_SEP` normalization at node + edge load time (LightRAG ordering caveat documented inline), coverage diagnostic, and the `build_visualization` seams (`_compute_node_metadata` / `_process_edges` / `_render_filter_section` / etc.) that future features should extend.
- **[`visualize-frontend.md`](analysis/visualize-frontend.md)** — JS/CSS runtime: view modes + drill-down (with the **async-boundary invariant** for `switchView('node')`), label LOD (Hubs / All / None, viewport-aware, universal cursor-hover) + runtime-configurable Label density section + URL params, per-node + per-edge fields the JS reads (`topicIds` + per-phrase `fullDescription`), click → detail panel routing (node / edge / category / cat-edge / cluster), edge selection highlight, inline rel-chips, unified nav history (5 kinds), `#detail-content` delegated click router, full-text search predicate + strict-context gate (`searchShowContext` + `passableNonSearch`) + pin-neighbor strict gate (`pinnedNeighbors` ∩ `passableNonSearch`), "At a Glance" stat-block + cluster banner + composable suffixes (`+N dim` / `+1 pinned` / `+N pin-context`), runtime graph algorithms (PPR, Yen's k-shortest paths), detail-panel additions (hub rank, cluster badge, Related/PPR section, Copy split-button with name/one-liner/Markdown formats, click-to-recenter title), per-phrase description split + multiplicity annotation, Topic-row provenance expand (per-row + group-level "Expand all"), source-topics residual `<details>` section, cluster summary panel + cluster lock + breadcrumb chip, **focus mode breadcrumb chip + active button × pattern** (replaces the toolbar Exit-Focus button; mirrors cluster-lock chip), category panel rework + category-edge panel, leave-one-out filter counts + gain/drop pills + legend, loading overlay, tooltip rendering (vis-network 9.1.2 quirks).
- **[`vocabulary-and-config.md`](analysis/vocabulary-and-config.md)** — two-tier config, `bootstrap()` flow, `entity_types.json` schema, `STRUCTURAL_REL_PINS`, full per-run env-var reference, codebase constants.
- **[`duckdb-views.md`](analysis/duckdb-views.md)** — `stats.py` view list, columns, example queries.
- **[`incremental-updates.md`](analysis/incremental-updates.md)** — per-tool support matrix for incremental updates (new topics + new posts), the edited-topic gotcha, Pass 3 stale-embedding recovery.

## `lightrag/` — LightRAG internals

Read before editing `query.py` / `discover_types.py` or debugging LightRAG.

- **[`LIGHTRAG_KNOWHOW.md`](lightrag/LIGHTRAG_KNOWHOW.md)** — ingestion APIs, chunking defaults, entity-extraction knobs, VDB structure, provenance. §18–§19 has the full collision/enrichment investigation behind Pass 3.
- **[`ProgramingWithCore.md`](lightrag/ProgramingWithCore.md)** — storage lifecycle, full `QueryParam`, storage backends, entity/relation CRUD.

## `discourse/` — Discourse domain reference

Read before editing `scraper.py` or writing field-aware queries.

- **[`DISCOURSE_TERMINOLOGY.md`](discourse/DISCOURSE_TERMINOLOGY.md)** — forum conceptual model, tags, roles, topic states, post interactions.
- **[`DISCOURSE_JSON_TERMINOLOGY.md`](discourse/DISCOURSE_JSON_TERMINOLOGY.md)** — JSON shapes, `id` vs `post_number`, `raw` vs `cooked`, pagination.

## `workflows/` — operational runbooks

Step-by-step procedures backing the `discover-entity-types`, `index-and-embed`, and `create-query-guide` skills. Authoritative reference when running the underlying CLIs without a skill host.

- **[`DISCOVER_ENTITY_TYPES.md`](workflows/DISCOVER_ENTITY_TYPES.md)**
- **[`INDEX_AND_EMBED.md`](workflows/INDEX_AND_EMBED.md)**
- **[`CREATE_QUERY_GUIDE.md`](workflows/CREATE_QUERY_GUIDE.md)**

## `ideas/` — forward-looking, not yet implemented

Priority-ordered proposals with effort/leverage notes. Read when the user asks "what's next?" or before scoping a feature in the same area.

- **[`visualizer-feature-roadmap.md`](ideas/visualizer-feature-roadmap.md)** — outstanding visualizer/UX features (topic provenance, time-window slider, LLM explain subgraph, multi-set path explorer, minimap+relayout, pinned set). All entries are confirmed doable against the existing graph — none require LightRAG re-indexing.
- **[`entity-resolution-llm-judge.md`](ideas/entity-resolution-llm-judge.md)** — Pass 5 / opt-in LLM-as-judge merge for the *semantic* dupes Pass 4 can't reach (`XYZ` ↔ `Cross-System Data Model`, `Acme Jira` ↔ `acme-jira-instance`). Tracks upstream HKUDS/LightRAG Issue #1323 + PR #2102; lays out three implementation paths (post-hoc judge ~$1, vendor-PR-#2102 ~$12, wait-for-upstream).
- **[`visualizer-build-perf.md`](ideas/visualizer-build-perf.md)** — speedup options for the cold visualizer build (currently ~6 min, layout dominates 95%). Five levers ranked by impact + risk: spring-layout iterations down (free, ~40%), python-louvain (~5% extra), multiprocessing for articulation/Louvain/layout (only worth it paired with #3), python-igraph for layout (~10-30×, the big lever, has a C-extension caveat), python-igraph for Louvain (consolidates with #3). Recommended sequence: conservative first, igraph only if cold builds happen often enough to matter.
