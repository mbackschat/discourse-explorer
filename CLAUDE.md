# Discourse Explorer

Generic Python scraper for any Discourse forum with GraphRAG-powered querying. Five CLI tools: `scraper` (HTTP + filesystem), `discover_types` (schema discovery for the KG vocabulary), `query` (GraphRAG index/ask), `visualize` (HTML graph), `stats` (DuckDB analytics). Only `scraper` talks to the Discourse instance; `discover_types` and `query` make LLM calls to OpenAI or Ollama; the rest are fully offline.

> **Audience.** This file is a maintainer-facing map. User-facing project overview + setup live in `README.md`; per-tool usage reference (CLI flags, env vars, examples, end-to-end workflow) lives in `docs/MANUAL.md`; deep technical references under `docs/analysis/`. Don't duplicate here.

## RULE #1: never name the source forum or its operator. Anywhere.

This repository is public. The forum it was built against, and the organization that runs it, are not. Neither the hostname, nor the short name, nor the organization's initials may appear in **anything that ships**: source, comments, docstrings, tests, fixtures, docs, skill files, log samples, branch names, and **commit messages** — messages especially, because a message cannot be corrected without rewriting history.

**The forbidden strings are deliberately not written down here.** Naming them in a tracked file would publish exactly what the rule suppresses. They live outside the repository, one case-insensitive regex per line, in:

```
${XDG_CONFIG_HOME:-~/.config}/discourse-explorer/private-names
```

**Check before every commit, and always before a publish:**

```bash
./scripts/check-private-names.sh                 # HEAD content + commit messages
./scripts/check-private-names.sh <ref>           # audit any other ref
```

Exit 1 means a hit, exit 2 means the pattern file is missing (set it up rather than skipping the check).

Write around the name instead of writing it. "the production corpus" carries every meaning "the *<name>* corpus" did, in the same number of words. Measurements and counts are worth keeping; it is only the identity that must go.

**If a name reaches the public remote, the fix is a fresh squashed root, not a follow-up commit.** A scrub commit cleans the tip and leaves the string in every prior commit, which is where anyone would find it. Re-rooting requires the maintainer's explicit go-ahead and is only safe while nothing depends on this repository's history.

**Audit commit metadata too, not just content.** Author and committer emails are not reachable by any content search and are shown on every commit by the host, so an old work address published on every commit is invisible to `git grep` and to `check-private-names.sh` alike. Pin the identity per repository rather than inheriting a global default:

```bash
git config --local user.email <the address you want published>
```

Note that a force-push does not erase the old objects on the host immediately — they stay reachable by SHA until the host garbage-collects. Deleting and recreating the repository is the only guaranteed purge.

## RULE #2: be SSD-aware. Review every code path that writes to disk, before running it.

This and RULE #1 override everything else in this file. The data dir lives on an external SSD, and this codebase can turn a handful of logical edits into tens of gigabytes of writes without any warning in the output.

**Before running anything that mutates `graphrag/`, count the flushes.** Not the edits, the *flushes*. Ask: how many times does this write a whole file to disk? If the answer is more than once per phase, it is wrong and must be batched.

Why the arithmetic is so bad here:

- `FaissVectorDBStorage.index_done_callback` has **no dirty guard**. Every call rewrites all three index files in full, ~500MB on a 1.4K-topic corpus, whether or not anything changed. (`JsonKVStorage` does check `storage_updated`; Faiss does not.)
- `NetworkXStorage.index_done_callback` rewrites the entire ~20MB graphml.
- **Every LightRAG CRUD helper flushes on every call**: `amerge_entities`, `aedit_entity`, `aedit_relation`, `adelete_by_relation`, `adelete_by_entity`. One call, one full flush.

So a loop of N edits costs **N × ~520MB**. Fifty edits is 26GB.

Measured per call, with the delegating `_*_impl` layer resolved:

