# Query guide — knowledge graph

Tailored reference for asking useful questions against the forum GraphRAG. Numbers reflect the current index, not architecture in the abstract.

**Scale:** 33 topics indexed · 404 nodes · 660 edges.
**Models** (from `config/.env`): extraction `gpt-4.1-mini` · query-time synthesis `gpt-4.1-mini`.
**Snapshot:** numbers reflect the 2026-04-29 index state. Regenerate after any re-index — see §12.

## 1. What retrieval will + won't do

- **Will** answer *"tell me about X"*, *"summarize what's discussed around Y"*, *"how does X relate to Z"* — if X/Y/Z are named entities or themes in the graph.
- **Won't** count, rank by frequency, enumerate exhaustively, do time-series. Retrieval is `top_k`-bounded; the LLM never sees the whole corpus.

For *how many / which is most / rank*, use `stats` first.

## 2. Pick a mode

| Question shape | Mode |
|---|---|
| "how many / which has most / list all …" | **`stats`** (graph can't count) |
| Centered on one named entity | **`local`** |
| Broad theme, cause/effect across topics | **`global`** |
| Structural reasoning about how components relate | **`hybrid`** (graph-only) |
| Mixed / unsure | **`mix`** (default) |
| Sanity check vs. graph value | **`naive`** (pure vector) |

## 3. Commands

```bash
uv run discourse-explorer query . "your question"                  # mix (default)
uv run discourse-explorer query . "question" --mode local          # entity-anchored
uv run discourse-explorer query . "question" --mode global         # theme / cause-effect
uv run discourse-explorer query . "question" --mode hybrid         # graph-only
uv run discourse-explorer query . "question" --mode naive          # pure vector
uv run discourse-explorer stats --path . <subcmd>                  # counts / SQL / search
```

## 4. What's actually in this graph

### 4.1 Entity vocabulary

Structural (auto-emitted from topic JSON): **User, Topic, Category, Tag**.

Content (LLM-extracted, from `config/entity_types.json`):

| Type | Nodes |
|---|---:|
| Issue | 118 |
| Guide | 56 |
| Mod | 31 |
| Character | 22 |
| Game | 19 |

**~25% of non-structural entities fell outside this 5-item vocab** — `other` (71), `UNKNOWN` (4), `event` (3), `location` (1), plus smaller buckets. They retrieve normally; they just won't respond to `entity_type=X` filters in viz / SQL.

### 4.2 Best-connected content entities (top 15 by degree)

High degree = richer retrieval context in `local` mode. Entities below degree ~10 exist in the graph but retrieve thinly.

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

### 4.3 Coverage by category

Query only where there's content. Weak categories (1–2 topics) will hedge badly — don't scope there.

| Category | Topics |
|---|---:|
| Bug Reports | 10 |
| Announcements | 5 |
| Show & Tell | 5 |
| Help & Hints | 4 |
| Translations & Localization | 3 |
| Speedruns & Challenges | 3 |
| Staff | 2 |
| General | 1 |

## 5. Relation vocabulary (what verbs the graph supports)

Extracted edge keywords from the graphml (677 unique phrases), stemmed + frequency-ranked. Lets you judge which question verbs will retrieve well.

**Structural edges (Pass 1, always present):** `posted` · `authored` · `participated` · `tagged` · `tag` · `labeled`. Useful for "who posted in …" but `stats` usually answers faster.

**Content-extracted edges (top by frequency):**

| Verb (stemmed) | Count |
|---|---:|
| support | 9 |
| workaround | 9 |
| player experience | 8 |
| bug occurrence | 7 |
| inspiration | 6 |
| user report | 6 |
| participation | 6 |
| gameplay impact | 6 |
| commentary | 5 |
| appreciation | 5 |
| communication | 5 |
| lore interest | 5 |

**Sparsely represented, avoid framing around these:** ownership ("who owns …"), succession ("who introduced / deprecated …"), performance metrics ("how fast is …"). Re-frame as composition or problem-state questions instead.

## 6. Question library (tailored to THIS corpus)

These example queries leverage §4.2 entities and §4.3–4.4 scope settings to ensure retrieval hits relevant content. We demonstrate a "chain broad → narrow" approach: start with a wide global category question to identify key entities, then pivot to a focused local question on those entities for detailed insights. This approach also shows how scoping tightens retrieval by including at least one entity, category, or version in every query for precision.

### 6.1 Troubleshooting / error diagnosis (local)

Focus on quoted issue names or error codes to anchor embeddings and pinpoint relevant diagnostic discussions.

```bash
uv run discourse-explorer query . \
  "What troubleshooting advice is given for the Softlock Bug issue?" --mode local
```

```bash
uv run discourse-explorer query . \
  "How do players report workarounds for Crash On Android errors?" --mode local
```

```bash
uv run discourse-explorer query . \
  "Are there diagnostic steps available for the Lighthouse Cipher issue?" --mode local
```

### 6.2 Performance investigation (mix)

Explore performance bottlenecks and the chain of gameplay impact spanning from a slow component.

```bash
uv run discourse-explorer query . \
  "What performance issues affect player experience for the Doubloon SDK mod?" 
```

```bash
uv run discourse-explorer query . \
  "How does the Softlock issue influence overall gameplay impact?" 
```

```bash
uv run discourse-explorer query . \
  "What workarounds mitigate bug occurrence tied to Localization Choices?" 
```

### 6.3 Migration / upgrade planning (global)

Scope to version tags where applicable; here none exist, so focus globally on relevant planning topics.

```bash
uv run discourse-explorer query . \
  "What community commentary exists on upgrading Crown Of Brine: Reborn?" --mode global
```

```bash
uv run discourse-explorer query . \
  "Which issues do players encounter during migration related to the Doubloon SDK mod?" --mode global
```

```bash
uv run discourse-explorer query . \
  "What user reports highlight challenges in migrating Localization Choices?" --mode global
```

### 6.4 Architecture understanding (hybrid)

Use graph-only queries to clarify relations, avoiding prose from document chunks.

```bash
uv run discourse-explorer query . \
  "How is the relationship between Softlock and Softlock Bug depicted in the graph?" --mode hybrid
```

```bash
uv run discourse-explorer query . \
  "What architecture insights emerge from connections between Admin Guide and Glitchless Category?" --mode hybrid
```

```bash
uv run discourse-explorer query . \
  "Describe the network structure around Scurvy Harpooner in relation to gameplay impact." --mode hybrid
```

### 6.5 Component / API reference (local)

Anchor queries on named components or mods as found in forum documentation.

```bash
uv run discourse-explorer query . \
  "What API references exist for the Doubloon SDK mod?" --mode local
```

```bash
uv run discourse-explorer query . \
  "Are there documented functions or features for the Spectral Cutlass component?" --mode local
```

```bash
uv run discourse-explorer query . \
  "Which Admin Guide sections reference the Softlock issue directly?" --mode local
```

### 6.6 Category-scoped synthesis (global)

Since no categories exceed 100 topics, global queries still include issue or entity scopes for synthesis.

```bash
uv run discourse-explorer query . \
  "Synthesize community support trends for the Lighthouse Cipher issue over time." --mode global
```

```bash
uv run discourse-explorer query . \
  "Summarize appreciation and commentary related to Fan Event discussions." --mode global
```

### 6.7 How-to / pattern recognition (mix)

Leverage LLM strengths to generalize from multiple retrieved discussions for procedural guidance.

```bash
uv run discourse-explorer query . \
  "What common workaround patterns address Softlock issues reported by players?" 
```

```bash
uv run discourse-explorer query . \
  "How do users typically participate in resolving Localization Choices problems?" 
```

```bash
uv run discourse-explorer query . \
  "Which strategies improve gameplay impact when dealing with Crash On Android?" 
```

### 6.8 Comparative / trade-off (mix)

Compare high-degree entities or issues to extract nuanced trade-offs.

```bash
uv run discourse-explorer query . \
  "Compare player experiences between the Softlock issue and Glitchless Category challenges." 
```

```bash
uv run discourse-explorer query . \
  "What are the trade-offs in bug occurrence between Lighthouse Cipher and Localization Choices?" 
```

```bash
uv run discourse-explorer query . \
  "How does gameplay impact differ for Doubloon SDK versus Crown Of Brine: Reborn?" 
```

### 6.9 Security / auth / compliance (local)

No explicit auth entities present; scope to related issue discussions if relevant.

```bash
uv run discourse-explorer query . \
  "Are there security concerns mentioned in relation to the Doubloon SDK mod?" --mode local
```

```bash
uv run discourse-explorer query . \
  "Does the Admin Guide address any compliance requirements for the Glitchless Category?" --mode local
```

### 6.10 Community gaps & unresolved questions (global)

Detect unanswered questions on key issues or events.

```bash
uv run discourse-explorer query . \
  "What unresolved questions remain about the Softlock Bug?" --mode global
```

```bash
uv run discourse-explorer query . \
  "Which community gaps exist for the Fan Event participation?" --mode global
```

```bash
uv run discourse-explorer query . \
  "Show stats --path . unanswered" --mode global
```

### 6.11 Onboarding / learning path (mix)

Aggregate advice from experienced posters to guide newcomers.

```bash
uv run discourse-explorer query . \
  "What learning path is recommended for new players tackling the Scurvy Harpooner character?" 
```

```bash
uv run discourse-explorer query . \
  "Which Admin Guide sections do seasoned users suggest to understand Softlock issues?" 
```

```bash
uv run discourse-explorer query . \
  "What community advice aids onboarding around the Doubloon SDK mod?" 
```

## 7. Stats + query recipes (for top-N)

```bash
# Which categories have most topics → synthesize over the leaders
uv run discourse-explorer stats --path . sql \
  "SELECT category, COUNT(*) n FROM topics WHERE category != 'Unknown' GROUP BY category ORDER BY n DESC LIMIT 5"

# Highest-reply topics → pick one → deep-dive
uv run discourse-explorer stats --path . sql \
  "SELECT title, posts_count, category FROM topics ORDER BY posts_count DESC LIMIT 10"
uv run discourse-explorer query . \
  "Summarize the thread titled '<title>' — problem, debate, resolution."

# Unanswered / open problems (built-in)
uv run discourse-explorer stats --path . unanswered
```

## 8. When answers feel weak — tuning knobs

Env vars in `<data-dir>/config/.env` (or inline: `TOP_K=100 uv run ...`).

| Knob | Default | Raise when | Lower when |
|---|---:|---|---|
| `TOP_K` | 60 | Answers cite too few topics | Context too large |
| `CHUNK_TOP_K` | 20 | Need more direct quotes | Token budget ceiling |
| `MAX_TOTAL_TOKENS` | 30000 | Truncation visible in answers | Cost reduction |
| `MAX_ENTITY_TOKENS` | 6000 | Entity-heavy truncation | Rebalance to chunks |
| `MAX_RELATION_TOKENS` | 8000 | Relation-heavy truncation | Rebalance to entities |

Model swap: `QUERY_MODEL=<id>`. Reasoning-tier (gpt-5-series) worth slowness for synthesis; `gpt-4.1-mini` fine for narrow lookup.

**Biggest remaining quality lever:** enable `RERANK_PROVIDER=jina` (or cohere / ali). See MANUAL §Rerank.

## 9. Blind spots on THIS graph

- **25% of content entities** landed outside the declared vocabulary (see §4.1). They retrieve normally; they just won't filter on `entity_type=X`.
- **Extraction quality is topic-dependent.** Check `graphrag/kv_store_doc_status.json` for topics whose Pass 2 failed — those keep only their structural `Topic` node. Questions scoped to those return sparse results.
- **Weak slices to avoid scoping to:** categories — Staff, General
- **User handles may leak into content entities.** Top-degree `other`-typed entries that look like usernames are forum handles the LLM extracted as content — they compete with real concepts during retrieval. If a top-N result is a handle rather than a concept, discount it.

## 10. Bad-question → good-question rewrites

| Original (hedges) | Better |
|---|---|
| "Top-10 pain points" | "Recurring pain points in the <strong category> category" + `--mode global` |
| "List all bugs" | "Bugs reported in <strong version>" (version scope) |
| "Which topic has most replies" | `stats sql "SELECT title, posts_count FROM topics ORDER BY posts_count DESC LIMIT 10"` |
| "Summarize the forum" | "Summarize main themes in top-3 categories by activity" (after `stats`) |
| "What are users saying?" | Scope by named entity: "…about <top content entity>" |
| "How common is X?" | `stats --path . search "X"`, then synthesize on top |
| "Who owns / introduced X?" | Edges underrepresent this — use `stats` over posts / user_activity |

## 11. References

- `docs/MANUAL.md` — full CLI + env reference, rerank setup, mode semantics.
- `docs/lightrag/ProgramingWithCore.md` — authoritative `QueryParam` reference.
- `logs/INDEX_AND_EMBED-*.md` — per-run findings; include what got indexed and at what quality.
- `logs/CREATE-QUERY-GUIDE-*.md` — per-generation log for this guide.

## 12. Regenerating this guide after re-index

This guide drifts as the graph grows or is re-extracted. Regenerate:

```text
# Claude Code
/create-query-guide <data-dir>

# Codex
$create-query-guide <data-dir>
```

Or run the module directly:

```bash
uv run python -m discourse_explorer.derive_query_guide <data-dir>
```

Sources it reads:

- **Node / edge counts, entity-type distribution, degree ranking** — `graphrag/graph_chunk_entity_relation.graphml`.
- **Edge-verb frequency (§5)** — every edge's `keywords` attribute, stemmed via `rel_clusters._tokenize` + `_stem`.
- **Per-category counts (§4.3)** — `topics/*.json` → `category_name`.
- **Per-version counts (§4.4)** — `topics/*.json` → `tags[].name` matching `r'^20\d\d[\.․]\d\d$'` (accepts both ASCII `.` and U+2024 `․`).
- **Blind-spot hints (§9)** — skim `graphrag/kv_store_doc_status.json` for non-`processed` entries.

§6 is LLM-authored against these facts; §1–§5 and §7–§12 are template-substituted and change only when the numbers change.
