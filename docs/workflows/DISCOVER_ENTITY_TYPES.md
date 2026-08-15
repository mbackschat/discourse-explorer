# Workflow: Discover & Finalize the Entity-Type Vocabulary

**Audience:** Claude Code, Codex, and other skill hosts. Read the shared [host compatibility contract](../../.claude/skills/HOST-COMPATIBILITY.md) before using this runbook. This guide tells you how to lead the user through vocabulary discovery and selection before a knowledge-graph re-index.

## Purpose

Produce a final entity-type vocabulary in `<data-dir>/config/entity_types.json` that constrains LightRAG's entity extraction. A good vocabulary makes extraction consistent, kills taxonomy drift, and keeps the visualizer in sync without hand-maintained `_TYPE_MAP` lists.

## When to run this workflow

- Fresh scrape of a new forum.
- Existing forum whose content focus has shifted substantially (new product, new community, etc.).
- Whenever the `Other` bucket in `graph.html` looks large (>10% of nodes).

Run this **before** `INDEX_AND_EMBED.md`. The indexing step writes the vocabulary into the graph; changing it after requires `--clear` and re-indexing.

## Guardrails

- **Never silently change the vocabulary without user approval.** The user is the final authority.
- **`<data-dir>/config/entity_types.json` is the single source of truth.** `query.py` and `visualize.py` read it at startup. No source edits required. Never edit `discourse_explorer/query.py` or `discourse_explorer/visualize.py` to change the vocabulary — the JSON is authoritative.
- **Distillation can silently drop important types.** The LLM sometimes interprets "structural types handled separately" too broadly and drops things like `Version` even when they're the #1 raw label. Always review raw labels, not just the distilled list.
- **Structural types are fixed and validated.** `User, Topic, Category, Tag` must be present with `structural: true`. `config.py`'s `load_entity_types()` raises on load if they're missing or misflagged. Don't rename them.

## The flow (step by step)

### Step 1 — Confirm LLM + API key with the user

Ask the user to choose. Proposals:

| Task | Recommended | Why |
|---|---|---|
| Discovery sampling | `gpt-4.1-mini` (matches extraction default) | Cleaner JSON output than gpt-4o-mini. ~$0.05–$0.50 total. Avoid gpt-5-series — reasoning overhead doesn't help label classification and slows the run. |
| Distillation | same model (one extra call) | No reason to switch models mid-flow |

Confirm `OPENAI_API_KEY` is set in `<data-dir>/config/.env`:

```bash
uv run python -c "
from discourse_explorer.config import bootstrap
rc = bootstrap(None)  # reads DISCOURSE_DATA_DIR from project-root .env
print('provider:', 'OpenAI' if rc.is_openai else 'Ollama')
print('key set :', bool(rc.openai_api_key))
"
```

Do not paste keys into the chat — have the user put it in `<data-dir>/config/.env` and re-run.

### Step 2 — First pass: small sample (exploration)

Start small to verify the pipeline works and give the user an initial vocabulary draft:

```bash
uv run discourse-explorer discover-types <data-dir> --model gpt-4.1-mini --sample-size 30 --top 30
```

The script runs three phases:
1. Structural profile (no LLM): counts topics, posts, categories, tags, users.
2. LLM-driven content-type discovery: samples N topics, asks the LLM what kinds of entities appear.
3. Distillation: condenses the raw labels into 4–6 content types — and writes the result to `<data-dir>/config/entity_types.json`, merging with any existing structural entries.

It also writes `<data-dir>/discovery_result.json` for audit/review.

### Step 3 — Full run: larger sample (stable vocabulary)

If the small run produced a reasonable draft, run a bigger sample to stabilize:

```bash
uv run discourse-explorer discover-types <data-dir> --model gpt-4.1-mini --sample-size 300 --top 60
```

**Cost benchmark (300 samples on gpt-4.1-mini):** ~$0.50, ~2 minutes.

### Step 4 — Review with the user

