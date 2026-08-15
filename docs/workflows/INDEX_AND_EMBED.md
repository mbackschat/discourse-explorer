# Workflow: Configure & Run the Knowledge-Graph Index

**Audience:** Claude Code, Codex, and other skill hosts. Read the shared [host compatibility contract](../../.claude/skills/HOST-COMPATIBILITY.md) before using this runbook. This guide tells you how to lead the user through model/embedding/gleaning choices and execute an index run in **either** mode.

## Purpose

Build or update the knowledge graph at `<data_dir>/graphrag/` using LightRAG's multi-pass ingest (structural custom-KG + LLM extraction).

## Pick the mode FIRST — it gates the rest of this runbook

Every step below is labelled with the modes it applies to. **Resolve the mode before step 0**; several steps are destructive and must be skipped on `--resume`.

| | `--resume` (default) | `--full` |
|---|---|---|
| **Use when** | New/changed topic JSON was scraped and you want it in the graph | `entity_types.json`, embedding model, embedding dimension, or gleaning level changed; or a fresh corpus |
| **Destroys the graph** | No | **Yes** — rmtree's `graphrag/` |
| **Cost** | Only the documents missing from `doc_status` | Full re-extraction, hours + ~$6 on the reference corpus |
| **Steps 4 and 5** | **SKIP BOTH** | Mandatory |

**`--resume` is the default.** Reach for `--full` only when one of its triggers above actually fired, and only with explicit user approval.

> **The trap this table exists to prevent.** Step 5's validation sample runs `--index --clear --limit 50`. On a data dir that already holds a graph you were asked to *extend*, that wipes it. An agent that treats "steps 0–11 in order" as unconditional destroys the very thing the run was supposed to add to. Steps 4 and 5 are `--full`-only. There are no exceptions.

## Guardrails

- **`--index --clear` is destructive.** It wipes `graphrag/` before rebuilding. Always back up first if the current graph is expensive to recompute.
- **Embedding model and dimension are baked into the vector store.** Changing either *requires* `--clear`. Without `--clear`, the store silently mismatches and query quality degrades without error.
- **Do not change indexing config mid-run.** The config is read at process start. A running index uses the config as of its launch time.
- **Quote the data-dir path when it contains spaces** (it often does: `"/Users/foo/Downloads/xxx Discourse/..."`).
- **Never run `--clear` without user confirmation.** It costs money and time to rebuild.

## The flow (step by step)

### Step 0 — Confirm the vocabulary is finalized

Before touching indexing, verify `<data-dir>/config/entity_types.json` loads and validates:

```bash
uv run python -c "
from discourse_explorer.config import (
    bootstrap, load_entity_types, content_type_names, structural_type_names,
)
rc = bootstrap(None)
vocab = load_entity_types(rc.data_dir)
print('structural:', structural_type_names(vocab))
print('content   :', content_type_names(vocab))
"
```

If `config.py` raises, halt and send the user to `DISCOVER_ENTITY_TYPES.md`. The error message points at the fix (missing structural types, malformed JSON, etc.).

### Step 1 — Show current defaults, elicit user choices

Read the current per-run configuration so the user sees what's about to run:

```bash
uv run python -c "
from discourse_explorer.config import bootstrap
rc = bootstrap(None)
print('data dir     :', rc.data_dir)
print('provider     :', 'OpenAI' if rc.is_openai else 'Ollama')
print('extraction   :', rc.default_extraction_model())
print('query model  :', rc.query_model or '(inherits extraction)')
print('openai embed :', f'{rc.openai_embed_model} ({rc.openai_embed_dim}d)')
print('ollama embed :', f'{rc.embed_model} ({rc.ollama_embed_dim}d)')
print('gleaning     :', rc.gleaning)
"
```

Config source: `<data-dir>/config/.env` (created during `/discover-entity-types` or copied from `discourse_explorer/config/env.example`). When the user picks model/embedding/gleaning values during this run, persist those choices back into that file so re-runs pick them up without CLI flags.

Then ask the user for three choices, using one structured multi-question interaction when the host supports it:

#### Choice A — Extraction model

Present a table with estimated cost for this specific corpus (compute from topic count × avg tokens — see "Cost estimation" below). Current pricing landscape (check with WebSearch if unsure of current prices):

