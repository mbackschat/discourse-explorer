"""Tests for the Pass-1 checkpoint's core safety invariant.

**The ledger never advances without a verified flush of the state it describes.**

`pass1_payload_hashes.json` records "this topic's structural payload is durably
in the graph". Pass 1 consults it on the next run and *skips* every topic whose
hash matches. So a ledger entry written for work that never reached disk does
not cause redundant work — it causes that topic's nodes and edges to be missing
from the graph permanently, with no error and nothing in the logs.

The bug this file exists to prevent (found in cold review, 2026-08-15): the
ledger saved on the topic index while the flush ran off LightRAG's
`_insert_done` counter, which only advances when a topic is actually inserted.
Traced with the real loop arithmetic:

    topics=1399 skipped=1314 -> storage flushes at []
                             -> ledger saves at [250, 500, 750, 1000, 1250]

That is the routine refresh — precisely the case the skip ledger was built for.

Run via:
    uv run python -m unittest tests.test_pass1_checkpoint
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.query import (  # noqa: E402
    _INSERT_DONE_STORAGE_ATTRS,
    PASS1_HASH_FILE,
    LedgerFlushError,
    _checkpoint_pass1,
    _load_pass1_hashes,
)


def _ledger_entries(hashes: dict) -> dict:
    """Ledger v2 entries from `{tid: hash}`, so these tests stay about ordering.

    v2 records the document ids a topic produced (see `_pass1_plan`); the
    purge path owns that contract, not the checkpoint path.
    """
    return {tid: {"hash": h, "docs": []} for tid, h in hashes.items()}


class _RecordingStore:
    """Storage that records flush order into a shared list."""

    def __init__(self, name, log, result=True):
        self.name = name
        self.namespace = name
        self._log = log
        self._result = result

    async def index_done_callback(self):
        self._log.append(self.name)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _RecordingRag:
    def __init__(self, log, failing=None):
        for attr in _INSERT_DONE_STORAGE_ATTRS:
            result = True
            if failing is not None and attr == failing:
                result = False
            setattr(self, attr, _RecordingStore(attr, log, result))


class Pass1CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "graphrag"
        self.dir.mkdir(parents=True)
        self.ledger = self.dir / PASS1_HASH_FILE

    def _run(self, rag, seen, dirty):
        return asyncio.run(_checkpoint_pass1(rag, self.dir, seen, dirty))

    # --- the invariant ----------------------------------------------------

    def test_ledger_is_not_written_when_a_storage_fails_to_persist(self):
        """The whole point. A failed graphml/Faiss write must not be followed
        by a ledger claiming those topics are seeded."""
        log = []
        rag = _RecordingRag(log, failing="chunk_entity_relation_graph")
        with self.assertRaises(LedgerFlushError):
            self._run(rag, _ledger_entries({"101": "hash-a"}), dirty=True)
        self.assertFalse(
            self.ledger.exists(),
            "ledger was advanced despite a storage reporting failure — a resume "
            "would now skip topics whose graph writes never landed",
        )

    def test_ledger_is_written_after_a_fully_successful_flush(self):
        log = []
        rag = _RecordingRag(log)
        dirty = self._run(rag, _ledger_entries({"101": "hash-a", "102": "hash-b"}), dirty=True)
        self.assertFalse(dirty, "checkpoint must clear the dirty flag")
        self.assertEqual(
            _load_pass1_hashes(self.dir),
            _ledger_entries({"101": "hash-a", "102": "hash-b"}))

    def test_flush_happens_before_the_ledger_write(self):
        """Ordering, observed rather than asserted about: every storage must
        appear in the flush log before the ledger file exists on disk."""
        log = []
        seen_at_flush = {}

        class _Watcher(_RecordingStore):
            async def index_done_callback(_self):
                seen_at_flush[_self.name] = self.ledger.exists()
                return await _RecordingStore.index_done_callback(_self)

        rag = _RecordingRag(log)
        for attr in _INSERT_DONE_STORAGE_ATTRS:
            setattr(rag, attr, _Watcher(attr, log))
        self._run(rag, _ledger_entries({"101": "hash-a"}), dirty=True)

        self.assertTrue(self.ledger.exists())
        self.assertTrue(
            all(existed is False for existed in seen_at_flush.values()),
            f"ledger existed during the flush: {seen_at_flush}",
        )

    # --- the SSD / no-op guard -------------------------------------------

    def test_clean_checkpoint_touches_nothing(self):
        """A resume that skipped every topic must not flush. Faiss rewrites
        ~500MB per flush with no dirty guard of its own."""
        log = []
        rag = _RecordingRag(log)
        dirty = self._run(rag, _ledger_entries({"101": "hash-a"}), dirty=False)
        self.assertFalse(dirty)
        self.assertEqual(log, [], "flushed despite nothing having been inserted")
        self.assertFalse(self.ledger.exists())

    def test_clean_checkpoint_does_not_clobber_an_existing_ledger(self):
        self.ledger.write_text(json.dumps(_ledger_entries({"999": "prior-run"})))
        rag = _RecordingRag([])
        self._run(rag, {}, dirty=False)
        self.assertEqual(_load_pass1_hashes(self.dir),
                         _ledger_entries({"999": "prior-run"}))

    # --- the interleaving that actually broke -----------------------------

    def test_checkpoint_runs_off_the_topic_index_not_the_insert_counter(self):
        """Regression for the decoupled-clocks bug.

        Simulates a resume where nearly every topic is skipped. The checkpoint
        must fire on the topic index and, because nothing was inserted, must
        persist nothing at all — as opposed to the old behaviour, which saved
        the ledger on that same schedule while the flush never ran once.
        """
        PASS1_CHECKPOINT_EVERY = 250
        total, changed_at = 1399, {700}
        log = []
        rag = _RecordingRag(log)

        seen, dirty, ledger_saves = {}, False, []
        for idx in range(1, total + 1):
            if idx in changed_at:              # an insert actually landed
                seen[str(idx)] = f"hash-{idx}"
                dirty = True
            if idx % PASS1_CHECKPOINT_EVERY == 0:
                before = self.ledger.exists() and self.ledger.stat().st_mtime_ns
                dirty = self._run(rag, seen, dirty)
                after = self.ledger.exists() and self.ledger.stat().st_mtime_ns
                if before != after:
                    ledger_saves.append(idx)

        # Only the checkpoint following the single real insert may persist.
        self.assertEqual(ledger_saves, [750])
        self.assertEqual(
            len(log), len(_INSERT_DONE_STORAGE_ATTRS),
            "expected exactly one flush of every storage, got "
            f"{len(log)} callbacks — the ledger and the flush have drifted apart",
        )
        self.assertEqual(_load_pass1_hashes(self.dir),
                         _ledger_entries({"700": "hash-700"}))


if __name__ == "__main__":
    unittest.main()
