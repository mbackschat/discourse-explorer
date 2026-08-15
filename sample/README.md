# Sample Forum

A self-contained synthetic Discourse forum used for demos and regression testing of the parent project. One integer seed → a deterministic, fictional fan-community forum that the scraper, query, visualize, and stats tools work against unmodified.

Design, module map, pipeline shapes, and operational details live under [`docs/index.md`](docs/index.md).

## What's implemented

| Generator                            | Module                                  | Status     |
|--------------------------------------|-----------------------------------------|------------|
| Categories pool draw                 | `sample/seed/generators/categories.py`  | ✓ Sit 1    |
| Tags pool with cluster reservation   | `sample/seed/generators/tags.py`        | ✓ Sit 2    |
| Users with Pareto activity weights   | `sample/seed/generators/users.py`       | ✓ Sit 3    |
| Timeline with release-event bursts   | `sample/seed/generators/timeline.py`    | ✓ Sit 4    |
| Topic titles via templates           | `sample/seed/generators/topics.py`      | ✓ Sit 5    |
| Posts: reply trees                   | `sample/seed/generators/posts.py`       | ✓ Sit 6    |
| Blocklist + minimal CLI              | `sample/seed/cli.py` + `blocklist.txt`  | ✓ Sit 7    |
| Provider scaffold + Ollama provider  | `sample/seed/content/llm.py` + `providers/` | ✓ Sit 8 |
| OpenAI provider                      | `sample/seed/content/providers/openai.py` | ✓ Sit 9 |
| Body generator + cache + blocklist   | `sample/seed/content/bodies.py` + `cache.py` | ✓ Sit 10 |
| Wire bodies into posts               | `sample/seed/pipeline.py` + `cli.py`    | ✓ Sit 11   |
| Live Discourse stack (compose + Make) | `sample/docker-compose.yml` + `Makefile` | ✓ Sit 12 (Phase 3.1) |
| Discourse API client + admin-key bootstrap | `sample/seed/discourse_api.py` + `make api-key` | ✓ Sit 13 (Phase 3.2) |
| Live `init` pipeline                  | `sample/seed/pipeline.py::push_forum` + `cli.py` live mode | ✓ Sit 14 (Phase 3.3) |

Phase 2 — Content (complete): Sit 8 ✓ provider scaffold + Ollama; Sit 9 ✓ OpenAI provider; Sit 10 ✓ body generator + cache + blocklist post-filter; Sit 11 ✓ wired bodies into posts; OpenAI smoke test produces real bodies, cached.

LLM bodies are cached on disk under `sample/seed/content/cache/<product>-<provider>-seed<N>-<scale>.json`. The cache file for `(crown-of-brine, openai, seed=42, scale=tiny)` is committed to the repo, so anyone reproducing the seed can re-bake it offline without an OpenAI API key — the second run completes in well under a second from the cache.

The product universe — game-series titles, engine names, lore anchors, version/platform tags, release events — lives in `sample/seed/product/crown_of_brine.py`. All universe names are fabricated; hardware platforms (Amiga, Steam Deck, Windows, …) use their real names because that's how fan forums actually discuss running classic games.

## Run

`init --dry-run` runs every Phase-1/2 generator in dependency order (categories → tags → users → timeline → topics → posts) and writes the result to a single JSON file — no Docker required. Without `--no-llm` the seeder calls the LLM (OpenAI if `OPENAI_API_KEY` is set, otherwise Ollama via `OLLAMA_HOST`) for each post body, with a per-`(product, provider, seed, scale)` cache short-circuiting repeat runs.

```bash
# Real LLM bodies (uses OPENAI_API_KEY from .env if set; cache populated on first run):
uv run --env-file=.env python -m sample.seed init \
    --seed 42 --scale tiny --dry-run --output /tmp/sample-42.json

# Fully offline structural bake (placeholder bodies, no LLM, no network):
uv run python -m sample.seed init \
    --seed 42 --scale tiny --dry-run --no-llm --output /tmp/sample-42.json
```

The JSON has eight top-level keys:

