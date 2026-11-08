"""Regression tests verifying full audit and repair across single-chat metadata copilot.

Covers:
1. Multi-video batch routing & single-chat state clearance (preventing single-video collision)
2. Normal multiline & flattened single-line batch instruction parsing (JlxukCkusZc & uBH2f6sKZcc)
3. HTTP 400 error diagnosis & secret token redaction
4. Complete removal of YouTube Studio export analyzer and studio prompts
5. Metadata validation constraints & _writable_snippet sanitation
6. Snapshot-based stale proposal detection
"""

import unittest
from unittest.mock import MagicMock, patch

import core.batch_instructions as batch_instructions
import core.batch_metadata as batch_metadata
import strategist_chat
import youtube_analytics_connector as connector


ID_A = "JlxukCkusZc"
ID_B = "uBH2f6sKZcc"


class TestBatchRoutingAndStateClearance(unittest.TestCase):
    """Verifies that 2+ video references route deterministically to batch and clear single-video state."""

    def test_multi_video_references_detected(self):
        text = f"Update video {ID_A} and video {ID_B} with new titles."
        refs = strategist_chat._youtube_references_from_message(text)
        self.assertIn(ID_A, refs)
        self.assertIn(ID_B, refs)
        self.assertEqual(len(refs), 2)

    def test_multi_video_triggers_batch_intent(self):
        text = f"https://www.youtube.com/watch?v={ID_A} and https://youtu.be/{ID_B}"
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertTrue(strategist_chat._is_batch_intent(text, urls))

    def test_comparison_intent_not_routed_to_batch(self):
        text = f"Compare https://www.youtube.com/watch?v={ID_A} versus https://youtu.be/{ID_B} retention curve"
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertFalse(strategist_chat._is_batch_intent(text, urls))

    def test_staging_text_batch_clears_single_video_session_state(self):
        fake_state = {
            "strategist_target_reference": ID_A,
            "strategist_chat_proposal": {"title": "Old Single Proposal"},
            "strategist_chat_digest": "abcdef123456",
            "copilot_proposal": {"title": "Old Single Proposal"},
            "copilot_approval_digest": "abcdef123456",
        }
        with patch.object(strategist_chat, "st") as mock_st:
            mock_st.session_state = fake_state
            with patch.object(batch_metadata, "prepare_metadata_batch", return_value={"rows": []}):
                res = strategist_chat._stage_text_metadata_batch(f"{ID_A} and {ID_B}", youtube=None)
                self.assertIsNotNone(res)
                # Verify single-video keys are wiped
                self.assertIsNone(fake_state.get("strategist_target_reference"))
                self.assertIsNone(fake_state.get("strategist_chat_proposal"))
                self.assertIsNone(fake_state.get("strategist_chat_digest"))
                self.assertIsNone(fake_state.get("copilot_proposal"))
                self.assertIsNone(fake_state.get("copilot_approval_digest"))


