"""Tests for tag-label normalization in the Pass 1 KG builder + document text.

Discourse tag *names* carry U+2024 ONE DOT LEADER where a period belongs
(periods aren't legal in tag names), but tags created later use a plain
hyphen instead. The same release therefore reaches us under two names --
`2025․06` and `2025-06` -- while the `slug` is consistently `2025-06`.

Building graph nodes from `name` splits one release across two entities, which
Pass 4 canonicalization cannot merge (casefolding doesn't map U+2024 to `-`).
These tests pin the invariant: **one tag identity per release, taken from the
slug**, in both places tags are rendered.

Run via:
    uv run python -m unittest tests.test_topic_tag_labels
"""

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.config import STRUCTURAL_REL_KEYWORDS  # noqa: E402
from discourse_explorer.query import (  # noqa: E402
    _topic_to_custom_kg,
    topic_to_document,
)

# U+2024 ONE DOT LEADER, as it appears in real scraped tag names.
DOT_LEADER = "․"


def _topic(tags, tid=1, title="A topic", posts=None):
    return {
        "id": tid,
        "title": title,
        "category_name": "Data Services",
        "created_at": "2026-06-01T00:00:00Z",
        "tags": tags,
        "posts": posts if posts is not None else [
            {"username": "alice", "plain_text": "Some body text.", "post_number": 1},
        ],
    }


def _tag_entity_names(payload):
    return [e["entity_name"] for e in payload["entities"]
            if e["entity_type"] == "tag"]


class TagLabelFromSlugTests(unittest.TestCase):
    """Pass 1 tag entities must be keyed on the slug, not the display name."""

    def test_dot_leader_name_yields_slug_keyed_entity(self):
        payload = _topic_to_custom_kg(_topic(
            [{"id": 148, "name": f"2025{DOT_LEADER}06", "slug": "2025-06"}]
        ))
        self.assertEqual(["2025-06"], _tag_entity_names(payload))

    def test_dot_leader_and_hyphen_variants_collapse_to_one_identity(self):
        """The whole point: both spellings of one release land on one node."""
        dotted = _topic_to_custom_kg(_topic(
            [{"id": 148, "name": f"2025{DOT_LEADER}06", "slug": "2025-06"}], tid=1))
        hyphen = _topic_to_custom_kg(_topic(
            [{"id": 144, "name": "2025-06", "slug": "2025-06"}], tid=2))
        self.assertEqual(_tag_entity_names(dotted), _tag_entity_names(hyphen))

    def test_relationship_endpoint_uses_the_same_label(self):
        """A node keyed on slug with an edge keyed on name would orphan the edge."""
        payload = _topic_to_custom_kg(_topic(
            [{"id": 148, "name": f"2025{DOT_LEADER}06", "slug": "2025-06"}]))
        tag_edges = [r for r in payload["relationships"]
                     if r["keywords"] == STRUCTURAL_REL_KEYWORDS["topic_tagged"]]
        self.assertEqual(["2025-06"], [r["tgt_id"] for r in tag_edges])

    def test_placeholder_slug_falls_back_to_the_display_name(self):
        """Discourse mints `<id>-tag` when a name won't slugify (all-numeric
        names collide with tag IDs). That token has no merge potential, so it
        must never become the entity name."""
        payload = _topic_to_custom_kg(_topic(
            [{"id": 169, "name": "202506", "slug": "169-tag"}]))
        self.assertEqual(["202506"], _tag_entity_names(payload))

    def test_placeholder_slug_kept_out_of_the_document_header(self):
        doc = topic_to_document(_topic(
            [{"id": 169, "name": "202506", "slug": "169-tag"}]))
        self.assertIn("(tags: 202506)", doc)
        self.assertNotIn("169-tag", doc)

    def test_header_falls_back_to_slug_when_name_is_absent(self):
        doc = topic_to_document(_topic([{"id": 1, "slug": "kernel"}]))
        self.assertIn("(tags: kernel)", doc)

    def test_labels_are_stripped(self):
        payload = _topic_to_custom_kg(_topic([{"id": 1, "slug": "  kernel  "}]))
        self.assertEqual(["kernel"], _tag_entity_names(payload))

    def test_non_string_slug_does_not_break_the_document_join(self):
        """A non-str slug used to propagate into `", ".join(...)` and raise
        TypeError, which the retry loop would burn 3 attempts on before
        dropping the topic from the graph entirely."""
        doc = topic_to_document(_topic([{"id": 1, "slug": 202506}]))
        self.assertIn("(tags: 202506)", doc)  # coerced, not a TypeError

    def test_falls_back_to_name_when_slug_absent(self):
        payload = _topic_to_custom_kg(_topic([{"id": 7, "name": "how-to"}]))
        self.assertEqual(["how-to"], _tag_entity_names(payload))

    def test_legacy_plain_string_tags_still_supported(self):
        payload = _topic_to_custom_kg(_topic(["how-to", "search"]))
        self.assertEqual(["how-to", "search"], _tag_entity_names(payload))

    def test_blank_and_slugless_blank_tags_are_skipped(self):
        payload = _topic_to_custom_kg(_topic(
            [{"id": 1, "name": "", "slug": ""}, "", {"id": 2, "slug": "kernel"}]))
        self.assertEqual(["kernel"], _tag_entity_names(payload))


class DocumentHeaderTagTests(unittest.TestCase):
    """Pass 2's LLM reads the document header; it must see the same label."""

    def test_header_keeps_the_display_name_not_the_slug(self):
        """The header is part of the text LightRAG hashes for doc dedupe.
        Normalizing it to the slug rewrote 1018 of 1399 doc ids and turned an
        85-document incremental into a 1099-document re-extraction (~13x cost,
        measured 2026-08-14). Tag identity is normalized in the graph nodes
        instead, which are not part of the hashed text."""
        doc = topic_to_document(_topic(
            [{"id": 144, "name": f"2025{DOT_LEADER}06", "slug": "2025-06"}]))
        self.assertIn(f"(tags: 2025{DOT_LEADER}06)", doc)

    def test_header_still_renders_legacy_string_tags(self):
        doc = topic_to_document(_topic(["how-to"]))
        self.assertIn("(tags: how-to)", doc)


if __name__ == "__main__":
    unittest.main()