- `seed`, `scale`, `product_name` — bake parameters echoed back so the file is self-describing.
- `categories` — list of category names (5–7 entries).
- `tags` — list of tag slugs (25–35 entries, all cluster combos reserved).
- `users` — list of `{username, display_name, role, activity_weight}` records.
- `topics` — list of `{id, title, category, tags, author_username, created_at}` records (datetimes ISO-8601).
- `posts` — list of `{id, topic_id, post_number, parent_post_id, author_username, body, created_at, is_accepted_solution, quote_target_id}` records.

Pretty-printed with `indent=2, sort_keys=True` for easy diffing across seeds.

The generator modules are also importable directly if you'd rather drive them from a REPL:

```python
from sample.seed.generators.categories import generate_categories
from sample.seed.generators.tags import generate_tags
from sample.seed.generators.users import generate_users
from sample.seed.generators.timeline import make_timeline
from sample.seed.generators.topics import generate_topics
from sample.seed.generators.posts import generate_posts
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec

spec = GenerationSpec(seed=42, scale="tiny", product=crown_of_brine)

cats = generate_categories(spec)               # 5-7 category names
tags = generate_tags(spec)                     # 25-35 tags including reserved cluster combos
users = generate_users(spec)                   # users[0]=admin, users[1]=mod, rest=regulars
tl = make_timeline(spec, total_topics=spec.scale_targets()["topics"])
topics = generate_topics(spec, cats, tags, users, tl)
print(generate_posts(spec, topics, users, tl)[:3])  # OP + first replies of topic 1
```

### `extend --add-topics N --add-replies M --release-burst V --mixed` (Sits 15-18)

`extend` re-bakes the base forum offline (deterministic from `--seed` + `--scale` + `--product`) and produces:

- `--add-topics N` (Sit 15): N new topics + their posts dated AFTER the base bake's last timestamp.
- `--add-replies M` (Sit 16): M replies appended to BASE topics, distributed weighted by each topic's existing post count, dated AFTER the base bake's last timestamp.
- `--release-burst <version>` (Sit 17): a self-contained cluster of 10-20 topics + 50-100 replies inside a 7-day window centred on a fictional release date 7-23 days past the base. Every burst topic is tagged with `<version>` (one of the product's `game-version` axis values, e.g. `remaster`, `game-1`, `game-2`, `game-3`) plus one of `[bug, feature-request, hint-needed]` weighted 60/25/15. Burst replies attach to burst topics only — distinct from `--add-replies`, which targets base topics.

All three flags compose — pass any combination (at least one must be active). Categories / users / tags are reused from the base — they are NOT re-emitted in the extension's JSON, since on a live forum they're presumed already-pushed by the original `init`. JSON shape is `ForumExtension`: `(base_seed, base_scale, base_product_name, extend_seed, add_topics_n, add_replies_n, release_burst_version, new_topics, new_posts)`. `new_posts` is heterogeneous: posts on extension topics, replies on burst topics, AND appended replies on base topics all share the same id namespace; callers distinguish via `topic_id ∈ {t.id for t in new_topics}`.

```bash
# Topics-only — 5 new topics on base seed=42
uv run python -m sample.seed extend \
    --seed 42 --scale tiny --extend-seed 7 \
    --add-topics 5 --no-llm \
    --dry-run --output /tmp/sample-ext-topics.json

# Replies-only — 10 new replies appended to base topics
uv run python -m sample.seed extend \
    --seed 42 --scale tiny --extend-seed 7 \
    --add-replies 10 --no-llm \
    --dry-run --output /tmp/sample-ext-replies.json

# Release burst — 10-20 topics + 50-100 replies tagged `remaster`
uv run python -m sample.seed extend \
    --seed 42 --scale tiny --extend-seed 7 \
    --release-burst remaster --no-llm \
    --dry-run --output /tmp/sample-ext-burst.json

# Mixed — 3 new topics + 7 appended replies + a release burst in one bake
uv run python -m sample.seed extend \
    --seed 42 --scale tiny --extend-seed 7 \
    --add-topics 3 --add-replies 7 --release-burst remaster --no-llm \
    --dry-run --output /tmp/sample-ext-mixed.json

# --mixed (Sit 18) — scale-derived defaults across all three modes.
# Tiny scale → 5 add-topics + 15 add-replies + a `remaster`-tagged burst.
uv run python -m sample.seed extend \
    --seed 42 --scale tiny --extend-seed 7 \
    --mixed --no-llm \
    --dry-run --output /tmp/sample-ext-auto.json
```

Same `(base seed, scale, product, extend-seed, add-topics, add-replies, release-burst)` -> bit-for-bit identical extension. Topic ids start at `max(base.topics.id) + 1`; post ids start at `max(base.posts.id) + 1`. Topic titles dedupe AGAINST base titles (`(2)`, `(3)`, … suffixes) so a future live push won't trip Discourse's `title_has_already_been_used` check. Appended replies attach to base topics ONLY; burst replies attach to burst topics ONLY (orthogonal mode semantics — extension topics vs. thread revival on existing topics vs. release-day cluster stay distinct).

**Live push for `extend` is deferred to a unified end-of-Phase-4 sit.** All four `extend` modes (Sits 15, 16, 17, 18) ship offline first; the live-push wiring lands once for the whole `extend` surface. The offline path errors out cleanly without `--dry-run`.

## Tests

Tests live under `sample/tests/` and run with sample-local discovery:

```bash
make -C sample test
# or directly:
uv run python -m unittest discover sample/tests
```

The project-wide test runner does **not** pick these up — that's intentional; the sample subtree has its own (lighter) test posture documented in [`CLAUDE.md`](CLAUDE.md).

## Phase 3 — live Discourse stack (in progress)

Brings up a local Discourse via Docker for end-to-end seeding (Sit 13+). Sit 12 ships the stack and Make targets; the API client + live `init` pipeline land in Sits 13-14.

`sample/.env` ships in the repo with deterministic dev defaults (admin /
`SampleAdminDev1234`, db / `SampleDbDev1234`, port 4200) so the stack just
works. The `!sample/.env` whitelist in the top-level `.gitignore` keeps it
tracked despite the global `.env` rule. Edit it locally if you want
non-default credentials — but don't commit your override.

```bash
# 1. Bring up the stack (first run pulls ~1.5 GB; subsequent runs are fast)
make -C sample up

# 2. Wait ~3-5 minutes for Discourse to bootstrap. Check progress:
make -C sample logs

# 3. Once "Started GET /" appears in the logs, open http://localhost:4200
#    and log in with the credentials from sample/.env

# 4. Extract an admin API key into sample/.env so the seeder can post to the forum.
#    Idempotent — no-op if DISCOURSE_API_KEY is already set.
make -C sample api-key

# 5. When done, stop the stack:
make -C sample down       # keeps data
make -C sample nuke       # full reset, deletes volumes
```

`make -C sample status` shows container state without tailing logs. The
DISCOURSE_API_KEY line that `make api-key` appends to `sample/.env` is per-stack
and changes on every nuke + bootstrap — leave it out of commits.

`make api-key` runs `bundle exec rails runner` inside the Discourse container to mint a fresh admin API key (`ApiKey.create!(user: <admin>)`) and writes it back to `sample/.env`. The key is a master-key-flagged admin key, which is what Sit 14's pipeline relies on for backdating posts via the `created_at` field. The Discourse API client itself (`sample/seed/discourse_api.py`) authenticates via `Api-Key` + `Api-Username` headers — the same scheme the parent project's scraper uses.

### Live `init` (Sit 14)

`init` without `--dry-run` runs every generator and POSTs the result into the live Discourse stack. Required env: `DISCOURSE_API_KEY`, `DISCOURSE_API_USERNAME`, and either `DISCOURSE_URL` (full base URL) or `DISCOURSE_HOST` + `DISCOURSE_PORT` (the CLI combines them when HOST has no scheme + no port — same `.env` the compose stack consumes). All populated by `make up` + `make api-key` against a freshly-bootstrapped stack; `DISCOURSE_API_USERNAME` defaults to `DISCOURSE_USERNAME` if unset.

```bash
# 7. Seed the running forum end-to-end (against a fresh stack).
#    Source the env first so the same vars used by docker-compose feed the seeder.
set -a && . sample/.env && set +a
export DISCOURSE_API_USERNAME="${DISCOURSE_API_USERNAME:-${DISCOURSE_USERNAME:-admin}}"
uv run python -m sample.seed init --seed 42 --scale tiny --no-llm

# 8. Verify what landed
curl -s -H "Api-Key: $DISCOURSE_API_KEY" -H "Api-Username: $DISCOURSE_API_USERNAME" \
    http://localhost:4200/categories.json | jq '.category_list.categories | length'
curl -s -H "Api-Key: $DISCOURSE_API_KEY" -H "Api-Username: $DISCOURSE_API_USERNAME" \
    http://localhost:4200/latest.json | jq '.topic_list.topics | length'
# Expected (tiny): 6 seeded categories (plus 2 Discourse defaults), ~30 seeded topics, ~120-150 posts
```

**Timing.** Cold-boot Discourse takes ~2–3 min after `make up` (volumes wiped). The push itself takes **10–20 min for `tiny` scale** (~150 calls): the per-request 0.4s pacing in `pipeline.py` is small compared with Discourse's per-post Sidekiq job overhead, and the per-user `rate_limit_create_post` setting throttles heavy posters even after the seeder lowers it. See [`docs/analysis/live-push-walkthrough.md`](docs/analysis/live-push-walkthrough.md) for the full operational story.

**Browser review.** While the stack is up, open `http://localhost:4200` in a browser:

- **Admin** — username `admin` (the `DISCOURSE_USERNAME` from `sample/.env`), password = `DISCOURSE_PASSWORD`. Full admin UI; visit `/admin/dashboard`, `/admin/users`, `/admin/site_settings`.
- **Seeded user** — username from the push (e.g., `jolly_helmsman` — list at `/u`), password = `sample-seeder-password-1234` (the shared fixture in `pipeline.py::_FIXTURE_PASSWORD`). Useful for seeing the forum from a regular member's perspective.
- **Anonymous** — `/categories`, `/latest`, individual topics, `/u` are all readable without login.

`init` walks **categories → users → topics+OPs → replies** in chronological order via `pipeline.push_forum`. Tags auto-create when first attached to a topic, so there's no explicit tag pass. Every seeded user is **promoted to moderator** immediately after creation (Sit 14.1 — staff users bypass `RateLimiter#rate_unlimited?`, which sidesteps Discourse's full per-user rate-limit surface in one move; cosmetic cost is a moderator badge on every seeded user). The seeded `User.role` field still records admin / moderator / regular as design intent — the activity-weight Pareto distribution carries the "heavy posters tend to be staff" signal through authorship counts regardless of the in-Discourse role. User passwords are the shared fixture `sample-seeder-password-1234` and emails are derived as `<username>@sample.local`. The bitnami admin is NOT pushed (it's the API caller; seeded usernames come from the nautical-pair pool and won't collide).

Two seeder-side fields don't translate for Sit 14: `is_accepted_solution` (requires the `discourse-solved` plugin — not installed by default on bitnami) and `quote_target_id` (would require post-body BBCode rewriting). Both are preserved in the `--dry-run` JSON for fixture replay; neither blocks the scraper from reading the forum back.

`init` (live mode) and `--dry-run` are mutually exclusive — re-run with `--dry-run --output ...` to capture the bake JSON separately. Live mode does not write a JSON record on its own.

Re-running live `init` against a forum that already has the same seed's data fails fast: the FIRST collision is on `Category name has already been taken` (categories are pushed before topics). Seeded names are deterministic per seed, so any partial leftover wins the race. The clean reset is `make -C sample nuke` followed by `make up && make api-key`. Don't try to "patch" via individual API deletes — partial state is the bug.
