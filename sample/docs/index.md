# Sample subtree — architecture map

Entry point for the `sample/` subtree's documentation. The deep dives under `analysis/` are the sources of truth; this file is the router.

## What this subtree is

A self-contained synthetic-Discourse-forum seeder. Brings up a local Discourse via Docker, seeds it with a fictional fan-community forum derived from a single integer seed, and exposes the same `init` / `extend` lifecycle the parent project's tools (`scrape`, `discover-types`, `query`, `visualize`, `stats`) consume against a real forum.

The seeder is supporting infrastructure, not production code: tests are pragmatic (cover invariants, not every branch); LLM body generation is opt-out (`--no-llm` for fast structural runs); cache files for the LLM-generated bodies are committed so re-bakes don't pay the LLM bill twice.

## Where to look

Read these in order on first contact:

1. **[`sample/README.md`](../README.md)** — user-facing: how to run `init` / `extend`, scale presets, troubleshooting, fixture demo. Start here if you just want to use the seeder.
2. **[`sample/CLAUDE.md`](../CLAUDE.md)** — maintainer scope: subtree-local invariants (no real names, determinism, generator hygiene, blocklist gate), test posture, package wiring. Read every session.
3. **[`analysis/seeder-internals.md`](analysis/seeder-internals.md)** — design + module map: the Crown of Brine universe, scale presets, determinism contract, injected dynamics, the four pipeline shapes (`build_forum`, `push_forum`, `extend_forum`, `push_extension`), per-module purpose, why Docker. Read before changing anything in `sample/seed/`.
4. **[`analysis/live-push-walkthrough.md`](analysis/live-push-walkthrough.md)** — operational runbook for live mode: cold-boot timing, what `make api-key` mints, push timing, site-settings the seeder tunes, rate-limit handling, partial-fail recovery, browser review, parent-scraper verification. Read before re-running live mode or touching `push_forum` / `push_extension`.
5. **[`analysis/provenance-and-costs.md`](analysis/provenance-and-costs.md)** — cache + cost rationale: how the committed JSON cache was generated, what triggers re-generation, model-choice rationale, how to re-bake safely. Read before changing anything that might invalidate the cache (renames in `crown_of_brine.py`, model swaps, seed/scale changes).

## When to update which doc

| Change                                       | Doc                                          |
|----------------------------------------------|----------------------------------------------|
| User-visible CLI surface, new run example    | `README.md`                                  |
| Subtree invariant, test posture, conventions | `CLAUDE.md`                                  |
| Module added / removed / re-shaped           | `analysis/seeder-internals.md`               |
| Pipeline shape changed (init/extend/push)    | `analysis/seeder-internals.md`               |
| Universe edit (categories, tags, lore)       | `analysis/seeder-internals.md`               |
| Live-mode operational quirk discovered       | `analysis/live-push-walkthrough.md`          |
| LLM model / cache key / re-bake procedure    | `analysis/provenance-and-costs.md`           |
