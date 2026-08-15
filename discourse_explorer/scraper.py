#!/usr/bin/env python3
"""
Discourse forum scraper with full and delta sync support.

Usage:
    discourse-explorer scrape              # Delta sync (full on first run)
    discourse-explorer scrape --full       # Force full re-sync
    discourse-explorer scrape --dry-run    # Show what would be fetched
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

from discourse_explorer.auth import AuthError, get_session
from discourse_explorer.config import (
    MAX_RETRIES,
    REQUEST_DELAY,
    RETRY_BACKOFF,
    ConfigError,
    RuntimeConfig,
    SitePaths,
    bootstrap,
    site_paths_from_dir,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class HTMLTextExtractor(HTMLParser):
    """Strip HTML tags to produce plain text."""

    def __init__(self):
        super().__init__()
        self._pieces = []

    def handle_data(self, data):
        self._pieces.append(data)

    def get_text(self):
        return "".join(self._pieces).strip()


def html_to_text(html: str) -> str:
    extractor = HTMLTextExtractor()
    extractor.feed(html)
    return extractor.get_text()


def ensure_dirs(paths: SitePaths):
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.topics_dir.mkdir(parents=True, exist_ok=True)


def load_sync_state(paths: SitePaths) -> dict:
    if paths.sync_state_file.exists():
        return json.loads(paths.sync_state_file.read_text())
    return {"last_sync": None, "synced_topics": {}}


def save_sync_state(paths: SitePaths, state: dict):
    paths.sync_state_file.write_text(json.dumps(state, indent=2))


def save_index(paths: SitePaths, index: list):
    paths.index_file.write_text(json.dumps(index, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Rate-limited HTTP client
# ---------------------------------------------------------------------------

class RateLimitedClient:
    """Wraps a requests.Session with rate limiting, retries, and backoff."""

    def __init__(self, session: requests.Session):
        self.session = session
        self.delay = REQUEST_DELAY
        self._last_request_time = 0.0
        self.request_count = 0

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET with rate limiting, retry on 429, and exponential backoff on errors."""
        for attempt in range(MAX_RETRIES + 1):
            # Enforce minimum delay between requests
            elapsed = time.time() - self._last_request_time
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)

            self._last_request_time = time.time()
            self.request_count += 1

            try:
                resp = self.session.get(url, **kwargs)
            except requests.exceptions.ConnectionError as e:
                if attempt < MAX_RETRIES:
                    wait = self.delay * (RETRY_BACKOFF ** (attempt + 1))
                    print(f"  Connection error, retrying in {wait:.0f}s... ({e})")
                    time.sleep(wait)
                    continue
                raise

            if resp.status_code == 429:
                # Rate limited — respect Retry-After header
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    wait = int(retry_after)
                else:
                    wait = self.delay * (RETRY_BACKOFF ** (attempt + 1))
                    wait = min(wait, 120)  # cap at 2 minutes
                print(f"  Rate limited (429). Waiting {wait:.0f}s...")
                # Increase base delay adaptively
                self.delay = min(self.delay * 1.5, 10.0)
                time.sleep(wait)
                continue

            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                wait = self.delay * (RETRY_BACKOFF ** (attempt + 1))
                print(f"  Server error ({resp.status_code}), retrying in {wait:.0f}s...")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        # Should not reach here, but just in case
        resp.raise_for_status()
        return resp


# ---------------------------------------------------------------------------
# Scraper logic
# ---------------------------------------------------------------------------

def fetch_categories(client: RateLimitedClient, base_url: str, paths: SitePaths) -> dict:
    """Fetch all categories and save to file."""
    print("Fetching categories...")
    resp = client.get(f"{base_url}/categories.json")
    data = resp.json()
    categories = data.get("category_list", {}).get("categories", [])

    result = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "categories": [
            {
                "id": c["id"],
                "name": c["name"],
                "slug": c["slug"],
                "description": c.get("description_text", ""),
                "topic_count": c.get("topic_count", 0),
                "post_count": c.get("post_count", 0),
                "color": c.get("color", ""),
                "parent_category_id": c.get("parent_category_id"),
                "subcategory_ids": c.get("subcategory_ids", []),
            }
            for c in categories
        ],
    }
    paths.categories_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"  Found {len(result['categories'])} categories.")
    return result


