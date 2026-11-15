"""Safety tests for the scheduled channel strategist."""

import unittest

from core.channel_strategy_agent import classify_metadata_change, _complete_reach_value, _benchmarkable_video_rows, _aggregate_video_analytics_rows
from youtube_analytics_connector import rank_channel_video_opportunities


class ChannelStrategyClassificationTests(unittest.TestCase):
    def test_title_case_and_whitespace_is_minor(self):
        classification, _, changed = classify_metadata_change(
            {"title": "Build AI Agents in Python!", "description": "Same", "tags": []},
            {"title": "  BUILD AI AGENTS IN PYTHON!  ", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "minor")
        self.assertEqual(changed, ["title"])

    def test_title_punctuation_change_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "GPT-4.1 for Python!", "description": "Same", "tags": []},
            {"title": "GPT 4 1 for Python", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")
    def test_title_word_rewrite_is_major(self):
        classification, _, changed = classify_metadata_change(
            {"title": "Build AI Agents in Python", "description": "Same", "tags": []},
            {"title": "Create Better AI Agents with Python", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")
        self.assertEqual(changed, ["title"])

    def test_emoji_change_is_major_even_when_words_match(self):
        classification, _, _ = classify_metadata_change(
            {"title": "Build AI Agents 🚀", "description": "Same", "tags": []},
            {"title": "Build AI Agents 🔥", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")

    def test_number_change_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "5 AI tools for students", "description": "Same", "tags": []},
            {"title": "7 AI tools for students", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")

    def test_two_source_grounded_added_tags_are_minor(self):
        classification, _, changed = classify_metadata_change(
            {"title": "AI tools", "description": "Learn Gemini and Python workflows", "tags": ["AI"]},
            {"title": "AI tools", "description": "Learn Gemini and Python workflows", "tags": ["AI", "Gemini", "Python"]},
            source_text="AI tools Learn Gemini and Python workflows",
        )
        self.assertEqual(classification, "minor")
        self.assertEqual(changed, ["tags"])

    def test_unsupported_tag_addition_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI"]},
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI", "Guaranteed income"]},
            source_text="AI tools Learn Gemini",
        )
        self.assertEqual(classification, "major")

    def test_tag_removal_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI", "Gemini"]},
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI"]},
            source_text="AI tools Learn Gemini",
        )
        self.assertEqual(classification, "major")

    def test_combined_field_change_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "AI tools!", "description": "Learn Gemini", "tags": ["AI"]},
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI", "Gemini"]},
            source_text="AI tools Learn Gemini",
        )
        self.assertEqual(classification, "major")

    def test_only_confirmed_standard_videos_enter_peer_benchmarks(self):
        rows = [
            {"video_id": "long0000001", "duration_seconds": 240, "format_group": "Standard videos"},
            {"video_id": "short000001", "duration_seconds": 45, "creator_content_type": "SHORTS", "format_group": "Shorts"},
            {"video_id": "unknown00001", "duration_seconds": 160, "creator_content_type": "UNSPECIFIED", "format_group": "Format unconfirmed"},
        ]
        benchmarkable = _benchmarkable_video_rows(rows)
        self.assertEqual([row["video_id"] for row in benchmarkable], ["long0000001"])
    def test_complete_reach_data_is_available_to_ranking(self):
        status = "Loaded daily reach reports for all 28 selected days. CTR is impression-weighted across those reports."
        self.assertEqual(_complete_reach_value(status, 1250), 1250)
        self.assertEqual(_complete_reach_value(status, 0.037), 0.037)

    def test_partial_reach_data_with_at_least_90_percent_coverage_is_usable(self):
        status = "Partial reach coverage: reports are available for 26 of 28 selected days."
        self.assertEqual(_complete_reach_value(status, 1250), 1250)
        self.assertEqual(_complete_reach_value(status, 0.037), 0.037)

    def test_partial_reach_data_below_90_percent_coverage_is_not_used(self):
        status = "Partial reach coverage: reports are available for 22 of 28 selected days."
        self.assertIsNone(_complete_reach_value(status, 1250))
        self.assertIsNone(_complete_reach_value(status, 0.037))

    def test_creator_content_type_metrics_are_aggregated_without_losing_format(self):
        rows = [
            {"creatorContentType": "SHORTS", "views": 60, "estimatedMinutesWatched": 30, "averageViewDuration": 30, "averageViewPercentage": 50, "likes": 4},
            {"creatorContentType": "SHORTS", "views": 40, "estimatedMinutesWatched": 20, "averageViewDuration": 30, "averageViewPercentage": 75, "likes": 2},
        ]
        metrics, content_type = _aggregate_video_analytics_rows(rows)
        self.assertEqual(content_type, "SHORTS")
        self.assertEqual(metrics["views"], 100)
        self.assertEqual(metrics["likes"], 6)
        self.assertAlmostEqual(metrics["averageViewPercentage"], 60.0)

    def test_mixed_creator_content_types_remain_unconfirmed(self):
        rows = [
            {"creatorContentType": "SHORTS", "views": 60},
            {"creatorContentType": "VIDEO_ON_DEMAND", "views": 40},
        ]
        metrics, content_type = _aggregate_video_analytics_rows(rows)
        self.assertEqual(content_type, "MIXED")
        self.assertEqual(metrics["views"], 100)

    def test_under_three_minute_uploads_can_be_ranked_in_their_own_duration_group(self):
        period = "2026-10-16..2026-11-12"
        rows = [
            {"video_id": "short000001", "format_group": "Under-3-minute uploads (format unconfirmed)", "ctr_period": period, "ctr_percent": 1.2, "impressions": 250, "views": 300, "average_view_percentage": 40},
            {"video_id": "short000002", "format_group": "Under-3-minute uploads (format unconfirmed)", "ctr_period": period, "ctr_percent": 5.0, "impressions": 250, "views": 300, "average_view_percentage": 60},
            {"video_id": "short000003", "format_group": "Under-3-minute uploads (format unconfirmed)", "ctr_period": period, "ctr_percent": 5.5, "impressions": 250, "views": 300, "average_view_percentage": 55},
            {"video_id": "short000004", "format_group": "Under-3-minute uploads (format unconfirmed)", "ctr_period": period, "ctr_percent": 4.8, "impressions": 250, "views": 300, "average_view_percentage": 58},
        ]
        ranked = rank_channel_video_opportunities(rows, limit=10)
        self.assertEqual([item["video_id"] for item in ranked], ["short000001"])
        self.assertTrue(ranked[0]["reasons"][0].startswith("Thumbnail CTR is "))

    def test_missing_reach_metric_stays_unavailable_even_with_complete_coverage(self):
        status = "Loaded daily reach reports for all 28 selected days."
        self.assertIsNone(_complete_reach_value(status, None))

    def test_identical_metadata_is_no_change(self):
        current = {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI"]}
        self.assertEqual(classify_metadata_change(current, current)[0], "none")


if __name__ == "__main__":
    unittest.main()
