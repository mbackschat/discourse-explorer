# Sample subtree — maintainer notes

Scope-local instructions for `sample/`. Read alongside the project root `CLAUDE.md`. Architecture, module map, and pipeline shapes live in [`docs/index.md`](docs/index.md) → its `analysis/` deep dives — start there before non-trivial changes.

## Subtree-local invariants

- **No real names anywhere.** No real game studios, franchises, engines, characters, persons, or companies in any generated content (titles, post bodies, usernames, tags, category names, lore vocab). Hardware/OS platforms by real name (Amiga, Atari ST, Commodore 64, DOS, classic Macintosh, FM Towns, Windows, Linux, Steam Deck, Nintendo Switch, iOS, Android) are explicitly OK in the platform-tag axis because that's how fan forums actually discuss running classic games — the *only* exception. The blocklist post-filters every generated body; a hit is treated as a rename bug.
- **Determinism via seed.** All randomness flows through `Rng` instances derived from `GenerationSpec.rng(*salt)`. No module-level `random.choice` (or equivalent) anywhere. Each generator asks for its own salted stream (`spec.rng("categories")`, …) so adding or re-ordering a generator doesn't shift unrelated outputs. Same `(seed, scale, product)` → bit-for-bit identical structure across machines.
- **Generator hygiene.** Reservation sets cover every per-axis floor; generators don't paper over a missing axis or reservation gap. Generators raise on malformed product constants (typos, infeasible targets, empty pools) instead of silently truncating. Validation at multiple layers (module load, factory entry, `__post_init__`) is intentional — cost is negligible, value is catching bad state at the earliest possible moment.
- **Body generation is gated by the blocklist post-filter.** `content/bodies.generate_body` runs every LLM response through `content/blocklist.check`. Hits trigger up to 3 retries with an explicit avoidance note; a 4th still-banned response raises `BlocklistViolation` rather than shipping real-franchise leaks. A failing/unavailable LLM falls back to a deterministic template body but does NOT cache it. The cache is also untouched on a hard `BlocklistViolation` so the next run gets a clean attempt.

## The fixture's tags are bare strings, and that is correct

`sample/fixtures/seed42-tiny/topics/*.json` stores `tags` as `["remaster", "bug", ...]` — a plain list of strings. The production corpus stores `tags` as a list of objects: `[{"id": 144, "name": "2025-06", "slug": "2025-06"}, ...]`.

**Both shapes are legitimate and both must keep working.** The sample scrapes `bitnamilegacy/discourse`, whose API returns the string form; current Discourse returns the object form. `stats.py`'s `topic_tags` view normalizes every shape through `to_json` + `json_extract_string` precisely so neither is privileged — including the `JSON[]` type DuckDB infers when a glob spans both.

Do **not** "fix" the fixture to match production. Rewriting it to the object form would delete the only regression coverage for the string path, and `tag_id` legitimately reads `NULL` here for the same reason. See [`docs/analysis/duckdb-views.md`](../docs/analysis/duckdb-views.md) for the column contract.

## The fixture is the skill suite's committed output, not just a demo corpus

`fixtures/seed42-tiny/` was **built by the skills themselves**, and every skill left its artifact behind. That makes it a regression oracle for the skills, which is a second job on top of being an offline demo. Treat it accordingly.

| Artifact | Produced by |
|---|---|
| `graphrag/`, `logs/INDEX_AND_EMBED-*.full.stdout.txt`, `logs/INDEX_AND_EMBED-*.md`, `config/index-and-embed-*.json` | `/index-and-embed` |
| `QUERY-GUIDE.md`, `logs/CREATE-QUERY-GUIDE-*.md`, `config/create-query-guide-*.json` | `/create-query-guide` |
| `visualize/graph.html`, `visualize/data.js`, `visualize/cache/` | `/index-and-embed` step 9 |
| `answers/query-*.md` | `/query-advisor` |
| `reports/pain-points/`, `reports/community-health/` | `/forum-report` |

**The committed stdout log answers questions without running anything.** `logs/INDEX_AND_EMBED-20260429-021123.full.stdout.txt` is a full healthy run, so it is the reference for what the pass sequence, the indentation, and the completion lines actually look like. Three examples of what it settles on inspection: Pass 4 runs inside a normal `--index` (so a follow-up `--canonicalize-only` is a no-op, not a cleanup step); milestone lines sit at column 0 while progress lines are indented, so a `^Pass ` log filter silently drops every progress tick and must be `^ *Pass `; and `Pass 1 complete:` is matched by prefix in the skill, so its trailing fields can change safely.

