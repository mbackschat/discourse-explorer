"""Tests for indexing persistence policy: SSD-friendly flushing + cache reuse.

Covers the write-amplification and progress helpers in `discourse_explorer.query`:

  - `_progress_line(...)`          — pure formatter (rate / ETA), 3 call sites
  - `_all_storages(rag)`           — storage set suppressed around Pass 1
  - `_clear_graph_dir(...)`        — `--clear` that preserves the LLM cache
  - `_ensure_cache_provenance(...)`— drops a cache built by another model

Why this matters: `FaissVectorDBStorage.index_done_callback` has no dirty
guard (unlike `JsonKVStorage`), so every flush rewrites all three Faiss
indices in full -- ~498MB. Fewer flushes is the only lever we control.

And the LLM response cache is keyed on `mode:cache_type:hash(prompt)` with
**no model component** (`lightrag/utils.py::generate_cache_key`), so reusing
a cache across a model change would silently serve the old model's
completions. Provenance must gate the reuse.

Run via:
    uv run python -m unittest tests.test_index_persistence
"""

import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.query import (  # noqa: E402
    CACHE_PROVENANCE_FILE,
    LLM_CACHE_FILE,
    PERSIST_EVERY,
    LedgerFlushError,
    _INSERT_DONE_STORAGE_ATTRS,
    _all_storages,
    _cache_is_parseable,
    _clear_graph_dir,
    _ensure_cache_provenance,
    _flush_ledger_last,
    _progress_line,
    _should_flush,
    _suppress_index_done,
)


class _FakeStore:
    def __init__(self, name):
        self.name = name
        # Real LightRAG storages carry `namespace`; the failure message reports it.
        self.namespace = name

    async def index_done_callback(self):
        return True


class _FakeRag:
    """Only the storage attributes `_all_storages` should collect."""

    def __init__(self, missing=()):
        for attr in _INSERT_DONE_STORAGE_ATTRS:
            setattr(self, attr, None if attr in missing else _FakeStore(attr))


class ProgressLineTests(unittest.TestCase):
    def test_reports_rate_and_eta(self):
        line = _progress_line("Pass 1", done=100, total=1000, elapsed=50.0)
        self.assertIn("100/1000", line)
        self.assertIn("Pass 1", line)
        self.assertIn("elapsed=50s", line)
        # 100 in 50s = 2/s; 900 remaining => 450s
        self.assertIn("ETA=450s", line)

    def test_zero_elapsed_does_not_divide_by_zero(self):
        line = _progress_line("Pass 1", done=5, total=10, elapsed=0.0)
        self.assertIn("ETA=", line)

    def test_complete_reports_zero_eta(self):
        line = _progress_line("Pass 1", done=10, total=10, elapsed=10.0)
        self.assertIn("ETA=0s", line)

    def test_optional_ok_failed_counts_are_rendered(self):
        line = _progress_line("Pass 4a", done=10, total=20, elapsed=5.0,
                              ok=8, failed=2)
        self.assertIn("8 ok", line)
        self.assertIn("2 failed", line)

    def test_counts_omitted_when_not_supplied(self):
        self.assertNotIn("ok", _progress_line("Pass 1", 1, 2, 1.0))


class AllStoragesTests(unittest.TestCase):
    def test_matches_lightrags_own_insert_done_storage_list(self):
        """Non-circular check: parse the storages LightRAG's `_insert_done`
        actually flushes and compare against our tuple. Asserting a hand-copied
        count would only prove the code equals itself, which would not have
        caught `full_entities` / `full_relations` going missing.
        """
        import inspect

        import lightrag.lightrag as lr

        # `_insert_done`'s body references `self.<attr>` only for the storages it
        # flushes, so an exact set comparison is the honest assertion. An earlier
        # version filtered `found` by suffix, which silently excluded
        # `full_entities` / `full_relations` — the very two that had gone missing —
        # and passed vacuously. Compare both directions.
        src = inspect.getsource(lr.LightRAG._insert_done)
        found = set(re.findall(r"self\.(\w+)", src))
        self.assertEqual(
            found, set(_INSERT_DONE_STORAGE_ATTRS),
            "_INSERT_DONE_STORAGE_ATTRS must match LightRAG._insert_done exactly; "
            f"missing={found - set(_INSERT_DONE_STORAGE_ATTRS)} "
            f"extra={set(_INSERT_DONE_STORAGE_ATTRS) - found}")

    def test_collects_every_present_storage(self):
        self.assertEqual(len(_INSERT_DONE_STORAGE_ATTRS), len(_all_storages(_FakeRag())))

    def test_skips_absent_storages(self):
        rag = _FakeRag(missing=("doc_status", "llm_response_cache"))
        names = {s.name for s in _all_storages(rag)}
        self.assertNotIn("doc_status", names)
        self.assertEqual(len(_INSERT_DONE_STORAGE_ATTRS) - 2, len(names))


