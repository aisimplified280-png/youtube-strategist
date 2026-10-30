"""Comprehensive test suite for AI Simplified Lab — YouTube Metadata Copilot Product Reset.

Verifies all 20 acceptance criteria specified in Section 11 of the reset specification:
1. Single chat landing (no multipage navigation).
2. Regular video URL parsing (standard, shortened, embed).
3. Shorts URL parsing and classification.
4. Video details retrieval (title, desc, tags, duration, format, real metrics, transcript).
5. Unavailable / private video handling (VideoNotFoundError).
6. Public external video inspection (read-only, write disabled).
7. Target video switching (invalidates previous proposals and digests).
8. Multi-file Studio upload in-memory.
9. Exact ID matching with title fallback.
10. Date ranges separated (never summed or averaged).
11. Studio evidence in LLM prompt.
12. Metadata proposal formulation (3 title angles, mobile description, hashtags, tags).
13. Conversational revision flow.
14. Approval digest invalidation on revision.
15. Validation enforcement (Title <=100, Desc <=5000, Tags <=500).
16. Pre-write stale check prevents overwriting modified YouTube metadata.
17. Post-write verification retry loop.
18. Fail-safe LLM fallback (Groq / Gemini / heuristic).
19. Owner isolation and Demo Mode safety.
20. Responsive layout (max-width 880px, min 44px touch targets).
"""

import json
import os
import unittest
from unittest.mock import MagicMock, Mock, patch

import strategist_chat
import studio_export_analyzer
import youtube_analytics_connector as connector
from core.creator_db import record_proposal_decision, get_recent_proposal_history


