# Pain-points audit — seed42-tiny (replayable)

*2026-04-29T00:42:54Z by Showboat 0.6.1*
<!-- showboat-id: 14036ccb-e6c0-42fd-b383-6b3df34eb68f -->

Companion to ../01-2026-04-29-full-corpus.md (narrative) and sibling 01-2026-04-29-full-corpus.md (commands-only).

Re-run from project root: `showboat verify <path-to-this-doc>`. From other CWD: `showboat --workdir /path/to/discourse-explorer verify <path-to-this-doc>`.

LLM drill-downs (6 × --mode local) are intentionally excluded — non-deterministic and cost-bearing. All stats blocks below are deterministic and verifiable.

## Schema verification

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql 'DESCRIBE topic_summary' 2>/dev/null
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
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql 'DESCRIBE posts' 2>/dev/null
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

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql 'DESCRIBE topic_threads' 2>/dev/null
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
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql 'DESCRIBE user_activity' 2>/dev/null
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

## Skeleton probes

### Probe 1 — Theme frequency (corpus-calibrated keyword sweep)

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
WITH themes(theme) AS (VALUES
  ('softlock'),('crash'),('glitch'),('bug'),('broken'),('stuck'),('workaround'),
  ('not working'),('doesn''t work'),('can''t'),('cannot'),('unable'),
  ('untranslated'),('mistranslat'),('localization'),
  ('issue'),('problem'),('help'),('confused'),('unclear'),
  ('remaster'),('amiga'),('android')
)
SELECT t.theme,
       COUNT(DISTINCT p.topic_id) AS topics,
       COUNT(*) AS posts
FROM themes t
JOIN posts p ON p.plain_text ILIKE '%' || t.theme || '%'
GROUP BY t.theme
ORDER BY topics DESC, posts DESC, theme
" 2>/dev/null
```

```output
theme        | topics | posts
-------------+--------+------
help         | 25     | 57   
issue        | 15     | 37   
stuck        | 14     | 29   
bug          | 14     | 25   
workaround   | 11     | 28   
remaster     | 10     | 35   
glitch       | 9      | 20   
problem      | 7      | 13   
crash        | 5      | 10   
softlock     | 4      | 10   
localization | 2      | 9    
untranslated | 2      | 6    
amiga        | 2      | 4    
broken       | 2      | 3    
confused     | 2      | 3    
android      | 1      | 4    

(16 rows)
```

### Probe 2 — Top-engagement unresolved (posts_count >= 5, open topics)

Note: all topics show views=0 — seeder quirk; the views >= 50/80 triggers from the skill catalog are unusable on this corpus.

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT id, title, category, posts_count, views, created_at::DATE AS created
FROM topic_summary
WHERE posts_count >= 5 AND closed = false AND archived = false
ORDER BY posts_count DESC, views DESC, id
LIMIT 20
" 2>/dev/null
```

```output
id | title                                                 | category                    | posts_count | views | created   
---+-------------------------------------------------------+-----------------------------+-------------+-------+-----------
27 | Hint needed: the lighthouse cipher                    | Help & Hints                | 8           | 0     | 2025-11-05
34 | Doubloon SDK fails to load music stems                | Bug Reports                 | 8           | 0     | 2026-02-04
31 | Glitchless category for Crown of Brine                | Speedruns & Challenges      | 7           | 0     | 2025-12-04
38 | Reminder: tagging convention for `solved`             | Announcements               | 7           | 0     | 2026-03-09
35 | Embroidered the spectral cutlass — finished piece     | Show & Tell                 | 6           | 0     | 2026-02-18
15 | Untranslated dialogue trees in Crown of Brine: Reborn | Translations & Localization | 5           | 0     | 2025-05-24
16 | Localization notes for Chapter 3: Below the Brine     | Translations & Localization | 5           | 0     | 2025-05-26
22 | Painted miniature of the Drowned Market               | Show & Tell                 | 5           | 0     | 2025-08-30

(8 rows)
```

