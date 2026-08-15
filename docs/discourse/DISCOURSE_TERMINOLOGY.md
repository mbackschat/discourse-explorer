# DISCOURSE_TERMINOLOGY

Compact conceptual model of a Discourse forum, optimized for LLMs/agents.

## Core hierarchy

```text
Site
└── Category
    └── Topic
        └── Post
```

- **Site**: the entire Discourse forum instance.
- **Category**: top-level content area; strong organizational boundary.
- **Subcategory**: nested category under a parent category.
- **Topic**: one discussion thread inside a category.
- **Post**: one individual message inside a topic.

## Key distinctions

- **Category** = broad container
- **Topic** = specific discussion thread
- **Post** = message within a thread
- **Tag** = lightweight label attached to a topic

## Important entities

- **Tag**: flexible metadata for topics; cross-cuts categories.
- **User**: forum account.
- **Group**: collection of users for permissions, mentions, and access control.
- **Trust Level**: user capability tier (TL0–TL4).
- **Moderator**: manages content/community.
- **Admin**: full site configuration authority.

## Conversation types

- **Public topic**: visible forum discussion.
- **Private message (PM)**: topic-like private conversation between users/groups.

## Topic states

- **Pinned**: kept near the top of lists.
- **Closed**: no new replies allowed.
- **Archived**: frozen; limited interaction/editing.
- **Unlisted**: hidden from normal topic lists.
- **Solved**: a solution has been marked (if enabled).

## User relationship states

Applied to topics/categories/tags:

- **Watching**: notify for all new replies.
- **Tracking**: show unread/new counts.
- **Normal**: default behavior.
- **Muted**: suppress from normal lists/notifications.

## Post interaction terms

- **Reply**: response in a topic.
- **Reply to post**: reply linked to a specific earlier post.
- **Quote**: excerpt of another post in a reply.
- **Like**: lightweight positive feedback.
- **Bookmark**: personal saved reference.
- **Flag**: report for moderator review.

## Composition terms

- **Composer**: editor UI for writing topics/posts/messages.
- **Draft**: saved unfinished content.
- **Reply as linked topic**: creates a new topic that references an existing post/topic.

## Navigation views

- **Latest**: recently active topics.
- **New**: topics new to the current user.
- **Unread**: tracked topics with unread posts.
- **Top**: high-engagement topics.
- **Categories**: category overview.

## Minimal mental model

Use this mapping when parsing Discourse data:

- A **site** contains **categories**
- A **category** contains **topics**
- A **topic** contains ordered **posts**
- A **topic** may also have **tags**
- **Users** create posts/topics
- **Groups** and **trust levels** affect permissions
- **PMs** behave like private topics

## Example

```text
Site: discourse.example.com
Category: Support
Subcategory: Authentication
Topic: JWT login fails after refresh
Posts:
  1. User reports bug
  2. Staff asks for logs
  3. User shares config
  4. Staff provides fix
Tags: jwt, api, bug
```
