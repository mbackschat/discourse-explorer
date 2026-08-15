"""Guards for indexing invariants that live inside `index_topics`.

`index_topics` is a ~400-line async function needing a live LightRAG, so its
internals cannot be called from a test. That is precisely where the two worst
bugs of 2026-08-14/15 lived. Rather than leave them uncovered, the structural
invariants are asserted against the **parsed source** — the same technique as
`AllStoragesTests.test_matches_lightrags_own_insert_done_storage_list`, which
checks our storage list against LightRAG's own source instead of against a copy
we maintain.

A structural guard is weaker than a behavioural one. It is much stronger than
the nothing that was there before: each of these went green under a mutation
that reintroduced a real data-loss bug.

Run via:
    uv run python -m unittest tests.test_index_safety_guards
"""

import asyncio
import ast
from collections import Counter
import errno
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.query import (  # noqa: E402
    _INSERT_DONE_STORAGE_ATTRS,
    IndexLockError,
    LedgerFlushError,
    _all_storages,
    _defer_ledger_flush,
    _flush_storages,
    _progress_line,
    index_lock,
)

_QUERY_SRC = (_ROOT / "discourse_explorer" / "query.py").read_text()
_QUERY_AST = ast.parse(_QUERY_SRC)


def _func(name, tree=None):
    """The FunctionDef/AsyncFunctionDef named `name`, at any nesting depth."""
    for node in ast.walk(tree if tree is not None else _QUERY_AST):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in query.py")


def _called_names(node):
    """Names of every function called under `node`, in SOURCE order.

    `ast.walk` is breadth-first, so sorting by position is required — without
    it this returned calls in an order unrelated to the file and the ordering
    assertions below were meaningless.
    """
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name:
                found.append((n.lineno, n.col_offset, name))
    return [name for _, _, name in sorted(found)]


class LedgerCouplingGuards(unittest.TestCase):
    """The Pass-1 skip ledger must only ever advance behind a verified flush."""

    def test_save_pass1_hashes_is_called_from_exactly_one_place(self):
        """More than one caller means more than one clock, which is the bug.

        The original defect was a second call site in the Pass 1 loop that ran
        on the topic index while the flush ran on the insert counter.
        """
        callers = []
        for node in ast.walk(_QUERY_AST):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call in ast.walk(node):
                    if (isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "_save_pass1_hashes"):
                        callers.append(node.name)
        # `_checkpoint_pass1` is nested-walked from module level too; dedupe.
        self.assertEqual(
            {"_checkpoint_pass1"}, set(callers),
            f"_save_pass1_hashes must be reachable only through "
            f"_checkpoint_pass1, but is called from: {sorted(set(callers))}")

    def test_checkpoint_flushes_before_it_saves(self):
        calls = _called_names(_func("_checkpoint_pass1"))
        self.assertIn("_flush_ledger_last", calls)
        self.assertIn("_save_pass1_hashes", calls)
        self.assertLess(
            calls.index("_flush_ledger_last"), calls.index("_save_pass1_hashes"),
            "the ledger is saved before the flush it is supposed to attest to")

    def test_checkpoint_short_circuits_when_nothing_was_inserted(self):
        """Without this, a mostly-skipped resume rewrites ~500MB of Faiss
        indices for no reason (Faiss has no dirty guard of its own)."""
        src = ast.get_source_segment(_QUERY_SRC, _func("_checkpoint_pass1"))
        self.assertIn("if not dirty", src)

    def test_pass1_suppresses_the_counter_driven_flush(self):
        """Pass 1 must own its checkpointing outright. Leaving LightRAG's
        counter-driven flush active is what let the two clocks drift."""
        self.assertIn('_flush_state["suppressed"] = True', _QUERY_SRC)
        self.assertIn('_flush_state["suppressed"] = False', _QUERY_SRC)

    def test_pass2_defers_the_ledger_flush(self):
        """doc_status self-flushes on upsert, so without the deferral the
        `_flush_ledger_last` ordering is a no-op."""
        calls = _called_names(_func("index_topics"))
        self.assertIn("_defer_ledger_flush", calls)

    def test_payload_construction_cannot_abort_the_pass(self):
        """One malformed topic must cost that topic, not the whole run.

        `_topic_to_custom_kg` raises KeyError without an `id`;
        `_pass1_payload_hash` raises UnicodeEncodeError on a lone surrogate.
        """
        src = ast.get_source_segment(_QUERY_SRC, _func("index_topics"))
        build = src.index("payload = _topic_to_custom_kg(topic)")
        prefix = src[:build]
        # The nearest enclosing statement before the call must be a `try:`.
        self.assertRegex(
            prefix[-400:], r"try:\s*\n(\s*#.*\n)*\s*$",
            "payload construction is not inside a try — a malformed topic "
            "aborts the entire multi-hour pass")


class FlushFailureGuards(unittest.TestCase):
    """NetworkX and Faiss return False instead of raising on write failure."""

    class _Store:
        def __init__(self, name, result=True):
            self.name = self.namespace = name
            self._result = result

        async def index_done_callback(self):
            if isinstance(self._result, BaseException):
                raise self._result
            return self._result

    def test_flush_storages_raises_when_a_storage_returns_false(self):
        stores = [self._Store("graph", False), self._Store("vdb", True)]
        with self.assertRaises(LedgerFlushError) as ctx:
            asyncio.run(_flush_storages(stores))
        self.assertIn("graph", str(ctx.exception))

    def test_flush_storages_raises_when_a_storage_raises(self):
        stores = [self._Store("vdb", OSError("disk gone"))]
        with self.assertRaises(LedgerFlushError):
            asyncio.run(_flush_storages(stores))

    def test_flush_storages_accepts_none_returning_storages(self):
        """JsonKVStorage returns None, not True. That must not read as failure."""
        stores = [self._Store("kv", None)]
        asyncio.run(_flush_storages(stores))