| Model | Quality | Approx. cost + runtime for 1300-topic corpus (gleaning=1) |
|---|---|---|
| **gpt-4.1-mini (recommended default)** | Best tuple-format discipline among cheap non-reasoning models | **~$6–8, ~10–15 h** |
| gpt-4o-mini | Cheaper; slightly more `<\|#\|>` format slippage under stress | ~$3–5, ~8–12 h |
| gpt-5-mini ⚠ | Reasoning overhead — avoid for indexing | ~$10–15, **~40–60 h** |
| gpt-5.2 ⚠ | Flagship, best synthesis quality but pointless when vocab is constrained | ~$40+, **~60–100 h** |

**Recommend gpt-4.1-mini.** Extraction punishes format slippage — each chunk produces `<|#|>`-delimited tuples, and a dropped field is a lost relation. gpt-4.1-mini has measurably better instruction-following than gpt-4o-mini at ~2× the token cost, which is tiny in absolute terms (~$3 delta) for a permanent index. Both are non-reasoning; neither has gpt-5-series' ~5× latency overhead.

Reserve gpt-5-series for `--query-model`, where reasoning helps synthesis over retrieved context.

#### Choice B — Embedding model

| Model | Dim | $/1M tokens | Notes |
|---|---|---|---|
| text-embedding-3-small | 1536 | $0.02 | cheap, baseline |
| text-embedding-3-large | 3072 | $0.13 | Recommended — better semantic recall, 6.5× cost on embeddings side (still negligible vs extraction) |
| text-embedding-ada-002 | 1536 | $0.10 | legacy |

Changing the embedding model **requires `--clear`**. Warn the user.

#### Choice C — Gleaning passes

| Passes | Effect | Cost multiplier |
|---|---|---|
| 0 | Extract each chunk once. Baseline recall. | 1.0× |
| 1 (recommended) | One "what did you miss?" re-pass per chunk. Meaningfully better recall. | ~1.5× |
| 2+ | Diminishing returns. | ~2× and up |

gleaning=1 is the single highest-ROI lever on the whole pipeline. Recommend `1` unless the user is cost-bound.

### Step 2 — Cost estimation (tailor to the specific corpus)

Before confirming, compute expected cost:

```bash
uv run python -c "
import glob, json, os, tiktoken
from discourse_explorer.config import bootstrap
from discourse_explorer.query import topic_to_document

rc = bootstrap(None)
enc = tiktoken.get_encoding('cl100k_base')
total_tokens = 0
n = 0
for f in glob.glob(os.path.join(str(rc.data_dir), 'topics', '*.json')):
    n += 1
    t = json.loads(open(f).read())
    total_tokens += len(enc.encode(topic_to_document(t)))

# Rough estimate: LightRAG chunks at ~1200 tokens with ~1500-token extraction
# prompt overhead, so input per chunk ≈ 2700. Output per chunk ≈ 500.
# Entity summarization adds some more output tokens on merges.
est_chunks = total_tokens // 1200 + n
gleaning = 1  # or user's chosen value
est_in = est_chunks * 2700 * (1 + 0.5 * gleaning)
est_out = est_chunks * 500 * (1 + 0.5 * gleaning)

print(f'Topics               : {n}')
print(f'Total content tokens : {total_tokens:,}')
print(f'Est. extraction chunks: {est_chunks:,}')
print(f'Est. input tokens    : {est_in:,.0f}')
print(f'Est. output tokens   : {est_out:,.0f}')
"
```

Multiply by the chosen model's `$/M` rates and add ~$0.10–$0.65 for embeddings (size of corpus × embedding $/1M). Present that total to the user before running.

### Step 3 — Probe OpenAI rate limits (OpenAI provider only)

Knowing your account's RPM/TPM ceilings determines how far you can safely push `llm_model_max_async` without stuttering. Tier labels drift; observed limits are ground truth.

```bash
uv run discourse-explorer query "$DISCOURSE_DATA_DIR" --detect-limits
```

Output is a compact table: the chat model's RPM/TPM ceilings, a tier hint, and a recommended `llm_model_max_async` + `max_parallel_insert` that assumes 50% headroom on both ceilings. Use these for the concurrency prompt in the user question. Low-tier accounts (RPM ≤500) should leave concurrency at the defaults — bumping up only causes rate-limit stutter.

Cost: ~$0.0001 (one 1-token chat call).

### Step 4 — Back up the existing graph — **`--full` ONLY**

**On `--resume`: skip this step and step 5, and go straight to step 6.** A resume does not destroy anything, so there is nothing to insure against.

On `--full`: always back up, even if the current graph is stale.

```bash
# Backup is just a copy; fast.
cp -a "$DISCOURSE_DATA_DIR/graphrag" "$DISCOURSE_DATA_DIR/graphrag.bak"
du -sh "$DISCOURSE_DATA_DIR/graphrag.bak"  # sanity check
```