def fetch_all_topic_ids(client: RateLimitedClient, base_url: str, since: str | None = None) -> list[dict]:
    """
    Fetch all topics via /latest.json pagination.
    If `since` is given (ISO timestamp), stop when all topics on a page are older.
    Returns list of topic metadata dicts.
    """
    print("Fetching topic list...")
    all_topics = []
    page = 0
    seen_ids = set()

    while True:
        resp = client.get(f"{base_url}/latest.json?page={page}")
        data = resp.json()
        topics = data.get("topic_list", {}).get("topics", [])

        if not topics:
            break

        new_on_page = 0
        all_older = True

        for t in topics:
            if t["id"] in seen_ids:
                continue
            seen_ids.add(t["id"])
            new_on_page += 1

            topic_meta = {
                "id": t["id"],
                "title": t.get("title", ""),
                "category_id": t.get("category_id"),
                "created_at": t.get("created_at", ""),
                "bumped_at": t.get("bumped_at", ""),
                "last_posted_at": t.get("last_posted_at", ""),
                "posts_count": t.get("posts_count", 0),
                "reply_count": t.get("reply_count", 0),
                "views": t.get("views", 0),
                "like_count": t.get("like_count", 0),
                "tags": t.get("tags", []),
                "pinned": t.get("pinned", False),
                "closed": t.get("closed", False),
                "archived": t.get("archived", False),
            }
            all_topics.append(topic_meta)

            # Check if this topic is newer than our last sync
            bumped = t.get("bumped_at", t.get("last_posted_at", ""))
            if since and bumped and bumped > since:
                all_older = False

        print(f"  Page {page}: {new_on_page} topics (total: {len(all_topics)})")

        # Stop conditions
        if new_on_page == 0:
            break
        if since and all_older:
            print(f"  All topics on page {page} are older than last sync — stopping pagination.")
            break

        page += 1

    print(f"  Total topics found: {len(all_topics)}")
    return all_topics


