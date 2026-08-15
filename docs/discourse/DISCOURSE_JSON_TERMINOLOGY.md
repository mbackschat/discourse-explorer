# DISCOURSE_JSON_TERMINOLOGY

Compact terminology for understanding Discourse JSON responses when scraping or ingesting forum data with LLMs/agents.

## Core JSON shapes

### Topic list response

```json
{
  "users": [...],
  "topic_list": {
    "topics": [...],
    "more_topics_url": "...",
    "per_page": 30
  }
}
```

Meaning:
- **topic_list**: one page of topic/thread summaries
- **topics**: array of topic objects
- **users**: side-loaded user records referenced by topics
- **more_topics_url**: pagination continuation

### Single topic response

```json
{
  "id": 123,
  "title": "Example topic",
  "slug": "example-topic",
  "posts_count": 42,
  "post_stream": {
    "posts": [...],
    "stream": [111, 112, 113]
  }
}
```

Meaning:
- **post_stream**: topic content container
- **posts**: included post objects in current chunk
- **stream**: ordered list of all post IDs in the topic

## Most important terms

- **id**: entity ID; meaning depends on object type
- **topic_id**: parent topic ID of a post
- **category_id**: parent category ID of a topic
- **post_number**: ordinal position of a post within a topic
- **slug**: URL-safe title fragment; not the durable key
- **title**: topic title
- **tags**: labels attached to a topic
- **posters**: compact participant summary in topic lists

## Topic fields commonly used

- **id**
- **title**
- **slug**
- **category_id**
- **tags**
- **posts_count**
- **reply_count**
- **views**
- **like_count**
- **created_at**
- **last_posted_at**
- **pinned**
- **closed**
- **archived**

Interpretation:
- topic = thread metadata, not the full thread content

## Post fields commonly used

- **id**
- **topic_id**
- **post_number**
- **username**
- **name**
- **created_at**
- **updated_at**
- **raw**
- **cooked**
- **reply_to_post_number**
- **quote_count**
- **reads**
- **readers_count**
- **score**
- **actions_summary**

Interpretation:
- post = the actual message/content unit

## Critical distinctions

### `id` vs `post_number`

- **id** = globally unique post ID
- **post_number** = position inside one topic

Use:
- `id` for deduplication
- `post_number` for reconstructing topic order

### `raw` vs `cooked`

- **raw** = original author text/source markup
- **cooked** = rendered HTML

Use:
- `raw` for NLP, embeddings, LLM ingestion
- `cooked` for UI rendering or preserving rendered formatting

### `topic_list.topics` vs `post_stream.posts`

- **topic_list.topics** = thread summaries
- **post_stream.posts** = actual message objects

## Pagination terms

- **more_topics_url**: next page for topic lists
- **page**: page number parameter on some routes
- **per_page**: number of topics per page
- **chunk_size**: number of posts included in current topic chunk
- **stream**: use these post IDs to fetch or reconstruct the full topic

## Topic state fields

- **pinned**
- **closed**
- **archived**
- **unlisted** (may appear depending on endpoint/setup)

These are operational flags on a topic.

## User-related JSON terms

- **users**: side-loaded user records
- **username**: stable textual user handle
- **name**: display name
- **trust_level**: user capability tier
- **admin**: admin flag
- **moderator**: moderator flag

## Category-related JSON terms

- **category_id**: foreign key from topic to category
- category objects usually contain:
  - **id**
  - **name**
  - **slug**
  - **parent_category_id**

## Useful scraper mental model

Normalize Discourse JSON into:

```text
Category
- id
- name
- slug
- parent_category_id

Topic
- id
- slug
- title
- category_id
- created_at
- last_posted_at
- posts_count
- views
- like_count
- tags[]
- pinned
- closed
- archived

Post
- id
- topic_id
- post_number
- username / user_id
- created_at
- updated_at
- raw
- cooked
- reply_to_post_number

User
- id
- username
- name
- trust_level
- admin
- moderator
```

## Two important scraper rules

1. **Topic list endpoints are summary endpoints**
   - good for discovering topics
   - not enough for full-content extraction

2. **Single topic responses may still be partial**
   - included `posts` may only be the first chunk
   - `stream` is the authoritative ordered list of all post IDs

## Minimal glossary

- **topic_list** = page of topic summaries
- **topics** = array of topic/thread summaries
- **users** = side-loaded user lookup records
- **post_stream** = topic post container
- **posts** = included post objects
- **stream** = ordered list of all post IDs in topic
- **id** = entity ID
- **topic_id** = topic foreign key on post
- **category_id** = category foreign key on topic
- **post_number** = post order within topic
- **slug** = URL fragment
- **raw** = source text
- **cooked** = rendered HTML
- **tags** = topic labels
- **posters** = participant summary
- **more_topics_url** = topic-list pagination
- **chunk_size** = included post count in current topic chunk

## Recommended LLM/agent interpretation

When reading Discourse JSON:
- treat **topics** as metadata containers
- treat **posts** as the actual conversational corpus
- prefer **raw** over **cooked** for semantic analysis
- use **post_number** to preserve conversation order
- use **id** fields as durable keys
- join **topic -> category** via `category_id`
- join **post -> topic** via `topic_id`
