# Community-health audit — seed42-tiny (replayable)

*2026-04-29T00:25:07Z by Showboat 0.6.1*
<!-- showboat-id: 528f7af6-94a2-4854-8089-dd2af815d82e -->

Companion to ../01-2026-04-29-full-corpus.md (narrative) and sibling 01-2026-04-29-full-corpus.md (commands-only).

Re-run from project root: `showboat verify <path-to-this-doc>`. From other CWD: `showboat --workdir /path/to/discourse-explorer verify <path-to-this-doc>`.

## Schema verification

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "DESCRIBE topic_threads"
```

```output
column_name       | column_type | null | key | default | extra
------------------+-------------+------+-----+---------+------
topic_id          | BIGINT      | YES  |     |         |      
title             | VARCHAR     | YES  |     |         |      
category          | VARCHAR     | YES  |     |         |      
created_at        | TIMESTAMP   | YES  |     |         |      
views             | BIGINT      | YES  |     |         |      
posts_count       | BIGINT      | YES  |     |         |      
creator           | VARCHAR     | YES  |     |         |      
first_responder   | VARCHAR     | YES  |     |         |      
first_response_at | TIMESTAMP   | YES  |     |         |      
response_time     | INTERVAL    | YES  |     |         |      
last_responder    | VARCHAR     | YES  |     |         |      
last_response_at  | TIMESTAMP   | YES  |     |         |      
unique_responders | BIGINT      | YES  |     |         |      

(13 rows)
```

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "DESCRIBE topic_summary"
```

```output
column_name     | column_type | null | key | default | extra
----------------+-------------+------+-----+---------+------
id              | BIGINT      | YES  |     |         |      
title           | VARCHAR     | YES  |     |         |      
slug            | VARCHAR     | YES  |     |         |      
category_id     | BIGINT      | YES  |     |         |      
category_raw    | VARCHAR     | YES  |     |         |      
category        | VARCHAR     | YES  |     |         |      
parent_category | VARCHAR     | YES  |     |         |      
created_at      | TIMESTAMP   | YES  |     |         |      
last_posted_at  | TIMESTAMP   | YES  |     |         |      
bumped_at       | TIMESTAMP   | YES  |     |         |      
views           | BIGINT      | YES  |     |         |      
like_count      | BIGINT      | YES  |     |         |      
posts_count     | BIGINT      | YES  |     |         |      
reply_count     | BIGINT      | YES  |     |         |      
pinned          | BOOLEAN     | YES  |     |         |      
closed          | BOOLEAN     | YES  |     |         |      
archived        | BOOLEAN     | YES  |     |         |      
first_poster    | VARCHAR     | YES  |     |         |      
tags            | VARCHAR     | YES  |     |         |      

(19 rows)
```

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "DESCRIBE user_activity"
```

```output
column_name         | column_type | null | key | default | extra
--------------------+-------------+------+-----+---------+------
username            | VARCHAR     | YES  |     |         |      
total_posts         | BIGINT      | YES  |     |         |      
topics_participated | BIGINT      | YES  |     |         |      
topics_created      | HUGEINT     | YES  |     |         |      
replies_written     | HUGEINT     | YES  |     |         |      
topics_helped       | BIGINT      | YES  |     |         |      
likes_received      | HUGEINT     | YES  |     |         |      
total_reads         | HUGEINT     | YES  |     |         |      
first_seen          | TIMESTAMP   | YES  |     |         |      
last_seen           | TIMESTAMP   | YES  |     |         |      

(10 rows)
```

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "DESCRIBE posts"
```

```output
column_name          | column_type | null | key | default | extra
---------------------+-------------+------+-----+---------+------
topic_id             | BIGINT      | YES  |     |         |      
topic_title          | VARCHAR     | YES  |     |         |      
category_id          | BIGINT      | YES  |     |         |      
category             | VARCHAR     | YES  |     |         |      
parent_category      | VARCHAR     | YES  |     |         |      
post_id              | BIGINT      | YES  |     |         |      
post_number          | BIGINT      | YES  |     |         |      
username             | VARCHAR     | YES  |     |         |      
display_name         | VARCHAR     | YES  |     |         |      
created_at           | TIMESTAMP   | YES  |     |         |      
updated_at           | TIMESTAMP   | YES  |     |         |      
plain_text           | VARCHAR     | YES  |     |         |      
like_count           | BIGINT      | YES  |     |         |      
reply_count          | BIGINT      | YES  |     |         |      
reply_to_post_number | BIGINT      | YES  |     |         |      
quote_count          | BIGINT      | YES  |     |         |      
reads                | BIGINT      | YES  |     |         |      

(17 rows)
```

