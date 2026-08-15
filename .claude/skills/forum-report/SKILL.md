---
name: forum-report
description: >-
  Compose a pre-designed analytical report against a scraped Discourse corpus — pain-points audit,
  community health, decision brief, and (for software-platform forums) release-quality audit or
  customization-ceiling audit. Produces a multi-section Markdown artifact at
  `<data-dir>/reports/<type>/NN-<date>-<slug>.md`, built from stats probes with optional per-topic
  graph drill-downs. Use when the user wants a **report** (thematic synthesis, multi-section
  deliverable) rather than a one-shot answer. Triggers: "forum audit", "pain-points report",
  "community health", "analyze the forum", "summarize the forum", "report on X", "top themes",
  "top pain points", "decision brief", "executive summary of the forum". (Also: "release-quality
  audit", "customization audit" for software-platform corpora.) For single-question asks (route
  mode + knobs for one question), use `/query-advisor` instead.
---

# Forum report

## Host compatibility

Before executing this skill, read [`../HOST-COMPATIBILITY.md`](../HOST-COMPATIBILITY.md). Operations such as “ask the user,” “invoke the skill,” and “delegate execution” use the host bindings defined there.

Compose a structured, multi-section **report** against a Discourse corpus. Unlike `/query-advisor` (which routes a *question*), this skill produces a *deliverable*: a pre-shaped narrative built from stats probes plus optional graph drill-downs into the high-signal topics the probes surface.

## When to invoke

- User asks for a report shape: *"give me a pain-points audit"*, *"forum health report"*, *"top themes in the `<category>` category"*, *"summarize recurring complaints"*, *"what's the state of the forum"*.
- Phrasing is thematic and plural: *top*, *themes*, *audit*, *analyze*, *overview*, *report on*, *summarize the forum*.
- User types `/forum-report` explicitly, or a natural-language trigger from the description frontmatter.

**When NOT this skill** — route to `/query-advisor` instead:

- Single-topic, single-entity questions: *"what did `<user>` say about `<concept>`"*, *"what causes `<specific symptom>`"*, *"summarize topic NNNN"*.
- Exact counts or lookups: *"how many topics are tagged `<tag>`"*.

If a user starts with a report-shape phrase but really wants one question answered, ask the user to confirm before committing.

## The flow

1. **Parse invocation + resolve data dir** — same rules as `/query-advisor`: CLI arg ≻ `DISCOURSE_DATA_DIR` ≻ ask the user.
2. **Read `<data-dir>/QUERY-GUIDE.md`** — corpus ground truth (scale, top entities, categories, version tags, blind spots). Halt (soft) if missing; offer to generate it first.
3. **Pick report type** by asking the user — from the catalog below, plus "Custom" for an ad-hoc shape.
4. **Pick depth level** (L0–L3) — default per report type; user can override.
5. **Confirm scope + persistence path** — full corpus vs. time-windowed vs. category-scoped; path defaults to `<data-dir>/reports/<type>/<NN>-<YYYY-MM-DD>-<slug>.md`.
6. **Run the stats skeleton** for the chosen type. Surface intermediate results in chat with one-line interpretation per probe.
7. **Gate on drill-down cost** — for L2/L3, enumerate the top-N candidates the triggers surfaced and ask the user before spending on graph queries.
8. **Delegate execution** using `/query-advisor`'s two-tier delegation pattern for anything above L1.
9. **Persist** to the path.
10. **Condensed chat echo** — TL;DR, top findings, drill-down coverage warning if any, pointer to file. Never echo the full report body.

## Report-type catalog

Five starter types. Each defines a purpose, target reader, recommended depth, skeleton probes, drill-down triggers, and narrative outline.

### Portability — which types apply to which forums

| Type | Universal? | When it applies |
|---|---|---|
| Pain-points audit | **Yes** | Any forum — users complain about problems everywhere (software bugs, failed recipes, wilted plants, broken techniques). |
| Community health | **Yes** | Any forum — response time, helper concentration, unanswered-topic density are always meaningful. |
| Decision brief | **Yes** | Any forum — distilled top findings + recommendations work for any stakeholder audience. |
| Release/version-quality audit | **Conditional** | Only when the corpus is about a versioned product and topics are tagged with version identifiers. Skip for general-interest forums, hobby communities, etc. |
| Customization-ceiling audit | **Conditional** | Only when the forum is about an extensible software platform (framework, SDK, API-driven product). For non-software forums this type is inapplicable. |

If the user picks a conditional type against a forum where it doesn't apply, stop and ask the user how to proceed — offer the closest universal type + the "Custom" escape.

### Vocabulary warning: calibrate probes to the corpus

**The probe keyword lists below are starter sets, illustrative only.** Before running any theme-frequency sweep, read `QUERY-GUIDE.md` §4.2 (top entities), §4.3/§4.4 (categories/versions), §5 (relation verbs), and §6 (question library) to identify the *corpus-specific* pain vocabulary. Then add those terms to the sweep.