### Probe 3 — Category × theme crosstab

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
WITH themes(theme) AS (VALUES ('softlock'),('crash'),('glitch'),('workaround'),('untranslat'),('remaster'),('amiga')),
     hits AS (
  SELECT p.category, t.theme, COUNT(DISTINCT p.topic_id) AS topics
  FROM themes t JOIN posts p ON p.plain_text ILIKE '%' || t.theme || '%'
  GROUP BY p.category, t.theme
)
SELECT category, theme, topics
FROM hits
WHERE topics > 0
ORDER BY category, topics DESC, theme
" 2>/dev/null
```

```output
category                    | theme      | topics
----------------------------+------------+-------
Announcements               | remaster   | 3     
Announcements               | workaround | 1     
Bug Reports                 | workaround | 10    
Bug Reports                 | glitch     | 6     
Bug Reports                 | crash      | 5     
Bug Reports                 | softlock   | 4     
Bug Reports                 | remaster   | 3     
Bug Reports                 | amiga      | 2     
Help & Hints                | remaster   | 1     
Show & Tell                 | glitch     | 1     
Show & Tell                 | remaster   | 1     
Speedruns & Challenges      | glitch     | 2     
Translations & Localization | remaster   | 2     
Translations & Localization | untranslat | 2     

(14 rows)
```

### Probe 4 — Gap-admission phrasing (denial / "can't be done" voice)

Sparse signal (2 hits) typical for a community forum without active staff voice. Both hits are from users reporting NPC-interaction softlocks (topics 21 and 26).

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT topic_id, post_number, username, LEFT(plain_text, 180) AS snippet
FROM posts
WHERE plain_text ILIKE '%not supported%'
   OR plain_text ILIKE '%not possible%'
   OR plain_text ILIKE '%can''t do that%'
   OR plain_text ILIKE '%no way to%'
   OR plain_text ILIKE '%won''t work%'
   OR plain_text ILIKE '%no plan%'
   OR plain_text ILIKE '%short answer is no%'
   OR plain_text ILIKE '%doesn''t exist%'
ORDER BY topic_id, post_number
" 2>/dev/null
```

```output
topic_id | post_number | username         | snippet                                                     
---------+-------------+------------------+-------------------------------------------------------------
21       | 1           | salty_cabin-boy  | Hey folks, just wanted to flag a pretty frustrating bug I...
26       | 3           | scurvy_harpooner | I ran into the exact same issue when I tried to chat with...

(2 rows)
```

## Enriched References

Topics cited by drill-downs (topics 27, 34, 31, 15, 16, 26) plus unanswered community-health hotspots (20, 21, 23).

```bash
uv run discourse-explorer stats --path sample/fixtures/seed42-tiny sql "
SELECT id, title, category, posts_count, views, like_count, created_at::DATE AS created, tags
FROM topic_summary
WHERE id IN (15, 16, 20, 21, 23, 26, 27, 31, 34)
ORDER BY id
" 2>/dev/null
```

```output
id | title                                                        | category                    | posts_count | views | like_count | created    | tags                                    
---+--------------------------------------------------------------+-----------------------------+-------------+-------+------------+------------+-----------------------------------------
15 | Untranslated dialogue trees in Crown of Brine: Reborn        | Translations & Localization | 5           | 0     | 0          | 2025-05-24 | localization, remaster, voice-acting    
16 | Localization notes for Chapter 3: Below the Brine            | Translations & Localization | 5           | 0     | 0          | 2025-05-26 | game-2, localization, puzzle-design     
20 | Can't get past the Drowned Market — any tips?                | Help & Hints                | 1           | 0     | 0          | 2025-07-21 | localization, locations, music, remaster
21 | Softlock when interacting with the pirate-hopeful protago... | Bug Reports                 | 1           | 0     | 0          | 2025-07-29 | amiga, bug, remaster, spoiler           
23 | Untranslated music stems in Crown of Brine II: The Phanto... | Translations & Localization | 1           | 0     | 0          | 2025-09-02 | feature-request, remaster               
26 | Softlock when interacting with the lighthouse hermit         | Bug Reports                 | 4           | 0     | 0          | 2025-10-05 | game-2, voice-acting, walkthrough       
27 | Hint needed: the lighthouse cipher                           | Help & Hints                | 8           | 0     | 0          | 2025-11-05 | hint-needed                             
31 | Glitchless category for Crown of Brine                       | Speedruns & Challenges      | 7           | 0     | 0          | 2025-12-04 | classic-mac, duplicate, modded, solved  
34 | Doubloon SDK fails to load music stems                       | Bug Reports                 | 8           | 0     | 0          | 2026-02-04 | game-1, game-2, wont-fix                

(9 rows)
```
