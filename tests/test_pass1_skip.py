"""Pass 1 skips topics whose structural payload is byte-identical to last run.

Pass 1 re-seeds Category/Topic/Tag/User nodes for *every* topic on *every* run,
with no dedupe. On a `--clear` build that is cheap (~63 topics/min on an empty
graph). On a `--resume` — the common case — each insert merges against the full
existing graph and each Faiss upsert of a present id takes the remove-then-re-add
path, measured at 10.6 topics/min on a 15,756-node graph: ~2 hours of almost
entirely redundant work to add 69 new topics.

The skip key is a hash of the payload `_topic_to_custom_kg` **produces**, not of
the topic file. That distinction is the whole design:

  * topic edited            -> payload differs -> not skipped
  * KG-building code changed -> payload differs -> not skipped, corpus-wide

So a change like the 2026-08-14 tag-slug fix, which must propagate to every
topic, invalidates the cache automatically instead of being silently suppressed.
An earlier version of this idea was rejected precisely because a file-keyed hash
would have suppressed it.

Run via:
    uv run python -m unittest tests.test_pass1_skip
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.query import (  # noqa: E402
    PASS1_HASH_FILE,
    Pass1Action,
    _load_pass1_hashes,
    _pass1_payload_hash,
    _pass1_plan,
    _save_pass1_hashes,
    _topic_to_custom_kg,
)


def _topic(tid=1, title="A topic", tags=None):
    return {
        "id": tid,
        "title": title,
        "category_name": "Data Services",
        "created_at": "2026-06-01T00:00:00Z",
        "tags": tags if tags is not None else [{"id": 1, "name": "how-to", "slug": "how-to"}],
        "posts": [{"username": "alice", "plain_text": "Body.", "post_number": 1}],
    }


class PayloadHashTests(unittest.TestCase):
    def test_identical_payloads_hash_identically(self):
        a = _pass1_payload_hash(_topic_to_custom_kg(_topic()))
        b = _pass1_payload_hash(_topic_to_custom_kg(_topic()))
        self.assertEqual(a, b)

    def test_edited_topic_changes_the_hash(self):
        a = _pass1_payload_hash(_topic_to_custom_kg(_topic(title="A topic")))
        b = _pass1_payload_hash(_topic_to_custom_kg(_topic(title="Edited title")))
        self.assertNotEqual(a, b)

    def test_changed_tag_identity_changes_the_hash(self):
        """The property that makes this safe: a change in how nodes are built
        invalidates every affected topic, so corpus-wide fixes still propagate."""
        a = _pass1_payload_hash(_topic_to_custom_kg(
            _topic(tags=[{"id": 144, "name": "2025․06", "slug": "2025-06"}])))
        b = _pass1_payload_hash(_topic_to_custom_kg(
            _topic(tags=[{"id": 144, "name": "2025․06", "slug": "2025-06-CHANGED"}])))
        self.assertNotEqual(a, b)

    def test_hash_is_order_stable(self):
        """dict ordering must not make an unchanged topic look changed."""
        p = _topic_to_custom_kg(_topic())
        reordered = {k: p[k] for k in reversed(list(p))}
        self.assertEqual(_pass1_payload_hash(p), _pass1_payload_hash(reordered))


class HashStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "graphrag"
        self.dir.mkdir(parents=True)

    def test_roundtrip(self):
        ledger = {"1": {"hash": "abc", "docs": ["topic-1"]},
                  "2": {"hash": "def", "docs": ["topic-2", "doc-ff"]}}
        _save_pass1_hashes(self.dir, ledger)
        self.assertEqual(ledger, _load_pass1_hashes(self.dir))

    def test_absent_store_yields_empty_so_nothing_is_skipped(self):
        self.assertEqual({}, _load_pass1_hashes(self.dir))

    def test_corrupt_store_yields_empty_rather_than_raising(self):
        """A truncated hash file must degrade to 'redo everything', never to a
        crash or to skipping work that was not actually done."""
        (self.dir / PASS1_HASH_FILE).write_text("{not json")
        self.assertEqual({}, _load_pass1_hashes(self.dir))

    def test_store_lives_in_graph_dir_so_clear_resets_it(self):
        _save_pass1_hashes(self.dir, {"1": "abc"})
        self.assertTrue((self.dir / PASS1_HASH_FILE).exists())


class SkipDecisionTests(unittest.TestCase):
    """The decision Pass 1 makes per topic, expressed end to end."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "graphrag"
        self.dir.mkdir(parents=True)

    def _skips(self, topics, known):
        """Ask the production decision function, never a copy of it.

        This used to re-implement the comparison inline, which meant it could
        keep passing while the real loop diverged.
        """
        out = []
        for t in topics:
            h = _pass1_payload_hash(_topic_to_custom_kg(t))
            action, _ = _pass1_plan(known.get(str(t["id"])), h)
            out.append(action is Pass1Action.SKIP)
        return out

    def test_unchanged_topics_skip_changed_ones_do_not(self):
        t1, t2 = _topic(1), _topic(2)
        known = {
            "1": _pass1_payload_hash(_topic_to_custom_kg(t1)),
            "2": _pass1_payload_hash(_topic_to_custom_kg(t2)),
        }
        _save_pass1_hashes(self.dir, known)
        loaded = _load_pass1_hashes(self.dir)
        edited = _topic(2, title="Now edited")
        self.assertEqual([True, False], self._skips([t1, edited], loaded))

    def test_brand_new_topic_is_never_skipped(self):
        _save_pass1_hashes(self.dir, {"1": _pass1_payload_hash(_topic_to_custom_kg(_topic(1)))})
        loaded = _load_pass1_hashes(self.dir)
        self.assertEqual([False], self._skips([_topic(99)], loaded))


if __name__ == "__main__":
    unittest.main()
