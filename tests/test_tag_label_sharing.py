"""One tag identity across all three consumers of scraped tags.

A Discourse tag's `name` is not stable across scrapes -- verified on the production
corpus 2026-08-14, tag id=144 (slug `2025-06`) is named `2025․06` with U+2024
ONE DOT LEADER in April-fetched topics and `2025-06` in August-fetched ones.
The `slug` is stable, so `config.tag_label` derives identity from it.

Three modules read tags and all three must agree, or the graph, the stats views
and QUERY-GUIDE.md describe different vocabularies:

  - `query.py`              -> graph node names + the LLM-visible doc header
  - `derive_query_guide.py` -> the version list in QUERY-GUIDE.md
  - `stats.py`              -> the `topic_tags` / `topic_summary` DuckDB views

Run via:
    uv run python -m unittest tests.test_tag_label_sharing
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.config import tag_label  # noqa: E402
from discourse_explorer.derive_query_guide import scan_topics  # noqa: E402
from discourse_explorer.stats import _connect  # noqa: E402

DOT_LEADER = "․"


def _write_corpus(tmp: Path, topics):
    (tmp / "topics").mkdir(parents=True, exist_ok=True)
    for t in topics:
        (tmp / "topics" / f"{t['id']}.json").write_text(json.dumps(t))
    return tmp


def _topic(tid, tags, title=None, category="Data Services"):
    return {
        "id": tid,
        "title": title or f"Topic {tid}",
        "slug": f"topic-{tid}",
        "category_id": 3,
        "category_name": category,
        "created_at": "2026-06-01T00:00:00.000Z",
        "last_posted_at": "2026-06-02T00:00:00.000Z",
        "bumped_at": "2026-06-02T00:00:00.000Z",
        "tags": tags,
        "views": 10, "like_count": 0, "posts_count": 1, "reply_count": 0,
        "pinned": False, "closed": False, "archived": False,
        "posts": [{
            "id": tid * 10, "post_number": 1, "username": "alice",
            "display_name": "Alice", "created_at": "2026-06-01T00:00:00.000Z",
            "updated_at": "2026-06-01T00:00:00.000Z", "cooked": "<p>hi</p>",
            "plain_text": "hi", "like_count": 0, "reply_count": 0,
            "reply_to_post_number": None, "quote_count": 0, "reads": 1,
        }],
        "fetched_at": "2026-08-14T00:00:00.000Z",
    }


# The same Discourse tag as serialized by two different scrapes.
APRIL = {"id": 144, "name": f"2025{DOT_LEADER}06", "slug": "2025-06"}
AUGUST = {"id": 144, "name": "2025-06", "slug": "2025-06"}


class SharedHelperTests(unittest.TestCase):
    def test_both_scrape_eras_yield_one_label(self):
        self.assertEqual(tag_label(APRIL), tag_label(AUGUST))
        self.assertEqual("2025-06", tag_label(APRIL))

    def test_placeholder_slug_falls_back_to_name(self):
        self.assertEqual("202506",
                         tag_label({"id": 169, "name": "202506", "slug": "169-tag"}))

    def test_legacy_string_tag(self):
        self.assertEqual("how-to", tag_label("how-to"))


class QueryGuideVersionTests(unittest.TestCase):
    """`scan_topics` feeds QUERY-GUIDE.md's version coverage table."""

    def _versions(self, tags_per_topic):
        tmp = Path(tempfile.mkdtemp())
        _write_corpus(tmp, [_topic(i + 1, tags)
                            for i, tags in enumerate(tags_per_topic)])
        return dict(scan_topics(tmp / "topics").by_version)

    def test_hyphen_named_version_tags_are_detected(self):
        """The version regex previously accepted only period / U+2024 forms, so
        once labels became slugs it matched nothing and the guide's version
        section came out empty."""
        self.assertEqual({"2025-06": 1}, self._versions([[AUGUST]]))

    def test_both_eras_collapse_into_one_version_row(self):
        versions = self._versions([[APRIL], [AUGUST], [APRIL]])
        self.assertEqual({"2025-06": 3}, versions,
                         "one release must not appear as two versions")

    def test_non_version_tags_are_excluded(self):
        self.assertEqual({}, self._versions([[{"id": 7, "name": "how-to",
                                               "slug": "how-to"}]]))


class StatsTagViewTests(unittest.TestCase):
    """`topic_tags` must expose real id/name/slug, not an unnested struct."""

    def _conn(self, tags_per_topic):
        tmp = Path(tempfile.mkdtemp())
        _write_corpus(tmp, [_topic(i + 1, tags)
                            for i, tags in enumerate(tags_per_topic)])
        return _connect(tmp)

    def test_tag_columns_are_scalars_not_structs(self):
        conn = self._conn([[APRIL]])
        row = conn.execute(
            "SELECT tag_id, tag_name, tag_slug FROM topic_tags").fetchone()
        self.assertEqual(144, row[0])
        self.assertEqual(f"2025{DOT_LEADER}06", row[1])
        self.assertEqual("2025-06", row[2])

    def test_both_eras_group_as_one_slug(self):
        conn = self._conn([[APRIL], [AUGUST]])
        rows = conn.execute(
            "SELECT tag_slug, COUNT(*) FROM topic_tags GROUP BY 1").fetchall()
        self.assertEqual([("2025-06", 2)], rows)

    def test_topic_summary_tags_uses_the_graph_consistent_label(self):
        conn = self._conn([[APRIL]])
        tags = conn.execute(
            "SELECT tags FROM topic_summary WHERE id = 1").fetchone()[0]
        self.assertEqual("2025-06", tags)
        self.assertNotIn(DOT_LEADER, tags)

    def test_legacy_string_tags_still_work(self):
        """Older scrapes stored bare strings; DuckDB infers VARCHAR[] then."""
        conn = self._conn([["how-to", "search"]])
        rows = conn.execute(
            "SELECT tag_name, tag_slug FROM topic_tags ORDER BY 1").fetchall()
        self.assertEqual([("how-to", "how-to"), ("search", "search")], rows)

    def test_topic_with_no_tags_is_not_dropped_from_summary(self):
        conn = self._conn([[]])
        self.assertEqual(1, conn.execute(
            "SELECT COUNT(*) FROM topic_summary").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