- A **software-platform** forum speaks in `customize | not supported | workaround | crashes | error`.
- A **cooking** forum speaks in `burned | curdled | fell flat | didn't rise | overcooked`.
- A **gardening** forum speaks in `wilted | didn't sprout | pest | overwatered`.
- A **board-game** forum speaks in `unbalanced | house rule | exploit | confusing`.

The probe *shape* (keyword-frequency sweep → crosstab → top-engagement list → staff-admitted-gap density) is universal. The *vocabulary* must be calibrated per corpus. Reject the starter list uncritically, especially for non-software forums — irrelevant keywords produce empty tables and false confidence.

### 1. Pain-points audit (universal)

- **Purpose.** Surface recurring problems, unresolved complaints, and confirmed gaps — whatever the forum is about.
- **Reader.** Whoever owns the problem space: product manager, community moderator, course instructor, editorial lead, maintainer. The label adapts to the corpus; the report's job is to show "what keeps going wrong" regardless.
- **Recommended depth.** **L2** — themes from stats, then `--mode local` per top unresolved item.

**Skeleton probes:**

1. **Theme frequency over post bodies** — sweep pain-signal keywords. **Calibrate the keyword list to the corpus before running** (see "Vocabulary warning" above).
   ```sql
   SELECT COUNT(DISTINCT topic_id) AS topics, COUNT(*) AS posts
   FROM posts WHERE raw_body ILIKE '%<theme>%'
   ```
   Starter themes — some universally negative (*not working, broken, stopped, failed, can't, wrong, doesn't*); some general-distress markers (*problem, issue, same issue, same problem, help, stuck, confused, unclear*). Add corpus-specific vocabulary from `QUERY-GUIDE.md` §4.2, §5, §6. A non-software corpus will replace *customize* / *workaround* / *not supported* with whatever users say when things go wrong in that domain.
2. **Top-engagement unresolved** — deterministic, corpus-agnostic:
   ```sql
   SELECT id, title, category, posts_count, views, created_at
   FROM topic_summary
   WHERE posts_count >= 5 AND (closed = false OR has_accepted_answer = false)
   ORDER BY posts_count DESC, views DESC LIMIT 20
   ```
3. **Category × theme crosstab** — which categories concentrate which themes. Reveals domain structure of the pain.
4. **Staff/expert-admitted-gap density** — posts from *staff/admins/moderators/recognized experts* containing gap-admission phrasing (universal starter: `not supported | not possible | can't do that | no way to | won't work`). In any forum, recurring authoritative "no" is a strong signal. Adapt phrasing from `QUERY-GUIDE.md` §5/§6 if the corpus has a different vernacular.

**Drill-down triggers (L2):**

- Top 10 by `posts_count DESC` from probe 2 → `--mode local` per topic title.
- Topics matching the **repeat-user-confirmation** trigger (see "Drill-down triggers" below).

**Narrative outline:**

- Executive summary (top 5 themes by count + views)
- Per-theme sections: topic list, engagement metrics, drill-down quotes (for L2+)
- Cross-cutting patterns (what recurs across themes)
- Unresolved hotspots (top-10 worth a human read)
- Enriched References
- Reproducibility — cross-link to companion file `queries/<same-name>.md` (mandatory — see §"Stats and query commands")

### 2. Release/version-quality audit (conditional — versioned-product forums)

- **When this applies.** The corpus is about a versioned product (software, hardware, a game edition, a published spec) AND topics carry version-identifier tags. Check `QUERY-GUIDE.md` §4.4 for version tags. If none exist, skip this type or use Custom.
- **Purpose.** Assess release-by-release quality — breakage density, migration pain, post-release hotspots.
- **Reader.** Release manager, migration planner, QA lead, product owner for that version line.
- **Recommended depth.** **L2** — per-version breakage list + drill into top breakages for symptom/cause/fix-version.

**Skeleton probes:**

1. **Topics per version tag** — pattern depends on corpus version-tag convention. `QUERY-GUIDE.md` §4.4 lists them. If tags are year-based (`2024.06`, `2025.06`), filter `LIKE '20%'`; if semver (`v1.2.3`), filter `LIKE 'v%' OR LIKE '_.%.%'`; etc. Also watch for Unicode-quirk version separators (e.g., `U+2024` one-dot-leader in some Discourse instances — see `/query-advisor` §"SQL invariants").
   ```sql
   SELECT tag_label, COUNT(*) AS topics
   FROM topic_tags WHERE tag_label <corpus-specific version filter>
   GROUP BY tag_label ORDER BY 1 DESC
   ```
