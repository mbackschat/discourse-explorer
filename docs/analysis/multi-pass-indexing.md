# Multi-pass indexing in `query.py`

Deep reference for how `query --index` builds the GraphRAG knowledge graph, why it has four passes, and which of them are load-bearing.

Companion docs:
- `docs/lightrag/LIGHTRAG_KNOWHOW.md` §18–§19 — collision/enrichment investigation that motivated Pass 3.
- `docs/lightrag/LIGHTRAG_KNOWHOW.md` §20 — name-axis exact-string keying that motivated Pass 4.
- `docs/analysis/entity-name-canonicalization.md` — Pass 4 design + observed numbers + the two bugs caught during first canonical-corpus run.

## Overview

Each topic JSON is fed through four passes in order:

| Pass | Function | Purpose | LLM cost |
|---|---|---|---|
| 1 | `_topic_to_custom_kg(topic)` → `rag.ainsert_custom_kg(...)` | Seed deterministic structural entities + edges (User / Topic / Category / Tag) from the JSON fields | none |
| 2 | `rag.ainsert(documents)` | LLM extraction over post bodies → content-level entities + free-form relation keywords | dominant cost |
| 3 | `_enrich_structural_types` → `rag.aedit_entity` | Re-assert structural `entity_type` on every Pass 1 node; repairs Pass 2's Counter-vote overwrites | small (one re-embed per structural entity) |
| 4 | `_canonicalize_case_dupes` + `_canonicalize_user_paraphrases` → `rag.amerge_entities` | Collapse case-fold dupes (`jdoe` / `Jdoe` / `JDoe`) and `^User ` / ` Person$` paraphrases of Pass-1 user seeds; bulk-refresh the resulting Faiss VDBs | small (one batched re-embed per merged target + every rerouted edge) |

Graph is stored under `<data-dir>/graphrag/`. Vector store is `FaissVectorDBStorage` (binary `faiss_index_<namespace>.index` + `.meta.json` sidecar per collection).

## Pass 1: structural custom-KG seed

`_topic_to_custom_kg(topic)` produces a `{chunks, entities, relationships}` payload with:

- **Entities**: `category` (from `category_name`), `topic` (from `title`), `tag` (each tag), `user` (each unique poster). All entity_types are **lowercase** to match LightRAG's Pass 2 auto-lowering so the Counter merge at `_merge_nodes_then_upsert` can compare like-for-like. PascalCase would never vote equal.
- **Relationships** with canonical keywords from `config.STRUCTURAL_REL_KEYWORDS`:
  - `topic_in_category` — `"posted in, category, section"`
  - `topic_tagged` — `"tagged, tag, labeled"`
  - `user_posted` — `"posted, authored, participated"`
- **Chunks**: the full topic document, pre-split via `_split_for_embedding` to stay under the OpenAI embedding API's 8192-token cap. First chunk keeps the canonical `tid = f"topic-{topic['id']}"`; overflow chunks suffix as `tid-p1`, `tid-p2`, … so their `source_id`s stay unique.

Each topic's Pass 1 insert is wrapped in a **3-attempt retry with exponential backoff** (`2^attempt` seconds). A transient timeout doesn't abort the run — the topic is logged as failed and the loop moves on.

Pass 1 is idempotent by entity name. Re-running it on an existing graph produces no net change beyond incrementing merge counts.

**Idempotent is not the same as convergent.** Re-seeding merges the *new* payload in; it never removes what the topic used to point at. A topic whose tag was renamed or whose post was deleted therefore leaves the old node behind, still asserting `Topic tagged with <old>` about a topic that no longer says so. A renamed tag or a departed poster therefore strands its old node, and the graph has no way to tell it apart from a live one.

So the ledger records *which documents* a topic produced, and a changed topic has them deleted before the new payload is seeded:

| ledger entry | `_pass1_plan` | effect |
|---|---|---|
| absent / malformed | `INSERT` | seed; nothing to replace |
| hash matches | `SKIP` | no work, no writes |
| hash differs | `RESEED` | purge the recorded documents, then seed |