Show the user:
- The top N raw labels with their vote counts (from stdout).
- The proposed content types from distillation (now visible in `<data-dir>/config/entity_types.json`).
- Your analysis: which labels cluster under which proposed type, and what's falling to `Other`.

Example format:

```markdown
| Cluster | Raw labels (votes) | Covered by proposed vocab? |
|---|---|---|
| Modeling | DocumentModel(42), FormModel(38), Model(17) ≈ 150 | Yes — `Model` |
| Versions | SoftwareVersion(42), Release(8) ≈ 58 | NO — distillation dropped this, flag to user |
| Issues   | ErrorMessage(30), BugReport(21) ≈ 111 | Yes — `Issue` |
```

**Flag any strong raw-label clusters that the distillation dropped.** Your job is to catch these — distillation is not authoritative.

### Step 5 — Let the user tweak

Ask the user, offering options like:
- Keep the discovered vocabulary as-is.
- Add/drop specific types (list the candidates from Step 4).
- Re-run with a larger sample.

For any type the user chooses to add or drop, edit `<data-dir>/config/entity_types.json` directly. The structure is:

```json
{
  "version": 1,
  "types": [
    {"name": "User",      "color": "#E57373", "structural": true},
    {"name": "Topic",     "color": "#4FC3F7", "structural": true},
    {"name": "Category",  "color": "#FFD54F", "structural": true},
    {"name": "Tag",       "color": "#BA68C8", "structural": true},
    {"name": "Model",     "color": "#81C784", "structural": false},
    {"name": "Issue",     "color": "#F06292", "structural": false}
  ]
}
```

- Only content types (`structural: false`) should be added or dropped. The four structural entries must remain.
- Pick distinct colors for new content types. Material Design palettes work well.

**Record the rationale** for any hand-edit in the per-run findings log under `<data-dir>/logs/` so future re-runs have context.

### Step 6 — Verify the vocabulary loads cleanly

Run this before proceeding to indexing. `load_entity_types()` validates that the structural types are present and correctly flagged:

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

Expected: the four structural types list + the content types the user finalized. If `config.py` raises, the error message points at the fix.

## Re-using or re-inspecting past runs

`discover_types.py` persists every run to `<data-dir>/discovery_result.json`. Review anytime without re-spending:

```bash
# Print the cached summary — top labels, distilled types, rationale
uv run discourse-explorer discover-types <data-dir> --show-artifact --top 60
```

Useful flags:
- `--show-artifact` — read the prior run's JSON and print. No LLM cost.
- `--no-distill` — run Phase 1+2 only, skip the Phase 3 LLM call. Useful for cheap sample iteration.
- `--top N` — control how many top raw labels to print (any mode).

## Commands reference

```bash
# Cheap first draft (~$0.05)
uv run discourse-explorer discover-types <data-dir> --model gpt-4.1-mini --sample-size 30

# Full run (~$0.50, ~2 min)
uv run discourse-explorer discover-types <data-dir> --model gpt-4.1-mini --sample-size 300 --top 60

# Review the cached result (free)
uv run discourse-explorer discover-types <data-dir> --show-artifact --top 60

# Phase 1+2 only (no distillation cost)
uv run discourse-explorer discover-types <data-dir> --sample-size 100 --no-distill
```

## Anti-patterns (don't do these)

- **Don't use the distilled list verbatim without reviewing raw labels.** Distillation drops important types sometimes.
- **Don't edit `discourse_explorer/query.py` or `discourse_explorer/visualize.py`.** The vocabulary is in JSON; source edits cause drift.
- **Don't remove or rename the structural types** (`User`, `Topic`, `Category`, `Tag`). They're load-bearing for `_topic_to_custom_kg` and `config.py` validates on load.
- **Don't re-add the old `_TYPE_MAP` + 10 raw-type lists.** That was the problem we fixed — the constrained vocabulary makes it unnecessary.

## Handoff to indexing

Once the JSON vocabulary is final and loads cleanly, proceed to `INDEX_AND_EMBED.md` for the destructive re-index.
