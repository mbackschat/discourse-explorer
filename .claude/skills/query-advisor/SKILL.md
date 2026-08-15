---
name: query-advisor
description: >-
  Analyze ONE natural-language question against the scraped Discourse corpus and recommend which
  tool to run (`stats` vs `query`), which retrieval mode
  (`local`/`global`/`hybrid`/`mix`/`naive`), and tuned knobs (`TOP_K`, `CHUNK_TOP_K`, env vars).
  Use when the user asks a single question they want answered from the forum data and you want the
  right tool + config before spending tokens. Invocation shape: the question is the load-bearing
  argument; the data dir is optional (resolved from `DISCOURSE_DATA_DIR` or a discovered
  `./data/*/` candidate if omitted). Triggers: "how should I ask", "which mode for", "advise on
  this question", "help me ask", "plan a query", "best way to query", "stats or graph for". NOT
  for report-shaped asks — thematic, plural, multi-section requests ("top pain points", "audit",
  "top themes") belong to `/forum-report`; if one arrives here, hand it off rather than answering.
---

# Query advisor

## Host compatibility

Before executing this skill, read [`../HOST-COMPATIBILITY.md`](../HOST-COMPATIBILITY.md). Operations such as “ask the user,” “invoke the skill,” and “delegate execution” use the host bindings defined there.

Pick the right tool, mode, and knobs **before** running a query. Saves tokens, time, and the "top-10 by frequency" hedge trap.

## The flow

1. **Parse the invocation** → extract question + optional data dir.
2. **Resolve the data dir** → CLI arg ≻ `DISCOURSE_DATA_DIR` ≻ ask the user.
3. **Read `<data-dir>/QUERY-GUIDE.md`** → ground truth for this corpus. Halt (soft) if missing.
4. **Classify the question** → count/filter vs synthesis vs both.
5. **Route** → `stats`, `query`, or `stats` + `query` combined.
6. **Pick mode + knobs** (for `query`) or compose (for combined).
7. **Present the recommendation** → diagnosis, command(s), rationale, optional better phrasing.
8. **Ask the user** whether to run, split ambiguous concepts, + where to persist. This MUST happen in the main conversation.
9. **Execute** — for any non-trivial chain, delegate execution with a self-contained prompt (see "Two-tier delegation"). Skip delegation for pure-stats or show-only runs.
10. **Persist first, then echo proportional to persistence** — condensed summary in chat when the answer was written to a file; full body in chat only when the user chose "show in chat" (no file). Footer with path + line count.

## When to invoke

- User asks for advice on how to query: *"How should I ask about X?"*, *"Which mode for this?"*.
- User proposes a question whose shape would hedge badly on the default tool (frequency ranking against `query`, open-ended synthesis against `stats`). Route them proactively.
- User types `/query-advisor "<question>"` or `/query-advisor <data-dir> "<question>"` explicitly.

**When NOT this skill — hand off to `/forum-report` instead.** If the user's phrasing is thematic, plural, and report-shaped (*"pain-points audit"*, *"top themes"*, *"analyze the forum"*, *"community health report"*, *"top-10 pain points"*, *"summarize the forum"*), it's a *report* ask, not a *question* ask. The advisor can still route it, but the outcome — multiple chained queries that synthesize a multi-section deliverable — is exactly what `/forum-report` is designed for. Ask the user: *"This sounds like a report ask — use `/forum-report`? Or route it as a one-shot question?"*. Only stay in advisor if the user says so explicitly.

## Parse the invocation

The question is the required argument; everything else is optional.

1. **Extract the question.** Usually the last quoted string, or the only arg that obviously isn't a path. If the invocation is ambient (natural-language mid-conversation), treat the whole message as the question.
2. **Extract the data dir (optional).** If prefixed (e.g. `/query-advisor ./data/my-forum "question"`), resolve with `Path(arg).expanduser().resolve()` and confirm `<path>/topics/` exists.
3. **Resolve data dir when not given:**
   - `DISCOURSE_DATA_DIR` from the project-root `.env` via `bootstrap(None).data_dir`. Use silently if present and valid — single-forum case is common.
   - If bootstrap fails, scan `ls -d ./data/*/` and ask the user to choose, with an "Other" escape.
   - Never analyze against the wrong graph — recommendations depend on what's indexed.

All `<data-dir>` references below resolve to the chosen path.

## Read the corpus's query guide

After the data dir is resolved, read `<data-dir>/QUERY-GUIDE.md` once and keep it in context for the rest of this invocation. The guide is the corpus-specific ground truth that stops the advisor from hallucinating against an imaginary graph shape.

- **Present**: use §4 (scale, top entities, categories, versions) as routing ground truth — **never recommend a category or version not in §4.3 / §4.4**, prefer the highest-degree entities from §4.2 over ones the user named loosely. Use §5 (relation verbs) to sanity-check the question's verb — if the user asks about a kind of relationship (ownership, succession, causation, dependence, …) and §5 shows it's sparse in this corpus, surface that in the rationale. If §6 (question library) contains a template matching the user's question (fuzzy match on intent + named entities), prefer that template's mode + knobs and cite the matching `§6.x`.

- **Treat §9 (blind spots) as hard constraints.** If the user's question falls in a blind spot (weak category/version, user-handle-as-entity trap, out-of-vocab fallback bucket), surface it in the rationale — don't hide it behind a confident recommendation.