Both id families are recorded, because both accrete: Pass 1's own chunk documents (`topic-<id>`, `topic-<id>-pN`, since `ainsert_custom_kg` defaults `full_doc_id` to each chunk's `source_id`) and the `doc-<md5>` id Pass 2's `ainsert` will mint for the same topic. The Pass 2 id has to be captured here, at seed time: once the topic changes, the old text is gone and its hash can no longer be recomputed from anything on disk.

`adelete_by_doc_id` reaches only Pass 2's work. It resolves a document to graph elements through `full_entities` / `full_relations`, and `ainsert_custom_kg` registers nothing there — after an index run those stores hold only `doc-` keys, so a `topic-<id>` purge deletes the chunk, the document and the doc_status row while touching the graph not at all. Pass 1's own nodes must therefore be retracted explicitly, which is why the ledger also records the relation pairs:

- delete the relations the new payload no longer contains (topic-scoped, so exact)
- then drop an endpoint that is structural **and** either has no edges left or cites only chunks that have been deleted

The second condition is what a renamed tag needs. Retracting `Topic tagged with <old>` leaves the old tag node holding content edges Pass 2 had extracted from the previous text; those cite the deleted chunk, so the node is stale by construction even though its degree is not zero. A node reachable from surviving chunks is kept, which is how a shared entity survives one of its topics changing.

`adelete_by_doc_id` costs **two all-storage flushes per call**, the most expensive helper in the API, and the retraction adds one flush per CRUD call on top, so all of it runs inside Pass 1's existing suppression. `tests/test_pass1_doc_purge.py::PurgeWriteBatchingTests` pins that: 25 documents cost one write per file inside the context and 50 without it.

Entries written before the `docs` field exist carry only a hash. They still skip correctly, so the migration is free on unchanged topics; a changed topic under one of them purges the derivable `topic-<id>` and logs that the rest could not be recovered.

## Pass 2: LLM extraction

`rag.ainsert(documents)` runs LightRAG's standard extraction pipeline — entity + relation extraction per chunk, optional gleaning re-passes, merge with existing nodes.

Key configuration:

- `addon_params["entity_types"]` receives only `content_type_names(vocab)` — the content types from `<data-dir>/config/entity_types.json`. Structural types are **omitted from the allowed list** so Pass 2 can't *intentionally* produce them.
- `chunk_token_size=1200` (LightRAG default; Pass 2 re-chunks its own input, so the 8192-cap issue only applies to Pass 1's custom-KG chunks).
- `entity_extract_max_gleaning` from `rc.gleaning` (default `1` = one "what did you miss?" re-extraction pass).
- `FORCE_LLM_SUMMARY_ON_MERGE` — resolved by `bootstrap()` into `RuntimeConfig.force_llm_summary_on_merge` and passed explicitly to the LightRAG constructor. Project default is `999` (`config.SUMMARY_ON_MERGE_DEFAULT`), which skips the per-entity summary cascade for a 3–5× speedup; LightRAG's own default of `8` fires an extra LLM call per entity once 8 descriptions accumulate. It is passed explicitly rather than left to the ambient environment so that a hand-typed CLI run and `scripts/index.sh` cost the same.

**File-path provenance**: `rag.ainsert(documents, file_paths=[...])` carries `topic-<id>.json` through extraction so every entity's `source_id` traces back to its topic. Without this, entities would get `unknown_source`.

Pass 2 is the expensive step. Budget ~$6 and ~2.5–3 hours for a 1300-topic corpus on `gpt-4.1-mini` + `gleaning=1` + Tier-3 concurrency.

## Pass 3: structural enrichment

The problem Pass 3 solves: LightRAG's `_merge_nodes_then_upsert` resolves entity-type collisions via a Counter vote, with ties broken in favor of the *incoming* batch. Pass 2 extracts content entities whose names happen to match Pass 1's structural names (category names and topic titles appear verbatim in post prose). Pass 2 wins the vote and silently re-types our `category` nodes to `component`, `document`, or whatever Pass 2's LLM decided. Without intervention, ~50% of categories and sporadic topics get mis-typed (observed on our 50-topic validation sample).

Fix: `_enrich_structural_types` walks every structural (name, type) pair from the topic set, deduplicates across topics (a category like "Data Services" appears in many topics but only needs one re-assertion), and calls `rag.aedit_entity(name, updated_data={"entity_type": type}, allow_rename=False)`. `aedit_entity` is a **direct attribute write** that bypasses the Counter-vote merge, so Pass 3 survives any future Pass 2 re-extractions.

### Pass 3 mechanics

- **Concurrency**: `asyncio.gather` under a semaphore of `llm_model_max_async`. `aedit_entity` internally re-embeds the entity (name + description), and that embedding call competes for the same OpenAI rate-limit budget as Pass 2's extraction calls.
- **Skip-if-already-correct gate**: before calling `aedit_entity`, do a cheap `rag.chunk_entity_relation_graph.get_node(name)` read. If the stored type already matches the target, skip the expensive re-embed. Cuts the steady-state "nothing changed" Pass 3 cost from ~30 minutes to ~1 second on a clean 1800-entity graph.
- **`_suppress_index_done` context manager**: during Pass 3, swap each storage's `index_done_callback` to an async no-op. Without this, every `aedit_entity` call flushes the ~20 MB graphml under a storage lock, serializing the concurrent gather pool. One batched flush at the end of the phase recovers the full concurrency benefit.
- **Verification**: after Pass 3, read the GraphML, count entity types, and flag missing topics. Warns if topic count in graph < topic count in source.

### `--enrich-only` recovery mode

Use when a prior run completed structurally but some `aedit_entity` calls hit `TimeoutError: Embedding func: Worker execution timeout after 60 s`. The *type* write commits synchronously before the embedding call — so type is correct but the Faiss embedding is stale.

```
uv run discourse-explorer query <data-dir> --index --enrich-only
```

Skips Pass 1 + Pass 2, runs only Pass 3 with `force_rewrite=True` (bypasses the skip-if-already-correct gate so every entity gets re-embedded regardless of stored type). Cost: ~$0.02 + a few minutes at tier-appropriate concurrency. Incompatible with `--clear`.

## Disk persistence batching

`PERSIST_EVERY = 200` (module-level in `query.py`) is a monkey-patch around `rag._insert_done` that only flushes every 200th call instead of every call. It matters because `FaissVectorDBStorage.index_done_callback` has no dirty guard — unlike `JsonKVStorage`, which checks `storage_updated` before writing — so every flush unconditionally rewrites all three Faiss index files (~498MB on a 1400-topic corpus). Flush count is therefore a direct multiplier on bytes written to disk.

Pass 1 suppresses this path entirely (`_flush_state["suppressed"] = True`) and checkpoints on its own clock — the topic index, every `PASS1_CHECKPOINT_EVERY` topics, via `_checkpoint_pass1`. It also flushes at the phase boundary from a `finally`, because the per-topic handler catches only `Exception` (not `KeyboardInterrupt`/`CancelledError`).

**Pass 1's checkpoint must run off the topic index, not off the `_insert_done` counter.** An earlier version drove the flush from that counter while saving `pass1_payload_hashes.json` on the topic index. The counter only advances when a topic is genuinely inserted, so a resume that skipped 1,314 of 1,399 topics never reached the interval: the ledger saved five times and the graph flushed *zero* times. A kill there made the next resume skip those topics permanently. `_checkpoint_pass1` now writes the ledger strictly as a consequence of a verified flush, and short-circuits entirely when nothing was inserted since the last checkpoint — which is also what keeps a mostly-skipped resume from rewriting ~500MB of Faiss for nothing.

Flushes are ordered by `_flush_ledger_last`: every other storage first, then `doc_status`. LightRAG's own `_insert_done` gathers all storages concurrently in no order, so a crash mid-flush could otherwise leave `doc_status` marking documents PROCESSED whose entities were never written — and a resume silently *skips* those documents.

**Ordering alone is not sufficient, and was a no-op until Pass 2 also deferred the ledger.** `JsonDocStatusStorage.upsert` ends with its own `await self.index_done_callback()` (`kg/json_doc_status_impl.py:222`), and LightRAG marks a document PROCESSED (`lightrag.py:2162`) *before* calling `_insert_done` (`lightrag.py:2185`). So `kv_store_doc_status.json` was already durable by the time `_flush_ledger_last` ran, and writing it "last" changed nothing. Pass 2 now wraps its `ainsert` in `_defer_ledger_flush`, which swaps that per-upsert callback for a no-op and hands the real one to `_flush_ledger_last` — making the ordered flush the only writer, which is what actually delivers the guarantee.

Does **not** cover `aedit_entity`'s direct `_persist_graph_updates` calls — Pass 3 handles its own batching via `_suppress_index_done`.

200 trades crash granularity for bytes written. A Pass 2 crash re-does at most ~199 documents of extraction, and it re-does rather than skips them: resume is driven by `doc_status`, and a ledger that lags behind the graph causes duplicate work, never a silent gap.

**That re-work is not free, and an earlier version of this doc wrongly said it was.** `llm_response_cache` is a `JsonKVStorage` in the same `_insert_done` set, so it is batched on exactly the same interval. The direct `llm_response_cache.index_done_callback()` calls in `lightrag.py` are on the cancellation and error paths, and `JsonKVStorage.finalize` only runs on a graceful exit — so a SIGKILL discards the completions for that same window along with their `doc_status`. Re-work after a *clean* stop is cache-served and nearly free; re-work after a kill re-bills. Raising 50 → 200 quadrupled that exposure to roughly $0.85 on the reference corpus. The trade is still worth it against ~498MB of Faiss rewrite per flush, but it is a trade, not a free lunch.

Two things make the lag direction safe rather than lossy. `_flush_ledger_last` writes `doc_status` only after every other storage has reported success, so the ledger can trail the graph but never lead it. And when a storage write fails, it raises `LedgerFlushError` instead of advancing the ledger — necessary because `NetworkXStorage` and `FaissVectorDBStorage` catch their own write errors and merely return `False`, which LightRAG's `_insert_done` discards.

**`LedgerFlushError` is loud at the Pass 3/4 call sites and quiet inside Pass 2.** During Pass 2 it is raised from `rag._insert_done`, which LightRAG calls inside a `try` whose `except Exception` (`lightrag.py:2195`) marks *that one document* FAILED and continues. So a genuine graphml or Faiss write failure mid-Pass-2 yields a logged traceback, one successfully-extracted document mislabelled FAILED, and a run that keeps going — rather than the halt the exception implies. The direction is still safe (FAILED → PENDING → reprocessed on the next run), but do not read "it raises" as "it stops the run" for Pass 2. The `_flush_storages` calls in Passes 3 and 4 are outside any such handler and do propagate to the top.

## Embedding cap workaround

OpenAI embedding APIs cap input at **8192 tokens**. LightRAG's normal `ainsert` chunks documents at `chunk_token_size=1200`, so Pass 2 is unaffected. The cap only matters for Pass 1's custom-KG chunks, which we author ourselves from `topic_to_document`.

`_split_for_embedding(text, max_tokens=8000)` in `query.py` pre-chunks oversize topic content before it hits `ainsert_custom_kg`. Uses `tiktoken` with the `cl100k_base` encoding (matches `text-embedding-3-*`). The 8000 floor leaves a safety buffer for tokenizer-vs-server drift.

## Vector-store format: Faiss vs NanoVectorDB

Switched to `FaissVectorDBStorage` in April 2026. Per-collection on-disk footprint:

| Backend | 3072d × 16k entities | Notes |
|---|---|---|
| NanoVectorDB (JSON text) | ~500 MB | Historical default |
| Faiss (binary float32 + meta sidecar) | ~120–200 MB | 3–5× smaller on disk |

`text-embedding-3-small` (1536d) roughly halves whichever backend. Binary indices compress less dramatically than the old JSONs — tar+zstd still wins, but the starting point is already small.

**Switching storage backends requires `--index --clear`** — indices aren't portable between NanoVectorDB and Faiss formats.

## Pass 4: entity-name canonicalization

The problem Pass 4 solves: LightRAG keys nodes by the entity-name *string verbatim* — `jdoe` / `Jdoe` / `JDoe` become three distinct nodes that Pass 3 cannot merge (different keys, different stored attributes, no Counter vote ever fires). Typical scale on a 1.3K-topic corpus: ~640 case-collision groups → ~710 redundant nodes (~4.3%).

Pass 4 runs two sub-passes against the existing graph (no re-index needed):

- **Pass 4a** (`_canonicalize_case_dupes`) — buckets every node by `nid.casefold()`. For each bucket with ≥2 variants, picks a canonical (Pass-1 seed wins regardless of case, else lowercase variant, else alphabetical first) and calls `rag.amerge_entities(source_entities=others, target_entity=canonical, target_entity_data={"entity_type": canonical_type})`. The explicit `target_entity_data` locks the canonical's type so the LightRAG default `keep_first` strategy can't promote a source variant's type.
- **Pass 4b** (`_canonicalize_user_paraphrases`) — for each `user`-typed Pass-1 seed, finds nodes whose `_strip_user_paraphrase_affixes(name).casefold()` matches the seed's casefolded name, and merges them in. Strips `^User ` / ` Person$` and collapses inner whitespace. Conditional: only fires when the stripped form lands on a known user seed (so `User Story` → `Story` is *not* triggered when no `story` user seed exists).

Background, rationale, and comparison against upstream PR #2102: **[`entity-name-canonicalization.md`](entity-name-canonicalization.md)**.

### The deferred-VDB-writes optimization

Naive sequential `await rag.amerge_entities(...)` per bucket on the canonical 1.3K-topic corpus took ~7h, dominated by per-merge OpenAI embedding calls (every merge re-embeds the merged target + every rerouted edge — ~3000-6000 total OpenAI roundtrips). The 4× speedup from suppressing the per-edit `index_done_callback` (`_suppress_index_done`, same trick Pass 3 uses) cut it to ~100min, but Faiss `_remove_faiss_ids` O(index_size × removed_count) per merge still dominated for hub buckets.

`_defer_pass4_writes(rag)` context manager replaces the four VDB methods (`entities_vdb.upsert`/`.delete`, `relationships_vdb.upsert`/`.delete`) with in-memory buffers for the duration of the merge phase. Per-merge work becomes pure NetworkX graph mutations (~50ms each instead of ~9.5s). All buffered VDB writes are then flushed by `_apply_pass4_writes(...)` in deletes-first-upserts-second order — single bulk Faiss `_remove_faiss_ids` call frees all stale IDs at once, then bulk upsert re-embeds via Faiss's internal batching (~10 contents per OpenAI call by default).

**Conflict handling in the buffers:** a buffered upsert later deleted gets popped from the upsert buffer; a buffered delete later upserted gets discarded from the delete buffer. LightRAG produces both patterns naturally — e.g. an edge rerouted then later folded.

**`embedding_func.func` is intentionally NOT replaced.** An earlier version stubbed it to zero-vectors as a "safety net," but doing so corrupted LightRAG's lazily-initialized embedding worker pool — the pool initialized against the stale stub during apply-phase startup and the first real OpenAI batch hung past the 60s worker timeout. Since both VDB upserts are buffered, no embedding call is ever reached during the merge phase, so the stub is unnecessary.

### Pass 4 mechanics

- **Storage suppression**: `_suppress_index_done` covers the same five storages Pass 3 covers. The buffered VDBs never flush per-merge anyway since they don't call through to disk; one explicit `_flush_storages` at the end commits everything.
- **Canonical-pick correctness**: `_pick_canonical_for_case_bucket` returns *any* member of the bucket that's in `pass1_seed_names`, regardless of case. Without this, the LLM's title-cased Pass-2 variant (e.g., `"How To Use X"`, type `issue`) sorts alphabetically before the Pass-1 topic seed (`"How to use X"`, type `topic`) and wins the canonical role — silently dropping topic-typed nodes from the graph.
- **Apply-phase order**: deletes before upserts. Frees Faiss IDs before re-adding so the upsert isn't tripping over stale vectors.

### `--canonicalize-only` recovery mode

Use when a prior run completed structurally but Pass 4 hasn't been run, or when re-running Pass 4 against an already-clean graph (it's idempotent — produces 0 merges).