def fetch_topic_full(
    client: RateLimitedClient,
    base_url: str,
    topic_id: int,
    categories_map: dict,
    use_print: bool,
) -> dict:
    """Fetch a single topic with all its posts.

    When an API key is configured, uses ?print=true to get up to 1000 posts
    per request (the rate limit for print mode is bypassed by admin API keys).
    Falls back to batch fetching for OIDC sessions or topics with >1000 posts.
    """
    url = f"{base_url}/t/{topic_id}.json"
    if use_print:
        url += "?print=true"

    resp = client.get(url)
    data = resp.json()

    # Extract posts already included
    post_stream = data.get("post_stream", {})
    all_post_ids = post_stream.get("stream", [])
    included_posts = post_stream.get("posts", [])
    included_ids = {p["id"] for p in included_posts}

    # Fetch any remaining posts in batches of 20
    # (needed for OIDC sessions, or topics with >1000 posts even with print=true)
    missing_ids = [pid for pid in all_post_ids if pid not in included_ids]
    if missing_ids:
        for i in range(0, len(missing_ids), 20):
            batch = missing_ids[i : i + 20]
            params = "&".join(f"post_ids[]={pid}" for pid in batch)
            batch_resp = client.get(
                f"{base_url}/t/{topic_id}/posts.json?{params}"
            )
            batch_data = batch_resp.json()
            included_posts.extend(batch_data.get("post_stream", {}).get("posts", []))

    # Sort posts by post_number
    included_posts.sort(key=lambda p: p.get("post_number", 0))

    # Build clean output
    category_id = data.get("category_id")
    category_name = categories_map.get(category_id, "Unknown")

    topic_data = {
        "id": data["id"],
        "title": data.get("title", ""),
        "slug": data.get("slug", ""),
        "category_id": category_id,
        "category_name": category_name,
        "created_at": data.get("created_at", ""),
        "last_posted_at": data.get("last_posted_at", ""),
        "bumped_at": data.get("bumped_at", ""),
        "tags": data.get("tags", []),
        "views": data.get("views", 0),
        "like_count": data.get("like_count", 0),
        "posts_count": data.get("posts_count", 0),
        "reply_count": data.get("reply_count", 0),
        "pinned": data.get("pinned_globally", False) or data.get("pinned", False),
        "closed": data.get("closed", False),
        "archived": data.get("archived", False),
        "posts": [
            {
                "id": p["id"],
                "post_number": p.get("post_number"),
                "username": p.get("username", ""),
                "display_name": p.get("display_username", p.get("name", "")),
                "created_at": p.get("created_at", ""),
                "updated_at": p.get("updated_at", ""),
                "cooked": p.get("cooked", ""),
                "plain_text": html_to_text(p.get("cooked", "")),
                "like_count": p.get("like_count", 0),
                "reply_count": p.get("reply_count", 0),
                "reply_to_post_number": p.get("reply_to_post_number"),
                "quote_count": p.get("quote_count", 0),
                "reads": p.get("reads", 0),
            }
            for p in included_posts
        ],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return topic_data


def save_topic(paths: SitePaths, topic_data: dict):
    path = paths.topics_dir / f"{topic_data['id']}.json"
    path.write_text(json.dumps(topic_data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main sync orchestration
# ---------------------------------------------------------------------------

def run_sync(base_url: str, rc: RuntimeConfig, full: bool = False, dry_run: bool = False):
    paths = rc.paths()
    ensure_dirs(paths)
    print(f"Site: {base_url}  (data: {paths.data_dir})")

    # Authenticate
    session = get_session(base_url, rc)
    client = RateLimitedClient(session)
    use_print_mode = bool(rc.discourse_api_key)

    # Fetch categories (always refresh)
    cat_data = fetch_categories(client, base_url, paths)
    categories_map = {c["id"]: c["name"] for c in cat_data["categories"]}

    # Load sync state
    state = load_sync_state(paths)
    last_sync = state.get("last_sync") if not full else None
    sync_start = datetime.now(timezone.utc).isoformat()

    if last_sync and not full:
        print(f"Delta sync — fetching topics updated since {last_sync}")
    else:
        print("Full sync — fetching all topics")

    # Fetch topic list
    all_topics = fetch_all_topic_ids(client, base_url, since=last_sync)

    # Determine which topics need fetching
    if last_sync and not full:
        topics_to_fetch = [
            t for t in all_topics
            if t["bumped_at"] > last_sync or t["last_posted_at"] > last_sync
        ]
        print(f"  {len(topics_to_fetch)} topics updated since last sync.")
    else:
        topics_to_fetch = all_topics

    if dry_run:
        print(f"\n[DRY RUN] Would fetch {len(topics_to_fetch)} topics:")
        for t in topics_to_fetch:
            print(f"  #{t['id']} — {t['title']} ({t['posts_count']} posts)")
        print(f"\nTotal API requests: ~{len(topics_to_fetch)} (plus pagination)")
        print(f"Estimated time at {REQUEST_DELAY}s delay: ~{len(topics_to_fetch) * REQUEST_DELAY:.0f}s")
        return

    # Fetch full content for each topic
    print(f"\nFetching {len(topics_to_fetch)} topics...")
    fetched = 0
    errors = []

    for i, topic_meta in enumerate(topics_to_fetch, 1):
        tid = topic_meta["id"]
        title = topic_meta["title"][:60]
        print(f"  [{i}/{len(topics_to_fetch)}] #{tid} — {title}...", end=" ", flush=True)

        try:
            topic_data = fetch_topic_full(client, base_url, tid, categories_map, use_print_mode)
            save_topic(paths, topic_data)
            fetched += 1
            print(f"OK ({len(topic_data['posts'])} posts)")

            # Update sync state per-topic
            state["synced_topics"][str(tid)] = topic_data.get("bumped_at", sync_start)
        except requests.exceptions.HTTPError as e:
            print(f"ERROR ({e})")
            errors.append({"id": tid, "title": title, "error": str(e)})
        except Exception as e:
            print(f"ERROR ({e})")
            errors.append({"id": tid, "title": title, "error": str(e)})

    # Build/update index from all topic files on disk
    print("\nBuilding topic index...")
    index = []
    for topic_file in sorted(paths.topics_dir.glob("*.json")):
        try:
            td = json.loads(topic_file.read_text())
            index.append({
                "id": td["id"],
                "title": td["title"],
                "category_id": td.get("category_id"),
                "category_name": td.get("category_name", ""),
                "created_at": td.get("created_at", ""),
                "last_posted_at": td.get("last_posted_at", ""),
                "bumped_at": td.get("bumped_at", ""),
                "posts_count": td.get("posts_count", 0),
                "views": td.get("views", 0),
                "like_count": td.get("like_count", 0),
                "tags": td.get("tags", []),
            })
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Skipping %s during index rebuild: %s", topic_file.name, exc)
            continue
    save_index(paths, index)

    # Save sync state
    state["last_sync"] = sync_start
    save_sync_state(paths, state)

    # Summary
    print(f"\n{'=' * 50}")
    print(f"Sync complete!")
    print(f"  Topics fetched:  {fetched}")
    print(f"  Errors:          {len(errors)}")
    print(f"  Total in index:  {len(index)}")
    print(f"  API requests:    {client.request_count}")
    print(f"  Sync timestamp:  {sync_start}")
    print(f"  Data directory:  {paths.data_dir}")

    if errors:
        print(f"\nFailed topics:")
        for e in errors:
            print(f"  #{e['id']} — {e['title']}: {e['error']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape discussions from a Discourse forum."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="Discourse forum URL (e.g. https://discourse.example.com). Can also be set via DISCOURSE_URL in <data-dir>/config/.env.",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output directory for scraped data (created if missing). "
             "Falls back to DISCOURSE_DATA_DIR in the project-root .env.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full re-sync (ignore previous sync state).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be fetched without actually fetching.",
    )
    args = parser.parse_args()

    try:
        rc = bootstrap(args.output)
    except ConfigError as e:
        parser.error(str(e))

    # Resolve URL: CLI arg takes precedence over the config-resolved env value
    base_url = (args.url or rc.discourse_url).rstrip("/")
    if not base_url:
        parser.error("Discourse URL is required. Pass it as an argument or set DISCOURSE_URL in <data-dir>/config/.env.")

    try:
        run_sync(base_url, rc, full=args.full, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\n\nInterrupted. Partial data has been saved.")
        sys.exit(1)
    except (ConfigError, AuthError) as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"\nHTTP error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