2. **Post-release pain keyword density per version** — join topics-by-version-tag to posts body containing upgrade/regression vocabulary. Starter: `upgrade | migration | regression | after upgrading | stopped working | broken after | doesn't work since`. Calibrate to the product's community's actual idiom.
3. **Error-pattern density per version** — corpus-specific error strings / symptom phrases surfaced by `QUERY-GUIDE.md` §3.1 or §6 if the guide catalogs them.
4. **Top engagement per version** — `topic_summary` + `topic_tags`, order by `posts_count DESC` within each version tag.
5. **Fix-version mentions** — post-body matches against **corpus-specific ticket/fix-version patterns**. Derive the regex from `QUERY-GUIDE.md`: look for how staff cite tickets (`JIRA-\d+`, `GH-\d+`, `#\d+`, `FOO-\d+`), what "fixed in" phrasing they use (*fixed in, shipped in, targeted for, will ship in, planned for*), and extension-release conventions if any.

**Drill-down triggers (L2):**

- Top 5 by engagement per recent version (typically the 2 most-recent major tags) → `--mode local` per topic.

**Narrative outline:**

- Per-version summary (counts, median posts, top breakages, unanswered rate)
- Migration pain patterns (recurring issues across versions)
- Fix-version tracking map (topic ↔ ticket ↔ target release)
- Release readiness recommendations
- Enriched References
- Reproducibility — cross-link to companion file `queries/<same-name>.md` (mandatory — see §"Stats and query commands")

### 3. Community health (universal)

- **Purpose.** Ops view — response times, bus factor, unanswered hotspots, contributor concentration, topic velocity.
- **Reader.** Community manager, forum moderator, ops lead, the team (or volunteer) maintaining the forum. Applies to any Discourse community regardless of subject.
- **Recommended depth.** **L0–L1** — pure stats. Graph usually adds nothing when the questions are about people and timing, not content.

**Skeleton probes:**

1. **Median first-response time per category** —
   ```sql
   SELECT category, COUNT(*) AS topics,
          EXTRACT(EPOCH FROM MEDIAN(response_time))/3600 AS hours_to_first_reply
   FROM topic_threads WHERE response_time IS NOT NULL
   GROUP BY category ORDER BY 3 DESC
   ```
2. **Top responders per category + share** — flag categories where one person handles ≥30%.
3. **Unanswered hotspots** — `stats unanswered` output, filtered by `views >= 50`.
4. **Per-user activity** — from `user_activity`; top responders, top askers, first-seen/last-seen.
5. **Topic velocity over time** — topics per month, quarter-over-quarter delta.

**Drill-down triggers:** none by default. If the user wants to explore specific unanswered hotspots, upgrade to L2 for those rows only.

**Narrative outline:**

- Category health matrix (volume × response time × bus factor)
- Top helpers and concentration warnings
- Unanswered hotspots
- Trending categories (velocity deltas)
- Recommendations (where to invest moderation/expertise)
- Reproducibility — cross-link to companion file `queries/<same-name>.md` (mandatory — see §"Stats and query commands")

### 4. Customization / extension-point ceiling (conditional — software-platform forums)

- **When this applies.** The corpus is about an **extensible software platform** — a framework, SDK, or API-driven product where end users write code against an extension surface. Typical signals: a `customize`/`override`/`plugin`/`extend` vocabulary, dedicated categories for integrations/widgets/APIs, topics discussing listener/hook/event mechanisms. For forums without this context (hobby, community, course, content-consumption forums), skip this type.
- **Purpose.** Map where the declarative platform runs out and users are forced into custom code or unofficial extension mechanisms. Reveals platform-openness gaps.
- **Reader.** Platform architect, API designer, developer advocate, product owner prioritizing extension-point investment.
- **Recommended depth.** **L3** — stats names themes; `--mode local` per top topic; `--mode global` per theme for cross-topic pattern synthesis.

