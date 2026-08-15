# Entity resolution via LLM-as-judge (semantic dupes)

Roadmap entry for a Pass 5 / opt-in flag that merges *semantic* entity dupes — the long tail that the deterministic Pass 4 (`docs/analysis/entity-name-canonicalization.md`) cannot reach. Tracks upstream LightRAG work and lays out three implementation paths in cost order.

> Names in the examples below (`jdoe`, `XYZ`, `Acme Jira`, `Order Service`, `Foo Client`, …) are illustrative substitutes — see the live corpus data dir for the real values.

Status: **proposed, not started.** Pass 4 (deterministic) has shipped — see [`docs/analysis/entity-name-canonicalization.md`](../analysis/entity-name-canonicalization.md) for what it does and doesn't catch. This Pass 5 idea kicks in only after we measure the residual semantic-dupe rate post-Pass-4 and decide it's worth spending LLM tokens to close.

## What this would catch

Cases where two entities mean the same real-world thing but their names share no character n-grams that case-fold or affix-strip can normalize:

| variant | canonical | folding rule that fails |
|---|---|---|
| `XYZ` | `Cross-System Data Model` | acronym ↔ expansion |
| `Acme Jira` | `acme-jira-instance` | display name ↔ instance handle |
| `Jira at Acme` | `Acme Jira` | natural language ↔ token order |
| `Foo Client` | `foo_client` | display name ↔ identifier |
| `Order Service v36.x` | `Order Service` (subset relation) | versioned ↔ unversioned (NB: this is *subset*, not equivalence — a judge needs to know not to merge these) |

These need semantic understanding, not string ops. They're the residual after Pass 4. We don't yet have a measured residual rate on the canonical corpus — quantifying it is **step 0** of any work here.

## Upstream landscape

The LightRAG community has converged on the same answer; the wheel is being built upstream.