## Skeleton probes

### Probe 1 — Median first-response time per category

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT category,
       COUNT(*) AS topics,
       ROUND(EXTRACT(EPOCH FROM MEDIAN(response_time))/3600, 1) AS hours_to_first_reply,
       ROUND(EXTRACT(EPOCH FROM MIN(response_time))/3600, 1) AS min_hours,
       ROUND(EXTRACT(EPOCH FROM MAX(response_time))/3600, 1) AS max_hours
FROM topic_threads
WHERE response_time IS NOT NULL
GROUP BY category
ORDER BY hours_to_first_reply DESC, category
"
```

```output
category                    | topics | hours_to_first_reply | min_hours | max_hours
----------------------------+--------+----------------------+-----------+----------
Translations & Localization | 2      | 159.5                | 159.5     | 215.9    
Help & Hints                | 3      | 141.8                | 37.8      | 213.3    
Bug Reports                 | 8      | 110.8                | 4.0       | 215.0    
Announcements               | 5      | 46.5                 | 15.7      | 126.4    
Show & Tell                 | 4      | 24.5                 | 20.8      | 131.5    
Speedruns & Challenges      | 3      | 12.2                 | 0.3       | 207.6    

(6 rows)
```

### Probe 2 — Top responder per category + concentration flag

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
WITH replies AS (
  SELECT category, username, COUNT(*) AS reply_count
  FROM posts
  WHERE post_number > 1
  GROUP BY category, username
),
totals AS (
  SELECT category, SUM(reply_count) AS total_replies FROM replies GROUP BY category
),
ranked AS (
  SELECT r.category, r.username, r.reply_count,
         ROW_NUMBER() OVER (PARTITION BY r.category ORDER BY r.reply_count DESC, r.username) AS rk,
         t.total_replies
  FROM replies r JOIN totals t USING (category)
)
SELECT category,
       username AS top_responder,
       reply_count,
       total_replies,
       ROUND(100.0 * reply_count / total_replies, 1) AS share_pct,
       CASE WHEN 100.0 * reply_count / total_replies >= 30 THEN 'CONCENTRATED' ELSE '' END AS flag
FROM ranked
WHERE rk = 1
ORDER BY share_pct DESC, category
"
```

```output
category                    | top_responder       | reply_count | total_replies | share_pct | flag        
----------------------------+---------------------+-------------+---------------+-----------+-------------
Staff                       | system              | 1           | 1             | 100.0     | CONCENTRATED
Translations & Localization | scurvy_harpooner    | 4           | 8             | 50.0      | CONCENTRATED
Speedruns & Challenges      | scurvy_harpooner    | 5           | 12            | 41.7      | CONCENTRATED
Show & Tell                 | salty_cabin-boy     | 4           | 13            | 30.8      | CONCENTRATED
Help & Hints                | rusty_quartermaster | 3           | 11            | 27.3      |             
Announcements               | salty_cabin-boy     | 3           | 14            | 21.4      |             
Bug Reports                 | salty_cabin-boy     | 5           | 24            | 20.8      |             

(7 rows)
```

### Probe 3b — Unanswered topics by views (deterministic SQL form)

Note: Probe 3a (built-in unanswered list) is captured in the companion file only; it is excluded here because its output is already covered by 3b and it does not need LLM interpretation.

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT id, title, category, posts_count, views, created_at::DATE AS created
FROM topic_summary
WHERE posts_count <= 1 AND closed = false AND archived = false
ORDER BY views DESC, posts_count DESC, id
"
```

```output
id | title                                                        | category                    | posts_count | views | created   
---+--------------------------------------------------------------+-----------------------------+-------------+-------+-----------
5  | Welcome to Discourse! :wave:                                 | General                     | 1           | 0     | 2026-04-28
6  | Admin Guide: Getting Started                                 | Staff                       | 1           | 0     | 2026-04-28
20 | Can't get past the Drowned Market — any tips?                | Help & Hints                | 1           | 0     | 2025-07-21
21 | Softlock when interacting with the pirate-hopeful protago... | Bug Reports                 | 1           | 0     | 2025-07-29
23 | Untranslated music stems in Crown of Brine II: The Phanto... | Translations & Localization | 1           | 0     | 2025-09-02
28 | Embroidered the lost map — finished piece                    | Show & Tell                 | 1           | 0     | 2025-11-27

