"""Smoke tests for the committed fixture at `sample/fixtures/seed42-tiny/`.

Phase 6: the fixture is the scraper output of `init --seed 42 --scale tiny
--no-llm` round-tripped through `discourse-explorer scrape`, with a minimal
GraphRAG bake (just `graph_chunk_entity_relation.graphml` + the two
chunk→topic kv_stores `visualize` needs — we strip the faiss vector
indices and the LLM-response cache so the fixture stays under the 3 MB
budget). Tests assert:

1. `discourse-explorer visualize` runs cleanly against the fixture and
   produces a non-empty `data.js`.
2. `discourse-explorer stats categories` runs cleanly and emits a
   non-empty table.
3. Every topic JSON + post body is blocklist-clean — catches a future
   model-update or cache-invalidation regression that re-introduces
   real-franchise leaks.

Tests run the real CLI via `subprocess.run`; the fixture is copied to a
tmpdir per-test so `visualize`'s `<data_dir>/visualize/` output and any
DuckDB scratch state from `stats` don't dirty the committed fixture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sample.seed.content import blocklist as blocklist_module


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "seed42-tiny"


class FixtureSmokeTests(unittest.TestCase):
    """One consolidated class — Phase 6's invariants together."""

    @classmethod
    def setUpClass(cls) -> None:
        if not _FIXTURE_DIR.exists():
            raise unittest.SkipTest(
                f"fixture missing at {_FIXTURE_DIR}; rebuild via Phase 6 docs"
            )

    def _copy_to_tmpdir(self, tmpdir: Path) -> Path:
        target = tmpdir / "fixture"
        shutil.copytree(_FIXTURE_DIR, target)
        return target

    def test_visualize_runs_and_writes_non_empty_data_js(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            data_dir = self._copy_to_tmpdir(Path(raw_tmp))
            result = subprocess.run(
                [
                    sys.executable, "-m", "discourse_explorer.cli",
                    "visualize", str(data_dir),
                ],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(
                result.returncode, 0,
                f"visualize failed:\nstdout: {result.stdout}\nstderr: {result.stderr}",
            )
            data_js = data_dir / "visualize" / "data.js"
            graph_html = data_dir / "visualize" / "graph.html"
            self.assertTrue(data_js.exists(), "data.js not written")
            self.assertTrue(graph_html.exists(), "graph.html not written")
            self.assertGreater(data_js.stat().st_size, 1024, "data.js suspiciously small")

    def test_stats_categories_emits_non_empty_table(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            data_dir = self._copy_to_tmpdir(Path(raw_tmp))
            result = subprocess.run(
                [
                    sys.executable, "-m", "discourse_explorer.cli",
                    "stats", "--path", str(data_dir), "categories",
                ],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(
                result.returncode, 0,
                f"stats categories failed:\nstdout: {result.stdout}\nstderr: {result.stderr}",
            )
            # Tiny fixture has 8 categories (6 seeded + 2 bitnami defaults).
            self.assertIn("Bug Reports", result.stdout)

    def test_blocklist_clean_across_topics_and_post_bodies(self) -> None:
        """No real-franchise leaks anywhere in the fixture's topic JSON."""
        topics_dir = _FIXTURE_DIR / "topics"
        topic_files = sorted(topics_dir.glob("*.json"))
        self.assertGreater(len(topic_files), 0, "fixture has no topic JSONs")
        violations: list[tuple[str, list[str]]] = []
        for f in topic_files:
            data = json.loads(f.read_text())
            # Title.
            title_hits = blocklist_module.check(data.get("title", ""))
            if title_hits:
                violations.append((f"{f.name}::title", title_hits))
            # Each post's body — `plain_text` is the scraper's stripped form.
            for post in data.get("posts", []):
                body = post.get("plain_text") or post.get("raw") or ""
                body_hits = blocklist_module.check(body)
                if body_hits:
                    violations.append(
                        (f"{f.name}::post#{post.get('post_number')}", body_hits)
                    )
        self.assertEqual(
            violations, [],
            f"blocklist hits in fixture: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