If a prior `graphrag.bak/` already exists, ask the user whether to overwrite (contains their last-known-good state).

### Step 5 — Mandatory 50-topic validation sample — **`--full` ONLY**

> **DESTRUCTIVE.** The command below carries `--clear`. It rmtree's `graphrag/` before indexing its 50 topics, leaving a 50-topic graph where a full one used to be. **Never run it on a `--resume`.** Dropping `--clear` is not a safe workaround either — that writes 50 topics' extraction into the live graph and leaves `doc_status` claiming they are complete, which then suppresses their re-extraction in the real run. If you want to validate settings without risk, do it in a scratch data dir.

**Do not skip** unless the config is byte-identical to a previous successful full run. Cost: ~$0.30 / ~10 min on `gpt-4.1-mini`. Catches:

- Reasoning-mode models that project to 50-hour runs (the gpt-5-mini trap)
- Pass-1/Pass-2 type collisions (structural entities retyped after name merge)
- Format error rates above ~5%
- Rate-limit stutter from too-aggressive concurrency
- Description-quality loss from `FORCE_LLM_SUMMARY_ON_MERGE` tweaks

Command (apply whatever env/CLI overrides the user selected):

```bash
# Summary threshold comes from config (default 999); do not set it here.

uv run discourse-explorer query "$DISCOURSE_DATA_DIR" \
    --index --clear --limit 50 \
    [--extraction-model <M>] [--gleaning <N>]
```

**Acceptance checks** (run after completion):

| Check | How | Pass threshold |
|---|---|---|
| Rate & full-run projection | `(50 × 60) / wall_clock_seconds`; multiply to total topic count | ≤12h projected |
| Format errors | `grep -c 'LLM output format error' <log>` | ≤5% of chunk count |
| Entity-type distribution | `Counter(d.get('entity_type') for _,d in nx.read_graphml(graphml).nodes(data=True))` | ≥90% in-vocab |
| Topic over-count | Count of `entity_type=topic` nodes | ≤2× limit. Pass 3 (`_enrich_structural_types`) auto-repairs Pass 1↔Pass 2 collisions via `aedit_entity`, so an under-count after Pass 3 indicates a Pass 1 write that never landed (genuine insert failure), not a merge collision. |
| Description quality | Inspect 3 entities: high-degree multi-chunk, collision-prone, single-chunk | Readable, no hallucinations, no contradictions |

**If any check fails**: halt. Report findings to the user. Consult prior per-run logs under `<data_dir>/logs/` for known fixes. Do not proceed on a failed sample — sampling exists to catch this cheaply.

**If all checks pass**: project full-run time from the sample rate, present to user, get explicit approval for full run.

### Step 6 — Final confirmation, then launch

Present the final configuration and ask for explicit approval. On `--full`, include the validated projection from step 5.

Then launch **via the script**, passing the mode you resolved before step 0 — not both, and never bare (a bare invocation exits 64 by design):

```bash
# --resume: the default. Adds new/changed topics to the existing graph.
DISCOURSE_DATA_DIR="$DISCOURSE_DATA_DIR" ./scripts/index.sh --resume
```

```bash
# --full: ONLY if an entity_types.json / embedding / gleaning change or a fresh
# corpus triggered it, steps 4-5 passed, and the user explicitly approved.
DISCOURSE_DATA_DIR="$DISCOURSE_DATA_DIR" ./scripts/index.sh --full
```

**Do not run `uv run discourse-explorer query ... --index` directly for a full run, and do not launch it as a host-managed background task** (`run_in_background=true`). Both leave the process in the launching shell's process group, where a group-wide kill reaps it, taking down multi-hour runs with no traceback. The script starts the run in its own session, sets `PYTHONUNBUFFERED=1` so the log does not lag behind reality, refuses to stack a second indexer on the same data dir, and fails loudly if the child dies immediately instead of printing a PID for a dead run.

Settings come from the layered `.env` chain, not the command line. If the user chose per-run overrides, write them into `<data-dir>/config/.env` before launching so the run stays reproducible from config alone.

**Watch it with a `Monitor` on the log path the script prints** — that is now the only completion signal, since the run is no longer a host-managed task. **Don't poll**, and before concluding a run has died, check the process list with **both** name spellings (`pgrep -fl "discourse-explorer query|discourse_explorer.query"`) as well as whether files under `graphrag/` are still changing.

### Step 7 — Handle failures

The run is not a host-managed task, so there is no `status: failed` to react to. You find out from the log Monitor, or from the process disappearing. Inspect the log (`tail -80` the path the script printed). Common failure modes we've seen:

| Symptom | Cause | Fix |
|---|---|---|
| `WorkerTimeoutError: Worker execution timeout after 60s` | OpenAI embeddings slow on a particular batch (transient) | Relaunch with `./scripts/index.sh --resume` — work already flushed to `graphrag/` is kept |
| `400 - Invalid 'input[0]': maximum input length is 8192 tokens` | A long topic produced an oversized custom-KG chunk | Already fixed in `_topic_to_custom_kg` via `_split_for_embedding`. If it recurs, lower `_EMBED_TOKEN_LIMIT` in `query.py` |
| `Rate limit exceeded` | OpenAI tier cap hit | Pause, lower `LLM_MODEL_MAX_ASYNC` in `<data-dir>/config/.env` (never on the command line, and never by editing `_get_rag`), or retry later |
| `Connection error` | Network blip | Retry |
| Gatekeeper / code-signing errors on `numpy` | macOS quarantine | `uv sync --reinstall-package numpy` |

For anything that looks non-transient, dig into the traceback *before* retrying. Unclear errors warrant consulting the user.

### Step 8 — Post-index verification (automatic + manual)

`index_topics` prints a verification block automatically on success:

```
Post-index verification
  Topics in graph    : 1331 (source: 1331)
  Categories in graph: 22
  Tags in graph      : 139
  Users in graph     : 336
  Total nodes        : ...
  Total edges        : ...
```

Check: does `Topics in graph` match `source`? If not, some topics failed Pass 1. Review the Pass 1 "failed topic ids" lines earlier in the log.

Also run the four standalone verifications (adapted for `$DISCOURSE_DATA_DIR`):

**1. Entity-type distribution sane:**
```bash
uv run python -c "
import networkx as nx, os
from collections import Counter
G = nx.read_graphml(f\"{os.environ['DISCOURSE_DATA_DIR']}/graphrag/graph_chunk_entity_relation.graphml\")
print(Counter(d.get('entity_type','') for _,d in G.nodes(data=True)).most_common())
"
```
Expect: ≥95% within the configured vocabulary.

**2. No reply-fusion bugs:**
```bash
uv run python -c "
import networkx as nx, os
G = nx.read_graphml(f\"{os.environ['DISCOURSE_DATA_DIR']}/graphrag/graph_chunk_entity_relation.graphml\")
suspects = [n for n in G.nodes() if 'reply' in n.lower() or 'replying' in n.lower()]
print(f'Fusion bugs: {len(suspects)}')
"
```
Expect: `0`.

**3. Every source category as a typed Category node:**
```bash
uv run python -c "
import json, glob, networkx as nx, os
dd = os.environ['DISCOURSE_DATA_DIR']
G = nx.read_graphml(f'{dd}/graphrag/graph_chunk_entity_relation.graphml')
graph_cats = {n for n,d in G.nodes(data=True) if d.get('entity_type','').lower()=='category'}
source_cats = set()
for f in glob.glob(f'{dd}/topics/*.json'):
    t = json.loads(open(f).read())
    if t.get('category_name'): source_cats.add(t['category_name'])
print(f'Source: {len(source_cats)}, Graph: {len(graph_cats)}, Missing: {len(source_cats - graph_cats)}')
"
```
Expect: `Missing: 0`.

**4. Spot-check query:**
```bash
uv run discourse-explorer query "$DISCOURSE_DATA_DIR" "what categories exist on this forum" --mode local
```
Expect: answer lists actual categories from the graph.

### Step 9 — Regenerate the visualization

```bash
uv run discourse-explorer visualize "$DISCOURSE_DATA_DIR" --open
```

Watch stdout for the coverage diagnostic:
```
Edge categorization coverage: XX.X% (N/Total fell to Other)
```
If coverage is <75%, a warning fires. Consider extending `_REL_KEYWORDS` in `visualize.py` (follow-up; not urgent).

Open the HTML and eyeball the legend: one color per type in `<data-dir>/config/entity_types.json`, `Other` bucket small.

### Step 10 — Cleanup

If all verifications pass and the user is satisfied, offer to remove the backup:

```bash
rm -rf "$DISCOURSE_DATA_DIR/graphrag.bak"
```

Do not remove without user approval — backups are cheap to keep.

### Step 11 — Suggest query-guide regeneration

After the cleanup offer, point the user at `/create-query-guide`. The guide's §4 tables (scale, top entities by degree, categories, versions) and §5 verb table are all now stale versus the fresh graph, and `/query-advisor` reads that guide to constrain its recommendations. Regenerating is cheap (~$0.05, ~30s).

