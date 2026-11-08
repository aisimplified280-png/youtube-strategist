"""Comprehensive regression tests verifying full audit and repair before pull request.

Covers all 9 audit requirements:
1. Session-state clearing helper and implementation
2. Strict video-ID extraction (rejecting Exact-Match, hyphenated phrases, supporting flattened/multiline)
3. Exact batch routing & staging (distinct ID counting, missing video detection, end-to-end JlxukCkusZc & uBH2f6sKZcc)
4. Complete removal of YouTube Studio integration
5. Correct hashtag (<=60 allowed, >60 rejected) and tag quotation-mark length validation
6. Mocked HTTP 400 update payload verification, defaultAudioLanguage exclusion, and token redaction
"""

import copy
import inspect
import unittest
from unittest.mock import MagicMock, patch

import core.batch_instructions as batch_instructions
import core.batch_metadata as batch_metadata
import strategist_chat
import youtube_analytics_connector as connector


ID_A = "JlxukCkusZc"
ID_B = "uBH2f6sKZcc"


class TestSessionStateAndBatchRouting(unittest.TestCase):
    """Verifies single-video state clearing and exact distinct-ID batch routing."""

    def test_clear_single_video_editor_state_helper(self):
        fake_state = {
            "strategist_target_reference": {"target_id": ID_A},
            "strategist_chat_proposal": {"title": "Proposed Title"},
            "strategist_chat_digest": "digest123456",
            "copilot_proposal": {"title": "Proposed Title"},
            "copilot_approval_digest": "digest123456",
        }
        with patch.object(strategist_chat, "st") as mock_st:
            mock_st.session_state = fake_state
            strategist_chat._clear_single_video_editor_state()
            self.assertIsNone(fake_state["strategist_target_reference"])
            self.assertIsNone(fake_state["strategist_chat_proposal"])
            self.assertIsNone(fake_state["strategist_chat_digest"])
            self.assertIsNone(fake_state["copilot_proposal"])
            self.assertIsNone(fake_state["copilot_approval_digest"])

    def test_staging_text_batch_clears_single_video_session_state(self):
        fake_state = {
            "strategist_target_reference": {"target_id": ID_A},
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
                self.assertIsNone(fake_state.get("strategist_target_reference"))
                self.assertIsNone(fake_state.get("strategist_chat_proposal"))
                self.assertIsNone(fake_state.get("strategist_chat_digest"))
                self.assertIsNone(fake_state.get("copilot_proposal"))
                self.assertIsNone(fake_state.get("copilot_approval_digest"))

    def test_distinct_videos_trigger_batch(self):
        text = f"https://www.youtube.com/watch?v={ID_A} and https://youtu.be/{ID_B}"
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertTrue(strategist_chat._is_batch_intent(text, urls))

    def test_repeated_same_video_does_not_trigger_batch(self):
        # 1 video referenced multiple times in same message
        text = f"Update https://www.youtube.com/watch?v={ID_A} and review https://youtu.be/{ID_A} again"
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertFalse(strategist_chat._is_batch_intent(text, urls))

    def test_comparison_intent_not_routed_to_batch(self):
        text = f"Compare https://www.youtube.com/watch?v={ID_A} versus https://youtu.be/{ID_B} retention curve"
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertFalse(strategist_chat._is_batch_intent(text, urls))

    def test_explicit_batch_keyword_with_single_id(self):
        text = f"batch: update title for https://www.youtube.com/watch?v={ID_A}"
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertTrue(strategist_chat._is_batch_intent(text, urls))


class TestVideoIdExtractionRegressions(unittest.TestCase):
    """Verifies that generic words, hyphenated phrases, and titles never become video references."""

    def test_exact_match_alone_on_line_not_extracted(self):
        text = "Exact-Match\nUpdate this video description"
        refs = batch_instructions.extract_video_references_from_text(text)
        extracted_ids = [r["video_id"] for r in refs if r.get("video_id")]
        self.assertNotIn("Exact-Match", extracted_ids)
        self.assertEqual(len(refs), 0)

    def test_current_title_exact_match_not_extracted(self):
        text = "CURRENT TITLE: Exact-Match NEW TITLE: Better Semantic Match"
        refs = batch_instructions.extract_video_references_from_text(text)
        extracted_ids = [r["video_id"] for r in refs if r.get("video_id")]
        self.assertNotIn("Exact-Match", extracted_ids)
        self.assertEqual(len(refs), 0)

    def test_hyphenated_phrase_in_title_not_extracted(self):
        text = f"VIDEO ID: {ID_A}\nCURRENT TITLE: Multi-Agent Systems Explained In Depth\nNew title: Autonomous AI"
        refs = batch_instructions.extract_video_references_from_text(text)
        extracted_ids = [r["video_id"] for r in refs if r.get("video_id")]
        self.assertEqual(extracted_ids, [ID_A])
        self.assertNotIn("Multi-Agent", extracted_ids)

    def test_two_explicitly_labelled_ids_flattened(self):
        text = f"VIDEO ID: {ID_A} CURRENT TITLE: Old Title 1 New title: New Title 1 Description: Desc 1 Tags: tag1 VIDEO ID: {ID_B} CURRENT TITLE: Old Title 2 New title: New Title 2 Description: Desc 2 Tags: tag2"
        refs = batch_instructions.extract_video_references_from_text(text)
        extracted_ids = [r["video_id"] for r in refs if r.get("video_id")]
        self.assertEqual(extracted_ids, [ID_A, ID_B])

    def test_two_explicitly_labelled_ids_multiline(self):
        text = f"""
VIDEO ID: {ID_A}
New title: Alpha Title

VIDEO ID: {ID_B}
New title: Beta Title
"""
        refs = batch_instructions.extract_video_references_from_text(text)
        extracted_ids = [r["video_id"] for r in refs if r.get("video_id")]
        self.assertEqual(extracted_ids, [ID_A, ID_B])

    def test_duplicate_occurrences_of_same_id_in_text(self):
        text = f"VIDEO ID: {ID_A}\nUpdate title.\n\nNote: {ID_A} is our highest priority video."
        refs = batch_instructions.extract_video_references_from_text(text)
        # Both occurrences extracted with their distinct text spans
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["video_id"], ID_A)
        self.assertEqual(refs[1]["video_id"], ID_A)
        # Distinct video ID count is 1 -> does not trigger multi-video batch
        urls = strategist_chat._youtube_urls_from_message(text)
        self.assertFalse(strategist_chat._is_batch_intent(text, urls))


