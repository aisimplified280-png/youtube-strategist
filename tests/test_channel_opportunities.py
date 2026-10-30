"""Tests for evidence-backed channel video opportunity ranking."""

import unittest
from unittest.mock import MagicMock

import youtube_analytics_connector as connector


PERIOD = "2026-10-01 to 2026-10-28"


class ChannelOpportunityRankingTests(unittest.TestCase):
    def test_ctr_recommendation_uses_same_period_format_cohort(self):
        rows = [
            {
                "video_id": "aaaaaaaaaaa",
                "title": "Low CTR tutorial",
                "format_group": "Standard videos",
                "views": 1000,
                "average_view_percentage": 45.0,
                "impressions": 500,
                "ctr_percent": 1.0,
                "ctr_source": "reach-october.csv",
                "ctr_period": PERIOD,
            },
            *[
                {
                    "video_id": video_id,
                    "title": title,
                    "format_group": "Standard videos",
                    "views": 1000,
                    "average_view_percentage": 45.0,
                    "impressions": 500,
                    "ctr_percent": ctr,
                    "ctr_source": "reach-october.csv",
                    "ctr_period": PERIOD,
                }
                for video_id, title, ctr in (
                    ("bbbbbbbbbbb", "Peer one", 4.0),
                    ("ccccccccccc", "Peer two", 4.5),
                    ("ddddddddddd", "Peer three", 5.0),
                )
            ],
        ]

        ranked = connector.rank_channel_video_opportunities(rows)

        self.assertEqual([row["video_id"] for row in ranked], ["aaaaaaaaaaa"])
        self.assertIn("Thumbnail CTR is 1.00%", ranked[0]["reasons"][0])
        self.assertIn("3 same-period comparable videos", ranked[0]["reasons"][0])
        self.assertEqual(ranked[0]["priority"], "High")

    def test_ctr_is_not_compared_across_different_studio_periods(self):
        rows = [
            {
                "video_id": "aaaaaaaaaaa",
                "title": "CTR candidate",
                "format_group": "Standard videos",
                "views": 1000,
                "average_view_percentage": 45.0,
                "impressions": 500,
                "ctr_percent": 1.0,
                "ctr_source": "old-period.csv",
                "ctr_period": "2026-09-01 to 2026-09-28",
            },
            *[
                {
                    "video_id": video_id,
                    "title": title,
                    "format_group": "Standard videos",
                    "views": 1000,
                    "average_view_percentage": 45.0,
                    "impressions": 500,
                    "ctr_percent": ctr,
                    "ctr_source": "new-period.csv",
                    "ctr_period": PERIOD,
                }
                for video_id, title, ctr in (
                    ("bbbbbbbbbbb", "Peer one", 4.0),
                    ("ccccccccccc", "Peer two", 4.5),
                    ("ddddddddddd", "Peer three", 5.0),
                )
            ],
        ]

        self.assertEqual(connector.rank_channel_video_opportunities(rows), [])

    def test_views_only_opportunity_does_not_invent_ctr(self):
        rows = [
            {
                "video_id": "aaaaaaaaaaa",
                "title": "Low views candidate",
                "format_group": "Standard videos",
                "views": 100,
                "average_view_percentage": 45.0,
            },
            *[
                {
                    "video_id": video_id,
                    "title": title,
                    "format_group": "Standard videos",
                    "views": views,
                    "average_view_percentage": 45.0,
                }
                for video_id, title, views in (
                    ("bbbbbbbbbbb", "Peer one", 1000),
                    ("ccccccccccc", "Peer two", 1100),
                    ("ddddddddddd", "Peer three", 1200),
                )
            ],
        ]

        ranked = connector.rank_channel_video_opportunities(rows)

        self.assertEqual([row["video_id"] for row in ranked], ["aaaaaaaaaaa"])
        self.assertTrue(any("views in the scan period" in reason for reason in ranked[0]["reasons"]))
        self.assertFalse(any("CTR" in reason for reason in ranked[0]["reasons"]))

    def test_channel_analytics_query_uses_valid_multi_video_filter(self):
        analytics = MagicMock()
        analytics.reports().query.return_value.execute.return_value = {
            "columnHeaders": [{"name": "video"}, {"name": "views"}],
            "rows": [["aaaaaaaaaaa", 12], ["bbbbbbbbbbb", 20]],
        }

        rows = connector._query_analytics(
            analytics,
            "2026-10-01",
            "2026-10-28",
            "views",
            video_ids=["aaaaaaaaaaa", "bbbbbbbbbbb", "aaaaaaaaaaa"],
            dimensions="video",
        )

        kwargs = analytics.reports().query.call_args.kwargs
        self.assertEqual(kwargs["filters"], "video==aaaaaaaaaaa,bbbbbbbbbbb")
        self.assertEqual(kwargs["dimensions"], "video")
        self.assertEqual(rows[0], {"video": "aaaaaaaaaaa", "views": 12})

    def test_channel_analytics_query_rejects_mixed_id_parameters(self):
        analytics = MagicMock()
        with self.assertRaises(ValueError):
            connector._query_analytics(
                analytics,
                "2026-10-01",
                "2026-10-28",
                "views",
                video_id="aaaaaaaaaaa",
                video_ids=["bbbbbbbbbbb"],
            )


if __name__ == "__main__":
    unittest.main()
