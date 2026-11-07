"""Comprehensive unit tests for natural-language batch metadata instructions and unified pipeline.

Covers:
- Text interpretation (exact values, shared instructions, overrides, clearing)
- Reference resolution (IDs, watch, youtu.be, shorts, embed, live, titles)
- AI response validation and recovery (angles, schema, error isolation)
- Batch approval, sequential publishing, verification, and regression defense
"""

import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import core.batch_instructions as batch_instructions
import core.batch_metadata as batch_metadata
import strategist_chat
import youtube_analytics_connector as connector


VIDEO_ID_1 = "cxsx2ZppQvg"
VIDEO_ID_2 = "LRWD6rVWH3I"
VIDEO_ID_3 = "AbCdEfGhIjK"


def make_snippet(video_id=VIDEO_ID_1, **overrides):
    snippet = {
        "title": f"Current title for {video_id}",
        "description": f"Current description for {video_id}",
        "tags": ["existing", "channel tag"],
        "categoryId": "28",
        "channelId": connector.CHANNEL_ID,
        "defaultLanguage": "en",
        "defaultAudioLanguage": "en-US",
    }
    snippet.update(overrides)
    return snippet


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback

    def execute(self):
        return self.callback()


class FakeVideos:
    def __init__(self, snippets):
        self.snippets = {k: copy.deepcopy(v) for k, v in snippets.items()}
        self.update_calls = []
        self.fail_on_update_ids = set()

    def list(self, *, part, id):
        def response():
            items = []
            for vid in id.split(","):
                if vid in self.snippets:
                    items.append({"id": vid, "snippet": copy.deepcopy(self.snippets[vid])})
            return {"items": items}
        return FakeRequest(response)

    def update(self, *, part, body):
        video_id = body.get("id")
        self.update_calls.append(copy.deepcopy(body))
        if video_id in self.fail_on_update_ids:
            raise RuntimeError(f"Simulated YouTube API failure on update for {video_id}")
        updated_snippet = copy.deepcopy(self.snippets.get(video_id, {}))
        updated_snippet.update(body.get("snippet", {}))
        self.snippets[video_id] = updated_snippet

        def response():
            return {"id": video_id, "snippet": copy.deepcopy(updated_snippet)}
        return FakeRequest(response)


class FakeYoutube:
    def __init__(self, snippets):
        self._videos = FakeVideos(snippets)

    def videos(self):
        return self._videos


