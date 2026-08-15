# Workflow: Generate a corpus-grounded QUERY-GUIDE.md

**Audience:** Claude Code, Codex, and other skill hosts. Read the shared [host compatibility contract](../../.claude/skills/HOST-COMPATIBILITY.md) before using this runbook. This guide tells you how to lead the user through regenerating `<data-dir>/QUERY-GUIDE.md` after a fresh index — or for the first time on a newly-indexed forum.

## Purpose

Produce `<data-dir>/QUERY-GUIDE.md` from the graphml + topic JSON + entity vocabulary. The guide is what `/query-advisor` reads to constrain its recommendations: entity names, category/version coverage, relation-verb frequency, and blind spots. Without a fresh guide, the advisor is routing against a graph shape that may no longer match reality.

## When to run this workflow

- **After `/index-and-embed` finishes.** The numbers drift with every re-index; stale tables mislead the advisor.
- **For the first time on a new corpus** that has been scraped + indexed but never had a guide generated.
- **After `/discover-entity-types` changed the vocabulary and the graph was re-indexed.** The entity-type histogram in §4.1 will have moved.

Don't run while indexing is still in flight — the graphml may be partially written.

## Guardrails

- **Overwriting is a state change.** Even though the graph itself is read-only during this run, `QUERY-GUIDE.md` is an artifact the user may have hand-edited. Ask before overwriting; default to back-up-then-overwrite.
- **§6 is LLM-authored; §1–§5 and §7–§12 are deterministic.** Don't hand-edit generated sections — they'll be overwritten on next run. If you need a stable local note, put it outside `QUERY-GUIDE.md` (e.g. a sibling `QUERY-NOTES.md` the workflow never touches).
- **Never invent entities in §6.** The module's LLM prompt pins §6's examples to the top-15 entities, strongest categories, and real version tags. If you see an example referencing a name not in §4.2/§4.3/§4.4, that's a regression — halt and diagnose before writing.

## Cost + runtime

- ~$0.05 on OpenAI `gpt-4.1-mini` for §6 (one call).
- ~30 seconds wall clock on a ~1000-topic corpus (dominated by graphml parse + one LLM round-trip).
- Output capped at 20 KB (self-imposed; flagged as a warning if exceeded).

## The flow (step by step)

### Step 0 — Resolve the data dir + confirm prerequisites

Use the same resolution pattern as `/index-and-embed` / `/discover-entity-types`:

1. Explicit skill argument (`/create-query-guide ./data/my-forum`) wins.
2. Otherwise probe `DISCOURSE_DATA_DIR` via `bootstrap(None).data_dir` and/or enumerate `ls -d ./data/*/`; ask the user to choose.
3. Don't silently fall back — even though this is read-only against the graph, the write-target is still per-forum.

Prerequisite checks (halt with routing advice if any fails):

| Missing | Halt + route to |
|---|---|
| `<data-dir>/topics/*.json` | Run the scraper first. |
| `<data-dir>/graphrag/graph_chunk_entity_relation.graphml` | `/index-and-embed` — can't derive §4 histograms before the graph is built. |
| `<data-dir>/config/entity_types.json` (via `load_entity_types()`) | `/discover-entity-types` — §4.1 depends on the declared vocabulary. |

### Step 1 — Parse the graphml

Counted in a single pass over `graphrag/graph_chunk_entity_relation.graphml`:

- Total node count, total edge count.
- Entity-type histogram (lowercase keys as they appear in the graph; map back to PascalCase vocabulary entries for §4.1).
- Top-N content entities by degree, excluding structural types (`topic` / `user` / `category` / `tag`).
- Out-of-vocab count: non-structural entities whose type isn't in `entity_types.json`'s content types.

Module: `discourse_explorer.derive_query_guide.parse_graphml()` + `_finalize_out_of_vocab()`. Uses stdlib `xml.etree.ElementTree` — no networkx needed for this pass.

### Step 2 — Scan topics/ for category + version coverage

Iterate every `<data-dir>/topics/*.json`:

- Increment `category_name` counter → §4.3 table.
- Every `tag.name` matching `r'^20\d\d[\.․]\d\d$'` → §4.4 table. **Critical:** version tags on Discourse use `․` (U+2024, one-dot leader), not ASCII `.`. The regex accepts both so we don't silently under-count.

Surface the character quirk in §4.4's generated prose — users running `stats sql "... WHERE tag = '2024.06'"` get zero rows unless they use `LIKE` or the actual Unicode.

### Step 3 — Harvest edge verbs

Reuse project helpers — don't reimplement:

- `rel_clusters.harvest_keywords(G)` — returns `{keyword_lowercased: occurrence_count}` across every edge. Requires `networkx.read_graphml()`.
- `rel_clusters._tokenize(keyword)` — stop-word-filtered tokenizer.
- `derive_query_guide._stem()` — light English stemmer (strips `ing/ed/es/s` suffixes) matching the ad-hoc stemmer used in the hand-authored v2 guide so counts stay comparable.

Split the result into:

