import unittest
from unittest.mock import patch

from ui.strategy_activity import _fetch_live_channel_performance


class StrategyLivePerformanceTests(unittest.TestCase):
    def test_live_channel_performance_fetches_two_mature_windows_and_deltas(self):
        with patch(
            "ui.strategy_activity.connector._query_analytics",
            side_effect=[
                [{"views": 150, "estimatedMinutesWatched": 300, "subscribersGained": 6, "subscribersLost": 2}],
                [{"views": 100, "estimatedMinutesWatched": 200, "subscribersGained": 4, "subscribersLost": 1}],
            ],
        ) as query:
            result = _fetch_live_channel_performance(object())
        self.assertEqual(result["status"], "available")
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result["metrics"]["views"]["delta_percent"], 50.0)
        self.assertEqual(result["metrics"]["subscribersGained"]["delta_percent"], 50.0)
        self.assertIn("channel==MINE", result["source"])

    def test_missing_analytics_is_explicitly_unavailable(self):
        result = _fetch_live_channel_performance(None)
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