class DeferLedgerFlushGuards(unittest.TestCase):
    class _Ledger:
        def __init__(self):
            self.writes = 0

        async def index_done_callback(self):
            self.writes += 1
            return True

    def test_swaps_the_callback_and_yields_the_real_one(self):
        ledger = self._Ledger()
        with _defer_ledger_flush(ledger) as real:
            # Behaviour, not identity: a bound method is rebuilt on every
            # attribute access, so `a.m is a.m` is False and identity proves
            # nothing here.
            asyncio.run(ledger.index_done_callback())
            self.assertEqual(0, ledger.writes, "suppressed callback still wrote")
            asyncio.run(real())
            self.assertEqual(1, ledger.writes, "yielded callback does not write")
        asyncio.run(ledger.index_done_callback())
        self.assertEqual(2, ledger.writes, "real callback was not restored")

    def test_restores_the_callback_when_the_body_raises(self):
        ledger = self._Ledger()
        with self.assertRaises(RuntimeError):
            with _defer_ledger_flush(ledger):
                raise RuntimeError("boom")
        asyncio.run(ledger.index_done_callback())
        self.assertEqual(1, ledger.writes,
                         "callback left suppressed after an exception — every "
                         "later ledger write would be silently discarded")


class IndexLockErrnoGuards(unittest.TestCase):
    """A filesystem without flock must not be reported as a live holder."""

    def _flock_raising(self, err):
        def _fake(fd, op):
            raise OSError(err, "mock")
        return _fake

    def test_would_block_means_another_run_holds_it(self):
        with mock.patch("fcntl.flock", self._flock_raising(errno.EWOULDBLOCK)):
            with self.assertRaises(IndexLockError):
                with index_lock(Path(self.tmp)):
                    pass

    def test_unsupported_filesystem_warns_and_proceeds(self):
        entered = False
        with mock.patch("fcntl.flock", self._flock_raising(errno.ENOTSUP)):
            with index_lock(Path(self.tmp)):
                entered = True
        self.assertTrue(
            entered,
            "a volume without flock support refused the run entirely, which "
            "sends the operator back to hunting processes by name")

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()


class ProgressLineGuards(unittest.TestCase):
    def test_skipped_renders_at_zero(self):
        """Absent-at-zero is indistinguishable from 'this build has no skip
        reporting' — two very different diagnoses on a resume."""
        line = _progress_line("Pass 1", 100, 1399, 60.0, ok=100, failed=0, skipped=0)
        self.assertIn("0 skipped", line)

    def test_skipped_omitted_when_the_caller_does_not_track_it(self):
        line = _progress_line("Pass 3", 10, 20, 5.0, ok=10, failed=0)
        self.assertNotIn("skipped", line)


if __name__ == "__main__":
    unittest.main()


class BatchedGraphWritesTests(unittest.TestCase):
    """CLAUDE.md RULE #2: N bulk edits must cost ONE write per file, not N.

    Every LightRAG graph helper persists on every call, and Faiss has no dirty
    guard, so each flush rewrites ~500MB regardless of what changed. A one-off
    repair script doing 54 edits in a bare loop was on course for ~27GB of
    writes before it was stopped; `adelete_by_doc_id` is worse still at two
    all-storage flushes per document.
    """

    class _CountingStore:
        def __init__(self, name, log):
            self.name = self.namespace = name
            self.log = log

        async def index_done_callback(self):
            self.log.append(self.name)
            return True

    def _rag(self, log):
        rag = type("R", (), {})()
        for attr in _INSERT_DONE_STORAGE_ATTRS:
            setattr(rag, attr, self._CountingStore(attr, log))
        return rag

    def test_fifty_edits_cost_one_flush_per_storage(self):
        from discourse_explorer.query import batched_graph_writes

        log = []
        rag = self._rag(log)

        async def body():
            async with batched_graph_writes(rag):
                for _ in range(50):
                    # Stand-in for adelete_by_relation / aedit_entity / etc.,
                    # each of which persists on every call.
                    for s in _all_storages(rag):
                        await s.index_done_callback()

        asyncio.run(body())
        counts = Counter(log)
        self.assertTrue(counts, "nothing flushed at all")
        self.assertEqual(
            set(counts.values()), {1},
            f"expected exactly ONE write per file, got {dict(counts)}")

    def test_without_the_context_the_same_loop_costs_fifty(self):
        """Guard for the guard: proves the assertion above is not vacuous."""
        log = []
        rag = self._rag(log)

        async def body():
            for _ in range(50):
                for s in _all_storages(rag):
                    await s.index_done_callback()

        asyncio.run(body())
        self.assertEqual(set(Counter(log).values()), {50})

    def test_body_exception_skips_the_flush(self):
        """A half-applied bulk edit must not reach disk."""
        from discourse_explorer.query import batched_graph_writes

        log = []
        rag = self._rag(log)

        async def body():
            async with batched_graph_writes(rag):
                raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            asyncio.run(body())
        self.assertEqual(log, [], "flushed despite the bulk edit failing")