- **Exception:** if the question obviously routes to `stats` only (pure count / filter question, no synthesis), skip reading the guide. It adds nothing to DuckDB SQL routing and the guide-missing halt below would be user-hostile for one-off counts.

### Guide-missing halt (soft)

If `<data-dir>/QUERY-GUIDE.md` does not exist and the question is *not* a pure-`stats` question, stop before routing. Ask the user to choose:

- **Generate it first (Recommended)** — invoke `/create-query-guide <data-dir>`, then re-run this advisor. Cost ~$0.05, runtime ~30s.
- **Proceed anyway** — advise from the decision model below. Warn explicitly in the rationale that recommendations may reference categories / entities / versions that don't exist (the advisor can't verify without the guide).
- **Cancel.**

Soft, not hard: retrieval works without the guide; the guide improves routing. Surface the tradeoff; don't silently proceed.

## Decision model — tool selection

Classify by **intent signal** first.

### Route to `stats` (DuckDB) when the question is COUNT / RANK / FILTER

| Signal | Example | stats recipe |
|---|---|---|
| *"how many"* | "How many topics in `<category>`?" | `stats tags` / `stats categories` / ad-hoc SQL |
| *"top-N" / "most" / "least"* | "Top-10 most-replied topics" | `stats sql "SELECT ... ORDER BY ... LIMIT N"` |
| *"list all X tagged/in/by"* | "All topics by `<username>`" | `stats sql` or `stats users` |
| *"when / over time / activity"* | "Activity since last quarter" | `stats activity` |
| *"unanswered / open"* | "Unanswered topics in `<category>`" | `stats unanswered` |
| *exact filter* | "Topics in `<category>` with `<tag>` tag" | `stats sql` |

Graph retrieval has **no counts** — `top_k` bounds whatever the LLM sees. For any "rank by frequency" or "exhaustive enumeration", route to `stats`. If synthesis is wanted *after* ranking, combine (below).

**Use pre-computed aggregate views before hand-rolling joins.** `stats.py` exposes several views that already do the common joins:

| View | Use for |
|---|---|
| `topic_summary` | **Go-to view** — `topics` columns + `first_poster` + comma-joined `tags`. Default choice for topic-metadata queries. |
| `topic_threads` | Response metrics per topic: `creator`, `first_responder`, `first_response_at`, `response_time` (INTERVAL), `unique_responders`. Natural for *"slow-to-answer"*, *"debate-heavy"*, *"threads that fizzled"*. |
| `user_activity` | Per-user aggregates: `topics_created`, `topics_helped`, `replies_written`, `likes_received`, `first_seen`, `last_seen`. Natural for *"most helpful contributors"*, *"active last N months"*. |
| `topic_participants` | `topic × user × role` (role ∈ `creator` / `responder` / `system`). Natural for role-based queries; **filter `role != 'system'` to exclude bots like discobot**. |

Column reference with the full contract: [`docs/analysis/duckdb-views.md`](../../../docs/analysis/duckdb-views.md).

#### Stop at `stats` when `stats` already answered the question

**Graph query is not a default destination** — it costs LLM tokens, wall-clock, and adds retrieval noise. If the user asked for a list, a count, or a filter, the `stats` output *is* the answer; don't escalate to `query` just because you can.

Stop at stats when:
- The question has one unambiguous data shape (list, count, filter) and no "why / how / what patterns" sub-question.
- The stats output is already readable without synthesis (≤ ~30 rows the user can eyeball).
- Intent cues: *"list", "which", "how many", "show me"* — and no *"explain", "summarize", "why"*.

If unsure, present the stats result first and offer an optional follow-up synthesis. Don't bundle synthesis into the initial recommendation.

### Route to `query` (GraphRAG) when the question is SYNTHESIS / EXPLANATION / PATTERN

See the combined mode table below for signal → mode mapping.

### Route to `stats` + `query` (combined) when both count and synthesis matter

Common patterns the user thinks they want from `query` alone but shouldn't:

- **"Top-N by frequency + explain each"** → `stats` ranks, `query` synthesizes around the named results.
- **"Which topics are most-engaged + what's in them"** → `stats` sorts by `posts_count`, `query` drills into the top-5 by title.
- **"Unanswered X + what's the missing context"** → `stats unanswered`, `query` for each open thread.
- **Time-scoped synthesis** ("pain points since `<date>`", "what changed since `<version>`") → `stats` enforces the date/version filter (retrieval can't), `query` synthesizes over the scoped slice.

#### Composition over monoliths

**Multiple `stats` probes and multiple `query` runs can and often should be chained** — the skill's job is to produce the shortest good answer, not the shortest command.

- **Multiple `stats` calls to triangulate** before any graph query: one for engagement ranking, one for category distribution, one for specific error/tag presence. Each is ~free; they verify the question has real signal before spending on retrieval.
- **Multiple `query` calls to decompose a broad question** into narrower landings. A single "top pain points across 2025+" query often hedges because 400+ topics is too broad a retrieval target. Splitting into 3–5 per-category or per-theme `local`/`mix` queries usually lands concretely.
- **Narrowing means narrowing by CONCEPT, not by topic count.** Listing 9 topics across 6 sub-concepts in one query ≠ a narrow query. The embedding gets diluted and retrieval gravitates to the dominant concept. Split by concept.
- **Mode-mixing across a chain is fine**: `global` for theme discovery → `local` per named theme for deep-dives → `hybrid` for a component comparison that came up in the first pass.

When chaining, present each step with its specific purpose so the user can stop early if an intermediate step already answered them. Don't queue 5 commands and run them all blindly.

**Canonical chain stages** — a well-composed chain typically progresses through (some of) these, in order. Stop at the stage that answers the question; not every question needs all five.

| Stage | Command shape | Purpose |
|---|---|---|
| 1. **Triangulate** | `stats tags` / `stats categories` / `stats users` / `stats activity` | Learn what the corpus actually contains. Cheap; no SQL needed. |
| 2. **Search** | `stats search "<kw>"` | Full-text probe over post bodies. Resolves spelling / shorthand; cheapest disambiguation. |
| 3. **Filter** | `stats sql "... WHERE ..."` or ad-hoc view queries | Scope the corpus to the question (time, category, tag, author). Deterministic. |
| 4. **Synthesize** | `query --mode <x>` (often multiple, narrow per concept) | Pattern / theme / explanation extraction. The only expensive stage. |
| 5. **Verify** | `stats sql` checking coverage of answer's cited topics | Sanity-check the synthesis against the stats-defined slice. Re-run stage 4 with a different shape if <50% landed. |

For programmatic chains (feeding one output into the next), add `--json` to any stats subcommand — structured output is cleaner to parse than the human table.

#### Graph as a vocabulary / coverage layer for stats

Two sub-patterns where a graph query earns its keep *before or after* stats, not as pure synthesis:

- **Disambiguation before stats.** User input looks unfamiliar (short abbreviation, likely typo, niche shorthand like "Relsh" or "DocRef"), and a first stats pass returned zero or near-zero rows. Before concluding "no data": (1) check the guide's §4.2 / §5 for a close match — cheapest win, no LLM cost. (2) Run `stats search "<term>"` — free full-text over post bodies; if any hits come back, the canonical spelling is in the snippets, extract it and feed back into stats. (3) Only if (1) and (2) both come up empty, run a minimal `--mode local` probe: *"What concept does '<term>' refer to in this corpus? List canonical names and synonyms."* Feed the resolved name back into stats.
- **Coverage verification after stats.** Stats looks reasonable but you suspect alias bleed — users may refer to the same concept with several tokens (abbreviation + long form, product codename + internal jargon, English + translation, nickname + full name). One `--mode local` probe — *"What other names / abbreviations / long forms do users use for `<term>` in this forum?"* — surfaces aliases an `ILIKE` wouldn't match. Redo stats with `OR` over the resolved vocabulary.

Situational, not defaults. Trigger when: the first stats returned suspiciously few rows for a concept you expect to be discussed; the user's term is clearly shorthand / abbreviation / typo; the user asked for comprehensive coverage ("all", "every"). Don't trigger when the term is already canonical in the guide or prior stats results.

### Mode selection for `query`

Authoritative reference: `docs/lightrag/ProgramingWithCore.md` §QueryParam.

| Mode | Retrieves | Pick when (signal → mode) |
|---|---|---|
| `local` | Top-k **entities** similar to query + their neighbors | Anchored on one/two named entities: *"what did X report"*, *"what is X"*, *"explain X"*, specific error code, component lookup, per-topic deep-dive. |
| `global` | Top-k **relationships** + aggregated context | Broad theme synthesis across topics: *"summarize themes in"*, *"why does X cause Y"*, *"recurring pain points in <category>"*. |
| `hybrid` | Local + global (no chunk fetching) | Structural / relational reasoning without prose quotes: *"how does X compare to Y"*, *"how does X relate to Z"*, architecture questions. |
| `mix` (CLI default) | Graph + vector over chunks, merged | Unsure of answer shape; *"how do users typically X"*; fuzzy general questions. Best default. LightRAG docs recommend `mix` when a reranker is configured. |
| `naive` | Plain vector similarity over chunks only | Sanity baseline — *"is the graph adding value over pure RAG"*. |
| `bypass` | No retrieval — raw query to LLM | Prompt-engineering experiments. Rarely useful here. |

## Decision model — knob tuning

Defaults as of 2026-04 (verify against `query.py` / `docs/lightrag/ProgramingWithCore.md` if in doubt): `TOP_K=60`, `CHUNK_TOP_K=20`, `MAX_TOTAL_TOKENS=30000`. Deviate when:

| Knob | Raise when | Lower when |
|---|---|---|
| `TOP_K` (→ 100–150) | Broad / cross-corpus; user wants "comprehensive"; a first run cited only 1–2 topics | Tightly scoped to one named entity; want cheap/fast; context budget tight (→ 20–30) |
| `CHUNK_TOP_K` (→ 30–40) | Troubleshooting / debug; answer needs verbatim quotes, error strings, command output | Summary-style; entity-first where relations matter more than chunks (→ 10) |

### `QUERY_MODEL` override

- **Synthesis-heavy** (multi-document reasoning, trade-offs, long pain-point lists): reasoning-tier model (gpt-5-series). Slower but noticeably better.
- **Narrow lookup** ("what is X", "did Y say Z"): cheap extraction-tier (`gpt-4.1-mini`). No reasoning needed.

### Enable rerank — biggest quality lever after TOP_K

`QueryParam.enable_rerank` defaults to True *when a reranker is configured*. If an answer feels imprecise or cites weakly-relevant chunks, **enabling rerank in `<data-dir>/config/.env` is usually a bigger upgrade than bumping `TOP_K`** — it re-scores retrieved chunks against the query via a cross-encoder:

```
RERANK_PROVIDER=jina   # jina | cohere | ali
RERANK_MODEL=jina-reranker-v2-base-multilingual
RERANK_API_KEY=...
```

Jina: ~$2 / 1M tokens. Cohere: ~$2 / 1K searches. Self-hosted BAAI/bge-reranker-v2-m3 via HuggingFace TEI works through `provider=cohere` + local `RERANK_BASE_URL=http://localhost:8080/rerank`. No re-index needed — rerank is pure query-time. See [MANUAL §Rerank](../../../docs/MANUAL.md#rerank-optional).

## Handling ambiguity — ask when routing isn't obvious

Many questions look one way at first but have two+ defensible interpretations. When the tool or mode choice genuinely depends on user intent, stop and ask the user rather than guess. One unnecessary clarifying question is cheap; running the wrong tool and hedging for 30 seconds is not.

**Rule of thumb:** confident when the question uses unambiguous intent words (*"how many"*, *"summarize"*, *"what is"*, *"rank"*, *"compare"*). In doubt when:

- The question could mean counting or synthesizing (*"users report X"* → `stats search` for count vs `query` for summary).
- A *"tell me about X"* could hit a specific entity (`local`) or broad themes (`global`).
- Intent-depth unclear (*"`<thing>` problems"* → single-error lookup vs cross-topic pattern).
- References a user/concept/error that may or may not be in the graph — run a stats probe first (see "Useful probes").

### Ambiguous-question template

Present 2–3 interpretations, each with its own routed command + one-line rationale. Let the user pick. Example:

> **Q: How should I interpret "`<concept>` problems"?**
>
> - *Troubleshoot a specific symptom I've hit with `<concept>`.* → `query ... --mode local`
> - *Learn the general pattern of problems users discuss around `<concept>`.* → `query ... --mode global`
> - *See which `<concept>`-related topics have the most engagement.* → `stats sql` (rank by posts_count), then drill in.

Don't force this pattern when one routing is obviously right. Only ask when two+ are genuinely plausible.

## Two-tier delegation: route in main, execute in subagent

Keep reasoning and disambiguation in the main conversation. Running commands, parsing output, enriching references via SQL, formatting the answer, and persisting are structured execution tasks that can move to a subagent, which also keeps massive query and log output out of the main conversation's context. Split the work:

**Main conversation — routing + disambiguation**

1. Parse invocation, resolve data dir, read `QUERY-GUIDE.md`.
2. Classify the question and compose the chain (stages, modes, knobs, split vs monolith).
3. Present the recommendation with diagnosis + commands + rationale.
4. Ask the user to resolve sub-concept splits, time scopes, and persistence destination. If you delegate before disambiguating, the subagent has to guess, which defeats the skill's purpose.
5. Delegate execution using the host binding from `HOST-COMPATIBILITY.md`.

**Execution subagent — execute + format + persist**

The subagent's prompt must be self-contained (it inherits none of this conversation). Include:

- **Task framing**: *"Execute this advisor-routed query chain, compose the answer body, persist, and report back a condensed summary."*
- **Exact commands to run** with inline env-var overrides (no routing decisions left for the subagent).
- **Resolved user choices**: persistence destination, sub-concept split, time scope — inputs, not questions.
- **Formatting contract**: summary-table schema (for pain-point shape: `# | sub-concept | pain point | cause | fix | ticket | version | topic`), enriched-references format, coverage-warning template, persistence-file entry format (no leading `---` — see "QUERY-ANSWERS.md entry format").
- **Return contract**: the subagent's final message should be the **condensed echo only** (TL;DR paragraph, summary table, top 3 takeaways, coverage warning, enriched references, footer with path + line count). **No raw query logs; no full per-section prose** — those live in the persisted file. The main conversation passes the subagent's return through to the user verbatim.

**When to skip delegation (stay in main):**

- User chose "just show in chat" (no persistence). Full body lands in main's context regardless; delegating saves nothing.
- Question routes to `stats` only (deterministic, ≤30 rows output). Subagent overhead exceeds savings.
- Stage 1 stats reveals the question is unanswerable (wrong data dir, zero matches on the scope the user confirmed). Return to user, don't spend on execution.

**Subagent prompt skeleton:**

```
You are executing a pre-routed query-advisor chain against the Discourse graph at <data-dir>. Routing and disambiguation are already complete — do not re-ask or re-decide.

Question: <user's original question>
User-confirmed sub-concept split: <list or "none (single query)">
Persistence destination: <path>

Commands to run, in order (synchronous, foreground — concurrent writes to LLM cache corrupt it):
  <stage-1 stats SQL>
  <stage-2 query 1 command>
  <stage-2 query 2 command>
  ...

For each query: capture stdout to /tmp/q<N>.md; extract the answer body after the "Querying ... via <model>..." line.

After all queries complete:
  1. Collect unique topic IDs across all References blocks (regex `topic-(\d+)`).
  2. Run one batched `stats sql` against `topic_summary` (id, title, category, strftime('%Y-%m'), posts_count, tags).
  3. Compose answer body: summary table (schema above) + per-sub-concept prose sections + cross-cutting themes + coverage warning if >50% of stage-1 topics didn't land + enriched References list.
  4. Persist to <path> using the entry format (no leading `---`; start with `## <ISO-datetime> — <title>`; separate from any prior entry with blank-line/---/blank-line).

Return to the main conversation ONLY:
  - TL;DR paragraph (3–5 sentences).
  - The summary table (markdown).
  - Top 3 takeaways as bullets.
  - Coverage warning if applicable.
  - Enriched References block.
  - Footer: "Full answer appended to <path> (<N> lines — <M> entries so far)."

Do NOT echo raw query output, per-section prose, or the full answer body in the return — those are in the file.
```

Fit the skeleton to the chain's actual shape: for a single-query run, the schema/table collapse to whatever structure the answer has; for `stats + query`, include the stats stage rows. **The skeleton is a template, not a literal prompt** — always fill in the resolved commands and user choices before delegating execution.

## Output format

Respond with these parts, in order:

1. **One-sentence diagnosis** of the question's intent (count/filter, synthesis, explanation, comparison).
2. **Recommended command(s)** in a copy-pasteable shell block. Include env-var overrides inline (`TOP_K=N uv run ...`) when deviating from defaults.
3. **Rationale** — 2–3 sentences, structured:
   - *"This is a [count/synthesis/explanation/comparison] question."*
   - *"[tool] is the right fit because [signal that sealed it]."*
   - *"Mode [X] because [what X retrieves that matches the intent]."*
   - *"If the answer feels weak, try [specific next step]."* ← value multiplier; tells the user how to recover without re-asking.
4. **Optional "better phrasing" variant** if the question as stated would hedge badly. Offer a scoped rewrite alongside.
5. **Offer to run + pick answer destination.** Ask the user, and also resolve any still-ambiguous interpretations (sub-concept split, time scope). The decisions gated here unblock the next step.
6. **Execute.** For non-trivial chains, delegate execution per "Two-tier delegation". The subagent persists and returns a condensed echo. Pass it through to the user, then add the footer (path + line count).

## Presenting results in chat

Users see chat; they don't read the MD file unless prompted. Everything consumed in chat follows these rules.

### Chat echo is proportional to persistence

The chat echo's job depends on whether the answer lives elsewhere. Tune the echo to the user's persistence choice — echoing the full body AND writing it to a file roughly doubles the output-token cost of the turn for no benefit, since users who chose persistence will read the file, not the chat.

**When the user chose to persist** (QUERY-ANSWERS.md, timestamped log, custom path) → echo a **condensed summary** (~10–15% of full body). The file has the full version; chat is for making the file discoverable and giving the user enough to decide whether to open it. Condensed echo includes, in order:

1. **TL;DR paragraph** (3–5 sentences): what the answer is, across what scope, what the headline finding was.
2. **Summary table**, if the answer has tabular structure (see "Tables are additive"). This is the skim surface users actually use.
3. **Top 3 cross-cutting takeaways** as a bullet list — the "if you read nothing else" synthesis across the chain.
4. **Coverage warning**, if applicable (citation-number hallucinations, <50% topic coverage, etc.).
5. **Enriched References block** — cheap and load-bearing for user trust.
6. **Footer**: path + line count + entry count, e.g. *"Full answer appended to `<data-dir>/QUERY-ANSWERS.md` (211 lines — 1 entry). Open there for per-section prose."*

**When the user chose "just show in chat"** (no persistence) → echo the **full answer body** with all sections and prose. Chat is the only artifact; there is no file to fall back to.

**The canonical run/persist/echo flow:**

1. Run the query chain directly or delegate execution per "Two-tier delegation".
2. Persist per the user's choice (unless they picked "just show in chat").
3. Echo per the mode above: condensed if persisted, full if show-only.
4. Footer names the persistence path and line count (or is skipped when nothing was written).

Skip the echo entirely only if the user explicitly says so mid-flow (e.g., "too long, just link it" → drop the summary table too, leave a one-line link). Don't skip silently.

### Enrich the References block with topic metadata

Don't let the answer end with a bare list of `topic-NNNN.json` filenames — users can't tell what any topic *is* without opening the file. Before echoing the answer, **enrich the `**References:**` section** with one-line metadata per topic via a single `stats sql` lookup.

Flow:

1. Parse topic IDs from the LLM's References block. Match `topic-(\d+)` — covers both `topic-2747.json` and bare `topic-2747`.
2. Run one batched lookup:
   ```bash
   uv run discourse-explorer stats --path <data-dir> sql \
     "SELECT id, title, category, created_at::DATE AS created, posts_count, tags
      FROM topic_summary WHERE id IN (2747, 3234, 3076, ...)"
   ```
3. Rewrite each reference line in this form — same in chat AND the persisted QUERY-ANSWERS.md entry (they stay consistent):
   - **[N] topic-NNNN** — "Title" · Category · YYYY-MM · N posts · tags: `t1, t2`

Enrichment cost is negligible (single DuckDB call, <100ms) and applies unconditionally — skip only if the answer had no References block at all.

**Failure modes:**

- **ID not found in `topic_summary`** → flag visibly: *"⚠️ not found in corpus; possible LLM fabrication"*. Don't silently drop it; a hallucinated topic ID is a signal the user should see.
- **LLM cited a topic without a numeric ID** (e.g. `topic-<name>.json` by name only, or just a relative reference like "that thread about X") → keep the raw cited form + note *"(no numeric id; metadata unavailable)"*.
- **Multi-reference per line** (e.g. `[1,3,5]`) → expand into separate enriched lines.

This turns raw filenames into self-explanatory citations *and* catches LLM ID hallucinations as a side effect.

### Tables are additive — they don't replace prose

When a synthesis answer has regular structure (topic × {symptom, cause, workaround, fix-version}; component × {responsibility, pitfall}; release × {breaking change}), **add a summary table *alongside* the prose — never replace the explanation with a table alone.**

Two different jobs:

- **The table lets the user scan.** Grep a topic, skim a column, see at a glance which items share a fix-version or a category.
- **The prose explains why each row matters.** Nuance, context, caveats, workaround subtleties that don't fit in a cell — this is the part that makes the answer actually useful.

A table with no prose hides the *why*; prose with no table forces the user to linearly-scan for facts that are naturally tabular. Deliver both.

**Reach for a table when:**

- The answer enumerates ≥3 findings with a consistent schema (the pain-point pattern is the common case: topic / symptom / cause / workaround / fix-version).
- Comparing 2–4 components or approaches with matching attributes.
- Time-scoped deltas (per-version changes).
- The user is likely to *filter* or *lookup* rather than read top-to-bottom.

**Don't force a table when:**

- The answer is a single-entity narrative (no repeating schema).
- Cells would be mostly empty (rows too heterogeneous).
- The table would restate what the prose already covers at the same resolution — it'd compete rather than complement.

Keep tables compact: ≤ ~6 columns, ≤ ~15 rows. Longer belongs back in the stats layer that produced it.

### Stats results must be surfaced + explained, not hidden

When `stats` is part of the answer (pre-query probe, primary result, or post-query verification), **show the actual rows AND interpret them in chat** — don't reduce to *"Result: 40 topics, clustering into X/Y/Z"*. The stats output is the deterministic ground truth the rest of the chain rests on; hiding it makes later steps impossible to sanity-check.

Minimum surfacing for every stats step that feeds downstream work:

1. **The actual rows** (truncated to top-20 if long) as a copy-paste-friendly Markdown table.
2. **A plain-English reading:** total count, category / time / engagement distribution, notable outliers.
3. **The bridge to the next step:** which rows will be scoped into the follow-up query (or trigger halting the chain) *and why*.

Raw table without interpretation defeats the advisor framing. One-line summary without the raw table hides load-bearing facts. Both matter.

### Translate LLM coverage jargon — don't pass it through

When a graph answer says *"topic X wasn't in the retrieved Context"* or *"retrieval didn't include these chunks"*, users often don't know what that means. Translate and decide whether to act.

**Translation (always):** *"topic X wasn't in the retrieved Context"* → *"the graph's retrieval didn't surface any chunks from topic X — the LLM saw the ID in your prompt but had nothing to say about it because its content didn't reach the answer."*

**Reaction based on miss severity:**

- **<50% of named topics landed** → the query shape is the problem. Recommend a re-run: split one broad query into per-topic `--mode local` runs, drop the ID list from the prompt, or bypass the graph with `cat <data-dir>/topics/<id>.json` for strict coverage.
- **Misses are the user's highest-priority topics** → always re-run or fall back to direct JSON read. Don't accept partial coverage silently.
- **Narrow subset landed and the summary is useful** → say so plainly, offer the re-run as optional. Don't bury the miss and carry on.

Never pass "not in Context" through without framing what happened and whether it matters.

### Why prompt-listed topic IDs don't strictly scope retrieval

Recurring confusion: naming topic IDs in the prompt (*"Focus on topics 3226, 3145, 2572, ..."*) does NOT force retrieval to fetch those. Retrieval embeds the whole question as one vector and finds chunks by cosine similarity. Topic IDs are just tokens — they bias the LLM's *phrasing* once chunks are in Context, but don't drive *which* chunks get retrieved.

Consequences:

- **Narrow by CONCEPT, not by topic count.** Listing 9 topics across 6 sub-concepts in one query = retrieval gravitates to the dominant concept and the rest get crowded out.
- **For strict per-topic coverage**, use separate `--mode local` queries (one per topic, anchored on the title) — or bypass the graph with a topic-JSON read.
- **The topic-list pattern is a phrasing hint, not a scoping tool.** Never recommend it to users as "this will make sure those topics get covered".

## Answer persistence — where to write

Forum research accumulates. A one-shot answer shown in chat alone is lost when the session ends; answers are valuable byproducts that should default to being saved, grep-able, reviewable alongside the graph.

### Destination options

**After presenting the recommendation, always ask the user** with these defaults:

| Option | Destination | When |
|---|---|---|
| **Append to `<data-dir>/QUERY-ANSWERS.md`** (Recommended default) | Single growing file at the data dir root, one entry per run, metadata frontmatter. | Most runs — accumulates a searchable log per forum. |
| Write to `<data-dir>/answers/query-<YYYYMMDD-HHMMSS>.md` | One file per query. | Standalone artifact for a teammate, or a large synthesis you want to version. |
| Custom path | Whatever the user types under "Other". | External docs, sibling `docs/` file, etc. |
| Don't write, just show in chat | No file write. | Throwaway questions, debug probes, sanity checks. |

Present these exactly; let the user pick. Don't silently default to "just show in chat" — the small friction of one confirmation click is the cost of not losing answers.

### QUERY-ANSWERS.md entry format

```markdown
## 2026-04-24 02:05 — What are the main pain points with `<named concept>`?

**Tool:** `query`
**Mode:** `mix`
**Model:** `gpt-5.2`
**Knobs:** `CHUNK_TOP_K=30` (default `TOP_K=60`)
**Data dir:** `/Volumes/RAMDisk/discourse.example.com`

**Advisor rationale:** brief explanation of why this routing was chosen.

**Command:**
```bash
uv run discourse-explorer query /Volumes/RAMDisk/discourse.example.com \
  "your question" --mode mix
```

**Answer:**

[full answer body]

**References:**
- **[1] topic-XXXX** — "Title" · Category · YYYY-MM · N posts · tags: `t1, t2`
- **[2] topic-YYYY** — "Title" · Category · YYYY-MM · N posts · tags: `t1, t2`
```

References are enriched from `topic_summary` metadata — see **Enrich the References block with topic metadata** in §"Presenting results in chat".

**Markdown rendering gotcha — do NOT start an entry (or the file) with a `---` line.** GitHub, VS Code, Obsidian, Jekyll, and most other Markdown renderers treat a leading `---` as the opening delimiter of YAML frontmatter and silently consume content until the next `---` on its own line. When a second `---` appears later in the entry (as a section separator, or even further down), everything between the two `---` lines disappears from the rendered output. Start every entry with its `##` header directly, with no preamble.

File structure:

- **First line of the file** = the first entry's `## <ISO datetime> — <title>` heading. No preamble, no leading `---`, no frontmatter.
- **Between entries** = blank line, then `---` on its own line, then blank line (renders as `<hr>`; the blank-line padding prevents setext-heading promotion of the previous line). This intra-entry separator is safe because no renderer interprets a `---` far from the file start as frontmatter.
- **Chronological order**: oldest entries first; newest appended at the end.

### How to append

1. **Append to the end** of `<data-dir>/QUERY-ANSWERS.md` (chronological; newest last — standard log convention).
2. **Start each entry with its `##` header directly — no leading `---`.** See "Markdown rendering gotcha" above.
3. If the file doesn't exist, `Write` it with the first entry's `##` header as line 1.
4. If the file exists, `Edit` to append. Anchor on a unique tail string (the last entry's References block), then insert `\n\n---\n\n` followed by the new entry's `## ...` heading. The blank-line padding around `---` is load-bearing for correct `<hr>` rendering.
5. Follow the run/persist/echo flow under **Chat echo is proportional to persistence** for everything else.

Skip persistence even when "Append to QUERY-ANSWERS.md" was chosen if:

- The `query` command failed (non-zero exit). Persist the error only if non-trivial.
- The user ran `stats` alone that printed a table — those belong in a shell session, not a Markdown log. Only persist `query` answer text.

## Useful probes before advising

Cheap, no-LLM probes to run when the question hinges on a name you haven't verified or the guide doesn't cover:

```bash
# What categories exist?
uv run discourse-explorer stats --path <data-dir> categories

# What tags are most popular?
uv run discourse-explorer stats --path <data-dir> tags | head -20

# Who are the top 20 posters?
uv run discourse-explorer stats --path <data-dir> users | head -20

# Does a specific user exist?
uv run discourse-explorer stats --path <data-dir> sql \
  "SELECT DISTINCT username FROM posts WHERE username ILIKE '%<name>%'"

# Is a specific topic in the graph?
uv run discourse-explorer stats --path <data-dir> search "<keyword>"
```

Use these when the question hinges on a name you haven't verified (see "Don't invent data paths" in the triangulation principle above), during "Disambiguation before stats", or during "Composition over monoliths" triangulation.

## SQL invariants — avoid wrong results

Five quirks of the `stats.py` views that will silently produce wrong answers if you hand-roll SQL around them:

- **Filter and group tags on `tag_label`, never on `tag_name`.** There is no bare `tag` column — `WHERE tag = '...'` raises a binder error — but `tag_name` is the subtler trap: it holds Discourse's *display* name, which is not stable across scrapes. The same release tag (id 144, slug `2025-06`) appears as `2025․06` with a `U+2024` one-dot leader in topics fetched before ~2026-08 and as `2025-06` after, so grouping by `tag_name` splits one release into two rows and filtering by it undercounts. Measured on the production corpus 2026-08-14: `WHERE tag_name = '2025-06'` → **66** rows, `WHERE tag_label = '2025-06'` → **214**.
- **`tag_label` is what `stats tags` prints and what the graph's tag nodes are named**, because both derive from the slug via `config.tag_label`. So a tag copied out of `stats tags` output can be pasted straight into a `tag_label` filter, and it will match the graph too. `topic_tags` also exposes `tag_id`, `tag_name` (display) and `tag_slug` (raw) when you specifically need them; `topic_summary.tags` is a comma-joined list of `tag_label`.
- **Corollary for the U+2024 quirk:** you no longer need `LIKE 'prefix%suffix'` gymnastics to dodge the lookalike character. Use `tag_label` and the homoglyph never appears.
- **`topic_participants.role = 'system'`** is derived from `username = 'system'` literally — it catches Discourse's built-in system account only, **not a general bot filter**. Other bot accounts (custom usernames) land in `responder`. For broad bot exclusion, filter by `username` explicitly.
- **`response_time` is an INTERVAL**; aggregating requires `EXTRACT(EPOCH FROM response_time)` to get seconds. Naive `AVG(response_time)` will error or cast wrong.
- **Category resolution is already done in `topics`**: the `category` column is the resolved name (subcategory → parent). Don't re-join `categories.json`; use `parent_category` if you need the hierarchy level.
- **`topic_summary.tags` is comma-joined text**, not an array; filter with `tags LIKE '%<tag-name>%'`.

Full view contract: [`docs/analysis/duckdb-views.md`](../../../docs/analysis/duckdb-views.md).

## Worked examples

**Entity names below are illustrative.** Substitute with real names from your corpus's `QUERY-GUIDE.md` §4.2 (top entities) and §4.3 (categories). What's universal about each example is the *shape* of the routing decision — not the specific tokens.

### Example: bad-shape question rerouted

User: *"What are the top-10 pain points in this forum?"*

First step — this is a report-shape ask; offer `/forum-report` as the redirect (see "When to invoke"). If the user declines and wants a one-shot answer, route as below.

```
Diagnosis: broad frequency-ranked synthesis — no counts in the graph; query alone will hedge.

Recommended:
    # Step 1 — stats: which categories generate the most discussion?
    uv run discourse-explorer stats --path <data-dir> sql \
      "SELECT category, COUNT(*) AS n FROM topics WHERE category != 'Unknown'
       GROUP BY category ORDER BY n DESC LIMIT 5"

    # Step 2 — query: synthesize pain points in the top categories.
    uv run discourse-explorer query <data-dir> \
      "What are the major recurring pain points users discuss in the top categories?
       Include causes and proposed workarounds." --mode global

Rationale: "top-10" is a ranking claim the graph can't support. Stats provides deterministic
category ranking; global mode then synthesizes themes over the named slice.

Better phrasing (single run): "Summarize the major recurring pain points in the top-3 categories
from the stats query above." --mode global
```

### Example: entity-anchored → local mode

User: *"What did <user> report about <component>?"*

```
Diagnosis: named-user + named-concept lookup. Entity-anchored.

Recommended:
    uv run discourse-explorer query <data-dir> \
      "What did <user> report about <component>?" --mode local

Rationale: <user> is a structural User node; local mode retrieves around user+component
entities and their neighbors, which is how forum-authorship graph signal works.

For completeness (no hedging): a structured stats fallback if the answer is thin:
    uv run discourse-explorer stats --path <data-dir> sql \
      "SELECT DISTINCT t.title FROM topic_participants tp
       JOIN topics t ON t.id = tp.topic_id
       WHERE tp.username = '<user>'
         AND t.title ILIKE '%<component>%'"
```

### Example: architecture question → hybrid mode

User: *"How does <component A> differ from <component B>?"*

```
Diagnosis: structural comparison of two named components. No prose quotes needed.

Recommended:
    uv run discourse-explorer query <data-dir> \
      "How does <component A> differ from <component B> in terms of responsibilities,
       customization points, and common pitfalls?" --mode hybrid

Rationale: both are entities with rich relation graphs; hybrid surfaces structural
edges without paying chunk-retrieval cost. Drop to mix if the answer feels under-cited.
```

### Example: counting / frequency → stats only

User: *"How many topics are tagged `<tag>`?"*

```
Diagnosis: exact count filter. No synthesis needed.

Recommended:
    # First, verify the exact tag name (watch for Unicode lookalikes):
    uv run discourse-explorer stats --path <data-dir> tags | grep -i '<tag>'
    # Then count:
    uv run discourse-explorer stats --path <data-dir> sql \
      "SELECT COUNT(*) FROM topic_tags WHERE tag_label = '<exact tag from step 1>'"

Rationale: graph can't count; DuckDB can. Filter on `tag_label` — it is exactly
what `stats tags` printed in step 1, and it matches the graph's tag node names.
Do NOT use `tag_name`: that is the display name, which varies by scrape date, so
it splits one release across two values and undercounts (66 vs 214 on a real
corpus). There is no bare `tag` column. See "SQL invariants".
```

### Example: unanswered + drill-in (built-in stats + per-topic synthesis)

User: *"What open questions are piling up in `<category>`?"*

```
Diagnosis: hybrid of count ("open questions") and synthesis ("what they're asking").
Start with the built-in stats subcommand, then drill into each interesting title.

Recommended:
    # Step 1 — stats: what's open? (stats unanswered is a built-in subcommand;
    # don't re-derive it in ad-hoc SQL)
    uv run discourse-explorer stats --path <data-dir> unanswered \
      | grep -i '<category>' | head -10

    # Step 2 — query: per-topic local deep-dive (repeat for titles of interest)
    uv run discourse-explorer query <data-dir> \
      "Summarize the thread titled '<paste title from step 1>' — problem,
       constraints, what's missing for an answer." --mode local

Rationale: 'unanswered' is a dedicated stats subcommand; using it beats rolling
a `WHERE posts_count = 1 AND NOT closed` query by hand. Per-topic --mode local
is the right synthesis path because each unanswered thread is entity-anchored;
batching them into one query would hedge (see "Why prompt-listed topic IDs don't
strictly scope retrieval").
```

### Example: sanity baseline

User: *"Compare graph retrieval vs pure vector search on this question."*

```
Recommended: run the same question twice, once with default mix, once with --mode naive.
    uv run discourse-explorer query <data-dir> "question" --mode mix
    uv run discourse-explorer query <data-dir> "question" --mode naive

Rationale: naive skips the graph entirely. If naive is roughly as good, the graph
isn't adding signal on this question and you can save compute.
```
