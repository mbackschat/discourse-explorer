"""§12 documents how §4.4 is extracted. These tests bind the doc to the code.

The provenance line drifted twice from what `scan_topics` actually does: it
kept naming `tags[].name` after the extraction moved to `config.tag_label`, and
it missed the `-` added to `_VERSION_TAG_RE` for slugs. Following it reproduced
the very per-release double count §4.4 warns about, so a reader auditing the
numbers would conclude the guide was wrong when it was right.

The regex half is now interpolated and cannot drift. These tests cover what
interpolation can't reach: that the rendered text still names the right source,
that the documented pattern is executable, and that the extraction really keys
on the slug.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from discourse_explorer.derive_query_guide import (
    GraphStats,
    GuideInputs,
    TopicStats,
    VerbStats,
    _VERSION_TAG_RE,
    compose_sections_7_to_12,
    scan_topics,
)

# Pulls the regex back OUT of the rendered markdown, so the assertions run
# against what a reader would actually copy. The pattern holds no `'`.
_DOCUMENTED_REGEX_RE = re.compile(r"matching `r'([^']+)'`")


def _empty_inputs() -> GuideInputs:
    return GuideInputs(
        graph=GraphStats(0, 0, [], [], 0, 0, 0),
        topics=TopicStats(0, [], []),
        verbs=VerbStats([], [], 0),
        extraction_model="test",
        query_model="test",
        vocab={},
        snapshot_date="2026-08-15",
    )


def _version_provenance_line() -> str:
    rendered = compose_sections_7_to_12(_empty_inputs())
    return next(
        line for line in rendered.splitlines()
        if line.startswith("- **Per-version counts")
    )


class VersionProvenanceDocTests(unittest.TestCase):
    def test_documented_regex_is_the_compiled_one(self):
        """Guards against someone re-typing the pattern as a literal."""
        match = _DOCUMENTED_REGEX_RE.search(_version_provenance_line())
        self.assertIsNotNone(match, "§12 no longer renders a copyable regex")
        self.assertEqual(match.group(1), _VERSION_TAG_RE.pattern)

    def test_documented_regex_matches_every_spelling_it_claims(self):
        """The doc is executable, not just equal to a string."""
        documented = re.compile(
            _DOCUMENTED_REGEX_RE.search(_version_provenance_line()).group(1))
        for spelling in ("2025-06", "2025.06", "2025․06"):
            with self.subTest(spelling=spelling):
                self.assertTrue(documented.match(spelling))
        for other in ("1999-06", "2025_06", "kernel"):
            with self.subTest(other=other):
                self.assertIsNone(documented.match(other))

    def test_provenance_names_the_slug_not_the_display_name(self):
        """The half interpolation can't reach: which field is read."""
        line = _version_provenance_line()
        self.assertIn("config.tag_label(tag)", line)
        self.assertNotIn("tags[].name", line)


class VersionExtractionKeysOnSlugTests(unittest.TestCase):
    def test_one_release_scraped_in_two_eras_counts_once(self):
        """Without this, §12 could describe a `tag_label` the code stopped using.

        Tag id=144 really does appear as name `2025․06` (April scrape) and
        `2025-06` (August scrape). Keying on `name` yields two rows of 1.
        """
        with tempfile.TemporaryDirectory() as tmp:
            topics = Path(tmp)
            for i, display in enumerate(("2025․06", "2025-06")):
                (topics / f"{i}.json").write_text(json.dumps({
                    "category_name": "Kernel",
                    "tags": [{"id": 144, "slug": "2025-06", "name": display}],
                }))

            stats = scan_topics(topics)

        self.assertEqual(stats.topics_total, 2)
        self.assertEqual(stats.by_version, [("2025-06", 2)])


if __name__ == "__main__":
    unittest.main()
