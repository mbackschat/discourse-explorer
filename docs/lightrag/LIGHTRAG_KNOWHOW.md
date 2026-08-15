# LIGHTRAG_KNOWHOW.md

> **Reader:** Coding agents working on the LightRAG integration in this repository.
> **Project:** Programmatic ingestion of a Discourse forum into a LightRAG knowledge graph on a local macOS machine, queried via OpenAI's API. Cost is not a constraint for embeddings.
> **LightRAG version this targets:** `lightrag-hku==1.4.16` (verify with `python -c "import lightrag; print(lightrag.__version__)"`). API surface has shifted across releases; pin the version.
> **Doc generated:** April 2026, with model recommendations verified against current OpenAI lineup at that date.

This document is a working reference, not a tutorial. It assumes you understand RAG and embeddings at a conceptual level. It exists to (a) hand you the verified-correct setup for *this* project, (b) flag the gotchas the LightRAG docs and most blog posts skip, and (c) point you at exact code locations when you need to verify behavior rather than trust prose.

---

## 1. Mental model: what LightRAG is doing

LightRAG = vector RAG + a knowledge graph extracted from chunks at index time + a dual-keyword query mechanism that lets you retrieve from either the entity layer or the relation layer of that graph.

**Indexing pipeline per document:** chunk by tokens → for each chunk, LLM extracts entities and relations using a structured-text format (`<|#|>` delimiters, NOT JSON) → optional gleaning pass to catch missed entities → merge entities/relations across chunks by name (with LLM-driven summarization when descriptions accumulate too much) → write to graph storage and to three separate vector indices (entities, relations, chunks).

**Query pipeline:** LLM splits the user question into `low_level_keywords` (specific entities) and `high_level_keywords` (themes) → batched embedding of all relevant texts → mode-dependent retrieval:

| Mode | Uses | Driven by |
|---|---|---|
| `naive` | chunks VDB only | query embedding |
| `local` | entity VDB → graph traversal → source chunks | low-level keywords |
| `global` | relation VDB → endpoint entities → source chunks | high-level keywords |
| `hybrid` | local ∪ global | both keyword sets |
| `mix` | hybrid + chunk VDB | both + query embedding |

→ token truncation per budget → chunk merging across the three retrieval paths → optional rerank → final LLM call with entities + relations + chunks formatted into a structured prompt → answer with `[n]` citations.

**Why the dual keyword split is the linchpin:** entity embeddings capture specifics (proper nouns, jargon); relation embeddings capture themes (because relations are extracted with a `keywords` field describing the *nature* of the connection). Splitting the query by abstraction level lets each side hit the index it actually matches. If keyword extraction fails or returns junk, retrieval quality collapses regardless of how good the graph is. Treat the keyword extraction prompt as critical infrastructure.

**Recommended default mode for this project:** `mix` with reranker enabled. `mix` is the only mode that combines graph-driven and pure-vector retrieval, and forum content is mixed (named entities like users/tools alongside narrative prose), so you want both surfaces.

---

## 2. The verified-correct OpenAI configuration for this project

### Embedding: `text-embedding-3-large` at full 3072 dimensions

OpenAI offers two current embedding models: `text-embedding-3-small` (1536 dims, $0.02/1M tokens) and `text-embedding-3-large` (3072 dims, $0.13/1M tokens). For a project where embedding cost is not a constraint, use `text-embedding-3-large` at full dimensionality. It's the highest-quality general-purpose embedding model OpenAI offers and is documented as the default for "RAG systems and enterprise-grade AI applications."

Do *not* use Matryoshka dimension reduction (the `dimensions` parameter to shorten 3072 → 1024/256). It exists for storage cost optimization. We don't care about storage cost; full dim gives best retrieval.

**The LightRAG default is `text-embedding-3-small` at 1536 dims.** You must override.

### Chat / extraction: `gpt-5.2` (or current strongest non-reasoning OpenAI model)

LightRAG's own guidance from `CLAUDE.md`: "Minimum 32B parameters recommended. 32KB context minimum (64KB recommended). **Avoid reasoning models during indexing.** Stronger models for query stage than indexing stage."

The "avoid reasoning models" point is critical. Reasoning models (the o-series: o1, o3, o4-mini) burn output tokens on internal chain-of-thought. The entity-extraction prompt does not benefit from reasoning — it benefits from precise instruction-following on a structured-output format. Reasoning models will be slower, more expensive, and no more accurate (often less accurate, because they over-think the format).

