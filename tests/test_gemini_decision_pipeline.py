"""Tests verifying Gemini decision pipeline preservation, unlimited catalogue, and shadow mode semantics."""

import unittest
from unittest.mock import MagicMock, patch

from channel_strategy_worker import (
    _metadata_candidates,
    _prepare_gemini_metadata_proposals,
    _process_candidate,
    run_strategy_agent,
)
from core.channel_strategy_agent import (
    collect_channel_snapshot,
    evaluate_autonomous_policy,
)
import youtube_analytics_connector as connector


class TestGeminiDecisionPipeline(unittest.TestCase):
    def setUp(self):
        self.before_metadata = {
            "title": "Old Safe Video Title",
            "description": "Safe description about AI models.",
            "tags": ["AI", "Tutorial"],
            "categoryId": "28",
        }
        self.mock_store = MagicMock()
        self.mock_store.create_strategy_action.side_effect = lambda owner, vals: {"id": "act_mock_1", **vals}
        self.mock_youtube = MagicMock()

    def test_no_change_decision_never_publishes(self):
        candidate = {
            "video_id": "test_vid_1",
            "title": "Old Safe Video Title",
            "confidence": 0.95,
            "decision": "no_change",
            "diagnosis": "performing",
            "fields_to_modify": [],
            "factual_evidence": "Video is performing well above cohort median.",
            "rationale": "Leave existing metadata intact.",
        }
        row = {
            "video_id": "test_vid_1",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": self.before_metadata,
            "fields_to_modify": [],
            "errors": [],
        }
        settings = {
            "automation_mode": "autonomous",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily"
            )

            self.assertEqual(result, "no_change")
            mock_apply.assert_not_called()
            self.mock_store.create_strategy_action.assert_called_once()
            args, kwargs = self.mock_store.create_strategy_action.call_args
            action_data = args[1] if len(args) > 1 else kwargs["values"]
            self.assertEqual(action_data["status"], "no_change")

    def test_review_decision_never_publishes(self):
        candidate = {
            "video_id": "test_vid_2",
            "title": "Old Safe Video Title",
            "confidence": 0.99,  # Very high confidence, but explicit decision is review
            "decision": "review",
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "New High Engagement Title",
            "factual_evidence": "CTR is 1.8% vs median 4.5%.",
            "rationale": "Needs human review of the creative title hook.",
        }
        row = {
            "video_id": "test_vid_2",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": {**self.before_metadata, "title": "New High Engagement Title"},
            "fields_to_modify": ["title"],
            "errors": [],
        }
        settings = {
            "automation_mode": "autonomous",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily"
            )

            self.assertEqual(result, "pending_review")
            mock_apply.assert_not_called()
            args, _ = self.mock_store.create_strategy_action.call_args
            action_data = args[1]
            self.assertEqual(action_data["status"], "pending_review")

    def test_low_confidence_never_publishes(self):
        # Confidence below 0.65 floor -> no_change (never publishes)
        candidate_floor = {
            "video_id": "test_vid_floor",
            "title": "Old Safe Video Title",
            "confidence": 0.50,
            "decision": "auto_publish",
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "Floor Title",
            "factual_evidence": "Low sample size.",
            "rationale": "Floor rationale.",
        }
        row_floor = {
            "video_id": "test_vid_floor",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": {**self.before_metadata, "title": "Floor Title"},
            "fields_to_modify": ["title"],
            "errors": [],
        }
        settings = {
            "automation_mode": "autonomous",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result_floor, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate_floor, row_floor, settings, cadence="daily"
            )
            self.assertEqual(result_floor, "no_change")
            mock_apply.assert_not_called()

        # Confidence between 0.65 and 0.85 -> pending_review (never publishes)
        candidate_mid = {
            "video_id": "test_vid_mid",
            "title": "Old Safe Video Title",
            "confidence": 0.75,
            "decision": "auto_publish",
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "Mid Title",
            "factual_evidence": "Moderate confidence.",
            "rationale": "Mid rationale.",
        }
        row_mid = {
            "video_id": "test_vid_mid",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": {**self.before_metadata, "title": "Mid Title"},
            "fields_to_modify": ["title"],
            "errors": [],
        }
        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result_mid, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate_mid, row_mid, settings, cadence="daily"
            )
            self.assertEqual(result_mid, "pending_review")
            mock_apply.assert_not_called()

    def test_missing_or_invalid_gemini_decision_fails_closed(self):
        # Missing confidence and decision must fail closed to pending_review
        candidate = {
            "video_id": "test_vid_4",
            "title": "Old Safe Video Title",
            "confidence": None,
            "decision": None,
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "New Ambiguous Title",
        }
        row = {
            "video_id": "test_vid_4",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": {**self.before_metadata, "title": "New Ambiguous Title"},
            "fields_to_modify": ["title"],
            "errors": [],
        }
        settings = {
            "automation_mode": "autonomous",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily"
            )

            self.assertEqual(result, "pending_review")
            mock_apply.assert_not_called()
            args, _ = self.mock_store.create_strategy_action.call_args
            action_data = args[1]
            self.assertEqual(action_data["status"], "pending_review")
            self.assertIn("Fail-closed", action_data["reason"])

    def test_deterministic_gates_reject_angle_brackets_even_with_auto_publish(self):
        candidate = {
            "video_id": "test_vid_5",
            "title": "Old Safe Video Title",
            "confidence": 0.95,
            "decision": "auto_publish",
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "Title with <invalid> HTML tags",
            "factual_evidence": "Testing angle bracket rejection.",
            "rationale": "High confidence but contains angle brackets.",
        }
        row = {
            "video_id": "test_vid_5",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": {**self.before_metadata, "title": "Title with <invalid> HTML tags"},
            "fields_to_modify": ["title"],
            "errors": [],
        }
        settings = {
            "automation_mode": "autonomous",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily"
            )

            self.assertEqual(result, "failed")
            mock_apply.assert_not_called()
            args, _ = self.mock_store.create_strategy_action.call_args
            action_data = args[1]
            self.assertEqual(action_data["status"], "failed")
            self.assertIn("angle bracket", action_data["reason"].lower())

    def test_auto_publish_succeeds_when_all_gates_pass(self):
        candidate = {
            "video_id": "test_vid_auto",
            "title": "Old Safe Video Title",
            "confidence": 0.92,
            "decision": "auto_publish",
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "Validated High Quality Title",
            "factual_evidence": "CTR is 1.9% with 500 impressions.",
            "rationale": "Clear winning angle.",
        }
        proposed_meta = {**self.before_metadata, "title": "Validated High Quality Title"}
        row = {
            "video_id": "test_vid_auto",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": proposed_meta,
            "fields_to_modify": ["title"],
            "errors": [],
        }
        settings = {
            "automation_mode": "autonomous",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 14}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal", return_value={"result": "success", "url": "https://youtu.be/test_vid_auto"}) as mock_apply, \
             patch.object(connector, "fetch_video_snippet", return_value=proposed_meta):
            result, daily_count, cadence_count = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily", daily_count=0, cadence_count=0
            )

            self.assertEqual(result, "auto_published")
            self.assertEqual(daily_count, 1)
            self.assertEqual(cadence_count, 1)
            mock_apply.assert_called_once()
            # Verify the proposal passed to apply matches the exact validated Gemini proposal
            applied_args = mock_apply.call_args[0]
            self.assertEqual(applied_args[3]["title"], "Validated High Quality Title")

    def test_prepare_gemini_metadata_proposals_honors_fields_to_modify(self):
        candidates = [
            {
                "video_id": "vid_title_only",
                "title": "Current Title",
                "description": "Current Description",
                "tags": ["Tag1", "Tag2"],
                "fields_to_modify": ["title"],
                "proposed_title": "New Gemini Title",
                "proposed_description": "Gemini suggested this description but it should be ignored",
                "proposed_tags": ["IgnoredTag"],
            },
            {
                "video_id": "vid_tags_only",
                "title": "Current Title 2",
                "description": "Current Description 2",
                "tags": ["OldTag"],
                "fields_to_modify": ["tags"],
                "proposed_title": "Should be ignored",
                "proposed_tags": ["NewTag1", "NewTag2"],
            },
        ]

        mock_youtube = MagicMock()
        mock_youtube.videos().list().execute.return_value = {
            "items": [
                {
                    "id": "vid_title_only",
                    "snippet": {
                        "title": "Current Title",
                        "description": "Current Description",
                        "tags": ["Tag1", "Tag2"],
                        "categoryId": "28",
                    },
                },
                {
                    "id": "vid_tags_only",
                    "snippet": {
                        "title": "Current Title 2",
                        "description": "Current Description 2",
                        "tags": ["OldTag"],
                        "categoryId": "28",
                    },
                },
            ]
        }

        rows = _prepare_gemini_metadata_proposals(mock_youtube, candidates)
        self.assertEqual(len(rows), 2)

        # Row 1: only title modified
        row1 = rows[0]
        self.assertEqual(row1["proposed"]["title"], "New Gemini Title")
        self.assertEqual(row1["proposed"]["description"], "Current Description")
        self.assertEqual(row1["proposed"]["tags"], ["Tag1", "Tag2"])

        # Row 2: only tags modified
        row2 = rows[1]
        self.assertEqual(row2["proposed"]["title"], "Current Title 2")
        self.assertEqual(row2["proposed"]["description"], "Current Description 2")
        self.assertEqual(row2["proposed"]["tags"], ["NewTag1", "NewTag2"])

    def test_shadow_mode_records_shadow_published_and_flags_cooldown_blocked(self):
        candidate = {
            "video_id": "test_shadow_vid",
            "title": "Old Safe Video Title",
            "confidence": 0.95,
            "decision": "auto_publish",
            "diagnosis": "packaging",
            "fields_to_modify": ["title"],
            "proposed_title": "New High CTR Title",
            "factual_evidence": "CTR is 2.1%.",
            "rationale": "High confidence packaging improvement.",
        }
        row = {
            "video_id": "test_shadow_vid",
            "status": "ready",
            "before": self.before_metadata,
            "proposed": {**self.before_metadata, "title": "New High CTR Title"},
            "fields_to_modify": ["title"],
            "errors": [],
        }
        # In Shadow Mode, automation_mode is shadow
        settings = {
            "automation_mode": "shadow",
            "cooldown_days": 7,
            "require_approval_for_all_changes": False,
        }

        # Case 1: Shadow mode with cooldown allowed
        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 10}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=True), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily"
            )
            self.assertEqual(result, "shadow_published")
            mock_apply.assert_not_called()
            args, _ = self.mock_store.create_strategy_action.call_args
            evidence = args[1]["evidence"]
            self.assertTrue(evidence["shadow"])
            self.assertFalse(evidence["cooldown_blocked"])

        # Case 2: Shadow mode with cooldown blocked (modified 3 days ago)
        with patch("channel_strategy_worker._record_live_metadata_snapshot", return_value={"days_since_modification": 3}), \
             patch("channel_strategy_worker._snapshot_allows_publish", return_value=False), \
             patch.object(connector, "apply_approved_dashboard_proposal") as mock_apply:
            result, _, _ = _process_candidate(
                self.mock_store, self.mock_youtube, "owner_123", "run_123",
                candidate, row, settings, cadence="daily"
            )
            self.assertEqual(result, "shadow_published")
            mock_apply.assert_not_called()
            args, _ = self.mock_store.create_strategy_action.call_args
            evidence = args[1]["evidence"]
            self.assertTrue(evidence["shadow"])
            self.assertTrue(evidence["cooldown_blocked"])
            self.assertIn("seven-day", evidence["cooldown_reason"].lower())

    def test_scheduled_run_skips_if_already_completed(self):
        mock_store = MagicMock()
        mock_store.get_strategy_settings.return_value = {"automation_mode": "shadow"}
        mock_store._request.side_effect = [
            [],  # active running runs check
            [{"id": "run_completed_1", "status": "completed"}],  # existing completed runs
        ]

        with patch("channel_strategy_worker.resolve_channel_owner", return_value=("owner_123", "ch_123")), \
             patch("channel_strategy_worker._get_owner_credentials", return_value=MagicMock()), \
             patch("channel_strategy_worker.build", return_value=MagicMock()), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "fake_gemini_key"}):
            res = run_strategy_agent(mock_store, MagicMock(), trigger_source="schedule", cadence="daily")
            self.assertEqual(res.get("status"), "already_completed")

    def test_manual_dispatch_allows_repeat_without_crash(self):
        mock_store = MagicMock()
        mock_store.get_strategy_settings.return_value = {"automation_mode": "shadow"}
        mock_store.create_strategy_run.return_value = {"id": "run_manual_1"}
        mock_store._request.side_effect = [
            [],  # active running runs check
            [{"id": "run_completed_1", "status": "completed"}],  # existing period runs
            [],  # interrupted actions
        ]

        with patch("channel_strategy_worker.resolve_channel_owner", return_value=("owner_123", "ch_123")), \
             patch("channel_strategy_worker._get_owner_credentials", return_value=MagicMock()), \
             patch("channel_strategy_worker.build", return_value=MagicMock()), \
             patch("channel_strategy_worker.collect_channel_snapshot", return_value={"metrics_period": {}, "catalogue": {"uploads_found": 10}}), \
             patch("channel_strategy_worker.generate_strategy_report", return_value=({"metadata_candidates": []}, "gemini-3.1-pro-preview")), \
             patch.dict("os.environ", {"GEMINI_API_KEY": "fake_gemini_key"}):
            res = run_strategy_agent(mock_store, MagicMock(), trigger_source="workflow_dispatch", cadence="daily")
            self.assertEqual(res.get("status"), "completed")
            # Verify dedup_key had '_manual_' suffix
            call_args = mock_store.create_strategy_run.call_args[0][1]
            self.assertIn("_manual_", call_args["dedup_key"])


if __name__ == "__main__":
    unittest.main()
