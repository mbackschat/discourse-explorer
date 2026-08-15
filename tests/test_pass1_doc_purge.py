"""A changed topic must replace its prior documents, not accrete beside them.

Pass 1 re-seeds a changed topic by merging in what it now points at and never
removing what it used to. A tag rename or a deleted post therefore strands the
old node, still asserting "Topic tagged with X" about a topic that no longer
says so. Three such orphans were found live on 2026-08-15 (tag `202506`, users
`svogt` and `tulio.natale`) and removed by hand.

The fix is to purge the topic's previously-recorded documents before re-seeding
it. That needs the ledger to remember *which* documents a topic produced, so
this covers the v1 -> v2 ledger migration as well as the decision itself.

`adelete_by_doc_id` flushes all twelve storages **twice** per call (CLAUDE.md
RULE #2), so the purge is only safe inside a suppression context. The last test
here is the one that proves it.
"""
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from discourse_explorer.query import (
    _INSERT_DONE_STORAGE_ATTRS,
    Pass1Action,
    _all_storages,
    batched_graph_writes,
    _load_pass1_hashes,
    _pass1_doc_ids,
    _pass1_plan,
    _pass2_doc_id,
    _purge_prior_docs,
    _save_pass1_hashes,
    topic_to_document,
)

PAYLOAD = {
    "chunks": [
        {"content": "a", "source_id": "topic-42", "file_path": "topic-42.json"},
        {"content": "b", "source_id": "topic-42-p1", "file_path": "topic-42.json"},
    ],
    "entities": [],
    "relationships": [],
}


class LedgerFormatTests(unittest.TestCase):
    def test_reads_the_v1_flat_format(self):
        """v1 was `{tid: hash}`. A live ledger is in that shape right now."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "pass1_payload_hashes.json").write_text(
                json.dumps({"42": "abc", "43": "def"}))

            loaded = _load_pass1_hashes(d)

        self.assertEqual(loaded["42"], {"hash": "abc", "docs": []})
        self.assertEqual(loaded["43"], {"hash": "def", "docs": []})

    def test_reads_and_round_trips_the_v2_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            ledger = {"42": {"hash": "abc", "docs": ["topic-42", "doc-ff"]}}
            _save_pass1_hashes(d, ledger)

            self.assertEqual(_load_pass1_hashes(d), ledger)

    def test_a_damaged_entry_degrades_to_reseed_not_to_skip(self):
        """Trusting a damaged ledger skips work that was never done, silently."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "pass1_payload_hashes.json").write_text(
                json.dumps({"42": ["not", "a", "hash"], "43": None}))

            loaded = _load_pass1_hashes(d)

        for tid in ("42", "43"):
            with self.subTest(tid=tid):
                action, _ = _pass1_plan(loaded.get(tid), "anything")
                self.assertIs(action, Pass1Action.INSERT)


class DocIdTests(unittest.TestCase):
    def test_collects_every_chunk_source_id_in_order(self):
        """Overflow chunks get their own doc ids; missing one strands its nodes."""
        self.assertEqual(_pass1_doc_ids(PAYLOAD), ["topic-42", "topic-42-p1"])

    def test_deduplicates_without_reordering(self):
        payload = {"chunks": [{"source_id": s} for s in ("a", "b", "a", "c")]}
        self.assertEqual(_pass1_doc_ids(payload), ["a", "b", "c"])


class Pass1PlanTests(unittest.TestCase):
    def test_unchanged_topic_is_skipped_and_purges_nothing(self):
        prior = {"hash": "same", "docs": ["topic-42"]}

        action, purge = _pass1_plan(prior, "same")

        self.assertIs(action, Pass1Action.SKIP)
        self.assertEqual(purge, [])

    def test_new_topic_inserts_and_purges_nothing(self):
        action, purge = _pass1_plan(None, "fresh")

        self.assertIs(action, Pass1Action.INSERT)
        self.assertEqual(purge, [])

    def test_changed_topic_reseeds_and_purges_its_recorded_docs(self):
        prior = {"hash": "old", "docs": ["topic-42", "topic-42-p1", "doc-ff"]}

        action, purge = _pass1_plan(prior, "new")

        self.assertIs(action, Pass1Action.RESEED)
        self.assertEqual(purge, ["topic-42", "topic-42-p1", "doc-ff"])

    def test_changed_v1_entry_reseeds_but_has_nothing_recorded_to_purge(self):
        """v1 never recorded doc ids, so the caller must derive what it can.

        Returning [] rather than a guess keeps the guessing in one place: the
        caller knows the topic id and can derive `topic-<id>`, which this
        function cannot see.
        """
        action, purge = _pass1_plan({"hash": "old", "docs": []}, "new")

        self.assertIs(action, Pass1Action.RESEED)
        self.assertEqual(purge, [])