class LedgerOrderingTests(unittest.TestCase):
    """The doc_status ledger must never be durable before the state it describes."""

    def _run(self, rag, fail=(), raise_in=()):
        order = []
        for s in _all_storages(rag):
            s.index_done_callback = _recorder(
                s.name, order,
                result=False if s.name in fail else True,
                boom=s.name in raise_in)
        asyncio.run(_flush_ledger_last(rag))
        return order

    def test_doc_status_is_flushed_last(self):
        order = self._run(_FakeRag())
        self.assertEqual("doc_status", order[-1])
        self.assertEqual(1, order.count("doc_status"),
                         "ledger must be written exactly once, not twice")

    def test_all_storages_flush(self):
        order = self._run(_FakeRag())
        self.assertEqual(len(_INSERT_DONE_STORAGE_ATTRS), len(order))

    def test_works_when_no_ledger_exists(self):
        order = self._run(_FakeRag(missing=("doc_status",)))
        self.assertNotIn("doc_status", order)
        self.assertEqual(len(_INSERT_DONE_STORAGE_ATTRS) - 1, len(order))

    def test_ledger_is_not_advanced_when_a_storage_reports_failure(self):
        """Faiss/NetworkX swallow their own write errors and return False. If we
        advanced the ledger anyway, a resume would SKIP those documents and their
        entities would be missing permanently."""
        rag = _FakeRag()
        order = []
        for s in _all_storages(rag):
            s.index_done_callback = _recorder(
                s.name, order, result=s.name != "chunk_entity_relation_graph")
        with self.assertRaises(LedgerFlushError) as ctx:
            asyncio.run(_flush_ledger_last(rag))
        self.assertIn("chunk_entity_relation_graph", str(ctx.exception))
        self.assertNotIn("doc_status", order)

    def test_ledger_is_not_advanced_when_a_storage_raises(self):
        rag = _FakeRag()
        order = []
        for s in _all_storages(rag):
            s.index_done_callback = _recorder(
                s.name, order, boom=s.name == "entities_vdb")
        with self.assertRaises(LedgerFlushError) as ctx:
            asyncio.run(_flush_ledger_last(rag))
        self.assertIn("entities_vdb", str(ctx.exception))
        self.assertNotIn("doc_status", order)

    def test_concurrent_flushes_do_not_disable_the_ledger(self):
        """An earlier implementation nested a monkey-patch to reorder the ledger;
        two overlapping calls restored the no-op as the 'original' and silently
        disabled doc_status writes for the rest of the process."""
        rag = _FakeRag()
        order = []
        for s in _all_storages(rag):
            s.index_done_callback = _recorder(s.name, order, delay=0.01)

        async def main():
            await asyncio.gather(_flush_ledger_last(rag), _flush_ledger_last(rag))
            order.clear()
            await _flush_ledger_last(rag)

        asyncio.run(main())
        self.assertEqual("doc_status", order[-1],
                         "ledger flush must survive overlapping calls")


def _recorder(name, sink, result=True, boom=False, delay=0.0):
    async def _cb():
        if delay:
            await asyncio.sleep(delay)
        if boom:
            raise OSError(f"simulated write failure in {name}")
        sink.append(name)
        return result
    return _cb