class TestBatchInstructionParsing(unittest.TestCase):
    """Verifies parsing of multiline and flattened whitespace inputs for test IDs."""

    def test_multiline_parsing_extracts_both_videos(self):
        text = f"""
VIDEO 1: {ID_A}
CURRENT TITLE: Old Title 1
New title: AI Breakthrough That Changes Everything
Description: Detailed breakthrough overview with new benchmarks.
Tags: ai, breakthrough, machine learning

VIDEO 2: {ID_B}
CURRENT TITLE: Old Title 2
New title: 5 Agentic AI Design Patterns Explained
Description: Architecture patterns for production agents.
Tags: agents, architecture, engineering
"""
        channel_videos = [
            {"video_id": ID_A, "title": "Old Title 1", "description": "", "tags": []},
            {"video_id": ID_B, "title": "Old Title 2", "description": "", "tags": []},
        ]
        plan = batch_instructions.parse_batch_instructions(text, channel_videos=channel_videos)
        self.assertEqual(len(plan["videos"]), 2)
        v1 = plan["videos"][0]
        v2 = plan["videos"][1]
        self.assertEqual(v1["video_id"], ID_A)
        self.assertEqual(v1["title_intent"], "exact")
        self.assertEqual(v1["exact_title"], "AI Breakthrough That Changes Everything")
        self.assertEqual(v1["exact_description"], "Detailed breakthrough overview with new benchmarks.")
        self.assertIn("breakthrough", v1["exact_tags"])

        self.assertEqual(v2["video_id"], ID_B)
        self.assertEqual(v2["title_intent"], "exact")
        self.assertEqual(v2["exact_title"], "5 Agentic AI Design Patterns Explained")
        self.assertEqual(v2["exact_description"], "Architecture patterns for production agents.")
        self.assertIn("engineering", v2["exact_tags"])

    def test_flattened_single_line_parsing(self):
        # Flattened newlines where user or paste buffer removed all CRLFs
        text = (
            f"VIDEO 1: {ID_A} CURRENT TITLE: Old Title 1 "
            f"New title: Flattened AI Breakthrough "
            f"Description: Single line flattened description. "
            f"Tags: ai, flattened, test "
            f"VIDEO 2: {ID_B} CURRENT TITLE: Old Title 2 "
            f"New title: Flattened Architecture Patterns "
            f"Description: Second single line description. "
            f"Tags: patterns, agent, production"
        )
        channel_videos = [
            {"video_id": ID_A, "title": "Old Title 1", "description": "", "tags": []},
            {"video_id": ID_B, "title": "Old Title 2", "description": "", "tags": []},
        ]
        plan = batch_instructions.parse_batch_instructions(text, channel_videos=channel_videos)
        self.assertEqual(len(plan["videos"]), 2)
        v1 = plan["videos"][0]
        v2 = plan["videos"][1]
        self.assertEqual(v1["video_id"], ID_A)
        self.assertEqual(v1["exact_title"], "Flattened AI Breakthrough")
        self.assertEqual(v1["exact_description"], "Single line flattened description.")
        self.assertIn("flattened", v1["exact_tags"])

        self.assertEqual(v2["video_id"], ID_B)
        self.assertEqual(v2["exact_title"], "Flattened Architecture Patterns")
        self.assertEqual(v2["exact_description"], "Second single line description.")
        self.assertIn("patterns", v2["exact_tags"])


class TestHttpErrorDiagnosisAndSecurity(unittest.TestCase):
    """Verifies that Google HTTP 400 error payloads are cleanly unpacked and tokens redacted."""

    def test_extract_http_error_details_from_google_response(self):
        resp_body = b"""{
            "error": {
                "code": 400,
                "message": "Invalid tag containing angle brackets: <demo>",
                "errors": [
                    {
                        "message": "Invalid tag containing angle brackets: <demo>",
                        "domain": "youtube.video",
                        "reason": "invalidTag",
                        "location": "snippet.tags",
                        "locationType": "other"
                    }
                ]
            }
        }"""
        fake_resp = MagicMock(status=400)
        from googleapiclient.errors import HttpError
        error = HttpError(resp=fake_resp, content=resp_body)

        details = connector.extract_http_error_details(error)
        self.assertEqual(details["status"], 400)
        self.assertEqual(details["reasons"], ["invalidTag"])
        self.assertEqual(details["locations"], ["snippet.tags"])
        self.assertIn("Invalid tag containing angle brackets", details["message"])

    def test_token_redaction_in_error_message(self):
        fake_token = "ya29.a0AfH6SMD_secret_oauth_token_value_xyz"
        raw_error_text = f"HTTP 400 using Authorization: Bearer {fake_token} failed: bad request"
        formatted = strategist_chat._apply_error_message(RuntimeError(raw_error_text))
        self.assertNotIn(fake_token, formatted)
        self.assertIn("[REDACTED_AUTH_TOKEN]", formatted)

    def test_client_secret_redaction(self):
        secret = "client_secret_GOCSPX-1234567890abcdef"
        msg = f"Failed with {secret} rejected"
        formatted = strategist_chat._apply_error_message(RuntimeError(msg))
        self.assertNotIn(secret, formatted)
        self.assertIn("[REDACTED_SECRET]", formatted)


