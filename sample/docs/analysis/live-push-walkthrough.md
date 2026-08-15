# Live push walkthrough

Operational reference for running `python -m sample.seed init` against a real Discourse stack — the cold-boot story, what `make api-key` actually does, what timing to expect, why the seeder tunes a handful of site settings, and how to recover when a partial push leaves the forum in a half-seeded state. Pairs with the live-mode section in `sample/README.md`; the README documents the happy path, this file documents the operator-facing reality.

## What `make up` actually does on a cold boot

`docker compose up -d` starts three containers — `bitnamilegacy/discourse`, `bitnami/postgresql`, `bitnami/redis`. The discourse container's first-boot sequence runs DB migrations, asset compilation, the bitnami auto-create-admin step (driven by `DISCOURSE_USERNAME` / `DISCOURSE_PASSWORD` / `DISCOURSE_EMAIL` from `sample/.env`), and Sidekiq warm-up. On an Apple-Silicon Mac with the legacy image already pulled, the wall-clock sequence is roughly:

| Phase                      | Duration   | Visible signal                              |
|----------------------------|------------|---------------------------------------------|
| Image start                | ~5 s       | `docker compose ps` shows containers        |
| DB migrations + init       | ~60–90 s   | postgres logs settle                        |
| Asset compile + admin seed | ~60–90 s   | `/srv/status` returns 200                   |
| Sidekiq warm-up            | ~10–30 s   | first POST request succeeds without retry   |

Total: **~2–3 min** for a clean cold boot when the volumes were just wiped (`make nuke`). A warm restart (`make down` → `make up` with volumes intact) is **~10–30 s**.

`make up` returns as soon as Docker has started the containers — not when Discourse is ready. Poll `/srv/status` to wait for readiness:

```bash
until curl -sf -o /dev/null http://localhost:4200/srv/status; do sleep 2; done
```

A 60-poll × 1-sec budget is fine for warm restarts; cold boot needs the loop above (no fixed budget).

## What `make api-key` actually does

The target runs `bin/rake "api_key:create_master[sample seeder]"` inside the discourse container, which mints a **master** API key (the rake task name says it all). Master keys carry three properties the seeder relies on:

1. **Bypass per-IP rate limits.** Rack-middleware throttling on user-creation, topic-creation, etc. is the classic friction-point for any bulk import; master keys skip it.
2. **Backdate `created_at` on POST `/posts.json`.** Regular admin keys can read backdated timestamps via the API but can't *set* them on creation; master keys can. The seeder's chronological timeline is meaningless without this.
3. **Impersonate any user via `Api-Username` per request.** `create_topic` and `create_post` rely on this to attribute posts to the seeded users instead of the bitnami admin.

The Makefile target is **idempotent**: it grep's `sample/.env` for a non-empty `DISCOURSE_API_KEY=...` and exits with "skipping" if one is already present. This is so re-running `make api-key` after a stack-reuse doesn't churn the .env. After `make nuke`, you must clear `DISCOURSE_API_KEY=` manually before running `make api-key`, or the old key (now orphaned in the wiped DB) is reused and every subsequent API call fails 403:

```bash
sed -i.bak 's|^DISCOURSE_API_KEY=.*|DISCOURSE_API_KEY=|' sample/.env && rm -f sample/.env.bak
make -C sample api-key
```

If the `nuke` was followed quickly by `up`, the rake call may race the DB-bootstrap and emit a less-helpful Ruby trace. Re-run the target after the `/srv/status` loop completes.

## Sit 14.1 — diagnosing and fixing the mid-push stall

The Sit 14 verification surfaced a hang that didn't match its initial appearance. This section is the post-mortem so future maintainers can recognise the same shape and skip straight to the fix instead of re-walking the whole investigation.

### What it looked like (wrong hypothesis)

The push reliably stalled mid-way through topics + replies (around 20 topics / 42 posts at `tiny` scale). Symptoms on the python side:

- The process kept running for tens of minutes with **near-zero CPU** (~0.3 sec total in 13+ min wall-clock).
- `lsof -p <pid>` showed **one TCP connection in `CLOSE_WAIT`** to `localhost:4200` — Discourse had FIN'd, our process hadn't read EOF.
- The configured `DiscourseClient(timeout=30.0)` did NOT release the hang.

The first hypothesis: half-closed keep-alive socket + `urllib3.Retry` interaction. The fix attempts (in this order) were `timeout=(5.0, 30.0)` tuple form for stricter per-attempt enforcement, and `Session.headers["Connection"] = "close"` to disable keep-alive entirely. **Both were applied; the hang reproduced at the same exact point.** That was the signal the keep-alive theory was wrong.

