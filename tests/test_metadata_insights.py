import json
import unittest
from datetime import date
from unittest.mock import Mock, patch

from core.llm import LLMError

import youtube_analytics_connector as connector
from core.metadata.insights import (
    fetch_video_performance,
    generate_metadata_insights,
    metadata_standard_checks,
)


def resource():
    return {
        "snippet": {
            "title": "AI agents explained",
            "description": "Learn how AI agents work.",
            "tags": ["AI agents", "AI agent tutorial"],
            "categoryId": "28",
            "defaultLanguage": "en",
        }
    }


class MetadataInsightsTests(unittest.TestCase):
    def test_metadata_checks_use_youtube_character_and_byte_limits(self):
        value = resource()
        value["snippet"]["title"] = "x" * 101
        value["snippet"]["description"] = "🙂" * 1251
        value["snippet"]["tags"] = ["tag"] * 130
        checks = {item["field"]: item for item in metadata_standard_checks(value)}
        self.assertEqual(checks["title"]["status"], "error")
        self.assertIn("100", checks["title"]["detail"])
        self.assertEqual(checks["description"]["status"], "error")
        self.assertIn("bytes", checks["description"]["detail"])
        self.assertEqual(checks["tags"]["status"], "error")
        self.assertIn("500", checks["tags"]["detail"])

    def test_metadata_checks_flag_duplicate_tags_and_do_not_overstate_tag_importance(self):
        value = resource()
        value["snippet"]["tags"] = ["ChatGPT", "chatgpt"]
        checks = {item["field"]: item for item in metadata_standard_checks(value)}
        self.assertEqual(checks["tags"]["status"], "warning")
        self.assertIn("minimal role", checks["tags"]["detail"])

    def test_performance_fetch_compares_current_and_previous_mature_periods(self):
        rows = [
            {"views": 120, "estimatedMinutesWatched": 60, "averageViewPercentage": 45,
             "subscribersGained": 6, "subscribersLost": 1},
            {"views": 100, "estimatedMinutesWatched": 50, "averageViewPercentage": 40,
             "subscribersGained": 4, "subscribersLost": 2},
        ]
        analytics = object()
        with patch(
            "core.metadata.insights.connector._query_analytics",
            side_effect=[[rows[0]], [rows[1]]],
        ) as query:
            performance = fetch_video_performance(
                analytics, "abcdefghijk", today=date(2026, 11, 18)
            )
        self.assertEqual(performance["status"], "available")
        self.assertEqual(performance["current_period"], ["2026-10-19", "2026-11-15"])
        self.assertEqual(performance["previous_period"], ["2026-09-21", "2026-10-18"])
        self.assertEqual(performance["metrics"]["views"]["delta_percent"], 20.0)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args_list[0].kwargs["video_id"], "abcdefghijk")

    def test_missing_analytics_returns_explicit_unavailable_status(self):
        result = fetch_video_performance(None, "abcdefghijk", today=date(2026, 11, 18))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("not connected", result["reason"])

    def test_ai_recommendations_are_validated_and_limited_to_safe_text_fields(self):
        response = {
            "summary": "The description is sparse.",
            "findings": [{"field": "description", "severity": "medium", "issue": "No context"}],
            "recommendations": [
                {
                    "field": "title",
                    "issue": "The title can be clearer.",
                    "recommendation": "Describe the value accurately.",
                    "evidence": "The current title is broad.",
                    "suggested_value": "How AI Agents Work",
                    "confidence": 0.93,
                },
                {
                    "field": "status",
                    "suggested_value": {"privacyStatus": "public"},
                    "confidence": 1.0,
                },
                {
                    "field": "title",
                    "suggested_value": "x" * 101,
                    "confidence": 0.9,
                },
            ],
        }
        router = Mock()
        router.generate.return_value = (json.dumps(response), "Groq", "test-model")
        with patch("core.metadata.insights.LLMRouter", return_value=router):
            result = generate_metadata_insights({}, resource(), {"status": "available"})
        self.assertEqual(result["analysis_status"], "ai_and_rules")
        self.assertEqual(len(result["recommendations"]), 1)
        self.assertEqual(result["recommendations"][0]["field"], "title")
        self.assertEqual(result["provider"], "Groq")

    def test_provider_failure_preserves_deterministic_checks(self):
        router = Mock()
        router.generate.side_effect = LLMError("providers unavailable")
        with patch("core.metadata.insights.LLMRouter", return_value=router):
            result = generate_metadata_insights({}, resource(), {"status": "unavailable"})
        self.assertEqual(result["analysis_status"], "rules_only")
        self.assertTrue(result["standard_checks"])
        self.assertEqual(result["recommendations"], [])


if __name__ == "__main__":
    unittest.main()
