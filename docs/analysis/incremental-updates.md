# Incremental update findings

**Context:** Investigation into whether each tool in the `discourse_explorer` pipeline supports incremental updates (new topics + new posts in existing topics) without destructive rebuild.

## Per-tool support matrix

| Tool | Incremental? | Mechanism | Citation |
|---|---|---|---|
| **scraper** (`discourse_explorer.scraper`) | ✅ Native delta sync | `sync_state.json` stores `last_sync` timestamp. `/latest.json` pages are client-side filtered to topics where `bumped_at > last_sync OR last_posted_at > last_sync`. Both new topics AND topics with new replies bump those fields server-side, so both are caught. Each changed topic is re-fetched whole via `/t/{id}.json` and **overwrites** its JSON file (no per-post merging). `--full` forces a complete re-sync. | `scraper.py:348-364`, `scraper.py:172` (`fetch_all_topic_ids`), `scraper.py:394-395` (per-topic sync-state update) |
| **discover_types** (`discourse_explorer.discover_types`) | Schema-level only — not tied to topic counts | Re-run only when content focus shifts significantly (new product area, new issue category) or when the graph's `Other` bucket in `visualize` exceeds ~10%. Normal incremental scraping doesn't need it. | `docs/workflows/DISCOVER_ENTITY_TYPES.md` ("When to run") |
| **query --index** (no `--clear`) | Partial — see gotcha | LightRAG's `ainsert` keys documents by `compute_mdhash_id(full_text)`; already-processed docs skip via `doc_status`. Pass 1 (`ainsert_custom_kg`) runs for every topic on every invocation — idempotent by entity name, no LLM cost. Pass 2 (`ainsert`) skips unchanged docs → LLM cost only for new/changed content. Pass 3 (`_enrich_structural_types`) runs for every topic, but a skip-if-already-correct gate reads `get_node(name)` first and bypasses `aedit_entity` entirely when the stored type already matches — typically ≥90% of structural entities on a clean graph, so Pass 3 overhead on steady-state incremental runs is a few hundred fast graph reads, not 1800 re-embeds. | `query.py:476` (globs all topics, no mtime filter), `.venv/.../lightrag.py:1301,1430` (content-hash doc IDs), `query.py::_enrich_structural_types` (skip gate) |
| **query --index --enrich-only** | N/A — targeted refresh | Skips Pass 1 + Pass 2. Runs only Pass 3 against the existing graph with `force_rewrite=True` — bypasses the skip gate so every structural entity gets re-written + re-embedded. Use when Pass 3 in a prior run had `Embedding func: Worker execution timeout` on some entities (types committed fine, embeddings stale). Cost on a 1300-topic corpus: ~$0.02 + ~30 min at concurrency=13. | `query.py::index_topics(..., enrich_only=True)`, `query.py::_enrich_structural_types(..., force_rewrite=True)` |
| **visualize** (`discourse_explorer.visualize`) | ✅ Cache-assisted | Reads current `graphrag/graph_chunk_entity_relation.graphml` and rewrites `<data-dir>/visualize/{graph.html,data.js}` from scratch every run. Two build-time caches under `<data-dir>/visualize/cache/` are reused when valid: `layout.json` (spring-layout positions, invalidated by structural sha256) and `rel-clusters.json` (corpus-derived keyword→bucket clustering — see `rel_clusters.py`). Both auto-rebuild on schema or input changes, so re-running after each `--index` is always safe. | `visualize.py::build_visualization`, `rel_clusters.py::load_or_build` |
| **stats** (`discourse_explorer.stats`) | ✅ Stateless | In-memory DuckDB views over `topics/*.json` at each invocation. Always reflects disk state. No sync step ever needed. | `stats.py::_connect` (no persisted DB) |

## Standard incremental workflow

```bash
# 1. Delta scrape — catches both new topics and topics with new replies.
uv run discourse-explorer scrape https://discourse.example.com --output <data-dir>

# 2. Incremental index — no --clear. LightRAG skips unchanged docs via content-hash;
#    new/updated docs flow through Pass 2. Pass 1 + Pass 3 re-assert structure.
uv run discourse-explorer query <data-dir> --index

# 3. Regenerate the visualization.
uv run discourse-explorer visualize <data-dir>

# 4. Stats is query-time and always current; no sync step.
uv run discourse-explorer stats --path <data-dir> <subcmd>
```