class Pass2DocIdTests(unittest.TestCase):
    def test_sanitizes_before_hashing_like_lightrag_does(self):
        """The sanitizer runs *before* the hash (`lightrag.py:1424-1430`).

        Hashing the raw text instead yields a plausible-looking id that matches
        nothing, so a purge keyed on it silently deletes zero documents while
        reporting success. Text with a lone surrogate makes the two differ.
        """
        from lightrag.utils import compute_mdhash_id, sanitize_text_for_encoding

        topic = {"id": 7, "title": "t", "posts": [{"plain_text": "a\ud800b"}]}
        text = topic_to_document(topic)
        self.assertNotEqual(
            sanitize_text_for_encoding(text), text,
            "fixture no longer exercises the sanitizer; pick dirtier text")

        self.assertEqual(
            _pass2_doc_id(topic),
            compute_mdhash_id(sanitize_text_for_encoding(text), prefix="doc-"))
        self.assertNotEqual(
            _pass2_doc_id(topic), compute_mdhash_id(text, prefix="doc-"))

    def test_is_stable_for_the_same_topic(self):
        topic = {"id": 7, "title": "t", "posts": [{"plain_text": "body"}]}
        self.assertEqual(_pass2_doc_id(topic), _pass2_doc_id(dict(topic)))


class _FakeRag:
    """Records deletions; optionally fails on a chosen doc id."""

    def __init__(self, fail_on=None):
        self.deleted = []
        self.fail_on = fail_on

    async def adelete_by_doc_id(self, doc_id):
        self.deleted.append(doc_id)
        if doc_id == self.fail_on:
            raise RuntimeError("no such document")


class PurgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_every_recorded_document(self):
        rag = _FakeRag()

        purged = await _purge_prior_docs(rag, ["topic-42", "topic-42-p1"], 42)

        self.assertEqual(rag.deleted, ["topic-42", "topic-42-p1"])
        self.assertEqual(purged, 2)

    async def test_one_failed_delete_does_not_abort_the_rest(self):
        """A doc id the ledger remembers may already be gone.

        Aborting there would strand the remaining ids *and* kill a multi-hour
        run over a document that is already in the desired state.
        """
        rag = _FakeRag(fail_on="topic-42")

        purged = await _purge_prior_docs(rag, ["topic-42", "topic-42-p1"], 42)

        self.assertEqual(rag.deleted, ["topic-42", "topic-42-p1"])
        self.assertEqual(purged, 1)

    async def test_nothing_recorded_means_no_calls_at_all(self):
        rag = _FakeRag()

        self.assertEqual(await _purge_prior_docs(rag, [], 42), 0)
        self.assertEqual(rag.deleted, [])


class _FlushingStore:
    def __init__(self, name, log):
        self.name = self.namespace = name
        self.log = log

    async def index_done_callback(self):
        self.log.append(self.name)
        return True


class _FlushingRag:
    """A rag whose `adelete_by_doc_id` costs what the real one costs.

    `adelete_by_doc_id` flushes all twelve storages **twice** per call
    (CLAUDE.md RULE #2's table): once for the pre-rebuild persist, once after
    the rebuild. Modelling both is the point — a fake that flushed once would
    understate the bill this test exists to cap.
    """

    def __init__(self, log):
        for attr in _INSERT_DONE_STORAGE_ATTRS:
            setattr(self, attr, _FlushingStore(attr, log))

    async def adelete_by_doc_id(self, doc_id):
        for _ in range(2):
            for store in _all_storages(self):
                await store.index_done_callback()


class PurgeWriteBatchingTests(unittest.IsolatedAsyncioTestCase):
    DOCS = [f"topic-{i}" for i in range(25)]

    async def test_purging_inside_the_context_costs_one_write_per_file(self):
        log = []
        rag = _FlushingRag(log)

        async with batched_graph_writes(rag):
            await _purge_prior_docs(rag, self.DOCS, 42)

        counts = Counter(log)
        self.assertTrue(counts, "nothing flushed at all")
        self.assertEqual(
            set(counts.values()), {1},
            f"expected exactly ONE write per file, got {dict(counts)}")

    async def test_without_the_context_the_same_purge_costs_fifty(self):
        """Guard for the guard: 25 documents x 2 flushes = 50 per file.

        At ~520MB per all-storage flush that is roughly 26GB, which is the
        scale of write this whole rule exists to prevent.
        """
        log = []
        rag = _FlushingRag(log)

        await _purge_prior_docs(rag, self.DOCS, 42)

        self.assertEqual(set(Counter(log).values()), {2 * len(self.DOCS)})


if __name__ == "__main__":
    unittest.main()
