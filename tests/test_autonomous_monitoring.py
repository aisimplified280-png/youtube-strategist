"""Comprehensive unit and integration tests for the Autonomous YouTube Metadata Engine."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from core.channel_strategy_agent import (
    classify_metadata_change,
    collect_channel_snapshot,
    evaluate_autonomous_policy,
    generate_strategy_report,
    list_channel_upload_ids,
)
from core.strategy_learning import (
    classify_evaluation_outcome,
    compute_metric_deltas,
    format_learning_feedback_for_prompt,
    rollback_strategy_action,
)


class TestFullCataloguePagination(TestCase):
    def test_list_channel_upload_ids_full_pagination_beyond_500(self):
        youtube = MagicMock()
        # Mock channels().list().execute()
        channels_mock = MagicMock()
        channels_mock.list.return_value.execute.return_value = {
            "items": [{
                "id": "UC_TEST_CHANNEL",
                "contentDetails": {"relatedPlaylists": {"uploads": "UU_TEST_PLAYLIST"}},
            }]
        }
        youtube.channels.return_value = channels_mock

        # Generate 600 unique video IDs across multiple pages
        all_ids = [f"VID_{i:07d}" for i in range(600)]
        playlist_items_mock = MagicMock()

        def side_effect(part, playlistId, maxResults, pageToken=None):
            req = MagicMock()
            start = int(pageToken) if pageToken else 0
            end = min(start + maxResults, len(all_ids))
            items = [{"contentDetails": {"videoId": vid}} for vid in all_ids[start:end]]
            next_token = str(end) if end < len(all_ids) else None
            req.execute.return_value = {
                "items": items,
                "nextPageToken": next_token,
            }
            return req

        playlist_items_mock.list.side_effect = side_effect
        youtube.playlistItems.return_value = playlist_items_mock

        # When limit is None, should fetch all 600 videos and not be truncated
        ids, truncated = list_channel_upload_ids(youtube, "UC_TEST_CHANNEL", limit=None)
        self.assertEqual(len(ids), 600)
        self.assertFalse(truncated)

    def test_list_channel_upload_ids_bounded_limit(self):
        youtube = MagicMock()
        channels_mock = MagicMock()
        channels_mock.list.return_value.execute.return_value = {
            "items": [{
                "id": "UC_TEST_CHANNEL",
                "contentDetails": {"relatedPlaylists": {"uploads": "UU_TEST_PLAYLIST"}},
            }]
        }
        youtube.channels.return_value = channels_mock

        all_ids = [f"VID_{i:07d}" for i in range(100)]
        playlist_items_mock = MagicMock()

        def side_effect(part, playlistId, maxResults, pageToken=None):
            req = MagicMock()
            start = int(pageToken) if pageToken else 0
            end = min(start + maxResults, len(all_ids))
            items = [{"contentDetails": {"videoId": vid}} for vid in all_ids[start:end]]
            next_token = str(end) if end < len(all_ids) else None
            req.execute.return_value = {
                "items": items,
                "nextPageToken": next_token,
            }
            return req

        playlist_items_mock.list.side_effect = side_effect
        youtube.playlistItems.return_value = playlist_items_mock

        ids, truncated = list_channel_upload_ids(youtube, "UC_TEST_CHANNEL", limit=50)
        self.assertEqual(len(ids), 50)
        self.assertTrue(truncated)


class TestCadenceAndDiagnosis(TestCase):
    @patch("core.channel_strategy_agent.list_channel_upload_ids")
    @patch("youtube_dashboard.fetch_reach_metrics")
    @patch("core.channel_strategy_agent._query_or_status")
    def test_collect_channel_snapshot_monthly_cadence_and_diagnosis(
        self, mock_query, mock_reach, mock_list_ids
    ):
        mock_list_ids.return_value = (["kLK0N14PT1Y", "jJbg1WtvxbM"], False)
        youtube = MagicMock()
        # Mock video list
        youtube.videos().list().execute.return_value = {
            "items": [
                {
                    "id": "kLK0N14PT1Y",
                    "snippet": {"title": "RAG Pipeline Fixes", "channelId": "UC123", "publishedAt": "2026-09-01T00:00:00Z"},
                    "contentDetails": {"duration": "PT8M30S"},
                    "statistics": {"viewCount": "5000"},
                },
                {
                    "id": "jJbg1WtvxbM",
                    "snippet": {"title": "AI Short Idea", "channelId": "UC123", "publishedAt": "2026-09-01T00:00:00Z"},
                    "contentDetails": {"duration": "PT45S"},
                    "statistics": {"viewCount": "1200"},
                },
            ]
        }
        mock_query.return_value = ([
            {
                "video": "kLK0N14PT1Y",
                "views": 1500,
                "estimatedMinutesWatched": 6000,
                "averageViewDuration": 240,
                "averageViewPercentage": 47.0,
                "likes": 50,
                "comments": 10,
                "shares": 5,
                "subscribersGained": 25,
                "subscribersLost": 2,
            },
            {
                "video": "jJbg1WtvxbM",
                "views": 1200,
                "estimatedMinutesWatched": 600,
                "averageViewDuration": 30,
                "averageViewPercentage": 66.0,
                "likes": 80,
                "comments": 5,
                "shares": 2,
                "subscribersGained": 10,
                "subscribersLost": 1,
            },
        ], "available")

        mock_reach.return_value = ({
            "kLK0N14PT1Y": {"impressions": 10000, "ctr": 2.2, "ctr_status": "complete"},
            "jJbg1WtvxbM": {"impressions": 5000, "ctr": 5.0, "ctr_status": "complete"},
        }, "Loaded daily reach reports for all 28 days")

        snapshot = collect_channel_snapshot(
            youtube, MagicMock(), MagicMock(), MagicMock(),
            channel_id="UC123",
            today=date(2026, 11, 15),
            cadence="monthly",
        )

        self.assertEqual(snapshot["cadence"], "monthly")
        self.assertEqual(snapshot["metrics_period"]["current_start"], "2026-10-01")
        self.assertEqual(snapshot["metrics_period"]["current_end"], "2026-10-31")

        # Video diagnosis checks
        rows = snapshot["video_rows"]
        rag_video = next(r for r in rows if r["video_id"] == "kLK0N14PT1Y")
        short_video = next(r for r in rows if r["video_id"] == "jJbg1WtvxbM")

        # Standard video with low CTR (< median) and sufficient impressions -> packaging
        self.assertEqual(rag_video["diagnosis"], "packaging")
        # Under 180s video -> format_unconfirmed
        self.assertEqual(short_video["diagnosis"], "format_unconfirmed")


class TestAutonomousPolicy(TestCase):
    def setUp(self):
        self.settings = {
            "automation_mode": "autonomous",
            "emergency_pause": False,
            "min_confidence_auto_publish": 0.85,
            "max_daily_minor_publishes": 3,
            "max_weekly_publishes": 5,
            "max_monthly_publishes": 10,
        }
        self.before = {
            "title": "Why Your RAG Pipeline Still Misses Answers",
            "description": "Original description covering embeddings and hybrid search.",
            "tags": ["rag", "ai", "llm"],
        }
        self.proposed = {
            "title": "Why RAG Still Fails in Production (And the Fix)",
            "description": "Updated description with clear technical breakdown.\n\nOriginal description covering embeddings and hybrid search.",
            "tags": ["rag", "ai", "llm", "vector-db", "retrieval"],
        }
        self.decision = {
            "video_id": "kLK0N14PT1Y",
            "confidence": 0.90,
            "decision": "auto_publish",
            "diagnosis": "packaging",
            "cadence": "daily",
            "rationale": "Directly addresses 2.2% CTR bottleneck with proven production framing.",
        }

    def test_autonomous_mode_approves_multi_field_when_gates_pass(self):
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
            daily_count=0,
            cadence_count=0,
        )
        self.assertEqual(status, "auto_publish")
        self.assertIn("title", changed)
        self.assertIn("description", changed)
        self.assertIn("tags", changed)

    def test_emergency_pause_holds_for_review(self):
        self.settings["emergency_pause"] = True
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
        )
        self.assertEqual(status, "pending_review")
        self.assertIn("emergency pause", reason.lower())

    def test_paused_mode_holds_for_review(self):
        self.settings["automation_mode"] = "paused"
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
        )
        self.assertEqual(status, "pending_review")
        self.assertIn("paused by owner", reason)

    def test_shadow_mode_records_shadow_publish(self):
        self.settings["automation_mode"] = "shadow"
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
        )
        self.assertEqual(status, "shadow_publish")
        self.assertIn("Shadow mode active", reason)

    def test_cooldown_failure_holds_for_review(self):
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=False,
            cooldown_reason="Metadata modified 3 days ago.",
        )
        self.assertEqual(status, "pending_review")
        self.assertIn("cooldown", reason.lower())

    def test_daily_cap_exceeded_holds_for_review(self):
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
            daily_count=3,
        )
        self.assertEqual(status, "pending_review")
        self.assertIn("daily limit", reason.lower())

    def test_confidence_below_threshold_holds_for_review(self):
        self.decision["confidence"] = 0.78
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
        )
        self.assertEqual(status, "pending_review")
        self.assertIn("below autonomous threshold", reason.lower())

    def test_low_confidence_results_in_no_change(self):
        self.decision["confidence"] = 0.55
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
            cooldown_allows_publish=True,
        )
        self.assertEqual(status, "no_change")

    def test_angle_brackets_rejected(self):
        self.proposed["title"] = "Why <RAG> Fails in Production"
        status, reason, changed = evaluate_autonomous_policy(
            self.settings, self.decision, self.before, self.proposed,
        )
        self.assertEqual(status, "rejected")
        self.assertIn("bracket", reason.lower())


class TestLearningLoopAndRollback(TestCase):
    def test_metric_deltas_computation(self):
        baseline = {"ctr_percent": 2.0, "impressions": 10000, "views": 200, "average_view_percentage": 40.0}
        post = {"ctr_percent": 3.2, "impressions": 12000, "views": 384, "average_view_percentage": 42.5}
        deltas = compute_metric_deltas(baseline, post)
        self.assertAlmostEqual(deltas["ctr_delta_absolute"], 1.2)
        self.assertAlmostEqual(deltas["ctr_growth_percent"], 60.0)
        self.assertEqual(deltas["views_delta"], 184)
        self.assertAlmostEqual(deltas["views_growth_percent"], 92.0)

    def test_classify_evaluation_outcome_improved(self):
        baseline = {"ctr_percent": 2.0, "impressions": 10000, "views": 200}
        post = {"ctr_percent": 2.8, "impressions": 12000, "views": 336}
        deltas = compute_metric_deltas(baseline, post)
        outcome = classify_evaluation_outcome(baseline, post, deltas)
        self.assertEqual(outcome, "improved")

    def test_classify_evaluation_outcome_declined(self):
        baseline = {"ctr_percent": 3.5, "impressions": 10000, "views": 350}
        post = {"ctr_percent": 2.4, "impressions": 10000, "views": 240}
        deltas = compute_metric_deltas(baseline, post)
        outcome = classify_evaluation_outcome(baseline, post, deltas)
        self.assertEqual(outcome, "declined")

    def test_format_learning_feedback_for_prompt(self):
        store = MagicMock()
        store.list_action_evaluations.return_value = [
            {
                "action_id": "act-1",
                "video_id": "kLK0N14PT1Y",
                "observation_days": 14,
                "outcome": "improved",
                "metric_deltas": {"ctr_delta_absolute": 0.85, "views_growth_percent": 25.0},
            }
        ]
        store.get_strategy_action.return_value = {
            "video_title": "Why RAG Misses Answers",
            "proposed_metadata": {"title": "Why RAG Fails in Production"},
        }
        feedback = format_learning_feedback_for_prompt(store, "owner-1")
        self.assertIn("Empirical Learning from Past Automated Optimizations", feedback)
        self.assertIn("Why RAG Misses Answers", feedback)
        self.assertIn("replicate this clarity", feedback)

    @patch("youtube_analytics_connector.fetch_video_snippet")
    @patch("youtube_analytics_connector.apply_approved_dashboard_proposal")
    def test_rollback_strategy_action_success(self, mock_apply, mock_snippet):
        store = MagicMock()
        youtube = MagicMock()

        store.get_strategy_action.return_value = {
            "id": "act-100",
            "run_id": "run-100",
            "video_id": "kLK0N14PT1Y",
            "status": "auto_published",
            "before_metadata": {
                "title": "Original Baseline Title",
                "description": "Original Description",
                "tags": ["orig1", "orig2"],
                "categoryId": "28",
            },
            "proposed_metadata": {
                "title": "New Changed Title",
                "description": "New Description",
                "tags": ["new1"],
                "categoryId": "28",
            },
        }

        mock_snippet.return_value = {
            "title": "New Changed Title",
            "description": "New Description",
            "tags": ["new1"],
            "categoryId": "28",
        }
        mock_apply.return_value = {"result": "success"}

        success, msg = rollback_strategy_action(store, youtube, "owner-1", "act-100")
        self.assertTrue(success)
        self.assertIn("Successfully rolled back", msg)
        mock_apply.assert_called_once()
        store.update_strategy_action.assert_called()