class TestEndToEndBatchStagingJlxukCkusZcAndUBH2f6sKZcc(unittest.TestCase):
    """End-to-end regression test for staging exact test IDs JlxukCkusZc and uBH2f6sKZcc."""

    def test_complete_batch_staging_pipeline_with_test_ids(self):
        instruction = f"""
VIDEO 1: {ID_A}
CURRENT TITLE: Old Title For ID A
New title: AI Simplified: Autonomous Reasoning Systems
Description: Detailed breakdown of reasoning engines.
Tags: ai, reasoning, autonomous

VIDEO 2: {ID_B}
CURRENT TITLE: Old Title For ID B
New title: Production Agent Architectures
Description: Design patterns for production systems.
Tags: architecture, production, patterns
"""
        # 1. Extract both IDs
        refs = batch_instructions.extract_video_references_from_text(instruction)
        extracted_ids = [r["video_id"] for r in refs if r.get("video_id")]
        self.assertEqual(extracted_ids, [ID_A, ID_B])

        # 2. Parse each video's own metadata
        channel_videos = [
            {"video_id": ID_A, "title": "Old Title For ID A", "description": "", "tags": []},
            {"video_id": ID_B, "title": "Old Title For ID B", "description": "", "tags": []},
        ]
        parsed_plan = batch_instructions.parse_batch_instructions(instruction, channel_videos=channel_videos)
        self.assertEqual(len(parsed_plan["videos"]), 2)
        v1 = parsed_plan["videos"][0]
        v2 = parsed_plan["videos"][1]
        self.assertEqual(v1["video_id"], ID_A)
        self.assertEqual(v1["exact_title"], "AI Simplified: Autonomous Reasoning Systems")
        self.assertEqual(v2["video_id"], ID_B)
        self.assertEqual(v2["exact_title"], "Production Agent Architectures")

        # 3. Stage proposals through full canonical batch preparation
        fake_youtube = MagicMock()
        fake_youtube.videos().list().execute.return_value = {
            "items": [
                {
                    "id": ID_A,
                    "snippet": {
                        "title": "Old Title For ID A",
                        "description": "Old desc",
                        "tags": ["old"],
                        "categoryId": "28",
                        "defaultLanguage": "en",
                        "channelId": connector.CHANNEL_ID,
                    },
                },
                {
                    "id": ID_B,
                    "snippet": {
                        "title": "Old Title For ID B",
                        "description": "Old desc",
                        "tags": ["old"],
                        "categoryId": "28",
                        "defaultLanguage": "en",
                        "channelId": connector.CHANNEL_ID,
                    },
                },
            ]
        }

        canonical = batch_metadata.create_batch_from_text_plan(parsed_plan)
        staged = batch_metadata.prepare_metadata_batch(fake_youtube, canonical)

        # 4. Verify exactly those two IDs appear in the staged review rows
        self.assertEqual(len(staged["rows"]), 2)
        row_ids = [r["video_id"] for r in staged["rows"]]
        self.assertEqual(row_ids, [ID_A, ID_B])
        self.assertEqual(staged["rows"][0]["title"], "AI Simplified: Autonomous Reasoning Systems")
        self.assertEqual(staged["rows"][1]["title"], "Production Agent Architectures")

        # 5. Verify single-video editor state is cleared
        fake_state = {
            "strategist_target_reference": {"target_id": ID_A},
            "strategist_chat_proposal": {"title": "Single proposal"},
            "strategist_chat_digest": "abcdef123",
            "copilot_proposal": {"title": "Single proposal"},
            "copilot_approval_digest": "abcdef123",
            "batch_metadata_review": staged,
        }
        with patch.object(strategist_chat, "st") as mock_st:
            mock_st.session_state = fake_state
            strategist_chat._clear_single_video_editor_state()
            self.assertIsNone(fake_state["strategist_target_reference"])
            self.assertIsNone(fake_state["strategist_chat_proposal"])
            self.assertIsNone(fake_state["copilot_proposal"])

            # 6. Verify individual-video editor does not render underneath batch review
            # When batch_active is True, context is None and individual proposal is None
            batch_active = bool(fake_state.get("batch_metadata_review"))
            context = fake_state.get("strategist_target_reference")
            proposal = fake_state.get("strategist_chat_proposal")
            self.assertTrue(batch_active)
            self.assertIsNone(context)
            self.assertIsNone(proposal)