class MetadataCopilotAcceptanceTests(unittest.TestCase):
    """Test suite covering the 20 acceptance criteria for the Metadata Copilot reset."""

    def setUp(self):
        self.session_state = {}

    # Criterion 1: Single chat landing (no multipage navigation)
    def test_01_single_chat_landing_no_multipage_navigation(self):
        """app.py must not use st.navigation or multi-page routing."""
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            app_code = f.read()

        self.assertNotIn("st.navigation", app_code, "app.py must not use st.navigation")
        self.assertNotIn("page_today", app_code, "app.py must not define today page")
        self.assertNotIn("page_workspace", app_code, "app.py must not define workspace page")
        self.assertNotIn("page_ideas", app_code, "app.py must not define ideas page")
        self.assertNotIn("page_memory", app_code, "app.py must not define memory page")
        self.assertIn("render_copilot_chat", app_code, "app.py must render the copilot chat directly")

    # Criterion 2: Regular video URL parsing
    def test_02_regular_video_url_parsing(self):
        """Correctly parses standard, shortened, and embed YouTube URLs."""
        test_cases = [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtube.com/watch?v=dQw4w9WgXcQ&feature=shared", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ]
        for url, expected_id in test_cases:
            with self.subTest(url=url):
                self.assertEqual(strategist_chat.parse_video_id(url), expected_id)

    # Criterion 3: Shorts URL parsing and classification
    def test_03_shorts_url_parsing_and_classification(self):
        """Correctly parses /shorts/ URLs and classifies format as YouTube Short."""
        short_url = "https://www.youtube.com/shorts/MjIHEGngvF8"
        video_id = strategist_chat.parse_video_id(short_url)
        self.assertEqual(video_id, "MjIHEGngvF8")

        # Classification by URL
        format_info_url = strategist_chat._classify_video_format(None, is_short_url=True)
        self.assertEqual(format_info_url["format"], "YouTube Short")
        self.assertIn("Confirmed by YouTube Shorts URL", format_info_url["note"])

        # Classification by duration <= 60s
        format_info_sec = strategist_chat._classify_video_format(45, is_short_url=False)
        self.assertEqual(format_info_sec["format"], "Short Candidate")

        # Standard video > 60s
        format_info_long = strategist_chat._classify_video_format(620, is_short_url=False)
        self.assertEqual(format_info_long["format"], "Standard Video")

    # Criterion 4: Video details retrieval
    def test_04_video_details_retrieval(self):
        """Fetches title, description, tags, duration, format, real view/like/comment counts, and transcript."""
        mock_youtube = MagicMock()
        mock_response = {
            "items": [{
                "id": "abc123def45",
                "snippet": {
                    "title": "Real Production Test",
                    "description": "Video description here.",
                    "tags": ["python", "ai"],
                    "channelTitle": "AI Simplified Lab",
                    "channelId": "UC_TEST_CHANNEL_123",
                    "publishedAt": "2026-05-10T12:00:00Z",
                    "categoryId": "28",
                },
                "contentDetails": {
                    "duration": "PT5M30S",
                },
                "statistics": {
                    "viewCount": "4500",
                    "likeCount": "210",
                    "commentCount": "35",
                },
            }]
        }
        mock_youtube.videos().list().execute.return_value = mock_response

        with patch("strategist_chat._transcript_text", return_value=("[00:01] Hello world", "Transcript loaded")):
            details = strategist_chat.fetch_full_video_details(
                mock_youtube,
                "abc123def45",
                owner_channel_id="UC_TEST_CHANNEL_123",
            )

        self.assertEqual(details["target_id"], "abc123def45")
        self.assertEqual(details["title"], "Real Production Test")
        self.assertEqual(details["duration_seconds"], 330)
        self.assertEqual(details["duration_formatted"], "5:30")
        self.assertEqual(details["views"], 4500)
        self.assertEqual(details["likes"], 210)
        self.assertEqual(details["comments"], 35)
        self.assertTrue(details["is_owner"])
        self.assertEqual(details["target_transcript"], "[00:01] Hello world")

    # Criterion 5: Unavailable / private video handling
    def test_05_unavailable_or_private_video_handling(self):
        """Displays clear error message if video does not exist or is private without crashing."""
        mock_youtube = MagicMock()
        mock_youtube.videos().list().execute.return_value = {"items": []}

        with self.assertRaises(connector.VideoNotFoundError):
            strategist_chat.fetch_full_video_details(mock_youtube, "nonexistent1")

    # Criterion 6: Public external video inspection
    def test_06_public_external_video_inspection_read_only(self):
        """Allows inspecting external videos, but disables metadata write action."""
        mock_youtube = MagicMock()
        mock_response = {
            "items": [{
                "id": "external1234",
                "snippet": {
                    "title": "Competitor Video",
                    "description": "External creator description",
                    "tags": ["competitor"],
                    "channelTitle": "External Channel",
                    "channelId": "UC_OTHER_CHANNEL_999",
                    "publishedAt": "2026-01-01T00:00:00Z",
                },
                "contentDetails": {"duration": "PT10M"},
                "statistics": {"viewCount": "100000"},
            }]
        }
        mock_youtube.videos().list().execute.return_value = mock_response

        with patch("strategist_chat._transcript_text", return_value=("", "No transcript")):
            details = strategist_chat.fetch_full_video_details(
                mock_youtube,
                "external1234",
                owner_channel_id="UC_MY_OWN_CHANNEL",
            )

        self.assertEqual(details["target_id"], "external1234")
        self.assertFalse(details["is_owner"])
        self.assertIn("External Video", details["ownership_status"])

    # Criterion 7: Target video switching
    def test_07_target_video_switching_invalidates_proposals_and_digests(self):
        """Pasting a new URL updates active context and clears previous proposal and approval digests."""
        session_state = {
            "strategist_target_reference": {"target_id": "vid11111111"},
            "strategist_chat_proposal": {"target_id": "vid11111111", "proposed": {}},
            "strategist_chat_digest": "old_approval_digest_12345",
            "copilot_proposal": {"target_id": "vid11111111"},
            "copilot_approval_digest": "old_approval_digest_12345",
        }
        mock_youtube = MagicMock()
        mock_response = {
            "items": [{
                "id": "vid22222222",
                "snippet": {"title": "Second Video", "description": "", "tags": []},
                "contentDetails": {"duration": "PT2M"},
                "statistics": {},
            }]
        }
        mock_youtube.videos().list().execute.return_value = mock_response

        with patch("strategist_chat.st.session_state", session_state), patch(
            "strategist_chat._transcript_text", return_value=("", "")
        ):
            context, err = strategist_chat._load_conversation_videos(
                mock_youtube, {}, "https://www.youtube.com/watch?v=vid22222222"
            )

        self.assertEqual(context["target_id"], "vid22222222")
        self.assertNotIn("strategist_chat_proposal", session_state)
        self.assertNotIn("strategist_chat_digest", session_state)
        self.assertNotIn("copilot_proposal", session_state)
        self.assertNotIn("copilot_approval_digest", session_state)

    # Criterion 8: Multi-file Studio upload in-memory
    def test_08_multi_file_studio_upload_in_memory(self):
        """User can attach multiple CSV/TSV/XLSX files simultaneously in memory."""
        csv1 = (
            "Video,Video title,Video publish time,Views,Impressions,Impressions click-through rate (%)\n"
            "vid11111111,First Title,May 1 2026,100,2000,5.0\n"
        ).encode("utf-8")
        csv2 = (
            "Video,Video title,Video publish time,Views,Average percentage viewed (%)\n"
            "vid22222222,Second Title,May 2 2026,500,45.2\n"
        ).encode("utf-8")

        mock_file1 = Mock(name="content.csv", spec=["name", "getvalue"])
        mock_file1.name = "content.csv"
        mock_file1.getvalue.return_value = csv1

        mock_file2 = Mock(name="totals.csv", spec=["name", "getvalue"])
        mock_file2.name = "totals.csv"
        mock_file2.getvalue.return_value = csv2

        index, notices = studio_export_analyzer.index_uploaded_studio_files([mock_file1, mock_file2])
        self.assertIn("id:vid11111111", index)
        self.assertIn("id:vid22222222", index)
        self.assertEqual(len(notices), 2)

    # Criterion 9: Exact ID matching with title fallback
    def test_09_exact_id_matching_with_title_fallback(self):
        """Studio data is matched to active video by exact ID first, with title fallback second."""
        evidence_index = {
            "id:targetid111": [{"source_file": "file1.csv", "views": 1000}],
            "title:my exact video title": [{"source_file": "file2.csv", "views": 2000}],
        }
        # Exact ID match
        ev1 = studio_export_analyzer.find_video_evidence(evidence_index, "targetid111", "Different Title")
        self.assertEqual(len(ev1), 1)
        self.assertEqual(ev1[0]["views"], 1000)

        # Title fallback match
        ev2 = studio_export_analyzer.find_video_evidence(evidence_index, "unknownid22", "My Exact Video Title")
        self.assertEqual(len(ev2), 1)
        self.assertEqual(ev2[0]["views"], 2000)

    # Criterion 10: Date ranges separated (never summed or averaged)
    def test_10_date_ranges_separated_never_summed(self):
        """Metrics from different timeframes are reported independently, never summed together."""
        evidence = [
            {"source_file": "sep.csv", "date_start": "2026-09-01", "date_end": "2026-09-07", "views": 100},
            {"source_file": "oct.csv", "date_start": "2026-10-01", "date_end": "2026-10-07", "views": 200},
        ]
        prompt = strategist_chat._build_prompt(
            {"title": "Target", "description": "", "tags": []},
            {},
            "",
            "",
            [],
            "Optimize metadata",
            studio_evidence=evidence,
        )
        self.assertIn("2026-09-01", prompt)
        self.assertIn("2026-10-01", prompt)
        self.assertIn("never sum them unless the report itself represents non-overlapping rows", prompt)

    # Criterion 11: Studio evidence in LLM prompt
    def test_11_studio_evidence_in_llm_prompt(self):
        """Extracted metrics (CTR, retention, APV) are incorporated into the LLM optimization prompt."""
        evidence = [{
            "source_file": "retention.csv",
            "ctr_percent": 3.8,
            "average_percentage_viewed": 52.4,
            "impressions": 12000,
        }]
        prompt = strategist_chat._build_prompt(
            {"title": "Target", "description": "", "tags": []},
            {},
            "",
            "",
            [],
            "Review performance",
            studio_evidence=evidence,
        )
        self.assertIn("3.8", prompt)
        self.assertIn("52.4", prompt)
        self.assertIn("12000", prompt)

    # Criterion 12: Metadata proposal formulation with 3 title angles
    def test_12_metadata_proposal_formulation_three_angles(self):
        """Prompt requires 3 distinct title angles, mobile-optimized description with hashtags, and separate tags."""
        prompt = strategist_chat._build_prompt(
            {"title": "Old Title", "description": "Old desc", "tags": ["old"]},
            {},
            "",
            "",
            [],
            "Suggest new metadata",
        )
        self.assertIn("1) Curiosity / Intrigue", prompt)
        self.assertIn("2) Direct Value / Outcome", prompt)
        self.assertIn("3) Search / SEO", prompt)
        self.assertIn("first 150 characters of the description serve as a clear, engaging mobile search preview hook", prompt)
        self.assertIn("Return separate YouTube search tags matching developer query intent", prompt)

    # Criterion 13: Conversational revision flow
    def test_13_conversational_revision_flow(self):
        """Chat history turn appends and prompts retain conversational instructions."""
        history = []
        strategist_chat._append_history(history, "user", "Make the title punchier")
        strategist_chat._append_history(history, "assistant", "Here is a punchier option")
        strategist_chat._append_history(history, "user", "Add Python to tags")

        prompt = strategist_chat._build_prompt(
            {"title": "Punchy Title", "description": "", "tags": ["python"]},
            {},
            "",
            "",
            history,
            "Add Python to tags",
        )
        self.assertIn("USER: Make the title punchier", prompt)
        self.assertIn("ASSISTANT: Here is a punchier option", prompt)
        self.assertIn("LATEST USER INSTRUCTION:\nAdd Python to tags", prompt)

    # Criterion 14: Approval digest invalidation on revision
    def test_14_approval_digest_invalidation_on_revision(self):
        """Digest calculation produces deterministic SHA-256 and invalidates upon any field change."""
        meta1 = {"title": "Title A", "description": "Desc A", "tags": ["tag1"]}
        meta2 = {"title": "Title B", "description": "Desc A", "tags": ["tag1"]}

        digest1 = strategist_chat._proposal_digest("target_vid", meta1)
        digest2 = strategist_chat._proposal_digest("target_vid", meta2)

        self.assertNotEqual(digest1, digest2, "Any change to proposed metadata must invalidate approval digest")
        # Same metadata gives identical digest
        self.assertEqual(digest1, strategist_chat._proposal_digest("target_vid", meta1))

    # Criterion 15: Validation enforcement (limits)
    def test_15_validation_enforcement_blocks_oversized_metadata(self):
        """Metadata exceeding character limits raises MetadataValidationError."""
        long_title = "A" * 101
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": long_title, "description": "valid", "tags": ["a"]})

        long_desc = "B" * 5001
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "Valid", "description": long_desc, "tags": ["a"]})

        long_tags = ["tag_" + str(i) * 30 for i in range(25)]  # total > 500
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "Valid", "description": "Valid", "tags": long_tags})

    # Criterion 16: Pre-write stale check blocks writing if YouTube metadata changed
    def test_16_pre_write_stale_check_blocks_write_on_drift(self):
        """Write is blocked if YouTube snippet changed after proposal generation."""
        mock_youtube = MagicMock()
        video_id = "vid12345678"
        before_snippet = {
            "title": "Original Stale Title",
            "description": "Original Description",
            "tags": ["original"],
            "categoryId": "28",
        }
        proposed_snippet = {
            "title": "New Approved Title",
            "description": "New Approved Description",
            "tags": ["new_tag"],
            "categoryId": "28",
        }
        approved_digest = connector.metadata_proposal_digest(video_id, before_snippet, proposed_snippet)

        # YouTube returns drifted snippet
        drifted_snippet = {
            "title": "Drifted Title On YouTube",
            "description": "Drifted Description",
            "tags": ["drifted"],
            "categoryId": "28",
        }
        with patch("youtube_analytics_connector.fetch_video_snippet", return_value=drifted_snippet):
            with self.assertRaises(RuntimeError) as cm:
                connector.apply_approved_dashboard_proposal(
                    mock_youtube,
                    video_id,
                    before_snippet,
                    proposed_snippet,
                    approved_digest,
                    audit_callback=None,
                )
            self.assertIn("Video metadata changed after review", str(cm.exception))

    # Criterion 17: Post-write verification retry loop
    def test_17_post_write_verification_retry_loop(self):
        """Verifies updated metadata via YouTube API before returning success."""
        mock_youtube = MagicMock()
        video_id = "vid12345678"
        before_snippet = {
            "title": "Original Title",
            "description": "Original Description",
            "tags": ["orig"],
            "categoryId": "28",
        }
        proposed_snippet = {
            "title": "New Title",
            "description": "New Description",
            "tags": ["new"],
            "categoryId": "28",
        }
        approved_digest = connector.metadata_proposal_digest(video_id, before_snippet, proposed_snippet)

        with patch("youtube_analytics_connector.fetch_video_snippet", side_effect=[before_snippet, proposed_snippet]):
            result = connector.apply_approved_dashboard_proposal(
                mock_youtube,
                video_id,
                before_snippet,
                proposed_snippet,
                approved_digest,
                audit_callback=None,
            )
        self.assertEqual(result.get("result"), "success")

    # Criterion 18: Fail-safe LLM fallback
    @patch("strategist_chat.requests.post")
    def test_18_fail_safe_llm_fallback(self, mock_post):
        """Falls back from primary provider to secondary on failure."""
        groq_resp = Mock(status_code=429)
        gemini_resp = Mock(status_code=200)
        gemini_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "assistant_message": "Gemini fallback recommendation",
                            "title": "Fallback Title",
                            "description": "Fallback description",
                            "tags": ["fallback"],
                            "rationale": ["Handled by secondary provider"],
                        })
                    }]
                }
            }]
        }
        mock_post.side_effect = [groq_resp, gemini_resp]

        result, provider, model = strategist_chat._generate_with_fallbacks(
            groq_key="groq-key",
            groq_model="groq-model",
            groq_fallbacks=[],
            gemini_key="gemini-key",
            gemini_model="gemini-model",
            gemini_fallbacks=[],
            prompt="Optimize video",
        )
        self.assertEqual(provider, "Gemini")
        self.assertEqual(result["title"], "Fallback Title")

    # Criterion 19: Owner isolation and Demo Mode safety
    def test_19_owner_isolation_and_demo_mode_safety(self):
        """SQLite proposal_history records owner decisions and Demo Mode returns safe static data."""
        import uuid
        unique_owner = f"owner_{uuid.uuid4().hex}"

        # Test demo mode isolation
        demo_history = get_recent_proposal_history(limit=5, is_demo=True)
        self.assertIsInstance(demo_history, list)
        self.assertGreater(len(demo_history), 0)

        # Test live recording with owner isolation
        record_proposal_decision(
            video_id="test_demo_vid",
            proposal_digest="demo_digest_abc",
            status="applied",
            proposal_dict={"title": "New Title"},
            feedback="Applied successfully",
            owner_id=unique_owner,
        )

        history = get_recent_proposal_history(owner_id=unique_owner)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["video_id"], "test_demo_vid")
        self.assertEqual(history[0]["status"], "applied")

        # Other owner sees no records
        other_history = get_recent_proposal_history(owner_id=f"other_{uuid.uuid4().hex}")
        self.assertEqual(len(other_history), 0)

    # Criterion 20: Responsive layout properties
    def test_20_responsive_layout_properties_in_theme(self):
        """ui/theme.py defines Claude-inspired max-width 880px and >=44px touch targets."""
        theme_path = os.path.join(os.path.dirname(__file__), "..", "ui", "theme.py")
        with open(theme_path, "r", encoding="utf-8") as f:
            theme_code = f.read()

        self.assertIn("max-width: 880px", theme_code, "Layout must center at max-width 880px")
        self.assertIn("min-height: 44px", theme_code, "Interactive controls must maintain >=44px touch targets")
        self.assertIn("copilot-video-card", theme_code, "Must define copilot container styles")


if __name__ == "__main__":
    unittest.main()