- **Issue #1323 — "Automatic merging of the same entity under different names"** ([link](https://github.com/HKUDS/LightRAG/issues/1323))
  - Open, 30 comments, milestone `v1.4.8` (slipped — current upstream is `v1.4.15` released 2026-04-19, doesn't include it).
  - Maintainer @danielaskdd's recommended workflow: per-document dedup batch, similarity-threshold candidate retrieval, LLM judge with strict prompt.

- **PR #2102 — "Use LLM to deduplicate extracted similar entities during the insertion phase"** ([link](https://github.com/HKUDS/LightRAG/pull/2102))
  - Open, branch `duplicate_dev`, +1,407 LOC across 5 files (`lightrag/duplicate.py` is 968 new lines). Cited by the maintainer as the reference implementation; held back pending alignment-accuracy + perf evaluation.
  - API shape:
    ```python
    enable_deduplication: bool = False
    deduplication_config = {
        "strategy": "llm_based",
        "llm_based": {
            "batch_size":           DEDUP_BATCH_SIZE,           # default 30
            "similarity_threshold": DEDUP_SIMILARITY_THRESHOLD, # default 0.85 (cosine)
            "strictness_level":     "strict" | "medium" | "loose",
            "system_prompt":        None,                       # use default if None
        },
    }
    ```
  - Strict mode: *"merge ONLY if they represent the exact same real-world concept (e.g., spelling variations, synonyms, or explicit duplicates). Never merge nodes that are merely topically related."* — exactly what we'd want for `XYZ` ↔ `Cross-System Data Model`.
  - **Caveat**: insertion-phase only. Runs per-document. Cross-topic singletons may never co-occur in a dedup batch unless `duplicate.py` also has a global pass (verify before relying on it).

- **Manual GUI by @johnshearing** ([repo](https://github.com/johnshearing/LightRAG), [video](https://youtu.be/70iZxleULYY))
  - In-progress upstream PR for a WebUI tab. Find-by-substring + side-by-side compare + merge button. Already in production use by some community members for messy-corpus cleanup.

- **Available today in `v1.4.13`**: `LightRAG.amerge_entities(source_entities=[...], target_entity=..., merge_strategy={...})` — the merge *primitive* works; only the discovery step is missing upstream.

## Three implementation paths

In cost order, lowest first.

### Path C — post-hoc judge against existing graph (recommended starting point, ~$1)

Lift only the LLM-judge logic + similarity-blocking core from PR #2102's `duplicate.py`; run it as a Pass 5 against the graph already on disk. **No re-index.**

- Embeddings already in `<data-dir>/graphrag/faiss_index_entities.index` — free reuse.
- Candidate generation: for each entity, retrieve top-k similar where cosine ≥ threshold (default 0.85).
- Block by removing case-fold groups already merged by Pass 4 (those are deterministic; no need to spend LLM tokens re-confirming).
- Batch surviving candidate groups into LLM-judge calls (PR #2102's strict-mode prompt verbatim).
- For each YES verdict: `rag.amerge_entities(...)`.

Estimated cost on a typical 1.3K-topic corpus (~1,500 candidate groups post-Pass-4):

| component | tokens | cost @ `gpt-4.1-mini` |
|---|---|---|
| ~300 batched judge calls × ~5K in / ~0.5K out | 1.5M in + 0.15M out | ~$0.84 |
| candidate retrieval (re-uses existing VDB) | 0 | $0 |
| **total** | | **~$1** |

Pros: cheapest, no re-index, no upstream-fork burden, starts paying off whatever residual exists today.
Cons: not aligned with PR #2102's API — when upstream merges, we'd want to retire this and switch.

### Path B — vendor PR #2102 + full re-index (~$12)

Drop `duplicate.py` + the small `operate.py` / `lightrag.py` hooks from PR #2102's branch into our `.venv` (or carry as a patch file). Set `enable_deduplication=True` in `_get_rag(...)`. Run `--index --clear`.

| component | cost |
|---|---|
| base re-index (Pass 1+2+3, same as a recent canonical run) | ~$5.99 |
| dedup-judge bill (~1,330 docs × ~1 batch × ~6.5K in / ~1K out @ `gpt-4.1-mini`) | ~$5.60 |
| **total** | **~$11.50 ± $4** |

Plus ~15-20 h wall clock and the cost of carrying ~1,400 LOC of unmerged upstream code as a local fork.

Pros: aligned with upstream API; when PR #2102 lands we just delete our fork.
Cons: most expensive on every axis; redundant work for the deterministic subset Pass 4 already handles.

### Path A — wait for upstream (~$0 today, indefinite timeline)

Do nothing until PR #2102 merges, then turn the flag on during the next planned re-index. Requires no project-local code.

Pros: zero implementation cost, zero maintenance burden.
Cons: PR #2102 has been open through several v1.4.x releases; "indefinite" is not hyperbole. Meanwhile semantic dupes accumulate as the corpus grows.

## Recommended sequence

1. ~~Ship Pass 4 first~~ — **done**, see [`docs/analysis/entity-name-canonicalization.md`](../analysis/entity-name-canonicalization.md).
2. **Measure** the residual semantic-dupe rate post-Pass-4 on the canonical corpus. If it's < ~50 groups, the long tail probably isn't worth $1 either; close this idea.
3. If residual is meaningful, **try Path C** as a one-shot run with `--strictness=strict`. Inspect the merge log; spot-check 20 random merges; if accuracy is acceptable, accept the merges.
4. If Path C proves valuable enough to want it on every re-index, **revisit Path B** (vendor PR #2102) only at that point — and only if PR #2102 still hasn't merged upstream.

## Decision deferrals

When this work picks up, these are the open questions:

- **Strictness floor.** PR #2102's `strict` mode is well-scoped; `medium` and `loose` carry over-merge risk. Default to `strict` on first run; never enable `loose` without per-merge human review.
- **Judge model.** Default is the extraction model (`gpt-4.1-mini`). For the residual post-Pass-4 (~hundreds of groups, not thousands), upgrading the judge to a more capable model adds ~$3-5 and may be worth it for a once-per-quarter run. Don't assume the cheapest model.
- **Pass-1 seed sanctity.** When the judge proposes merging a Pass-1 user/category/topic node into a Pass-2 LLM-extracted node, the Pass-1 seed should always win as `target_entity` — its identity comes from the Discourse JSON, not the LLM's extraction. Codify this as a hard rule, not a judge instruction.
- **Subset vs equivalence.** The judge prompt must distinguish "same concept" from "subset/superset" (e.g. `Order Service v36.x` ⊂ `Order Service` — these are *related*, not the same). PR #2102's strict prompt handles this correctly; do not weaken it.
- **Logging.** Every merge gets logged with: source IDs, target ID, judge response, similarity scores. Stored under `<data-dir>/logs/CANONICALIZE-<date>.md` for audit + rollback planning.

## Why this is parked, not killed

The deterministic Pass 4 catches the dominant pattern (the bulk of all dupes we'd estimate) for free. Spending real LLM money on the residual only makes sense once we've measured the residual and confirmed it carries query-impact-relevant value. Running Path C blind on a corpus where Pass 4 already collapsed the easy 70% would be premature optimization.

When the residual is measured, this doc becomes the playbook.