**Skeleton probes (calibrate vocabulary from `QUERY-GUIDE.md` §4.2 / §5 / §6 for this platform's idiom):**

1. **Customization-language frequency** over post bodies — starter sweep: `customize | override | extend | plugin | hook | callback | listener | event handler | custom | workaround | not supported`. Replace or extend with the platform's native extension vocabulary (e.g., sagas, annotations, decorators, data providers, middleware, adapters — whichever terms this platform uses).
2. **Per-category customization concentration** — which categories generate the most custom-code discussion.
3. **Request-vs-denial ratio** — density of request phrasing (`is it possible | is there a way | we need | can we | how do I`) vs. denial phrasing (`currently not possible | not supported | no plan | short answer is no | won't work`).
4. **Response-time delta** — compare median first-response time for customization topics vs. non-customization topics. A large gap signals that the platform team doesn't have good answers for extension questions.

**Drill-down triggers (L3):**

- Top 10 customization topics by engagement → `--mode local` per topic.
- Top 5 customization themes by frequency → `--mode global` per theme for cross-topic pattern synthesis.

**Narrative outline:**

- Customization signal density (how pervasive is the gap?)
- Per-category ceiling map
- Recurring themes — populate with the **actual themes this corpus surfaces** from probe 1 (for a UI-heavy platform: visibility/readonly/styling/lifecycle; for a data-pipeline platform: transforms/schemas/connectors; for an API gateway: auth/rate-limits/middleware — whichever dimensions show up).
- Unofficial extension mechanisms in use — name whichever this platform's community has invented (sagas, listeners, annotations, plugins, middleware, user scripts, CSS overrides, etc. — derived from probe 1 rather than assumed).
- Recommendations for official extension APIs
- Enriched References
- Reproducibility — cross-link to companion file `queries/<same-name>.md` (mandatory — see §"Stats and query commands")

### 5. Decision brief / exec summary (universal)

- **Purpose.** Distilled top-5 structural findings + prioritized recommendations for whoever's making the decisions.
- **Reader.** Senior stakeholder for the forum's subject: product leadership, community owner, editorial director, course author, research lead. The role name is domain-specific; the need — "what matters most, what should we do about it" — is universal.
- **Recommended depth.** **L3, scoped to top-5 findings only** — deep synthesis, narrow scope. Relies on prior reports in `<data-dir>/reports/` if they exist.

**Skeleton probes:**

1. **Survey prior reports** — list `<data-dir>/reports/*/` and read any relevant artifacts for cached findings (Read tool, no SQL).
2. **Top-5 structural gaps by combined signal** — highest combined score of `posts_count`, `views`, denial-phrasing density, and unresolved rate. Exact weighting is corpus-dependent.
3. **Response-time risk** — category × bus-factor × slow-response combined into a per-category risk score.
4. **Emerging vs. persistent** — what's trended up in the most recent time window vs. what's been unfixed the longest.

**Drill-down triggers:**

- The top 5 structural gaps only → each gets a `--mode local` + `--mode global` pair.

**Narrative outline:**

- The big picture — what works, what doesn't
- Top 5 structural gaps (one section each, sourced from drill-downs)
- Prioritized recommendations (Critical / High / Medium / Lower)
- Meta-insight (one-paragraph thesis grounded in the counts, not abstract claims)
- Enriched References
- Reproducibility — cross-link to companion file `queries/<same-name>.md` (mandatory — see §"Stats and query commands")

## Depth levels

Four gradations. The report type's recommended depth is the default; the user can override.

| Level | What it produces | Typical cost | When it's enough |
|---|---|---|---|
| **L0** | Single DuckDB subcommand output (`stats tags`, `stats unanswered`, etc.) | ~free | One count or list; no narrative. |
| **L1** | Multi-probe stats synthesis — keyword sweeps, cross-tabs, frequency tables + narrative. No graph. | ~free | Themes and frequencies are the deliverable; reader wants "what's the shape", not "what does each topic say". |
| **L2** | L1 + `--mode local` drill-down per top-N surfaced topic | ~N × ~$0.05 (gpt-5.2) | Need per-topic detail for the highest-signal items — symptoms, quoted causes, workaround recipes. |
| **L3** | L2 + `--mode global` over theme slices for cross-topic pattern synthesis | ~K × ~$0.10 | Need cross-topic themes, fix-version maps, recommendations grounded in actual content. |

**Escalation/de-escalation:** if L1 feels thin, escalate specific rows to L2 without redoing probes. If L3 is too expensive, run L1 first as an intermediate artifact and decide from there.

## Drill-down triggers (stats-derived, not LLM judgment)

Universal SQL patterns that surface drill-down candidates deterministically. Run them as part of the skeleton; the subagent drills into whatever rows they return.

| Trigger | SQL shape | Use for |
|---|---|---|
| **High engagement** | `posts_count >= 5 AND views >= 50` | Most-discussed topics; broad heuristic. |
| **High-view low-answer** | `views >= 80 AND posts_count <= 2` | Docs-debt candidates; unmet needs. |
| **Repeat-user confirmation** | ≥2 distinct users with posts containing `same problem\|same issue\|we also\|confirming this` | Cross-project issues. |
| **Authority-admitted gap** | A post from staff / admin / recognized expert contains denial phrasing. Starter (software): `not supported\|currently not possible\|no plan\|there's no way\|short answer is no`. Non-software equivalents: `can't do that\|won't work\|wrong approach\|not a thing\|doesn't exist`. Calibrate from `QUERY-GUIDE.md` §5/§6. | Confirmed gaps. |
| **Self-answered late** | `first_poster_username = last_poster_username AND posts_count >= 3 AND last_post_at - first_post_at >= 7 days` | Self-service gaps — user eventually figured it out alone. |
| **Unresolved despite discussion** | `posts_count >= 5 AND (closed = false OR has_accepted_answer = false)` | Active dead-ends. |
| **Cross-user multi-post** | ≥3 distinct users, `posts_count >= 5` | Genuinely widespread issues. |

Thresholds above are starting points — surface them in the drill-down gate prompt so the user can raise/lower before approving spend.

## Execution flow

1. **Skeleton.** Run all stats probes for the chosen type. Capture rows; build intermediate Markdown tables. Show each table in chat with a one-line interpretation ("Top themes: `workaround` in 47 topics, `not supported` in 27 topics, …").
2. **Gate (L2+).** Enumerate drill-down candidates:
   - *"Proceed with per-topic drill-down — 10 topics × `--mode local` = ~$0.50, ~3 min. Approve?"*
   - *"Narrow to top-K instead"* (default K=5)
   - *"Skip drill-down, deliver L1"*
3. **Delegate execution** (L2+) with the forum-report subagent prompt skeleton defined below. Populate it with the exact per-topic commands, persistence path, report-type narrative outline, incremental command-log capture rule, report numbering, showboat behavior, and return contract. It follows `/query-advisor`'s two-tier separation but does not reuse the advisor's skeleton verbatim.
4. **Synthesize narrative** per the report type's outline. Keep tables ≤6 columns / ≤15 rows (anything bigger belongs back in stats).
5. **Persist** to `<data-dir>/reports/<type>/<NN>-<YYYY-MM-DD>-<slug>.md`. NN = `(max existing + 1)` zero-padded; start at `01`.
6. **Condensed chat echo** — TL;DR + top findings + drill-down coverage + pointer. Never echo the full body.

## Persistence

**Path convention:** `<data-dir>/reports/<type>/<NN>-<YYYY-MM-DD>-<slug>.md`

- `<type>` ∈ `pain-points`, `release-quality`, `community-health`, `customization-ceiling`, `decision-brief`, `custom`.
- `<NN>` — zero-padded sequence within that type's directory. Start `01`; increment per run.
- `<YYYY-MM-DD>` — generation date.
- `<slug>` — lowercase-kebab summary (`full-corpus`, `2025.06`, `modeling-category`, `q2-2026`).

**Companion file:** `<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>.md` — same filename, sibling `queries/` subfolder. See §"Stats and query commands" for content + cross-link convention.

**Replayable showboat doc** (auto-generated when `showboat` is on PATH): `<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md` — same stem as the companion with `-showboat` suffix. See §"Showboat replayable variant" for the strict rules around fences in `note` content and the deterministic-`ORDER BY` requirement.

Examples:
- `reports/pain-points/01-2026-04-24-full-corpus.md` + `reports/pain-points/queries/01-2026-04-24-full-corpus.md` (+ `01-2026-04-24-full-corpus-showboat.md` if showboat available)
- `reports/release-quality/01-2026-04-24-2025.06-audit.md` + `reports/release-quality/queries/01-2026-04-24-2025.06-audit.md`
- `reports/community-health/02-2026-05-15-q2-update.md` + `reports/community-health/queries/02-2026-05-15-q2-update.md`

**Markdown rendering gotcha** — same rule as `QUERY-ANSWERS.md`: **do NOT start the file or any section with a leading `---` line**. GitHub/Obsidian/VS Code treat it as YAML frontmatter and swallow content until the next `---`. Start with `# <title>`. Between major sections use blank-line / `---` / blank-line for a clean `<hr>`.

**Report header format** (plain metadata block, not YAML):

```markdown
# <Report title>

**Report type:** pain-points
**Depth:** L2
**Data dir:** /Volumes/RAMDisk/discourse.example.com
**Scope:** full corpus (1,331 topics) | 2024+ (731 topics) | custom
**Generated:** 2026-04-24
**Skeleton probes:** 4 (listed below)
**Drill-downs:** 10 × `--mode local`

---

## Executive summary
...
```

## Stats and query commands (mandatory companion file)

Every report has a **companion file** at `<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>.md` — same filename as the report, in a sibling `queries/` subfolder — listing the exact commands used. Two reasons:

- **Reproducibility.** Re-running the same probes against a future graph state is the whole point of change reports and the natural follow-up for any audit. Without the commands, a reader has to reconstruct them from result tables.
- **Auditability.** Numbers in the narrative are checkable only if the SQL is visible. The U+2024 version-tag quirk, keyword-sweep CTEs, response-time conversions — none are obvious from result tables alone.

**Why a sibling file (not an inline appendix).** Stakeholder readers want a clean narrative; reviewers / auditors want commands. A separate file serves both without HTML tricks (`<details>` rendering varies across Markdown renderers), keeps the report short, and lets the queries file be opened, diffed, or re-run independently.

**Companion file structure** — start with `# Stats and query commands — <Report title>`, then a metadata block pointing back to the report, then `###` subsections in this order:

1. **Schema verification** — `DESCRIBE` / `information_schema` calls run while validating columns.
2. **Skeleton probes** — every `discourse-explorer stats … sql "…"` and `discourse-explorer stats <subcmd>` call run during the skeleton phase, in order. Include the literal SQL text (not paraphrased).
3. **Drill-downs (L2+)** — every `discourse-explorer query <data-dir> "…" --mode <mode>` call, one per fenced bash block, with a `# topic NNNN — <slug>` comment line above each.
4. **Enriched-references batch** — the single batched `discourse-explorer stats … sql` against `topic_summary` used for the References table.

**Report-side cross-link.** The report itself ends with a brief `## Reproducibility` section pointing at the companion (and the showboat doc, when one was emitted):

```markdown
## Reproducibility

All commands run to produce this report — schema verification, skeleton probes, drill-downs, and the enriched-references batch — are captured in the companion file: [`queries/<NN>-<YYYY-MM-DD>-<slug>.md`](queries/<NN>-<YYYY-MM-DD>-<slug>.md). Re-run any block there to verify a number or rebuild a probe against a fresh index.

**Replayable variant** (stats blocks only — LLM drill-downs excluded by design): [`queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md`](queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md). Verify manually from the project root with `showboat verify <path>`. (If you invoke from elsewhere, use `showboat --workdir <project-root> verify <path>` — `uv run` and the captured `--path <relative>` arguments both need a uv-project-root CWD.) _(Omit this whole paragraph when `showboat` was not on PATH at generation time.)_
```

**Companion-side back-link.** The companion file's metadata block points back the other way:

```markdown
# Stats and query commands — <Report title>

Companion to [`../<NN>-<YYYY-MM-DD>-<slug>.md`](../<NN>-<YYYY-MM-DD>-<slug>.md).
Replayable variant: [`<NN>-<YYYY-MM-DD>-<slug>-showboat.md`](<NN>-<YYYY-MM-DD>-<slug>-showboat.md). _(Omit if not generated.)_

**Generated:** <YYYY-MM-DD>
**Data dir:** <DATA_DIR>
**Total commands:** <N>
```

**Capture rule for the subagent.** As the subagent executes commands, append each to a running command log. Build the companion file from the log incrementally — do **not** reconstruct from memory at the end (drift risk). **Persist the companion file before persisting the report** so the report's cross-link is never broken.

**Don't** include stdout / result tables in the companion file; those already appear in the report body. The companion is *commands only*.

## Showboat replayable variant (auto when `showboat` is on PATH)

When `command -v showboat >/dev/null 2>&1` succeeds, also emit a third artifact at `<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md`. This is a [showboat](https://github.com/anthropics/showboat-style-tool)-format Markdown doc whose probe blocks can be re-run via `showboat verify <path>` and diffed against captured output — turning the audit's numbers into independently verifiable claims rather than trust-me tables.

If `showboat` is **not** on PATH, skip silently with one chat-echo line (*"showboat not on PATH — skipped replayable variant."*). Do not error; do not ask.

### Critical invariant: never put executable-language fences inside `note` content

`showboat verify` parses the rendered Markdown for fenced code blocks and **re-executes every block whose language is not `output` or `image`**. The capture-time origin (`exec` vs. `note`) is invisible to verify — both end up as `\`\`\`bash` fences in the same file.

Consequences:

- ❌ Don't write ` ```bash showboat verify <self> ``` ` inside an intro `note` — verify will recurse on every replay.
- ❌ Don't write ` ```bash uv run discourse-explorer query … --mode local ``` ` inside a "drill-down example" note — verify will fire a real LLM API call on every replay.
- ❌ Comments like *"NOT executed"* above a fenced block don't change anything; showboat doesn't read them.

Real incident: an early version of this skill embedded `showboat verify <self>` inside a `note`. The recursion combined with concurrent retries produced a **2,300-process explosion**. Separately, a drill-down `note` containing a `bash` fence with `discourse-explorer query --mode local` made silent OpenAI API calls on every verify, polluting the `kv_store_llm_response_cache.json` fixture. Both were the same class of bug.

**For non-runnable examples in `note` content, use:**

- **Inline backticks**: ``` `uv run discourse-explorer query … --mode local` ``` (single line, narrative-friendly)
- **Quote blocks**: `> uv run discourse-explorer query …` (multi-line, still inert)
- **Indented code**: 4-space leading indent renders as code in Markdown and is invisible to showboat verify

### What goes in the doc — and what doesn't

`exec` blocks (re-run by `verify`):

1. Schema verification (`information_schema`, `DESCRIBE …`).
2. Every skeleton probe (the literal `discourse-explorer stats … sql "…"` calls).
3. The enriched-references batch.

`note` blocks (Markdown text only — never executable fences):

- Title, generation metadata, caveats.
- Section headers between exec groups.
- One-line interpretation per probe.
- Drill-down explanation: describe what L2+ runs would do, in prose. The example command goes in inline code or a quote block, never a fence.

LLM drill-downs (`discourse-explorer query … --mode local|global`) are **never** included as `exec` blocks — they're non-deterministic (verify would diff every replay) and they cost real money per call (verify over 10 drill-downs is ~$0.50). The commands-only companion file is where every drill-down command lives for L2+ runs.

### Determinism rule for `ORDER BY`

DuckDB's tie-breaking on rows with equal sort keys is **not** stable across runs. Every `ORDER BY` in an `exec`-captured probe MUST end with a unique-or-near-unique tiebreaker — `id`, `category`, `tag_label`, `theme`, etc. Without this, `verify` will diff same-numbers-different-row-order and look like a regression that isn't one. Calibration examples:

- `ORDER BY topics DESC, posts DESC` → add `, theme` (or `tag_label`).
- `ORDER BY posts_count DESC, like_count DESC` → add `, id` (likes can be all-zero in seed fixtures, useless as tiebreaker).
- `ORDER BY topics DESC` over a category column → add `, category`.

This rule applies only to `exec` blocks. The commands-only companion file may keep narrative-readable `ORDER BY` clauses without tiebreakers (no verify runs against it).

### The skill never runs `showboat verify` itself

Verification is a **user-initiated** manual step. The skill's responsibility ends at generation. Reasons:

- Concurrent verifies on the same doc fan out heavy subprocess chains (`showboat → bash → uv → python → duckdb` per `exec` block × N stacked verifies).
- A skill that runs verify ties the skill's success to a transient runtime state (the user might already be running their own verify in another shell).
- Verify costs scale with corpus size (the canonical 1331-topic corpus may take 30–60s); blocking the skill on it is friction with no payoff.

The chat echo footer must **never** report a verify result. Just point the user at the command.

### Generation pattern

```bash
SB="<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md"
showboat init "$SB" "<Report title> (replayable)"

# Intro note — prose only. No executable fences.
showboat note "$SB" "Companion to ../<NN>-...md (narrative) and sibling to <NN>-...md (commands-only).

Re-run from the project root with: \`showboat verify <path-to-this-doc>\`. From any other CWD, pass the project root explicitly: \`showboat --workdir <project-root> verify <path-to-this-doc>\`."

# Probe section — header note + exec block. Never put bash inside the note.
showboat note "$SB" "## Skeleton probes

### Probe A — <one-line description>. Tiebreaker: \`id\`."

cat <<'BASH' | showboat exec "$SB" bash >/dev/null
uv run discourse-explorer stats --path <data-dir> sql "
SELECT ...
ORDER BY ..., id
"
BASH
```

### Path convention

`<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md` — same filename as the companion, with `-showboat` suffix. Persist **after** both the report and the companion file: it's the bonus artifact, and a failed showboat generation must not break the canonical pair.

### How the user verifies

From the project root:

```bash
showboat verify <data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md
```

From any other CWD, pin the project root explicitly so `uv run` and the captured `--path <relative>` arguments resolve:

```bash
showboat --workdir <project-root> verify <path-to-doc>
```

A clean run is silent (exit 0). A diff means either the underlying graph snapshot has changed (re-scrape, re-index, fixture regeneration — investigate before treating as a bug) or a probe lacks a deterministic `ORDER BY` tiebreaker (fix the probe; see § "Determinism rule").

### Recovery if a verify goes wrong

In the rare case a verify hangs or you accidentally launch concurrent runs:

```bash
pkill -9 -f "showboat verify"
pkill -9 -f "discourse-explorer stats"
pgrep -fl showboat | wc -l   # confirm 0
```

The skill's invariants (no executable fences in notes, deterministic `ORDER BY`) are the primary defense — they make recursive verify and persistent diffs structurally impossible. The kill commands above are a fallback for ad-hoc concurrent invocations.

## Chat echo is proportional to persistence

Reports are **always persisted** (no show-only mode — they're too long, and the user chose this skill specifically because they want a deliverable). Chat always gets a **condensed summary**:

1. One-paragraph TL;DR of the report's headline finding.
2. Report type + depth + scope (one line).
3. Top 3–5 findings as bullets.
4. Drill-down coverage note if applicable (*N drilled / M candidates; misses flagged*).
5. Enriched References for drilled topics (L2+ only).
6. Footer:
   - *"Full report at `<path>` (`<N>` lines)."*
   - *"Companion: `<companion-path>` (`<N>` lines)."*
   - When showboat was used: *"Replayable variant: `<showboat-path>` (`<N>` blocks). Verify with `showboat verify <path>` from the project root."* Never report a verify result — the skill does not run verify.
   - When showboat was not on PATH: *"showboat not on PATH — skipped replayable variant."*

Never echo the full report body. Same reasoning as `/query-advisor`: double-writing doubles output-token cost for no benefit.

## Two-tier delegation

Same pattern as `/query-advisor` §"Two-tier delegation":

- **Main conversation** — pick report type, depth, scope, persistence path, and gate drill-down cost. Keep every user decision here.
- **Execution subagent** — run skeleton probes + drill-downs, compose narrative, persist, and return a condensed echo.

**Skip delegation** for:

- L0 / L1 runs with <10 probes and no LLM calls — stay in main; overhead exceeds savings.
- Reports the user wants to inspect probe-by-probe rather than receive as a finished artifact.

Subagent prompt skeleton (adapt to the chosen report type):

```
You are executing a pre-routed forum-report chain against the Discourse graph at <data-dir>.
Report type: <type>
Depth: <L1|L2|L3>
Scope: <full corpus | 2024+ | custom>
Persistence path: <path>

Maintain a running command log throughout — every command you execute (schema verification, skeleton probes, drill-downs, enriched-references batch) gets appended verbatim to the log as you go. The log becomes the companion file.

Skeleton probes (run in order, capture each as a Markdown table AND append the literal command to the running log):
  <probe 1 SQL>
  <probe 2 SQL>
  ...

Drill-down candidates (approved): <list of topic IDs + titles>
Per candidate, run (and append to log):
  uv run discourse-explorer query <data-dir> \
    "<question template anchored on the title>" --mode local

After all commands complete:
  1. Collect unique topic IDs from drill-down References blocks.
  2. Enrich via one batched `stats sql` against `topic_summary` (append to log).
  3. Compose narrative per the report's outline. End with a brief `## Reproducibility` section cross-linking to the companion file at `queries/<same-filename>.md` per §"Stats and query commands". If `command -v showboat` succeeds, also include the showboat-doc cross-link paragraph from that template.
  4. Build the companion file at `<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>.md` (same filename as the report) from the running log. Persist this BEFORE the report so the cross-link is never broken.
  5. Persist the report to <path> using the "no leading `---`" rule.
  6. If `command -v showboat >/dev/null 2>&1` succeeds, build the replayable variant at `<data-dir>/reports/<type>/queries/<NN>-<YYYY-MM-DD>-<slug>-showboat.md` per §"Showboat replayable variant". Strict invariants: only stats blocks become `exec`; every `note` is prose-only with no executable-language fences; every `exec` probe has a deterministic `ORDER BY` ending in a tiebreaker. **Do NOT run `showboat verify`** — that is a user-initiated step. The skill's job ends at generation. If `showboat` is absent, skip silently.

Return to the main conversation ONLY:
  - TL;DR paragraph (3–5 sentences).
  - Top 3–5 findings as bullets.
  - Drill-down coverage note.
  - Enriched References (drilled topics only).
  - Footer: "Full report at <path> (<N> lines)."

Do NOT echo full probe tables, full drill-down prose, or the narrative body.
```

## Shared references

Inherited unchanged from `/query-advisor`:

- **Data-dir resolution** — `/query-advisor` §"Parse the invocation".
- **QUERY-GUIDE.md reading** — corpus ground truth for entity / category / version constraints.
- **Two-tier delegation** — the main conversation routes and the execution subagent executes.
- **Enriched-references recipe** — one batched `stats sql` against `topic_summary` to add title/category/date/posts/tags to every cited topic ID; also catches LLM ID hallucinations.
- **Markdown rendering gotcha** — no leading `---`.
- **SQL invariants** — group/filter tags on `tag_label` (not `tag_name`, which is the scrape-date-dependent display name and undercounts), `role='system'` ≠ general bot filter, etc. `/query-advisor` §"SQL invariants".

Reference material:

- [`docs/analysis/duckdb-views.md`](../../../docs/analysis/duckdb-views.md) — view + column reference.
- [`docs/lightrag/ProgramingWithCore.md`](../../../docs/lightrag/ProgramingWithCore.md) — `QueryParam` reference for drill-down mode selection.
- Example report shapes: check `<data-dir>/reports/` — if a project has generated reports before, they're reference implementations of the shapes.

## Adding new report types

The catalog above is the starter set. Future entries follow the same format:

- **Purpose** (one line — what decision this supports)
- **Reader** (one line — who acts on this)
- **Recommended depth** (L0/L1/L2/L3)
- **Skeleton probes** (3–6 items, each with SQL shape or command)
- **Drill-down triggers** (SQL conditions)
- **Narrative outline** (section headings in order — always followed by the mandatory `Reproducibility` cross-link section pointing at the companion file at `queries/<same-name>.md`, defined once at §"Stats and query commands"; don't repeat it in your outline)

Candidate future types: requirements radar, emerging-themes / trend report, knowledge-base gap report, moderation-load report (flagged / edited / deleted density), newcomer-onboarding friction, seasonal / cyclical patterns, cross-forum comparison. Some will apply to every Discourse corpus; some are conditional (e.g., software-specific ones like documentation-coverage audit, integration landscape, security-topics audit). Mark the conditional ones explicitly, same as types 2 and 4 above.