| call | flushes | scope |
|---|---|---|
| `aedit_entity`, `amerge_entities`, `aedit_relation` | 1 | 5 storages |
| `adelete_by_relation`, `adelete_by_entity`, `acreate_*` | 1 | 5 storages |
| `ainsert_custom_kg` | 1 | **all 12** |
| **`adelete_by_doc_id`** | **2** | **all 12** |
| `aquery` → `_query_done` | 1 | the ~99MB LLM response cache |

**Use `batched_graph_writes(rag)`. It is the only sanctioned way to mutate the graph in bulk.**

```python
async with batched_graph_writes(rag):
    for src, tgt in doomed:
        await rag.adelete_by_relation(src, tgt)
# exactly one write per file, here
```

It suppresses **all twelve** storages (not just the five `_persist_graph_updates` touches), so it also covers the `_insert_done` path used by `ainsert_custom_kg` and `adelete_by_doc_id`. The single flush on exit is `_flush_ledger_last`: ordered, and it raises rather than swallowing a failed write. If the body raises, the flush is skipped and the edits are dropped, which is the safe direction.

`tests/test_index_safety_guards.py::BatchedGraphWritesTests` asserts one-write-per-file for a 50-edit loop, and proves the assertion is not vacuous by showing the same loop costs 50 without it.

Pass 1, Pass 3 and Pass 4 use the lower-level `_suppress_index_done` + `_flush_storages` pair directly. New code should prefer `batched_graph_writes`.

**Verify, do not assume.** Instrument `networkx.write_graphml`, `faiss.write_index` and `lightrag.utils.write_json`, then count writes per file and assert at most one. Reading the call graph is not enough: the public helpers delegate to `_*_impl` functions, so an AST scan of `aedit_entity` shows zero flush sites while the real cost is one full flush per call.

**The failure mode is silent and fast.** A repair touching 54 edges (34 `adelete_by_relation`, 12 `aedit_relation`, 8 `aedit_entity`) in a bare loop is ~27GB of writes to change 54 edges, and nothing in the output says so. Killing such a run mid-write tears the graphml and forces a restore from backup, so the damage is not bounded by noticing quickly. The tools to prevent it live in the same module as the calls that cause it.

The general form of the rule is not LightRAG-specific: **any loop that rewrites a whole file per iteration is a bug.** Batch the writes, or accumulate in memory and write once. This is also why `PERSIST_EVERY` exists for Pass 2, and why the Pass 1 checkpoint short-circuits when nothing was inserted.

## Branch layout: one branch, published directly

`main` is the only branch, it tracks `github/main`, and it is what gets pushed. There is no separate public branch.

**Before pushing, run the RULE #1 check.** It is the only gate that matters here, because a push is immediate:

```bash
./scripts/check-private-names.sh && git push github main
```

## Design principles

- **Intuitive UI + simplicity over configurability.** When a UX problem can be solved either by (a) making default behavior predictable + composable, or (b) adding a toggle/setting for an exceptional case, prefer **(a)** unless there's a concrete, recurring user need for the knob. Filters should compose; derived / automatic UI (1-hop halos, neighbor dimming, search context, pin context) should respect every other active filter rather than bypass them. **Explicit user actions (clicks, searches) can be sticky in proportion to their explicitness, but their *derived* highlights — neighbor dimming, halos — must still pass every active filter.** Two adjacent toggles read like a settings panel; one is digestible; zero is best when the default is right. Avoid settings creep. The maintainer's stated preference: "I honor intuitive UIs and simplicity."

## Commands

```bash
uv sync                                                                                      # install deps
uv run discourse-explorer scrape <URL> --output <PATH> [--full|--dry-run]         # scrape
uv run discourse-explorer discover-types <PATH> [--sample-size N] [--top N]         # discover entity-type vocabulary
uv run discourse-explorer discover-types <PATH> --show-artifact                     # review prior run (no LLM cost)
uv run discourse-explorer query <PATH> --index [--clear] [--gleaning N] [--limit N] # build/rebuild knowledge graph
uv run discourse-explorer query <PATH> --detect-limits                              # probe OpenAI RPM/TPM + recommend concurrency
uv run discourse-explorer query <PATH> "question" [--mode local|global|mix]         # query
./scripts/index.sh --resume | --full                                                # detached index; mode REQUIRED (bare = exit 64), see below
uv run discourse-explorer visualize <PATH> [--open] [--hub-label-count N]           # emit <PATH>/visualize/graph.html (opens in Node View)
uv run discourse-explorer stats --path <PATH> <subcmd>                              # tags|users|categories|activity|unanswered|search|sql
```