(6 rows)
```

### Probe 4 — Top users by activity

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT username,
       total_posts,
       topics_participated,
       topics_created,
       replies_written,
       topics_helped,
       first_seen::DATE AS first_seen,
       last_seen::DATE AS last_seen
FROM user_activity
ORDER BY total_posts DESC, username
LIMIT 15
"
```

```output
username            | total_posts | topics_participated | topics_created | replies_written | topics_helped | first_seen | last_seen 
--------------------+-------------+---------------------+----------------+-----------------+---------------+------------+-----------
scurvy_harpooner    | 24          | 17                  | 5              | 19              | 14            | 2025-05-28 | 2026-03-13
rusty_quartermaster | 19          | 12                  | 9              | 10              | 6             | 2025-05-05 | 2026-03-17
salty_cabin-boy     | 17          | 13                  | 5              | 12              | 9             | 2025-05-01 | 2026-03-17
jolly_helmsman      | 11          | 9                   | 4              | 7               | 6             | 2025-05-05 | 2026-03-18
soggy_rigger        | 11          | 11                  | 1              | 10              | 10            | 2025-06-01 | 2026-03-15
soggy_gull          | 8           | 7                   | 1              | 7               | 6             | 2025-05-24 | 2026-03-13
soggy_marlin        | 8           | 6                   | 1              | 7               | 6             | 2025-05-27 | 2026-03-14
weathered_harpooner | 6           | 6                   | 1              | 5               | 5             | 2025-05-28 | 2026-03-20
briny_privateer     | 5           | 4                   | 2              | 3               | 3             | 2025-08-30 | 2026-02-07
lucky_cooper        | 3           | 3                   | 1              | 2               | 2             | 2025-05-26 | 2026-03-14

(10 rows)
```

### Probe 5 — Topic velocity per month

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT date_trunc('month', created_at)::DATE AS month,
       COUNT(*) AS topics,
       SUM(posts_count) AS posts,
       SUM(views) AS views
FROM topic_summary
GROUP BY 1
ORDER BY 1
"
```

```output
month      | topics | posts | views
-----------+--------+-------+------
2025-05-01 | 6      | 26    | 0    
2025-07-01 | 3      | 4     | 0    
2025-08-01 | 1      | 5     | 0    
2025-09-01 | 3      | 7     | 0    
2025-10-01 | 1      | 4     | 0    
2025-11-01 | 2      | 9     | 0    
2025-12-01 | 4      | 13    | 0    
2026-01-01 | 1      | 2     | 0    
2026-02-01 | 2      | 14    | 0    
2026-03-01 | 7      | 28    | 0    
2026-04-01 | 3      | 4     | 0    

(11 rows)
```

## Enriched references batch

Topics drilled: 20, 21, 23.

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT id, title, category, posts_count, views, like_count, created_at::DATE AS created, tags
FROM topic_summary
WHERE id IN (20, 21, 23)
ORDER BY id
"
```

```output
id | title                                                        | category                    | posts_count | views | like_count | created    | tags                                    
---+--------------------------------------------------------------+-----------------------------+-------------+-------+------------+------------+-----------------------------------------
20 | Can't get past the Drowned Market — any tips?                | Help & Hints                | 1           | 0     | 0          | 2025-07-21 | localization, locations, music, remaster
21 | Softlock when interacting with the pirate-hopeful protago... | Bug Reports                 | 1           | 0     | 0          | 2025-07-29 | amiga, bug, remaster, spoiler           
23 | Untranslated music stems in Crown of Brine II: The Phanto... | Translations & Localization | 1           | 0     | 0          | 2025-09-02 | feature-request, remaster               

(3 rows)
```
