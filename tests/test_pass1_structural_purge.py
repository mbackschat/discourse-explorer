"""A changed topic must lose the structural claims it no longer makes.

`adelete_by_doc_id` cannot do this. It finds what to remove via
`full_entities.get_by_id(doc_id)`, and `ainsert_custom_kg` never registers its
entities there — after an index run `full_entities` holds only `doc-` keys, so
`adelete_by_doc_id("topic-42")` deletes the chunk, the document and the
doc_status row and touches the graph not at all. A renamed tag therefore kept
its node, still asserting `Topic tagged with <old>` about a topic that no
longer says so, while the chunk it cited no longer existed.

So the structural half is purged explicitly, from what the ledger recorded:

  - delete the relations the new payload no longer contains — these are
    topic-scoped, so deleting them is exact
  - then drop any endpoint left with no edges at all, but only if it is a
    structural type. A tag or user is shared across topics; it may only go
    when nothing references it, and a content entity is never ours to remove.
"""
import unittest

from discourse_explorer.query import (
    _pass1_rel_pairs,
    _stale_structural_relations,
)

PAYLOAD = {
    "chunks": [{"source_id": "topic-42"}],
    "entities": [
        {"entity_name": "kernel", "entity_type": "tag"},
        {"entity_name": "alice", "entity_type": "user"},
    ],
    "relationships": [
        {"src_id": "A topic", "tgt_id": "kernel"},
        {"src_id": "alice", "tgt_id": "A topic"},
    ],
}


class RelPairTests(unittest.TestCase):
    def test_collects_every_relationship_as_an_ordered_pair(self):
        self.assertEqual(
            _pass1_rel_pairs(PAYLOAD),
            [["A topic", "kernel"], ["alice", "A topic"]])

    def test_is_json_round_trippable(self):
        """The ledger is JSON, so pairs must be lists, not tuples."""
        import json

        pairs = _pass1_rel_pairs(PAYLOAD)
        self.assertEqual(json.loads(json.dumps(pairs)), pairs)

    def test_deduplicates(self):
        payload = {"relationships": [
            {"src_id": "a", "tgt_id": "b"},
            {"src_id": "a", "tgt_id": "b"},
        ]}
        self.assertEqual(_pass1_rel_pairs(payload), [["a", "b"]])

    def test_skips_a_relationship_missing_an_endpoint(self):
        payload = {"relationships": [{"src_id": "a"}, {"src_id": "a", "tgt_id": "b"}]}
        self.assertEqual(_pass1_rel_pairs(payload), [["a", "b"]])


class StaleRelationTests(unittest.TestCase):
    def test_a_renamed_tag_yields_exactly_the_dropped_edge(self):
        prior = {"rels": [["A topic", "music"], ["alice", "A topic"]]}
        new = {"relationships": [
            {"src_id": "A topic", "tgt_id": "soundtrack"},
            {"src_id": "alice", "tgt_id": "A topic"},
        ]}

        self.assertEqual(_stale_structural_relations(prior, new),
                         [("A topic", "music")])

    def test_an_unchanged_payload_yields_nothing(self):
        prior = {"rels": _pass1_rel_pairs(PAYLOAD)}

        self.assertEqual(_stale_structural_relations(prior, PAYLOAD), [])

    def test_a_v1_entry_yields_nothing_rather_than_guessing(self):
        """v1 recorded no relations. Deleting on a guess would be worse."""
        self.assertEqual(_stale_structural_relations({"hash": "x", "docs": []}, PAYLOAD), [])
        self.assertEqual(_stale_structural_relations(None, PAYLOAD), [])

    def test_direction_matters(self):
        """(a,b) and (b,a) are different edges; treating them as one would
        delete a live edge while leaving the dead one."""
        prior = {"rels": [["a", "b"]]}
        new = {"relationships": [{"src_id": "b", "tgt_id": "a"}]}

        self.assertEqual(_stale_structural_relations(prior, new), [("a", "b")])


if __name__ == "__main__":
    unittest.main()
