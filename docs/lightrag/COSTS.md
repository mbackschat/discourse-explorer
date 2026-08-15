# LightRAG Cost Analysis: Discourse Forum Indexing

> **Scenario:** Medium-sized Discourse forum, ~10,000 topics × ~20 messages per topic, indexed with OpenAI for both LLM and embedding work, on a local macOS machine.
> **Pricing date:** April 2026. Pricing changes regularly — re-verify against the OpenAI pricing page before committing to numbers.
> **Companion to:** `LIGHTRAG_KNOWHOW.md`. This doc focuses on cost; that doc focuses on configuration.

This document explains where LightRAG spends money (LLM calls and embedding calls), recommends models, and works out the actual cost numbers for a forum the size described above.

## When LightRAG calls LLMs

Three call sites, in order of total volume:

**Per chunk during indexing (mandatory) — entity/relation extraction.** One LLM call per chunk. Input: ~1,500 tokens of system prompt + few-shot examples (cacheable, identical across all chunks) + ~1,200 tokens of chunk content. Output: ~3,000 tokens of structured `entity<|#|>...` / `relation<|#|>...` records. This is the dominant cost line in the entire pipeline.

**Per chunk during indexing (default-on, optional) — gleaning.** One additional LLM call per chunk that re-sends the system prompt + the prior extraction as conversation history + a "what did you miss?" prompt. Adds ~50% to the per-chunk LLM cost. Controlled by `entity_extract_max_gleaning` (default `1`). Setting to `0` saves cost but measurably reduces recall on dense chunks.

**Per popular entity/relation during indexing (intermittent) — description summarization.** When the same entity is extracted from many chunks, descriptions accumulate. Past a threshold, a separate LLM call summarizes them into one coherent description. Sparse for most entities, frequent for popular ones (active users, common categories). Long tail; total impact ~5-10% of indexing cost.

**Per query (mandatory, twice) — keyword extraction + answer synthesis.** Keyword extraction is cheap (small prompt, small JSON output). Answer synthesis is the expensive one — receives the assembled context block of entities + relations + chunks, often 10-20k input tokens, emits 500-1,500 output tokens.

## When LightRAG calls embeddings

Three call sites, all input-only (embeddings produce vectors, no output tokens):

**Per chunk during indexing.** Raw chunk text → vector. One call per chunk, ~1,200 tokens each.

**Per entity during indexing.** `entity_name + " " + description` → vector. Re-computed whenever the merged description changes (so popular entities get re-embedded several times across the indexing run). ~60 tokens per call.

**Per relation during indexing.** `keywords + " " + description + " " + src + " " + tgt` → vector. Same re-embedding pattern as entities. ~60 tokens per call.

**Per query.** The user query, low-level keywords, and high-level keywords get embedded. LightRAG batches these into a single API call when possible (`operate.py:3637-3653`). Trivial cost (~$0.00005 per query).

## Recommended models (verified April 2026)

| Role | Model | Why |
|---|---|---|
| Embedding | `text-embedding-3-large` @ 3072 dims | Best OpenAI embedding; embedding cost is negligible regardless of choice |
| Indexing LLM | `gpt-5.2` | Permanent quality lock-in justifies the strongest non-reasoning model; cost is one-time |
| Query LLM | `gpt-5.2` (matched) or `gpt-4o-mini` (cost-optimized) | Free choice — graph quality is unaffected by query-model choice |
| Reranker | `jina-reranker-v2-base-multilingual` | Cheap and high-quality; meaningfully improves `mix` mode |

Avoid for the indexing LLM: any o-series reasoning model (`o1`, `o3`, `o4-mini`) or `-thinking`-suffixed variants. They burn output tokens on internal chain-of-thought, produce malformed extraction output, and are slower and more expensive.

## Cost analysis

### Corpus sizing assumptions

For 10,000 topics × ~20 messages, English forum-typical text:

- Average message: ~200 tokens (mix of short replies + longer detailed posts; the ratio matters more than the absolute)
- Per topic: 20 × 200 = 4,000 content tokens + ~200 metadata/header tokens ≈ 4,200 tokens
- Total corpus: ~42M tokens
- Chunks (1200/100 stride defaults): ~4 chunks per topic average → **~40,000 chunks total**

**Sensitivity:** forums with shorter replies trend ~25,000 chunks; forums with detailed long-form posts can hit 60,000+. Treat the numbers below as ±50%.

### Embeddings (text-embedding-3-large @ $0.13 / 1M tokens)

| Item | Tokens | Cost |
|---|---|---|
| Chunk embeddings (40k × 1,200) | 48M | $6.24 |
| Entity embeddings (~200k events × 60 tokens, accounting for re-embedding on merges) | 12M | $1.56 |
| Relation embeddings (~400k events × 60 tokens) | 24M | $3.12 |
| **Total embedding cost (one-time)** | **~84M** | **~$11** |