### What it actually was

Inspecting Discourse's container logs (`docker logs sample-discourse-1 --tail 30`) revealed the real shape:

```
Started POST "/posts.json" for 192.168.65.1 at 2026-04-26 18:25:38 +0000
...
Completed 200 OK in 129ms

Started POST "/posts.json" for 192.168.65.1 at 2026-04-26 18:26:09 +0000
...
Completed 429 Too Many Requests in 19ms

Started POST "/posts.json" for 192.168.65.1 at 2026-04-26 18:26:09 +0000
...
Completed 429 Too Many Requests in 73ms
```

Discourse's **`Rack::Attack` per-IP throttle middleware** was returning 429 with a `Retry-After` header set to a very large value (the "you've blown the per-IP burst limit, come back in an hour" hint). Crucially, **`urllib3.Retry` honours `Retry-After` by default** — and `time.sleep(<huge_value>)` happens *inside* the retry loop, with no network activity, no CPU, and nothing for the per-attempt timeout to fire on. The CLOSE_WAIT socket from `lsof` was a *stale prior connection*, not the cause: the hang was a pure sleep deadlock inside urllib3.

This also invalidated the earlier "10-20 min push reality, dominated by Sidekiq job processing" claim. The "1 post per minute" observed throughput was actually 4 successful POSTs in 3 sec followed by 30+ minutes of urllib3 sleeping on a 429 hint. Sidekiq isn't slow; the path was just deadlocked.

### Why master keys don't bypass this

`make api-key` mints a *master* API key (rake task `api_key:create_master[...]`), which bypasses **per-user** rate limits — `rate_limit_create_post`, `rate_limit_new_user_create_post`, etc. — and is required for the seeder's `created_at` backdating. But `Rack::Attack` is an **per-IP HTTP middleware** that runs ahead of Discourse's own auth-aware throttles, so admin / master status doesn't help. Anything talking to Discourse from `192.168.65.1` (the docker-host bridge) shares one IP-bucket with everything else on that host, including the `make api-key` rake task and the operator's browser sessions.

### The fix — three layers

**Layer 1: stop urllib3 from honouring Discourse's `Retry-After`** (the deadlock cause). In `_build_session`:

```python
retry = Retry(
    total=_RETRY_TOTAL,
    backoff_factor=_RETRY_BACKOFF,
    status_forcelist=_RETRY_STATUS_FORCELIST,
    allowed_methods=_RETRY_METHODS,
    raise_on_status=False,
    respect_retry_after_header=False,   # <-- key change for the deadlock
    backoff_max=15,
)
```

`respect_retry_after_header=False` makes urllib3 fall back to its own short exponential backoff (0.5s → 1s → 2s, capped at 15s). Persistent 429s now exhaust retries within seconds and surface as a real exception we can act on, instead of `time.sleep()`-ing for an hour.

**Layer 2: handle transient 429 bursts ourselves.** In `_request`, a bounded outer retry loop catches 429s that bubble past urllib3 and sleeps a fixed `_RATE_LIMIT_BACKOFF = 30.0` sec between attempts, up to `_RATE_LIMIT_MAX_RETRIES = 3` rounds. The 30-sec window roughly matches Rack::Attack's default burst-window length; transient bursts recover cleanly. Past 3 rounds, the 429 surfaces as `DiscourseAPIError(status=429, ...)` so `PushResult.errors` records it without aborting the whole push.

**Layer 3: stop hitting the rate limits in the first place.** Layers 1 + 2 turn a deadlock into a fail-fast, but a fail-fast still fails the push when Discourse's per-user content-policy caps fire (`max_topics_in_first_day=3`, `max_topics_per_day=20`, `rate_limit_create_post=5`, etc.). Tuning these site settings ahead of time is whack-a-mole: Discourse has dozens of them, defaults are aggressive, and new ones get added between releases. Verified against `lib/rate_limiter.rb` (`rate_unlimited?`) and `app/models/user.rb` (`new_user_posting_on_first_day?`), every per-user rate check short-circuits to true when `user.staff?` returns true (staff = admin OR moderator).

So the real fix is `pipeline.push_forum` calling `client.grant_moderator(uid)` immediately after each `create_user`. The endpoint is `PUT /admin/users/:id/grant_moderation.json` (`app/controllers/admin/users_controller.rb#grant_moderation`, no body required); the master API key has the privilege. Cosmetic side effect: every seeded user appears with a moderator badge in the Discourse UI — fine for a fixture forum, would be wrong for a production import tool.

