import os
import unittest
from unittest.mock import patch

from ui.strategy_activity import _fetch_live_channel_performance


class StrategyLivePerformanceTests(unittest.TestCase):
    def test_live_channel_performance_fetches_two_mature_windows_and_deltas(self):
        with patch(
            "ui.strategy_activity.connector._query_analytics",
            side_effect=[
                [{
                    "views": 150,
                    "estimatedMinutesWatched": 300,
                    "subscribersGained": 6,
                    "subscribersLost": 2,
                }],
                [{
                    "views": 100,
                    "estimatedMinutesWatched": 200,
                    "subscribersGained": 4,
                    "subscribersLost": 1,
                }],
            ],
        ) as query:
            result = _fetch_live_channel_performance(object())

        self.assertEqual(result["status"], "available")
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result["metrics"]["views"]["delta_percent"], 50.0)
        self.assertEqual(result["metrics"]["subscribersGained"]["delta_percent"], 50.0)
        self.assertIn("channel==MINE", result["source"])

    def test_channel_overview_uses_live_review_count_and_explains_saved_performance(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "strategy_activity.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('top_cols[3].metric("Pending now", pending_total_now)', source)
        self.assertIn("Current open review queue", source)
        self.assertIn("stored in the latest completed Channel Strategist scan", source)
        self.assertIn("Fetch latest available channel performance", source)
        self.assertIn("does not contain thumbnail impressions or CTR", source)


if __name__ == "__main__":
    unittest.main()