### Running a full index: hard-won rules

**0. NEVER check whether an index is running with `pgrep -f "discourse_explorer.query"`.** That pattern cannot match the console entry point, whose command line is `discourse-explorer query` — hyphen, space. Use **both** forms, always:

```bash
pgrep -fl "discourse-explorer query|discourse_explorer.query"
```

Getting this wrong is the most expensive mistake available here. When every liveness check comes back empty, runs believed dead are still alive, and a fresh indexer launched on top of one gives concurrent writers on a single `graphrag/` — measured at a drop from **15,756 nodes to 4,566**. The symptoms (storage-lock contention, `storage_updated` reload churn, stalled embedding pools) look like network, disk or OOM problems, so the true cause is easy to chase for hours.

`query.py` now takes an exclusive `flock` on the data dir (`index_lock`, lock file `.index.lock`) and exits code 2 naming the holder, so a second indexer *cannot* start. The lock is the guarantee; the grep is only for diagnosis. A stale lock file is harmless — the OS drops the lock when the process exits, including on SIGKILL.

**1. Launch full `--index` runs only via `./scripts/index.sh`.** It starts the run in its own session (Python `start_new_session=True`, since macOS has no `setsid`) so a process-group kill aimed at the launching shell cannot reach it — `nohup` alone does **not** achieve this, it only blocks SIGHUP. It also sets `PYTHONUNBUFFERED=1`; without it the log block-buffers and trails reality by minutes, which is how a healthy run got misread as dead. Short `--limit N` sample runs may be invoked directly.

**2. Never set indexing knobs on the launch command line.** Every setting comes from the layered `.env` chain, so a run is reproducible from config alone. A knob exported by the launcher instead makes the same nominal command behave differently through the script than typed by hand — `FORCE_LLM_SUMMARY_ON_MERGE` alone is a 3–5× cost difference — and leaves the user unable to override it. It belongs in `config.SUMMARY_ON_MERGE_DEFAULT`, resolved by `bootstrap()` and passed explicitly to the LightRAG constructor.

**3. Never change `topic_to_document`'s output to "improve" it.** That text is what LightRAG hashes for document-level dedupe, so any edit re-keys every affected document and converts an incremental update into a full re-extraction. Normalizing tags there rewrote 1,018 of 1,399 doc ids and turned an 85-document update into 1,099 documents, ~13× the cost. Identity normalization belongs in the graph node names (`config.tag_label`), which are not part of the hashed text — `config.tag_display` deliberately keeps the display name for the header.

**4. Trust file state over log tails, and always confirm a "dead" process.** Before concluding a run has stopped, check the process list with rule 0's pattern *and* whether files under `graphrag/` are still changing. A quiet log means nothing.

`<PATH>` resolves via CLI ≻ shell `DISCOURSE_DATA_DIR` ≻ `<project-root>/.env`. Per-run env (URL, auth, models, gleaning, concurrency) lives in `<data-dir>/config/.env` — data-dir values override project-root dotenv. `config.bootstrap(cli_data_dir)` is the single entry point that resolves the chain and returns a frozen `RuntimeConfig`. Full reference: `docs/analysis/vocabulary-and-config.md`.

## Workflows (packaged as skills)

Recurring multi-step operations are packaged as project-scoped skills. Their canonical source is `.claude/skills/<name>/SKILL.md`; Codex discovers the same files through the `.agents/skills` symlink. Each skill's frontmatter enumerates its trigger phrases and description. Human-facing catalog and host-specific invocation syntax live in `docs/MANUAL.md` under "Guided workflows via Claude Code and Codex". Read `.claude/skills/HOST-COMPATIBILITY.md` for the authoritative mapping from semantic workflow operations to host-specific tools.