Per-query embedding cost is negligible. **Embedding is essentially free** — a few dollars for any reasonable forum. This is why "use the best embedding model regardless of cost" is the right call.

### LLM indexing (the dominant cost)

Per chunk: 1 extraction call (~2,800 input tokens, ~3,000 output tokens) + 1 gleaning call (~6,500 input tokens — large because of conversation history, ~1,500 output tokens). With OpenAI's automatic prompt caching (kicks in for prefixes ≥1024 tokens — applies to system prompt + few-shot examples, ~1,500 tokens), the input cost drops 10x for the cached portion after the first call.

| Model | Per-chunk cost | 40,000 chunks | + summary calls | **Total range** |
|---|---|---|---|---|
| `gpt-5.2` ($1.75 in / $14 out, $0.175 cached in) | ~$0.075 | $3,000 | +$120 | **$2,500–3,500** |
| `gpt-4o` ($2.50 in / $10 out) | ~$0.065 | $2,600 | +$80 | **$2,000–3,000** |
| `gpt-4o-mini` ($0.15 in / $0.60 out) | ~$0.004 | $160 | +$10 | **$150–250** |

Notes on the spread:

- **Output tokens dominate cost.** Extraction emits a lot of structured records. This is why `gpt-5.2` isn't dramatically more expensive than `gpt-4o` for indexing — the input-token discount on `gpt-5.2` partially offsets its higher output rate.
- **Prompt caching is automatic** but coverage depends on identical prefixes. LightRAG's design helps (system prompt is fixed, few-shot examples are fixed) but per-chunk content varies. Effective cache hit rate is ~40-50% of total input tokens.
- The +$0-200 for summary calls is highly variable: a forum with a few extremely active users (whose entity descriptions trigger many summarizations) lands at the high end.

### LLM querying (per question)

| Model | Per-query cost | 100 queries | 1,000 queries | 10,000 queries |
|---|---|---|---|---|
| `gpt-5.2` | ~$0.04 | $4 | $40 | $400 |
| `gpt-4o` | ~$0.05 | $5 | $50 | $500 |
| `gpt-4o-mini` | ~$0.005 | $0.50 | $5 | $50 |

`gpt-4o` is slightly more expensive than `gpt-5.2` per query because the output-rate difference dominates over input savings — a small inversion of the typical price hierarchy. `gpt-4o-mini` is ~10x cheaper than `gpt-5.2` but produces noticeably weaker syntheses.

### Rerank (per query, optional)

Jina: ~$0.02 per 1,000 queries. Cohere: ~$1 per 1,000 queries. Negligible either way.

## Recommended setup and total expected cost

For the stated scenario (cost not a constraint at indexing for a medium-sized forum):

**Configuration:** `gpt-5.2` for indexing, `text-embedding-3-large` at full 3072 dims, `jina-reranker`, `gpt-5.2` (matched) for queries.

**Expected costs:**

| Phase | Cost |
|---|---|
| **One-time indexing** | **~$3,000 ± $500** (mostly LLM extraction) |
| **One-time embedding** | **~$11** (essentially free) |
| **Per query** | **~$0.04** (LLM synthesis dominates) |
| **Per 1,000 queries** | **~$40** (synthesis) + **~$0.02** (rerank) |

The graph is permanent. Re-index only when you change `chunk_token_size`, `entity_types`, or the indexing LLM model.

The LLM cache lives in `kv_store_llm_response_cache.json` and is keyed **on the prompt text alone** — `lightrag/utils.py::generate_cache_key` builds `mode:cache_type:md5(prompt)` with **no model component**. So anything that changes the prompt (chunk size, `entity_types`, `language`, a LightRAG prompt-template edit) produces a clean miss, but switching the extraction *model* does **not**: the old model's completions would be served to the new one silently. `query.py` guards this with a `cache_provenance.json` sidecar recording the extraction model, and discards the cache whenever it doesn't match — an unlabelled cache is treated as untrusted. Embeddings never enter this cache, so changing `OPENAI_EMBED_MODEL` cannot poison it.

Because the cache is model-deterministic in practice, `--index --clear` **preserves** it (and only it) through the wipe when provenance matches, so a rebuild re-reads completions it already paid for instead of re-billing. A truncated or unparseable cache is dropped rather than carried across, since LightRAG reads it without catching `JSONDecodeError`.

Not covered by the provenance guard, and worth knowing: pointing `OPENAI_API_BASE` at a different vendor serving the same model name, a floating model alias whose snapshot rotates server-side, and query-mode entries in the same file (the sidecar records only the extraction model).