The site-setting tweaks (`rate_limit_create_post`, `max_topics_in_first_day`, `max_topics_per_day`, etc.) are still applied as defense-in-depth in case a future Discourse version moves the staff-bypass check into a setting-gated path, or in case `grant_moderator` itself fails for a particular user (`PushResult.errors` records the failure; subsequent posts by that user will hit the per-user caps and soft-skip).

The earlier `timeout=(5.0, 30.0)` tuple and `Session.headers["Connection"] = "close"` changes are kept. They didn't fix the symptom we cared about, but both are right on principle (stricter per-attempt enforcement; a bulk push doesn't benefit from connection reuse), and removing them would re-open ground we've already covered.

### Verification

End-to-end on a clean nuke + bootstrap + push at `tiny` scale: 6 categories, 10 users, 30 topics, 112 posts (close to the ~120 expected — minor variance from placeholder-body length filtering). Total push wall-clock ~3-4 min after the ~2-3 min cold boot. The parent project's scraper subsequently round-tripped all 33 topics (30 seeded + 3 Discourse-shipped welcome / FAQ / admin-guide).

### Lessons

- **TCP socket state is necessary but not sufficient evidence.** A `CLOSE_WAIT` is a real signal *something* is wedged on the network stack, but it doesn't tell you the cause. The python had no live connections at the time of the hang — the `CLOSE_WAIT` was a stale prior socket.
- **`urllib3.Retry`'s `Retry-After` honour is dangerous on a server that may say "come back in an hour."** Localhost dev forums absolutely will. So will any rate-limited public Discourse if you push hard enough. Always pair `respect_retry_after_header=False` with your own bounded outer retry — otherwise you're trading a deadlock for a fail-fast that gives up on a 30-sec burst window you could have waited out.
- **Server logs are authoritative when client behaviour is mysterious.** The python side showed "no CPU, no TCP, hung forever"; the Discourse container logs showed "429s, 429s, 429s." The mismatch was diagnostic.
- **Master keys ≠ rate-limit bypass.** They bypass *user-level* rate limits in the application that key against the API caller's identity, not the per-IP HTTP middleware that runs ahead of auth, and not per-impersonated-user content-policy caps. Bulk-import paths still need to think about Rack::Attack AND about the trust level / staff status of every impersonated author.
- **Don't enumerate settings; bypass the check.** Discourse has dozens of rate-limit settings. Even when you correctly raise three of them, the fourth bites. Promoting impersonated authors to staff routes the request through `RateLimiter#rate_unlimited?` → `true` and turns the entire surface into a no-op. Verified against the source rather than guessed.
- **Read the source before guessing setting names.** `max_topics_in_first_day` (correct) vs `max_first_day_topics` (wrong, silently no-ops on PUT) cost an entire verification cycle. `set_site_setting` doesn't 404 on unknown keys — Discourse's admin endpoint accepts the PUT and discards the value. The only safe verification is reading the actual setting back, or grepping `config/site_settings.yml` in the running container.

## Push timing reality

`pipeline.push_forum` POSTs in this order:

1. Site-setting tweaks (4 PUTs)
2. Categories (6 POSTs at `tiny` scale)
3. Users (10 POSTs at `tiny`)
4. Topics + posts in chronological order (~150 POSTs total at `tiny` — 30 OPs + ~120 replies)

The in-process pacing is `_PUSH_REQUEST_DELAY_SECONDS = 0.4` (a fixed sleep between every push request). At `tiny` scale that's a ~64-sec lower bound on wall-clock from the delay alone. With Sit 14.1's 429-recovery loop, a sustained burst that hits Rack::Attack adds up to 3 × 30 sec = 90 sec of recovery time per pinch-point — but only when the burst window is full, not on every request.

