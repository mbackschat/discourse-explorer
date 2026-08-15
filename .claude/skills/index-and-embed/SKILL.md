---
name: index-and-embed
description: >-
  Run LightRAG indexing in either mode — `--resume` (cheap, non-destructive, adds new/changed
  topics) or `--full` (destructive `--clear` rebuild). Picks the mode with the user, estimates
  cost against the actual corpus, backs up the prior graph before a destructive run, launches
  detached, and verifies after. Use whenever the user wants to index, re-index, refresh, or update
  the knowledge graph (triggers: "update the graph", "refresh the index", "index the new topics",
  "rebuild the graph", "re-index", "run indexing", "index the forum", "index the knowledge
  graph").
---

# Index and embed (build or update the knowledge graph)

## Pick the mode FIRST — this is the decision that costs money

Most requests to "update" or "re-index" after a scrape want **`--resume`**, not a rebuild. Getting this wrong costs ~$6 and hours instead of ~$0.45 and minutes.

| | `--resume` | `--full` |
|---|---|---|
| Destructive | no | **yes** — `rmtree(graphrag/)` |
| Extracts | only docs absent from `doc_status` | every topic |
| Typical cost | cents | ~$6-8 per 1300 topics |
| Use when | new topics scraped; a prior run died part-way | entity vocabulary, chunking, or extraction model changed |

**Default to `--resume`.** Choose `--full` only when something that shapes extraction itself changed. `docs/workflows/INDEX_AND_EMBED.md` opens by saying the same thing: do *not* rebuild merely because topic JSON was added.

**One trap that silently converts `--resume` into `--full`-scale spend:** LightRAG keys doc-dedupe on md5 of the document text `topic_to_document` produces. Any change to that text — including "harmless" cosmetic normalization of the tag header — re-keys every affected document, so they all re-extract. Measured 2026-08-14: 1,018 of 1,399 topics re-keyed, an 85-document update became 1,099. Before running `--resume`, if `topic_to_document` changed since the last index, expect full cost and say so. Verify cheaply by hashing a few topics against `kv_store_doc_status.json` keys.

## Host compatibility

Before executing this skill, read [`../HOST-COMPATIBILITY.md`](../HOST-COMPATIBILITY.md). Operations such as “ask the user,” “invoke the skill,” and “delegate execution” use the host bindings defined there.

**Goal:** bring `<data_dir>/graphrag/` up to date, in whichever mode the corpus actually needs — `--resume` to fold in new/changed topics, `--full` to rebuild from scratch — with user-confirmed model/embedding/gleaning/concurrency choices.

**When to run `--resume`** (the common case, cents and minutes): topics were scraped; a prior run stopped part-way.

**When to run `--full`** (destructive, ~$6–8 per ~1300 topics, hours): the entity vocabulary changed, the embedding model or dimension changed, gleaning changed, or `topic_to_document`'s text changed. Note a fresh scrape is **not** on this list — new topic JSON alone calls for `--resume`.

## Authoritative runbook

**Read `docs/workflows/INDEX_AND_EMBED.md` in full before running.** That document is the step-by-step protocol with model comparison tables, cost-estimation snippet, concurrency guidance, rate-limit probe, failure taxonomy, acceptance thresholds for the validation sample, and all verification snippets. This SKILL only names prerequisites and routes you there.

## Resolve the data directory first

Before anything else, settle which data dir this run operates on. Indexing is expensive and irreversible-without-backup, so clarity here is load-bearing. Source priority:

1. **Skill argument.** If the invocation included a path (e.g. `/index-and-embed ./data/my-forum` or a path in the natural-language message), treat that as authoritative. Resolve with `Path(arg).expanduser().resolve()` and confirm `<path>/topics/` + `<path>/graphrag/` exist.
2. **Ask the user.** If no path was given, enumerate candidate data dirs and let the user pick:
   - Run `ls -d ./data/*/ 2>/dev/null` to find subdirs with a `topics/` inside.
   - Also probe `DISCOURSE_DATA_DIR` via `uv run python -c "from discourse_explorer.config import bootstrap; print(bootstrap(None).data_dir)"`. If it succeeds, that's the recommended option.
   - Present each candidate as an option; "Other" is auto-added for a custom path.
   - Do not silently fall back to `DISCOURSE_DATA_DIR` — one confirmation click is cheap; running `--index --clear` on the wrong forum wipes a $6+ graph.

Pass the chosen path through the rest of the workflow and into every `uv run discourse-explorer query <data-dir> ...` invocation. All subsequent references to `<data-dir>` below resolve to this value.

## Prerequisites to check before doing anything

1. `<data-dir>/config/entity_types.json` exists and validates (config.py raises if the four structural types are missing). If it's missing, halt and route to `/discover-entity-types`.
2. `<data-dir>/topics/*.json` exists.
3. **Provider selection.** Probe `bootstrap(None).is_openai` (`True` iff `OPENAI_API_KEY` resolves in the layered `.env` chain). Branch:
   - **Key present** → OpenAI provider. Proceed.
   - **Key missing** → ask the user which provider to use:
     - *Option A: set an OpenAI key in `<data-dir>/config/.env` and re-invoke.* Halt the skill with a one-line instruction telling the user the exact key to add (`OPENAI_API_KEY=sk-...`) and the file path. **Do not accept the key pasted into the chat** — it ends up in logs/transcripts. Require the user to edit the file themselves, then re-run the skill.
     - *Option B: fall back to Ollama.* Probe `OLLAMA_HOST` (default `http://localhost:11434`) reachability with `curl -s -o /dev/null -w '%{http_code}' $OLLAMA_HOST/api/tags` before continuing. If not reachable, halt with instructions to start the daemon (`ollama serve`) and re-invoke.
   - Persist the decision into `<data-dir>/config/.env` so later steps and `scripts/index.sh` see the same provider without ambiguity.

   Also: when the user picks model/embedding/gleaning values during the run, **persist them back into `<data-dir>/config/.env`** so subsequent runs and `scripts/index.sh` pick them up without CLI flags.

## Invocation hygiene + live monitoring (always, for sample and full runs)

A full index is hours long. Runbook benchmarks are approximate. The user cannot see your tool calls — only your text output. Three rules make the difference between a responsive session and a silent black box:

### 1. Launch the full run **detached, via `scripts/index.sh`**

```bash
DISCOURSE_DATA_DIR=<data-dir> ./scripts/index.sh --resume   # non-destructive (usual case)
DISCOURSE_DATA_DIR=<data-dir> ./scripts/index.sh --full     # DESTRUCTIVE --clear rebuild
```

A mode is required; a bare invocation exits 64 rather than defaulting to the destructive path. The script starts the run in its **own session** (`start_new_session=True`; macOS has no `setsid`), prints its PID and log path, and then verifies the child is still alive — so it reports failure instead of printing a PID for a run that already died.

**Do not launch a full run as a host-managed background task** (e.g. Bash `run_in_background: true`), and do not hand-roll `nohup`: `nohup` blocks SIGHUP but leaves the child in the launching shell's process group, so a process-group kill still reaches it.

**Before launching, confirm nothing else is indexing that data dir, matching BOTH command-line spellings:**

```bash
pgrep -fl "discourse-explorer query|discourse_explorer.query"
```

`discourse_explorer.query` (dot) appears only under `python -m`; normal CLI and script use produce `discourse-explorer query` (hyphen, space). On 2026-08-14 a check that matched only the dotted form reported every live run as dead, three indexers ended up writing one `graphrag/` concurrently, and the graph went from 15,756 nodes to 4,566. `query.py` now holds an exclusive `flock` on the data dir and exits code 2 if one is held, so this is belt-and-braces — but a stacked run is the single most damaging failure mode in this project, and the lock message is the thing to read if a launch refuses.

Settings come **only** from the layered `.env` chain, never from the launch command. If a run needs a non-default knob, write it into `<data-dir>/config/.env` and relaunch; do not prefix it on the command line, or the run stops being reproducible from config alone.

Do not poll for the PID. Arm a `Monitor` on the log path the script prints (rule 2 below), and read progress from `Pass N progress:` lines, which carry elapsed + rate + ETA.

**Sample runs** (`--limit N`), which are short and interactive, may be run directly:

```bash
PYTHONUNBUFFERED=1 uv run python -u -m discourse_explorer.query \
  <data-dir> --index --clear --limit N \
  > <data-dir>/logs/INDEX_AND_EMBED-<ts>.sampleN.stdout.txt 2>&1
```

> **A sample run carries `--clear`: it DESTROYS the existing graph.** It is a pre-flight for a `--full` build on a data dir whose graph is expendable — never a way to "test" against a graph you want to keep. If a good graph exists and you only want to validate settings, either back it up first, or point the sample at a throwaway copy of the data dir. Dropping `--clear` here is not a fix: the sample would then merge N topics into the real graph.

**Do not use `| tee`.** Python block-buffers stdout when piped, which hides progress for 5+ minutes — empirically observed on this project. Direct file redirect with `-u` and `PYTHONUNBUFFERED=1` gives live progress without the stack-of-buffers problem. Use the **same `<ts>` as the findings log** so the pair is obvious.

### 2. Always arm a persistent `Monitor` on the stdout file

Silence is not success. The filter must catch progress (so you can relay pacing) and failure signatures (so a crash isn't invisible). Derive the alternation from what `query.py` actually prints at the time you write it — don't hardcode — but it needs to cover, at minimum, the `^ *Pass ` milestone lines, `Traceback`, and any `WARNING`/`Error` the module emits.

**Anchor `Pass` with `^ *Pass `, not `^Pass `.** Milestone lines (`Pass 1: Inserting…`) start at column 0, but the *progress* lines — the ones carrying rate and ETA, the only thing that tells you the run is alive — are indented four spaces by `_progress_line`. A `^Pass ` filter matches the milestones and silently drops every progress tick, so the Monitor goes quiet mid-pass and the run looks dead. Verified against a live log: `grep -cE "^Pass "` returned 1 where `grep -cE "^ *Pass "` returned 3.

Use `tail -F` (not `-f`) so the tail survives truncation, `grep -E --line-buffered` so events don't sit in pipe buffers, and `persistent: true` on the `Monitor` call (the full run is multi-hour, well past the default 5-minute timeout).

Progress markers, in order: `Pass 1: Inserting...` / `Pass 1 progress:` / `Pass 1 complete:` → `Pass 2: LLM extraction...` → `Pass 3: Re-asserting...` / `Pass 3 progress:` / `Pass 3 complete:` → `Pass 4a`/`Pass 4b progress:` (canonicalization) → `Post-index verification` → `Indexing complete`. Failure markers worth catching explicitly: `LedgerFlushError` (a storage write failed and the `doc_status` ledger was deliberately not advanced), `Refusing to start` (data-dir lock held), and `failed after 3 attempts`.

**Reading Pass 1 on a `--resume`.** Progress lines read `(N ok, N failed, N skipped)` — the skipped field is always present, including at zero. Pass 1 keeps a ledger of the payload hash it last seeded per topic (`graphrag/pass1_payload_hashes.json`), so unchanged topics are skipped outright. `skipped` climbing fast with `ok` near zero is the healthy steady state, not a stall.

Three cases legitimately show `0 skipped`, and none of them is a fault:

- the first run after `--clear` (the ledger is wiped along with the graph);
- the first run after the ledger feature itself landed, which has to seed every topic once to *build* the ledger;
- any run following a change to how structural nodes are built — the hash covers the produced payload, so such a change correctly invalidates every topic.

On any *later* resume, `0 skipped` with a populated `pass1_payload_hashes.json` is a genuine defect worth stopping for.

**Reading `N stale doc(s) purged, N stale edge(s) retracted, N orphan(s) dropped`.** A topic whose payload changed has its previously-recorded documents deleted before the new payload is seeded, so an edit replaces the old nodes instead of accreting beside them. `purged` should track the number of *changed* topics, not the number of topics: a resume where everything is unchanged purges nothing. `purged` climbing while `ok` stays at zero would mean documents are being deleted without being re-seeded, which is worth stopping for. `retracted` counts structural relations the topic no longer asserts, and `dropped` counts nodes left dead by that retraction — a renamed tag typically shows 1 retracted and 1-2 dropped. All three at zero on a run with a non-zero `ok` means only brand-new topics were seeded, which is normal.

The ledger gained a `docs` field for this. Entries written before it exist carry only a hash, and those still skip correctly — the migration costs nothing on unchanged topics. A *changed* topic under one of those older entries logs `v1 ledger entry, purging 'topic-<id>' only`: the primary document is derivable from the topic id, but overflow chunks and the Pass 2 document are not, so they persist until that topic changes again.

If the filter gets rate-limited (Pass 2's per-doc `INFO` lines are high-volume), narrow it rather than widening — the goal is one event per *meaningful* state transition.

### 3. Relay each milestone to the user concisely, and project the ETA

When a Monitor event arrives, echo it to the user in one sentence. Include a percentage or fraction, and — once you have a pacing data point — a projected ETA. Example:

> `Pass 1 at 500/1331 (38%).`
>
> `Pass 2 underway. At sample-3's rate (~9 min for 50 topics), extrapolated ~4h for the full 1331.`

Don't narrate internal deliberation, don't speculate past the data. Short confirmations are enough. The user should never wonder whether the job is alive.

**Don't poll.** Monitor events handle mid-run updates; that is what the Monitor in rule 2 is for. (This previously said `run_in_background: true` notifies you on exit — it does, but rule 1 forbids launching the run that way, so the Monitor on the log file is your only signal.)

**Do confirm before declaring a run dead.** A quiet log is not evidence: the log lags if `PYTHONUNBUFFERED` is unset, and a run can be mid-checkpoint. Check the process list with **both** spellings from rule 1, *and* whether files under `graphrag/` are still changing. Reporting a live run as dead is how a second indexer gets launched on top of it.

## Write a findings log as you go

**Create a timestamped markdown log at the start of the run**, append to it as you progress, finalize when done. Path:

```
<data-dir>/logs/INDEX_AND_EMBED-$(date +%Y%m%d-%H%M%S).md
```

(Create `logs/` under the data dir if it doesn't already exist.)

This log is **distinct** from the raw stdout/stderr file that `scripts/index.sh` writes (`index-*-<timestamp>.log`). The raw log is verbose and mechanical; the findings log is your curated write-up — what was decided, what the probe reported, what the sample showed, what verifications returned, and any anomalies.

Capture, in rough order:

1. **Starting config** — provider, extraction model, embedding model + dim, gleaning passes, concurrency, summary-cascade knob, vocabulary state (last-edited date of `<data-dir>/config/entity_types.json`).
2. **Rate-limit probe output** (step 3) — RPM/TPM ceilings + recommended concurrency. Note if you deviate from the recommendation and why.
3. **User choices at step 1** — the multi-question answer. If the user overrode a default, record the rationale.
4. **Cost estimate** (step 2) — corpus token count, projected spend, whether the user accepted.
5. **Validation sample results** (step 5, `--full` only) — wall clock, rate (topics/min), format-error rate, entity-type distribution, topic-count check, the 3 sample description inspections, projected full-run time.
6. **Sample-vs-baseline comparison** — was the rate within 20% of a previous successful run? Any acceptance thresholds tripped?
7. **If any check failed** — what the issue was, what options were considered, what was decided (and why that option, not the others).
8. **Full-run metrics** (after step 10) — wall clock, topics/min, **Pass 1 / Pass 2 / Pass 3 breakdown** (indexing runs three internal phases: `ainsert_custom_kg` for structural seeds, `ainsert` for LLM extraction, `_enrich_structural_types` for collision repair via `aedit_entity`), post-index verification block, any warnings encountered mid-run. Pass 3 is parallelized at `llm_model_max_async` concurrency; log its re-assertion count (e.g. `Pass 3 complete: 1800/1800 structural entities re-asserted`).
9. **Manual verification snippet outputs** (step 8) — raw numbers for each of the four checks, not just pass/fail.
10. **Failures + resolutions** — anything that required a retry, config tweak, or mid-run decision.
11. **Final "is this graph shippable" judgment** — plus the reasoning. If you decided to ship despite a residual issue (like Option C's type-label collision), document why it's acceptable.

**Style:** pedagogical, not a stack trace. Explain *why*, not just *what*. Future sessions should be able to reconstruct the decision trail without re-asking.

## Remember run settings across invocations

At the start of every run (immediately after resolving the data dir, before asking the user), **gather all available values for each tunable from up to four sources**:

1. **Recommended** — what the runbook and/or `--detect-limits` probe suggests for this corpus / OpenAI tier. Re-computed every run.
2. **Codebase default** — what happens when neither env nor CLI is set (documented in `config.py` and the "Config knobs" table below).
3. **Current `<data-dir>/config/.env`** — the value the tool would actually use right now if launched with no flags. Parsed via `bootstrap()`.
4. **Last skill-run JSON** — the value persisted by the most recent `/index-and-embed` invocation. Glob `<data-dir>/config/index-and-embed-*.json`, lexicographic sort (the `YYYYMMDD-HHMMSS` suffix makes this chronological), take the last.

**Always ask the user for every tunable** — even if all four sources agree. Never silently apply.

For each user question, build the option list by:

- Collecting the values that exist across the four sources.
- Deduplicating — identical values collapse into one option, with its label listing every source that produced it (e.g. `Use 13 (recommended, matches last skill-run)`).
- Marking the runbook-recommended option with `(Recommended)` in the label.
- If a source has no distinct value to contribute (e.g. no prior JSON), skip that source — do not pad with a phantom option.

The resulting question typically has 1–4 concrete options plus the auto-added `Other` for a custom value. When only one option exists, still ask — confirmation is cheap; accidental $6 re-runs are not.

**Why both `.env` and JSON exist alongside each other:** `.env` is authoritative for *what the tool reads at launch* — `scripts/index.sh` and any non-skill CLI invocation consume only `.env`. The JSON snapshots *what the user last chose via this skill specifically*, so a future skill run can surface that value even when `.env` has been hand-edited in the interim. When both sources exist and differ, the skill surfaces both as distinct options and the user picks.

Fields captured for this skill:

| Field | Source |
|---|---|
| `provider` | `OPENAI_API_KEY` presence |
| `extraction_model` | `EXTRACTION_MODEL` / `--extraction-model` |
| `query_model` | `QUERY_MODEL` / `--query-model` |
| `gleaning` | `GLEANING` / `--gleaning N` |
| `openai_embed_model` | `OPENAI_EMBED_MODEL` |
| `openai_embed_dim` | `OPENAI_EMBED_DIM` (auto-resolved if unset) |
| `concurrency` | user choice at step 1 |
| `summary_skip_threshold` | `FORCE_LLM_SUMMARY_ON_MERGE` |

After the user has answered all pre-run questions (step 6 / final confirmation, before the run launches), write the JSON:

```
<data-dir>/config/index-and-embed-<YYYYMMDD-HHMMSS>.json
```

Use the same timestamp as the findings log so the pair is obvious. Shape:

```json
{
  "skill": "index-and-embed",
  "timestamp": "20260423-044603",
  "settings": {
    "provider": "OpenAI",
    "extraction_model": "gpt-4.1-mini",
    "query_model": "gpt-4.1-mini",
    "gleaning": 1,
    "openai_embed_model": "text-embedding-3-large",
    "openai_embed_dim": 3072,
    "concurrency": 8,
    "summary_skip_threshold": 999
  }
}
```

Store **semantic values**, not host-specific option labels — labels change, semantic values don't. Don't store post-run metrics (wall-clock, sample pass/fail, cost) — those belong in the per-run findings log, not the settings JSON.

## Entry protocol

Follow steps 0–11 of `docs/workflows/INDEX_AND_EMBED.md`, **skipping steps 4 and 5 unless the mode is `--full`** (they back up and then destructively re-sample the graph). Ask the user at every user-facing decision point: model, embedding, gleaning, concurrency, sample-looks-good-proceed-to-full-run. The workflow doc specifies:

- **Step 3**: `uv run discourse-explorer query --detect-limits` for OpenAI rate-limit probe + concurrency recommendation.
- **Step 5** (`--full` only): **mandatory** 50-topic validation sample (~$0.30 / ~10 min) with explicit acceptance thresholds; don't skip unless config is byte-identical to a previous successful run. It carries `--clear` — never run it on a `--resume`.
- **Step 6**: final user approval, then launch via `scripts/index.sh <mode>`.
- **Step 8**: four manual verification snippets.

When in doubt about behaviour, consult the workflow doc's failure-taxonomy table (step 7) before retrying anything.

## Config knobs (quick reference)

Full semantics + trade-offs are in the workflow doc. This is a summary so you don't have to read the doc just to enumerate what can be overridden.

All env vars live in `<data-dir>/config/.env`. CLI flags override for a single run without touching the file.

| Knob | Env (in `<data-dir>/config/.env`) | CLI | Default |
|---|---|---|---|
| Extraction model | `EXTRACTION_MODEL` | `--extraction-model` | `gpt-4.1-mini` |
| Query model | `QUERY_MODEL` | `--query-model` | inherits extraction |
| Gleaning passes | `GLEANING` | `--gleaning N` | `1` |
| OpenAI embedding | `OPENAI_EMBED_MODEL` | — | `text-embedding-3-large` |
| LLM concurrency | `LLM_MODEL_MAX_ASYNC` | `--llm-concurrency N` | `8` (OpenAI) / `1` (Ollama); `--detect-limits` recommends per tier |
| Parallel inserts | `MAX_PARALLEL_INSERT` | `--parallel-insert N` | `4` |
| Summary-skip threshold | `FORCE_LLM_SUMMARY_ON_MERGE` | — | `999` (`config.SUMMARY_ON_MERGE_DEFAULT`, skips the cascade; LightRAG's own default is `8`, which costs 3–5× more) |
| Sample-run cap | — | `--limit N` | unlimited |
| Enrich-only rerun | — | `--enrich-only` | off — runs only Pass 3 (structural type re-assertion) against an existing graph; refreshes stale embeddings from prior Pass 3 timeouts without re-indexing |
| Canonicalize-only rerun | — | `--canonicalize-only` | off — runs only Pass 4 (entity-name merges) against an existing graph. No LLM cost during merges, ~150s on a 1.3K-topic corpus. **Reach for this whenever canonicalization rules changed or a prior Pass 4 was interrupted — never propose a full re-index to apply a merge rule.** Incompatible with `--clear`. |

## After a successful index: suggest regenerating the query guide

On a clean finish (all four manual verifications in workflow step 8 pass), add the following to the findings log and surface it as the last bullet of the final user-visible summary:

> **Query guide regeneration needed: YES.** The corpus scale, entity histogram, top-degree list, category counts, version counts, and relation-verb frequencies are all now stale versus any prior `<data-dir>/QUERY-GUIDE.md`. Run `/create-query-guide <data-dir>` to refresh (~$0.05, ~30s). `/query-advisor` reads that guide to constrain routing — leaving it stale will have the advisor referencing a graph shape that no longer matches reality.

**Do not auto-invoke `/create-query-guide`.** The user just spent hours + money on the index; don't chain another decision-requiring skill without a break. Surface the suggestion, then stop — the user decides whether to run it now or later.

## Anti-patterns

- Don't run `--clear` without explicit user approval — money + time + irreversibility.
- Don't skip the backup. Cheap insurance.
- Don't skip the 50-topic validation sample unless the config is byte-identical to a previous successful full run (documented in the per-run log under `<data_dir>/logs/`).
- Don't wait for a background-task completion notification — the run is not host-managed, so none will arrive. The log Monitor is the only signal.
- **Never check for a running indexer with a single-spelling pattern.** `ps aux | grep discourse_explorer.query` and `pgrep -f "discourse_explorer.query"` both return empty against a live run, because the console entry point's command line is `discourse-explorer query` — hyphen, space. That false negative is what caused the 2026-08-14 incident. Always use both forms: `pgrep -fl "discourse-explorer query|discourse_explorer.query"`. The `flock` now makes a second writer impossible (exit 2 naming the holder), so the grep is for diagnosis only — never to decide whether it is safe to launch.
- Don't `kill` a process this check turns up without confirming with the user first. A long-running index that looks idle is usually alive; see CLAUDE.md rule 4.
- Don't auto-retry on non-transient errors (consult the workflow's failure taxonomy first).
- Don't change the embedding model or dimension without `--clear`. Silent dim mismatch; no error.
- Don't remove `graphrag.bak/` without explicit user confirmation.