## Cost-reduction levers (when you need them)

In order of best-effort-to-savings ratio:

**1. Downgrade the query model.** If per-query volume gets high (say 10,000+ queries/day → ~$400/day with `gpt-5.2`), switch the query model to `gpt-4o-mini` via `param.model_func` on `QueryParam`. Drops to ~$50/day. Retrieval quality is unaffected (it's driven by embeddings + extracted graph, both still produced by the strong indexing model). Synthesis quality drops noticeably but is often acceptable.

**2. Disable gleaning.** Set `entity_extract_max_gleaning=0`. Cuts indexing cost ~40%. Reduces entity-extraction recall by ~10-20% on dense chunks. Acceptable trade-off if your forum has clean, well-structured posts; bad trade-off if posts are dense and information-rich.

**3. Use the OpenAI Batch API.** 50% off both input and output tokens for non-real-time work. The full indexing pass is asynchronous and tolerant of 24-hour turnaround, so this is a clean fit. Halves indexing cost. Requires more code (batch job submission, polling, retrieval) and is not built into LightRAG — you'd need to implement it as a custom `llm_model_func` that batches requests, submits, and waits. Worth it for very large corpora.

**4. Downgrade the indexing model.** Switch to `gpt-4o` for indexing. Saves ~25% (~$700) at meaningful but contained quality loss. Switching to `gpt-4o-mini` saves ~95% (~$2,800) but the entity-extraction quality drops noticeably — more entity-name fragmentation, weaker descriptions, more extraction errors that violate the structured-record format and get dropped. **Bad trade-off for a medium-sized forum.** You'd be trading $2,800 against permanent graph quality you'll query against for months.

**5. Skip embeddings on entities/relations.** Not actually a lever in current LightRAG (the three vector indices are mandatory architecture), but worth noting as an upper bound: even if you could skip them, savings would be ~$5. Embeddings just aren't where the money goes.

## Sanity-check checklist before paying for indexing

Run through this before kicking off a $3,000 indexing job:

1. **Verify the entity-type vocabulary is set** via `addon_params={"entity_types": [...]}`. Without this, the LLM uses the default 11-type vocabulary and you get a graph dominated by under-typed `Concept` and `Other` nodes — and you'll want to re-index, doubling your cost.
2. **Verify the embedding wrapper is correct** (per `LIGHTRAG_KNOWHOW.md` §8 gotcha #4). Wrong wrapping → indexing fails partway through, but only after burning some calls.
3. **Verify `enable_llm_cache=True`** (default — but worth checking). Without it, every retry pays full cost.
4. **Test on a 100-topic subset first.** Will reveal extraction quality issues, embedding-dim mismatches, prompt-formatting bugs at ~$30 instead of ~$3,000. Use a temporary `working_dir`, then delete and re-run on the full corpus.
5. **Make sure `working_dir` is durable.** If it's in `/tmp`, you'll lose the entire indexed graph on reboot and pay again. Put it somewhere stable.

## What this analysis ignores

- **Failed runs and retries.** OpenAI's tenacity-backed retry logic in `lightrag/llm/openai.py:728-735` is automatic but does occasionally fail past max attempts on sustained rate limits. Failed extractions don't get re-tried automatically by LightRAG itself — you may need to re-run on affected chunks. Budget +5% for this.
- **Cost from changes to `chunk_token_size` mid-project.** Changing this setting invalidates the LLM cache (every chunk hash changes), so you pay full LLM cost again. Decide on chunk size up front.
- **Cost from changes to the entity-extraction prompt.** Same problem — prompt change → cache miss → full re-pay. Don't tune prompts on a 40,000-chunk corpus; tune on a small subset first.
- **Cost from changing the indexing LLM after the fact.** If you decide to re-extract with a smarter model later, you pay the full extraction cost again. Choose the strongest model you're willing to commit to up front.
- **Cost of LLM thinking/reasoning tokens** if you accidentally configure a reasoning model (`o1`, `o3`, etc.). These charge for internal reasoning output in addition to visible output. Can multiply costs 3-10x silently. Don't do it.

## Re-verification

This document was generated against:
- LightRAG `1.4.16`
- OpenAI pricing as of April 2026 (`gpt-5.2`: $1.75 in / $14 out / $0.175 cached in; `gpt-4o`: $2.50 in / $10 out; `gpt-4o-mini`: $0.15 in / $0.60 out; `text-embedding-3-large`: $0.13 / 1M)

When pricing or LightRAG version changes, re-verify the per-token rates and recompute. The structural breakdown (which call sites cost what) is stable across LightRAG minor versions; the absolute numbers are not.