As of April 2026, the right choice is **`gpt-5.2`** (OpenAI's strongest non-reasoning frontier model for general-purpose work). If `gpt-5.2` is unavailable in your OpenAI account, fall back in order: `gpt-5.1` → `gpt-5` → `gpt-4.1` → `gpt-4o`. All are non-reasoning. Avoid: anything with `o1`, `o3`, `o4` in the name; anything `-thinking` suffixed.

> **⚠ Empirical note:** On this project, `gpt-5-mini` exhibited reasoning-mode behavior in practice — per-call latency ~5× higher than gpt-4o-mini's, and an output:input price ratio of 8:1 consistent with thinking-token billing. This turned a ~10h indexing projection into a ~50h actual run. Treat the gpt-5 family as **reasoning-tier until proven otherwise** for each specific variant; do a 20-topic dry-run before committing to a full `--index`. Current project default for indexing is `gpt-4.1-mini` (non-reasoning, newer than gpt-4o, better tuple-format discipline than gpt-4o-mini).

### Indexing model and query model are independent decisions

**Critical clarification:** the model used at indexing time does NOT constrain the model used at query time. They consume and produce different things:

- **Indexing model** reads chunk text and writes structured records (entities, relations) to the graph. Its output is plain text stored in the KG. Done once per chunk, permanent unless you re-index.
- **Query model** reads the user question (for keyword extraction) and the retrieved context (for answer synthesis). It never sees the indexing model's internal state — only the structured records it produced, which any model can consume.

There is no compatibility coupling. A graph built with `gpt-5.2` can be queried with `gpt-4o`, `gpt-4o-mini`, Claude, or a local Llama with no technical constraint. The only effect of using a weaker query model is a weaker final answer — not because of incompatibility, but because the weaker model is weaker at the synthesis task.

This means the two model choices are decoupled and you optimize them separately:

| Stage | Frequency | Quality lock-in | Optimization rule |
|---|---|---|---|
| **Indexing** | Once per chunk, ever | Permanent — extraction errors get baked into the graph | Use the best you can afford. The cost is paid once. |
| **Querying** | Once per user question | Transient — bad answers can be re-asked | Match cost to the per-query value of the answer. |

### Recommendation for this project

You said cost is not an issue at embedding time and is also acceptable at indexing time for a medium-sized Discourse forum. That settles indexing trivially: **use `gpt-5.2` for indexing.** The graph quality is locked in permanently, and the ~$50-300 one-time cost for a typical forum is not a concern.

For querying you have a free choice with no downstream consequences:

- **Match (`gpt-5.2` for both)** — best end-to-end quality. Per-query cost on the order of cents. The right default unless you have a high query volume.
- **Downgrade (`gpt-4o` or `gpt-4o-mini` for queries)** — ~10x cheaper per query. Noticeably weaker on synthesis (longer answers feel more generic, citation choices are weaker), but retrieval quality is unaffected because retrieval is driven by embeddings + extracted graph, both of which were produced by the strong indexing model.
- **Upgrade for queries only** — not applicable here since `gpt-5.2` is already the strongest non-reasoning option.

Per LightRAG's `CLAUDE.md`: "Stronger models for query stage than indexing stage." That advice exists for cost-constrained scenarios where the indexing pass is the expensive bulk job and you'd cut costs there first. **It does not apply when cost isn't a constraint at indexing.** You're inverting the typical asymmetry: prioritize indexing quality (because it's permanent), be flexible on query quality (because it's per-question and reversible).

Implementation pattern: keep the indexing model as the global default (`llm_model_func` in the `LightRAG` constructor), and optionally override per-query via `QueryParam.model_func`. See §3 for the code and §8 gotcha #16 for caveats.

### Reranker: Jina or Cohere

LightRAG ships bindings for `jina_rerank` and `cohere_rerank` (`lightrag/rerank.py:368, 435`). Either works. Jina's default model is `jina-reranker-v2-base-multilingual`. Both require a separate API key. If you don't configure a reranker, `mix` mode still works but loses meaningful quality on the chunk-merging step. **Configure a reranker.** It's the highest-leverage quality improvement after the model choices.

---

## 3. Canonical setup code

This is the verified-correct way to instantiate LightRAG for this project. Copy and adapt; do not improvise on the wrapping pattern — the gotchas below are real.

```python
import asyncio
import os
from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc
from lightrag.rerank import jina_rerank  # or cohere_rerank
from functools import partial

# Embedding model configuration
OPENAI_EMBED_MODEL = "text-embedding-3-large"
OPENAI_EMBED_DIM = 3072  # full dimensionality

# Chat model configuration
# Indexing and querying use independent models. See §2 for the rationale.
INDEXING_LLM_MODEL = "gpt-5.2"   # locked into the graph permanently — use the best
QUERY_LLM_MODEL    = "gpt-5.2"   # free to change per-query; matches indexing here


# ---- LLM wrappers ----
# openai_complete_if_cache takes (model, prompt, ...) but LightRAG calls
# llm_model_func as (prompt, ...). Bind the model name into a wrapper.
# Indexing wrapper: used as the global default. Drives entity extraction,
# gleaning, summarization, and the keyword-extraction step at query time.
async def _indexing_llm_func(prompt, system_prompt=None, history_messages=None, **kwargs):
    return await openai_complete_if_cache(
        INDEXING_LLM_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        **kwargs,
    )

# Query wrapper: used ONLY for the final answer-synthesis step at query time,
# via QueryParam.model_func. If you set QUERY_LLM_MODEL == INDEXING_LLM_MODEL
# you can skip this and omit the override; included here for the override pattern.
async def _query_llm_func(prompt, system_prompt=None, history_messages=None, **kwargs):
    return await openai_complete_if_cache(
        QUERY_LLM_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        **kwargs,
    )


# ---- Embedding wrapper ----
# CRITICAL: openai_embed is already decorated with @wrap_embedding_func_with_attrs
# (defaulting to text-embedding-3-small @ 1536 dims). To override the model and
# dimension, wrap the underlying .func — NOT the decorated outer function —
# inside an EmbeddingFunc with the correct dim. See gotcha #4 below.
embedding_func = EmbeddingFunc(
    embedding_dim=OPENAI_EMBED_DIM,
    max_token_size=8191,  # text-embedding-3-large input limit
    model_name=OPENAI_EMBED_MODEL,  # used for VDB collection-name suffixing
    func=partial(openai_embed.func, model=OPENAI_EMBED_MODEL),
)


# ---- Reranker wrapper (optional but strongly recommended) ----
async def _rerank_func(query: str, documents: list[dict], top_n: int | None = None, **kwargs):
    return await jina_rerank(
        query=query,
        documents=documents,
        top_n=top_n,
        model="jina-reranker-v2-base-multilingual",
        api_key=os.environ["JINA_API_KEY"],
        **kwargs,
    )


# ---- LightRAG instance ----
async def make_rag(working_dir: str, entity_types: list[str]) -> LightRAG:
    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=_indexing_llm_func,        # global default = indexing model
        llm_model_name=INDEXING_LLM_MODEL,        # cosmetic; for logging
        embedding_func=embedding_func,
        rerank_model_func=_rerank_func,

        # Entity-type vocabulary (per Discourse recommendations in §6)
        addon_params={
            "language": "English",
            "entity_types": entity_types,
        },

        # Concurrency tuning for OpenAI tier with Macbook (see §7)
        llm_model_max_async=8,           # concurrent LLM calls (default 4)
        embedding_func_max_async=16,     # concurrent embedding calls (default 8)
        embedding_batch_num=64,          # texts per embedding batch (default 10)
        max_parallel_insert=4,           # concurrent doc inserts (default 2, max recommended 10)

        # Caching — keep enabled. Critical for cost & retry-friendliness.
        enable_llm_cache=True,
        enable_llm_cache_for_entity_extract=True,
    )
    await rag.initialize_storages()  # ← REQUIRED. See gotcha #1.
    return rag


async def main():
    rag = await make_rag(working_dir="./rag_storage", entity_types=DISCOURSE_ENTITY_TYPES)
    try:
        # Indexing uses the global llm_model_func (= INDEXING_LLM_MODEL).
        await rag.ainsert(documents)

        # Querying: the global default also handles the keyword-extraction LLM
        # call. To use a different model for the FINAL answer-synthesis call only,
        # pass model_func on QueryParam. Comment it out to use the global default.
        result = await rag.aquery(
            "your question",
            param=QueryParam(
                mode="mix",
                enable_rerank=True,
                model_func=_query_llm_func,   # ← omit this to reuse INDEXING_LLM_MODEL
            ),
        )
        print(result.content)
    finally:
        await rag.finalize_storages()  # ← REQUIRED for clean shutdown.


if __name__ == "__main__":
    asyncio.run(main())
```

Storage choice for macOS local: **leave the storage backends at default.** They are JSON for KV, NanoVectorDB (in-memory + JSON file) for vectors, and NetworkX (in-memory + GraphML file) for graph. No Docker, no Postgres, no Neo4j needed. Files live in `working_dir`. This works fine for a single Discourse forum (typical: thousands of topics, low millions of chunks at most). Only consider Postgres or Neo4j if (a) the graph exceeds ~50k nodes and queries get slow, or (b) you need concurrent multi-process access.

---

## 4. The async lifecycle (most common point of failure)

`LightRAG(...)` constructs the object. **It does not initialize storage backends.** You must call `await rag.initialize_storages()` before any other async method, and `await rag.finalize_storages()` for clean shutdown.

If you skip `initialize_storages`, the first `ainsert` or `aquery` call will throw something like `AttributeError: __aenter__` or `KeyError: 'history_messages'`. The error message does not say "you forgot to initialize." Memorize this signature.

If you skip `finalize_storages`, in-memory storage backends (NanoVectorDB, NetworkX) may not flush their final state to disk. Always wrap usage in try/finally.

There is also a deprecated `auto_manage_storages_states=True` option that will do init/finalize automatically, but the LightRAG codebase comment explicitly marks it deprecated (`lightrag/lightrag.py:518`). Don't use it.

---

## 5. Indexing internals worth knowing

### Default chunking

`chunking_by_token_size` in `operate.py:101`. Defaults: `chunk_token_size=1200`, `chunk_overlap_token_size=100`. Tokenizer is `tiktoken` by default. The chunker uses sliding-window tokenization, not character/sentence boundaries. For Discourse data (post bodies are typically 100-1500 tokens), the default is fine. Don't override unless you have a specific reason.

You *can* configure character-first splitting by passing `split_by_character` to `ainsert`, but for forum data this is usually not what you want — it would split each topic at every newline rather than respecting natural chunk boundaries.

### Entity extraction format

The LLM is asked to output records like:

```
entity<|#|>EntityName<|#|>EntityType<|#|>Description
relation<|#|>SourceEntity<|#|>TargetEntity<|#|>keywords, more keywords<|#|>Description
<|COMPLETE|>
```

`<|#|>` is `PROMPTS["DEFAULT_TUPLE_DELIMITER"]`, `<|COMPLETE|>` is `PROMPTS["DEFAULT_COMPLETION_DELIMITER"]`. This format was chosen over JSON because LLMs handle the line-per-record termination more reliably than nested braces. **Do not modify these delimiters** unless you also override every prompt that references them.

### Gleaning

Default `entity_extract_max_gleaning=1` — one extra LLM pass per chunk after the initial extraction, asking "what did you miss?". Gleaning costs roughly +50% of extraction LLM tokens but improves recall meaningfully. Set to `0` only if cost is a constraint (here it isn't, leave at 1). Do not set higher than 1; diminishing returns and OpenAI conversation history bloats.

### Merge & summarize

When the same entity appears in many chunks, descriptions accumulate. If the count crosses a threshold, LightRAG fires a separate LLM call (the "summary" prompt in `prompt.py`) to compact descriptions into one coherent paragraph. This is per-entity and can produce a long tail of LLM calls for popular entities. Indexing a forum where one user posts in 500 topics will trigger many summary calls for that user. Budget accordingly.

### What gets vectorized

Three separate VDBs get populated for each new/updated entity, relation, or chunk:

| VDB | What's embedded |
|---|---|
| `entities` | `entity_name + " " + description` |
| `relations` | `keywords + " " + description + " " + src + " " + tgt` |
| `chunks` | raw chunk text |

This is why the entity description quality matters for `local` mode retrieval, and why relation keywords matter for `global` mode retrieval. Both are LLM-extracted and quality-bounded by the model.

---

## 6. Discourse-specific guidance

### 6.1 Use a constrained, discourse-fit entity vocabulary

LightRAG defaults to 11 generic types (`Person, Creature, Organization, Location, Event, Concept, Method, Content, Data, Artifact, NaturalObject`). These fit narrative text but not forum content and produce a graph dominated by under-typed `Concept` and `Other` nodes.

**The correct vocabulary has two layers:**

```python
DISCOURSE_ENTITY_TYPES = [
    # Structural — directly from Discourse's own data model. Universal.
    "User", "Topic", "Category", "Tag",
    # Content — domain-specific. Discover from your actual corpus (see §6.4).
    # Example for a software-support forum:
    "Tool", "Issue", "Concept", "Reference",
]
```

Use `User` (Discourse's term), not `Person`. The LLM sees "user" in source text far more than "person", and it disambiguates from references to third parties mentioned in posts.

Pass via `addon_params={"entity_types": DISCOURSE_ENTITY_TYPES}` (already shown in §3). The vocabulary is interpolated into the extraction prompt as a soft constraint — the LLM is told to pick from this list or use `Other` as fallback. **The parser does not enforce membership** (`operate.py:386`), so a misbehaving LLM can still emit non-vocabulary types. With GPT-5.x this is rare; the prompt is followed reliably.

### 6.2 Use `insert_custom_kg` for structural data; do NOT rely on LLM extraction for it

This is the single biggest quality win for Discourse data and is consistently underused. Each scraped topic JSON already contains `category_name`, `tags`, and `posts[*].username` as explicit, structured fields. **Do not flatten these into prose and ask the LLM to re-extract them.** That route loses categories, drops tags, and fragments usernames.

Instead: build a custom KG payload from the JSON and call `rag.ainsert_custom_kg(payload)` *before* the regular `rag.ainsert(documents)` for content. Both passes share the same graph; entities with matching names merge automatically.

Custom KG payload shape (`lightrag/lightrag.py:2376`):

```python
{
    "chunks": [{"content": "<text>", "source_id": "<id>", "file_path": "<file>"}, ...],
    "entities": [
        {"entity_name": "Acme", "entity_type": "Category",
         "description": "...", "source_id": "<id>"}, ...
    ],
    "relationships": [
        {"src_id": "Topic Title", "tgt_id": "Acme",
         "description": "Topic posted in Acme", "keywords": "posted in, category",
         "weight": 1.0, "source_id": "<id>"}, ...
    ],
}
```

Use canonical `keywords` strings on structural relationships so global-mode queries about "what was posted in category X" reliably retrieve them. Use 2-3 comma-separated terms per edge for embedding richness. Example map:

```python
STRUCTURAL_REL_KEYWORDS = {
    "topic_in_category": "posted in, category, section",
    "topic_tagged":      "tagged, tag, labeled",
    "user_posted":       "posted, authored, participated",
}
```

For LLM-extracted relationships (the content layer), do NOT try to constrain keywords. They drive global-mode retrieval via embedding similarity, and free-form keywords give richer semantic match. There is no `addon_params["relationship_types"]` knob in LightRAG — this is intentional design, not an oversight.

### 6.3 Format topic documents to avoid known fusion bugs

When flattening a topic for the LLM-extraction pass, watch for username-fusion. The naïve format `bob (replying to #1):` causes the LLM to occasionally extract `Bob (Replying To #1)` as an entity name, fragmenting the User node. Fix by separating:

```
bob:
(in reply to post #1)
I'm getting a 500 error...
```

Two-line format, reply pointer on its own line, no semantic loss.

### 6.4 Discover the content-half vocabulary empirically

The structural half (`User`, `Topic`, `Category`, `Tag`) is universal across Discourse. The content half depends on what the forum is about. The standard GraphRAG "discover-then-design" pattern applies: sample 30 topics, run an LLM-driven open-ended extraction asking "what kinds of things are discussed", aggregate the labels, distill into 4-6 content types. Do this once before indexing, paste the result into the `DISCOURSE_ENTITY_TYPES` constant. Do not try to programmatically auto-update this. The discovered list should be reviewed by a human or by the main agent conversation with user confirmation before committing.

### 6.5 Default query mode

`mix` with `enable_rerank=True`. For queries explicitly about structure ("what categories exist", "who posts most about X"), `local` mode also works well because the structural pass produced clean entity nodes. For broad thematic queries ("what are the recurring complaints about feature Y"), `global` and `mix` are stronger.

---

## 7. Concurrency and rate limits on macOS local

The Macbook is the network client. OpenAI does the work. Tune for OpenAI rate limits, not for the Mac's CPU.

| Parameter | Default | Recommended for this project | Reasoning |
|---|---|---|---|
| `llm_model_max_async` | 4 | 8 | OpenAI tier 1+ tolerates this easily for chat |
| `embedding_func_max_async` | 8 | 16 | Embeddings have higher RPM limits than chat |
| `embedding_batch_num` | 10 | 64 | OpenAI accepts up to 2048 texts per batch; 64 is a sweet spot for latency vs round-trip count |
| `max_parallel_insert` | 2 | 4 | Per `CLAUDE.md`, max recommended is 10. 4 balances throughput against rate-limit risk |

If you see `RateLimitError` retries during indexing: lower `llm_model_max_async` first (the bulk of LLM calls happen during extraction). The `openai_embed` and `openai_complete_if_cache` functions have built-in tenacity-based retry with exponential backoff (`lightrag/llm/openai.py:728-735`), so transient rate limits self-recover; only intervene for sustained pressure.

For a typical Discourse forum (~5k topics, ~50k posts):

- Embedding cost (text-embedding-3-large): roughly $5-20 one-time
- LLM extraction cost: dominant cost, model-dependent — expect $50-300 for one full index pass with GPT-5.2
- Re-indexing because you changed config: pays the LLM cost again unless `enable_llm_cache=True` (which it is by default — keep it). The cache is keyed on prompt+model; identical prompts return cached responses and don't re-bill.

**The LLM cache is per `working_dir` and stored in `kv_store_llm_response_cache.json`.** When you change `entity_types` or other prompt-affecting config, the cache becomes stale (new prompts → new hashes → cache miss). When you change `chunk_token_size`, every chunk hash changes and the cache is fully invalidated. Plan re-indexing accordingly.

---

## 8. Critical gotchas

These are the failure modes that the README does not adequately call out. Memorize.

1. **`await rag.initialize_storages()` is required after instantiation.** Skipping it produces cryptic `AttributeError: __aenter__` or `KeyError: 'history_messages'` errors at first use. Always pair with `await rag.finalize_storages()` in a `finally` block.

2. **Embedding model changes require wiping vector storage.** The vector dimensions are fixed at index creation. Switching from `text-embedding-3-small` (1536) to `text-embedding-3-large` (3072) on an existing graph will fail at first vector upsert. Solution: delete `working_dir` and re-index. The `model_name` field on `EmbeddingFunc` is used to suffix VDB collection names (`base.py:_generate_collection_suffix`) precisely so accidental swaps surface as missing-collection errors instead of silent dimension mismatches.

3. **Do not pass `model="text-embedding-3-large"` directly to `openai_embed` and expect 3072 dims.** The `openai_embed` function is decorated with `@wrap_embedding_func_with_attrs(embedding_dim=1536, ...)`. The decorator wraps the function to validate output dimension against 1536. Passing a different model will request 3072 from OpenAI but the wrapper validation will reject it. You MUST override the wrapper by constructing a new `EmbeddingFunc` with `embedding_dim=3072` and either `partial(openai_embed.func, model="text-embedding-3-large")` or your own async function. The pattern in §3 is correct.

4. **Double-wrapping `EmbeddingFunc` is a known footgun.** Code from `lightrag/utils.py:421-470`: if you pass an already-wrapped `openai_embed` (decorated function) as the `func` argument to `EmbeddingFunc`, you get nested wrapping where the inner wrapper's settings override the outer's. The class detects and unwraps up to depth 3, but don't rely on this — always wrap the bare `.func`:
   - ❌ `EmbeddingFunc(embedding_dim=3072, func=openai_embed)` — wraps a wrapper
   - ✅ `EmbeddingFunc(embedding_dim=3072, func=partial(openai_embed.func, model="text-embedding-3-large"))`

5. **Keyword extraction is the single most fragile step at query time.** If the LLM returns malformed JSON or empty keyword lists, retrieval falls back to using the raw query string as a low-level keyword (`operate.py:3230`), which works poorly. GPT-5.x is reliable here; smaller / older models are not. If you observe poor retrieval, log the extracted hl/ll keywords first.

6. **Reasoning models break entity extraction.** Do not use `o1`, `o3`, `o4-mini`, `gpt-5-thinking`, or any other reasoning-tier model for `llm_model_func`. They produce extraction outputs that violate the `<|#|>` line-record format because they emit reasoning preambles. Stick to non-reasoning frontier models.

7. **`entity_type` is normalized to lowercase, no spaces, on parse.** `"NaturalObject"` and `"Natural Object"` both become `"naturalobject"`. Any visualization or downstream code comparing against the configured vocabulary must compare case-and-space-insensitively, OR use the canonical forms throughout.

8. **The merge-time entity-type vote is majority-rules with no tie-breaker.** When a popular entity is extracted with type `Person` once and `Other` twice across chunks, it ends up as `Other` permanently. Sparse-evidence entities with weak typing votes are a real failure mode. Constraining the vocabulary tightly (per §6.1) reduces the variance.

9. **`addon_params` is the right place for `entity_types` and `language`. NOT `llm_model_kwargs`.** Common mistake: passing `entity_types` to `llm_model_kwargs`. The LLM never sees it. The correct path is `addon_params` → interpolated into the system prompt by `extract_entities` (`operate.py:2905`).

10. **Workspace isolation with default storage is by `working_dir` directory.** If you want two separate Discourse forums on the same machine, give each its own `working_dir`. Do not use the `workspace=` parameter as a shortcut — it has different semantics per backend (`base.py` and `kg/`); for file-based storage it creates a subdirectory inside `working_dir`, which is fine but redundant if you're already separating by `working_dir`.

11. **`naive` mode bypasses the graph entirely.** It's just vector search over chunks. Useful as a baseline to compare graph-augmented retrieval against, but not what you want as a default. If a query in `mix` mode is worse than `naive`, that's a strong signal the graph layer is hurting more than helping (usually because of bad entity types).

12. **`only_need_context=True` is the debugging escape hatch.** Set it on `QueryParam` to get the retrieved context back instead of an LLM-generated answer. Use this when answers look wrong — usually retrieval is the problem, and you can see the retrieved entities/relations/chunks directly. `only_need_prompt=True` similarly returns the full prompt without firing the LLM.

13. **Streaming responses don't return the raw_data structure.** If you use `QueryParam(stream=True)`, you get an async iterator of text deltas but lose the structured `raw_data` field that contains entities/relations/citations. For programmatic use, prefer non-streaming.

14. **`include_references=True` is supported but only for some endpoints.** It's a `QueryParam` field that triggers reference list inclusion in the response. Read `lightrag/lightrag.py` aquery flow before depending on it; not all storage/mode combinations populate it.

15. **The `tests/test_*.py` files are integration-heavy.** Don't run the full test suite blindly — many require real database instances (`postgres`, `neo4j`, `qdrant`, etc.). Use `pytest tests` (default) which only runs offline tests. Set `LIGHTRAG_RUN_INTEGRATION=true` only when you have the matching services running.

16. **`QueryParam.model_func` overrides ONLY the final answer-synthesis call, not every LLM call at query time.** A single query fires (at minimum) two LLM calls: keyword extraction + answer synthesis. Setting `param.model_func=X` makes only the synthesis call use `X`; the keyword-extraction call continues to use the global `llm_model_func` from the constructor (`operate.py:3209-3214`). This is usually what you want — keyword extraction is cheap and the global model is fine — but if you need the keyword-extraction step to use a specific model too, you have to override the global `llm_model_func` instead. This asymmetry surprises people who assume `param.model_func` is a complete query-time model swap.

17. **OpenAI embedding APIs cap input at 8192 tokens per call.** All current OpenAI embedding models (`text-embedding-3-small`, `text-embedding-3-large`, `text-embedding-ada-002`) reject inputs exceeding 8192 tokens with `400 Invalid 'input[0]': maximum input length is 8192 tokens`. LightRAG's built-in chunker (`chunk_token_size=1200` default) keeps `ainsert()` calls safely under this. **But if you're calling `ainsert_custom_kg()` with full-document chunks** (as hybrid-ingest setups commonly do), you must pre-chunk anything over ~8000 tokens before passing. LightRAG does not enforce this on the custom-KG path. Mitigation: wrap your chunk content with a tiktoken-based splitter using `cl100k_base` and split at 8000 tokens with a safety buffer. See `discourse_explorer/query.py::_split_for_embedding` for a working pattern.

18. **`ainsert_custom_kg()` and `ainsert()` merge by name with Counter-vote type semantics, not strict last-write-wins.** When the same entity name appears in both a custom-KG payload (Pass 1) and LLM-extracted content (Pass 2), LightRAG merges them: descriptions are *concatenated* (with `<SEP>` separator, good — preserves signal), but `entity_type` is resolved by a majority vote. The exact code (`operate.py::_merge_nodes_then_upsert`, lines 1756-1762):

    ```python
    entity_type = sorted(
        Counter([dp["entity_type"] for dp in nodes_data] + already_entity_types).items(),
        key=lambda x: x[1],
        reverse=True,
    )[0][0]
    ```

    Critical mechanics:
    - `already_entity_types` carries **exactly one vote** from the stored-node's prior type (line 1645+1662), regardless of how many times Pass 1 previously asserted that type across different topics.
    - Python's `sorted()` is stable; the concat order is `[batch] + already`, so on a **tie, batch values come first** and win.
    - Net effect: any single Pass 2 extraction for a shared name produces a 1-vs-1 tie, and Pass 2 wins it. Pass 1's structural typing gets overwritten silently.

    **Case folding makes it worse.** `operate.py::_handle_single_entity_extraction` (line 441) forcibly lowercases every LLM-extracted `entity_type`: `entity_type = entity_type.replace(" ", "").lower()`. If your Pass 1 writes PascalCase (`Category`), it's a different Counter key from Pass 2's `category` — the merge can't even *see* them as the same label. Observed failure mode on a 50-topic validation sample: 6 of 12 categories + 1 of 50 topics retyped; 85.6% in-vocab when checked case-insensitively.

    **There is no knob to flip the merge to "first-write wins" or "prefer custom_kg".** But three remediations exist, with different trade-offs:
    1. **Lowercase Pass 1's `entity_type` strings** so they match Pass 2's auto-lowered labels. Doesn't fix collisions (ties still go to batch), but eliminates the case-mismatch that turns every merge into a miss. Prerequisite for anything else.
    2. **Namespace Pass 1 entity names** so Pass 2's extractions can't collide (`"[Cat] Data Services"` instead of `"Data Services"`). Side effect: creates duplicate nodes — one structural and one content — for the same concept, losing merge-based evidence unification.
    3. **Post-ingest enrichment with `aedit_entity` (recommended).** See note #19. Lets the natural merge run, then force-re-asserts structural types in a third pass. Uses an official CRUD API; no duplicate nodes; aligned with the maintainer's guidance in [Discussion #1077](https://github.com/HKUDS/LightRAG/discussions/1077).

19. **The canonical fix for structural-type preservation is a Pass 3 `aedit_entity` enrichment loop.** `aedit_entity` (in `utils_graph.py:524` / exposed on `LightRAG` as `aedit_entity`) performs a **direct write** of the entity's stored attributes — it bypasses the Counter merge, so its type assignment survives any future Pass 2 extractions in the same run. The pattern, per our `discourse_explorer/query.py::_enrich_structural_types`:

    ```python
    # After Pass 1 (ainsert_custom_kg) and Pass 2 (ainsert) both complete:
    for name, etype in deduplicated_structural_pairs:
        await rag.aedit_entity(
            name,
            updated_data={"entity_type": etype},
            allow_rename=False,
        )
    ```

    Notes and caveats:
    - `aedit_entity` re-embeds the entity in the VDB on every call. On a 1300-topic forum this is ~1800 unique structural entities × ~$0.00001 per embedding ≈ **~$0.02 enrichment cost** (versus $6–8 for the index itself).
    - Deduplicate `(name, entity_type)` across topics before calling — a category like `"Data Services"` appears in many topics but only needs one re-assertion.
    - `aedit_entity` raises if the entity doesn't exist in the graph (e.g. if Pass 1 silently failed to insert it). Catch and log; a partial enrichment is still strictly better than no enrichment.
    - Related CRUD APIs worth knowing about for more elaborate patterns:
      - `acreate_entity(name, entity_data)` — create from scratch (use if you want to skip Pass 1 entirely and author structure post-hoc).
      - `amerge_entities(source_entities, target_entity, merge_strategy={"entity_type": "keep_first"})` — merge multiple nodes with explicit type-priority (useful when the graph has both `"Data Services"` and `"[Cat] Data Services"` and you want to fold them together).
      - All three are documented in `lightrag/lightrag.py:4204–4396` and in the maintainer's [`ProgramingWithCore.md`](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md).

    This project uses the Pass 3 enrichment approach; a 50-topic validation sample exposed the merge collision it solves. See the sample findings log under `<data-dir>/logs/INDEX_AND_EMBED-*.md` for the specific retype evidence that motivated the fix.

20. **Entity *names* are keyed by exact string — no case fold, no whitespace normalization, no alias resolution.** This is the name-axis equivalent of #18. `_handle_single_entity_extraction` (`operate.py:386-459`) routes names through `sanitize_and_normalize_extracted_text(...)` (`utils.py:2114`), which handles HTML / Chinese punctuation / full-width chars / surrounding quotes — but **not casing**. The entity_type one line later gets `.lower()` (`operate.py:441`); the name does not. `ainsert_custom_kg` skips even that helper and calls `upsert_node(entity_name, ...)` raw (`lightrag/lightrag.py:2449`). Net effect: `jdoe`, `Jdoe`, `JDoe`, `User Jdoe` all become independent graph nodes; `_merge_nodes_then_upsert` only fires on exact name matches. On a 1.3K-topic corpus this typically produces ~640 case-collision groups → ~710 redundant nodes (~4.3%).

    Upstream is aware: HKUDS/LightRAG **Issue #1323** ("Automatic merging of the same entity under different names", milestone `v1.4.8`, slipped) and **PR #2102** ("Use LLM to deduplicate extracted similar entities", branch `duplicate_dev`, +1,407 LOC, open against current `main` as of `v1.4.15` released 2026-04-19). PR #2102's approach: per-document insertion-phase batch, embedding-similarity blocking (cosine ≥ 0.85), LLM-as-judge with strict/medium/loose strictness levels.

    Project-local remedies, in cost order:
    1. **Deterministic Pass 4 (shipped).** `casefold()`-bucket the existing graph + strip `^User `/` Person$` for `user`-typed nodes + `rag.amerge_entities(...)` per group, then bulk-refresh the entity + relationship Faiss VDBs. The merge phase buffers all VDB writes via `_defer_pass4_writes` (per-merge work is pure NetworkX graph mutations); the apply phase flushes deletes-then-upserts in two bulk operations per VDB. Wall clock on the canonical 1.3K-topic corpus: **~150 s** (vs naive sequential ~7 h). Catches the case-only and simple-paraphrase subset (~70% of all dupes on a typical corpus). See [`docs/analysis/entity-name-canonicalization.md`](../analysis/entity-name-canonicalization.md) for the full design + the two bugs caught during the first canonical run.
    2. **Post-hoc LLM-judge (planned, parked).** Lift PR #2102's `duplicate.py` core, run as a Pass 5 against the existing graph (no re-index). Catches the semantic residual (`XYZ` ↔ `Cross-System Data Model`, `Acme Jira` ↔ `acme-jira-instance`). Estimated cost ~$1 on a 1.3K-topic corpus. See [`docs/ideas/entity-resolution-llm-judge.md`](../ideas/entity-resolution-llm-judge.md).
    3. **Vendor PR #2102 + re-index.** Aligns with upstream API, but ~$12 cost, ~15-20 h wall clock, ~1,400 LOC fork burden until upstream merges. Same idea doc has the rationale for why this is the *third* choice, not the first.

---

## 9. API surface reference (the calls used most)

```python
# Construction
rag = LightRAG(
    working_dir: str,                               # required
    llm_model_func: Callable,                       # required
    llm_model_name: str = "gpt-4o-mini",           # cosmetic; for logging
    embedding_func: EmbeddingFunc,                  # required
    rerank_model_func: Callable | None = None,     # optional but recommended
    addon_params: dict = {},                        # entity_types, language go here
    chunk_token_size: int = 1200,
    chunk_overlap_token_size: int = 100,
    entity_extract_max_gleaning: int = 1,
    enable_llm_cache: bool = True,
    enable_llm_cache_for_entity_extract: bool = True,
    llm_model_max_async: int = 4,
    embedding_func_max_async: int = 8,
    embedding_batch_num: int = 10,
    max_parallel_insert: int = 2,
    vector_db_storage_cls_kwargs: dict = {},        # e.g. {"cosine_better_than_threshold": 0.2}
)

# Lifecycle (REQUIRED)
await rag.initialize_storages()
await rag.finalize_storages()

# Insertion
await rag.ainsert(text_or_list_of_text, ids=None, file_paths=None)
await rag.ainsert_custom_kg(payload, full_doc_id=None)
await rag.ainsert_custom_chunks(text, chunks_list)  # bypass auto-chunking

# Querying
result = await rag.aquery(query: str, param: QueryParam = ...)

# QueryParam fields most relevant
QueryParam(
    mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass"] = "mix",
    top_k: int = 40,                          # entities or relations to retrieve
    chunk_top_k: int = 20,                    # chunks to retrieve initially
    max_entity_tokens: int = 6000,
    max_relation_tokens: int = 8000,
    max_total_tokens: int = 30000,
    enable_rerank: bool = True,
    only_need_context: bool = False,          # debugging: return retrieved context
    only_need_prompt: bool = False,           # debugging: return full prompt
    response_type: str = "Multiple Paragraphs",
    user_prompt: str | None = None,           # extra instructions for answering LLM
    stream: bool = False,
    conversation_history: list[dict] = [],    # multi-turn
    model_func: Callable | None = None,       # override LLM for this query only
    include_references: bool = False,
)

# Result is a QueryResult dataclass:
result.content              # str — the answer (or context if only_need_context)
result.raw_data             # dict — entities, relationships, chunks, metadata
result.response_iterator    # async iterator if stream=True
result.is_streaming         # bool
```

---

## 10. Pointers into the LightRAG source

When in doubt, verify against the source. The repo is well-organized but big. These are the authoritative entry points:

| Topic | File | Line(s) |
|---|---|---|
| Public API exports | `lightrag/__init__.py` | 1-23 |
| `LightRAG` class definition | `lightrag/lightrag.py` | search `class LightRAG` |
| `QueryParam` definition | `lightrag/base.py` | 84-170 |
| Default constants (chunk size, async limits, entity types) | `lightrag/constants.py` | full file is small |
| Chunking | `lightrag/operate.py` | 101 (`chunking_by_token_size`) |
| Entity extraction orchestrator | `lightrag/operate.py` | 2883 (`extract_entities`) |
| Single-entity parser (validation rules) | `lightrag/operate.py` | 386 (`_handle_single_entity_extraction`) |
| Single-relationship parser | `lightrag/operate.py` | 473 (`_handle_single_relationship_extraction`) |
| Cross-chunk entity merge | `lightrag/operate.py` | 1623 (`_merge_nodes_then_upsert`) |
| Query orchestrator | `lightrag/operate.py` | 3164 (`kg_query`) |
| Keyword extraction | `lightrag/operate.py` | 3406 (`extract_keywords_only`) |
| Per-mode dispatch | `lightrag/operate.py` | 3573 (`_perform_kg_search`) |
| All prompts (system, examples, query) | `lightrag/prompt.py` | full file |
| OpenAI bindings | `lightrag/llm/openai.py` | 671 (`gpt_4o_complete`), 737 (`openai_embed`) |
| Reranker bindings | `lightrag/rerank.py` | 368 (`cohere_rerank`), 435 (`jina_rerank`) |
| Storage abstract base classes | `lightrag/base.py` | 217 (`BaseVectorStorage`), 568 (`upsert_node`) |
| `EmbeddingFunc` class (the wrapper) | `lightrag/utils.py` | 421 |
| `wrap_embedding_func_with_attrs` decorator | `lightrag/utils.py` | 1088 |
| Custom KG insertion | `lightrag/lightrag.py` | 2376 (`ainsert_custom_kg`) |

The maintainers keep a `CLAUDE.md` and `AGENTS.md` at the repo root with their own architecture notes — read those first if you're touching anything substantial.

---

## 11. What this doc deliberately does not cover

- **Other LLM providers** (Ollama, Anthropic, Bedrock, Gemini). Out of scope for this OpenAI-only project. If you need them, see `lightrag/llm/`.
- **Other storage backends** (Postgres, Neo4j, Qdrant, Milvus). Defaults are correct for macOS local use. If the graph grows past ~50k nodes and queries get slow, revisit; otherwise leave alone.
- **The FastAPI server (`lightrag-server`)** and React WebUI in `lightrag/api/` and `lightrag_webui/`. We're using LightRAG as a library, not the bundled service.
- **Multi-tenancy / workspaces.** One Discourse forum, one `working_dir`. If you ever index a second forum, give it its own directory.
- **Document deletion / updates.** LightRAG supports `adelete_by_doc_id` and `aupdate` but they have edge cases (orphan entities if all chunks referencing them are deleted). For this project, full re-index is simpler than incremental update.
- **The `.env` / `config.ini` configuration.** The library reads many env vars, but for programmatic use, pass everything explicitly to the constructor as shown in §3. Implicit config from the environment is the source of subtle bugs.

---

## 12. When to mistrust this document

This doc was generated against `lightrag-hku==1.4.16` in April 2026. Mistrust it when:

- The pinned version differs from what's installed. Check `import lightrag; print(lightrag.__version__)`. APIs have shifted between minor versions in the past (notably around `addon_params` and storage initialization).
- OpenAI's model lineup has changed. `gpt-5.2` was the strongest non-reasoning model at doc-time; later releases will supersede it. The principle "use the strongest non-reasoning frontier model" is durable; the specific model string is not.
- A recommendation here contradicts something in `CLAUDE.md` or `AGENTS.md` at the repo root. Those are maintained by the LightRAG authors and should win.
- You see behavior that contradicts a claim here. Verify against the file:line reference in §10. If the source has changed, this doc is stale — flag it.
