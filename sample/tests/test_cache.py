"""Tests for `sample.seed.content.cache.Cache`.

Sit 10. Covers the on-disk JSON cache behaviour:

* `set` then `get` round-trips a body verbatim.
* `get` for a missing key returns `None`.
* Constructing against a non-existent path yields an empty cache.
* Persistence across construction — a fresh `Cache` reads back what a prior
  instance wrote.

Cache is write-through, so there's no separate `flush` to test; every `set`
must already be visible on disk by the time it returns.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sample.seed.content.cache import Cache


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.cache_path = self.tmp_path / "test-cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_then_get_round_trips_the_body(self) -> None:
        cache = Cache(self.cache_path)
        cache.set(7, 1, "the OP body")
        self.assertEqual(cache.get(7, 1), "the OP body")

    def test_get_for_missing_key_returns_none(self) -> None:
        cache = Cache(self.cache_path)
        self.assertIsNone(cache.get(1, 1))
        # Setting a different key does NOT make a sibling miss return non-None.
        cache.set(1, 1, "OP")
        self.assertIsNone(cache.get(1, 2))

    def test_construction_with_nonexistent_path_is_empty(self) -> None:
        # File definitely doesn't exist yet.
        self.assertFalse(self.cache_path.exists())
        cache = Cache(self.cache_path)
        self.assertEqual(len(cache), 0)
        self.assertIsNone(cache.get(1, 1))

    def test_round_trip_across_construction(self) -> None:
        first = Cache(self.cache_path)
        first.set(2, 1, "OP body")
        first.set(2, 3, "third post body")
        first.set(11, 1, "another topic OP")

        # Drop the first instance — a fresh Cache must reconstruct from disk.
        del first
        second = Cache(self.cache_path)
        self.assertEqual(len(second), 3)
        self.assertEqual(second.get(2, 1), "OP body")
        self.assertEqual(second.get(2, 3), "third post body")
        self.assertEqual(second.get(11, 1), "another topic OP")

    def test_set_writes_through_to_disk_immediately(self) -> None:
        # Write-through invariant: after `set` returns, the JSON on disk
        # must already include the entry. No explicit `flush` step.
        cache = Cache(self.cache_path)
        cache.set(5, 2, "reply body")
        self.assertTrue(self.cache_path.exists())
        on_disk = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"5:2": "reply body"})

    def test_len_reflects_entry_count(self) -> None:
        cache = Cache(self.cache_path)
        self.assertEqual(len(cache), 0)
        cache.set(1, 1, "a")
        self.assertEqual(len(cache), 1)
        cache.set(1, 2, "b")
        self.assertEqual(len(cache), 2)
        # Overwriting an existing key doesn't grow the cache.
        cache.set(1, 1, "a-updated")
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.get(1, 1), "a-updated")

    def test_construction_against_existing_empty_file(self) -> None:
        # An empty file (created by some prior step but not yet populated)
        # must be tolerated as "empty cache", not raise on JSON decode.
        self.cache_path.write_text("", encoding="utf-8")
        cache = Cache(self.cache_path)
        self.assertEqual(len(cache), 0)
        cache.set(1, 1, "after empty start")
        self.assertEqual(cache.get(1, 1), "after empty start")


if __name__ == "__main__":
    unittest.main()
