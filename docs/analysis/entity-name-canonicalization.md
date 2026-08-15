# Entity-name canonicalization (Pass 4)

How `query --index` collapses case-fold dupes (`jdoe` / `Jdoe` / `JDoe`) and `^User ` / ` Person$` paraphrases of Pass-1 user seeds without re-indexing — what the code does, why it's structured this way, and the two bugs it exposed on a first canonical-corpus run.

> Names in the examples below (`jdoe`, `acme9`, `XYZ`, …) are illustrative substitutes — see the live corpus data dir for real values.

Companion docs:
- `multi-pass-indexing.md` — the four-pass overview (Pass 4 sub-sections summarize what's here).
- `docs/lightrag/LIGHTRAG_KNOWHOW.md` §18-§20 — LightRAG merge contract: §18-§19 cover the type axis (which Pass 3 fixes), §20 covers the name axis (which Pass 4 fixes).
- `docs/ideas/entity-resolution-llm-judge.md` — Pass 5 / opt-in LLM-judge for the *semantic* dupe long tail Pass 4 can't reach.

## Why Pass 4 exists

LightRAG keys graph nodes by the entity-name **string verbatim** — there is no case fold, no whitespace normalization, no alias resolution at the name axis. Entity *types* get `.lower()` (`operate.py:441`); entity *names* never do.

Both ingestion paths upsert nodes by raw name:

- **Pass 1** (`_topic_to_custom_kg` in `query.py:338`) reads `post["username"]` from the Discourse JSON and writes it as the entity_name verbatim. `LightRAG.ainsert_custom_kg` (`lightrag.py:2449`) calls `chunk_entity_relation_graph.upsert_node(entity_name, ...)` with no normalization.
- **Pass 2** (`_handle_single_entity_extraction` in `lightrag/operate.py:386-459`) routes each LLM-emitted name through `sanitize_and_normalize_extracted_text(...)` which handles HTML / Chinese punctuation / full-width chars / surrounding quotes — but explicitly not casing.

So when a chunk says `"@Jdoe said: ..."` the extractor coins `Jdoe`; when another chunk says `by JDoe` it coins `JDoe`; and our Pass-1 seed already wrote `jdoe`. All three become independent nodes — `_merge_nodes_then_upsert` only fires on exact name matches.

Pass 3 (`_enrich_structural_types`) restores the *type* axis on Pass-1 seeds via `aedit_entity` direct writes — but its scope is type-only. The name axis is untouched until Pass 4.

### What a real corpus looks like (canonical 1.3K-topic corpus)

Pre-Pass-4 casefold-bucket diagnostic over `graph_chunk_entity_relation.graphml`:

| metric | value |
|---|---|
| total nodes | 16,502 |
| case-collision groups (≥2 variants folding to the same lowercase) | **639** |
| extra nodes lost to case duplication | **709 (~4.3%)** |

Worst offenders span both users and product tokens — illustrative shape:

```
[6] ACME JIRA, ACME Jira, AcMe JIRA, Acme JIRA, Acme Jira, acme JIRA
[5] ACME9, AcmE9, Acme9, acmE9, acme9
[4] XYZ, XyZ, Xyz, xyz
[4] ACME, AcMe, Acme, acme
[3] jdoe, Jdoe, JDoe
... and 624 more groups
```

### Two distinct failure modes Pass 4 catches

**A. Pure case dupes** (`jdoe` / `Jdoe` / `JDoe`) — the dominant pattern. Both Pass 1 and Pass 2 contribute: Pass 1 writes the lowercase Discourse `username`; Pass 2 picks up however the chunk text spelled the name. Pass 4a handles these via `casefold()` bucketing.

**B. LLM-coined paraphrases** (`User jdoe`, `Jdoe Person`, `J Doe`) — the extractor invented a new surface form. Pass 4b handles these conditionally: only strips `^User ` / ` Person$` and collapses inner whitespace when the result lands on a known Pass-1 user seed (so `User Story` → `Story` is *not* triggered when no `story` user seed exists).

For the `jdoe` example specifically, Pass 4 collapsed a 7-variant cluster:

| id | type | source | variant kind | merged into |
|---|---|---|---|---|
| `jdoe` | `user` | `custom_kg` | Pass-1 seed | **canonical** |
| `Jdoe` | `other` | many topics | Pass-2 case dupe | jdoe (Pass 4a) |
| `JDoe` | `other` | a few chunks | Pass-2 case dupe | jdoe (Pass 4a) |
| `J Doe` | `other` | one topic | Pass-2 paraphrase (whitespace) | jdoe (Pass 4b) |
| `User jdoe` | `other` | one topic | Pass-2 paraphrase (prefix) | jdoe (Pass 4b) |
| `User Jdoe` | `other` | one topic | Pass-2 paraphrase (prefix) | jdoe (Pass 4b) |
| `Jdoe Person` | `other` | one topic | Pass-2 paraphrase (suffix) | jdoe (Pass 4b) |

## How Pass 4 works

```python
# query.py — call site inside index_topics, after Pass 3 completes
_pass4_stores = _pass3_storages(rag)
with _suppress_index_done(_pass4_stores), \
        _defer_pass4_writes(rag) as (ent_ups, rel_ups, ent_dels, rel_dels):
    merged_case = await _canonicalize_case_dupes(rag, pass1_seed_names)
    merged_para = await _canonicalize_user_paraphrases(rag, user_seed_names)
counts = await _apply_pass4_writes(rag, ent_ups, rel_ups, ent_dels, rel_dels)
await _flush_storages(_pass4_stores)
```

Five pieces:

1. **`_pick_canonical_for_case_bucket(variants, pass1_seed_names) -> str`** — picks the canonical for a case-fold bucket. Priority: Pass-1 seed in bucket (any case) > fully-lowercase variant > alphabetical first.

2. **`_strip_user_paraphrase_affixes(name) -> str`** — pure string transform; strips `^User ` / ` Person$` and collapses inner whitespace.

3. **`_canonicalize_case_dupes(rag, pass1_seed_names) -> int`** — iterates all graph nodes, buckets by `casefold()`, calls `rag.amerge_entities(...)` for each bucket with ≥2 variants. Locks the canonical's type via `target_entity_data={"entity_type": canonical_type}`.

4. **`_canonicalize_user_paraphrases(rag, user_seed_names) -> int`** — for each `user`-typed Pass-1 seed, finds nodes whose stripped+casefolded form matches the seed and merges them in.

5. **`_defer_pass4_writes(rag)` + `_apply_pass4_writes(rag, ...)`** — buffer all VDB writes during the merge phase, then flush in bulk. See next section.

### The deferred-VDB-writes optimization

Naive sequential `await rag.amerge_entities(...)` per bucket on the canonical 1.3K-topic corpus measured **~7h wall clock**, dominated by per-merge OpenAI embedding calls (every `amerge_entities` re-embeds the merged target entity AND every rerouted edge — ~3000-6000 OpenAI roundtrips total).

Adding `_suppress_index_done` (the same trick Pass 3 uses to avoid per-edit graphml flushes serializing on the storage lock) brought it down to **~100min**. Per-bucket time dropped from ~40s to ~9.5s, but Faiss `_remove_faiss_ids` O(index_size × removed_count) per merge still dominated for hub buckets (a hub user with 50 incident edges triggers a 50-entry `relationships_vdb.upsert`, which removes 50 IDs from the 24K-edge Faiss index — ~1.2M operations per merge).

`_defer_pass4_writes(rag)` replaces the four VDB methods (`entities_vdb.upsert`/`.delete`, `relationships_vdb.upsert`/`.delete`) with in-memory buffers for the duration of the merge phase. Per-merge work becomes pure NetworkX graph mutations (~50ms each instead of ~9.5s). Final wall clock: **~151s** for the canonical corpus.

The buffered writes flush in `_apply_pass4_writes(rag, ...)` after the merge phase exits the context. Order: deletes first (frees Faiss IDs in one bulk `_remove_faiss_ids` call), then upserts (Faiss internal-batches embeddings at `_max_batch_size=10` per OpenAI call). For the canonical corpus that's:

- 746 entity deletes — single bulk call
- 12,717 edge deletes — single bulk call
- 639 entity upserts — ~64 OpenAI batched calls
- 8,683 edge upserts — ~870 OpenAI batched calls (the new tail bottleneck)

**Conflict handling** in the buffers: a buffered upsert later deleted gets popped from the upsert buffer; a buffered delete later upserted gets discarded from the delete buffer. LightRAG produces both patterns naturally — e.g. an edge rerouted then later folded together when both endpoints were source-side variants.

### Why `embedding_func.func` is intentionally NOT replaced

An earlier version of `_defer_pass4_writes` ALSO stubbed `rag.embedding_func.func` to return zero-vectors as a "safety net for any callsite we missed." This was a mistake — see the "Bug 2" entry below. The current implementation only buffers the four VDB methods; the embedding func is left alone.

## Bugs caught during first canonical-corpus run

Two fixes landed before the run was clean. Both are codified in `tests/test_canonicalize_name_dupes.py` so they can't regress.

### Bug 1: canonical-pick lost 76 of 1331 topic-typed nodes

**Symptom**: post-Pass-4 verification reported `WARNING: 76 topics missing from graph`. Diagnostic confirmed 0 case-collision groups (Pass 4 *worked*), but the topic count had dropped from 1331 to 1255.

**Cause**: the original `_pick_canonical_for_case_bucket` checked `if v.casefold() == v and v in seeds_lc` — i.e., "this variant is fully lowercase AND its lowercase form is in the seed casefolds." That's correct for users (Discourse `username` is lowercase, so the Pass-1 seed satisfies both checks). But for topics whose seed name is title-cased (`"How to use X"`, the title from JSON), the seed *itself* doesn't satisfy `v.casefold() == v` — it's title-cased. The check fell through to alphabetical-first selection, which picks the LLM's title-cased Pass-2 variant (`"How To Use X"`, type `issue` or `other`) over the Pass-1 topic seed. The merged canonical then carried the LLM variant's name and type, and 76 topic-typed nodes silently became `issue`/`document`/`other`-typed nodes under different name strings.

**Fix**: prefer *any* member of the bucket present in `pass1_seed_names`, regardless of case. Loop unconditionally:

```python
for v in variants:
    if v in pass1_seed_names:
        return v
```

The signature also changed from `seeds_lc: set[str]` (casefolded) to `pass1_seed_names: set[str]` (original case). Test: `PickCanonicalTests::test_pass1_seed_wins_when_titlecase`.

### Bug 2: stubbed embedding func wedged the worker pool, hung apply phase

**Symptom**: after merges completed cleanly, the apply phase printed `re-embedding 639 entities ...`, then `Embedding func: 13 new workers initialized`, then `re-embedding 8683 edges ...`, then `Worker timeout for task ... after 60s`, then nothing. Process exited via the `finally` block without flushing. User saw zero calls hit the OpenAI dashboard.

**Cause**: `_defer_pass4_writes` was monkey-patching `rag.embedding_func.func = _placeholder_embed` "for safety." LightRAG's embedding worker pool initializes lazily on first use. During Pass 4 merges, no embedding call was made (correct — buffered upserts skip Faiss). When the apply phase ran the first real `entities_vdb.upsert(...)`, Faiss spun up 13 new workers — and they bound to the stale stub reference somehow, hanging on the first call past the 60s worker timeout. The synchronous OpenAI test from outside the process worked fine, confirming the key was good; the issue was internal state corruption.

**Fix**: don't replace `embedding_func.func`. The buffered VDB upserts never reach Faiss during the merge phase, so the stub was always redundant. Removing it eliminates the hang. Test: `DeferAndApplyTests::test_defer_does_not_touch_embedding_func`.

## What Pass 4 does NOT catch (out of scope)

Semantic dupes that case-folding can't reach:

- `XYZ` ↔ `Cross-System Data Model` (acronym ↔ expansion)
- `Acme Jira` ↔ `acme-jira-instance` ↔ `Jira at Acme` (display name ↔ instance handle ↔ natural language)
- `Foo Client` ↔ `foo_client` (display name ↔ identifier)
- `Order Service v36.x` ⊂ `Order Service` (versioned ↔ unversioned, *subset* not equivalence — must not merge)

These need the LLM-as-judge approach. Roadmap: **[`docs/ideas/entity-resolution-llm-judge.md`](../ideas/entity-resolution-llm-judge.md)**.

## Why this isn't a LightRAG upstream PR

Upstream Issue [#1323](https://github.com/HKUDS/LightRAG/issues/1323) tracks the same problem with a more ambitious scope (LLM-judge with embedding blocking — see PR [#2102](https://github.com/HKUDS/LightRAG/pull/2102)). #2102 is open against `main` as of `v1.4.15` (released 2026-04-19) and not merged. Our Pass 4 is a project-local stopgap that handles the deterministic subset cheaply — and which would still be valuable as a pre-filter even if PR #2102 lands (every group Pass 4 collapses is one fewer LLM-judge call).

## Verification recipe

```python
import xml.etree.ElementTree as ET
from collections import defaultdict
ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
root = ET.parse("<data-dir>/graphrag/graph_chunk_entity_relation.graphml").getroot()
buckets = defaultdict(list)
for n in root.findall(".//g:node", ns):
    buckets[n.get("id", "").casefold()].append(n.get("id"))
multi = {k: v for k, v in buckets.items() if len(v) > 1}
print(f"groups={len(multi)}  extra_nodes={sum(len(v)-1 for v in multi.values())}")
```

Run on a Pass-4-clean graph: expect `groups=0  extra_nodes=0`.

## Re-running Pass 4

`--canonicalize-only` skips Passes 1-3, runs only Pass 4 against the existing graph:

```
uv run discourse-explorer query <data-dir> --index --canonicalize-only
```

Idempotent — re-running on a clean graph produces 0 merges and finishes in seconds. **Destructive** in the sense that `amerge_entities` rewrites the graph + VDBs in place; back up `<data-dir>/graphrag/` before first use on a corpus you can't easily rebuild.
