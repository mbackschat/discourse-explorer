"""Tests for `sample.seed.generators.timeline`.

Pragmatic posture (per `sample/CLAUDE.md`): cover the invariants that
matter — determinism, range, sortedness, length, the burst-density signal
(the *whole point* of the generator), validation guards — and skip the
rest.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from sample.seed.generators.timeline import (
    Timeline,
    _validate_release_events,
    make_timeline,
)
from sample.seed.product import crown_of_brine
from sample.seed.universe import GenerationSpec


def _spec(seed: int, scale: str = "tiny") -> GenerationSpec:
    return GenerationSpec(seed=seed, scale=scale, product=crown_of_brine)


class TimelineTests(unittest.TestCase):
    def test_determinism(self) -> None:
        """Same seed + total -> identical offsets list (full equality)."""
        a = make_timeline(_spec(42), total_topics=200)
        b = make_timeline(_spec(42), total_topics=200)
        self.assertEqual(a.offsets, b.offsets)
        # base_epoch is also equal because both samples read the same midnight.
        self.assertEqual(a.base_epoch, b.base_epoch)

    def test_offsets_in_range(self) -> None:
        """Every offset lies in `[0, 365]` (inclusive)."""
        timeline = make_timeline(_spec(42), total_topics=500)
        for off in timeline.offsets:
            with self.subTest(offset=off):
                self.assertGreaterEqual(off, 0)
                self.assertLessEqual(off, 365)

    def test_offsets_sorted(self) -> None:
        """Iterating topics by index yields chronological timestamps."""
        timeline = make_timeline(_spec(42), total_topics=200)
        self.assertEqual(list(timeline.offsets), sorted(timeline.offsets))

    def test_length_matches_total(self) -> None:
        """The pre-computed list has exactly `total_topics` entries."""
        for total in (1, 30, 150, 500):
            with self.subTest(total=total):
                timeline = make_timeline(_spec(42), total_topics=total)
                self.assertEqual(len(timeline.offsets), total)
                self.assertEqual(timeline.total_topics, total)

    def test_burst_density_invariant(self) -> None:
        """At medium scale (500 topics), there are ≥3 dense windows.

        Methodology:
            - Bucket the 365-day window into 7-day buckets (53 buckets).
            - Baseline = total_topics / num_buckets.
            - A bucket is "dense" if its count exceeds 2× baseline.
            - Expect ≥3 dense buckets — corresponding to release-event
              clusters surviving the law-of-large-numbers averaging.

        Threshold rationale: with 4 release events at ~50% burst probability
        over 500 topics, each event should attract ~62 topics within ±3 days
        (one bucket). Baseline is 500/53 ≈ 9.4, so a dense bucket needs > 19
        topics. The 4 release-event buckets should comfortably clear that;
        we assert ≥3 (not ≥4) to leave room for an unlucky seed where one
        event's draws happen to spill into adjacent buckets.
        """
        total = 500
        timeline = make_timeline(_spec(42, scale="medium"), total_topics=total)
        bucket_size = 7
        num_buckets = (365 // bucket_size) + 1  # 53 buckets covering [0, 365]
        counts = [0] * num_buckets
        for off in timeline.offsets:
            counts[off // bucket_size] += 1
        baseline = total / num_buckets
        dense = [c for c in counts if c > 2 * baseline]
        self.assertGreaterEqual(
            len(dense),
            3,
            f"expected ≥3 dense windows (>{2 * baseline:.1f} topics each), "
            f"got {len(dense)}; bucket counts = {counts}",
        )

    def test_seed_varies(self) -> None:
        """Different seeds yield at least one differing offset."""
        a = make_timeline(_spec(42), total_topics=200)
        b = make_timeline(_spec(99), total_topics=200)
        self.assertNotEqual(
            a.offsets,
            b.offsets,
            "expected seeds 42 and 99 to produce different offsets",
        )

    def test_is_burst_window_on_event(self) -> None:
        """A datetime exactly on a release-event date returns True."""
        timeline = make_timeline(_spec(42), total_topics=30)
        for event_date in timeline.release_event_dates():
            with self.subTest(event=event_date):
                self.assertTrue(timeline.is_burst_window(event_date))

    def test_is_burst_window_far_from_event(self) -> None:
        """A datetime 30 days from any release event returns False.

        We construct a "far" date by taking each event date and shifting it
        +30 days, then verifying that shift lands at least 7 days away from
        every event. The smallest gap between adjacent events in the default
        constants is ~95 days, so +30 is comfortably outside any ±3-day
        radius for the next event too.
        """
        timeline = make_timeline(_spec(42), total_topics=30)
        events = timeline.release_event_dates()
        for event_date in events:
            far = event_date + timedelta(days=30)
            # Sanity: confirm `far` really is >7 days from every event before
            # asserting it's not in any burst window — saves a future
            # constants edit silently violating this test's premise.
            min_gap = min(
                abs((far - e).total_seconds()) / 86400.0 for e in events
            )
            self.assertGreater(
                min_gap,
                7,
                f"test premise broken: {far} is only {min_gap:.1f} days from "
                "the nearest event; pick a wider offset",
            )
            with self.subTest(event=event_date):
                self.assertFalse(timeline.is_burst_window(far))

    def test_timestamp_for_topic_arithmetic(self) -> None:
        """`timestamp_for_topic(i) == base_epoch + timedelta(days=offsets[i])`."""
        timeline = make_timeline(_spec(42), total_topics=50)
        for i, off in enumerate(timeline.offsets):
            with self.subTest(i=i):
                expected = timeline.base_epoch + timedelta(days=off)
                self.assertEqual(timeline.timestamp_for_topic(i), expected)

    def test_offset_for_topic_out_of_range(self) -> None:
        """Out-of-range topic indices raise `IndexError`."""
        timeline = make_timeline(_spec(42), total_topics=10)
        with self.assertRaises(IndexError):
            timeline.offset_for_topic(10)
        with self.assertRaises(IndexError):
            timeline.offset_for_topic(-1)

    def test_invalid_total_topics_raises(self) -> None:
        """`make_timeline` with `total_topics <= 0` raises `ValueError`."""
        with self.assertRaises(ValueError):
            make_timeline(_spec(42), total_topics=0)
        with self.assertRaises(ValueError):
            make_timeline(_spec(42), total_topics=-5)

    def test_validate_release_events_offset_too_high(self) -> None:
        """An offset > 365 in `RELEASE_EVENTS` is rejected."""
        with self.assertRaises(ValueError):
            _validate_release_events([(0, "a"), (180, "b"), (400, "c")])

    def test_validate_release_events_offset_negative(self) -> None:
        """A negative offset in `RELEASE_EVENTS` is rejected."""
        with self.assertRaises(ValueError):
            _validate_release_events([(-1, "a"), (180, "b"), (300, "c")])

    def test_validate_release_events_too_few_distinct_days(self) -> None:
        """Fewer than 3 distinct days is rejected (burst-density floor)."""
        with self.assertRaises(ValueError):
            _validate_release_events([(10, "a"), (10, "b")])
        # Three entries but only two distinct days -> still rejected.
        with self.assertRaises(ValueError):
            _validate_release_events([(10, "a"), (10, "b"), (200, "c")])

    def test_validate_release_events_empty(self) -> None:
        """An empty `RELEASE_EVENTS` is rejected."""
        with self.assertRaises(ValueError):
            _validate_release_events([])

    def test_module_load_validation(self) -> None:
        """Re-importing the timeline module re-validates the default product.

        Smoke-checks that `crown_of_brine.RELEASE_EVENTS` passes module-load
        validation. This is the kind of regression a future constants edit
        would silently introduce. We re-import in a fresh subprocess so that
        a successful reload doesn't leak a new `Timeline` class identity
        into other tests in this module (which would break `isinstance`
        checks against the symbol imported at file top).
        """
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib; "
                "import sample.seed.generators.timeline as tl; "
                "importlib.reload(tl); "
                "assert callable(tl.make_timeline)",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"reload subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )

    def test_release_event_dates_sorted(self) -> None:
        """`release_event_dates()` returns dates in ascending order."""
        timeline = make_timeline(_spec(42), total_topics=30)
        dates = timeline.release_event_dates()
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(dates), len(crown_of_brine.RELEASE_EVENTS))

    def test_timeline_is_frozen(self) -> None:
        """`Timeline` is frozen — defends against accidental mutation."""
        timeline = make_timeline(_spec(42), total_topics=10)
        with self.assertRaises(Exception):
            timeline.total_topics = 99  # type: ignore[misc]

    def test_returns_timeline_instance(self) -> None:
        """Output is a `Timeline` instance."""
        timeline = make_timeline(_spec(42), total_topics=10)
        self.assertIsInstance(timeline, Timeline)


if __name__ == "__main__":
    unittest.main()