```
uv run discourse-explorer query <data-dir> --index --canonicalize-only
```

Skips Passes 1-3, runs only Pass 4. Cost: zero LLM during the merge phase, ~150s wall clock for a 1.3K-topic corpus (dominated by the bulk edge re-embed in the apply phase, ~870 OpenAI batched calls). Incompatible with `--clear` and with `--enrich-only`.

### Observed numbers (canonical 1.3K-topic corpus)

| metric | pre-Pass-4 | post-Pass-4 |
|---|---|---|
| total nodes | 16,502 | 15,756 (-746) |
| case-collision groups | 639 | 0 |
| total edges | 24,715 | 24,587 |
| topic-typed nodes | 1,331 | 1,331 (preserved) |
| Pass 4a merges | — | 639 case-fold buckets |
| Pass 4b merges | — | 34 user-paraphrase buckets |
| buffered: ent-upserts / rel-upserts | — | 639 / 8,683 |
| buffered: ent-deletes / rel-deletes | — | 746 / 12,717 |
| wall clock | — | **151 s** |

## Non-obvious invariants

- **Pass 3 is the contract that keeps Pass 1's typing alive.** Don't remove Pass 3 without replacing it with an equivalent guarantee (another direct-write mechanism that bypasses the Counter vote).
- **Pass 4 must run AFTER Pass 3.** Pass 4 reads `entity_type` from the live graph to decide which nodes are Pass-1 user seeds (for the paraphrase rule) and which structural types to preserve in `target_entity_data`. If Pass 3 hasn't restored types yet, Pass 4 sees Pass-2's Counter-vote overwrites and may pick wrong canonicals.
- **Pass 4 buffers VDB writes; do NOT also stub `embedding_func.func`.** The buffered upserts never call the embedding func during the merge phase, so the stub is redundant — and worse, stubbing corrupts LightRAG's lazily-initialized worker pool, causing the apply phase to hang on the first real embedding call past the 60s worker timeout.
- **Structural entity types emitted as lowercase** (`category`/`topic`/`tag`/`user`) to match LightRAG's Pass 2 normalization. The JSON vocabulary keeps PascalCase for human-readable display; `load_entity_types` validates the exact PascalCase names are present with `structural: true`.
- **Switching embedding model or dimension requires `--clear`.** Vectors are stored at a fixed dim in `graphrag/faiss_index_*.index`. Applies to OpenAI↔Ollama switches, model swaps within-provider (`-small` ↔ `-large`), and backend swaps (NanoVectorDB ↔ Faiss).
- **LightRAG caches LLM responses on disk inside `graphrag/`.** Deleting only the graphml leaves stale caches — always use `--clear` for a true rebuild.
- **Entity types tight, relation keywords loose.** Types are constrained via `addon_params` (tight classification vocabulary). Keywords stay free-form because they feed global-mode retrieval via embedding similarity; constraining them would degrade query quality. Only structural edges we author ourselves get canonical keywords (`config.STRUCTURAL_REL_KEYWORDS`, derived from `STRUCTURAL_REL_PINS`).
