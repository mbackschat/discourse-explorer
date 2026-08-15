"""Tests for visualize.py time-window helpers.

These cover the small pure functions added for the time-window slider:
ISO-8601 → Unix-second parsing and topic-id-list → (tMin, tMax) bounds.
"""
import unittest

from discourse_explorer.visualize import (
    _parse_topic_ts,
    _bounds_for_topics,
    _ts_to_month_bin,
)


class TestParseTopicTs(unittest.TestCase):
    def test_parses_discourse_iso_with_z(self):
        # Discourse ships createdAt like "2022-11-04T09:27:09.387Z".
        ts = _parse_topic_ts("2022-11-04T09:27:09.387Z")
        self.assertEqual(ts, 1667554029)

    def test_parses_iso_with_explicit_offset(self):
        ts = _parse_topic_ts("2022-11-04T09:27:09+00:00")
        self.assertEqual(ts, 1667554029)

    def test_returns_none_for_empty(self):
        self.assertIsNone(_parse_topic_ts(""))
        self.assertIsNone(_parse_topic_ts(None))

    def test_returns_none_for_garbage(self):
        self.assertIsNone(_parse_topic_ts("not-a-date"))


class TestBoundsForTopics(unittest.TestCase):
    def setUp(self):
        # Three topics: 2018-06, 2022-11, 2024-09 (rough Unix-second values).
        self.topic_to_ts = {
            "1001": 1528000000,  # 2018-06
            "1234": 1667554029,  # 2022-11
            "2000": 1725000000,  # 2024-08
        }

    def test_single_topic(self):
        self.assertEqual(_bounds_for_topics(["1234"], self.topic_to_ts),
                         (1667554029, 1667554029))

    def test_multi_topic_returns_min_max(self):
        bounds = _bounds_for_topics(["1234", "1001", "2000"], self.topic_to_ts)
        self.assertEqual(bounds, (1528000000, 1725000000))

    def test_unknown_ids_skipped(self):
        bounds = _bounds_for_topics(["1234", "9999"], self.topic_to_ts)
        self.assertEqual(bounds, (1667554029, 1667554029))

    def test_empty_returns_none(self):
        self.assertIsNone(_bounds_for_topics([], self.topic_to_ts))
        self.assertIsNone(_bounds_for_topics(["9999"], self.topic_to_ts))


class TestMonthBin(unittest.TestCase):
    """Month bin = months since 2018-01 (epoch). Slider step = 1 month."""

    def test_epoch_is_zero(self):
        # 2018-01-01T00:00:00Z = 1514764800
        self.assertEqual(_ts_to_month_bin(1514764800), 0)

    def test_2018_06(self):
        # 2018-06-04 ~ month index 5
        self.assertEqual(_ts_to_month_bin(1528070400), 5)

    def test_2022_11(self):
        # 2022-11-04 = 58 months after 2018-01
        self.assertEqual(_ts_to_month_bin(1667554029), 58)

    def test_pre_epoch_clamped_to_zero(self):
        # 2017-12 falls before epoch — clamp to 0 rather than negative.
        self.assertEqual(_ts_to_month_bin(1500000000), 0)


if __name__ == "__main__":
    unittest.main()