class TestStudioExportAnalyzerRemoval(unittest.TestCase):
    """Verifies studio export analyzer module is deleted and excluded from prompts."""

    def test_module_cannot_be_imported(self):
        with self.assertRaises(ModuleNotFoundError):
            import studio_export_analyzer  # noqa: F401

    def test_classify_video_format_solely_duration_and_url(self):
        # /shorts/ URL is YouTube Short
        res_short = strategist_chat._classify_video_format(100, is_short_url=True)
        self.assertEqual(res_short["format"], "YouTube Short")

        # > 180 seconds is Standard Video
        res_long = strategist_chat._classify_video_format(200, is_short_url=False)
        self.assertEqual(res_long["format"], "Standard Video")

        # <= 180 seconds without shorts url is Format unconfirmed
        res_unconfirmed = strategist_chat._classify_video_format(120, is_short_url=False)
        self.assertEqual(res_unconfirmed["format"], "Format unconfirmed")

    def test_build_prompt_has_no_studio_evidence(self):
        details = {
            "title": "Sample Title",
            "description": "Sample Desc",
            "tags": ["tag1"],
            "published_at": "2025-01-01T00:00:00Z",
            "duration_sec": 600,
            "views": 1000,
            "likes": 50,
            "comment_count": 10,
        }
        prompt = strategist_chat._build_prompt(
            target=details,
            reference={},
            transcript="",
            transcript_status="Unavailable",
            history=[],
            instruction="Improve this video",
            target_transcript="Sample Transcript",
            target_transcript_status="Available",
        )
        self.assertNotIn("Studio evidence", prompt)
        self.assertNotIn("Export evidence", prompt)


class TestMetadataConstraintsAndSanitation(unittest.TestCase):
    """Verifies YouTube Data API payload sanitation and strict pre-write validation."""

    def test_validate_metadata_rejects_angle_brackets(self):
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "<Invalid Title>", "description": "Valid Desc", "tags": ["tag1"]})

        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "Valid Title", "description": "Desc with <script>", "tags": ["tag1"]})

    def test_validate_metadata_rejects_commas_in_tags(self):
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "Valid Title", "description": "Valid Desc", "tags": ["tag1,tag2"]})

    def test_validate_metadata_rejects_oversized_fields(self):
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "A" * 101, "description": "Valid Desc", "tags": ["tag1"]})

        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "Valid Title", "description": "A" * 5001, "tags": ["tag1"]})

        # 501 characters total in tags
        with self.assertRaises(connector.MetadataValidationError):
            connector._validate_metadata({"title": "Valid Title", "description": "Valid Desc", "tags": ["a" * 90] * 6})

    def test_writable_snippet_excludes_defaultAudioLanguage(self):
        raw_snippet = {
            "title": "Title",
            "description": "Desc",
            "tags": ["tag"],
            "categoryId": "28",
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en-US",
            "thumbnails": {"default": {"url": "http://img"}},
            "channelTitle": "My Channel",
        }
        clean = connector._writable_snippet(raw_snippet)
        self.assertEqual(clean["defaultLanguage"], "en")
        self.assertNotIn("defaultAudioLanguage", clean)
        self.assertNotIn("thumbnails", clean)
        self.assertNotIn("channelTitle", clean)

    def test_stale_metadata_snapshot_ignores_dynamic_fields(self):
        before_snippet = {
            "title": "Title",
            "description": "Desc",
            "tags": ["tag"],
            "categoryId": "28",
            "defaultLanguage": "en",
        }
        # Current fetched snippet with dynamic extra fields from YouTube API
        current_fetched = {
            "title": "Title",
            "description": "Desc",
            "tags": ["tag"],
            "categoryId": "28",
            "defaultLanguage": "en",
            "publishedAt": "2025-01-01T00:00:00Z",
            "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/123/hqdefault.jpg"}},
        }
        self.assertEqual(
            connector._metadata_snapshot(before_snippet),
            connector._metadata_snapshot(current_fetched),
        )


if __name__ == "__main__":
    unittest.main()
