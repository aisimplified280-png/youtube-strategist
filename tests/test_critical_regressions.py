"""Comprehensive regression tests covering all 11 critical scenarios for Creator Growth Agent."""

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import core.creator_db as creator_db
from core.creator_db import (
    acquire_briefing_lock,
    extend_briefing_lock,
    release_briefing_lock,
    build_briefing_lock_key,
    get_latest_daily_briefing,
    save_daily_briefing,
    get_creator_preferences,
    set_creator_preference,
    record_proposal_decision,
    get_recent_proposal_history,
    get_pipeline_items,
    save_pipeline_item,
    update_pipeline_stage,
    get_radar_items,
    save_radar_item,
)
from core.daily_briefing import get_or_create_daily_briefing
from core.linter import lint_proposal, score_hook_text
from core.metadata_apply import compute_proposal_digest, apply_proposal
from core.prompts import build_strategist_prompt
from ui.components import char_counter, diff_viewer
import youtube_dashboard
import youtube_analytics_connector as connector


class TestCriticalRegressions(unittest.TestCase):

    def setUp(self):
        # Use isolated temporary database for each test run to ensure clean state
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_creator_memory.sqlite3"
        self._orig_db_env = os.environ.get("CREATOR_GROWTH_DB_PATH")
        os.environ["CREATOR_GROWTH_DB_PATH"] = str(self.test_db_path)

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["CREATOR_GROWTH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("CREATOR_GROWTH_DB_PATH", None)
        self.temp_dir.cleanup()

    # --- Scenario 1 & 2: Real-mode daily briefing with mocked clients & correct invocation ---
    def test_real_mode_daily_briefing_uses_existing_load_dashboard_data(self):
        """Daily briefing calls load_dashboard_data() with completed end_date and passes credentials."""
        mock_youtube = MagicMock()
        mock_analytics = MagicMock()
        mock_reporting = MagicMock()
        mock_credentials = MagicMock()

        sample_videos = [
            {
                "video_id": f"vid-{i}",
                "title": f"Video Title {i}",
                "published_at": "2026-09-01",
                "format": "long",
                "duration_seconds": 600,
                "views": 2000 + (i * 100),
                "impressions": 40000,
                "ctr": 2.5,
                "averageViewDuration": 240,
                "age_days": 20,
            }
            for i in range(8)
        ]

        mock_dashboard_return = {
            "channel_id": "test-channel-id",
            "video_rows": sample_videos,
            "reach_status": "Available",
            "catalogue_note": "Evaluated 8 videos",
            "api_warnings": [],
        }

        mock_candidate = {
            **sample_videos[0],
            "peer_count": 5,
            "peer_views_median": 4500,
            "relative_views_gap": -0.55,
            "peer_ctr_median": 4.8,
            "diagnosis": "Packaging review",
            "next_step": "Review title & thumbnail pairing.",
            "recommendation_evidence": "CTR 2.5% vs 4.8% peer median.",
        }

        with patch("youtube_dashboard.load_dashboard_data", return_value=mock_dashboard_return) as mock_load, \
             patch("youtube_dashboard.rank_optimization_candidates", return_value=[mock_candidate]) as mock_rank:
            
            briefing = get_or_create_daily_briefing(
                youtube=mock_youtube,
                analytics=mock_analytics,
                reporting=mock_reporting,
                credentials=mock_credentials,
                owner_id="owner_abc",
                channel_id="test-channel-id",
                force_refresh=True,
                is_demo=False,
            )

            # Verification: load_dashboard_data was invoked with exact signature and completed yesterday end_date
            mock_load.assert_called_once()
            args, kwargs = mock_load.call_args
            self.assertEqual(kwargs.get("youtube"), mock_youtube)
            self.assertEqual(kwargs.get("analytics"), mock_analytics)
            self.assertEqual(kwargs.get("reporting"), mock_reporting)
            self.assertEqual(kwargs.get("credentials"), mock_credentials)
            self.assertEqual(kwargs.get("video_limit"), 100)

            # End date must be yesterday (completed day)
            yesterday = date.today() - timedelta(days=1)
            self.assertEqual(kwargs.get("end_date"), yesterday)
            self.assertEqual(kwargs.get("start_date"), yesterday - timedelta(days=89))

            # Candidates ranked and actions built from measured diagnosis
            mock_rank.assert_called_once_with(sample_videos)
            self.assertEqual(briefing.get("status"), "ready")
            actions = briefing.get("prioritized_actions", [])
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["action_type"], "Packaging Overhaul (Title & Thumbnail)")
            self.assertEqual(actions[0]["video_id"], "vid-0")
            self.assertIn("CTR", actions[0]["evidence"])

    # --- Scenario 3: Missing or expired YouTube authorization ---
    def test_missing_or_expired_youtube_authorization(self):
        """Missing or expired client returns connection_failure, never empty success."""
        briefing = get_or_create_daily_briefing(
            youtube=None,
            analytics=None,
            owner_id="owner_abc",
            is_demo=False,
        )
        self.assertEqual(briefing.get("status"), "connection_failure")
        self.assertEqual(len(briefing.get("prioritized_actions", [])), 0)
        self.assertIn("disconnected", briefing.get("notice", "").lower())

    # --- Scenario 4: API exception and quota failure ---
    def test_api_exception_quota_failure_fails_closed_and_preserves_stale(self):
        """API failure on first run returns scan_failed; on refresh preserves stale briefing."""
        mock_youtube = MagicMock()
        mock_analytics = MagicMock()

        # 4a: First run scan failure
        with patch("youtube_dashboard.load_dashboard_data", side_effect=RuntimeError("Quota exceeded 403")):
            briefing = get_or_create_daily_briefing(
                youtube=mock_youtube,
                analytics=mock_analytics,
                owner_id="owner_abc",
                channel_id="chan_123",
                force_refresh=True,
                is_demo=False,
            )
            self.assertEqual(briefing.get("status"), "scan_failed")
            self.assertIn("Quota exceeded 403", briefing.get("notice"))

        # 4b: Save a prior successful briefing, then refresh with quota failure
        prior_briefing = {
            "briefing_id": "run-prior",
            "run_date": "2026-10-29",
            "channel_id": "chan_123",
            "status": "ready",
            "prioritized_actions": [{"video_id": "vid-prior", "title": "Prior Video"}],
        }
        save_daily_briefing(
            briefing_id="run-prior",
            channel_id="chan_123",
            run_date="2026-10-29",
            briefing_dict=prior_briefing,
            owner_id="owner_abc",
            is_demo=False,
        )

        with patch("youtube_dashboard.load_dashboard_data", side_effect=RuntimeError("API Network Timeout")):
            stale_briefing = get_or_create_daily_briefing(
                youtube=mock_youtube,
                analytics=mock_analytics,
                owner_id="owner_abc",
                channel_id="chan_123",
                force_refresh=True,
                is_demo=False,
            )
            self.assertEqual(stale_briefing.get("status"), "stale")
            self.assertIn("prior", stale_briefing.get("notice", "").lower())
            self.assertEqual(stale_briefing.get("prioritized_actions")[0]["video_id"], "vid-prior")

    # --- Scenario 5: Empty data versus genuinely healthy channel ---
    def test_empty_data_versus_genuinely_healthy_channel(self):
        """Distinguish 0 videos from healthy channel with 0 underperforming candidates."""
        mock_youtube = MagicMock()
        mock_analytics = MagicMock()

        # 5a: Zero video rows fetched
        with patch("youtube_dashboard.load_dashboard_data", return_value={"video_rows": [], "reach_status": ""}):
            briefing_empty = get_or_create_daily_briefing(
                youtube=mock_youtube,
                analytics=mock_analytics,
                owner_id="owner_abc",
                force_refresh=True,
                is_demo=False,
            )
            self.assertEqual(briefing_empty.get("status"), "insufficient_sample")
            self.assertEqual(len(briefing_empty.get("prioritized_actions", [])), 0)

        # 5b: 5 videos returned, but all performing above peer median (rank returns empty list)
        five_healthy = [{"video_id": f"vid-{i}", "views": 10000, "age_days": 20, "format": "long"} for i in range(5)]
        with patch("youtube_dashboard.load_dashboard_data", return_value={"video_rows": five_healthy, "reach_status": ""}), \
             patch("youtube_dashboard.rank_optimization_candidates", return_value=[]):
            briefing_healthy = get_or_create_daily_briefing(
                youtube=mock_youtube,
                analytics=mock_analytics,
                owner_id="owner_abc",
                force_refresh=True,
                is_demo=False,
            )
            self.assertEqual(briefing_healthy.get("status"), "ready")
            self.assertEqual(briefing_healthy["summary"]["opportunity_count"], 0)
            self.assertEqual(len(briefing_healthy.get("prioritized_actions", [])), 0)

    # --- Scenario 6: Insufficient peer sample size ---
    def test_insufficient_sample_size_flagged(self):
        """Fewer than 3 uploads must be flagged as insufficient_sample."""
        mock_youtube = MagicMock()
        mock_analytics = MagicMock()

        two_videos = [{"video_id": "v1", "views": 500}, {"video_id": "v2", "views": 600}]
        with patch("youtube_dashboard.load_dashboard_data", return_value={"video_rows": two_videos, "reach_status": ""}):
            briefing = get_or_create_daily_briefing(
                youtube=mock_youtube,
                analytics=mock_analytics,
                owner_id="owner_abc",
                force_refresh=True,
                is_demo=False,
            )
            self.assertEqual(briefing.get("status"), "insufficient_sample")
            self.assertIn("3 format-matched", briefing.get("notice"))

    # --- Scenario 7: Demo / Live cache isolation ---
    def test_demo_and_live_cache_isolation(self):
        """Demo briefings cannot be returned by production queries and cache keys are isolated."""
        today_str = date.today().isoformat()
        demo_key = build_briefing_lock_key("channel_1", today_str, is_demo=True)
        prod_key = build_briefing_lock_key("channel_1", today_str, owner_id="owner_1", is_demo=False)

        self.assertNotEqual(demo_key, prod_key)
        self.assertIn("demo", demo_key)
        self.assertIn("prod", prod_key)
        self.assertIn("owner_1", prod_key)

        # Get demo briefing
        demo_briefing = get_or_create_daily_briefing(is_demo=True)
        self.assertIn("Demo Mode", demo_briefing.get("channel_name"))

        # Production query must NOT return demo briefing
        prod_cached = get_latest_daily_briefing("channel_1", run_date=today_str, owner_id="owner_1", is_demo=False)
        self.assertIsNone(prod_cached)

    # --- Scenario 8: Anonymous Demo Mode cannot read or write owner data ---
    def test_anonymous_demo_mode_cannot_read_or_write_owner_data(self):
        """Demo mode reads only static fixtures and fails closed on write attempts."""
        # Setup private owner data
        set_creator_preference("voice_tone", "Top Secret Owner Voice", owner_id="owner_secret", is_demo=False)
        save_pipeline_item("pipe_sec_1", "Secret Video", "idea", owner_id="owner_secret", is_demo=False)

        # Demo Mode reads fixtures, never owner data
        demo_prefs = get_creator_preferences(is_demo=True)
        self.assertNotEqual(demo_prefs.get("voice_tone"), "Top Secret Owner Voice")

        demo_pipe = get_pipeline_items(is_demo=True)
        self.assertFalse(any(it["title"] == "Secret Video" for it in demo_pipe))

        # Demo Mode write attempts raise PermissionError
        with self.assertRaises(PermissionError):
            set_creator_preference("voice_tone", "Hacked Voice", is_demo=True)

        with self.assertRaises(PermissionError):
            save_pipeline_item("hack_1", "Hacked Video", "idea", is_demo=True)

        with self.assertRaises(PermissionError):
            update_pipeline_stage("pipe_sec_1", "published", is_demo=True)

        with self.assertRaises(PermissionError):
            record_proposal_decision("vid-1", "digest123", "approved", {}, is_demo=True)

        with self.assertRaises(PermissionError):
            save_radar_item("rad-1", "Title", "https://example.com", "model", "sum", [], is_demo=True)

        # Apply proposal in Demo Mode must forbid public writes
        apply_res = apply_proposal(None, "demo-vid-101", {}, {}, "any-digest", is_demo=True)
        self.assertEqual(apply_res.get("status"), "error")
        self.assertIn("disabled in Demo Mode", apply_res.get("message"))

    # --- Scenario 9: Workspace render, prompt, lint, digest, approve, apply, post-write ---
    def test_workspace_full_flow_contract(self):
        """Verify prompt generation, proposal lint, digest calculation, approval, and verified apply."""
        # 1. char_counter type safety
        badge = char_counter(current_len=65, max_len=100, warn_len=60)
        self.assertIn("char-warn", badge)
        self.assertIn("65/100", badge)

        # 2. diff_viewer signature
        # Should execute cleanly with 3 arguments
        with patch("streamlit.markdown") as mock_st_md:
            diff_viewer("Old Title", "New Title", "Title Comparison")
            mock_st_md.assert_called_once()

        # 3. build_strategist_prompt with target_snippet and instruction
        snippet = {
            "title": "Old Prompt Caching Title",
            "description": "Old description.",
            "tags": ["python", "caching"],
            "categoryId": "28",
        }
        prompt = build_strategist_prompt(target_snippet=snippet, instruction="Focus on speed.")
        self.assertIn("CURRENT TITLE: Old Prompt Caching Title", prompt)
        self.assertIn("Focus on speed.", prompt)

        # 4. lint_proposal with actual proposal dict
        proposal = {
            "title": "Clean Title Under Sixty Characters for Mobile",
            "description": "A well-structured description.\n\n#PythonAI #PromptCaching",
            "tags": ["python", "ai", "prompt caching"],
        }
        lint_res = lint_proposal(proposal, thumbnail_concept=None)
        self.assertIn("warnings", lint_res)
        self.assertIn("passes", lint_res)
        self.assertIsInstance(lint_res["warnings"], list)
        self.assertIsInstance(lint_res["passes"], list)

        # 5. compute_proposal_digest and immutable fields preservation
        proposed_snippet = dict(snippet)
        proposed_snippet["title"] = proposal["title"]
        proposed_snippet["description"] = proposal["description"]
        proposed_snippet["tags"] = proposal["tags"]

        digest = compute_proposal_digest(
            video_id="video-123",
            before_snippet=snippet,
            proposed_snippet=proposed_snippet,
        )
        self.assertTrue(isinstance(digest, str) and len(digest) == 64)

        # 6. Apply proposal verified update with mock YouTube
        mock_youtube = MagicMock()
        mock_apply_result = {
            "result": "success",
            "video_id": "video-123",
            "title": proposed_snippet["title"],
            "url": "https://www.youtube.com/watch?v=video-123",
        }
        with patch("youtube_analytics_connector.apply_approved_dashboard_proposal", return_value=mock_apply_result) as mock_connector_apply:
            apply_res = apply_proposal(
                youtube=mock_youtube,
                video_id="video-123",
                before_snippet=snippet,
                proposed_snippet=proposed_snippet,
                approved_digest=digest,
                is_demo=False,
            )
            self.assertEqual(apply_res.get("status"), "success")
            self.assertEqual(apply_res.get("title"), proposed_snippet["title"])
            mock_connector_apply.assert_called_once()

    # --- Scenario 10: Hook scoring with available and unavailable tools ---
    def test_hook_scoring_available_and_unavailable(self):
        """score_hook_text() returns dict with 5 dimensions when available, None when unavailable."""
        sample_text = "In this video, I will show you how prompt caching slashes API latency by 80%."

        # Case 10a: Available tool
        res = score_hook_text(sample_text)
        self.assertIsInstance(res, dict)
        self.assertIn("overall_score", res)
        self.assertIn("specificity", res)
        self.assertIn("address", res)
        self.assertIn("stakes", res)
        self.assertIn("curiosity", res)
        self.assertIn("brevity", res)
        self.assertIsInstance(res["overall_score"], int)

        # Case 10b: Unavailable tool
        with patch("core.linter.hookscore_tool", None):
            res_none = score_hook_text(sample_text)
            self.assertIsNone(res_none)

    # --- Scenario 11: Persistence across sessions and simulated restart ---
    def test_persistence_across_sessions_and_simulated_restart(self):
        """Data persists across new connections and lock can be extended during long runs."""
        # Store preferences and proposal history
        set_creator_preference("voice_tone", "Persistent Voice Tone", owner_id="owner_1", is_demo=False)
        record_proposal_decision(
            video_id="vid-persist-1",
            proposal_digest="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            status="approved",
            proposal_dict={"title": "Persisted Title"},
            feedback="Looks great",
            owner_id="owner_1",
            is_demo=False,
        )

        # Simulate application restart by reopening connection in a fresh call
        loaded_prefs = get_creator_preferences(owner_id="owner_1", is_demo=False)
        self.assertEqual(loaded_prefs.get("voice_tone"), "Persistent Voice Tone")

        history = get_recent_proposal_history(limit=5, owner_id="owner_1", is_demo=False)
        self.assertTrue(any(h["video_id"] == "vid-persist-1" for h in history))

        # Test briefing lock and lease extension
        lock_key = "test_long_operation_lock"
        owner_token = "token-test-123"
        self.assertTrue(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token=owner_token, owner_id="owner_1"))
        
        # Extend lease
        self.assertTrue(extend_briefing_lock(lock_key, owner_token=owner_token, extend_seconds=120))
        
        # Another token is denied
        self.assertFalse(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token="token-intruder", owner_id="owner_1"))
        
        # Release lock
        release_briefing_lock(lock_key, owner_token=owner_token)
        self.assertTrue(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token="token-new", owner_id="owner_1"))
        release_briefing_lock(lock_key, owner_token="token-new")


if __name__ == "__main__":
    unittest.main()