class SuppressRestoresOnExceptionTests(unittest.TestCase):
    """Pass 1 holds a whole pass of embeddings in memory; the boundary flush
    runs in a `finally`, which depends on suppression unwinding cleanly."""

    def test_callbacks_restored_when_body_raises(self):
        rag = _FakeRag()
        stores = _all_storages(rag)
        originals = [s.index_done_callback for s in stores]
        with self.assertRaises(KeyboardInterrupt):
            with _suppress_index_done(stores):
                raise KeyboardInterrupt
        self.assertEqual(originals, [s.index_done_callback for s in stores])


class ShouldFlushTests(unittest.TestCase):
    """A flush must not be claimed while storage callbacks are suppressed.

    Observed live on 2026-08-14: during Pass 1 the batched flush fired on
    schedule, gathered the suppressed no-ops, saw them all succeed and logged
    "In memory DB persist to disk" four times while the write sampler recorded
    zero rewrites. The log asserted durability that did not exist.
    """

    def test_flushes_on_the_interval_when_not_suppressed(self):
        self.assertTrue(_should_flush(200, 200, suppressed=False))
        self.assertTrue(_should_flush(400, 200, suppressed=False))

    def test_does_not_flush_between_intervals(self):
        self.assertFalse(_should_flush(199, 200, suppressed=False))
        self.assertFalse(_should_flush(201, 200, suppressed=False))

    def test_never_flushes_while_suppressed_even_on_the_interval(self):
        for counter in (200, 400, 600, 800, 1000, 1200):
            self.assertFalse(
                _should_flush(counter, 200, suppressed=True),
                f"counter {counter} would log a persist that never reaches disk")


class PersistEveryTests(unittest.TestCase):
    def test_batching_interval_is_ssd_conscious(self):
        """A full Faiss rewrite is ~498MB; 50 was ~28 flushes per pass."""
        self.assertGreaterEqual(PERSIST_EVERY, 200)


