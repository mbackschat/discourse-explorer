"""The data-dir index lock: two indexers must never run on one graph.

Two concurrent indexers corrupt `graphrag/`. LightRAG storages are process-local
in-memory copies flushed wholesale, so the second writer overwrites rather than
merges. Observed 2026-08-14: three overlapping runs reduced a graph from 15,756
to 4,566 nodes, and the fallout (lock contention, reload-on-`storage_updated`)
presented for hours as unexplained process deaths and network faults.

Root cause was a liveness check that could not match the process it looked for:
`pgrep -f "discourse_explorer.query"` never matches the console entry point's
`discourse-explorer query`. These tests exist so correctness no longer depends
on getting such a pattern right.

Run via:
    uv run python -m unittest tests.test_index_lock
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from discourse_explorer.query import (  # noqa: E402
    INDEX_LOCK_FILE,
    IndexLockError,
    index_lock,
)


class IndexLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "data"

    def test_lock_is_acquired_and_records_the_holder(self):
        with index_lock(self.dir):
            body = (self.dir / INDEX_LOCK_FILE).read_text()
        self.assertIn("pid=", body)
        self.assertIn("started=", body)

    def test_reentry_after_release_succeeds(self):
        with index_lock(self.dir):
            pass
        with index_lock(self.dir):
            pass  # must not raise — lock released on exit

    def test_lock_survives_an_exception_and_is_released(self):
        with self.assertRaises(ValueError):
            with index_lock(self.dir):
                raise ValueError("boom")
        with index_lock(self.dir):
            pass

    def test_second_process_is_refused_while_the_first_holds_it(self):
        """The case that actually mattered: a real second process."""
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(_ROOT)!r})
                from discourse_explorer.query import index_lock
                with index_lock(__import__("pathlib").Path({str(self.dir)!r})):
                    print("HELD", flush=True)
                    time.sleep(30)
            """)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual("HELD", holder.stdout.readline().strip(),
                             "holder process failed to acquire the lock")
            with self.assertRaises(IndexLockError) as ctx:
                with index_lock(self.dir):
                    pass
            msg = str(ctx.exception)
            self.assertIn("already active", msg)
            self.assertIn("pid=", msg, "must name the holding pid")
        finally:
            holder.kill()
            holder.wait(timeout=10)
            holder.stdout.close()
            holder.stderr.close()

    def test_lock_is_free_again_after_the_holder_is_killed(self):
        """flock is released by the OS on exit, including SIGKILL, so a crashed
        run never leaves the data dir permanently locked."""
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(_ROOT)!r})
                from discourse_explorer.query import index_lock
                with index_lock(__import__("pathlib").Path({str(self.dir)!r})):
                    print("HELD", flush=True)
                    time.sleep(30)
            """)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual("HELD", holder.stdout.readline().strip())
        holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()
        holder.stderr.close()
        with index_lock(self.dir):
            pass  # stale lock FILE remains, but the lock itself is gone


if __name__ == "__main__":
    unittest.main()
