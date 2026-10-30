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
        snippet["channelId"] = connector.CHANNEL_ID
        proposed_snippet["channelId"] = connector.CHANNEL_ID

        digest = compute_proposal_digest(
            video_id="video-123",
            before_snippet=snippet,
            proposed_snippet=proposed_snippet,
        )
        self.assertTrue(isinstance(digest, str) and len(digest) == 64)

        # 6. Apply proposal verified update with mock YouTube (testing actual connector)
        mock_youtube = MagicMock()
        list_execute_mock = MagicMock(side_effect=[
            {"items": [{"id": "video-123", "snippet": snippet}]},       # Pre-write refetch
            {"items": [{"id": "video-123", "snippet": proposed_snippet}]}, # Post-write verification
        ])
        mock_youtube.videos().list().execute = list_execute_mock
        mock_youtube.videos().update().execute.return_value = {"id": "video-123", "snippet": proposed_snippet}

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

        # Test stale metadata refetch rejection (metadata changed on YouTube after review)
        stale_snippet = dict(snippet)
        stale_snippet["title"] = "Modified Concurrently on YouTube"
        stale_list_mock = MagicMock(return_value={"items": [{"id": "video-123", "snippet": stale_snippet}]})
        mock_youtube.videos().list().execute = stale_list_mock
        stale_res = apply_proposal(
            youtube=mock_youtube,
            video_id="video-123",
            before_snippet=snippet,
            proposed_snippet=proposed_snippet,
            approved_digest=digest,
            is_demo=False,
        )
        self.assertEqual(stale_res.get("status"), "error")
        self.assertIn("changed after review", stale_res.get("message", ""))

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

    # --- Scenario 12: Unavailable Analytics metrics reported as insufficient_metrics ---
    def test_unavailable_analytics_metrics_reported_as_insufficient_metrics(self):
        """When Analytics returns no metrics for video rows (views=None), status is insufficient_metrics and never claims healthy."""
        mock_dashboard_return = {
            "channel_id": "test-channel-id",
            "video_rows": [
                {
                    "video_id": f"vid-{i}",
                    "title": f"Video {i}",
                    "published_at": "2026-09-01",
                    "format": "long",
                    "duration_seconds": 600,
                    "views": None,  # Analytics returned no metrics
                    "impressions": None,
                    "ctr": None,
                    "averageViewDuration": None,
                    "age_days": 20,
                    "analytics_status": "Unavailable: Analytics API returned HTTP 503.",
                }
                for i in range(5)
            ],
            "reach_status": "Unavailable: reach report not ready.",
            "catalogue_note": "Evaluated 5 videos",
            "api_warnings": ["Unavailable: Analytics API returned HTTP 503."],
        }

        with patch("youtube_dashboard.load_dashboard_data", return_value=mock_dashboard_return):
            briefing = get_or_create_daily_briefing(
                youtube=MagicMock(),
                analytics=MagicMock(),
                credentials=MagicMock(),
                owner_id="owner_unavail",
                is_demo=False,
            )

            # Must NOT claim ready or healthy
            self.assertEqual(briefing.get("status"), "insufficient_metrics")
            self.assertEqual(briefing.get("summary", {}).get("measured_videos_count"), 0)
            self.assertEqual(len(briefing.get("prioritized_actions", [])), 0)
            self.assertIn("Insufficient analytics metrics", briefing.get("notice", ""))

            # In Today page rendering, verify it does not display healthy claim
            from pages.today import render_today
            def _fake_cols(spec, *args, **kwargs):
                count = spec if isinstance(spec, int) else len(spec)
                return [MagicMock() for _ in range(count)]

            with patch("pages.today.get_or_create_daily_briefing", return_value=briefing), \
                 patch("streamlit.warning") as mock_st_warn, \
                 patch("streamlit.info") as mock_st_info, \
                 patch("streamlit.markdown"), \
                 patch("streamlit.columns", side_effect=_fake_cols), \
                 patch("streamlit.expander"):
                render_today(credentials=MagicMock(), subject="owner_unavail")
                # Warning should be rendered for insufficient analytics data
                warn_calls = [call[0][0] for call in mock_st_warn.call_args_list if call[0]]
                self.assertTrue(any("Insufficient Analytics Data" in str(w) or "unavailable" in str(w).lower() for w in warn_calls))
                # Info should NOT say all recent uploads are performing in line
                info_calls = [call[0][0] for call in mock_st_info.call_args_list if call[0]]
                self.assertFalse(any("All recent uploads are performing in line" in str(i) for i in info_calls))

    # --- Scenario 13: Today pending scan state handling ---
    def test_today_pending_scan_state_handling(self):
        """Today page explicitly handles 'pending' briefing state rather than displaying empty upload message."""
        pending_briefing = {
            "briefing_id": "briefing-pending",
            "run_date": "2026-10-30",
            "status": "pending",
            "summary": {"total_recent_videos": 0, "measured_videos_count": 0, "opportunity_count": 0},
            "prioritized_actions": [],
            "research_radar": [],
        }

        def _fake_cols(spec, *args, **kwargs):
            count = spec if isinstance(spec, int) else len(spec)
            return [MagicMock() for _ in range(count)]

        from pages.today import render_today
        with patch("pages.today.get_or_create_daily_briefing", return_value=pending_briefing), \
             patch("streamlit.info") as mock_st_info, \
             patch("streamlit.markdown"), \
             patch("streamlit.columns", side_effect=_fake_cols):
            render_today(credentials=MagicMock(), subject="owner_test")
            info_calls = [str(call[0][0]) for call in mock_st_info.call_args_list if call[0]]
            # Must show scan is in progress
            self.assertTrue(any("scan is currently in progress" in c for c in info_calls))
            # Must NOT claim 'No video uploads detected'
            self.assertFalse(any("No video uploads detected" in c for c in info_calls))

    # --- Scenario 14: Legacy database migration & creator_preferences owner_id upgrade ---
    def test_legacy_database_migration_and_owner_id_schema_upgrade(self):
        """Legacy SQLite database lacking owner_id column is automatically migrated on initialization."""
        import sqlite3
        legacy_db_file = Path(self.temp_dir.name) / "legacy_creator_db.sqlite3"
        # Create legacy database with old schema (without owner_id)
        conn = sqlite3.connect(str(legacy_db_file))
        with conn:
            conn.execute(
                """
                CREATE TABLE creator_preferences (
                    pref_key TEXT PRIMARY KEY,
                    pref_value TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO creator_preferences (pref_key, pref_value, updated_at_utc) VALUES ('voice_tone', 'Authoritative Engineering', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO creator_preferences (pref_key, pref_value, updated_at_utc) VALUES ('target_audience', 'Senior Python Devs', '2026-01-01T00:00:00Z')"
            )
        conn.close()

        # Point environment to this database and run get_db_connection()
        with patch.dict(os.environ, {"CREATOR_GROWTH_DB_PATH": str(legacy_db_file)}):
            migrated_conn = creator_db.get_db_connection()
            cur = migrated_conn.cursor()
            cur.execute("PRAGMA table_info(creator_preferences)")
            cols = {row[1] for row in cur.fetchall()}
            self.assertIn("owner_id", cols)
            migrated_conn.close()

            # Verify existing records are accessible via default_owner
            prefs = creator_db.get_creator_preferences(owner_id="default_owner", is_demo=False)
            self.assertEqual(prefs.get("voice_tone"), "Authoritative Engineering")
            self.assertEqual(prefs.get("target_audience"), "Senior Python Devs")

        # Test legacy oauth token store migration with real populated database
        from oauth_token_store import OAuthTokenStore
        signing_secret = "test-secret-at-least-32-chars-legacy-token"
        legacy_oauth_file = Path(self.temp_dir.name) / "ai-simplified-lab-strategist-oauth.sqlite3"
        target_oauth_file = Path(self.temp_dir.name) / "migrated_oauth" / "ai-simplified-lab-strategist-oauth.sqlite3"

        legacy_store = OAuthTokenStore(signing_secret, path=str(legacy_oauth_file))
        legacy_store.save_credentials_json("legacy_subject_1", "youtube", '{"access_token": "token-xyz-123"}')
        self.assertTrue(legacy_oauth_file.exists())
        self.assertFalse(target_oauth_file.exists())

        with patch("tempfile.gettempdir", return_value=self.temp_dir.name):
            migrated_store = OAuthTokenStore(signing_secret, path=str(target_oauth_file))
            self.assertTrue(target_oauth_file.exists())
            decrypted = migrated_store.get_credentials_json("legacy_subject_1", "youtube")
            self.assertEqual(decrypted, '{"access_token": "token-xyz-123"}')

    # --- Scenario 15: Workspace clears widget keys on video switch and apply ---
    def test_workspace_clears_widget_keys_on_video_switch_and_apply(self):
        """Switching target video in Workspace resets prop_title_in, prop_desc_in, and prop_tags_in widget keys."""
        import streamlit as st
        from pages.workspace import _clear_workspace_proposal_state

        # Populate previous video widget state
        st.session_state["workspace_active_video_id"] = "vid-101"
        st.session_state["prop_title_in"] = "Previous Video Proposed Title"
        st.session_state["prop_desc_in"] = "Previous Video Proposed Description"
        st.session_state["prop_tags_in"] = "prev, video, tags"
        st.session_state["workspace_proposal"] = {"title": "Previous Video Proposed Title"}
        st.session_state["workspace_approved_digest"] = "abcdef0123456789"

        # Call reset helper
        _clear_workspace_proposal_state()

        # All keys must be cleared from session state so next video inputs start fresh
        self.assertNotIn("prop_title_in", st.session_state)
        self.assertNotIn("prop_desc_in", st.session_state)
        self.assertNotIn("prop_tags_in", st.session_state)
        self.assertNotIn("workspace_proposal", st.session_state)
        self.assertNotIn("workspace_approved_digest", st.session_state)

    # --- Scenario 16: Analytics peer coverage and zero views as observed metric ---
    def test_analytics_peer_coverage_and_zero_views_observed(self):
        """Zero views is an observed metric; briefings require sufficient same-format, age-matched peers."""
        from core.daily_briefing import _generate_briefing_data
        from pages.today import render_today
        import streamlit as st

        # Video rows with observed metrics (including 0 views), but mismatched formats and ages
        # vid-1 is short (age 10), vid-2 is longform (age 10), vid-3 is longform (age 80)
        # vid-2 has age 10, vid-3 has age 80 -> abs(80 - 10) = 70 > max(14, 5) -> not peers!
        # None have >= 3 peers!
        video_rows = [
            {"video_id": "vid-1", "title": "Zero views short", "format": "short", "views": 0, "age_days": 10},
            {"video_id": "vid-2", "title": "Recent longform", "format": "longform", "views": 100, "age_days": 10},
            {"video_id": "vid-3", "title": "Old longform", "format": "longform", "views": 500, "age_days": 80},
        ]

        with patch("youtube_dashboard.load_dashboard_data", return_value={"video_rows": video_rows, "reach_status": "Ready", "api_warnings": []}):
            briefing = _generate_briefing_data(
                youtube=None,
                analytics=None,
                reporting=None,
                credentials=None,
                owner_id="owner-test",
                secrets={},
                channel_id="chan-test",
                run_date="2026-10-30",
            )
        self.assertEqual(briefing["status"], "insufficient_peer_sample")
        self.assertEqual(briefing["summary"]["measured_videos_count"], 3)
        self.assertEqual(briefing["summary"]["comparable_videos_count"], 0)
        self.assertEqual(briefing["summary"]["opportunity_count"], 0)
        self.assertEqual(briefing["prioritized_actions"], [])

        # Verify Today page does NOT display healthy channel message without peer coverage
        with patch("pages.today.get_or_create_daily_briefing", return_value=briefing), \
             patch.object(st, "warning") as mock_warn, \
             patch.object(st, "info") as mock_info, \
             patch.object(st, "columns", side_effect=lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]), \
             patch("pages.today.connection_status_banner"), \
             patch("pages.today.demo_banner"):
            render_today(subject="owner-test")
            info_calls = [str(call) for call in mock_info.call_args_list]
            self.assertTrue(any("Insufficient peer sample" in c or "same-format" in c for c in info_calls))
            self.assertFalse(any("All recent uploads are performing in line" in c for c in info_calls))

    # --- Scenario 17: Workspace post-apply render lifecycle ---
    def test_workspace_post_apply_render_lifecycle(self):
        """Deferred reset clears widget keys at the start of rerun before widgets are instantiated."""
        import streamlit as st
        from pages.workspace import render_workspace

        # Simulate state after an apply operation has succeeded
        st.session_state["_deferred_workspace_reset"] = True
        st.session_state["_applied_success_notice"] = "🎉 Update Applied and Verified! Metadata saved."
        st.session_state["prop_title_in"] = "Applied Title"
        st.session_state["prop_desc_in"] = "Applied Desc"
        st.session_state["prop_tags_in"] = "applied, tags"
        st.session_state["workspace_proposal"] = {"title": "Applied Title"}
        st.session_state["workspace_approved_digest"] = "digest123"

        with patch.object(st, "success") as mock_success, \
             patch.object(st, "balloons") as mock_balloons, \
             patch.object(st, "info"), patch.object(st, "warning"), patch.object(st, "error"), \
             patch.object(st, "text_input"), patch.object(st, "text_area"), \
             patch.object(st, "button", return_value=False), \
             patch.object(st, "columns", side_effect=lambda spec, *args, **kwargs: [MagicMock() for _ in range(spec if isinstance(spec, int) else len(spec))]), \
             patch("pages.workspace.connection_status_banner"):

            # Rendering workspace on the rerun pass
            render_workspace(youtube=MagicMock(), secrets={}, subject="owner-subject-1")

            # Must have cleared keys at the top of render_workspace before widgets were created
            self.assertNotIn("prop_title_in", st.session_state)
            self.assertNotIn("prop_desc_in", st.session_state)
            self.assertNotIn("prop_tags_in", st.session_state)
            self.assertNotIn("workspace_proposal", st.session_state)
            self.assertNotIn("workspace_approved_digest", st.session_state)
            self.assertNotIn("_deferred_workspace_reset", st.session_state)
            self.assertNotIn("_applied_success_notice", st.session_state)
            mock_success.assert_called_with("🎉 Update Applied and Verified! Metadata saved.")
            mock_balloons.assert_called_once()

    # --- Scenario 18: Owner data migration security and durability ---
    def test_owner_data_migration_security_and_durability(self):
        """claim_legacy_owner_records safely maps default_owner records to authenticated owner only."""
        from core.creator_db import (
            claim_legacy_owner_records, get_creator_preferences, DEFAULT_OWNER_ID, get_db_connection
        )

        test_db_path = Path(self.temp_dir.name) / "migration_test.sqlite3"
        with patch.dict(os.environ, {"CREATOR_GROWTH_DB_PATH": str(test_db_path)}):
            # Populate legacy records under DEFAULT_OWNER_ID
            conn = get_db_connection()
            with conn:
                conn.execute(
                    "INSERT INTO creator_preferences (owner_id, pref_key, pref_value, updated_at_utc) VALUES (?, ?, ?, ?)",
                    (DEFAULT_OWNER_ID, "channel_niche", "Autonomous AI Agents", "2026-10-30T00:00:00Z")
                )
                conn.execute(
                    "INSERT INTO production_pipeline (id, owner_id, title, stage, target_format, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                    ("pipe-legacy-1", DEFAULT_OWNER_ID, "Legacy Pipeline Title", "idea", "longform", "2026-10-30T00:00:00Z")
                )
                conn.execute(
                    "INSERT INTO proposal_history (owner_id, video_id, proposal_digest, status, proposal_json, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                    (DEFAULT_OWNER_ID, "vid-legacy", "digest123", "accepted", "{}", "2026-10-30T00:00:00Z")
                )
            conn.close()

            # Attempt to claim with unauthorized / demo subjects -> must fail / return empty
            self.assertEqual(claim_legacy_owner_records("demo"), {})
            self.assertEqual(claim_legacy_owner_records("anonymous"), {})
            self.assertEqual(claim_legacy_owner_records(""), {})

            # Claim with authenticated owner
            claimed = claim_legacy_owner_records("auth_owner_real_789")
            self.assertTrue(claimed.get("success"))

            # Verify owner has access to claimed data
            prefs = get_creator_preferences(owner_id="auth_owner_real_789")
            self.assertEqual(prefs.get("channel_niche"), "Autonomous AI Agents")

            # Verify intruder cannot re-claim already migrated data
            intruder_result = claim_legacy_owner_records("intruder_owner_000")
            self.assertFalse(intruder_result.get("success", False))

            # Demo mode must never see the claimed owner records
            demo_prefs = get_creator_preferences(is_demo=True)
            self.assertNotEqual(demo_prefs.get("channel_niche"), "Autonomous AI Agents")

    # --- Scenario 19: SQLite restart persistence integration ---
    def test_sqlite_restart_persistence_integration(self):
        """Simulates app restart across connection teardown, verifying persistence of memory and credentials."""
        from core.creator_db import (
            set_creator_preference, get_creator_preferences, save_pipeline_item, get_pipeline_items
        )
        from oauth_token_store import OAuthTokenStore

        persist_db_path = Path(self.temp_dir.name) / "restart_persist.sqlite3"
        persist_oauth_path = Path(self.temp_dir.name) / "restart_oauth.sqlite3"
        signing_secret = "secret-key-at-least-32-chars-long-12345"

        with patch.dict(os.environ, {"CREATOR_GROWTH_DB_PATH": str(persist_db_path)}):
            # 1. Save data before restart
            set_creator_preference("upload_frequency", "2x/week", owner_id="owner_persist")
            save_pipeline_item("item-persist-1", "Agents in Prod", "scripting", "longform", owner_id="owner_persist")

            token_store = OAuthTokenStore(signing_secret, path=str(persist_oauth_path))
            token_store.save_credentials_json("owner_persist", "youtube", '{"token": "persisted_oauth_token_val"}')

            # 2. Simulate complete restart: instantiate fresh store and read database
            fresh_token_store = OAuthTokenStore(signing_secret, path=str(persist_oauth_path))

            # 3. Verify data survived restart
            prefs = get_creator_preferences(owner_id="owner_persist")
            self.assertEqual(prefs.get("upload_frequency"), "2x/week")

            pipeline = get_pipeline_items(owner_id="owner_persist")
            self.assertEqual(len(pipeline), 1)
            self.assertEqual(pipeline[0]["title"], "Agents in Prod")

            creds = fresh_token_store.get_credentials_json("owner_persist", "youtube")
            self.assertEqual(creds, '{"token": "persisted_oauth_token_val"}')

    # --- Scenario 20: Research Radar provenance and backfill ---
    def test_radar_provenance_and_backfill(self):
        """Authoritative releases have verified source dates and existing records are backfilled."""
        from core.daily_briefing import AUTHORITATIVE_AI_RADAR_SOURCES
        from core.creator_db import get_db_connection, _init_db

        # Verify CrewAI Flows source date and URL
        crewai_item = next(item for item in AUTHORITATIVE_AI_RADAR_SOURCES if item["id"] == "radar-crewai-flows")
        self.assertEqual(crewai_item["source_date"], "2024-10-11")
        self.assertEqual(crewai_item["source_url"], "https://blog.crewai.com/announcing-crewai-flows/")

        # Verify backfill on existing DB with old dates
        radar_db_path = Path(self.temp_dir.name) / "radar_backfill.sqlite3"
        with patch.dict(os.environ, {"CREATOR_GROWTH_DB_PATH": str(radar_db_path)}):
            conn = get_db_connection()
            with conn:
                conn.execute(
                    "INSERT INTO research_radar (id, owner_id, title, source_url, category, source_date, retrieved_at_utc, channel_relevance, discovered_date, summary, video_angles_json) "
                    "VALUES ('radar-llama-3.2-edge', 'default_owner', 'Llama 3.2', 'https://ai.meta.com', 'model_release', '2026-09-25', '2026-10-30T00:00:00Z', 'relevance', '2026-10-30', 'summary', '[]')"
                )
                conn.execute(
                    "INSERT INTO research_radar (id, owner_id, title, source_url, category, source_date, retrieved_at_utc, channel_relevance, discovered_date, summary, video_angles_json) "
                    "VALUES ('radar-gemini-1.5-flash-8b', 'default_owner', 'Flash 8B', 'https://developers.googleblog.com', 'model_release', '2026-10-03', '2026-10-30T00:00:00Z', 'relevance', '2026-10-30', 'summary', '[]')"
                )
            _init_db(conn)

            cur = conn.cursor()
            cur.execute("SELECT source_date, retrieved_at_utc FROM research_radar WHERE id = 'radar-llama-3.2-edge'")
            llama_row = cur.fetchone()
            self.assertEqual(llama_row[0], "2024-09-25")
            self.assertEqual(llama_row[1], "2026-10-30T00:00:00Z")  # Preserves retrieval timestamp separately

            cur.execute("SELECT source_date FROM research_radar WHERE id = 'radar-gemini-1.5-flash-8b'")
            self.assertEqual(cur.fetchone()[0], "2024-10-03")
            conn.close()

    # --- Scenario 21: Candidate ranking zero-view edge case and division-by-zero protection ---
    def test_rank_optimization_candidates_with_zero_views(self):
        """Zero-view candidate is excluded from optimization, and zero peer median safely avoids division by zero."""
        from youtube_dashboard import rank_optimization_candidates

        # 1. Zero-view candidate is excluded from ranking (cannot diagnose packaging without traffic)
        video_rows = [
            {"video_id": "v-under", "title": "Zero views upload", "format": "longform", "views": 0, "age_days": 20},
            {"video_id": "v-peer1", "title": "Peer 1", "format": "longform", "views": 100, "age_days": 20},
            {"video_id": "v-peer2", "title": "Peer 2", "format": "longform", "views": 200, "age_days": 22},
            {"video_id": "v-peer3", "title": "Peer 3", "format": "longform", "views": 300, "age_days": 18},
        ]
        candidates = rank_optimization_candidates(video_rows)
        self.assertNotIn("v-under", [c["video_id"] for c in candidates])
        # v-peer1 has 100 views vs peer median of 200 (peers: v-under=0, v-peer2=200, v-peer3=300 -> median=200)
        self.assertEqual(candidates[0]["video_id"], "v-peer1")
        self.assertEqual(candidates[0]["peer_views_median"], 200)

        # 2. When all peers have 0 views (median = 0), candidate is skipped, preventing division by zero
        all_zero_rows = [
            {"video_id": "v-measured", "title": "Low views", "format": "longform", "views": 10, "age_days": 20},
            {"video_id": "v-z1", "title": "Zero 1", "format": "longform", "views": 0, "age_days": 20},
            {"video_id": "v-z2", "title": "Zero 2", "format": "longform", "views": 0, "age_days": 22},
            {"video_id": "v-z3", "title": "Zero 3", "format": "longform", "views": 0, "age_days": 18},
        ]
        zero_candidates = rank_optimization_candidates(all_zero_rows)
        self.assertEqual(zero_candidates, [])


if __name__ == "__main__":
    unittest.main()