Cost and runtime scale with **new content**, not corpus size. On a 1300-topic corpus the fixed overhead of a nothing-changed incremental run is now a few seconds (Pass 3's skip gate turns its former ~30 min re-embed cost into ~1800 fast graph reads). When topics actually change, the cost rises with the number of new/changed docs, not the full corpus.

## The updated-topic gotcha

When an existing topic gets a new reply:

1. Scraper overwrites `topics/<id>.json` in place with the current full thread (no merge — the JSON is replaced whole per CLAUDE.md's invariant: *"scraper is idempotent at the topic level — topic JSON is overwritten whole, not merged"*).
2. The file's content hash changes → LightRAG treats it as a **new document**, adds its extractions to the graph.
3. **The old document's extracted entities and edges remain** in the graph — there's no auto-cleanup.

Consequences:

- **Positive:** Entities shared between old and new versions (category names, users, recurring product concepts) pick up additional descriptions from the new version. Signal compounds.
- **Neutral to mild drift:** Entities that appeared only in the old version but not the new stay as orphans. Their embeddings remain retrievable, so they don't break queries, but they accumulate over time.
- **Self-healing for structural types:** Pass 3's `_enrich_structural_types` re-asserts `category`/`topic`/`tag`/`user` typing on every invocation via `aedit_entity`, which is a direct write bypassing LightRAG's Counter-vote merge. Structural types never drift.

## The stale-embedding failure mode

Pass 3's `aedit_entity` does two things per entity:
1. Writes the target `entity_type` into the graph node's attributes (`chunk_entity_relation_graph.upsert_node`).
2. Re-embeds the entity (name + description) and writes the vector into the Faiss `entities_vdb`.

Step 1 happens synchronously in-process; step 2 makes an OpenAI embedding API call with a 60 s worker timeout. **Step 2 can time out independently of step 1**: the graph write commits, then the re-embed raises `TimeoutError: Embedding func: Worker execution timeout after 60 s`, and the entity is recorded as "enrichment: skip" in stderr. Observed in our full run on 222 of 1828 structural entities (~12% failure rate) during an OpenAI slow patch.

**Consequences for the graph:**
- `entity_type` is correct — you can still filter, traverse structural edges, etc.
- The Faiss vector for those entities reflects the *pre-Pass-3* embedding, which usually differs only in a few tokens of description text (name + type mention). Semantic retrieval still works but ranks those entities slightly less relevantly than a fresh embedding would.

**How to detect and recover:**
- The `Pass 3 complete: <N>/<total>` line at the end of an indexing run tells you how many enrichments succeeded. If N < total, check stderr for `Enrichment: skip '<name>' (TimeoutError: ...)` lines — those are the stale ones.
- Recovery: `uv run discourse-explorer query <data-dir> --index --enrich-only`. Force-rewrites every structural entity; the second attempt usually succeeds (OpenAI latency is rarely persistent). On our corpus this took ~30 min at concurrency=13 for 1828 entities and costs pennies.

**Why `--enrich-only` exists separately from plain `--index`:** plain `--index` with the skip gate sees "type matches target → skip" and leaves the stale embedding alone. `--enrich-only` deliberately passes `force_rewrite=True` so every structural entity gets re-embedded regardless of the current stored type. See `query.py::_enrich_structural_types(..., force_rewrite)` for the branching.

## Performance tuning for incremental runs

Concurrency and persistence knobs matter on 1000+ topic corpora. Defaults in `query.py` target correctness; tune for speed after verifying the graph is sound.

| Knob | Env var | CLI | Default |
|---|---|---|---|
| LightRAG `llm_model_max_async` | `LLM_MODEL_MAX_ASYNC` | `--llm-concurrency N` | `8` (OpenAI) / `1` (Ollama) |
| LightRAG `max_parallel_insert` | `MAX_PARALLEL_INSERT` | `--parallel-insert N` | `4` |
| `PERSIST_EVERY` batching (Pass 2; Pass 1 suppresses flushes entirely) | — (`query.py` module constant) | — | `200` |
| Pass 3 `index_done_callback` suppression | — (unconditional) | — | Suppressed during Pass 3, single flush at phase end. See `_suppress_index_done` in `query.py`. |

Probe your OpenAI tier with `uv run discourse-explorer query <data-dir> --detect-limits` — prints the RPM/TPM ceiling and a recommended `llm_model_max_async` / `max_parallel_insert` pair. Tier 3 accounts (RPM ≥ 5000) can typically run `13 / 3`; Tier 1–2 should keep defaults.

## When to do a full `--clear` rebuild instead

Incremental indexing is the correct default, but these changes require a destructive rebuild of `graphrag/`:

| Trigger | Why |
|---|---|
| Vocabulary changed (`entity_types.json` edited, or `/discover-entity-types` produced a new list) | Pass 2 extraction constraints baked into prior entities won't apply retroactively. |
| Embedding model or dimension changed | Faiss index is bound to a fixed dimension; silent mismatch on read without `--clear`. (Also auto-invalidates `<data-dir>/visualize/cache/rel-clusters.json` — see below.) |
| Gleaning level changed | Affects recall; prior extractions used the old level. |
| Orphan accumulation is hurting queries | After many topic edits, stale entities can dilute retrieval. Rebuild cost: ~$6 / ~5h on a 1300-topic corpus (our measured numbers). |

## Visualize cache refresh

The `visualize/cache/` files are independent of `graphrag/`. They auto-invalidate on the conditions below; manual triggers are rarely needed:

| Cache | Auto-invalidates when… | Manual trigger |
|---|---|---|
| `cache/layout.json` | sha256 of (node names + edge tuples) differs — i.e. nodes/edges added or removed. Attribute-only changes (Pass 3 type rewrites) reuse positions. | `rm cache/layout.json` (re-runs the ~6 min spring_layout). |
| `cache/rel-clusters.json` | schema bump, llm/token mode flip, OpenAI embedding model or dim change, or `--cache-k` raised above the cached value. | `--regenerate-keyword-clusters` to rebuild from scratch. The runtime `Edge categorization coverage: X%` line falling below 75% is the signal that the cached vocabulary has drifted; the warning text recommends the same flag. |

Cost on `--regenerate-keyword-clusters`: ~$0.003-$0.005 in llm-cluster mode (re-embeds ~10k unique keywords + one batched LLM relabel call), free in token-cluster mode. Cheaper than a `--clear` graph rebuild by three orders of magnitude.

Changing `--max-rel-types` between runs does *not* invalidate the cache — the cached centroids (llm mode) or token counts (token mode) are reused; only the per-N derivation runs (one ~$0.0004 LLM call in llm mode, free in token mode). Already-seen N values are pure cache hits. [`MANUAL.md` §4](../MANUAL.md#4-graph-visualization) covers the user-facing flags.

## Related files

- `CLAUDE.md` — `query.py` architecture paragraph describes the three-pass ingest structure (Pass 1 custom_kg, Pass 2 LLM extraction, Pass 3 enrichment); `visualize.py` + `rel_clusters.py` paragraphs describe the visualize cache mechanics.
- [`docs/MANUAL.md`](../MANUAL.md) §4 — user-facing visualize cache management (file sizes, refresh commands, invalidation matrix).
- `docs/lightrag/LIGHTRAG_KNOWHOW.md` #18–#19 — full investigation into the name-collision problem that Pass 3 solves, and why `aedit_entity` is the right fix.
- [`docs/workflows/INDEX_AND_EMBED.md`](../workflows/INDEX_AND_EMBED.md) — the runbook for both modes; acceptance thresholds for the `--full` validation sample.
- `.claude/skills/index-and-embed/SKILL.md` — the entry point for the incremental workflow described above. `--resume` is its default mode; `--full` is the exception it gates behind explicit approval.
