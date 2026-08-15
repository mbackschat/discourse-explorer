# CREATE-QUERY-GUIDE run — 2026-04-29

**Data dir:** `/path/to/discourse-explorer/sample/fixtures/seed42-tiny`
**Output:** `QUERY-GUIDE.md` (14,935 bytes)
**Backup:** `QUERY-GUIDE.backup-20260429-021955.md`
**Elapsed:** 18.0s

## Graphml parse

- Nodes: 404
- Edges: 660
- Structural nodes: 78
- Content nodes: 326
- Out-of-vocab content: 80

### Entity-type histogram

| Type | Count |
|---|---:|
| issue | 118 |
| other | 71 |
| guide | 56 |
| topic | 33 |
| mod | 31 |
| tag | 26 |
| character | 22 |
| game | 19 |
| user | 11 |
| category | 8 |
| UNKNOWN | 4 |
| event | 3 |
| location | 1 |
| project | 1 |

### Top content entities by degree

| Entity | Type | Degree |
|---|---|---:|
| Softlock | issue | 20 |
| Admin Guide | guide | 17 |
| Drowned Market | location | 15 |
| Glitchless Category | issue | 13 |
| Localization Choices | issue | 13 |
| Scurvy Harpooner | character | 12 |
| Doubloon SDK | mod | 12 |
| Lighthouse Cipher | issue | 12 |
| Fan Event | event | 11 |
| Crash On Android | issue | 10 |
| Crown Of Brine: Reborn | game | 9 |
| Spectral Cutlass | other | 9 |
| Softlock Bug | issue | 9 |
| Salty Cabin-Boy | character | 8 |
| Soggy Rigger | character | 8 |

## Topic scan

- Topics: 33
- Categories: 8
- Version tags: 0

## Verb harvest

- Unique keyword phrases: 677
- Pin verbs: 9
- Content verbs surfaced in §5: 12

## §6 (question library)

- LLM model: `gpt-4.1-mini`

### §6 prompt (for reproducibility)

```
You are drafting §6 of a GraphRAG query guide for a scraped forum.

The guide is corpus-specific. Below are the real entities, categories,
version tags, and relation verbs present in THIS graph. Every example
query you produce MUST reference only names that appear in these lists —
NEVER invent an entity, category, or version.

## Corpus facts

**Top-connected content entities (name · type · degree):**
  - Softlock (issue, degree 20)
  - Admin Guide (guide, degree 17)
  - Drowned Market (location, degree 15)
  - Glitchless Category (issue, degree 13)
  - Localization Choices (issue, degree 13)
  - Scurvy Harpooner (character, degree 12)
  - Doubloon SDK (mod, degree 12)
  - Lighthouse Cipher (issue, degree 12)
  - Fan Event (event, degree 11)
  - Crash On Android (issue, degree 10)
  - Crown Of Brine: Reborn (game, degree 9)
  - Spectral Cutlass (other, degree 9)
  - Softlock Bug (issue, degree 9)
  - Salty Cabin-Boy (character, degree 8)
  - Soggy Rigger (character, degree 8)

**Strongest categories by topic count:**
  (none with ≥30 topics)

**Version tags:**
  (no version tags)

**Top content-extracted edge verbs (stemmed, count):** support (9), workaround (9), player experience (8), bug occurrence (7), inspiration (6), user report (6), participation (6), gameplay impact (6), commentary (5), appreciation (5)

## Task

For each of the 11 subsections below, render 2–3 example
shell commands in the form:

```bash
uv run discourse-explorer query . \
  "<question>" {mode_flag}
```

where `{mode_flag}` is `--mode local|global|hybrid` when the subsection
specifies a mode (never include `--mode mix` — it's the default).

### Subsections to render

6.1. Troubleshooting / error diagnosis (`local`) — Quote error codes verbatim — embeddings anchor on them.
6.2. Performance investigation (`mix`) — Spans the slow component and what chains onto it.
6.3. Migration / upgrade planning (`global`) — Scope to the strongest version tags from §4.4.
6.4. Architecture understanding (`hybrid`) — Graph-only, no chunks. Shape-of-graph over prose.
6.5. Component / API reference (`local`) — Forum-as-documentation. Anchor on the named component.
6.6. Category-scoped synthesis (`global`) — Pick from §4.3 strongest categories (>100 topics).
6.7. How-to / pattern recognition (`mix`) — LLM strength: generalize across retrieved chunks.
6.8. Comparative / trade-off (`mix`) — Needs both entities to have good degree (§4.2).
6.9. Security / auth / compliance (`local`) — Scope to auth-related entities if present.
6.10. Community gaps & unresolved questions (`global`) — Follow up with `stats --path . unanswered`.
6.11. Onboarding / learning path (`mix`) — Synthesize across experienced posters' recommendations.

## Output format

Emit the full §6 section as Markdown, starting with:

## 6. Question library (tailored to THIS corpus)

Include a 3-sentence intro that (a) says examples use §4.2 entities and
§4.3–4.4 scope so retrieval lands, (b) teaches "chain broad → narrow"
(start with a `global` category question, harvest named concepts, pivot
to `local`), (c) teaches "scope tightens retrieval" (add at least one
entity/category/version to every question).

Then each subsection as a `### {sid} {title} ({mode})` heading, one
one-line rationale, then the code block.

Do NOT add §6.12 or renumber. Do NOT include anything after the last
subsection. Emit valid Markdown only — no commentary, no explanation.

```
