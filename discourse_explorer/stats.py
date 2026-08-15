#!/usr/bin/env python3
"""
DuckDB-powered analytics for scraped Discourse data.

Reads topic JSON files directly — no import step needed.
Provides pre-built queries, keyword search, and an interactive SQL REPL.

Usage:
    uv run discourse-explorer stats --path ./data tags
    uv run discourse-explorer stats --path ./data users
    uv run discourse-explorer stats --path ./data categories
    uv run discourse-explorer stats --path ./data activity
    uv run discourse-explorer stats --path ./data unanswered
    uv run discourse-explorer stats --path ./data search "keyword"
    uv run discourse-explorer stats --path ./data sql
    uv run discourse-explorer stats --path ./data sql "SELECT ..."
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb

from discourse_explorer.config import ConfigError, bootstrap, site_paths_from_dir

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def _sql_quote(s: str) -> str:
    """Single-quote a SQL string literal, escaping embedded quotes."""
    return "'" + s.replace("'", "''") + "'"


def _connect(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB connection with views over the scraped JSON."""
    paths = site_paths_from_dir(data_dir)
    topics_glob = str(paths.topics_dir / "*.json")

    if not paths.topics_dir.exists() or not any(paths.topics_dir.glob("*.json")):
        print(f"Error: No scraped data found at {paths.topics_dir}")
        print("  Run the scraper first.")
        sys.exit(1)

    conn = duckdb.connect(":memory:")

    # Raw topics view (one row per topic, nested posts/tags)
    conn.execute(f"""
        CREATE VIEW topics_raw AS
        SELECT * FROM read_json({_sql_quote(topics_glob)}, auto_detect=true)
    """)

    # ------------------------------------------------------------------
    # Categories table + subcategory→parent lookup
    # ------------------------------------------------------------------
    cat_file = str(paths.categories_file)
    if paths.categories_file.exists():
        conn.execute(f"""
            CREATE TABLE categories AS
            SELECT
                c.id, c.name, c.slug, c.description,
                c.topic_count, c.post_count, c.color,
                c.parent_category_id,
                c.subcategory_ids
            FROM read_json({_sql_quote(cat_file)}) j,
                 LATERAL (SELECT UNNEST(j.categories) AS c)
        """)
        # Invert subcategory_ids → child_id, parent_name
        conn.execute("""
            CREATE TABLE _category_parents AS
            SELECT
                c.id   AS parent_id,
                c.name AS parent_name,
                UNNEST(c.subcategory_ids) AS child_id
            FROM categories c
            WHERE c.subcategory_ids IS NOT NULL
              AND LENGTH(c.subcategory_ids) > 0
        """)
    else:
        conn.execute("CREATE TABLE categories (id BIGINT, name VARCHAR, slug VARCHAR, description VARCHAR, topic_count BIGINT, post_count BIGINT, color VARCHAR, parent_category_id BIGINT, subcategory_ids BIGINT[])")
        conn.execute("CREATE TABLE _category_parents (parent_id BIGINT, parent_name VARCHAR, child_id BIGINT)")

    # ------------------------------------------------------------------
    # Topics view (flat; timestamps cast; category resolved)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE VIEW topics AS
        SELECT
            tr.id, tr.title, tr.slug,
            tr.category_id,
            tr.category_name                          AS category_raw,
            COALESCE(cp.parent_name, tr.category_name) AS category,
            cp.parent_name                             AS parent_category,
            TRY_CAST(tr.created_at    AS TIMESTAMP)    AS created_at,
            TRY_CAST(tr.last_posted_at AS TIMESTAMP)   AS last_posted_at,
            TRY_CAST(tr.bumped_at     AS TIMESTAMP)    AS bumped_at,
            tr.views, tr.like_count, tr.posts_count, tr.reply_count,
            tr.pinned, tr.closed, tr.archived
        FROM topics_raw tr
        LEFT JOIN _category_parents cp ON tr.category_id = cp.child_id
    """)

    # ------------------------------------------------------------------
    # Posts view (flattened: one row per post)
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE VIEW posts AS
        SELECT
            t.id          AS topic_id,
            t.title       AS topic_title,
            t.category_id,
            t.category,
            t.parent_category,
            p.id          AS post_id,
            p.post_number,
            p.username,
            p.display_name,
            TRY_CAST(p.created_at AS TIMESTAMP) AS created_at,
            TRY_CAST(p.updated_at AS TIMESTAMP) AS updated_at,
            p.plain_text,
            p.like_count,
            p.reply_count,
            p.reply_to_post_number,
            p.quote_count,
            p.reads
        FROM topics t
        JOIN topics_raw tr ON tr.id = t.id,
        LATERAL (SELECT UNNEST(tr.posts) AS p)
    """)

    # Tags view (flattened: one row per topic-tag pair).
    #
    # `tags` arrives in several shapes depending on when — and from which
    # Discourse build — the corpus was scraped: `STRUCT(id, name, slug)[]` from
    # current Discourse, a bare `VARCHAR[]` from older scrapes and from the
    # `bitnamilegacy/discourse` image the sample fixture uses, and `JSON[]`
    # whenever a glob spans both (DuckDB infers ONE type for the whole glob).
    #
    # Every shape is normalized through `to_json` rather than branched on the
    # inferred type name. Branching was fragile in three separate ways, all
    # reproduced: a struct without `id` raised `BinderException` at view-creation
    # time and took *every* `stats` subcommand down with it; a null `slug`
    # inferred `slug JSON` and raised `ConversionException`; and a mixed glob
    # inferred `JSON[]`, which contains neither the substring "STRUCT" nor a
    # usable scalar, so it silently fell to the bare-string branch and packed
    # raw JSON text — braces, quotes and all — into the column the graph joins
    # on. `to_json` + `json_extract_string` handles all of them with no probe.
    #
    # `tag_label` is the canonical identity and the column to join on: it
    # matches the graph node names, because `config.tag_label` derives both
    # from the slug. `tag_name` is Discourse's display name, which varies by
    # scrape date for the same tag (U+2024 `2025․06` vs `2025-06`).
    conn.execute("""
        CREATE VIEW topic_tags AS
        SELECT
            topic_id,
            topic_title,
            tag_id,
            tag_name,
            tag_slug,
            -- Slug is the stable identity; fall back to the display name when
            -- it is absent or is Discourse's `<id>-tag` placeholder. Both
            -- inputs arrive already trimmed (see below), so an empty string
            -- here means "nothing usable", exactly as in `config.tag_label`.
            -- NULLIF on the fallback spells that as SQL NULL rather than ''.
            CASE
                WHEN tag_slug <> ''
                     AND NOT regexp_matches(tag_slug, '^[0-9]+-tag$')
                THEN tag_slug
                ELSE NULLIF(tag_name, '')
            END AS tag_label
        FROM (
            SELECT
                t.id    AS topic_id,
                t.title AS topic_title,
                TRY_CAST(json_extract_string(to_json(tag), '$.id') AS BIGINT)
                                                                AS tag_id,
                -- '$' unwraps a scalar tag; '$.name' reads an object's field.
                -- Guarding on json_type keeps a null field inside an object
                -- from falling back to the serialized object itself.
                --
                -- Trimmed here, not in the CASE above, so every downstream
                -- test sees the same string `config.tag_label` does: it
                -- `.strip()`s before testing emptiness and before matching the
                -- placeholder slug, so an untrimmed '   ' passes `<> ''` on
                -- this side while failing on the Python side, and a padded
                -- '  144-tag  ' escapes the placeholder regex here only.
                -- `[[:space:]]` covers tab/newline (which `trim()` does not);
                -- `\p{Z}` covers NBSP and the exotic spaces. Together they are
                -- Python's whitespace set, verified character by character.
                regexp_replace(CASE
                     WHEN json_type(to_json(tag)) = 'OBJECT'
                     THEN json_extract_string(to_json(tag), '$.name')
                     ELSE json_extract_string(to_json(tag), '$')
                END, '^[[:space:]\\p{Z}]+|[[:space:]\\p{Z}]+$', '', 'g')
                                                                AS tag_name,
                regexp_replace(CASE
                     WHEN json_type(to_json(tag)) = 'OBJECT'
                     THEN json_extract_string(to_json(tag), '$.slug')
                     ELSE json_extract_string(to_json(tag), '$')
                END, '^[[:space:]\\p{Z}]+|[[:space:]\\p{Z}]+$', '', 'g')
                                                                AS tag_slug
            FROM topics_raw t, LATERAL (SELECT UNNEST(t.tags) AS tag)
            WHERE t.tags IS NOT NULL AND LENGTH(t.tags) > 0
        )
    """)

    # ------------------------------------------------------------------
    # Convenience view: topic + first poster + comma-separated tags
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE VIEW topic_summary AS
        SELECT
            t.*,
            fp.username                                           AS first_poster,
            COALESCE(STRING_AGG(tt.tag_label, ', '
                                ORDER BY tt.tag_label), '')       AS tags
        FROM topics t
        LEFT JOIN (
            SELECT topic_id, username FROM posts WHERE post_number = 1
        ) fp ON fp.topic_id = t.id
        LEFT JOIN topic_tags tt ON tt.topic_id = t.id
        GROUP BY ALL
    """)

    # ------------------------------------------------------------------
    # Topic participants: one row per (topic, user) with their role
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE VIEW topic_participants AS
        SELECT
            p.topic_id,
            p.topic_title,
            p.category,
            p.username,
            CASE
                WHEN p.post_number = 1             THEN 'creator'
                WHEN p.username = 'system'         THEN 'system'
                ELSE 'responder'
            END                                    AS role,
            COUNT(*)                               AS posts_in_topic,
            MIN(p.created_at)                      AS first_post_at,
            MAX(p.created_at)                      AS last_post_at,
            SUM(p.like_count)                      AS likes_received
        FROM posts p
        GROUP BY p.topic_id, p.topic_title, p.category,
                 p.username, role
    """)

    # ------------------------------------------------------------------
    # Topic threads: per-topic response metrics
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE VIEW topic_threads AS
        SELECT
            t.id                              AS topic_id,
            t.title,
            t.category,
            t.created_at,
            t.views,
            t.posts_count,
            creator.username                  AS creator,
            first_resp.username               AS first_responder,
            first_resp.responded_at           AS first_response_at,
            first_resp.responded_at - t.created_at AS response_time,
            last_resp.username                AS last_responder,
            last_resp.responded_at            AS last_response_at,
            resp_count.responders             AS unique_responders
        FROM topics t
        LEFT JOIN (
            SELECT topic_id, username
            FROM posts WHERE post_number = 1
        ) creator ON creator.topic_id = t.id
        LEFT JOIN (
            SELECT topic_id,
                   username,
                   created_at AS responded_at
            FROM posts
            WHERE post_number = (
                SELECT MIN(p2.post_number) FROM posts p2
                WHERE p2.topic_id = posts.topic_id
                  AND p2.post_number > 1
                  AND p2.username != (
                      SELECT p3.username FROM posts p3
                      WHERE p3.topic_id = posts.topic_id AND p3.post_number = 1
                  )
                  AND p2.username != 'system'
            )
        ) first_resp ON first_resp.topic_id = t.id
        LEFT JOIN (
            SELECT DISTINCT ON (topic_id)
                   topic_id, username, created_at AS responded_at
            FROM posts
            WHERE username != 'system'
              AND post_number > 1
              AND username != (
                  SELECT p3.username FROM posts p3
                  WHERE p3.topic_id = posts.topic_id AND p3.post_number = 1
              )
            ORDER BY topic_id, created_at DESC
        ) last_resp ON last_resp.topic_id = t.id
        LEFT JOIN (
            SELECT topic_id,
                   COUNT(DISTINCT username) AS responders
            FROM posts
            WHERE post_number > 1
              AND username != 'system'
              AND username != (
                  SELECT p3.username FROM posts p3
                  WHERE p3.topic_id = posts.topic_id AND p3.post_number = 1
              )
            GROUP BY topic_id
        ) resp_count ON resp_count.topic_id = t.id
    """)

    # ------------------------------------------------------------------
    # User activity: aggregated per user across all topics
    # ------------------------------------------------------------------
    conn.execute("""
        CREATE VIEW user_activity AS
        SELECT
            username,
            COUNT(*)                                                       AS total_posts,
            COUNT(DISTINCT topic_id)                                       AS topics_participated,
            SUM(CASE WHEN post_number = 1 THEN 1 ELSE 0 END)              AS topics_created,
            SUM(CASE WHEN post_number > 1
                      AND username != 'system' THEN 1 ELSE 0 END)         AS replies_written,
            COUNT(DISTINCT CASE WHEN post_number > 1
                                AND username != 'system'
                           THEN topic_id END)                              AS topics_helped,
            SUM(like_count)                                                AS likes_received,
            SUM(reads)                                                     AS total_reads,
            MIN(created_at)                                                AS first_seen,
            MAX(created_at)                                                AS last_seen
        FROM posts
        WHERE username != 'system'
        GROUP BY username
    """)

    return conn


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_table(result, max_col_width: int = 60):
    """Print a DuckDB result as a formatted text table."""
    if result is None:
        return

    columns = [desc[0] for desc in result.description]
    rows = result.fetchall()

    if not rows:
        print("(no results)")
        return

    # Format values
    def fmt(val):
        if val is None:
            return ""
        s = str(val)
        if len(s) > max_col_width:
            s = s[:max_col_width - 3] + "..."
        return s

    str_rows = [[fmt(v) for v in row] for row in rows]

    # Column widths
    widths = [len(c) for c in columns]
    for row in str_rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    # Print header
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    print(header)
    print("-+-".join("-" * w for w in widths))

    # Print rows
    for row in str_rows:
        print(" | ".join(val.ljust(widths[i]) for i, val in enumerate(row)))

    print(f"\n({len(rows)} rows)")


def _print_json(result):
    """Print a DuckDB result as JSON."""
    if result is None:
        return
    columns = [desc[0] for desc in result.description]
    rows = result.fetchall()
    data = [dict(zip(columns, row)) for row in rows]
    print(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Pre-built queries
# ---------------------------------------------------------------------------

def cmd_tags(conn, limit, as_json):
    """Tag distribution.

    Grouped on `tag_label`, not `tag_name`: Discourse's display name varies by
    scrape date, so grouping by name reports one release as two tags (e.g.
    `2023․06` with U+2024 alongside `2023-06`). The label matches the graph.
    """
    result = conn.execute(f"""
        SELECT tag_label AS tag, COUNT(*) AS topics
        FROM topic_tags
        GROUP BY tag_label
        ORDER BY topics DESC
        LIMIT {limit}
    """)
    (_print_json if as_json else _print_table)(result)


def cmd_users(conn, limit, as_json):
    """Top contributors by post count."""
    result = conn.execute(f"""
        SELECT
            username,
            COUNT(*) AS posts,
            SUM(like_count) AS likes,
            SUM(reads) AS total_reads,
            COUNT(DISTINCT topic_id) AS topics
        FROM posts
        GROUP BY username
        ORDER BY posts DESC
        LIMIT {limit}
    """)
    (_print_json if as_json else _print_table)(result)


def cmd_categories(conn, limit, as_json):
    """Category breakdown."""
    result = conn.execute(f"""
        SELECT
            category,
            COUNT(*) AS topics,
            SUM(views) AS total_views,
            SUM(like_count) AS total_likes,
            ROUND(AVG(posts_count), 1) AS avg_posts
        FROM topics
        GROUP BY category
        ORDER BY topics DESC
        LIMIT {limit}
    """)
    (_print_json if as_json else _print_table)(result)


def cmd_activity(conn, limit, as_json):
    """Post activity per month."""
    result = conn.execute(f"""
        SELECT
            strftime(created_at, '%Y-%m') AS month,
            COUNT(*) AS posts,
            COUNT(DISTINCT username) AS authors,
            COUNT(DISTINCT topic_id) AS topics
        FROM posts
        GROUP BY month
        ORDER BY month
        LIMIT {limit}
    """)
    (_print_json if as_json else _print_table)(result)


def cmd_unanswered(conn, limit, as_json):
    """Topics with no replies (only the original post)."""
    result = conn.execute(f"""
        SELECT
            id, title, category,
            created_at::DATE AS created,
            views
        FROM topics
        WHERE posts_count <= 1 AND NOT closed AND NOT archived
        ORDER BY created_at DESC
        LIMIT {limit}
    """)
    (_print_json if as_json else _print_table)(result)


def cmd_search(conn, query: str, limit: int, as_json: bool):
    """Full-text keyword search across post content."""
    # Use ILIKE for simplicity — works without FTS extension setup
    pattern = f"%{query}%"
    lower_query = query.lower()
    result = conn.execute(f"""
        SELECT
            topic_id,
            topic_title,
            username,
            created_at::DATE AS date,
            CASE
                WHEN LENGTH(plain_text) > 150
                THEN SUBSTRING(plain_text, GREATEST(1, POSITION(? IN LOWER(plain_text)) - 60), 150) || '...'
                ELSE plain_text
            END AS snippet
        FROM posts
        WHERE plain_text ILIKE ?
        ORDER BY created_at DESC
        LIMIT {limit}
    """, [lower_query, pattern])
    (_print_json if as_json else _print_table)(result)


# ---------------------------------------------------------------------------
# Interactive SQL REPL
# ---------------------------------------------------------------------------

def cmd_sql(conn, query: str | None, as_json: bool):
    """Execute SQL or start an interactive REPL."""
    if query:
        # One-shot query
        try:
            result = conn.execute(query)
            (_print_json if as_json else _print_table)(result)
        except duckdb.Error as e:
            print(f"Error: {e}")
            sys.exit(1)
        return

    # Interactive REPL
    print("DuckDB SQL REPL — views: topics, posts, topic_tags, topic_summary,")
    print("  categories, topic_participants, topic_threads, user_activity")
    print("Type .tables or .schema for metadata. Ctrl+D to exit.\n")

    while True:
        try:
            line = input("duckdb> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not line:
            continue

        if line.lower() == ".tables":
            result = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'")
            _print_table(result)
            continue

        if line.lower() == ".schema":
            for view_name in ("topics", "posts", "topic_tags"):
                print(f"\n-- {view_name}")
                result = conn.execute(f"DESCRIBE {view_name}")
                _print_table(result)
            continue

        if line.lower() in (".quit", ".exit"):
            break

        try:
            result = conn.execute(line)
            _print_table(result)
        except duckdb.Error as e:
            print(f"Error: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analytics for scraped Discourse data (powered by DuckDB)."
    )
    parser.add_argument(
        "--path", "-p", type=Path, default=None,
        help="Path to scraped data directory. Falls back to DISCOURSE_DATA_DIR in the project-root .env.",
    )

    sub = parser.add_subparsers(dest="command")

    for name, hlp in [
        ("tags", "Tag distribution"),
        ("users", "Top contributors"),
        ("categories", "Category breakdown"),
        ("activity", "Post activity per month"),
        ("unanswered", "Topics with no replies"),
    ]:
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--limit", type=int, default=30, help="Max rows (default: 30)")
        p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    search_p = sub.add_parser("search", help="Keyword search across posts")
    search_p.add_argument("query", help="Search term")
    search_p.add_argument("--limit", type=int, default=30, help="Max rows (default: 30)")
    search_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    sql_p = sub.add_parser("sql", help="Run SQL or start interactive REPL")
    sql_p.add_argument("query", nargs="?", default=None, help="SQL query (omit for REPL)")
    sql_p.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    try:
        rc = bootstrap(args.path)
    except ConfigError as e:
        parser.error(str(e))

    if not args.command:
        parser.print_help()
        sys.exit(1)

    conn = _connect(rc.data_dir)

    limit = getattr(args, "limit", 30)
    as_json = getattr(args, "as_json", False)

    if args.command == "tags":
        cmd_tags(conn, limit, as_json)
    elif args.command == "users":
        cmd_users(conn, limit, as_json)
    elif args.command == "categories":
        cmd_categories(conn, limit, as_json)
    elif args.command == "activity":
        cmd_activity(conn, limit, as_json)
    elif args.command == "unanswered":
        cmd_unanswered(conn, limit, as_json)
    elif args.command == "search":
        cmd_search(conn, args.query, limit, as_json)
    elif args.command == "sql":
        cmd_sql(conn, args.query, as_json)


if __name__ == "__main__":
    main()