**A sample-sized run does not exercise the Pass 1 checkpoint machinery.** 33 topics is below the 100-topic progress tick and far below `PASS1_CHECKPOINT_EVERY = 250` in `query.py`, which is why the committed log contains no `Pass 1 progress:` lines at all. A green sample run covers the launcher, the data-dir lock, the phase-boundary flush, the ledger write, and the skip path on a second run. It does **not** cover the mid-pass checkpoint. Do not read a passing sample run as coverage of that path.

**Nor does it exercise the document purge.** Pass 1 deletes a topic's previously-recorded documents before re-seeding it, but only when that topic's payload *changed* (`_pass1_plan` → `RESEED`). Two runs over an untouched fixture produce `INSERT` then `SKIP` and never once `RESEED`, so `N stale doc(s) purged` stays at zero and the whole `adelete_by_doc_id` path goes untouched. Exercising it means editing a topic in the copy between the two runs — change a tag, drop a post — and then checking that the old tag node is gone rather than merely joined by a new one.

The fixture also carries no `graphrag/pass1_payload_hashes.json`: it predates the ledger and the file is not ignored, just absent. A first run against a copy therefore seeds all 33 topics and writes a v2 ledger, which is the intended path, not a fault.

**Never re-run a skill against the committed fixture.** It rewrites ~20MB of version-controlled artifacts and costs real LLM spend. Copy it first (`cp -a sample/fixtures/seed42-tiny /tmp/sample-test`) and point the skill at the copy.

## Test posture — pragmatic, NOT strict red-green

The project root prefers red/green TDD; **this subtree overrides that**. The sample seeder is supporting infrastructure, not production logic, so the friction-to-value tradeoff tilts toward lighter ceremony:

- Cover invariants that matter (determinism, count ranges, set membership, blocklist filtering) — but write the implementation first, then the tests, then run them.
- No need for intermediate failing-test commits.
- Snapshot exact strings (`topics[0].title == "..."`) only after a generator stabilizes; structural assertions come first.
- Stochastic invariants (Pareto top-20 share, burst density) need thresholds chosen against finite-sample reality, not the theoretical asymptote. Validate empirically across a sweep of seeds before pinning.

## Test discovery + package wiring

Tests live in `sample/tests/` and use `unittest`. Run via `make -C sample test` (= `uv run python -m unittest discover sample/tests`). The **project-wide** test runner does NOT pick these up — don't move them into the top-level `tests/`.

`sample` is registered as a discovered package in the parent `pyproject.toml`, so `uv run python -m sample.seed` works without `sys.path` tricks. Single shared venv — sample is a sibling package, not a separate uv project.

## File-by-file purpose

One-liner per file; full prose in [`docs/analysis/seeder-internals.md`](docs/analysis/seeder-internals.md).

```
sample/
  Makefile                    # test + up/down/nuke/logs/status/api-key
  docker-compose.yml          # bitnamilegacy/discourse + postgresql + redis
  .env.example                # admin creds + LLM env-var template
  fixtures/seed42-tiny/       # committed pre-scraped snapshot for offline demos
  seed/
    __main__.py               # `python -m sample.seed` entry
    cli.py                    # argparse: init + extend
    pipeline.py               # build_forum, push_forum, extend_forum, push_extension
    discourse_api.py          # DiscourseClient — REST client for live mode
    forum.py                  # Forum, ForumExtension dataclasses (JSON shapes)
    blocklist.txt             # ~250-entry hand-curated real-name list
    rng.py                    # Rng wrapper around random.Random
    universe.py               # GenerationSpec + SCALE_PRESETS
    product/crown_of_brine.py # the fictional universe
    content/
      blocklist.py            # load_blocklist, check(text)
      llm.py                  # Provider Protocol + select_provider
      cache.py                # JSON Cache(path) keyed by (topic_id, post_index)
      bodies.py               # generate_body + BlocklistViolation
      cache/                  # committed per-(product, provider, seed, scale) caches
      providers/
        ollama.py             # OllamaProvider
        openai.py             # OpenAIProvider
    generators/
      categories.py           # generate_categories(spec) -> list[str]
      tags.py                 # generate_tags(spec) -> list[str]
      users.py                # generate_users(spec) -> list[User]
      timeline.py             # make_timeline(spec, total) -> Timeline
      topics.py               # generate_topics(...) -> list[Topic]
      posts.py                # generate_posts(...) -> list[Post]
  tests/                      # `make -C sample test`
  docs/
    index.md                  # entry router
    analysis/
      seeder-internals.md     # design + module prose + pipeline shapes
      live-push-walkthrough.md
      provenance-and-costs.md
```
