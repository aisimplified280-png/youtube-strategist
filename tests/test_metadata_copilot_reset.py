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
from core.linter import lint_description
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

    # Criterion 3: Shorts URL parsing and classification precedence
    def test_03_shorts_url_parsing_and_classification(self):
        """Correctly parses /shorts/ URLs and classifies format with strict precedence rules."""
        short_url = "https://www.youtube.com/shorts/MjIHEGngvF8"
        video_id = strategist_chat.parse_video_id(short_url)
        self.assertEqual(video_id, "MjIHEGngvF8")

        # 1. Duration > 180s takes absolute precedence over /shorts/ URL
        fmt_240s_short_url = strategist_chat._classify_video_format(240, is_short_url=True)
        self.assertEqual(fmt_240s_short_url["format"], "Standard Video")
        self.assertEqual(fmt_240s_short_url["badge"], "Video (>3m)")

        # 2. Duration > 60s published before Oct 15, 2024 takes precedence over /shorts/ URL
        fmt_90s_old_short_url = strategist_chat._classify_video_format(
            90, is_short_url=True, upload_date="2024-05-01T10:00:00Z"
        )
        self.assertEqual(fmt_90s_old_short_url["format"], "Standard Video")
        self.assertEqual(fmt_90s_old_short_url["badge"], "Video (>60s Pre-Oct 2024)")

        # 3. Studio Shorts evidence with impossible duration (>180s or >60s pre-Oct 2024)
        fmt_studio_impossible_180 = strategist_chat._classify_video_format(
            240, studio_format="Shorts"
        )
        self.assertEqual(fmt_studio_impossible_180["format"], "Standard Video")
        self.assertEqual(fmt_studio_impossible_180["badge"], "Video (>3m)")

        fmt_studio_impossible_old = strategist_chat._classify_video_format(
            90, studio_format="Shorts", upload_date="2024-04-10"
        )
        self.assertEqual(fmt_studio_impossible_old["format"], "Standard Video")
        self.assertEqual(fmt_studio_impossible_old["badge"], "Video (>60s Pre-Oct 2024)")

        # 4. Conflicting Studio Standard Video and Shorts URL evidence
        fmt_conflict = strategist_chat._classify_video_format(
            50, is_short_url=True, studio_format="Videos"
        )
        self.assertEqual(fmt_conflict["format"], "Format unconfirmed")
        self.assertEqual(fmt_conflict["badge"], "Format unconfirmed")
        self.assertIn("Conflicting format evidence", fmt_conflict["note"])

        # 5. Valid post-October 15, 2024 Short evidence
        fmt_post_oct_url = strategist_chat._classify_video_format(
            120, is_short_url=True, upload_date="2024-11-01"
        )
        self.assertEqual(fmt_post_oct_url["format"], "YouTube Short")
        self.assertEqual(fmt_post_oct_url["badge"], "Short")

        fmt_post_oct_studio = strategist_chat._classify_video_format(
            120, studio_format="Shorts", upload_date="2024-11-01"
        )
        self.assertEqual(fmt_post_oct_studio["format"], "YouTube Short")
        self.assertEqual(fmt_post_oct_studio["badge"], "Short")

        # 6. Existing standard-video and unconfirmed cases
        fmt_standard_long = strategist_chat._classify_video_format(620, is_short_url=False)
        self.assertEqual(fmt_standard_long["format"], "Standard Video")
        self.assertEqual(fmt_standard_long["badge"], "Video (>3m)")

        fmt_unconfirmed_short = strategist_chat._classify_video_format(45, is_short_url=False)
        self.assertEqual(fmt_unconfirmed_short["format"], "Format unconfirmed")
        self.assertEqual(fmt_unconfirmed_short["badge"], "Format unconfirmed")

        fmt_unconfirmed_none = strategist_chat._classify_video_format(None, is_short_url=False)
        self.assertEqual(fmt_unconfirmed_none["format"], "Format unconfirmed")

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
        self.assertIn("title_angles (array of exactly 3 objects", prompt)
        self.assertIn("must exactly match one of the three title_angles titles", prompt)
        self.assertIn("first 150 characters of the description serve as a clear, engaging mobile search preview hook", prompt)
        self.assertIn("Return separate YouTube search tags matching developer query intent", prompt)

    def test_title_angles_accept_only_three_distinct_structured_options(self):
        angles = strategist_chat._extract_title_angles([
            {"angle": "curiosity", "title": "Why Prompt Caching Changes AI API Speed"},
            {"angle": "direct_value", "title": "Speed Up Python LLM Calls with Prompt Caching"},
            {"angle": "search", "title": "Python Prompt Caching Tutorial for LLM APIs"},
        ])
        self.assertEqual(len(angles), 3)
        self.assertEqual([item["label"] for item in angles], ["💡 Curiosity", "🎯 Direct Value", "🔍 Search / SEO"])

        invalid = strategist_chat._extract_title_angles([
            {"angle": "curiosity", "title": "Same title"},
            {"angle": "direct_value", "title": "Same title"},
            {"angle": "seo", "title": "A separate title"},
        ])
        self.assertEqual(invalid, [])
        self.assertEqual(strategist_chat._extract_title_angles([]), [])

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

    # Criterion 19: Owner-isolated proposal history
    def test_19_owner_isolation_for_proposal_history(self):
        """SQLite proposal history is scoped to the authenticated owner."""
        import uuid
        unique_owner = f"owner_{uuid.uuid4().hex}"

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


    # Write Protection & Payload Audit: Title-only update preserves description, tags, categoryId
    def test_21_update_title_only_preserves_description_tags_and_category_id(self):
        """Applying a title-only update preserves categoryId, description, and tags."""
        before_snippet = {
            "categoryId": "28",
            "title": "Original Title",
            "description": "Original Description with key context",
            "tags": ["python", "machine learning"],
        }
        proposed_snippet = {"title": "Updated Title Only"}

        # Direct writable snippet test
        body = connector._writable_snippet(proposed_snippet, before_snippet)
        self.assertEqual(body["title"], "Updated Title Only")
        self.assertEqual(body["description"], "Original Description with key context")
        self.assertEqual(body["tags"], ["python", "machine learning"])
        self.assertEqual(body["categoryId"], "28")

        # End-to-end apply test
        mock_youtube = MagicMock()
        mock_youtube.videos().list().execute.return_value = {
            "items": [{"id": "vid123", "snippet": {**before_snippet, "title": "Updated Title Only"}}]
        }
        mock_youtube.videos().update().execute.return_value = {
            "id": "vid123",
            "snippet": {**before_snippet, "title": "Updated Title Only"},
        }
        expected_verified_21 = {**before_snippet, "title": "Updated Title Only"}
        with patch.object(connector, "fetch_video_snippet", side_effect=[before_snippet, expected_verified_21]):
            digest = connector.metadata_proposal_digest("vid123", before_snippet, proposed_snippet)
            res = connector.apply_approved_dashboard_proposal(
                mock_youtube, "vid123", before_snippet, proposed_snippet, digest, audit_callback=MagicMock()
            )
            self.assertEqual(res["title"], "Updated Title Only")
            # Verify payload sent to YouTube API
            update_call = mock_youtube.videos().update.call_args[1]
            sent_snippet = update_call["body"]["snippet"]
            self.assertEqual(sent_snippet["title"], "Updated Title Only")
            self.assertEqual(sent_snippet["description"], before_snippet["description"])
            self.assertEqual(sent_snippet["tags"], before_snippet["tags"])
            self.assertEqual(sent_snippet["categoryId"], "28")

    # Write Protection & Payload Audit: Description-only update preserves title, tags, categoryId
    def test_22_update_description_only_preserves_title_tags_and_category_id(self):
        """Applying a description-only update preserves categoryId, title, and tags."""
        before_snippet = {
            "categoryId": "28",
            "title": "Unchanged Title",
            "description": "Old Description",
            "tags": ["ai", "copilot"],
        }
        proposed_snippet = {"description": "Brand new description text"}

        body = connector._writable_snippet(proposed_snippet, before_snippet)
        self.assertEqual(body["title"], "Unchanged Title")
        self.assertEqual(body["description"], "Brand new description text")
        self.assertEqual(body["tags"], ["ai", "copilot"])
        self.assertEqual(body["categoryId"], "28")

        mock_youtube = MagicMock()
        mock_youtube.videos().list().execute.return_value = {
            "items": [{"id": "vid123", "snippet": {**before_snippet, "description": "Brand new description text"}}]
        }
        mock_youtube.videos().update().execute.return_value = {
            "id": "vid123",
            "snippet": {**before_snippet, "description": "Brand new description text"},
        }
        expected_verified_22 = {**before_snippet, "description": "Brand new description text"}
        with patch.object(connector, "fetch_video_snippet", side_effect=[before_snippet, expected_verified_22]):
            digest = connector.metadata_proposal_digest("vid123", before_snippet, proposed_snippet)
            connector.apply_approved_dashboard_proposal(
                mock_youtube, "vid123", before_snippet, proposed_snippet, digest, audit_callback=MagicMock()
            )
            sent_snippet = mock_youtube.videos().update.call_args[1]["body"]["snippet"]
            self.assertEqual(sent_snippet["title"], "Unchanged Title")
            self.assertEqual(sent_snippet["description"], "Brand new description text")
            self.assertEqual(sent_snippet["tags"], ["ai", "copilot"])
            self.assertEqual(sent_snippet["categoryId"], "28")

    # Write Protection & Payload Audit: Tags-only update preserves title, description, categoryId
    def test_23_update_tags_only_preserves_title_description_and_category_id(self):
        """Applying a tags-only update preserves categoryId, title, and description."""
        before_snippet = {
            "categoryId": "28",
            "title": "Keep This Title",
            "description": "Keep This Description",
            "tags": ["oldtag"],
        }
        proposed_snippet = {"tags": ["newtag1", "newtag2"]}

        body = connector._writable_snippet(proposed_snippet, before_snippet)
        self.assertEqual(body["title"], "Keep This Title")
        self.assertEqual(body["description"], "Keep This Description")
        self.assertEqual(body["tags"], ["newtag1", "newtag2"])
        self.assertEqual(body["categoryId"], "28")

        mock_youtube = MagicMock()
        mock_youtube.videos().list().execute.return_value = {
            "items": [{"id": "vid123", "snippet": {**before_snippet, "tags": ["newtag1", "newtag2"]}}]
        }
        mock_youtube.videos().update().execute.return_value = {
            "id": "vid123",
            "snippet": {**before_snippet, "tags": ["newtag1", "newtag2"]},
        }
        expected_verified_23 = {**before_snippet, "tags": ["newtag1", "newtag2"]}
        with patch.object(connector, "fetch_video_snippet", side_effect=[before_snippet, expected_verified_23]):
            digest = connector.metadata_proposal_digest("vid123", before_snippet, proposed_snippet)
            connector.apply_approved_dashboard_proposal(
                mock_youtube, "vid123", before_snippet, proposed_snippet, digest, audit_callback=MagicMock()
            )
            sent_snippet = mock_youtube.videos().update.call_args[1]["body"]["snippet"]
            self.assertEqual(sent_snippet["title"], "Keep This Title")
            self.assertEqual(sent_snippet["description"], "Keep This Description")
            self.assertEqual(sent_snippet["tags"], ["newtag1", "newtag2"])
            self.assertEqual(sent_snippet["categoryId"], "28")

    # Write Protection & Payload Audit: All 3 fields updated preserves categoryId and defaultLanguage, excludes defaultAudioLanguage
    def test_24_update_all_three_fields_preserves_category_id_and_language(self):
        """Updating title, description, and tags together preserves categoryId and defaultLanguage, but excludes defaultAudioLanguage."""
        before_snippet = {
            "categoryId": "28",
            "title": "Old Title",
            "description": "Old Desc",
            "tags": ["tag1"],
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en-US",
        }
        proposed_snippet = {
            "title": "Complete New Title",
            "description": "Complete New Description",
            "tags": ["tagA", "tagB"],
        }

        body = connector._writable_snippet(proposed_snippet, before_snippet)
        self.assertEqual(body["title"], "Complete New Title")
        self.assertEqual(body["description"], "Complete New Description")
        self.assertEqual(body["tags"], ["tagA", "tagB"])
        self.assertEqual(body["categoryId"], "28")
        self.assertEqual(body["defaultLanguage"], "en")
        self.assertNotIn("defaultAudioLanguage", body, "defaultAudioLanguage must NOT be in writable snippet")

        mock_youtube = MagicMock()
        mock_youtube.videos().list().execute.return_value = {
            "items": [{"id": "vid123", "snippet": {**before_snippet, **proposed_snippet}}]
        }
        mock_youtube.videos().update().execute.return_value = {
            "id": "vid123",
            "snippet": {**before_snippet, **proposed_snippet},
        }
        expected_verified_24 = {**before_snippet, **proposed_snippet}
        with patch.object(connector, "fetch_video_snippet", side_effect=[before_snippet, expected_verified_24]):
            digest = connector.metadata_proposal_digest("vid123", before_snippet, proposed_snippet)
            connector.apply_approved_dashboard_proposal(
                mock_youtube, "vid123", before_snippet, proposed_snippet, digest, audit_callback=MagicMock()
            )
            sent_snippet = mock_youtube.videos().update.call_args[1]["body"]["snippet"]
            self.assertEqual(sent_snippet["title"], "Complete New Title")
            self.assertEqual(sent_snippet["description"], "Complete New Description")
            self.assertEqual(sent_snippet["tags"], ["tagA", "tagB"])
            self.assertEqual(sent_snippet["categoryId"], "28")
            self.assertEqual(sent_snippet["defaultLanguage"], "en")
            self.assertNotIn("defaultAudioLanguage", sent_snippet, "defaultAudioLanguage must NOT be sent to YouTube API")

    # Write Protection & Payload Audit: Enforce description UTF-8 byte limit
    def test_25_description_utf8_byte_limit_enforced(self):
        """Description exceeding 5000 UTF-8 bytes fails validation even if under 5000 characters."""
        emoji_desc = "🚀" * 1300
        self.assertLess(len(emoji_desc), 5000)
        self.assertGreater(len(emoji_desc.encode("utf-8")), 5000)

        with self.assertRaises(connector.MetadataValidationError) as cm:
            connector._validate_metadata({
                "title": "Valid Title",
                "description": emoji_desc,
                "tags": ["test"],
            })
        self.assertIn("5000 UTF-8 bytes", str(cm.exception))

        lint_res = lint_description(emoji_desc)
        self.assertTrue(any("5,000 UTF-8 byte limit" in w for w in lint_res["warnings"]))

    # Write Protection & Payload Audit: Missing categoryId fails closed
    def test_26_missing_category_id_fails_closed(self):
        """Snippet missing categoryId fails closed before sending any request to YouTube."""
        before_snippet = {
            "title": "Title Without Category",
            "description": "Desc Without Category",
            "tags": ["tag"],
        }
        proposed_snippet = {
            "title": "New Title",
        }

        with self.assertRaises(connector.MetadataValidationError) as cm:
            connector._writable_snippet(proposed_snippet, before_snippet)
        self.assertIn("categoryId is missing", str(cm.exception))

        mock_youtube = MagicMock()
        digest = connector.metadata_proposal_digest("vid123", before_snippet, proposed_snippet)
        with self.assertRaises(connector.MetadataValidationError) as cm2:
            connector.apply_approved_dashboard_proposal(
                mock_youtube, "vid123", before_snippet, proposed_snippet, digest, audit_callback=MagicMock()
            )
        self.assertIn("categoryId is missing", str(cm2.exception))
        mock_youtube.videos().update.assert_not_called()


    # Safety: Disconnected Live Mode raises clear ConnectionError
    def test_27_disconnected_live_mode_raises_connection_error(self):
        """When is_demo=False and youtube=None, raises ConnectionError without silent fallback to demo data."""
        with self.assertRaises(ConnectionError) as cm:
            strategist_chat.fetch_full_video_details(
                None, "vid12345678"
            )
        self.assertIn("YouTube is not connected", str(cm.exception))

        # Check that loading videos in chat returns clear connection error and None context
        context, error_msg = strategist_chat._load_conversation_videos(
            None, {}, "https://www.youtube.com/watch?v=vid12345678"
        )
        self.assertIsNone(context, "Context must be None when disconnected in live mode")
        self.assertIn("YouTube is not connected", error_msg)

    # Safety: No synthetic video fixtures or simulated publish pathway remain.
    def test_28_demo_mode_and_simulated_approval_are_removed(self):
        """Disconnected requests fail; only the real verified update path remains."""
        self.assertFalse(hasattr(strategist_chat, "_get_demo_video_details"))
        import inspect
        source = inspect.getsource(strategist_chat.render_copilot_chat)
        self.assertNotIn("Simulate Approval", source)
        self.assertNotIn("Demo Mode Active", source)

        with self.assertRaises(ConnectionError):
            strategist_chat.fetch_full_video_details(None, "demo1234567")

        with self.assertRaises(RuntimeError) as cm:
            connector.apply_approved_dashboard_proposal(
                None, "demo1234567", {"title": "old"}, {"title": "new"}, "a" * 64, audit_callback=MagicMock()
            )
        self.assertIn("YouTube API client is not connected", str(cm.exception))

    # Safety: Fail-closed ownership verification
    def test_29_fail_closed_ownership_verification(self):
        """Ownership verification fails closed on missing expected ID, missing video ID, or external ID."""
        # 1. Missing expected channel ID
        with patch.object(connector, "CHANNEL_ID", None):
            is_owner, status = strategist_chat._check_ownership(
                {"channelId": "UC123"}, configured_channel_id=None
            )
            self.assertFalse(is_owner)
            self.assertIn("Missing configured owner channel ID", status)

        # 2. Missing video channel ID
        is_owner, status = strategist_chat._check_ownership(
            {"channelId": ""}, configured_channel_id="UC123"
        )
        self.assertFalse(is_owner)
        self.assertIn("Missing video channel ID", status)

        # 3. External video channel ID
        is_owner, status = strategist_chat._check_ownership(
            {"channelId": "UC_OTHER"}, configured_channel_id="UC123"
        )
        self.assertFalse(is_owner)
        self.assertIn("External Video", status)

        # 4. Positively matching channel ID
        is_owner, status = strategist_chat._check_ownership(
            {"channelId": "UC123"}, configured_channel_id="UC123"
        )
        self.assertTrue(is_owner)
        self.assertEqual(status, "Owner Channel (Editable)")

    # Studio Attachment Context: Lifecycle and Format Recalculation
    def test_30_studio_attachments_lifecycle(self):
        """Attaching files refreshes evidence and format; removing files clears evidence and index."""
        # Active video loaded with standard watch URL and duration 50s (initially Format unconfirmed)
        context = {
            "target_id": "test_vid_123",
            "title": "Agent Tutorial",
            "duration_seconds": 50,
            "duration_formatted": "0:50",
            "published_at": "2024-11-01",
            "is_short_url": False,
            "format_info": {"format": "Format unconfirmed", "badge": "Format unconfirmed"},
            "studio_performance_evidence": [],
        }

        # 1. Attach a Studio file that classifies video as Shorts
        csv_content = (
            "Video ID,Video Title,Content Type,Views,Watch time (hours)\n"
            "test_vid_123,Agent Tutorial,Shorts,1500,20.5\n"
        ).encode("utf-8")

        mock_file = MagicMock()
        mock_file.name = "totals_report.csv"
        mock_file.getvalue.return_value = csv_content

        evidence_index, notice = strategist_chat.update_studio_attachments(context, [mock_file])
        self.assertIn("id:test_vid_123", evidence_index)
        self.assertIn("Parsed 1 Studio report", notice)
        self.assertEqual(len(context["studio_performance_evidence"]), 1)
        self.assertEqual(context["format_info"]["format"], "YouTube Short")
        self.assertEqual(context["format_info"]["badge"], "Short")

        # 2. Remove all uploaded files (empty list)
        empty_index, empty_notice = strategist_chat.update_studio_attachments(context, [])
        self.assertEqual(empty_index, {})
        self.assertIsNone(empty_notice)
        self.assertEqual(len(context["studio_performance_evidence"]), 0)
        self.assertEqual(context["format_info"]["format"], "Format unconfirmed")
        self.assertEqual(context["format_info"]["badge"], "Format unconfirmed")


if __name__ == "__main__":
    unittest.main()
