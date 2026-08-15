"""Tests for `sample.seed.cli` and `sample.seed.__main__`.

Pragmatic posture: end-to-end smoke (init --dry-run produces JSON), JSON
shape + count parity with the underlying generators, and the extend stub
raises `NotImplementedError`.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sample.seed.cli import main
from sample.seed.generators.categories import generate_categories
from sample.seed.generators.posts import generate_posts
from sample.seed.generators.tags import generate_tags
from sample.seed.generators.timeline import make_timeline
from sample.seed.generators.topics import generate_topics
from sample.seed.generators.users import generate_users
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


_EXPECTED_KEYS = {
    "seed",
    "scale",
    "product_name",
    "categories",
    "tags",
    "users",
    "topics",
    "posts",
}


class InitDryRunTests(unittest.TestCase):
    def test_init_dry_run_end_to_end(self) -> None:
        """`init --seed 42 --scale tiny --dry-run` writes a parseable JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "sample.json"
            exit_code = main(
                [
                    "init",
                    "--seed",
                    "42",
                    "--scale",
                    "tiny",
                    "--dry-run",
                    "--no-llm",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists(), "output file not written")
            payload = json.loads(output.read_text())
            self.assertEqual(set(payload.keys()), _EXPECTED_KEYS)

    def test_json_counts_match_generators(self) -> None:
        """Counts in the JSON dump match what the generators produce directly."""
        seed, scale = 42, "tiny"
        spec = GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)
        categories = generate_categories(spec)
        tags = generate_tags(spec)
        users = generate_users(spec)
        timeline = make_timeline(
            spec, total_topics=spec.scale_targets()["topics"]
        )
        topics = generate_topics(spec, categories, tags, users, timeline)
        posts = generate_posts(spec, topics, users, timeline)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "sample.json"
            main(
                [
                    "init",
                    "--seed",
                    str(seed),
                    "--scale",
                    scale,
                    "--dry-run",
                    "--no-llm",
                    "--output",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text())

        self.assertEqual(payload["seed"], seed)
        self.assertEqual(payload["scale"], scale)
        self.assertEqual(payload["product_name"], "crown-of-brine")
        self.assertEqual(len(payload["categories"]), len(categories))
        self.assertEqual(len(payload["tags"]), len(tags))
        self.assertEqual(len(payload["users"]), len(users))
        self.assertEqual(len(payload["topics"]), len(topics))
        self.assertEqual(len(payload["posts"]), len(posts))

    def test_init_dry_run_requires_output(self) -> None:
        """`--dry-run` without `--output` exits non-zero."""
        exit_code = main(
            ["init", "--seed", "42", "--scale", "tiny", "--dry-run"]
        )
        self.assertNotEqual(exit_code, 0)

    def test_init_live_mode_requires_env_vars(self) -> None:
        """Live mode (no --dry-run) needs DISCOURSE_HOST/KEY/USERNAME.

        With those env vars unset, the CLI must fail with a clear error
        rather than blowing up mid-push. We clear them deliberately for
        the duration of the call so the CI machine's ambient env doesn't
        accidentally trigger a real push.
        """
        clear = {
            "DISCOURSE_HOST": "",
            "DISCOURSE_URL": "",
            "DISCOURSE_API_KEY": "",
            "DISCOURSE_API_USERNAME": "",
        }
        # Use patch.dict with `clear=False` so other env survives; we just
        # blank the four we care about.
        with patch.dict(os.environ, clear, clear=False):
            for k in clear:
                # patch.dict sets to "" — we want them truly absent so the
                # `not value` check in cli triggers. Pop them.
                os.environ.pop(k, None)
            exit_code = main(
                ["init", "--seed", "42", "--scale", "tiny", "--no-llm"]
            )
        self.assertNotEqual(exit_code, 0)

    def test_init_live_mode_rejects_output(self) -> None:
        """Live + --output is rejected — they're mutually exclusive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "sample.json"
            # Even if env vars are unset, the --output check fires first
            # (it's syntactic; the env check is semantic).
            with patch.dict(
                os.environ,
                {
                    "DISCOURSE_HOST": "http://localhost:4200",
                    "DISCOURSE_API_KEY": "test-key",
                    "DISCOURSE_API_USERNAME": "admin",
                },
                clear=False,
            ):
                exit_code = main(
                    [
                        "init",
                        "--seed",
                        "42",
                        "--scale",
                        "tiny",
                        "--no-llm",
                        "--output",
                        str(output),
                    ]
                )
            self.assertNotEqual(exit_code, 0)
            # No file written.
            self.assertFalse(output.exists())


class ExtendCliTests(unittest.TestCase):
    """Smoke tests for the `extend` CLI path (Sits 15 + 16)."""

    def test_extend_topics_only_dry_run_writes_json(self) -> None:
        """`extend --add-topics 5 --dry-run` produces a well-formed JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--add-topics", "5",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            payload = json.loads(output.read_text())

            self.assertEqual(payload["base_seed"], 42)
            self.assertEqual(payload["base_scale"], "tiny")
            self.assertEqual(payload["base_product_name"], "crown-of-brine")
            self.assertEqual(payload["extend_seed"], 7)
            self.assertEqual(payload["add_topics_n"], 5)
            self.assertEqual(payload["add_replies_n"], 0)

            self.assertEqual(len(payload["new_topics"]), 5)
            self.assertGreater(len(payload["new_posts"]), 5)  # OPs + replies

    def test_extend_replies_only_dry_run_writes_json(self) -> None:
        """`extend --add-replies 10` writes appended replies, no new topics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--add-replies", "10",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text())

            self.assertEqual(payload["add_topics_n"], 0)
            self.assertEqual(payload["add_replies_n"], 10)
            self.assertEqual(len(payload["new_topics"]), 0)
            self.assertEqual(len(payload["new_posts"]), 10)

    def test_extend_mixed_mode_dry_run(self) -> None:
        """Both `--add-topics` and `--add-replies` together produce both."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--add-topics", "3",
                    "--add-replies", "7",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text())

            self.assertEqual(payload["add_topics_n"], 3)
            self.assertEqual(payload["add_replies_n"], 7)
            self.assertEqual(len(payload["new_topics"]), 3)
            self.assertGreater(len(payload["new_posts"]), 7)  # 3 OPs + replies + 7 appended

    def test_extend_live_mode_rejects_output(self) -> None:
        """Sit 15.1: live + --output is rejected (mutually exclusive)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--add-topics", "5",
                    "--no-llm",
                    "--output", str(output),
                ]
            )
            self.assertNotEqual(exit_code, 0)
            self.assertFalse(output.exists())

    def test_extend_dry_run_requires_output(self) -> None:
        """`--dry-run` without `--output` exits non-zero with a clear error."""
        exit_code = main(
            [
                "extend",
                "--seed", "42",
                "--scale", "tiny",
                "--extend-seed", "7",
                "--add-topics", "5",
                "--no-llm",
                "--dry-run",
            ]
        )
        self.assertNotEqual(exit_code, 0)

    def test_extend_zero_total_exits_nonzero(self) -> None:
        """Zero topics AND zero replies = no-op extension; CLI rejects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertNotEqual(exit_code, 0)

    def test_extend_release_burst_dry_run_writes_json(self) -> None:
        """`extend --release-burst remaster` (alone) produces a burst cluster (Sit 17)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--release-burst", "remaster",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text())
            self.assertEqual(payload["release_burst_version"], "remaster")
            self.assertGreaterEqual(len(payload["new_topics"]), 10)
            self.assertLessEqual(len(payload["new_topics"]), 20)
            for t in payload["new_topics"]:
                self.assertIn("remaster", t["tags"])

    def test_extend_release_burst_invalid_version_exits_nonzero(self) -> None:
        """An unknown version string is rejected before the build runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--release-burst", "not-a-real-version",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertNotEqual(exit_code, 0)
            self.assertFalse(output.exists())

    def test_extend_mixed_runs_all_three_subflows(self) -> None:
        """Sit 18 integration test: --mixed exercises add-topics +
        add-replies + release-burst with scale-derived defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "extension.json"
            exit_code = main(
                [
                    "extend",
                    "--seed", "42",
                    "--scale", "tiny",
                    "--extend-seed", "7",
                    "--mixed",
                    "--no-llm",
                    "--dry-run",
                    "--output", str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            payload = json.loads(output.read_text())
            # Tiny-scale defaults from `_MIXED_SCALE_DEFAULTS`.
            self.assertEqual(payload["add_topics_n"], 5)
            self.assertEqual(payload["add_replies_n"], 15)
            # Default version = last `game-version` axis entry.
            self.assertEqual(payload["release_burst_version"], "remaster")
            # All three sub-flows produced output: 5 add-topics + 10-20
            # burst topics; ≥5 add-topic OPs + 50-100 burst posts + 15
            # appended replies.
            self.assertGreaterEqual(len(payload["new_topics"]), 5 + 10)
            self.assertGreaterEqual(len(payload["new_posts"]), 5 + 50 + 15)


if __name__ == "__main__":
    unittest.main()
