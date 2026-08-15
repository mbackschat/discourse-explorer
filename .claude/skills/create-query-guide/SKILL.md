---
name: create-query-guide
description: >-
  Generate a corpus-grounded QUERY-GUIDE.md for a scraped + indexed Discourse data dir. Parses
  graphml, topic JSON, and entity_types.json; derives scale, top-15 entities by degree,
  per-category / per-version coverage, and edge-verb frequencies; LLM-tailors the question library
  (§6) against those facts. The guide is what `/query-advisor` reads to constrain routing —
  without it, the advisor recommends abstract modes that may reference categories / versions /
  components not in the graph. Triggers: "create query guide", "regenerate query guide", "refresh
  query guide", "update QUERY-GUIDE.md", "query guide for this corpus". Run after
  `/index-and-embed` finishes (numbers drift with every re-index).
---

# Create query guide

## Host compatibility

Before executing this skill, read [`../HOST-COMPATIBILITY.md`](../HOST-COMPATIBILITY.md). Operations such as “ask the user,” “invoke the skill,” and “delegate execution” use the host bindings defined there.

**Goal:** produce `<data-dir>/QUERY-GUIDE.md`, sourced entirely from the data dir. §1–§5 + §7–§12 are template-substituted from deterministic extractions (graphml parse, topic scan, verb harvest, `config/.env`). §6 (question library) is LLM-authored against those facts — the subsection skeletons are hardcoded; the LLM only picks *which* real entities / categories / versions to render in each example query.

Cost: ~$0.05 on OpenAI `gpt-4.1-mini` for §6 (one call). Runtime: ~30s.

## Authoritative runbook

**Read `docs/workflows/CREATE_QUERY_GUIDE.md` in full before running.** That document has the step-by-step protocol (step 0 prerequisites, steps 1–7 extract-compose-write, step 8 verify), failure modes, and the hallucination spot-check. This SKILL only names the user-facing decisions and routes you there.

## Resolve the data directory first

Before anything else, settle which data dir this run operates on. Even though the run is read-only against the graph, the write target (`<data-dir>/QUERY-GUIDE.md`) is per-forum, and overwriting a hand-edited guide without confirmation is a sharp edge. Source priority:

1. **Skill argument.** If the invocation included a path (e.g. `/create-query-guide ./data/my-forum`), treat that as authoritative. Resolve with `Path(arg).expanduser().resolve()` and confirm `<path>/topics/` + `<path>/graphrag/` exist.
2. **Ask the user** when no path was given. Enumerate candidate data dirs and let the user pick:
   - Run `ls -d ./data/*/ 2>/dev/null` to find subdirs containing `topics/`.
   - Probe `DISCOURSE_DATA_DIR` via `uv run python -c "from discourse_explorer.config import bootstrap; print(bootstrap(None).data_dir)"`. If it succeeds, mark that candidate as *(Recommended — from DISCOURSE_DATA_DIR)*.
   - Present each candidate as an option; "Other" is auto-added.
   - Silently falling back to `DISCOURSE_DATA_DIR` is tempting because this run is cheap, but a wrong forum still loses any hand-edits to that forum's guide. One confirmation click is cheaper than the recovery.

Pass the chosen path through the rest of the skill. All `<data-dir>` references below resolve to this value.

## Prerequisites to check before running

Each halts and routes to the skill that fixes it:

1. `<data-dir>/topics/*.json` exists → else the corpus was never scraped. Halt and suggest running the scraper (`uv run discourse-explorer scrape <URL> --output <data-dir>`).
2. `<data-dir>/graphrag/graph_chunk_entity_relation.graphml` exists → else halt and route to `/index-and-embed`. The §4 histograms and §5 verb table both require a built knowledge graph.
3. `<data-dir>/config/entity_types.json` exists and `config.load_entity_types()` accepts it → else halt and route to `/discover-entity-types`. §4.1 splits entities into structural + content types using this vocabulary; without it the "out of vocab" percentage is meaningless.

Don't try to half-run with a partial input. The module raises `ConfigError` in each case; surface the message verbatim.

## Overwrite behavior

If `<data-dir>/QUERY-GUIDE.md` already exists, ask the user which policy to apply:

| Option | Behavior | When to pick |
|---|---|---|
| **Backup-then-overwrite** (Recommended default) | Rename existing → `QUERY-GUIDE.backup-<YYYYMMDD-HHMMSS>.md`, then write fresh. Matches the `graphrag.bak/` pattern in `/index-and-embed`. | Most runs. Cheap insurance against a hand-edit you forgot about. |
| **Write alongside** | Leave existing intact; write the new one as `QUERY-GUIDE-<timestamp>.md`. | When you want an A/B comparison before committing to the new version. Creates a pile of timestamped siblings over time — clean up manually. |
| **Cancel** | Abort without writing. | You changed your mind. |