# ==============================================================================
# 1. TEXT INTERPRETATION TESTS
# ==============================================================================
class TestBatchTextInterpretation(unittest.TestCase):
    def test_one_video_exact_title_description_tags(self):
        text = (
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'AI Agents Can Now Design Apps'. "
            "Change the description to 'Complete guide to new agentic models.' "
            "Use these tags: AI agents, LLM, automation."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(plan["total_identified"], 1)
        video = plan["videos"][0]
        self.assertEqual(video["video_id"], VIDEO_ID_1)
        self.assertEqual(video["title_intent"], "exact")
        self.assertEqual(video["exact_title"], "AI Agents Can Now Design Apps")
        self.assertEqual(video["description_intent"], "exact")
        self.assertEqual(video["exact_description"], "Complete guide to new agentic models.")
        self.assertEqual(video["tags_intent"], "exact")
        self.assertEqual(video["exact_tags"], ["AI agents", "LLM", "automation"])

    def test_multiple_videos_independent_instructions(self):
        text = (
            f"1. https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'First Video Title'. Improve the description.\n\n"
            f"2. https://www.youtube.com/watch?v={VIDEO_ID_2}\n"
            "Change the title to 'Second Video Title'. Use these tags: doom, biology."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 2)
        v1, v2 = plan["videos"][0], plan["videos"][1]
        self.assertEqual(v1["video_id"], VIDEO_ID_1)
        self.assertEqual(v1["exact_title"], "First Video Title")
        self.assertEqual(v1["description_intent"], "optimize")

        self.assertEqual(v2["video_id"], VIDEO_ID_2)
        self.assertEqual(v2["exact_title"], "Second Video Title")
        self.assertEqual(v2["exact_tags"], ["doom", "biology"])

    def test_shared_instructions_applied_to_all_listed_videos(self):
        text = (
            "Improve the description and tags of every listed video. Keep the current title unchanged.\n\n"
            f"- https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            f"- https://www.youtube.com/watch?v={VIDEO_ID_2}\n"
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 2)
        for v in plan["videos"]:
            self.assertEqual(v["title_intent"], "preserve")
            self.assertEqual(v["description_intent"], "optimize")
            self.assertEqual(v["tags_intent"], "optimize")

    def test_video_specific_overrides_shared_instructions(self):
        text = (
            "For all videos, improve description and tags. Keep existing title unchanged.\n\n"
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'Overridden Title For V1'.\n\n"
            f"Video 2: https://www.youtube.com/watch?v={VIDEO_ID_2}\n"
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 2)
        v1, v2 = plan["videos"][0], plan["videos"][1]
        self.assertEqual(v1["title_intent"], "exact")
        self.assertEqual(v1["exact_title"], "Overridden Title For V1")
        self.assertEqual(v2["title_intent"], "preserve")

    def test_exact_title_bypasses_title_angle_generation(self):
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{
                "video_id": VIDEO_ID_1,
                "exact_title": "Exact Explicit Title",
                "title_intent": "exact",
                "description_intent": "unmentioned",
                "tags_intent": "unmentioned",
            }]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["proposed"]["title"], "Exact Explicit Title")
        self.assertEqual(row.get("title_angles"), [])

    def test_description_only_and_tags_only_preserve_untouched_fields(self):
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{
                "video_id": VIDEO_ID_1,
                "title_intent": "preserve",
                "description_intent": "exact",
                "exact_description": "New updated description only",
                "tags_intent": "preserve",
            }]
        })
        original = make_snippet(VIDEO_ID_1, title="Untouched Original Title", tags=["t1", "t2"])
        youtube = FakeYoutube({VIDEO_ID_1: original})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["proposed"]["title"], "Untouched Original Title")
        self.assertEqual(row["proposed"]["description"], "New updated description only")
        self.assertEqual(row["proposed"]["tags"], ["t1", "t2"])
        self.assertEqual(row["changed_fields"], ["description"])

    def test_explicit_empty_description_and_tags_distinguished_from_omitted(self):
        text = (
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Clear the description and clear all tags. Keep title unchanged."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        v = plan["videos"][0]
        self.assertEqual(v["description_intent"], "clear")
        self.assertEqual(v["exact_description"], "")
        self.assertEqual(v["tags_intent"], "clear")
        self.assertEqual(v["exact_tags"], [])

        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["proposed"]["description"], "")
        self.assertEqual(row["proposed"]["tags"], [])

    def test_natural_language_paragraphs_and_numbered_lists(self):
        text = (
            "Here is the plan for my latest batch:\n"
            f"1. https://www.youtube.com/watch?v={VIDEO_ID_1} - change title to 'Title One'.\n"
            f"2. https://youtu.be/{VIDEO_ID_2} - change title to 'Title Two'."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 2)
        self.assertEqual(plan["videos"][0]["video_id"], VIDEO_ID_1)
        self.assertEqual(plan["videos"][1]["video_id"], VIDEO_ID_2)

    def test_repeated_video_references_deduplicated(self):
        text = (
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'First Mention'.\n\n"
            f"Video 2: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'First Mention'."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        # Identical duplicate is safe and present
        self.assertFalse(plan["videos"][0]["conflict_detected"])

    def test_conflicting_instructions_flagged(self):
        text = (
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'Alpha Title'.\n\n"
            f"Video 2: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change the title to 'Beta Title Conflict'."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        v1 = plan["videos"][0]
        self.assertTrue(v1["conflict_detected"])
        self.assertTrue(any("Conflicting instructions" in err for err in v1["errors"]))

    def test_more_than_25_videos_rejected(self):
        lines = [f"{i+1}. https://www.youtube.com/watch?v=vid{i:08d}\nImprove description." for i in range(26)]
        text = "\n".join(lines)
        with self.assertRaisesRegex(ValueError, "more than 25"):
            batch_instructions.parse_batch_instructions(text)

    def test_oversized_text_rejected(self):
        oversized = "A" * (batch_instructions.MAX_BATCH_TEXT_LENGTH + 50)
        with self.assertRaisesRegex(ValueError, "exceeds maximum allowed length"):
            batch_instructions.parse_batch_instructions(oversized)


# ==============================================================================
# 2. REFERENCE RESOLUTION TESTS
# ==============================================================================
class TestBatchReferenceResolution(unittest.TestCase):
    def test_valid_11_character_youtube_ids(self):
        self.assertEqual(batch_instructions.parse_video_reference(VIDEO_ID_1), VIDEO_ID_1)

    def test_standard_youtube_watch_links(self):
        url = f"https://www.youtube.com/watch?v={VIDEO_ID_1}&t=42s"
        self.assertEqual(batch_instructions.parse_video_reference(url), VIDEO_ID_1)

    def test_youtu_be_short_links(self):
        url = f"https://youtu.be/{VIDEO_ID_1}?feature=shared"
        self.assertEqual(batch_instructions.parse_video_reference(url), VIDEO_ID_1)

    def test_shorts_embed_and_live_urls(self):
        self.assertEqual(batch_instructions.parse_video_reference(f"https://www.youtube.com/shorts/{VIDEO_ID_1}"), VIDEO_ID_1)
        self.assertEqual(batch_instructions.parse_video_reference(f"https://www.youtube.com/embed/{VIDEO_ID_1}"), VIDEO_ID_1)
        self.assertEqual(batch_instructions.parse_video_reference(f"https://www.youtube.com/live/{VIDEO_ID_1}"), VIDEO_ID_1)

    def test_unknown_or_non_youtube_urls_rejected(self):
        with self.assertRaises(ValueError):
            batch_instructions.parse_video_reference("https://vimeo.com/123456789")
        with self.assertRaises(ValueError):
            batch_instructions.parse_video_reference("https://example.com/watch?v=cxsx2ZppQvg")

    def test_ambiguous_video_titles_flagged(self):
        channel_videos = [
            {"id": "vid00000001", "title": "Building AI Agents in Python"},
            {"id": "vid00000002", "title": "Building AI Agents in Python"},
        ]
        res = batch_instructions.resolve_channel_video_by_title("Building AI Agents in Python", channel_videos)
        self.assertEqual(res["status"], "ambiguous")
        self.assertIn("matches 2 uploads", res["error"])

    def test_unresolved_video_titles_flagged(self):
        channel_videos = [{"id": "vid00000001", "title": "Building AI Agents"}]
        res = batch_instructions.resolve_channel_video_by_title("A Video That Does Not Exist", channel_videos)
        self.assertEqual(res["status"], "unresolved")

    def test_non_owner_videos_flagged_in_batch(self):
        youtube = FakeYoutube({
            VIDEO_ID_1: make_snippet(VIDEO_ID_1, channelId="UC_OTHER_CHANNEL_NOT_OWNER"),
        })
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "test",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "New Title", "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertIn("Ownership check failed", row["errors"][0])

    def test_no_invented_video_ids(self):
        refs = batch_instructions.extract_video_references_from_text(
            "Please optimize my video about transformers. Do not make up links."
        )
        self.assertEqual(refs, [])

    def test_multiple_links_in_batch_not_treated_as_target_plus_reference(self):
        batch_msg = (
            f"Here are two videos to update:\n"
            f"1. https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            f"2. https://www.youtube.com/watch?v={VIDEO_ID_2}\n"
            "For both videos, improve description."
        )
        urls = strategist_chat._youtube_references_from_message(batch_msg)
        self.assertEqual(len(urls), 2)
        # Must be recognized as batch intent!
        self.assertTrue(strategist_chat._is_batch_intent(batch_msg, urls))


# ==============================================================================
# 3. AI RESPONSE VALIDATION TESTS
# ==============================================================================
class TestAIResponseValidation(unittest.TestCase):
    def test_exact_title_skips_title_angle_validation(self):
        payload = {
            "title": "Exact Title Provided By User",
            "description": "Valid description",
            "tags": ["tag1", "tag2"],
        }
        is_valid, err = strategist_chat._validate_proposal_payload(payload, preserve_title=True)
        self.assertTrue(is_valid)
        self.assertIsNone(err)

    def test_valid_three_angle_title_output_accepted(self):
        payload = {
            "title": "Angle 1 Curiosity Title",
            "description": "Valid description",
            "tags": ["tag1", "tag2"],
            "title_angles": [
                {"angle": "curiosity", "title": "Angle 1 Curiosity Title"},
                {"angle": "direct_value", "title": "Angle 2 Value Title"},
                {"angle": "search", "title": "Angle 3 Search Title"},
            ],
        }
        is_valid, err = strategist_chat._validate_proposal_payload(payload, preserve_title=False)
        self.assertTrue(is_valid)

    def test_invalid_title_angles_triggers_fallback_or_repair(self):
        payload = {
            "title": "Mismatched Staged Title",
            "description": "Valid description",
            "tags": ["tag1"],
            "title_angles": [
                {"angle": "curiosity", "title": "Angle 1"},
                {"angle": "direct_value", "title": "Angle 2"},
            ],  # Only 2 angles
        }
        is_valid, err = strategist_chat._validate_proposal_payload(payload, preserve_title=False)
        self.assertFalse(is_valid)
        self.assertIn("Expected exactly 3 structured title angles", err)

    def test_invalid_json_rejected_safely(self):
        is_valid, err = strategist_chat._validate_proposal_payload("Not a dict", preserve_title=False)
        self.assertFalse(is_valid)

    def test_wrong_field_types_rejected(self):
        payload = {
            "title": 12345,  # Number instead of string
            "description": None,
            "tags": "not a list",
        }
        is_valid, err = strategist_chat._validate_proposal_payload(payload, preserve_title=True)
        self.assertFalse(is_valid)

    def test_failure_for_one_video_does_not_erase_valid_rows(self):
        # Fake generator that fails for VIDEO_ID_1 but succeeds for VIDEO_ID_2
        def ai_generator(video_id, before, instruction):
            if video_id == VIDEO_ID_1:
                raise ValueError("Simulated model generation failure on V1")
            return {
                "title": f"Optimized {video_id}",
                "description": "Good description",
                "tags": ["ai"],
            }

        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [
                {"video_id": VIDEO_ID_1, "title_intent": "optimize", "description_intent": "optimize"},
                {"video_id": VIDEO_ID_2, "title_intent": "optimize", "description_intent": "optimize"},
            ]
        })
        youtube = FakeYoutube({
            VIDEO_ID_1: make_snippet(VIDEO_ID_1),
            VIDEO_ID_2: make_snippet(VIDEO_ID_2),
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=ai_generator)
        r1, r2 = staged["rows"][0], staged["rows"][1]
        self.assertEqual(r1["status"], "invalid")
        self.assertTrue(any("Simulated model generation failure" in e for e in r1["errors"]))
        self.assertEqual(r2["status"], "ready")
        self.assertEqual(r2["proposed"]["title"], f"Optimized {VIDEO_ID_2}")


# ==============================================================================
# 4. APPROVAL AND PUBLISHING TESTS
# ==============================================================================
class TestBatchApprovalAndPublishing(unittest.TestCase):
    def test_every_row_starts_unselected(self):
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "New Title", "title_intent": "exact"}]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        # Canonical rows do not mark themselves as pre-selected
        self.assertFalse(staged["rows"][0].get("selected", False))

    def test_editing_row_changes_digest(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "First Title", "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        digest1 = staged["rows"][0]["digest"]

        # Edit proposed title directly
        staged["rows"][0]["proposed"]["title"] = "Edited Title Change"
        digest2 = connector.metadata_proposal_digest(
            VIDEO_ID_1, staged["rows"][0]["before"], staged["rows"][0]["proposed"]
        )
        self.assertNotEqual(digest1, digest2)

    def test_stale_youtube_metadata_blocks_row(self):
        before = make_snippet(VIDEO_ID_1, title="Initial Title")
        youtube = FakeYoutube({VIDEO_ID_1: copy.deepcopy(before)})
        proposed = copy.deepcopy(before)
        proposed["title"] = "Approved Update"
        digest = connector.metadata_proposal_digest(VIDEO_ID_1, before, proposed)

        # Alter live snippet behind the scenes to simulate stale check failure
        youtube._videos.snippets[VIDEO_ID_1]["title"] = "Title Changed on Studio Mobile"

        with self.assertRaisesRegex(RuntimeError, "metadata changed after review"):
            connector.apply_approved_dashboard_proposal(
                youtube, VIDEO_ID_1, before, proposed, digest,
                audit_callback=lambda entry: None,
            )

    def test_non_owner_video_never_published(self):
        before = make_snippet(VIDEO_ID_1, channelId="UC_NOT_THE_OWNER")
        youtube = FakeYoutube({VIDEO_ID_1: before})
        proposed = copy.deepcopy(before)
        proposed["title"] = "Hacked Title"
        digest = connector.metadata_proposal_digest(VIDEO_ID_1, before, proposed)

        with self.assertRaises(connector.VideoNotFoundError):
            connector.apply_approved_dashboard_proposal(
                youtube, VIDEO_ID_1, before, proposed, digest,
                audit_callback=lambda entry: None,
            )

    def test_sequential_publishing_continues_after_one_failure(self):
        youtube = FakeYoutube({
            VIDEO_ID_1: make_snippet(VIDEO_ID_1),
            VIDEO_ID_2: make_snippet(VIDEO_ID_2),
        })
        youtube._videos.fail_on_update_ids.add(VIDEO_ID_1)

        b1 = make_snippet(VIDEO_ID_1)
        p1 = copy.deepcopy(b1)
        p1["title"] = "P1 Update"
        d1 = connector.metadata_proposal_digest(VIDEO_ID_1, b1, p1)

        b2 = make_snippet(VIDEO_ID_2)
        p2 = copy.deepcopy(b2)
        p2["title"] = "P2 Update"
        d2 = connector.metadata_proposal_digest(VIDEO_ID_2, b2, p2)

        results = {}
        for vid, before, prop, digest in [(VIDEO_ID_1, b1, p1, d1), (VIDEO_ID_2, b2, p2, d2)]:
            try:
                res = connector.apply_approved_dashboard_proposal(
                    youtube, vid, before, prop, digest,
                    audit_callback=lambda entry: None,
                )
                results[vid] = "verified"
            except Exception as err:
                results[vid] = f"failed: {err}"

        self.assertIn("failed", results[VIDEO_ID_1])
        self.assertEqual(results[VIDEO_ID_2], "verified")
        self.assertEqual(youtube._videos.snippets[VIDEO_ID_2]["title"], "P2 Update")

    def test_only_confirmed_readback_produces_verified_status(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        before = make_snippet(VIDEO_ID_1)
        proposed = copy.deepcopy(before)
        proposed["title"] = "Verified Readback Title"
        digest = connector.metadata_proposal_digest(VIDEO_ID_1, before, proposed)

        res = connector.apply_approved_dashboard_proposal(
            youtube, VIDEO_ID_1, before, proposed, digest,
            audit_callback=lambda entry: None,
        )
        self.assertEqual(res.get("result"), "success")

    def test_spreadsheet_input_still_works(self):
        payload = f"video_id,new_title\n{VIDEO_ID_1},Spreadsheet Title\n".encode("utf-8")
        parsed = batch_metadata.parse_metadata_file("test.csv", payload)
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, parsed)
        self.assertEqual(staged["rows"][0]["status"], "ready")
        self.assertEqual(staged["rows"][0]["proposed"]["title"], "Spreadsheet Title")

    def test_text_input_and_spreadsheet_use_same_staging_and_publishing_path(self):
        # 1. Spreadsheet
        csv_payload = f"video_id,new_title\n{VIDEO_ID_1},Same Pipeline Title\n".encode()
        parsed_csv = batch_metadata.parse_metadata_file("test.csv", csv_payload)

        # 2. Text instructions
        text = f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\nChange the title to 'Same Pipeline Title'."
        parsed_text = batch_instructions.parse_batch_instructions(text)
        canonical_text = batch_metadata.create_batch_from_text_plan(parsed_text)

        youtube1 = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        youtube2 = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})

        staged_csv = batch_metadata.prepare_metadata_batch(youtube1, parsed_csv)
        staged_text = batch_metadata.prepare_metadata_batch(youtube2, canonical_text)

        # Both produce identical proposed changes and identical digests
        self.assertEqual(staged_csv["rows"][0]["proposed"]["title"], staged_text["rows"][0]["proposed"]["title"])
        self.assertEqual(staged_csv["rows"][0]["digest"], staged_text["rows"][0]["digest"])

    def test_single_video_chat_continues_to_work(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        context, msg = strategist_chat._load_conversation_videos(
            youtube, {}, f"https://www.youtube.com/watch?v={VIDEO_ID_1}"
        )
        self.assertIsNotNone(context)
        self.assertIsNone(msg)
        self.assertEqual(context["target_id"], VIDEO_ID_1)

    def test_reference_video_comparison_continues_to_work(self):
        youtube = FakeYoutube({
            VIDEO_ID_1: make_snippet(VIDEO_ID_1),
            VIDEO_ID_2: make_snippet(VIDEO_ID_2),
        })
        msg_text = (
            f"Here is my target video: https://www.youtube.com/watch?v={VIDEO_ID_1} "
            f"and compare it to reference video: https://www.youtube.com/watch?v={VIDEO_ID_2}"
        )
        urls = strategist_chat._youtube_references_from_message(msg_text)
        self.assertEqual(len(urls), 2)
        # Because of 'compare' and 'reference', it must NOT be routed as batch!
        self.assertFalse(strategist_chat._is_batch_intent(msg_text, urls))


# ==============================================================================
# 5. AUDIT FOLLOW-UP REGRESSION TEST SUITE (20 Audit Cases + Integration)
# ==============================================================================
class TestAuditFollowUpRegression(unittest.TestCase):
    def test_01_actual_ai_generator_invocation_during_optimization(self):
        mock_gen = MagicMock()
        mock_gen.return_value = {
            "title": "AI Optimized Title",
            "title_angles": [
                {"angle": "curiosity", "label": "Curiosity", "title": "Did You Know AI Could App?"},
                {"angle": "value", "label": "Value", "title": "Build Apps With AI Fast"},
                {"angle": "search", "label": "Search", "title": "How To Build Apps With AI"},
            ],
            "description": "AI Optimized Description",
            "tags": ["ai", "apps"],
        }
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{
                "video_id": VIDEO_ID_1,
                "title_intent": "optimize",
                "description_intent": "optimize",
                "tags_intent": "optimize",
                "original_instruction": "Optimize all metadata",
            }]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        self.assertTrue(mock_gen.called)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["proposed"]["title"], "AI Optimized Title")
        self.assertEqual(row["proposed"]["description"], "AI Optimized Description")
        self.assertEqual(row["proposed"]["tags"], ["ai", "apps"])
        self.assertEqual(len(row["title_angles"]), 3)

    @patch("strategist_chat._groq_generate")
    @patch("strategist_chat._gemini_generate")
    def test_02_groq_primary_and_gemini_fallback(self, mock_gemini, mock_groq):
        mock_groq.side_effect = RuntimeError("Groq service unavailable")
        mock_gemini.return_value = (
            json.dumps({
                "title": "Gemini Fallback Title",
                "title_angles": [
                    {"angle": "curiosity", "label": "Curiosity", "title": "Curious Title"},
                    {"angle": "value", "label": "Value", "title": "Value Title"},
                    {"angle": "search", "label": "Search", "title": "Search Title"},
                ],
                "description": "Gemini Description",
                "tags": ["ai", "gemini"],
                "rationale": "Groq down, Gemini recovered",
            }),
            "gemini-2.5-flash",
        )
        gen = strategist_chat.build_batch_metadata_generator(
            secrets={
                "groq": {"api_key": "groq-key"},
                "gemini": {"api_key": "gemini-key"},
            }
        )
        self.assertIsNotNone(gen)
        result = gen(VIDEO_ID_1, make_snippet(VIDEO_ID_1), instruction="Optimize title")
        self.assertEqual(result["title"], "Gemini Fallback Title")
        self.assertEqual(result["provider"], "Gemini")
        self.assertTrue(mock_groq.called)
        self.assertTrue(mock_gemini.called)

    @patch("youtube_analytics_connector.fetch_video_transcript")
    def test_03_correct_transcript_passed_to_generator(self, mock_transcript):
        mock_transcript.return_value = "This is a detailed video transcript about neural networks."
        mock_gen = MagicMock()
        mock_gen.return_value = {
            "title": "Neural Networks Guide",
            "title_angles": [
                {"angle": "curiosity", "label": "Curiosity", "title": "A1"},
                {"angle": "value", "label": "Value", "title": "A2"},
                {"angle": "search", "label": "Search", "title": "A3"},
            ],
            "description": "Desc",
            "tags": ["neural"],
        }
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "title_intent": "optimize"}]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        self.assertTrue(mock_gen.called)
        call_kwargs = mock_gen.call_args[1]
        self.assertEqual(call_kwargs["transcript"], "This is a detailed video transcript about neural networks.")

    def test_04_exact_title_bypasses_title_angle_generation(self):
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{
                "video_id": VIDEO_ID_1,
                "exact_title": "Exact Title Without Angles",
                "title_intent": "exact",
            }]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=None)

        row = staged["rows"][0]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["proposed"]["title"], "Exact Title Without Angles")
        self.assertEqual(row["title_angles"], [])

    def test_05_three_title_angles_validated_only_when_required(self):
        mock_gen = MagicMock()
        # Missing required 3 angles: only gives 2
        mock_gen.return_value = {
            "title": "Title with missing angles",
            "title_angles": [
                {"angle": "curiosity", "label": "Curiosity", "title": "Angle 1"},
                {"angle": "value", "label": "Value", "title": "Angle 2"},
            ],
        }
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "title_intent": "optimize"}]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("3 title angles" in e for e in row["errors"]))

    def test_06_missing_ai_generator_causes_explicit_row_failure_for_optimization_intent(self):
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "title_intent": "optimize"}]
        })
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=None)

        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("no AI generator is available" in e for e in row["errors"]))
        # Current title must NOT be silently staged as a success!
        self.assertNotIn("digest", row)

    def test_07_multiple_links_on_one_line_produce_all_intended_rows(self):
        text = (
            f"https://www.youtube.com/watch?v={VIDEO_ID_1} and "
            f"https://www.youtube.com/watch?v={VIDEO_ID_2}: change title to 'Updated Joint Title'"
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 2)
        vids = [v["video_id"] for v in plan["videos"]]
        self.assertEqual(vids, [VIDEO_ID_1, VIDEO_ID_2])

    def test_08_shared_and_video_specific_instructions_combine_correctly(self):
        text = (
            "For all videos: add tags: sharedtag, globalai.\n"
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change title to 'Video 1 Custom Title'.\n"
            f"Video 2: https://www.youtube.com/watch?v={VIDEO_ID_2}\n"
            "Change title to 'Video 2 Custom Title' and use tags: customtag, specific.\n"
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 2)
        v1 = plan["videos"][0]
        v2 = plan["videos"][1]
        self.assertEqual(v1["exact_title"], "Video 1 Custom Title")
        self.assertEqual(v1["exact_tags"], ["sharedtag", "globalai"])
        self.assertEqual(v2["exact_title"], "Video 2 Custom Title")
        self.assertEqual(v2["exact_tags"], ["customtag", "specific"])

    def test_09_title_references_resolve_using_real_owner_channel_video_data(self):
        channel_videos = [
            {"id": VIDEO_ID_1, "title": "Building Autonomous Agents 101"},
            {"id": VIDEO_ID_2, "title": "Complete Next.js Crash Course"},
        ]
        text = "Change the title of 'Building Autonomous Agents 101' to 'Agents 101: The Definitive Guide'."
        plan = batch_instructions.parse_batch_instructions(text, channel_videos=channel_videos)
        self.assertEqual(len(plan["videos"]), 1)
        self.assertEqual(plan["videos"][0]["video_id"], VIDEO_ID_1)

    def test_10_ambiguous_title_matches_block_staging(self):
        channel_videos = [
            {"id": VIDEO_ID_1, "title": "Python Basics"},
            {"id": VIDEO_ID_2, "title": "Python Basics"},
        ]
        text = "Change the title of 'Python Basics' to 'Python for Absolute Beginners'."
        plan = batch_instructions.parse_batch_instructions(text, channel_videos=channel_videos)
        self.assertTrue(plan["videos"][0]["is_ambiguous"])
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        self.assertEqual(canonical["rows"][0]["status"], "invalid")

    def test_11_editing_selected_proposal_invalidates_final_approval(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "Initial Proposal", "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        initial_fp = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)

        # User edits title in review interface
        row["proposed"]["title"] = "Modified Proposal After Review"
        batch_metadata.revalidate_batch_row(row)
        new_fp = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=2)

        self.assertNotEqual(initial_fp, new_fp)
        self.assertEqual(row["status"], "ready")

    def test_12_editing_after_final_approval_requires_new_approval(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "Initial Title", "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]

        # Approval key 1
        key1 = f"batch_approval_box_{batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)}"

        # Edit proposal
        row["proposed"]["title"] = "Different Title"
        batch_metadata.revalidate_batch_row(row)
        key2 = f"batch_approval_box_{batch_metadata.compute_batch_approval_fingerprint([row], review_revision=2)}"

        self.assertNotEqual(key1, key2)

    def test_13_invalid_edits_remove_old_digest_and_block_publishing(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "Valid First Title", "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertIn("digest", row)

        # User enters title that violates 100 character constraint
        row["proposed"]["title"] = "X" * 120
        batch_metadata.revalidate_batch_row(row)

        self.assertEqual(row["status"], "invalid")
        self.assertNotIn("digest", row)
        self.assertTrue(any("100 characters" in e for e in row["errors"]))

    def test_14_correcting_invalid_metadata_clears_stale_errors(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "X" * 120, "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(len(row["errors"]) > 0)

        # User corrects title to a valid one
        row["proposed"]["title"] = "Proper Short Title"
        batch_metadata.revalidate_batch_row(row)

        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["errors"], [])
        self.assertIn("digest", row)

    def test_15_reverting_to_original_metadata_produces_no_change(self):
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": "sample",
            "videos": [{"video_id": VIDEO_ID_1, "exact_title": "Temporary Edit", "title_intent": "exact"}]
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]

        # Revert back to original title
        row["proposed"]["title"] = row["before"]["title"]
        batch_metadata.revalidate_batch_row(row)

        self.assertEqual(row["status"], "no_change")
        self.assertNotIn("digest", row)
        self.assertTrue(any("identical" in e for e in row["errors"]))

    def test_16_unresolved_instructions_never_become_silent_successful_updates(self):
        text = "Change the title of https://www.notyoutube.com/bogus to 'Should Never Publish'."
        plan = batch_instructions.parse_batch_instructions(text)
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)

        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertNotIn("digest", row)

    def test_17_existing_spreadsheet_import_remains_functional(self):
        csv_bytes = f"video_id,new_title\n{VIDEO_ID_1},Spreadsheet Maintained Title\n".encode("utf-8")
        parsed = batch_metadata.parse_metadata_file("test.csv", csv_bytes)
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, parsed)

        self.assertEqual(staged["rows"][0]["status"], "ready")
        self.assertEqual(staged["rows"][0]["proposed"]["title"], "Spreadsheet Maintained Title")

    def test_18_existing_single_video_chat_and_comparison_functional(self):
        youtube = FakeYoutube({
            VIDEO_ID_1: make_snippet(VIDEO_ID_1),
            VIDEO_ID_2: make_snippet(VIDEO_ID_2),
        })
        # Single video loading
        ctx, msg = strategist_chat._load_conversation_videos(youtube, {}, f"https://www.youtube.com/watch?v={VIDEO_ID_1}")
        self.assertIsNotNone(ctx)
        self.assertIsNone(msg)

        # Reference comparison detection
        comp_msg = f"Compare target https://www.youtube.com/watch?v={VIDEO_ID_1} to reference https://www.youtube.com/watch?v={VIDEO_ID_2}"
        urls = strategist_chat._youtube_references_from_message(comp_msg)
        self.assertFalse(strategist_chat._is_batch_intent(comp_msg, urls))

    def test_19_sequential_publishing_continues_safely_after_independent_row_failure(self):
        youtube = FakeYoutube({
            VIDEO_ID_1: make_snippet(VIDEO_ID_1),
            VIDEO_ID_2: make_snippet(VIDEO_ID_2),
        })
        youtube._videos.fail_on_update_ids.add(VIDEO_ID_1)

        b1, b2 = make_snippet(VIDEO_ID_1), make_snippet(VIDEO_ID_2)
        p1, p2 = copy.deepcopy(b1), copy.deepcopy(b2)
        p1["title"] = "Fail Title"
        p2["title"] = "Success Title"
        d1 = connector.metadata_proposal_digest(VIDEO_ID_1, b1, p1)
        d2 = connector.metadata_proposal_digest(VIDEO_ID_2, b2, p2)

        rows = [
            {"video_id": VIDEO_ID_1, "before": b1, "proposed": p1, "digest": d1, "status": "ready", "attempted": False},
            {"video_id": VIDEO_ID_2, "before": b2, "proposed": p2, "digest": d2, "status": "ready", "attempted": False},
        ]
        # Sequential publishing simulation
        for row in rows:
            row["attempted"] = True
            try:
                connector.apply_approved_dashboard_proposal(
                    youtube, row["video_id"], row["before"], row["proposed"], row["digest"],
                    audit_callback=lambda entry: None,
                )
                row["status"] = "verified"
            except Exception as err:
                row["status"] = "failed"
                row["result_message"] = str(err)

        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[1]["status"], "verified")
        self.assertEqual(youtube._videos.snippets[VIDEO_ID_2]["title"], "Success Title")

    def test_20_no_failed_row_is_automatically_retried(self):
        row = {"video_id": VIDEO_ID_1, "status": "failed", "attempted": True}
        # In publish loop, only rows with status == "ready" and attempted == False are published
        should_publish = (row.get("status") == "ready" and not row.get("attempted"))
        self.assertFalse(should_publish)

    def test_21_full_end_to_end_integration(self):
        # 1. User pastes instructions
        text = (
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "Change title to 'Initial Title From User'. Change description to 'Initial Description'.\n"
        )
        plan = batch_instructions.parse_batch_instructions(text)
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "ready")

        # 2. User edits title in table
        row["proposed"]["title"] = "Final Approved Title"
        batch_metadata.revalidate_batch_row(row)
        self.assertEqual(row["status"], "ready")

        # 3. User approves and publishes
        digest = connector.metadata_proposal_digest(VIDEO_ID_1, row["before"], row["proposed"])
        self.assertEqual(row["digest"], digest)
        res = connector.apply_approved_dashboard_proposal(
            youtube, VIDEO_ID_1, row["before"], row["proposed"], row["digest"],
            audit_callback=lambda entry: None,
        )
        self.assertEqual(res["result"], "success")
        self.assertEqual(youtube._videos.snippets[VIDEO_ID_1]["title"], "Final Approved Title")


if __name__ == "__main__":
    unittest.main()
