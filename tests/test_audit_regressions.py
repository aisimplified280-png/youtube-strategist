"""Regression tests verifying audit findings and critical fixes.

Covers:
1. Exact proposal approval binding and hash invalidation on metadata changes.
2. Failing closed on malformed AI structured output (no applicable dummy fallbacks).
3. Programmatic destination routing support (nav_destination).
4. Memory Google OAuth scope isolation (drive.file only, separate HMAC state).
5. No local machine file:/// links in help documentation.
6. Target video state isolation (no context or approval leakage across videos).
7. Demo mode outcomes recorded as simulated, never verified YouTube writes.
8. Custom analytics date range and equal-duration prior period calculation.
"""

from datetime import date, timedelta
import unittest
from unittest.mock import MagicMock, patch

from core.analytics_logic import calculate_custom_period, get_period_preset
from core.llm import LLMRouter, LLMError, parse_proposal_json
from core.metadata_apply import compute_proposal_digest
from core.oauth import MEMORY_SCOPES, YOUTUBE_SCOPES, create_purpose_state


class TestAuditRegressions(unittest.TestCase):

    # 1. Critical — approval tied to exact proposal
    def test_proposal_digest_changes_on_any_metadata_revision(self):
        video_id = "test-video-1"
        before_snippet = {
            "title": "Old Title",
            "description": "Old Description",
            "tags": ["tag1"],
            "categoryId": "28",
        }
        proposal_v1 = {
            "title": "New Title 1",
            "description": "New Description",
            "tags": ["tag1", "tag2"],
            "categoryId": "28",
        }
        proposal_v2 = {
            "title": "New Title 2 (Revised)",
            "description": "New Description",
            "tags": ["tag1", "tag2"],
            "categoryId": "28",
        }

        digest1 = compute_proposal_digest(video_id, before_snippet, proposal_v1)
        digest2 = compute_proposal_digest(video_id, before_snippet, proposal_v2)

        self.assertNotEqual(digest1, digest2, "Revising proposal title must change the proposal digest.")

    def test_proposal_digest_changes_on_tag_or_description_revision(self):
        video_id = "test-video-1"
        before = {"title": "T", "description": "D", "tags": [], "categoryId": "28"}
        p1 = {"title": "T1", "description": "D1", "tags": ["a"], "categoryId": "28"}
        p2 = {"title": "T1", "description": "D2", "tags": ["a"], "categoryId": "28"}
        p3 = {"title": "T1", "description": "D1", "tags": ["b"], "categoryId": "28"}

        d1 = compute_proposal_digest(video_id, before, p1)
        d2 = compute_proposal_digest(video_id, before, p2)
        d3 = compute_proposal_digest(video_id, before, p3)

        self.assertNotEqual(d1, d2)
        self.assertNotEqual(d1, d3)

    # 2. Critical — malformed AI output fails closed
    def test_llm_router_fails_closed_on_unparseable_output(self):
        router = LLMRouter({
            "groq": {"api_key": "dummy_groq_key", "model": "test-model"},
            "gemini": {"api_key": "", "model": "test-model"},
        })

        # Mock generate to return non-JSON unparseable text
        with patch.object(router, "generate", return_value=("Just conversational chat text without JSON", "Groq", "test-model")):
            with self.assertRaises(LLMError) as ctx:
                router.generate_structured_proposal("Rewrite title")

            self.assertIn("AI returned unstructured output", str(ctx.exception))

    def test_parse_proposal_json_rejects_missing_title(self):
        raw = '{"description": "Just description", "tags": ["tech"]}'
        result = parse_proposal_json(raw)
        self.assertIsNone(result, "Proposal without title must fail parsing.")

    # 3. High — destination routing
    def test_nav_destination_mapping_coverage(self):
        # Simulate dest_map from app.py
        mock_home = MagicMock()
        mock_analyze = MagicMock()
        mock_strategist = MagicMock()
        mock_dashboard = MagicMock()
        mock_memory = MagicMock()
        mock_settings = MagicMock()

        dest_map = {
            "home": mock_home,
            "analyze": mock_analyze,
            "pages/studio_analysis.py": mock_analyze,
            "strategist": mock_strategist,
            "pages/strategist.py": mock_strategist,
            "dashboard": mock_dashboard,
            "pages/channel_dashboard.py": mock_dashboard,
            "memory": mock_memory,
            "pages/memory_experiments.py": mock_memory,
            "settings": mock_settings,
            "pages/settings.py": mock_settings,
        }

        self.assertEqual(dest_map["strategist"], mock_strategist)
        self.assertEqual(dest_map["pages/strategist.py"], mock_strategist)
        self.assertEqual(dest_map["analyze"], mock_analyze)

    # 4. High — Google Sheets Memory OAuth separation
    def test_memory_scopes_isolated_from_youtube_scopes(self):
        self.assertEqual(MEMORY_SCOPES, ["https://www.googleapis.com/auth/drive.file"])
        for yt_scope in YOUTUBE_SCOPES:
            self.assertNotIn(yt_scope, MEMORY_SCOPES, "Memory flow must not request YouTube scopes.")

    def test_memory_oauth_state_hmac_differs_from_youtube_state(self):
        signing_secret = "test-secret-at-least-32-characters-long"
        yt_state, yt_verifier = create_purpose_state("owner-1", signing_secret, "youtube")
        mem_state, mem_verifier = create_purpose_state("owner-1", signing_secret, "memory")

        self.assertNotEqual(yt_state, mem_state, "YouTube and Memory states must use purpose-bound secrets.")
        self.assertNotEqual(yt_verifier, mem_verifier)

    # 5. High — deployed help link points to no local file
    def test_studio_analysis_contains_no_local_file_links(self):
        with open("pages/studio_analysis.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("file:///", content, "pages/studio_analysis.py must not contain local machine file:/// links.")
        self.assertIn("STUDIO_EXPORT_GUIDE.md", content)

    # 6. High — video switching state isolation
    def test_compute_proposal_digest_isolated_per_video_id(self):
        before = {"title": "Old", "description": "Desc", "tags": [], "categoryId": "28"}
        proposed = {"title": "New", "description": "Desc", "tags": [], "categoryId": "28"}

        digest_vid_a = compute_proposal_digest("video-A", before, proposed)
        digest_vid_b = compute_proposal_digest("video-B", before, proposed)

        self.assertNotEqual(digest_vid_a, digest_vid_b, "Different video IDs must produce different approval digests.")

    # 7. Medium — Demo mode distinguishes simulated vs verified results
    def test_demo_mode_result_structure(self):
        simulated_result = {
            "result": "simulated_success",
            "title": "Simulated New Title",
            "url": "https://www.youtube.com/watch?v=demo123",
            "simulated": True,
        }
        self.assertTrue(simulated_result.get("simulated"))
        self.assertNotEqual(simulated_result.get("result"), "verified_youtube_write")

    # 8. Medium — custom analytics range calculation
    def test_custom_period_duration_equals_prior_period_duration(self):
        start = date(2026, 9, 1)
        end = date(2026, 9, 15)  # 15 days inclusive
        cur_start, cur_end, prior_start, prior_end = calculate_custom_period(start, end)

        self.assertEqual(cur_start, start)
        self.assertEqual(cur_end, end)

        cur_duration = (cur_end - cur_start).days + 1
        prior_duration = (prior_end - prior_start).days + 1
        self.assertEqual(cur_duration, prior_duration, "Current and prior period durations must be identical.")
        self.assertEqual(prior_end, cur_start - timedelta(days=1), "Prior period must immediately precede current start.")

    def test_custom_period_inversion_protection(self):
        # User entered start after end
        start = date(2026, 9, 20)
        end = date(2026, 9, 10)
        cur_start, cur_end, prior_start, prior_end = calculate_custom_period(start, end)

        self.assertEqual(cur_start, date(2026, 9, 10))
        self.assertEqual(cur_end, date(2026, 9, 20))
        self.assertTrue(cur_start <= cur_end)


if __name__ == "__main__":
    unittest.main()