class ClearGraphDirTests(unittest.TestCase):
    def _graph_dir(self, model="gpt-4.1-mini", with_cache=True, with_prov=True):
        d = Path(tempfile.mkdtemp()) / "graphrag"
        d.mkdir(parents=True)
        (d / "graph_chunk_entity_relation.graphml").write_text("<graphml/>")
        (d / "faiss_index_entities.index").write_bytes(b"\x00" * 32)
        (d / "kv_store_doc_status.json").write_text("{}")
        if with_cache:
            (d / LLM_CACHE_FILE).write_text('{"default:extract:abc": "cached"}')
        if with_prov:
            (d / CACHE_PROVENANCE_FILE).write_text(
                json.dumps({"extraction_model": model}))
        return d

    def test_preserves_cache_when_model_matches(self):
        d = self._graph_dir(model="gpt-4.1-mini")
        kept = _clear_graph_dir(d, "gpt-4.1-mini")
        self.assertTrue(kept)
        self.assertTrue((d / LLM_CACHE_FILE).exists())
        self.assertEqual('{"default:extract:abc": "cached"}',
                         (d / LLM_CACHE_FILE).read_text())

    def test_drops_cache_when_model_differs(self):
        d = self._graph_dir(model="gpt-4o-mini")
        kept = _clear_graph_dir(d, "gpt-4.1-mini")
        self.assertFalse(kept)
        self.assertFalse((d / LLM_CACHE_FILE).exists())

    def test_drops_cache_when_provenance_unknown(self):
        """An unlabelled cache could come from any model — refuse to trust it."""
        d = self._graph_dir(with_prov=False)
        self.assertFalse(_clear_graph_dir(d, "gpt-4.1-mini"))
        self.assertFalse((d / LLM_CACHE_FILE).exists())

    def test_removes_all_other_state_even_when_cache_is_kept(self):
        d = self._graph_dir(model="gpt-4.1-mini")
        _clear_graph_dir(d, "gpt-4.1-mini")
        self.assertFalse((d / "graph_chunk_entity_relation.graphml").exists())
        self.assertFalse((d / "faiss_index_entities.index").exists())
        self.assertFalse((d / "kv_store_doc_status.json").exists())

    def test_records_provenance_for_the_new_run(self):
        d = self._graph_dir(model="gpt-4.1-mini")
        _clear_graph_dir(d, "gpt-4.1-mini")
        prov = json.loads((d / CACHE_PROVENANCE_FILE).read_text())
        self.assertEqual("gpt-4.1-mini", prov["extraction_model"])

    def test_truncated_cache_is_not_carried_across_the_wipe(self):
        """LightRAG writes this file non-atomically and reads it without
        catching JSONDecodeError, so preserving corrupt bytes would make every
        later --clear fail on startup — turning the recovery command into the
        thing that perpetuates the damage."""
        d = self._graph_dir(model="gpt-4.1-mini")
        (d / LLM_CACHE_FILE).write_text('{"default:extract:abc": "trunca')
        self.assertFalse(_clear_graph_dir(d, "gpt-4.1-mini"))
        self.assertFalse((d / LLM_CACHE_FILE).exists())

    def test_no_stash_file_is_left_behind(self):
        d = self._graph_dir(model="gpt-4.1-mini")
        _clear_graph_dir(d, "gpt-4.1-mini")
        leftovers = [p.name for p in d.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual([], leftovers)

    def test_missing_dir_is_a_noop(self):
        d = Path(tempfile.mkdtemp()) / "absent"
        self.assertFalse(_clear_graph_dir(d, "gpt-4.1-mini"))

    def test_no_cache_present_is_not_an_error(self):
        d = self._graph_dir(with_cache=False)
        self.assertFalse(_clear_graph_dir(d, "gpt-4.1-mini"))


class CacheProvenanceTests(unittest.TestCase):
    """Guards the non-clear path too: a model switch must not read a stale cache."""

    def _dir(self):
        d = Path(tempfile.mkdtemp()) / "graphrag"
        d.mkdir(parents=True)
        (d / LLM_CACHE_FILE).write_text('{"default:extract:abc": "cached"}')
        return d

    def test_model_change_drops_the_cache(self):
        d = self._dir()
        (d / CACHE_PROVENANCE_FILE).write_text(
            json.dumps({"extraction_model": "gpt-4o-mini"}))
        _ensure_cache_provenance(d, "gpt-4.1-mini")
        self.assertFalse((d / LLM_CACHE_FILE).exists())
        self.assertEqual("gpt-4.1-mini",
                         json.loads((d / CACHE_PROVENANCE_FILE).read_text())["extraction_model"])

    def test_same_model_keeps_the_cache(self):
        d = self._dir()
        (d / CACHE_PROVENANCE_FILE).write_text(
            json.dumps({"extraction_model": "gpt-4.1-mini"}))
        _ensure_cache_provenance(d, "gpt-4.1-mini")
        self.assertTrue((d / LLM_CACHE_FILE).exists())

    def test_unlabelled_existing_cache_is_dropped_then_labelled(self):
        d = self._dir()
        _ensure_cache_provenance(d, "gpt-4.1-mini")
        self.assertFalse((d / LLM_CACHE_FILE).exists())
        self.assertTrue((d / CACHE_PROVENANCE_FILE).exists())

    def test_labels_a_fresh_dir_so_the_next_run_can_trust_the_cache(self):
        """Without this, a first build leaves the cache unlabelled and the
        following run throws away completions it already paid for."""
        d = Path(tempfile.mkdtemp()) / "graphrag"
        _ensure_cache_provenance(d, "gpt-4.1-mini")
        self.assertEqual("gpt-4.1-mini",
                         json.loads((d / CACHE_PROVENANCE_FILE).read_text())["extraction_model"])

    def test_corrupt_provenance_is_treated_as_unknown(self):
        d = self._dir()
        (d / CACHE_PROVENANCE_FILE).write_text("{not json")
        _ensure_cache_provenance(d, "gpt-4.1-mini")
        self.assertFalse((d / LLM_CACHE_FILE).exists())


if __name__ == "__main__":
    unittest.main()
