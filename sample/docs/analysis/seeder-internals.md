# Seeder internals

How the synthetic-forum seeder is built: the product universe, the determinism contract, the injected dynamics, the module map, and the four pipeline shapes (offline build, live push, offline extend, live extend push).

Read this before changing anything in `sample/seed/`. Operational concerns (cold-boot timing, rate limits, recovery) live in [`live-push-walkthrough.md`](live-push-walkthrough.md); cache + cost concerns live in [`provenance-and-costs.md`](provenance-and-costs.md).

## Goal

One command, deterministic output:

```bash
make -C sample up
uv run --env-file=sample/.env python -m sample.seed init --seed 42 --scale tiny
uv run --env-file=sample/.env discourse-explorer scrape http://localhost:4200 --output ~/data/sample-42 --full
uv run --env-file=sample/.env python -m sample.seed extend --mixed --extend-seed 7
uv run --env-file=sample/.env discourse-explorer scrape http://localhost:4200 --output ~/data/sample-42 --full
```

Same `(seed, scale, product)` tuple → bit-for-bit identical forum across machines.

## Why a synthetic forum

A public Discourse instance demos the scraper, but content drifts (demos and tests aren't reproducible), scale isn't tunable (no "exactly 30 topics" smoke test), forum dynamics aren't controllable (no guarantee of pain-point clusters or accepted-answer markers), and rate-limit / robots policies change without warning. A synthetic forum solves all four.

## Default product universe — Crown of Brine

A four-game comedic pirate-themed point-and-click adventure series by the in-fiction studio "Brackwater Interactive". A fan-community forum gives entity-type discovery a different vocabulary (lore, characters, puzzles, voice acting, modding) than a tech-product bug-tracker, and gives the visualizer character-arc subgraphs to draw.

| Axis                  | Values                                                                                              |
|-----------------------|-----------------------------------------------------------------------------------------------------|
| Titles                | *Crown of Brine* (1992), *Crown of Brine II: The Phantom Galleon* (1993), *Crown of Brine III: Tides of Forgetting* (2002), *Crown of Brine: Reborn* (2024 remaster) |
| Engines               | JESTER (classic-era), TIDE (remaster)                                                               |
| Mod tools             | Compass Editor, Doubloon SDK                                                                        |
| Storefronts           | GroggyDeck, TidalKey, PortShelf                                                                     |
| Platforms (vintage)   | Amiga, Atari ST, Commodore 64, DOS PC, classic Macintosh, FM Towns                                  |
| Platforms (modern)    | Windows, Linux                                                                                      |
| Platforms (handheld)  | Steam Deck, Nintendo Switch                                                                         |
| Platforms (mobile)    | iOS, Android                                                                                        |
| Categories *(seeded)* | 5–7 drawn per seed from a pool of ~10                                                               |
| Tags *(seeded)*       | 25–35 drawn per seed from a pool of ~60                                                             |

The fictional studio, game titles, in-fiction engines (JESTER, TIDE), characters, storefronts, and mod tools are all fabricated. Hardware and OS platforms use their real names — that's how fan forums actually discuss running classic games. No real persons, game studios, game franchises, game engines, or game characters in any generated content.

The universe abstraction is designed for swap-in (`product/` is a directory with one file today), but the second product is deferred until concrete demand.

### Category pool

Three are *core* and always drawn: **Announcements**, **Help & Hints**, **Bug Reports**. The seeder then picks 2–4 more from: Modding & Fan Projects, Lore & Theories, Show & Tell, Off-Topic Tavern, Speedruns & Challenges, Translations & Localization, Voice Cast Talk. Different seeds → visibly different forum shape, but every forum has the basic Q&A skeleton.

### Tag pool

Drawn per seed across these axes (each contributes some core tags + some optional):

- **status** — spoiler, hint-needed, solved, won't-fix, duplicate, abandoned
- **game-version** — game-1, game-2, game-3, remaster
- **type** — bug, feature-request, lore, fan-art, fan-fiction, modded, theory, walkthrough, speedrun
- **subject** — voice-acting, localization, music, art-style, puzzle-design, dialogue, characters, locations
- **platform** — amiga, atari-st, c64, dos, classic-mac, fm-towns, windows, linux, steam-deck, switch, ios, android
- **engine** — jester-engine, tide-engine
- **mod-tool** — compass-editor, doubloon-sdk

Pain-point clusters force certain tag *combinations* (e.g. `bug + tide-engine + remaster + game-2`) so cluster signals survive the random subset draw — generators reserve those tags before the random sampler runs.

## Scale presets

| Preset   | Users | Topics | Posts | LLM cost (one-time) |
|----------|-------|--------|-------|---------------------|
| `tiny`   | 10    | 30     | ~120  | ~$0.10              |
| `small`  | 30    | 150    | ~700  | ~$0.50              |
| `medium` | 80    | 500    | ~2.5k | ~$2                 |
| `large`  | 200   | 2k     | ~12k  | ~$8                 |

Costs hit once per `(seed, scale, product, provider)` — cache covers re-runs.

## Determinism

- A single integer seed drives everything. `GenerationSpec.rng(*salt)` returns a derived `Rng` per component (`spec.rng("categories")`, `spec.rng("tags")`, `spec.rng("users")`, `spec.rng("timeline")`, …) so adding or re-ordering a generator doesn't shift unrelated outputs.
- No module-level `random.choice` (or equivalent) anywhere in the subtree. Every consumer takes an `Rng` explicitly.
- Iteration over dicts/sets is sorted before consumption.
- Timestamps: epoch base = today − 12 months; all `created_at` values are `base + deterministic_offset`. Repeating the seed N months later produces the same forum *shape*; absolute dates shift.
- Post bodies cached at `content/cache/<product>-<provider>-seed<N>-<scale>.json` keyed by `(topic_id, post_index)`. Cache miss → call LLM → write back. Cache files are committed; the same seed on a different machine reads from cache, no LLM bill. Provider is part of the cache key because OpenAI vs Ollama produce visibly different bodies.

## Injected forum dynamics

Random uniform output makes for boring graphs and uninformative reports. The seeder injects structure tuned to a fan-community forum:

- **Pain-point clusters** — 3-5 recurring complaint themes, each spread across 8-15 topics with overlapping tags. Drives `/forum-report` pain-points audit.
- **Pareto activity** — 80% of posts from 20% of users; a small core of long-tenured "harbor regulars" plus newcomers drawn in by the remaster. Gives `stats users` a realistic shape.
- **Solved markers** — ~15% of Help & Hints topics flagged solved.
- **Unanswered backlog** — ~10% of topics with no replies (lore questions, obscure mod issues).
- **Cross-references** — ~20% of replies quote/link another topic. Gives the visualizer's reply graph density.
- **Release bursts** — topic-creation spikes around remaster launch and anniversary dates of games I/II/III.

## Module map

```
sample/
  Makefile                        # test + up/down/nuke/logs/status for the Docker stack
  docker-compose.yml              # bitnami/discourse + postgresql + redis stack
  .env.example                    # admin creds + LLM env-var template
  README.md                       # user-facing overview + run examples
  CLAUDE.md                       # maintainer scope + nav into sample/docs/
  fixtures/seed42-tiny/           # committed pre-scraped snapshot for offline demos (588 KB)
  seed/
    __main__.py                   # `python -m sample.seed` entry point
    cli.py                        # argparse: `init` and `extend` subcommands
    pipeline.py                   # build_forum + push_forum + extend_forum + push_extension
    discourse_api.py              # DiscourseClient — REST client for live mode
    forum.py                      # Forum + ForumExtension dataclasses (JSON dump shapes)
    blocklist.txt                 # ~250-entry hand-curated real-name list
    rng.py                        # Rng wrapper around random.Random
    universe.py                   # GenerationSpec dataclass + SCALE_PRESETS
    product/
      crown_of_brine.py           # the fictional universe — see "Default product universe"
    content/
      blocklist.py                # load_blocklist() + check(text) -> hits
      llm.py                      # Provider Protocol + select_provider()
      cache.py                    # JSON Cache(path) keyed by (topic_id, post_index)
      bodies.py                   # generate_body(...) + BlocklistViolation
      cache/                      # committed per-(product, provider, seed, scale) JSON caches
      providers/
        ollama.py                 # OllamaProvider (POST /api/generate)
        openai.py                 # OpenAIProvider (chat.completions.create)
    generators/
      categories.py               # generate_categories(spec) -> list[str]
      tags.py                     # generate_tags(spec) -> list[str]
      users.py                    # generate_users(spec) -> list[User]
      timeline.py                 # make_timeline(spec, total) -> Timeline
      topics.py                   # generate_topics(...) -> list[Topic]
      posts.py                    # generate_posts(...) -> list[Post]
  tests/                          # unittest discovery via `make test`
  docs/
    index.md                      # entry router; read first
    analysis/
      seeder-internals.md         # this file
      live-push-walkthrough.md    # operational runbook for live mode
      provenance-and-costs.md     # cache + cost rationale
```

### CLI surface — `seed/cli.py`

`uv run python -m sample.seed init --seed N --scale {tiny,small,medium,large} --product crown-of-brine` runs every generator in dependency order via `pipeline.build_forum`, bundles the result into a `Forum` (`seed/forum.py`), and either dumps JSON (`--dry-run --output PATH`) or POSTs the bake to a running Discourse instance via `pipeline.push_forum`. Live and `--dry-run` are mutually exclusive.

`uv run python -m sample.seed extend --seed BASE --scale SCALE --extend-seed K [--add-topics N] [--add-replies M] [--release-burst VERSION] [--mixed]` re-bakes the base forum offline to discover its categories / users / tags, then produces:

- **`--add-topics N`** — N net-new topics + their posts, dated AFTER the base bake's last timestamp.
- **`--add-replies M`** — M replies appended to BASE topics, distributed weighted by each topic's existing post count.
- **`--release-burst VERSION`** — a self-contained 10-20 topic + 50-100 reply cluster within a 7-day window centred on a fictional release date 7-23 days past `base_end_ts`. Every burst topic carries the `<version>` tag (one of the `game-version` axis values) plus one of `[bug, feature-request, hint-needed]` weighted 60/25/15. Categories biased toward Bug Reports / Help & Hints / Announcements.
- **`--mixed`** — convenience: populates `--add-topics` / `--add-replies` from `_MIXED_SCALE_DEFAULTS[scale]` (tiny=5/15, small=15/50, medium=30/100, large=50/200) and `--release-burst` from the last `game-version` axis entry. Explicit flags override.

At least one of `--add-topics` / `--add-replies` / `--release-burst` must be active. Modes compose. JSON dump shape is `ForumExtension`: `(base_seed, base_scale, base_product_name, extend_seed, add_topics_n, add_replies_n, release_burst_version, new_topics, new_posts)`. `new_posts` is heterogeneous — posts on extension topics, replies on burst topics, and appended replies on base topics share the same id namespace; callers distinguish via `topic_id ∈ {t.id for t in new_topics}`.

`--no-llm` skips body generation entirely; every post gets a deterministic placeholder body. Without it, the CLI builds a `(provider, cache)` pair via `select_provider()` + `Cache(path)`, wraps them in a `BodyProvider` closure, and forwards it to `pipeline.build_forum` / `pipeline.extend_forum`.

### Pipeline shapes — `seed/pipeline.py`

`build_forum(spec, *, body_provider=None, product_name=None) -> Forum` is the single generator-orchestration spine. Generator dependency order is fixed and load-bearing for determinism: `categories → tags → users → timeline → topics → posts`. Each generator asks `spec.rng(<salt>)` for its own derived stream, so order matters only for what each generator gets to consume — not for the randomness used inside it. Same `(seed, scale, product)` plus same `body_provider` → bit-for-bit identical `Forum`.

`push_forum(forum, client) -> PushResult` POSTs the `Forum` to a running Discourse via the `DiscourseClient`. Push order: **categories → users → topics-with-OPs → replies**, with topics walked in chronological order (`(created_at, id)`). Tags are auto-created on first attachment, so no explicit tag-creation pass. Within a topic, the seeder's `post_number` equals Discourse's `post_number` (Discourse assigns post numbers in arrival order; we author replies in seeder-generation order), so reply parents map by `parent.post_number` directly — `reply_to_post_number` is set only when the parent isn't the OP.

Every seeded user is `grant_moderator`'d immediately after `create_user`. Staff status routes every per-user rate-limit check through `RateLimiter#rate_unlimited?` → `true`, removing the rate-limit surface from the seeder. Activity-weight Pareto still drives WHO authors what — the seeded `User.role` field records admin/mod/regular as design intent. The bitnami admin (the API caller) is NOT in `forum.users`; the nautical adjective+noun pool wouldn't realistically collide with the bitnami default.

`extend_forum(base_spec, *, add_topics_n=0, add_replies_n=0, release_burst_version=None, extend_seed, body_provider=None, base_product_name=None) -> ForumExtension` is the offline extension path. Re-bakes the base via `build_forum`, derives `base_end_ts = max(base.posts.created_at)`, then dispatches to the active modes:

- Topic extension (`add_topics_n>0`): builds a `Timeline` with `base_epoch=base_end_ts` and uniform `[1, 30]`-day offsets, then calls `generate_topics(count=N, id_offset=max(base.topics.id), seen_titles={base titles}, skip_cluster_anchors=True)` and `generate_posts(id_offset=max(base.posts.id), skip_cluster_anchors=True)`.
- Reply extension (`add_replies_n>0`): `_generate_appended_replies` picks base topics weighted by existing post count, picks authors via `User.activity_weight`, advances per-topic clocks initialised to `max(topic.last_post_ts, base_end_ts)`, mirrors `generate_posts`'s 80%/20% OP/non-OP parent split.
- Release burst (`release_burst_version` set): `_generate_release_burst` anchors a fictional release date 7-23 days past `base_end_ts`, draws 10-20 topics + 50-100 replies all dated within a 7-day window centred on it. Replies attach to burst topics ONLY (self-contained — distinct from `add_replies_n` which targets base topics). All other game-version tags are excluded from burst topics' tag set so the version signal stays unambiguous.

Determinism: same `(base_spec, add_topics_n, add_replies_n, release_burst_version, extend_seed, body_provider)` → bit-for-bit identical `ForumExtension`. Reply density on extension topics inherits `base_spec.scale`; burst counts are extend-seed-derived in fixed `[10,20]` / `[50,100]` ranges.

`push_extension(extension, base_forum, client) -> PushResult` POSTs an extension to a running Discourse. Resolves base categories by name via `client.list_categories()` (one GET), maps base topic titles → live topic ids via `client.list_topics_in_category(id, slug)` per base category (paginated), then POSTs new topics + their replies + appended replies on base topics. Reuses base users/categories — no `create_*` for those. Live append-reply `parent_post_id` is dropped: Discourse renders replies with no `reply_to_post_number` as reply-to-OP, which preserves the 80%-case parent-as-OP semantics; resolving live `post_number` for base posts would require an extra GET per topic.

**Backdated-timestamp interaction with the scraper:** extension topics carry `created_at` 1-30 days after `base_end_ts` (seeder convention). `/latest.json` sorts by `bumped_at`, so the scraper's delta path doesn't see them — `--full` is required to pick them up. Structural seeder/scraper interaction, not a push bug.

### `skip_cluster_anchors` kwarg

`generate_topics` and `generate_posts` accept this kwarg. `True` skips the "first N topics are cluster anchors" reservation pattern (and the matching reply-floor enforcement in posts). Used exclusively by the extension path — cluster anchors are a "first N topics of the forum" reservation; extension topics aren't first by definition. Side benefit: `--add-topics N` accepts `N < len(CLUSTER_TAG_COMBINATIONS)` cleanly.

### LLM providers — `seed/content/llm.py` + `seed/content/providers/`

`select_provider()` mirrors `discourse_explorer/config.py`: if `OPENAI_API_KEY` is set and non-empty, the seeder routes to `OpenAIProvider` with `EXTRACTION_MODEL` (default `gpt-4.1-mini`); otherwise it falls back to `OllamaProvider` configured from `OLLAMA_HOST` (default `http://localhost:11434`) and `EXTRACTION_MODEL` (default `qwen2.5:14b`). Provider modules are import-on-demand. Integration tests for both providers are gated behind `SAMPLE_LLM_INTEGRATION` and (for OpenAI) a real `OPENAI_API_KEY` — the default `make -C sample test` run skips both.

### Body generator + cache — `seed/content/bodies.py` + `seed/content/cache.py`

`generate_body(topic, post, rng, llm, cache, blocklist=..., parent_body=None)` is the single seam between the structural generators and the LLM. Cache hit on `(topic.id, post.post_number)` short-circuits the provider; cache miss builds a deterministic prompt (function of topic title, category, sorted-lowercased tags, OP-vs-reply role, optional parent excerpt truncated to 600 chars) and calls `llm.generate`. Empty-string or raised-exception responses fall back to a deterministic template body and are NOT cached (so a future run with a working provider repopulates the slot). A non-empty response is run through `blocklist.check`; hits trigger up to 3 retries with an explicit avoidance note naming the offending term, then raise `BlocklistViolation` on the 4th still-banned attempt. `Cache(path)` is JSON write-through (atomic via tmp + `os.replace`), keyed by `"{topic_id}:{post_index}"`. Cache filename convention is `<product>-<provider>-seed<N>-<scale>.json`. Extension runs use `<product>-<provider>-extend-<base-seed>-<extend-seed>-<scale>.json` so init and extend caches don't share `(topic_id, post_number)` keys.

### Blocklist — `seed/blocklist.txt` + `seed/content/blocklist.py`

Hand-curated list of real game franchises, studios, engines, and iconic characters that must NOT appear in any generated artefact. Hardware/OS platforms are explicitly NOT in the list — `Nintendo Switch` is allowed as a platform display, so `Nintendo` (the studio) is intentionally absent and its franchises (Mario, Zelda, …) are listed individually instead. `load_blocklist()` is `lru_cache`d; `check(text)` runs one combined word-boundary regex over the input and returns the sorted lowercased hits.

### Product surface — `product/crown_of_brine.py`

The universe constants:

- `CORE_CATEGORIES`, `CATEGORY_POOL` — driven by `generate_categories`.
- `TAG_POOL_BY_AXIS`, `CORE_TAGS_BY_AXIS`, `CLUSTER_TAG_COMBINATIONS` — driven by `generate_tags`.
- `USERNAME_PARTS` — `{"adjectives": [...], "nouns": [...]}` — driven by `generate_users`.
- `RELEASE_EVENTS` — `list[(offset_days_from_base, label)]`, all events in `[0, 365]`, ≥3 distinct days — driven by `make_timeline`.
- `GAME_TITLES`, `MOD_TOOLS`, `PLATFORM_DISPLAY_NAMES`, `LORE_VOCAB`, `TITLE_TEMPLATES_BY_CATEGORY`, `TAG_AFFINITY_BY_CATEGORY` — driven by `generate_topics`. `LORE_VOCAB` is a dict of slot-fill pools (puzzles / chapters / locations / character_archetypes / items / verbs / asset_types). `TITLE_TEMPLATES_BY_CATEGORY` keys EVERY entry of `CATEGORY_POOL` with ≥4 template strings. `TAG_AFFINITY_BY_CATEGORY` values are tag-axis names (keys of `TAG_POOL_BY_AXIS`), not literal tags.

The entire universe stays in one file so a swap to a new product (`hexenwald_saga.py`, …) is one drop-in.

### Live Discourse stack — `docker-compose.yml` + Makefile

Image: **`bitnamilegacy/discourse`** (Bitnami catalog frozen post-Aug-2025; documented stopgap). Auto-creates the admin user from `DISCOURSE_USERNAME` / `DISCOURSE_PASSWORD` / `DISCOURSE_EMAIL` at first boot, so `make up` reaches a logged-in-able instance with no interactive setup. The official `discourse/discourse_docker` image requires manual `discourse-setup` and isn't suitable for a one-shot dev stack.

Stack composition: `discourse` (port `${DISCOURSE_PORT:-4200}` on the host, `3000` inside per bitnami's default), `postgresql`, `redis` (`ALLOW_EMPTY_PASSWORD=yes` for dev only). Volumes are named (`postgresql_data`, `redis_data`, `discourse_data`) so `make down` keeps them and `make nuke` (`down -v`) wipes them. `down` is safe-by-default; `nuke` is the explicit full-reset escape hatch.

The Makefile assumes invocation as `make -C sample <target>`. A `check-env` guard on every Docker target fails loudly with a copy-and-paste fix message if `sample/.env` is missing.

### API key bootstrap + REST client — `make api-key` + `seed/discourse_api.py`

`make api-key` is a one-shot extract-and-write target. It runs `bundle exec rails runner` inside the discourse container to call `ApiKey.create!(user: <admin>)`, captures the printed key, and splices it back into `sample/.env` via a portable `sed -i.bak ... && rm -f .env.bak` form (works on both BSD/macOS sed and GNU sed). Idempotent: if `DISCOURSE_API_KEY=…` is already non-empty in `.env` it's a no-op. Run AFTER `make up` reports "Started GET /" — the admin user must exist before the rails query can find it. The minted key is a **master** key — needed for `created_at` backdating + per-IP rate-limit bypass + `Api-Username` impersonation.

`DiscourseClient(base_url, api_key, api_username, *, timeout=30.0)` mirrors the parent project's `discourse_explorer/scraper.py` conventions: per-request `Api-Key` + `Api-Username` headers; a shared `requests.Session` mounted with a `urllib3.Retry` adapter (3 retries, backoff factor 0.5, retry on 429 + 500/502/503/504, POST in `allowed_methods`, `respect_retry_after_header=False`, `backoff_max=15`). The disable-`Retry-After` knob is critical: Discourse's `Rack::Attack` middleware returns 429s with multi-thousand-second `Retry-After` hints that would otherwise deadlock the push via `time.sleep()` in the retry loop. `_request` adds a bounded outer 429-retry loop (`_RATE_LIMIT_BACKOFF=30s` × `_RATE_LIMIT_MAX_RETRIES=3` rounds) for transient bursts past the urllib3 budget.

Public methods cover both the create-side (`create_category`, `create_tag` — a no-op since Discourse auto-creates tags on attachment, kept for pipeline parity, `create_user`, `create_topic`, `create_post`) and the read-side helpers `push_extension` needs (`list_categories`, `list_topics_in_category`). `created_at` is rendered via `_to_iso` as a UTC ISO-8601 string with trailing `Z`; `author_username` swaps the `Api-Username` header per-call so a topic / reply can be authored as the impersonated user. `set_site_setting(name, value)` is used pre-push to raise `max_topics_in_first_day` / `max_topics_per_day` / `max_replies_in_first_day` / `unique_posts_mins` as defense-in-depth alongside the staff-bypass. `grant_moderator(user_id)` calls `PUT /admin/users/:id/grant_moderation.json`. Errors raise `DiscourseAPIError(url, status, body)` so Discourse's validation messages survive the boundary.

Tests in `tests/test_discourse_api.py`: unit tests mock `Session.request` and verify header injection, payload shape, error mapping, and the retry-adapter config. Integration tests are double-gated behind `SAMPLE_DISCOURSE_INTEGRATION=1` AND `DISCOURSE_HOST` — opting in round-trips category / user / backdated topic+tags / impersonated reply against the live stack. Per-run epoch suffixes on names avoid collisions across reruns; `make -C sample nuke` is the documented teardown escape hatch.

### Email + password derivation

`email = f"{username}@sample.local"` (RFC-6762-reserved domain — won't accept real mail, fine for a localhost demo). `password = "sample-seeder-password-1234"` (single shared placeholder, satisfies Discourse's default `min_password_length=10`). Both are documented inline in `pipeline.py`.

### Topic-title dedup

The slot-fill template vocabulary is finite, so at small scales seeded titles can collide. Discourse rejects duplicate titles by default (`title_has_already_been_used`). `_dedupe_titles` appends `(2)`, `(3)`, … in chronological-id order to later occurrences. Deterministic by construction (single pass, no rng draws). `extend` passes `seen_titles={base titles}` so extension titles never collide with base titles either.

### Deliberately not pushed

`Post.is_accepted_solution` and `Post.quote_target_id` don't translate to Discourse. The accepted-solution marker requires the `discourse-solved` plugin (not installed on bitnami); quote rewriting requires post-body BBCode rewriting (`[quote="user, post:N, topic:M"]`) that re-introduces LLM-generated content into the cache-invalidation surface. The seeder JSON keeps both fields so `--dry-run` artifacts and fixture replay still see the cross-references.

### Env-var resolution

The live CLI accepts either `DISCOURSE_URL` (full base URL — preferred when set) or `DISCOURSE_HOST` + `DISCOURSE_PORT`. When `DISCOURSE_HOST` has no scheme and no `:port`, the CLI combines it with `DISCOURSE_PORT` (`http://{host}:{port}`). This lets the seeder consume the same `sample/.env` the docker-compose stack uses without duplication. `DISCOURSE_API_USERNAME` is a separate var; if absent, the README walkthrough falls back to `DISCOURSE_USERNAME`.

## Why Docker (not synthesizing on-disk JSON directly)

The scraper is a headline feature; bypassing it would hide it from the demo. Docker also gives:

- A real Discourse API surface — anything the scraper depends on (pagination shapes, `cooked` vs `raw`, post_number quirks) gets exercised against the actual implementation, not our model of it.
- A reusable artifact — once seeded, the same instance can be poked manually in the browser, used to test new auth modes, or reseeded with a different product.

A synthetic-JSON path (bypass Docker, write the data dir directly) is left as a future option — second code path to maintain, deferred until concrete demand.
