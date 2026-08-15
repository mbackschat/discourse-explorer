---
name: discover-entity-types
description: >-
  Sample the scraped Discourse corpus, distill an entity-type vocabulary with an LLM, and guide
  the user through review + hand-tuning. Use when the user wants to discover, rediscover, or
  re-derive the entity-type vocabulary (triggers: "discover types", "run discovery", "rediscover
  vocabulary", "find entity types"). Run before --index --clear on a new corpus.
---

# Discover entity types

## Host compatibility

Before executing this skill, read [`../HOST-COMPATIBILITY.md`](../HOST-COMPATIBILITY.md). Operations such as “ask the user,” “invoke the skill,” and “delegate execution” use the host bindings defined there.

**Goal:** produce the content-type portion of `<data-dir>/config/entity_types.json` via LLM-driven corpus sampling. The structural portion (`User`, `Topic`, `Category`, `Tag`) is preserved automatically — never edit it by hand.

**When to run:** fresh scrape of a new forum, substantial content shift, or ≥10% `Other` bucket in an existing graph.

## Authoritative runbook

**Read `docs/workflows/DISCOVER_ENTITY_TYPES.md` in full before running.** That document is the step-by-step protocol with model recommendations, cost/time estimates, script-flag reference, and anti-patterns. This SKILL only names prerequisites and routes you there.

## Resolve the data directory first

Before anything else, settle which data dir this run operates on. Source priority:

1. **Skill argument.** If the invocation included a path (e.g. `/discover-entity-types ./data/my-forum` or a path in the natural-language message), treat that as authoritative. Resolve with `Path(arg).expanduser().resolve()` and confirm `<path>/topics/` exists.
2. **Ask the user.** If no path was given, enumerate candidate data dirs and let the user pick:
   - Run `ls -d ./data/*/ 2>/dev/null` (or look at the CWD's `data/`) to find subdirs with a `topics/` inside.
   - Also probe `DISCOURSE_DATA_DIR` via `uv run python -c "from discourse_explorer.config import bootstrap; print(bootstrap(None).data_dir)"`. If it succeeds, that's the recommended option.
   - Present each candidate as an option; "Other" is auto-added by the tool for a custom path.
   - Do not silently fall back to `DISCOURSE_DATA_DIR` without showing the user which one you chose — one confirmation click is cheap; a run on the wrong forum is expensive.

Pass the chosen path through the rest of the workflow. All subsequent references to `<data-dir>` below resolve to this value.

## Prerequisites to check before doing anything

1. `<data-dir>/topics/*.json` exists (scraped data present).
2. **Provider selection.** Probe `bootstrap(None).is_openai` (`True` iff `OPENAI_API_KEY` resolves in the layered `.env` chain). Branch:
   - **Key present** → OpenAI provider. Proceed.
   - **Key missing** → ask the user which provider to use:
     - *Option A: set an OpenAI key in `<data-dir>/config/.env` and re-invoke.* Halt the skill with a one-line instruction telling the user the exact key to add (`OPENAI_API_KEY=sk-...`) and the file path. **Do not accept the key pasted into the chat** — it ends up in logs/transcripts. Require the user to edit the file themselves, then re-run the skill.
     - *Option B: fall back to Ollama.* Probe `OLLAMA_HOST` (default `http://localhost:11434`) reachability with `curl -s -o /dev/null -w '%{http_code}' $OLLAMA_HOST/api/tags` before continuing. If not reachable, halt with instructions to start the daemon (`ollama serve`) and re-invoke.
   - Persist the decision into `<data-dir>/config/.env` so later runs and other skills see the same provider without ambiguity.
3. If a prior `<data-dir>/discovery_result.json` exists, offer `--show-artifact` (no LLM cost) so the user can review before spending again.

## Write a findings log as you go

**Create a timestamped markdown log at the start of the run**, append to it as you progress, finalize when done. Path:

```
<data-dir>/logs/DISCOVER_ENTITY_TYPES-$(date +%Y%m%d-%H%M%S).md
```

(Create `logs/` under the data dir if it doesn't already exist.)

Capture, in rough order:

1. **Context** — provider, model, sample size chosen, whether this is a fresh run or a refinement of a prior artifact.
2. **Phase 1 structural profile** — topic/post counts, top categories/tags/users. The no-LLM baseline.
3. **Phase 2 raw labels with counts** — the full distinct-label list, not just top 15. Future sessions use this to audit whether the distillation missed something.
4. **Phase 3 distillation output** — machine-proposed content types + the rationale string the LLM produced.
5. **User's hand-tuning decisions** — each add/drop with the *why* recorded (e.g. "added Version because SoftwareVersion=42 dominated but distillation dropped it").
6. **Final content-type list** — exactly as written to `<data-dir>/config/entity_types.json`.
7. **Anomalies or anti-pattern encounters** — and how they were resolved (or deferred).

**Style:** pedagogical, not a stack trace. Explain *why* decisions were made, not just *what* was decided. A future session should be able to reconstruct the reasoning without re-asking questions.

## Remember run settings across invocations

At the start of every run (immediately after resolving the data dir, before asking the user), **gather all available values for each tunable from up to four sources**:

1. **Recommended** — what this SKILL / the runbook suggests for a typical corpus (e.g. `sample_size=300`, `gpt-4.1-mini`).
2. **Codebase default** — the CLI flag default if unset (usually the same as "Recommended" for this skill, but not always).
3. **Current `<data-dir>/config/.env`** — the `OPENAI_API_KEY` / `EXTRACTION_MODEL` state right now, as parsed by `bootstrap()`. This determines the provider and the model that would run with no CLI flags.
4. **Last skill-run JSON** — the value persisted by the most recent `/discover-entity-types` invocation. Glob `<data-dir>/config/discover-entity-types-*.json`, lexicographic sort (the `YYYYMMDD-HHMMSS` suffix makes this chronological), take the last.

**Always ask the user for every tunable** — even if all four sources agree. Never silently apply.

For each user question, build the option list by:

- Collecting the values that exist across the four sources.
- Deduplicating — identical values collapse into one option, with its label listing every source that produced it (e.g. `Use 300 topics (matches last skill-run)`).
- Marking the runbook-recommended option with `(Recommended)` in the label.
- If a source has no distinct value to contribute (e.g. no prior JSON, or `sample_size` isn't captured in `.env`), skip that source — do not pad with a phantom option.

The resulting question typically has 1–3 concrete options plus the auto-added `Other` for a custom value. Skill-only knobs (`sample_size`, `top`, `no_distill`) have at most two distinct sources (recommended + last JSON). Provider/model can have up to all four.

Fields captured for this skill:

| Field | Source | Example |
|---|---|---|
| `provider` | "OpenAI provider" answer | `"OpenAI"` or `"Ollama"` |
| `model` | Recommended model for that provider | `"gpt-4.1-mini"` |
| `sample_size` | "Sample plan" answer | `300` |
| `top` | Top-N raw labels CLI flag | `60` |
| `no_distill` | Whether the Phase 3 distillation step is skipped | `false` |

After the user has answered all pre-discovery questions (before the `uv run` launches), write the JSON:

```
<data-dir>/config/discover-entity-types-<YYYYMMDD-HHMMSS>.json
```

Use the same timestamp as the findings log so the pair is obvious. Shape:

```json
{
  "skill": "discover-entity-types",
  "timestamp": "20260423-044603",
  "settings": {
    "provider": "OpenAI",
    "model": "gpt-4.1-mini",
    "sample_size": 300,
    "top": 60,
    "no_distill": false
  }
}
```

Store **semantic values**, not host-specific option labels — labels change, semantic values don't. Don't store post-discovery decisions (which types to add back, keep `Event`, etc.) — those are corpus-specific and shouldn't pre-seed a future run's options.

## Entry protocol

Follow steps 1–7 of `docs/workflows/DISCOVER_ENTITY_TYPES.md`. Ask the user at every user-facing decision point: model pick, sample size, tweak-vs-accept on the discovered list, content-type color assignments. Halt if prerequisites fail rather than improvising.

`discover_types` writes directly to `<data-dir>/config/entity_types.json` — **no source-code edits are ever required**. For hand-tuning after the run, edit that JSON file directly after confirming the changes with the user when iterating interactively.

**Always launch discovery with `PYTHONUNBUFFERED=1` and `python -u`, and redirect to the log file directly (no `tee`).** Python block-buffers stdout when piped, which hides progress for minutes. Example:

```bash
PYTHONUNBUFFERED=1 uv run python -u -m discourse_explorer.discover_types \
  <data-dir> --model gpt-4.1-mini --sample-size 300 --top 60 \
  > <data-dir>/logs/DISCOVER_ENTITY_TYPES-<ts>.stdout.txt 2>&1
```

## Anti-patterns

- Don't use the distilled list verbatim without reviewing raw labels — distillation drops important types sometimes (we've seen this with `Version` on this project).
- Don't edit `discourse_explorer/query.py` or `discourse_explorer/visualize.py` to change the vocabulary. The vocabulary lives in JSON; source edits cause drift.
- Don't remove or rename the four structural types (`User`, `Topic`, `Category`, `Tag`); they're load-bearing in `_topic_to_custom_kg` and `config.py` validates on load.
- Don't run this skill if no scraped data exists — fail early, direct user to the scraper.