- **Pin verbs** — any keyword in `config.STRUCTURAL_REL_PINS[*].keywords_csv`. Authored by Pass 1; always present.
- **Content verbs** — the remainder, stemmed + ranked. Top-N feeds §5.

### Step 4 — Read models from config/.env

Via `bootstrap(data_dir)`:

- `rc.default_extraction_model()` → §header "extraction".
- `rc.query_model` (falling back to extraction) → §header "query-time synthesis".

### Step 5 — Compose §1–§5 and §7–§12 from the template

Deterministic string substitution over the extracted facts. Code is in `derive_query_guide.compose_header/compose_section4/compose_section5/compose_sections_7_to_12`. Nothing LLM-authored here; identical inputs → identical output.

### Step 6 — Compose §6 via one LLM call

Prompt is built in `derive_query_guide._build_section6_prompt()`. Structure:

1. Corpus facts: top-15 content entities, strong categories (≥30 topics), version tags, top content verbs.
2. Task: render 2–3 example queries per subsection.
3. Constraint: every entity/category/version named must be in the provided lists.
4. Subsection skeleton: 11 subsections with their recommended mode + a one-line rationale for each.
5. Output format: Markdown only; begin with `## 6. Question library …`; don't renumber or add §6.12+.

Cost: one call. Model: `gpt-4.1-mini` default on OpenAI; `rc.extraction_model` default on Ollama. User can override via `--section6-model`.

Log the full prompt + chosen model + response to the findings log for reproducibility and later audit.

Failure modes:

- **LLM returned Markdown wrapped in a fence** → module strips the fence automatically.
- **LLM didn't start with `## 6.`** → module prepends the heading so §6 is always well-formed.
- **LLM invented a name not in §4** → detectable at verification (step 8). Halt and re-run; temperature 0 on gpt-4.1-mini usually eliminates this.

### Step 7 — Write + apply overwrite policy

If `<data-dir>/QUERY-GUIDE.md` exists, apply the policy the user chose at skill entry:

| Policy | Behavior |
|---|---|
| `backup` (default) | Rename existing → `QUERY-GUIDE.backup-<YYYYMMDD-HHMMSS>.md`, then write fresh. |
| `alongside` | Leave existing alone; write new one as `QUERY-GUIDE-<timestamp>.md`. |
| `fail` | Error out. Used when the skill was invoked with `--overwrite-policy=fail` for CI. |

If the target doesn't exist yet, just write.

Then write the findings log at `<data-dir>/logs/CREATE-QUERY-GUIDE-<timestamp>.md` — include graphml parse stats, entity-type histogram, topic scan summary, §6 prompt + model, elapsed time, final output path + byte count, backup path if any.

### Step 8 — Verify

Automated checks:

- **Size cap.** Warn (don't fail) if output > 20 KB.
- **LLM hallucination spot-check.** Scan §6 for entity names NOT in §4.2's top-15 list (simple substring match on the entity name list). Any hits are flagged in stderr — the user should review and decide whether to re-run.

Manual user-facing confirmation after write:

- Print the output path + byte count.
- Print the backup path (if any).
- Print the findings-log path.
- Suggest running `/query-advisor "test question"` to confirm the advisor now reads the fresh guide.

## Invocation shapes

### Via the skill (recommended)

```
/create-query-guide                          # uses DISCOURSE_DATA_DIR or prompts
/create-query-guide ./data/my-forum          # explicit
```

The skill asks the user to confirm the overwrite policy and §6 model choice, persists the §6 model into `<data-dir>/config/create-query-guide-<ts>.json`, then shells out to the module.

### Direct CLI (no skill)

```bash
# Defaults: backup-then-overwrite, gpt-4.1-mini for §6 on OpenAI
uv run python -m discourse_explorer.derive_query_guide <data-dir>

# Pick a different §6 model
uv run python -m discourse_explorer.derive_query_guide <data-dir> --section6-model gpt-4.1

# Skip §6 entirely (emit the skeleton, no LLM call)
uv run python -m discourse_explorer.derive_query_guide <data-dir> --no-section6

# Print to stdout without writing
uv run python -m discourse_explorer.derive_query_guide <data-dir> --dry-run

# Write alongside as QUERY-GUIDE-<ts>.md (keep existing intact)
uv run python -m discourse_explorer.derive_query_guide <data-dir> --overwrite-policy alongside
```

## Anti-patterns

- **Don't regenerate mid-index.** The graphml may be partially written; §4 counts will be wrong.
- **Don't hand-edit the generated `QUERY-GUIDE.md`.** Your edit will be overwritten on next regen. Put durable notes in a sibling doc.
- **Don't skip the findings log.** It's the only record of what §6 prompt was used — load-bearing if the LLM introduces a regression next run.
- **Don't rename `QUERY-GUIDE.md`.** The advisor hardcodes that filename; renaming breaks the `/query-advisor` integration.

## After a successful run

Suggest (don't auto-run) `/query-advisor "<any example question>"` as a sanity check — the advisor now reads the fresh guide and should cite §4 facts in its rationale. If the advisor still hedges or references stale names, re-run this workflow with `--no-section6` first to rule out an LLM hallucination issue, then diagnose from the findings log.
