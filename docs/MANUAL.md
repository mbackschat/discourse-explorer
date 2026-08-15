# Discourse Explorer — Manual

Per-tool usage reference. For project overview, the demo path, and configuration / authentication setup, see the [README](../README.md).

## 1. Scraping

```bash
# Full form
uv run discourse-explorer scrape https://discourse.example.com --output ./data/my-forum

# Using defaults (URL + DATA_DIR from env)
uv run discourse-explorer scrape

# Preview / force full re-scrape
uv run discourse-explorer scrape --output ./data/my-forum --dry-run
uv run discourse-explorer scrape --output ./data/my-forum --full
```

**Delta sync is automatic.** First run performs a full scrape; subsequent runs only fetch topics created or updated since the last run (tracked in `<data-dir>/sync_state.json`). `--full` discards the state and re-scrapes everything.

### Output layout

```
./data/my-forum/
├── config/
│   ├── .env                    # per-run: URL, auth, models, embedding, gleaning
│   └── entity_types.json       # entity-type vocabulary (names + structural flags)
├── categories.json             # all forum categories
├── index.json                  # topic index (metadata only)
├── sync_state.json             # delta sync timestamp
├── topics/                     # one JSON per topic
│   └── *.json
├── discovery_result.json       # cached output from discover_types (if run)
├── graphrag/                   # knowledge graph (after indexing)
├── visualize/                  # rendered visualization
│   ├── graph.html              # open in a browser
│   ├── data.js                 # node + edge payload
│   └── cache/                  # build-time-only (layout + keyword clustering)
├── logs/                       # workflow logs
├── QUERY-ANSWERS.md            # accumulated Q&A log (from query-advisor)
└── QUERY-GUIDE.md              # per-forum query playbook (hand-curated)
```

**Disk usage.** `graphrag/` scales with corpus × embedding dim. A 1300-topic forum with `text-embedding-3-large` (3072d) + `gleaning=1` produces **~120–200 MB**. `-small` (1536d) roughly halves.

Multiple forums in parallel: just give each its own path.

## 2. Analytics

DuckDB over the scraped JSON. No indexing step — reads topic files directly.

```bash
uv run discourse-explorer stats -p ./data/my-forum tags          # tag distribution
uv run discourse-explorer stats -p ./data/my-forum users         # top contributors
uv run discourse-explorer stats -p ./data/my-forum categories    # category breakdown
uv run discourse-explorer stats -p ./data/my-forum activity      # posts per month
uv run discourse-explorer stats -p ./data/my-forum unanswered    # topics with no replies
uv run discourse-explorer stats -p ./data/my-forum search "keyword"

# With DISCOURSE_DATA_DIR set, --path can be omitted:
uv run discourse-explorer stats tags
```

All subcommands support `--limit N` and `--json`.

### SQL REPL

```bash
uv run discourse-explorer stats -p ./data/my-forum sql                                  # interactive
uv run discourse-explorer stats -p ./data/my-forum sql "SELECT COUNT(*) FROM topics"    # one-shot
```

Type `.schema` to inspect columns, `.tables` to list views, Ctrl+D to exit.

**Views** (defined in `_connect()`):

| View | Grain | Purpose |
|---|---|---|
| `topics` | topic | Flat metadata. Timestamps cast. Category resolved (subcategory → parent). |
| `posts` | post | Every post flattened out of its parent topic. |
| `topic_tags` | topic × tag | Tag membership. |
| `categories` | category | From `categories.json`. |
| `topic_summary` | topic | `topics` + `first_poster` + comma-joined `tags`. The go-to view. |
| `topic_participants` | topic × user × role | Role ∈ {`creator`, `responder`, `system`}, per-topic activity. |
| `topic_threads` | topic | Response metrics: creator, first/last responder, `response_time`, unique responders. |
| `user_activity` | user | Aggregates: topics_created, topics_helped, replies_written, likes_received, first/last_seen. |

Example queries:

```sql
-- Most viewed open topics with their tags
SELECT id, title, first_poster, tags, views
FROM topic_summary
WHERE NOT closed AND NOT archived
ORDER BY views DESC
LIMIT 20;

-- Response time by category (seconds → hours)
SELECT category,
       COUNT(*) AS topics,
       ROUND(MEDIAN(EXTRACT(EPOCH FROM response_time)) / 3600, 1) AS median_hours
FROM topic_threads
WHERE response_time IS NOT NULL
GROUP BY category
ORDER BY median_hours DESC;
```

## 3. GraphRAG: index → query & visualize

[LightRAG](https://github.com/HKUDS/LightRAG)-powered knowledge graph. Two stages: **index once** (expensive, builds `<data-dir>/graphrag/`), then **use the graph** (query + visualize — both are cheap).

Pipeline: `scrape → discover-types (recommended) → --index → query and/or visualize`.

### Choosing a provider

Auto-detected by `OPENAI_API_KEY` in `<data-dir>/config/.env`:

- **OpenAI** (recommended for speed). Defaults: `gpt-4.1-mini` extraction, `text-embedding-3-large` (3072d) embeddings. Probe your tier before committing:
  ```bash
  uv run discourse-explorer query ./data/my-forum --detect-limits
  ```
- **Ollama** (local, free). Install Ollama and pull models:
  ```bash
  ollama pull qwen2.5:14b
  ollama pull nomic-embed-text
  ```

**Benchmark** (1331-topic / ~5.5 M-token forum, `gleaning=1`, `FORCE_LLM_SUMMARY_ON_MERGE=999`):

| Provider | Extraction model | Runtime | Est. cost |
|---|---|---|---|
| OpenAI | **gpt-4.1-mini** (default) | ~2.5–3 h | ~$6–8 |
| OpenAI | gpt-4o-mini | ~2–2.5 h | ~$3–5 |
| Ollama | qwen2.5:14b (M4 / 48 GB) | ~15 h | free |

> Avoid gpt-5-series for `--index` — reasoning latency is ~5× higher. Reserve it for `--query-model`.

Indexing is resumable — re-run `--index` (no `--clear`) to pick up after a crash.

### Speed knobs (OpenAI)

| Knob | Default | Set for speed | Effect |
|---|---|---|---|
| `FORCE_LLM_SUMMARY_ON_MERGE` | `999` (project default) | `999` | Skip per-entity summary cascade. 3–5× speedup. Set `8` for LightRAG's summarizing behaviour. |
| `--gleaning` / `GLEANING` env | `1` | `0` | Drop the "what did you miss?" pass. ~1.5× speedup, real recall loss. |
| `--llm-concurrency` / `LLM_MODEL_MAX_ASYNC` | `8` | up to probe-recommended (e.g. `13` on Tier 3) | Parallel LLM calls. |
| `--parallel-insert` / `MAX_PARALLEL_INSERT` | `4` | probe-recommended (e.g. `3` on Tier 3) | Parallel doc inserts. |

Full env-var reference in **[`analysis/vocabulary-and-config.md`](analysis/vocabulary-and-config.md)**.

### Discover entity types (recommended before first index)

Sample N topics with an LLM, distill 4–6 content types tailored to your forum. Combined with the four structural types (`User`, `Topic`, `Category`, `Tag`), written to `<data-dir>/config/entity_types.json`.

```bash
# Cheap exploration
uv run discourse-explorer discover-types ./data/my-forum --sample-size 30

# Full pass for production (~$0.50, ~2 min with gpt-4.1-mini)
uv run discourse-explorer discover-types ./data/my-forum --sample-size 300 --top 60

# Review a prior run with no LLM cost
uv run discourse-explorer discover-types ./data/my-forum --show-artifact --top 60
```

Detailed workflow: [`workflows/DISCOVER_ENTITY_TYPES.md`](workflows/DISCOVER_ENTITY_TYPES.md).

### Build the knowledge graph

```bash
uv run discourse-explorer query ./data/my-forum --index           # build (resumes if interrupted)
uv run discourse-explorer query ./data/my-forum --index --clear   # wipe and rebuild
uv run discourse-explorer query ./data/my-forum --index --clear --limit 50   # 50-topic sample (~$0.30)
```

Indexing runs four passes: structural seed → LLM extraction → structural enrichment → entity-name canonicalization. Deep mechanics: **[`analysis/multi-pass-indexing.md`](analysis/multi-pass-indexing.md)**.

Pass 4 collapses case-fold dupes (`jdoe` / `Jdoe` / `JDoe`) and `^User ` / ` Person$` paraphrases of Pass-1 user seeds without re-indexing. Run standalone against an existing graph with `--canonicalize-only` (zero LLM cost during merges, ~150s wall clock for a 1.3K-topic corpus). Background: **[`analysis/entity-name-canonicalization.md`](analysis/entity-name-canonicalization.md)**.

**Launch wrapper** — use this for any run long enough to outlive your shell. It detaches into its own session, logs to `<data-dir>/logs/`, and refuses to stack a second indexer on the same data dir:

```bash
./scripts/index.sh --resume  # add new/changed topics to the existing graph (cheap, non-destructive)
./scripts/index.sh --full    # DESTRUCTIVE: wipes graphrag/ and rebuilds from scratch
```

A mode is required. A bare `./scripts/index.sh` exits 64 rather than guessing — the destructive mode is a poor thing to reach by typo.

Full workflow with cost estimation: [`workflows/INDEX_AND_EMBED.md`](workflows/INDEX_AND_EMBED.md).

**Refresh stale structural embeddings** (after Pass 3 timeouts — type committed but Faiss embedding didn't):

```bash
uv run discourse-explorer query ./data/my-forum --index --enrich-only
```

Skips Pass 1 + Pass 2, runs only the structural re-assertion pass. ~$0.02 and a few minutes.

### Ask questions

```bash
uv run discourse-explorer query ./data/my-forum "your question" [--mode MODE]
```

LightRAG exposes **five retrieval modes**. CLI default is `mix`:

| Mode | When to pick it |
|---|---|
| **`mix`** (default) | General-purpose Q&A. Combines graph traversal and vector search — best single choice when you don't know the answer shape. |
| **`local`** | Narrow, entity-anchored questions ("What did users report about custom conditions?"). |
| **`global`** | Broad, synthesis-oriented questions ("Summarize the main themes of Data Services"). |
| **`hybrid`** | Local + global blended, graph-only (no chunk fetching). |
| **`naive`** | Plain vector similarity over chunks, skips the graph. Baseline. |

Authoritative reference: [`lightrag/ProgramingWithCore.md`](lightrag/ProgramingWithCore.md) §QueryParam.

Examples:

```bash
# Default — mix.
uv run discourse-explorer query ./data/my-forum \
  "What are the top unsolved problems in Data Services?"

# Entity-anchored narrow lookup.
uv run discourse-explorer query ./data/my-forum \
  "What did users report about custom conditions?" --mode local

# Broad synthesis.
uv run discourse-explorer query ./data/my-forum \
  "Summarize the main themes of the forum" --mode global

# Cheap indexing, strong query reasoning.
uv run discourse-explorer query ./data/my-forum "complex question" \
  --query-model gpt-5.2
```

Retrieval knobs (env vars, no CLI flag): `TOP_K`, `CHUNK_TOP_K`, `MAX_ENTITY_TOKENS`, `MAX_RELATION_TOKENS`, `MAX_TOTAL_TOKENS`. Set in `<data-dir>/config/.env` for persistent tuning. Full list in [`analysis/vocabulary-and-config.md`](analysis/vocabulary-and-config.md).

### Rerank (optional)

LightRAG supports a rerank step that re-scores retrieved chunks against the query using a cross-encoder. Disabled by default; configure per-forum in `<data-dir>/config/.env`:

```
RERANK_PROVIDER=jina   # jina | cohere | ali
RERANK_MODEL=jina-reranker-v2-base-multilingual
RERANK_API_KEY=...
```

Providers: Jina ($2/1M tokens), Cohere ($2/1K searches), Ali (region-priced). Self-hosted BAAI/bge-reranker-v2-m3 via HuggingFace TEI works through `provider=cohere` + local `RERANK_BASE_URL=http://localhost:8080/rerank`. No re-index needed — rerank is pure query-time.

### Incremental updates (after a delta scrape)

You don't need `--clear` when you've only added new topics.

```bash
uv run discourse-explorer scrape                             # delta scrape
uv run discourse-explorer query ./data/my-forum --index       # incremental — no --clear
uv run discourse-explorer visualize ./data/my-forum           # regenerate viz
```

LightRAG's `doc_status` tracker skips already-processed documents; only new/modified topics incur LLM cost. Pass 3's skip gate keeps the "nothing changed" re-run under a second.

Full per-tool support matrix and the edited-topic gotcha: [`analysis/incremental-updates.md`](analysis/incremental-updates.md).

## 4. Graph Visualization

Reads `<data-dir>/graphrag/` and generates an interactive HTML explorer ([screenshot in the README](../README.md#screenshot)). No LLM calls, no additional cost (beyond the first-run OpenAI embedding of unique edge keywords — ~$0.003 one-time).

```bash
uv run discourse-explorer visualize ./data/my-forum           # generate
uv run discourse-explorer visualize ./data/my-forum --open    # generate and open in browser
```

Features:

- Nodes colored by entity type, sized by connections.
- **Full-text search** across labels + descriptions + entity types (concept queries like `permission` surface entities whose names don't contain the word but whose descriptions do). 1-hop search-context dim is gated by every other active filter (type / degree / community) and toggled via a `Show 1-hop context` checkbox under the search box. The same strict-gate applies to a pinned (clicked) node's neighbor halo — only the explicitly clicked node is sticky beyond filters. The Nodes stat exposes every override as a composable suffix: `(+N dim)` for search context, `(+1 pinned)` for the pin itself, `(+N pin-context)` for the pin's filter-passing neighbors.
- **Click-to-inspect detail panels** for nodes, edges, super-categories, and category-edges. Click the panel title (entity name or `Edge`) to recenter the canvas on it. The right-panel back/forward arrows traverse a unified history of node, category, edge, cluster, and category-edge inspections.
- **Description split per merged extraction** — LightRAG concatenates one description per chunk extraction with `<SEP>`; the panel renders each phrase as its own paragraph (left-accent border + dashed phrase-boundary separator) with an `N phrases · K topics` annotation when N>1. Markdown copy emits `### Phrase N` H3 subsections per phrase.
- **Source topics** — Topic-typed connections in the node panel get a per-row `▸` expand revealing date + posts + author + first-post excerpt + Open-topic-preview modal action; a per-group `Expand all` / `Collapse all` toggle covers Topic groups with ≥2 expandable rows. A residual `Source topics` section (collapsed by default when there's overlap with the Topic neighbors above; hidden entirely when residual is empty) covers any extraction chunks not represented as 1-hop Topic neighbors. Edge panels list per-edge topic provenance directly. Closes the graph→source loop so any LLM-extracted claim is verifiable against the original post.
- **Category view** shows a dozen super-category bubbles (sized by member count) with aggregate edges between them. Hover any bubble for a quick `top hubs / member count` summary; click for a richer panel (entity-type composition histogram, top members by within-category degree, inter-category edges with top-3 concrete bridges drillable from the panel). Click an aggregate edge for the **category-edge** panel: relationship-type histogram, top concrete bridges by weight, and per-side top contributors — turns "Component–Issue: 3,200 edges" into something actionable.
- **Cluster summary panel** — clicking the `Cluster #N` badge on any node opens a panel with composition by entity type, top members by within-cluster degree, top relationship types, and bridges to other clusters. A persistent breadcrumb chip (top-right) shows the lock state with split label / × hit-zones (label opens the panel; × unlocks).
- **Focus mode breadcrumb chip + active-button ×** — clicking `Focus 1-hop` / `Focus 2-hop` flips the button itself to an active state with a trailing `×`, and adds an amber chip next to the breadcrumb (`Focus 1-hop · ivluu (24) ×`). Either × exits focus and restores filters. Replaces the easily-overlooked toolbar Exit-Focus button.
- **Edge selection** thickens + brightens the selected edge on the canvas, restored on deselect.
- **At a Glance** section in the control panel summarizes the current visible/filtered set with a stat-block header (`Nodes / Edges / In viewport`, tabular-nums for easy scanning) followed by top entities by degree, dominant super-categories, dominant rel-types — all clickable to drill in. When a cluster is locked, a banner reframes the lists as the cluster's auto-summary.
- **Leave-one-out filter counts** on every entity/rel row: `L (N)` where L is "visible now (matches every other filter)" and N is the graph total. When a row is unchecked and would gain back items if re-enabled, a `+M` chip appears; click to re-enable. Threshold sliders show the same shape with a `−drop` chip when the threshold has hidden items.
- **Label density** section in the control panel — three live sliders (Hub labels top-N, Hubs-mode auto-label viewport ≤ N, All-mode label cap) plus a `247 labels · 1,234 nodes in viewport` readout that updates on every zoom / filter change. URL params (`?hubLabels=N&viewportThreshold=N&allCap=N`) override defaults at init; sliders take over after.
- **Graph algorithms** computed at build time: a "Cut node" badge marks articulation points (removal disconnects part of the graph); a "Cluster #N" badge marks the node's Louvain community (click to lock the canvas to that community). The detail panel's **"Related"** section runs Personalized PageRank from the inspected node to surface 2- and 3-hop structurally-close entities. **Find path to…** uses Yen's algorithm to highlight up to **3** distinct shortest paths in different colors.
- **Copy split-button** on every detail panel — `[Copy][▾]`. Default copies the entity name; the ▾ menu offers a one-liner (forum-domain content + composition stats) and full-Markdown (panel as a take-home document with all sections).
- **Pathfinding**, degree/weight sliders, hover tooltips on every control.

**Opens in Node View** — full graph, with a viewport-aware label LOD: zoom out shows global hubs only; zoom in pops labels for whatever's in the viewport. The Category | Node toggle (top-left of the toolbar) is one click away if you'd rather start from the super-category overview; it stays visible even when the control panel is collapsed. The breadcrumb above the toolbar tracks the drill path (`All › Issue`); the `All` segment returns to Category view. A loading overlay covers the canvas during the first paint and during Category → Node toggles (the ~16k-node re-render blocks the main thread for 1-3 s; the spinner is compositor-thread driven so it keeps moving).

Labels honour a three-state LOD picker in the toolbar (`<select>`): **Hubs** (default, top-N global + viewport-aware fill) · **All** (cap 200 by degree, viewport-aware) · **None** (super-nodes only). Cursor-hover reveals an individual node's label in any mode. Hub count is tunable with `--hub-label-count N` at build time, or live-adjustable from the **Label density** section.

Output all lives under `<data-dir>/visualize/`. Only `graph.html` and `data.js` are browser-loaded; anything under `cache/` is build-time-only. **Share by copying or zipping the whole `visualize/` folder** — moving just `graph.html` opens to an empty network.

Common flags:

```bash
--max-rel-types N             # legend bucket count (default 12)
--balance-threshold F         # rebalance trigger when max/median > F (default 4.0)
--min-bucket-pct F            # drop buckets below F% of edges (default 0.5)
--regenerate-keyword-clusters # wipe and rebuild the keyword clustering cache
--hub-label-count N           # top-N hubs whose labels stay visible in the default Labels: Hubs mode (default 100; 0 = none)
```

Full output layout, cache-management rules, color palette: **[`analysis/visualize-build.md`](analysis/visualize-build.md)** (Python build) + **[`analysis/visualize-frontend.md`](analysis/visualize-frontend.md)** (JS / CSS runtime). Prune-and-split balancing for relationship buckets: **[`analysis/rel-clusters-algorithm.md`](analysis/rel-clusters-algorithm.md)**.

## Guided workflows via Claude Code and Codex

Recurring multi-step operations are packaged as project-scoped skills. Their canonical source is `.claude/skills/`, and Codex discovers the same files through `.agents/skills`. In Claude Code, invoke a skill with its slash command. In Codex, invoke it with `$skill-name`. Both hosts also support the natural-language triggers below.

| Skill | Claude Code | Codex | Natural-language triggers | What happens |
|---|---|---|---|---|
| Discover entity-type vocabulary | `/discover-entity-types [<data-dir>]` | `$discover-entity-types [<data-dir>]` | "discover types", "run discovery", "rediscover vocabulary" | Sample scraped topics with an LLM, distill a content-type vocabulary, review raw labels, hand-tune, write to `<data-dir>/config/entity_types.json`. |
| Build or update the knowledge graph | `/index-and-embed [<data-dir>]` | `$index-and-embed [<data-dir>]` | "update the graph", "refresh the index", "index the new topics", "rebuild the graph", "re-index", "run indexing" | Pick the mode (`--resume` by default, `--full` only when the vocabulary/embedding/gleaning changed), elicit any config choices, estimate cost against the real corpus, back up the graph before a destructive run, launch detached via `scripts/index.sh`, verify, regenerate `visualize/graph.html`. |
| Regenerate the query guide | `/create-query-guide [<data-dir>]` | `$create-query-guide [<data-dir>]` | "create query guide", "regenerate query guide", "refresh QUERY-GUIDE.md" | Parse graphml + topic JSON + entity vocabulary; derive scale, top-15 entities by degree, per-category / per-version coverage, and edge-verb frequencies; LLM-tailor the question library (§6) against those facts; write `<data-dir>/QUERY-GUIDE.md` (backing up any prior). ~$0.05, ~30s. Run after `index-and-embed`. |
| Advise on a query | `/query-advisor [<data-dir>] "<question>"` | `$query-advisor [<data-dir>] "<question>"` | "how should I ask", "which mode for", "best way to query" | Read `QUERY-GUIDE.md` for corpus ground truth, analyze the *question*, route to the right tool (`stats` vs `query`), pick the retrieval mode, tune knobs, and persist the answer (default: append to `<data-dir>/QUERY-ANSWERS.md`). One question → one answer. |
| Compose a forum report | `/forum-report [<data-dir>]` | `$forum-report [<data-dir>]` | "pain-points audit", "release-quality audit", "community health", "customization audit", "decision brief", "analyze the forum", "top themes" | Pick a pre-designed *report* type (pain-points, release-quality, community-health, customization-ceiling, decision-brief, or custom), pick a depth level (L0 stats-only → L3 stats + graph synthesis), run the skeleton probes, gate drill-down cost, persist to `<data-dir>/reports/<type>/NN-<date>-<slug>.md`. Report ask vs. single question; complements `query-advisor`. |

Each skill is a thin entry point that reads the corresponding runbook in [`workflows/`](workflows/) (for the build-oriented skills) or encodes its own decision model (`query-advisor`, `forum-report`). Detailed docs remain the authoritative reference for failure modes. The shared [host compatibility contract](../.claude/skills/HOST-COMPATIBILITY.md) defines how each host asks questions, invokes skills, and delegates execution.

**Question vs report:** `query-advisor` routes one question and produces one answer. `forum-report` composes a multi-section deliverable from many probes. If phrasing is thematic + plural (*audit, top themes, analyze the forum*), it's a report; one specific question gets routed by the advisor. The advisor offers a redirect when it detects report-shape phrasing.

If you're running the tools directly without a skill host, follow [`workflows/DISCOVER_ENTITY_TYPES.md`](workflows/DISCOVER_ENTITY_TYPES.md), [`INDEX_AND_EMBED.md`](workflows/INDEX_AND_EMBED.md), and [`CREATE_QUERY_GUIDE.md`](workflows/CREATE_QUERY_GUIDE.md) step by step.

### End-to-end workflow for a new forum

The skills compose into a linear pipeline. You run each one once per corpus; subsequent changes (new scrapes, vocabulary shifts, question-asking) loop back into the right step.

```
1. scrape     →   2. discover-types   →   3. index-and-embed   →   4. create-query-guide   →   5a. query-advisor   (one question → one answer)
   (CLI)          (skill)                  (skill)                  (skill)                     5b. forum-report    (report type → multi-section artifact)
```

1. **Scrape the forum.** Only the `scraper` CLI talks to Discourse. Per-URL throttling + auth handling are in the module; call it once, or use `--full` for incremental resumes.
   ```bash
   uv run discourse-explorer scrape https://your-forum.example.com --output ./data/your-forum
   ```

2. **`discover-entity-types <data-dir>`.** Sample the scraped posts, ask an LLM what *kinds* of entities show up, distill into a 4–6-item content vocabulary, and review raw labels before committing. Writes `<data-dir>/config/entity_types.json`. Run this whenever the community's content focus shifts — it shapes what LightRAG extracts in the next step.

3. **`index-and-embed <data-dir>`.** Destructive rebuild of `<data-dir>/graphrag/` against the confirmed vocabulary. The skill elicits model + gleaning + concurrency choices, estimates cost (~$6–8 on a ~1300-topic corpus), backs up the old graph, runs in the background with a live monitor, verifies after, and regenerates the visualization. Takes hours — the skill keeps you informed without polling.

4. **`create-query-guide <data-dir>`.** Derives `<data-dir>/QUERY-GUIDE.md` from the fresh graph: scale, top-15 entities by degree, per-category and per-version coverage tables, relation-verb inventory, and an LLM-tailored question library (§6) that references only entities / categories / versions actually present. Cheap (~$0.05) and fast (~30s). **Always run after `index-and-embed`** — the numbers drift with every re-index, and a stale guide feeds stale recommendations to the advisor.

5a. **`query-advisor "<question>"`** (loop as needed). Reads `QUERY-GUIDE.md` as ground truth, classifies the question, routes to `stats` (count/filter) or `query` (synthesis) with the right mode, and persists the answer (by default, accumulating to `<data-dir>/QUERY-ANSWERS.md`). The guide's §4 facts prevent the advisor from recommending categories, versions, or entities that don't exist in *this* corpus.

5b. **`forum-report`** (compose a deliverable). Pick a pre-designed report type (pain-points audit, release-quality audit, community health, customization-ceiling audit, decision brief, or custom), pick a depth level (L0 stats-only → L3 stats + graph synthesis), run the skeleton probes, gate drill-down cost before L2+ spend, and persist to `<data-dir>/reports/<type>/NN-<date>-<slug>.md`. A commands-only companion lives at `<data-dir>/reports/<type>/queries/NN-<date>-<slug>.md` for reproducibility.

   **Optional replayable variant.** If [`showboat`](https://github.com/anthropics/showboat-style-tool) is on PATH, the skill also emits a third artifact at `<data-dir>/reports/<type>/queries/NN-<date>-<slug>-showboat.md` — same probes captured as executable + output blocks. Verify the audit's numbers by running `showboat verify <path>` from the project root (or `showboat --workdir <project-root> verify <path>` from any other CWD). A clean run is silent (exit 0); a diff means either the underlying graph snapshot has changed (re-scrape, re-index) or a probe needs a more deterministic `ORDER BY` tiebreaker. The skill never runs `verify` itself — that's a manual step the user owns. If `showboat` isn't installed, the skill skips this variant silently and the canonical pair (report + companion) is unaffected.

   Complements the advisor — use `forum-report` when the ask is plural/thematic (*audit, top themes, analyze the forum*) rather than a single question.

Loop-back rules:

- **New scrapes** (incremental or full) → re-run step 3 (if the delta is large enough to move entity-type distributions), then step 4.
- **Vocabulary tuning** (adding or removing a content type) → step 2, then step 3 (requires `--clear`), then step 4.
- **Embedding model or dimension change** → step 3 with `--clear`, then step 4.
- **Just asking more questions** → step 5a only.
- **Periodic reports** (release-quality audit per version, quarterly community-health update) → step 5b; re-run when the corpus has grown meaningfully since the last artifact.
- **Advisor keeps referencing stale names** → step 4, then step 5a again.