> Next step (not a blocker):
> ```
> /create-query-guide $DISCOURSE_DATA_DIR
> ```

Surface this as the last bullet of the final summary you give the user — don't auto-invoke. The indexer's job is done; the user decides whether to regenerate now or later. Leaving the guide stale isn't broken, but it will have `/query-advisor` routing against a graph shape that no longer matches reality.

## Commands reference

```bash
# Back up the current graph (always, before a destructive run)
cp -a "$DISCOURSE_DATA_DIR/graphrag" "$DISCOURSE_DATA_DIR/graphrag.bak"

# Destructive rebuild — wipes graphrag/ and re-extracts every topic
DISCOURSE_DATA_DIR="$DISCOURSE_DATA_DIR" ./scripts/index.sh --full

# Add new/changed topics, or resume after a failure — non-destructive, cheap
DISCOURSE_DATA_DIR="$DISCOURSE_DATA_DIR" ./scripts/index.sh --resume

# Per-run overrides go in <data-dir>/config/.env, NOT on the command line,
# so the run stays reproducible from config alone:
#   EXTRACTION_MODEL=gpt-4.1-mini
#   GLEANING=1

# Refresh structural embeddings after Pass 3 timeouts (no Pass 1/Pass 2; ~$0.02)
uv run discourse-explorer query "$DISCOURSE_DATA_DIR" --index --enrich-only

# Re-run ONLY Pass 4 name canonicalization against the existing graph.
# No Pass 1/2/3, no LLM cost during merges, ~150s on a 1.3K-topic corpus.
# Use when canonicalization RULES changed, or a prior Pass 4 was interrupted.
# This is the cheap way to apply a new merge rule; do NOT re-index for that.
uv run discourse-explorer query "$DISCOURSE_DATA_DIR" --index --canonicalize-only

# Regenerate viz
uv run discourse-explorer visualize "$DISCOURSE_DATA_DIR" --open

# Spot-check a query
uv run discourse-explorer query "$DISCOURSE_DATA_DIR" "your question" --mode mix
```

## Config surface (reference)

Per-run knobs live in `<data-dir>/config/.env` (CLI flags override for a single run):

| Knob | Env (in `<data-dir>/config/.env`) | CLI | Default |
|---|---|---|---|
| OpenAI extraction model | `EXTRACTION_MODEL` | `--extraction-model` | `gpt-4.1-mini` |
| Ollama extraction model | `EXTRACTION_MODEL` | `--extraction-model` | `qwen2.5:14b` |
| Query-time model | `QUERY_MODEL` | `--query-model` | reuses extraction |
| Gleaning passes | `GLEANING` | `--gleaning N` | `1` |
| OpenAI embedding model | `OPENAI_EMBED_MODEL` | — | `text-embedding-3-large` |
| OpenAI embedding dim | `OPENAI_EMBED_DIM` | — | auto-detected |
| Ollama embedding model | `EMBED_MODEL` | — | `nomic-embed-text` |
| Ollama embedding dim | `OLLAMA_EMBED_DIM` | — | `768` |
| Ollama host | `OLLAMA_HOST` | — | `http://localhost:11434` |
| LLM concurrency (`llm_model_max_async`) | `LLM_MODEL_MAX_ASYNC` | `--llm-concurrency N` | `8` (OpenAI) / `1` (Ollama); `--detect-limits` recommends per tier |
| Parallel inserts (`max_parallel_insert`) | `MAX_PARALLEL_INSERT` | `--parallel-insert N` | `4` |
| Data directory | `DISCOURSE_DATA_DIR` (project-root `.env` only) | positional arg / `--output` / `--path` | required |

## Anti-patterns (don't do these)

- **Don't run `--index --clear` without user approval.** Money + time.
- **Don't skip the backup.** `graphrag.bak/` is ~250MB on a 1300-topic corpus — cheap insurance.
- **Don't change embedding model/dim without `--clear`.** The vector store is bound to the old dimension.
- **Don't wait for a completion notification.** The run is not host-managed, so none arrives. Watch the log with a `Monitor`; that is the only signal.
- **Don't `cat /tmp/.../bjvz6lyw7.output`** to peek — use `tail -N <file>` for structured inspection.
- **Don't auto-retry on non-transient errors.** Read the traceback, fix the underlying cause, *then* retry.
- **Don't remove `graphrag.bak/` without user confirmation.** It's their safety net.

## Handoff

After this workflow completes with passing verifications, the graph is ready for `query_graph` and `visualize`. The `DISCOVER_ENTITY_TYPES.md` workflow can be re-run if the corpus grows significantly or focus shifts.
