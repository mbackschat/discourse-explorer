# DuckDB views (`stats.py`)

`stats.py::_connect(data_dir)` builds an in-memory DuckDB with views over `topics/*.json` (via `read_json`) and `categories.json`. No persisted DB — every invocation rebuilds. The `sql` subcommand opens a REPL (or runs a one-shot query).

The go-to view for ad-hoc work is **`topic_summary`**. Column lists below; at runtime, `.tables` and `.schema` inside the `sql` REPL are authoritative (they reflect whatever the current code produces, not this doc).

## View summary

| View | Grain | Purpose |
|---|---|---|
| `topics` | topic | Flat metadata, TIMESTAMPs, resolved `category` / `parent_category` / `category_raw` |
| `posts` | post | Every post with its topic's category columns and `updated_at` |
| `topic_tags` | topic × tag | Tag membership. Group and filter on **`tag_label`** — `tag_name` is the scrape-date-dependent display name |
| `categories` | category | Loaded from `categories.json`. Actually a `TABLE` (not a view). Columns: `id`, `name`, `slug`, `description`, `topic_count`, `post_count`, `color`, `parent_category_id`, `subcategory_ids`. |
| `topic_summary` | topic | `topics` columns + `first_poster` + comma-joined `tags`. Use this for most queries. |
| `topic_participants` | topic × user × role | Role ∈ {`creator`, `responder`, `system`} + per-topic post count, first/last timestamps, likes received |
| `topic_threads` | topic | Response metrics: `creator`, `first_responder`, `first_response_at`, `response_time` (INTERVAL), `last_responder`, `unique_responders` |
| `user_activity` | user | Aggregates: `topics_created`, `topics_helped`, `replies_written`, `total_posts`, `likes_received`, `total_reads`, `first_seen`, `last_seen` |

## Column reference

### `topics`

`id`, `title`, `slug`, `category_id`, `category_raw`, `category`, `parent_category`, `created_at`, `last_posted_at`, `bumped_at`, `views`, `like_count`, `posts_count`, `reply_count`, `pinned`, `closed`, `archived`

- `category` — resolved name: subcategory topics show their parent (e.g. "Engines" instead of "Unknown"). Use this for grouping.
- `parent_category` — non-null only when the topic is in a subcategory.
- `category_raw` — the original value from the scraped JSON.

### `posts`

`topic_id`, `topic_title`, `category_id`, `category`, `parent_category`, `post_id`, `post_number`, `username`, `display_name`, `created_at`, `updated_at`, `plain_text`, `like_count`, `reply_count`, `reply_to_post_number`, `quote_count`, `reads`

### `topic_tags`

`topic_id`, `topic_title`, `tag_id`, `tag_name`, `tag_slug`, `tag_label`

- `tag_label` — **the column to group and join on.** It matches the graph's tag node names, because both derive from the slug via `config.tag_label`.
- `tag_name` — Discourse's display name, which is *not* stable across scrapes: the same release tag (id 144, slug `2025-06`) appears as `2025․06` with U+2024 ONE DOT LEADER in topics fetched before ~2026-08 and as `2025-06` after. Grouping by `tag_name` splits one release into two rows.
- `tag_id` — the real Discourse tag id, or `NULL` for corpora scraped when `tags` was a bare `VARCHAR[]` (the view detects the shape at connect time and projects accordingly).

### `categories`

`id`, `name`, `slug`, `description`, `topic_count`, `post_count`, `color`, `parent_category_id`, `subcategory_ids`

- Defined as a `CREATE TABLE` in `stats.py`, not a view (but queries don't need to care).
- `subcategory_ids` is a `BIGINT[]` array; use `UNNEST(subcategory_ids)` to flatten.
- `parent_category_id` is JSON-typed (Discourse sometimes stores it nested); cast or extract as needed for joins.

### `topic_summary`

All `topics` columns + `first_poster`, `tags`

- `tags` — comma-joined `tag_label` values, so they read the same as the graph's tag nodes and carry no U+2024 homoglyphs.

### `topic_participants`

`topic_id`, `topic_title`, `category`, `username`, `role`, `posts_in_topic`, `first_post_at`, `last_post_at`, `likes_received`

### `topic_threads`

`topic_id`, `title`, `category`, `created_at`, `views`, `posts_count`, `creator`, `first_responder`, `first_response_at`, `response_time`, `last_responder`, `last_response_at`, `unique_responders`

### `user_activity`

`username`, `total_posts`, `topics_participated`, `topics_created`, `replies_written`, `topics_helped`, `likes_received`, `total_reads`, `first_seen`, `last_seen`

## Example queries

```sql
-- Most viewed open topics with their tags
SELECT id, title, first_poster, tags, views
FROM topic_summary
WHERE NOT closed AND NOT archived
ORDER BY views DESC
LIMIT 20;

-- Recently active threads (bumped in the last 30 days)
SELECT id, title, category, bumped_at::DATE AS last_activity, posts_count
FROM topics
WHERE bumped_at >= NOW() - INTERVAL '30 days'
ORDER BY bumped_at DESC;

-- Most replied-to topics
SELECT id, title, category, reply_count, views
FROM topics
ORDER BY reply_count DESC
LIMIT 15;

-- Topics started and never answered (single post, still open)
SELECT id, title, category, created_at::DATE AS created, views
FROM topics
WHERE posts_count = 1 AND NOT closed AND NOT archived
ORDER BY created_at DESC;

-- Response time by category (interval → hours)
SELECT category,
       COUNT(*) AS topics,
       ROUND(MEDIAN(EXTRACT(EPOCH FROM response_time)) / 3600, 1) AS median_hours
FROM topic_threads
WHERE response_time IS NOT NULL
GROUP BY category
ORDER BY median_hours DESC;
```

## Invariants

- **Category resolution is in the `topics` view** (subcategory → parent via `categories.json`). Downstream views inherit it; don't re-resolve in ad-hoc queries.
- **`response_time` is a DuckDB `INTERVAL`.** Use `EXTRACT(EPOCH FROM response_time)` to get seconds. DuckDB's default cast to text is readable (`01:23:45`) but not aggregatable.
- **`topic_participants.role`** is `creator` | `responder` | `system`, derived from:
  ```sql
  CASE WHEN post_number = 1 THEN 'creator'
       WHEN username = 'system' THEN 'system'
       ELSE 'responder'
  END
  ```
  The `system` role only applies when the username is literally `system` (Discourse's built-in account for automated / system-generated posts). **It is NOT a general bot filter** — other bot accounts (with their own usernames) land in `responder`. Filter `role != 'system'` to exclude Discourse system posts specifically, not arbitrary bots; for bot accounts, filter by `username` explicitly.
- **`topic_participants.likes_received`** is `HUGEINT` (SUM-promoted from BIGINT); cast to `BIGINT` if needed for JSON serialization.
- **`user_activity`** aggregate columns are a mix of `BIGINT` and `HUGEINT` depending on whether they come from a COUNT or a SUM — not type-homogeneous.
