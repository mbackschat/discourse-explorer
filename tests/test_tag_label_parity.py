"""`config.tag_label` and the `topic_tags.tag_label` SQL column must agree.

Tag identity is implemented twice, once in Python (graph node names, guide
version counts) and once in SQL (DuckDB views). CLAUDE.md requires all three
consumers to "agree on one vocabulary", but nothing enforced it, and the two
implementations drifted: SQL never trimmed, and Python turned a JSON `null`
tag into the literal label `"None"`.

A divergence here is quiet and expensive. It does not raise; it makes
`stats sql` disagree with the graph about which tags exist, which is exactly
the class of confusion that cost this project a re-index.

This is a differential test: same inputs, both implementations, assert equal.
It is the guard that lets the duplication stay.
"""
import json
import tempfile
import unittest
from pathlib import Path

from discourse_explorer.config import tag_display, tag_label
from discourse_explorer.stats import _connect

# (topic id, tag value as it appears in the scraped JSON, what it exercises).
# Spread over several files on purpose: a mixed-shape glob is what makes
# read_json infer JSON[] rather than a struct, and the view has to survive it.
TAG_CASES: list[tuple[int, object, str]] = [
    (1, "kernel", "legacy bare string"),
    (2, {"id": 144, "slug": "2025-06", "name": "2025․06"}, "slug wins over U+2024 name"),
    (3, {"id": 7, "slug": "144-tag", "name": "2025"}, "placeholder slug falls back to name"),
    (4, {"id": 8, "slug": "", "name": "widgets"}, "empty slug falls back to name"),
    (5, {"id": 9, "slug": None, "name": "nullslug"}, "null slug falls back to name"),
    (6, {"id": 10, "slug": "  spaced  ", "name": "Spaced"}, "slug needs trimming"),
    (7, {"id": 11, "slug": "   ", "name": "wsonly"}, "whitespace-only slug is empty"),
    (8, {"id": 12, "slug": "  144-tag  ", "name": "  padded  "}, "trim before every test"),
    (9, None, "JSON null tag has no label"),
    (10, "  padded-string  ", "bare string needs trimming"),
    # DuckDB's trim() strips spaces and NBSP but not tab/newline; the POSIX
    # [[:space:]] class strips tab/newline but not NBSP. Neither alone equals
    # str.strip(), so both classes are covered here to keep the view honest.
    (11, {"id": 13, "slug": "\ttabbed\t", "name": "Tabbed"}, "tab padding"),
    (12, {"id": 14, "slug": "\xa0nbsp\xa0", "name": "Nbsp"}, "NBSP padding"),
    (13, {"id": 15, "slug": "\xa0\tmixed\t\xa0", "name": "Mixed"},
     "NBSP and tab interleaved, which one pass of either class alone misses"),
]


def _topic(topic_id: int, tag) -> dict:
    """A topic complete enough for every view `_connect` builds.

    The views are created eagerly and reference these fields by name, so a
    sparse fixture fails at CREATE VIEW time with a BinderException rather
    than at query time. One real post, because `posts` UNNESTs the array and
    an all-empty column gives DuckDB no element type to bind against.
    """
    return {
        "id": topic_id,
        "title": f"topic {topic_id}",
        "slug": f"topic-{topic_id}",
        "category_id": 1,
        "category_name": "Kernel",
        "created_at": "2026-01-01T00:00:00.000Z",
        "last_posted_at": "2026-01-02T00:00:00.000Z",
        "bumped_at": "2026-01-02T00:00:00.000Z",
        "views": 1, "like_count": 0, "posts_count": 1, "reply_count": 0,
        "pinned": False, "closed": False, "archived": False,
        "tags": [tag],
        "posts": [{
            "id": topic_id * 100,
            "post_number": 1,
            "username": "alice",
            "display_name": "Alice",
            "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:00:00.000Z",
            "plain_text": "body",
            "like_count": 0, "reply_count": 0,
            "reply_to_post_number": None,
            "quote_count": 0, "reads": 1,
        }],
    }


def _expected(tag) -> str | None:
    """Python's answer, normalized to SQL's spelling of "no label".

    `tag_label` returns `""` for "nothing usable here"; SQL spells that NULL.
    Only the emptiness is compared, not which sentinel each side picked.
    """
    return tag_label(tag) or None


class TagLabelParityTests(unittest.TestCase):
    def test_sql_view_matches_config_tag_label_for_every_tag_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            topics = Path(tmp) / "topics"
            topics.mkdir()
            for topic_id, tag, _ in TAG_CASES:
                (topics / f"{topic_id}.json").write_text(
                    json.dumps(_topic(topic_id, tag)))

            conn = _connect(Path(tmp))
            try:
                rows = dict(conn.execute(
                    "SELECT topic_id, tag_label FROM topic_tags").fetchall())
            finally:
                conn.close()

        self.assertEqual(len(rows), len(TAG_CASES), "a tag shape vanished from the view")
        for topic_id, tag, what in TAG_CASES:
            with self.subTest(case=what):
                self.assertEqual(rows[topic_id], _expected(tag))

    def test_python_never_labels_a_null_tag(self):
        """`str(None).strip()` is `"None"`, which would become a real node."""
        self.assertEqual(tag_label(None), "")

    def test_null_tag_never_reaches_the_hashed_document_header(self):
        """`tag_display` feeds the text LightRAG hashes for doc-level dedupe.

        A literal "None" there is worse than a bad stats row: it is baked into
        a document id, and correcting it later re-keys that document into a
        full re-extraction (CLAUDE.md, rule 3).
        """
        self.assertEqual(tag_display(None), "")


if __name__ == "__main__":
    unittest.main()