**When the user explicitly invokes a skill or uses one of its trigger phrases, invoke the matching skill through the current host's skill mechanism rather than improvising.** `discover-entity-types`, `index-and-embed`, and `create-query-guide` delegate to the corresponding `docs/workflows/*.md` runbook; `query-advisor` and `forum-report` encode their own decision models. Ask the user at every defined decision point and halt on prerequisite failures. Improvising risks skipping the backup, the cost estimate, the prerequisite vocabulary check, or routing a report-shaped ask into a single hedging query.

Maintainer-facing invariants:

- `discover-entity-types` and `index-and-embed` are write-paths over the data dir (modifying `entity_types.json` and `graphrag/`). Always ask for the data dir explicitly.
- `query-advisor` and `forum-report` are read-only against the graph. Both persist output (advisor → `QUERY-ANSWERS.md`; report → `reports/<type>/NN-<date>-<slug>.md`) and silently use `DISCOURSE_DATA_DIR` when unambiguous — a wrong data dir on a read-only path costs at most one wasted run.
- Both skills use the shared two-tier pattern for non-trivial execution. Disambiguation always stays in the main conversation. `forum-report` additionally gates drill-down cost before L2+ (`--mode local` / `--mode global`) spend.
- Report vs. question: thematic + plural phrasing (*audit, top themes, analyze the forum*) → `forum-report`; one specific question → `query-advisor`. The advisor's "When to invoke" section encodes the redirect trigger.

## Reference documentation

Per-file catalog of `docs/` with full blurbs: **[`docs/index.md`](docs/index.md)**. The pointers below tell me when to crack each subdirectory.

- **`docs/lightrag/`** — read before editing `query.py` / `discover_types.py` or debugging LightRAG. Triggers: `ainsert*` changes, `addon_params` / gleaning / chunk tuning, `_topic_to_custom_kg` edits, query-mode or retrieval debugging. WebSearch is accepted for API surfaces not covered locally, or anything time-varying (OpenAI model lineup, pricing, rate-limit tiers).
- **`docs/discourse/`** — read before editing `scraper.py` or writing field-aware queries. Triggers: new topic-JSON field, pagination changes, DuckDB field references, `_topic_to_custom_kg` extensions, `raw` vs `plain_text` decisions.
- **`docs/ideas/`** — read when the user asks "what's next?" or before scoping a feature in the same area as an existing proposal.
- **`docs/analysis/`** — deep-dives on indexing, canonicalization, visualizer build/runtime, configuration. Open the relevant file when working on that subsystem; full blurbs in [`docs/index.md`](docs/index.md).

## Verifying changes

```bash
uv run python -c "import ast; [ast.parse(open(f).read()) for f in ['discourse_explorer/config.py','discourse_explorer/scraper.py','discourse_explorer/query.py','discourse_explorer/visualize.py','discourse_explorer/rel_clusters.py','discourse_explorer/stats.py','discourse_explorer/discover_types.py','discourse_explorer/auth.py']]"
uv run discourse-explorer scrape --help
uv run discourse-explorer scrape <URL> --output /tmp/x --dry-run
uv run python -m unittest discover -s tests
```

The `sample/` subtree carries its own lighter test suite and test posture. When changing it, follow [`sample/CLAUDE.md`](sample/CLAUDE.md) and run `uv run python -m unittest discover -s sample/tests`. Favour `--dry-run` on the scraper and running the offline tools against an existing data directory for regression checks.

## Architecture + invariants

Module-by-module map + cross-cutting invariants (the gotchas that recent code has silently broken when someone didn't know them): **[`docs/analysis/architecture-map.md`](docs/analysis/architecture-map.md)**.

Read this before making non-trivial changes. Each module entry links to its dedicated deep-dive doc.

## DuckDB views (`stats.py`)

Full view list, columns, and example queries: **[`docs/analysis/duckdb-views.md`](docs/analysis/duckdb-views.md)**. At runtime, `.tables` and `.schema` inside the `sql` REPL are authoritative over the doc.