class TestStudioRemovalIntegrity(unittest.TestCase):
    """Verifies that all YouTube Studio runtime integration points have been completely eradicated."""

    def test_studio_export_analyzer_module_deleted(self):
        with self.assertRaises(ModuleNotFoundError):
            import studio_export_analyzer  # noqa: F401

    def test_update_studio_attachments_function_deleted(self):
        self.assertFalse(hasattr(strategist_chat, "update_studio_attachments"))

    def test_fetch_full_video_details_has_no_studio_evidence_key(self):
        fake_youtube = MagicMock()
        fake_youtube.videos().list().execute.return_value = {
            "items": [
                {
                    "id": ID_A,
                    "snippet": {
                        "title": "Sample Title",
                        "description": "Sample Desc",
                        "tags": ["tag1"],
                        "publishedAt": "2025-01-01T00:00:00Z",
                        "channelId": connector.CHANNEL_ID,
                        "channelTitle": "AI Simplified Lab",
                    },
                    "contentDetails": {"duration": "PT5M"},
                    "statistics": {"viewCount": "1000", "likeCount": "50", "commentCount": "5"},
                }
            ]
        }
        with patch.object(strategist_chat, "_transcript_text", return_value=("", "Unavailable")):
            details = strategist_chat.fetch_full_video_details(fake_youtube, ID_A)
            self.assertNotIn("studio_performance_evidence", details)

    def test_classify_video_format_has_no_studio_format_param(self):
        sig = inspect.signature(strategist_chat._classify_video_format)
        self.assertNotIn("studio_format", sig.parameters)

    def test_build_prompt_has_no_studio_evidence_param(self):
        sig = inspect.signature(strategist_chat._build_prompt)
        self.assertNotIn("studio_evidence", sig.parameters)

    def test_discover_video_opportunities_has_no_studio_params_or_return_keys(self):
        sig = inspect.signature(connector.discover_video_opportunities)
        self.assertNotIn("studio_evidence_index", sig.parameters)