Pass the choice to the module via `--overwrite-policy=backup|alongside` (no need to map "cancel" — just don't invoke the module).

If no prior guide exists, skip the question and write directly.

## Remember run settings across invocations

At the start of every run (immediately after resolving the data dir, before asking about policy), **gather all available values for each tunable from up to four sources** — the same four-source pattern `/index-and-embed` uses:

1. **Recommended** — `gpt-4.1-mini` on OpenAI, `rc.extraction_model` on Ollama.
2. **Codebase default** — same as recommended; no divergence for this skill.
3. **Current `<data-dir>/config/.env`** — `EXTRACTION_MODEL` (proxies for "what the user has been using"). No dedicated `SECTION6_MODEL` env; the section6 model is skill-local, not persisted to `.env`.
4. **Last skill-run JSON** — the `section6_model` value persisted by the most recent `/create-query-guide` invocation. Glob `<data-dir>/config/create-query-guide-*.json`, lexicographic sort, take the last.

The only tunable worth surfacing is **`section6_model`**. Ask the user if any of the four sources disagree; otherwise accept the recommendation silently.

After the user has answered (or accepted the default), write the JSON before invoking the module:

```
<data-dir>/config/create-query-guide-<YYYYMMDD-HHMMSS>.json
```

Use the same timestamp that will appear in the findings log. Shape:

```json
{
  "skill": "create-query-guide",
  "timestamp": "20260424-091533",
  "settings": {
    "section6_model": "gpt-4.1-mini",
    "overwrite_policy": "backup"
  }
}
```

Semantic values only, not host-specific option labels.

## Invoke the module

```bash
uv run python -m discourse_explorer.derive_query_guide "<data-dir>" \
  --section6-model "<chosen-model>" \
  --overwrite-policy "<chosen-policy>"
```

The module prints per-step progress (`[1/4] Parsing graphml`, etc.) to stdout. Runtime is short enough that a foreground `Bash` call is fine — no background + Monitor pattern needed (unlike `/index-and-embed`).

If the user chose "Skip §6" (off-menu, but occasionally useful for debugging), add `--no-section6`. That path skips the LLM call and emits a skeleton placeholder — useful if OpenAI is down or the user just wants to re-verify the deterministic sections.

## Findings log

The module writes `<data-dir>/logs/CREATE-QUERY-GUIDE-<timestamp>.md` automatically. After the run, read it back and surface the load-bearing numbers to the user:

- Node / edge counts + top entity-type histogram rows.
- Top 5 content entities by degree.
- Total topics + number of categories + number of version tags.
- §6 model used + whether the LLM hallucination spot-check flagged anything.
- Final output path + byte count. Warn visibly if > 20 KB.

Don't copy the full log to chat — it's on disk for the user; one summary paragraph is enough.

## Post-run handoff

After a successful write, suggest (don't auto-run):

> Next: `/query-advisor "<any real question about this corpus>"` — the advisor now reads the fresh guide and should cite §4 facts in its rationale. If you see stale names or the advisor still hedges, check the findings log for the §6 LLM prompt + response.

If the hallucination spot-check in step 8 flagged entity names in §6 that aren't in §4.2's top-15, surface that in the final summary with a concrete list and suggest re-running the skill (LLM variance) or switching to `--section6-model gpt-4.1` (less variance on larger models).

## Anti-patterns

- **Don't regenerate while `/index-and-embed` is still running.** The graphml may be partially written; §4 counts will be wrong and the guide will need another regen. Wait for the index to finish.
- **Don't hand-edit the generated `QUERY-GUIDE.md`.** §1–§5 + §7–§12 are template output; your edit gets overwritten on the next run. Put durable notes in a sibling file (e.g. `QUERY-NOTES.md`) the skill never touches.
- **Don't run if graphml mtime is older than the existing guide's mtime.** That's a sign nothing has changed since the last regen; you'd pay ~$0.05 + write a bit-identical §1–§5. Check and short-circuit politely: "Graph hasn't changed since last guide — nothing to regenerate."
- **Don't rename `QUERY-GUIDE.md` to anything else.** `/query-advisor` hardcodes that exact filename.
- **Don't paste OpenAI keys into the chat.** If `OPENAI_API_KEY` is missing, halt with the one-line instruction to edit `<data-dir>/config/.env` directly, same as the sibling skills.