End-to-end on a tiny push: roughly **2–5 minutes** when no 429s are hit, **5–10 minutes** if Rack::Attack triggers a couple of times. (The earlier 10-20 min figure was a misdiagnosis — see "Sit 14.1" above; that wall-clock was urllib3 sleeping on Discourse's `Retry-After` hint, not real Sidekiq throughput.)

This is acceptable for a one-shot regression-test seed (the exact use case this subtree exists for). It is NOT acceptable for a bulk-import tool — and that's fine, because that's not what this is. Don't optimise it; the seeder is a fixture-builder, not a migration tool.

If you want a faster verification cycle, use `--dry-run` against the JSON dump path. Live mode exists for one purpose: prove the parent project's scraper can read back what the seeder pushed.

## Site settings the seeder tunes

Before the main push loop, `push_forum` issues four `PUT /admin/site_settings/<name>.json` calls to lower Discourse's per-user post-rate limits:

| Setting                              | Default | Push value | Reason                                   |
|--------------------------------------|---------|------------|------------------------------------------|
| `rate_limit_create_post`             | 5 s     | 1 s        | Heavy-poster Pareto would block self     |
| `rate_limit_create_topic`            | 5 s     | 1 s        | Same, for topic creation                 |
| `rate_limit_new_user_create_post`    | 30 s    | 1 s        | All seeded users are TL0 — this dominates |
| `unique_posts_mins`                  | 5 min   | 0          | Fixture bodies might collide deterministically |

These changes are **non-fatal on failure** (recorded to `PushResult.errors`) — the seeder still works at the default limits, just much slower. The settings are NOT reverted post-push: on a localhost test stack the next `make nuke` resets everything, and a real operator running a bulk import would tune these the same way. Crucially, none of these settings affect what the parent project's scraper *reads* — they're purely write-rate concerns, so the scraper sees an ordinary forum after the push.

## What if the push partial-fails

The seeder is **not idempotent**. Categories are created with deterministic names (e.g., `"Announcements"`, `"Help & Hints"` — see `crown_of_brine.CATEGORY_POOL`); a re-run against a forum that already has any of those names hits a 422 `Category Name has already been taken` on the first category and aborts. Topic titles are similarly deterministic (with the `(2)`, `(3)` dedup suffixes for slot-fill collisions); same fate.

**The only safe recovery is a full nuke + re-bootstrap:**

```bash
make -C sample nuke                                         # wipes all volumes
sed -i.bak 's|^DISCOURSE_API_KEY=.*|DISCOURSE_API_KEY=|' \
    sample/.env && rm -f sample/.env.bak                    # clear the orphaned key
make -C sample up                                           # cold boot ~2-3 min
until curl -sf -o /dev/null http://localhost:4200/srv/status; do sleep 2; done
make -C sample api-key                                      # mint a fresh master key
set -a && . sample/.env && set +a
export DISCOURSE_API_USERNAME="${DISCOURSE_API_USERNAME:-${DISCOURSE_USERNAME:-admin}}"
uv run python -m sample.seed init --seed 42 --scale tiny --no-llm
```

Don't try to "patch" via individual API deletes (`/categories/<id>.json` DELETE works for our seeded ones but not for the Discourse defaults, and the partial-state recovery surface multiplies fast). Partial state IS the bug.

**Don't double-spawn the push.** If a previous `init` is still running (background task, abandoned shell, etc.), pushing again from a second shell guarantees the second run hits the partial state created by the first. `pgrep -fa "sample.seed init"` before re-running — kill any stragglers with `kill <pid>` before nuking.

## Browser review

While the stack is up, open `http://localhost:4200`:

- **Admin** (`DISCOURSE_USERNAME` from `.env`, password = `DISCOURSE_PASSWORD`) — `/admin/dashboard`, `/admin/users`, `/admin/site_settings` (good place to see the seeder's tuning).
- **Seeded user** (any name from `/u`, password = `sample-seeder-password-1234`) — see what a regular forum member sees.
- **Anonymous** (`/categories`, `/latest`, individual topics) — read-only without login.

Useful sanity checks during a push:

```bash
# Posts created so far (refresh repeatedly to watch progress)
curl -sf -H "Api-Key: $DISCOURSE_API_KEY" -H "Api-Username: admin" \
    http://localhost:4200/site/statistics.json | jq '.posts_count, .topics_count, .users_count'

# Topics in latest with reply counts
curl -sf -H "Api-Key: $DISCOURSE_API_KEY" -H "Api-Username: admin" \
    "http://localhost:4200/latest.json?per_page=50" \
    | jq '.topic_list.topics[] | {id, posts_count, title}'
```

## Verifying with the parent scraper

The whole point of Sit 14 is to prove the parent project's scraper can read back the seeded forum. Once the push completes, run:

```bash
uv run discourse-explorer scrape http://localhost:4200 \
    --output /tmp/sample-scrape --dry-run
```

`--dry-run` is enough for the validation we care about: the scraper should enumerate topics, paginate posts, and exit cleanly. A real scrape (no `--dry-run`) downloads the JSON to `/tmp/sample-scrape/data/topics/` for further inspection by the visualizer / stats / query tools — see the project root README for those.

## Why Phase 3 sits here

This subtree is supporting infrastructure for the parent project's scraping pipeline, not a Discourse-import library. The "live push" path exists to give the scraper a real Discourse instance to read against — full stop. The seeder's value is the **bake → push → scrape → graph** loop closing without manual fixture editing; the per-call overhead and 10-20 min push time are acceptable as long as the loop terminates.