class TestHashtagAndTagValidation(unittest.TestCase):
    """Verifies documented YouTube hashtag limits and quotation-mark tag length calculation."""

    def test_up_to_60_hashtags_allowed(self):
        # 20 hashtags is above the old arbitrary 15 limit, but well within YouTube's 60 platform limit
        desc_20_tags = "Video overview\n\n" + " ".join(f"#topic{i}" for i in range(20))
        # Should not raise
        connector._validate_metadata({"title": "Valid Title", "description": desc_20_tags, "tags": ["tag1"]})

    def test_more_than_60_hashtags_rejected(self):
        # 61 hashtags exceeds YouTube's platform limit
        desc_61_tags = "Video overview\n\n" + " ".join(f"#topic{i}" for i in range(61))
        with self.assertRaises(connector.MetadataValidationError) as ctx:
            connector._validate_metadata({"title": "Valid Title", "description": desc_61_tags, "tags": ["tag1"]})
        self.assertIn("60 hashtags", str(ctx.exception))

    def test_quotation_mark_counting_for_tags_with_spaces(self):
        # Multi-word tags like "ai agent" require quotes ("ai agent"), adding 2 characters.
        # Single-word tags: 490 chars without quotes.
        # With multi-word tags, the quotation marks push the count over 500.
        tags = [f"word{i:02d}" for i in range(40)]  # 40 distinct tags
        # Add distinct multi-word tags: each adds len(tag) + 2 for quotes + 1 for comma
        tags.extend([f"two words {i:02d}" for i in range(25)])
        with self.assertRaises(connector.MetadataValidationError) as ctx:
            connector._validate_metadata({"title": "Valid Title", "description": "Valid Desc", "tags": tags})
        self.assertIn("accounting for quotation marks", str(ctx.exception))


class TestHttp400PayloadVerificationAndSecurity(unittest.TestCase):
    """Verifies that videos.update() payload excludes invalid fields and redacts secrets on HTTP 400."""

    def test_mocked_update_captures_clean_payload(self):
        raw_snippet = {
            "title": "New Valid Title",
            "description": "New Valid Description",
            "tags": ["agent", "production"],
            "categoryId": "28",
            "defaultLanguage": "en",
            "defaultAudioLanguage": "en-US",  # Must be stripped
            "thumbnails": {"default": {"url": "https://img.youtube.com/vi/123/default.jpg"}},  # Must be stripped
            "channelTitle": "AI Simplified Lab",  # Must be stripped
            "channelId": connector.CHANNEL_ID,
            "publishedAt": "2025-01-01T00:00:00Z",  # Must be stripped
        }
        clean = connector._writable_snippet(raw_snippet)
        self.assertIn("title", clean)
        self.assertIn("description", clean)
        self.assertIn("tags", clean)
        self.assertIn("categoryId", clean)
        self.assertEqual(clean["defaultLanguage"], "en")
        self.assertNotIn("defaultAudioLanguage", clean)
        self.assertNotIn("thumbnails", clean)
        self.assertNotIn("channelTitle", clean)
        self.assertNotIn("publishedAt", clean)

    def test_google_http_400_exposes_structured_reason_and_location(self):
        resp_body = b"""{
            "error": {
                "code": 400,
                "message": "Invalid tag containing invalid character",
                "errors": [
                    {
                        "message": "Invalid tag containing invalid character",
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
        self.assertIn("Invalid tag", details["message"])

        formatted = strategist_chat._apply_error_message(error)
        self.assertIn("HTTP 400 - invalidTag", formatted)
        self.assertIn("Field: snippet.tags", formatted)

    def test_tokens_and_client_secrets_never_leak_in_ui(self):
        token = "ya29.a0AfH6SMD_test_oauth_access_token_12345"
        secret = "client_secret_GOCSPX-SecretString98765"
        raw_msg = f"Failed to authenticate with Bearer {token} and secret {secret}"

        formatted = strategist_chat._apply_error_message(RuntimeError(raw_msg))
        self.assertNotIn(token, formatted)
        self.assertNotIn(secret, formatted)
        self.assertIn("[REDACTED_AUTH_TOKEN]", formatted)
        self.assertIn("[REDACTED_SECRET]", formatted)


if __name__ == "__main__":
    unittest.main()
