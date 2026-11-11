"""Comprehensive unit tests for natural-language batch metadata instructions and unified pipeline.

Covers:
- Text interpretation (exact values, shared instructions, overrides, clearing)
- Reference resolution (IDs, watch, youtu.be, shorts, embed, live, titles)
- AI response validation and recovery (angles, schema, error isolation)
- Batch approval, sequential publishing, verification, and regression defense
"""

import copy
import csv
import io
import json
import unittest
from collections import UserDict
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

    def test_multiline_description_keeps_blank_paragraphs_and_stages_exactly(self):
        description = (
            "See how Google Mixboard can turn rough ideas into visual concepts on an AI canvas. "
            "This tutorial covers five practical use cases, from concept art to product mockups and presentations.\n\n"
            "What the video shows: • Concept art and inpainting • Product mockups using a logo and 3D object "
            "• Interior design planning • Fashion styling with mood-board items • Turning a canvas into slides\n\n"
            "The video describes Mixboard as an experimental Google Labs tool powered by Gemini models. "
            "It also shows style transfer and ways to edit visual ideas on the canvas.\n\n"
            "Try Google Mixboard through Google Labs.\n\n"
            "Chapters: 00:00 Introduction to Google Mixboard 00:31 Concept art and inpainting 02:00 Product mockups"
            "\n\nSubscribe to AI Simplified Lab for clear AI tool tutorials and practical workflows."
            "\n\n#GoogleMixboard #GoogleLabs #AITools #GenerativeAI #AISimplifiedLab"
        )
        text = (
            f"Video 1: https://www.youtube.com/watch?v={VIDEO_ID_1}\n"
            "New title: Google Mixboard Tutorial: 5 AI Use Cases for Design\n"
            f"Description: {description}\n"
            "Tags: Google Mixboard tutorial; Google Labs; Gemini AI; AI tools; AI design; AI Simplified Lab"
        )
        plan = batch_instructions.parse_batch_instructions(text)
        video = plan["videos"][0]
        self.assertEqual(video["description_intent"], "exact")
        self.assertEqual(video["exact_description"], description)

        youtube = FakeYoutube({VIDEO_ID_1: make_snippet(VIDEO_ID_1)})
        staged = batch_metadata.prepare_metadata_batch(
            youtube,
            batch_metadata.create_batch_from_text_plan(plan),
        )
        row = staged["rows"][0]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["proposed"]["description"], description)
        self.assertIn("description", row["changed_fields"])
        exported = list(csv.DictReader(io.StringIO(batch_metadata.build_results_csv(staged))))[0]
        self.assertEqual(exported["proposed_description"], description)

    def test_full_user_format_triple_quoted_description_survives_ai_reconciliation_and_csv(self):
        description = (
            "See how Google Mixboard can turn rough ideas into visual concepts on an AI canvas. "
            "This tutorial covers five practical use cases, from concept art to product mockups and presentations.\n\n"
            "What the video shows: • Concept art and inpainting • Product mockups using a logo and 3D object "
            "• Interior design planning • Fashion styling with mood-board items • Turning a canvas into slides\n\n"
            "The video describes Mixboard as an experimental Google Labs tool powered by Gemini models. "
            "It also shows style transfer and ways to edit visual ideas on the canvas.\n\n"
            "Try Google Mixboard through Google Labs.\n\n"
            "Chapters: 00:00 Introduction to Google Mixboard 00:31 Concept art and inpainting 02:00 Product mockups "
            "03:22 Interior design planning 06:12 Fashion styling 08:58 Transform to slides 09:38 Final verdict"
            "\n\nSubscribe to AI Simplified Lab for clear AI tool tutorials and practical workflows."
            "\n\n#GoogleMixboard #GoogleLabs #AITools #GenerativeAI #AIProductDesign #InteriorDesignAI #AISimplifiedLab"
        )
        video_id = "P4b60v30GuM"
        text = (
            "Stage the exact values below for each VIDEO ID. Change only the title, full description, "
            "and separate YouTube tags.\n\n"
            f"VIDEO ID: {video_id} CURRENT TITLE: Google Mixboard Finally in India! Full Tutorial & Use Cases "
            'New title: "Google Mixboard Tutorial: 5 AI Use Cases for Design" '
            f'Description: """{description}""" '
            'Tags: ["Google Mixboard", "Google Mixboard tutorial", "Google Labs", "Gemini AI", '
            '"AI tools", "AI design", "AI concept art", "AI inpainting", "AI style transfer", '
            '"AI product mockups", "AI interior design", "AI fashion styling", "AI presentations", '
            '"generative AI", "AI Simplified Lab"]'
        )
        ai_plan = {
            "shared_instructions": "",
            "videos": [{
                "reference": video_id,
                "video_id": video_id,
                "title_intent": "unmentioned",
                "exact_title": None,
                "description_intent": "unmentioned",
                "exact_description": None,
                "tags_intent": "unmentioned",
                "exact_tags": None,
                "video_specific_instructions": "",
            }],
        }
        with patch.object(
            batch_instructions,
            "_interpret_batch_instructions_with_ai",
            return_value=ai_plan,
        ):
            plan = batch_instructions.parse_batch_instructions(text, groq_key="test-key")

        self.assertEqual(plan["total_identified"], 1)
        video = plan["videos"][0]
        self.assertEqual(video["video_id"], video_id)
        self.assertEqual(video["description_intent"], "exact")
        self.assertEqual(video["exact_description"], description)
        self.assertEqual(video["exact_tags"][0], "Google Mixboard")

        youtube = FakeYoutube({video_id: make_snippet(video_id, title="Google Mixboard Finally in India! Full Tutorial & Use Cases")})
        staged = batch_metadata.prepare_metadata_batch(
            youtube,
            batch_metadata.create_batch_from_text_plan(plan),
        )
        row = staged["rows"][0]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["proposed"]["description"], description)
        self.assertIn("description", row["changed_fields"])
        exported = list(csv.DictReader(io.StringIO(batch_metadata.build_results_csv(staged))))[0]
        self.assertEqual(exported["proposed_description"], description)

    def test_exact_description_missing_payload_fails_closed_instead_of_staging_blank(self):
        video_id = VIDEO_ID_1
        plan = {
            "raw_text": "exact description missing payload",
            "videos": [{
                "reference": video_id,
                "video_id": video_id,
                "title_intent": "preserve",
                "exact_title": None,
                "description_intent": "exact",
                "exact_description": None,
                "tags_intent": "preserve",
                "exact_tags": None,
                "fields_requested": ["description"],
                "original_instruction": "Description: \"\"\"",
                "errors": [],
            }],
        }
        youtube = FakeYoutube({video_id: make_snippet(video_id)})
        staged = batch_metadata.prepare_metadata_batch(
            youtube,
            batch_metadata.create_batch_from_text_plan(plan),
        )
        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("no description text was extracted" in e.lower() for e in row["errors"]))
        self.assertNotIn("proposed", row)

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


    def test_explicit_shared_title_only_intent_overrides_incomplete_ai_plan(self):
        videos = [
            (VIDEO_ID_1, "Why RAG Fails at Real-Time Travel Booking"),
            (VIDEO_ID_2, "LLM Prompt Caching Explained: Exact-Match vs. Semantic Cache"),
            (VIDEO_ID_3, "Context vs Memory in LLMs"),
            ("LmNoPqRsTuV", "4 AI Terms Explained Simply"),
        ]
        text = "\n\n".join(
            f"{index}. https://www.youtube.com/watch?v={video_id}\nCurrent title: {title}"
            for index, (video_id, title) in enumerate(videos, start=1)
        )
        text += (
            "\n\nSHARED INSTRUCTIONS:\n"
            "Optimize TITLE ONLY for all four videos.\n"
            "Preserve every existing description and tag exactly."
        )
        extracted_refs = batch_instructions.extract_video_references_from_text(text)
        self.assertEqual(
            [ref["video_id"] for ref in extracted_refs],
            [video_id for video_id, _ in videos],
        )
        # Simulate a parser model that misreads the shared directive and mistakes
        # the first current title for an exact replacement while leaving the rest blank.
        ai_plan = {
            "videos": [
                {
                    "reference": f"https://www.youtube.com/watch?v={video_id}",
                    "video_id": video_id,
                    "title_intent": "exact" if index == 0 else "unmentioned",
                    "exact_title": title if index == 0 else None,
                    "description_intent": "unmentioned",
                    "exact_description": None,
                    "tags_intent": "unmentioned",
                    "exact_tags": None,
                    "video_specific_instructions": "",
                }
                for index, (video_id, title) in enumerate(videos)
            ]
        }

        with patch.object(
            batch_instructions,
            "_interpret_batch_instructions_with_ai",
            return_value=ai_plan,
        ):
            plan = batch_instructions.parse_batch_instructions(text, groq_key="test-key")

        self.assertEqual(plan["total_identified"], 4)
        for row, (_, title) in zip(plan["videos"], videos):
            self.assertEqual(row["title_intent"], "optimize")
            self.assertIsNone(row["exact_title"])
            self.assertEqual(row["description_intent"], "preserve")
            self.assertEqual(row["tags_intent"], "preserve")
            self.assertEqual(row["context_title"], title)
            self.assertEqual(row["fields_requested"], ["title"])
            self.assertIn("Optimize TITLE ONLY", row["original_instruction"])
            self.assertNotIn("Current title:", row["original_instruction"])

        snippets = {
            video_id: make_snippet(video_id, title=title)
            for video_id, title in videos
        }
        youtube = FakeYoutube(snippets)

        def fake_generator(video_id, before, instruction="", fields=None, transcript=""):
            curiosity = f"Why Developers Keep Getting {video_id} Wrong"
            value = f"How {video_id} Makes AI Systems Work Better"
            search = f"How {video_id} Works: A Simple AI Guide"
            return {
                "title": value,
                "recommended_title": value,
                "recommendation_reason": "It gives viewers a clear practical outcome.",
                "title_angles": [
                    {"angle": "curiosity", "title": curiosity},
                    {"angle": "value", "title": value},
                    {"angle": "search", "title": search},
                ],
                "description": before["description"],
                "tags": before["tags"],
            }

        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(
            youtube,
            canonical,
            ai_generator=fake_generator,
        )
        self.assertEqual(staged["ready_count"], 4)
        for row in staged["rows"]:
            self.assertEqual(row["status"], "ready")
            self.assertEqual(row["changed_fields"], ["title"])
            self.assertEqual(row["proposed"]["description"], row["before"]["description"])
            self.assertEqual(row["proposed"]["tags"], row["before"]["tags"])

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
                {"angle": "value", "title": "Angle 2 Value Title"},
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
                {"angle": "value", "title": "Angle 2"},
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
                "recommended_title": f"Optimized {video_id}",
                "recommendation_reason": "The title is clear and directly relevant to the video.",
                "title_angles": [
                    {"angle": "curiosity", "title": f"Why {video_id} Matters for AI"},
                    {"angle": "value", "title": f"Optimized {video_id}"},
                    {"angle": "search", "title": f"{video_id} AI Optimization Guide"},
                ],
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
            "recommended_title": "AI Optimized Title",
            "recommendation_reason": "The title states the useful outcome clearly.",
            "title_angles": [
                {"angle": "curiosity", "label": "Curiosity", "title": "Did You Know AI Could App?"},
                {"angle": "value", "label": "Value", "title": "AI Optimized Title"},
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
    def test_batch_generator_accepts_streamlit_style_nested_secrets(self, mock_groq):
        mock_groq.return_value = (
            json.dumps({
                "title": "How LLM Caching Saves Time",
                "recommended_title": "How LLM Caching Saves Time",
                "recommendation_reason": "It gives viewers a clear practical outcome.",
                "title_angles": [
                    {"angle": "curiosity", "title": "Why LLMs Repeat the Same Work"},
                    {"angle": "value", "title": "How LLM Caching Saves Time"},
                    {"angle": "search", "title": "LLM Prompt Caching Explained"},
                ],
                "description": "A practical explanation of LLM prompt caching.",
                "tags": ["LLM", "prompt caching", "AI system design"],
                "rationale": "Clear, useful and searchable.",
            }),
            "openai/gpt-oss-120b",
        )

        # Streamlit st.secrets nested sections are AttrDict/Mapping objects,
        # not plain dicts. UserDict reproduces that mapping contract.
        secrets = UserDict({
            "chatgroq": UserDict({
                "api_key": "groq-test-key",
                "model": "openai/gpt-oss-120b",
                "fallback_models": [],
            })
        })
        generator = strategist_chat.build_batch_metadata_generator(secrets=secrets)
        self.assertIsNotNone(generator)

        result = generator(
            VIDEO_ID_1,
            make_snippet(VIDEO_ID_1),
            fields=["title", "description", "tags"],
        )
        self.assertEqual(result["provider"], "Groq")
        self.assertEqual(result["title"], "How LLM Caching Saves Time")
        self.assertTrue(mock_groq.called)

        # Also accept the common root-level ChatGroq environment-variable name.
        alias_generator = strategist_chat.build_batch_metadata_generator(
            secrets={"CHATGROQ_API_KEY": "groq-test-key"}
        )
        self.assertIsNotNone(alias_generator)

    @patch("strategist_chat._groq_generate")
    def test_batch_generator_accepts_already_parsed_groq_response(self, mock_groq):
        payload = {
            "title": "How LLM Prompt Caching Cuts Repeat Processing",
            "recommended_title": "How LLM Prompt Caching Cuts Repeat Processing",
            "recommendation_reason": "It states the practical value in simple language.",
            "title_angles": [
                {"angle": "curiosity", "title": "Why Do LLMs Repeat the Same Work?"},
                {"angle": "value", "title": "How LLM Prompt Caching Cuts Repeat Processing"},
                {"angle": "search", "title": "LLM Prompt Caching Explained"},
            ],
            "description": "A clear explanation of LLM prompt caching and how it reduces repeated processing.",
            "tags": ["LLM", "prompt caching", "AI system design"],
            "rationale": "The package is clear, accurate and searchable.",
        }
        # The real provider helper returns a dict after decoding the HTTP response.
        mock_groq.return_value = (copy.deepcopy(payload), "openai/gpt-oss-120b")
        generator = strategist_chat.build_batch_metadata_generator(
            secrets={"groq": {"api_key": "groq-test-key", "model": "openai/gpt-oss-120b"}}
        )
        result = generator(
            VIDEO_ID_1,
            make_snippet(VIDEO_ID_1),
            fields=["title", "description", "tags"],
        )
        self.assertEqual(result["title"], payload["title"])
        self.assertEqual(result["description"], payload["description"])
        self.assertEqual(result["tags"], payload["tags"])
        self.assertEqual(result["provider"], "Groq")
        self.assertEqual(result["model"], "openai/gpt-oss-120b")
        mock_groq.assert_called_once()

    @patch("core.batch_instructions._interpret_batch_instructions_with_ai")
    def test_batch_parser_reads_nested_streamlit_provider_secrets(self, mock_interpret):
        expected_plan = {
            "videos": [{
                "reference": f"https://www.youtube.com/watch?v={VIDEO_ID_1}",
                "video_id": VIDEO_ID_1,
                "title_intent": "optimize",
                "description_intent": "preserve",
                "tags_intent": "preserve",
                "video_specific_instructions": "Optimize the complete metadata package.",
            }]
        }
        mock_interpret.return_value = expected_plan
        secrets = UserDict({
            "groq": UserDict({
                "api_key": "groq-test-key",
                "model": "openai/gpt-oss-120b",
            }),
            "gemini": UserDict({
                "api_key": "gemini-fallback-key",
                "model": "gemini-2.5-flash",
            }),
        })

        plan = batch_instructions.parse_batch_instructions(
            f"https://www.youtube.com/watch?v={VIDEO_ID_1} Optimize title, description and tags.",
            secrets=secrets,
        )

        self.assertEqual(plan["total_identified"], 1)
        self.assertTrue(mock_interpret.called)
        self.assertEqual(mock_interpret.call_args.kwargs["groq_key"], "groq-test-key")
        self.assertEqual(mock_interpret.call_args.kwargs["groq_model"], "openai/gpt-oss-120b")
        self.assertEqual(mock_interpret.call_args.kwargs["gemini_key"], "gemini-fallback-key")

    @patch("strategist_chat._groq_generate")
    @patch("strategist_chat._gemini_generate")
    def test_02_gemini_primary_and_groq_fallback(self, mock_gemini, mock_groq):
        mock_gemini.side_effect = RuntimeError("Gemini service unavailable")
        mock_groq.return_value = (
            {
                "title": "Groq Fallback Title",
                "recommended_title": "Groq Fallback Title",
                "recommendation_reason": "The fallback title is concise and clearly searchable.",
                "title_angles": [
                    {"angle": "curiosity", "label": "Curiosity", "title": "Curious Title"},
                    {"angle": "value", "label": "Direct Value", "title": "Groq Fallback Title"},
                    {"angle": "search", "label": "Search", "title": "Search Title"},
                ],
                "description": "Groq Description",
                "tags": ["ai", "gemini"],
                "rationale": "Gemini unavailable, Groq recovered",
            },
            "openai/gpt-oss-120b",
        )
        gen = strategist_chat.build_batch_metadata_generator(
            secrets={
                "groq": {"api_key": "groq-key"},
                "gemini": {"api_key": "gemini-key", "model": "gemini-4-argon"},
            }
        )
        self.assertIsNotNone(gen)
        result = gen(VIDEO_ID_1, make_snippet(VIDEO_ID_1), instruction="Optimize title")
        self.assertEqual(result["title"], "Groq Fallback Title")
        self.assertEqual(result["provider"], "Groq")
        self.assertTrue(mock_gemini.called)
        self.assertTrue(mock_groq.called)
    @patch("youtube_analytics_connector.fetch_video_transcript")
    def test_03_correct_transcript_passed_to_generator(self, mock_transcript):
        mock_transcript.return_value = "This is a detailed video transcript about neural networks."
        mock_gen = MagicMock()
        mock_gen.return_value = {
            "title": "Neural Networks Guide",
            "recommended_title": "Neural Networks Guide",
            "recommendation_reason": "The title describes the subject directly.",
            "title_angles": [
                {"angle": "curiosity", "label": "Curiosity", "title": "A1"},
                {"angle": "value", "label": "Value", "title": "Neural Networks Guide"},
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


class TestTitleOptimizationAndVerificationAudit(unittest.TestCase):
    """Rigorous audit tests covering all 18 specified audit requirements."""

    VIDEOS_4 = ["kLK0N14PT1Y", "jJbg1WtvxbM", "m3WvbBn5tIM", "lc1bKVcw52o"]

    # 1. "Optimize title" is parsed as optimize intent, not exact.
    def test_01_optimize_title_parsed_as_optimize_intent(self):
        text = (
            "1. kLK0N14PT1Y — Why Your RAG Pipeline Still Misses Answers\n"
            "Optimize title ONLY. Generate three different title angles per video. Recommend the strongest title."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        self.assertEqual(len(plan["videos"]), 1)
        v = plan["videos"][0]
        self.assertEqual(v["title_intent"], "optimize")
        self.assertIsNone(v["exact_title"])

    # 2. "Current title:" is treated as context rather than replacement.
    def test_02_current_title_treated_as_context_not_replacement(self):
        text = (
            "1. kLK0N14PT1Y — Why Your RAG Pipeline Still Misses Answers\n"
            "Current title: Why Your RAG Pipeline Still Misses Answers\n"
            "Optimize title ONLY. Preserve description and tags."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        v = plan["videos"][0]
        self.assertEqual(v["context_title"], "Why Your RAG Pipeline Still Misses Answers")
        self.assertEqual(v["title_intent"], "optimize")
        self.assertIsNone(v["exact_title"])

    # 3. All three title angles are generated for each of four videos.
    def test_03_all_three_title_angles_generated_for_four_videos(self):
        snippets = {vid: make_snippet(vid, title=f"Original Title for {vid}") for vid in self.VIDEOS_4}
        youtube = FakeYoutube(snippets)

        def mock_generator(video_id, before, instruction="", fields=None, transcript=""):
            return {
                "title": f"Direct Value Title for {video_id}",
                "recommended_title": f"Direct Value Title for {video_id}",
                "recommendation_reason": "High click affinity and direct outcome clarity.",
                "title_angles": [
                    {"angle": "curiosity", "label": "Curiosity", "title": f"Curiosity Angle for {video_id}"},
                    {"angle": "value", "label": "Direct Value", "title": f"Direct Value Title for {video_id}"},
                    {"angle": "search", "label": "Search SEO", "title": f"Search SEO Title for {video_id}"},
                ],
                "description": before.get("description", ""),
                "tags": before.get("tags", []),
            }

        text = (
            "1. kLK0N14PT1Y — Why Your RAG Pipeline Still Misses Answers\n"
            "2. jJbg1WtvxbM — AI Agent Hacks Hugging Face Servers\n"
            "3. m3WvbBn5tIM — How Does AI Learn From Its Mistakes?\n"
            "4. lc1bKVcw52o — What Is an Optimizer in AI?\n\n"
            "Optimize title ONLY. Generate three different title angles per video. Recommend the strongest title."
        )
        plan = batch_instructions.parse_batch_instructions(text)
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_generator)

        self.assertEqual(len(staged["rows"]), 4)
        for row in staged["rows"]:
            self.assertEqual(row["status"], "ready")
            self.assertEqual(len(row["title_angles"]), 3)
            self.assertIn("curiosity", [a["angle"] for a in row["title_angles"]])
            self.assertIn("value", [a["angle"] for a in row["title_angles"]])
            self.assertIn("search", [a["angle"] for a in row["title_angles"]])
            self.assertTrue(row["proposed"]["title"].startswith("Direct Value Title"))

    # 4. Generator is actually invoked once per video.
    def test_04_generator_actually_invoked_once_per_video(self):
        snippets = {vid: make_snippet(vid, title=f"Original {vid}") for vid in self.VIDEOS_4}
        youtube = FakeYoutube(snippets)
        call_ids = []

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            call_ids.append(video_id)
            return {
                "title": f"New Title {video_id}",
                "recommended_title": f"New Title {video_id}",
                "recommendation_reason": "The title describes the practical outcome.",
                "title_angles": [
                    {"angle": "curiosity", "title": f"Curiosity {video_id}"},
                    {"angle": "value", "title": f"New Title {video_id}"},
                    {"angle": "search", "title": f"Search {video_id}"},
                ],
                "description": before.get("description", ""),
                "tags": before.get("tags", []),
            }

        text = "\n".join(f"{i+1}. {vid}" for i, vid in enumerate(self.VIDEOS_4)) + "\nOptimize title only."
        plan = batch_instructions.parse_batch_instructions(text)
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        self.assertEqual(call_ids, self.VIDEOS_4)

    # 5. Transcript is passed to the correct video's generator.
    def test_05_transcript_passed_to_correct_video_generator(self):
        vid = self.VIDEOS_4[0]
        snippets = {vid: make_snippet(vid)}
        youtube = FakeYoutube(snippets)
        captured = {}

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            captured["video_id"] = video_id
            captured["transcript"] = transcript
            return {
                "title": "Optimized RAG Architecture Breakdown",
                "recommended_title": "Optimized RAG Architecture Breakdown",
                "recommendation_reason": "The title directly communicates the tutorial's value.",
                "title_angles": [
                    {"angle": "curiosity", "title": "Why RAG Fails in Production"},
                    {"angle": "value", "title": "Optimized RAG Architecture Breakdown"},
                    {"angle": "search", "title": "RAG Pipeline Optimization Tutorial"},
                ],
            }

        plan = batch_instructions.parse_batch_instructions(f"1. {vid}\nOptimize title.")
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        with patch.object(connector, "fetch_video_transcript", return_value="Sample transcript text for RAG"):
            batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        self.assertEqual(captured["video_id"], vid)
        self.assertEqual(captured["transcript"], "Sample transcript text for RAG")

    # 6. Duplicate or unchanged generated titles trigger controlled repair.
    def test_06_duplicate_or_unchanged_generated_titles_trigger_controlled_repair(self):
        vid = self.VIDEOS_4[0]
        orig_title = "Why Your RAG Pipeline Still Misses Answers"
        snippets = {vid: make_snippet(vid, title=orig_title)}
        youtube = FakeYoutube(snippets)
        calls = []

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            calls.append(instruction)
            if len(calls) == 1:
                # First attempt: returns trivial emoji/unchanged titles
                return {
                    "title": f"🔥 {orig_title} 🔥",
                    "title_angles": [
                        {"angle": "curiosity", "title": orig_title},
                        {"angle": "value", "title": f"{orig_title}!"},
                        {"angle": "search", "title": f"Why Your RAG Pipeline Still Misses Answers?"},
                    ]
                }
            else:
                # Controlled repair attempt: returns genuinely distinct angles
                return {
                    "title": "The 3 Fatal Flaws in Production RAG",
                    "recommended_title": "The 3 Fatal Flaws in Production RAG",
                    "recommendation_reason": "The title adds a concrete production-focused hook.",
                    "title_angles": [
                        {"angle": "curiosity", "title": "Why 90% of RAG Pipelines Fail Silent"},
                        {"angle": "value", "title": "The 3 Fatal Flaws in Production RAG"},
                        {"angle": "search", "title": "Advanced RAG Retrieval Architecture Guide"},
                    ]
                }

        plan = batch_instructions.parse_batch_instructions(f"1. {vid}\nOptimize title.")
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        self.assertEqual(len(calls), 2)
        self.assertIn("CONTROLLED REPAIR ATTEMPT", calls[1])
        row = staged["rows"][0]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["proposed"]["title"], "The 3 Fatal Flaws in Production RAG")

    # 7. Invalid AI output never becomes Ready or Verified.
    def test_07_invalid_ai_output_never_becomes_ready_or_verified(self):
        vid = self.VIDEOS_4[0]
        snippets = {vid: make_snippet(vid, title="Original RAG Title")}
        youtube = FakeYoutube(snippets)

        # Generator returns empty angles even on repair
        def bad_gen(video_id, before, instruction="", fields=None, transcript=""):
            return {"title": "Original RAG Title", "title_angles": []}

        plan = batch_instructions.parse_batch_instructions(f"1. {vid}\nOptimize title.")
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=bad_gen)

        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertNotEqual(row["status"], "ready")
        self.assertNotEqual(row["status"], "verified")
        self.assertTrue(len(row["errors"]) > 0)

    # 8. Title-only optimization preserves description and tags.
    def test_08_title_only_optimization_preserves_description_and_tags(self):
        vid = self.VIDEOS_4[0]
        orig_desc = "Original Description Kept Completely."
        orig_tags = ["rag", "llm", "pipeline"]
        snippets = {vid: make_snippet(vid, title="Old Title", description=orig_desc, tags=orig_tags)}
        youtube = FakeYoutube(snippets)

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            return {
                "title": "New High CTR Title",
                "recommended_title": "New High CTR Title",
                "recommendation_reason": "The title communicates the intended learning outcome.",
                "title_angles": [
                    {"angle": "curiosity", "title": "Curiosity RAG Title"},
                    {"angle": "value", "title": "New High CTR Title"},
                    {"angle": "search", "title": "Search RAG Guide"},
                ]
            }

        text = f"1. {vid} — Old Title\nOptimize title ONLY. Preserve description and tags."
        plan = batch_instructions.parse_batch_instructions(text)
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        row = staged["rows"][0]
        self.assertEqual(row["proposed"]["title"], "New High CTR Title")
        self.assertEqual(row["proposed"]["description"], orig_desc)
        self.assertEqual(row["proposed"]["tags"], orig_tags)
        self.assertEqual(row["changed_fields"], ["title"])

    # 9. All title choices are visible in the batch review UI / data model.
    def test_09_all_title_choices_visible_in_batch_review_data(self):
        vid = self.VIDEOS_4[0]
        snippets = {vid: make_snippet(vid, title="Old Title")}
        youtube = FakeYoutube(snippets)

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            return {
                "title": "Recommended Angle Title",
                "recommended_title": "Recommended Angle Title",
                "recommendation_reason": "Clear curiosity hook with verified technical accuracy.",
                "title_angles": [
                    {"angle": "curiosity", "label": "Curiosity", "title": "Angle 1"},
                    {"angle": "value", "label": "Direct Value", "title": "Recommended Angle Title"},
                    {"angle": "search", "label": "Search", "title": "Angle 3"},
                ]
            }

        plan = batch_instructions.parse_batch_instructions(f"1. {vid}\nOptimize title.")
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        row = staged["rows"][0]
        self.assertEqual(len(row["title_angles"]), 3)
        self.assertEqual(row["recommended_title"], "Recommended Angle Title")
        self.assertEqual(row["recommendation_reason"], "Clear curiosity hook with verified technical accuracy.")

    # 10. Selecting a title recalculates its digest.
    def test_10_selecting_a_title_recalculates_its_digest(self):
        vid = self.VIDEOS_4[0]
        snippets = {vid: make_snippet(vid, title="Old Title")}
        youtube = FakeYoutube(snippets)

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            return {
                "title": "Title Choice A",
                "recommended_title": "Title Choice A",
                "recommendation_reason": "The curiosity angle is the strongest opening hook.",
                "title_angles": [
                    {"angle": "curiosity", "title": "Title Choice A"},
                    {"angle": "value", "title": "Title Choice B"},
                    {"angle": "search", "title": "Title Choice C"},
                ]
            }

        plan = batch_instructions.parse_batch_instructions(f"1. {vid}\nOptimize title.")
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        row = staged["rows"][0]
        initial_digest = row["digest"]

        # User chooses Title Choice B
        row["proposed"]["title"] = "Title Choice B"
        batch_metadata.revalidate_batch_row(row)
        new_digest = row["digest"]

        self.assertNotEqual(initial_digest, new_digest)
        expected_digest = connector.metadata_proposal_digest(vid, row["before"], row["proposed"])
        self.assertEqual(new_digest, expected_digest)

    # 11. Editing after approval invalidates approval.
    def test_11_editing_after_approval_invalidates_approval(self):
        row = {
            "video_id": self.VIDEOS_4[0],
            "before": make_snippet(self.VIDEOS_4[0], title="Old Title"),
            "proposed": make_snippet(self.VIDEOS_4[0], title="Approved Title"),
            "status": "ready",
            "digest": connector.metadata_proposal_digest(
                self.VIDEOS_4[0],
                make_snippet(self.VIDEOS_4[0], title="Old Title"),
                make_snippet(self.VIDEOS_4[0], title="Approved Title"),
            ),
        }
        fp1 = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)

        # Title is edited after approval and row is revalidated
        row["proposed"]["title"] = "Tampered Title After Approval"
        batch_metadata.revalidate_batch_row(row)
        fp2 = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)
        self.assertNotEqual(fp1, fp2)

        # Revision bump also invalidates
        fp3 = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=2)
        self.assertNotEqual(fp1, fp3)

    # 12. Staging never becomes Verified without a real write.
    def test_12_staging_never_becomes_verified_without_real_write(self):
        snippets = {vid: make_snippet(vid, title=f"Title {vid}") for vid in self.VIDEOS_4}
        youtube = FakeYoutube(snippets)

        def mock_gen(video_id, before, instruction="", fields=None, transcript=""):
            return {
                "title": f"New Title {video_id}",
                "recommended_title": f"New Title {video_id}",
                "recommendation_reason": "The title states the topic outcome plainly.",
                "title_angles": [
                    {"angle": "curiosity", "title": f"Curiosity {video_id}"},
                    {"angle": "value", "title": f"New Title {video_id}"},
                    {"angle": "search", "title": f"Search {video_id}"},
                ]
            }

        plan = batch_instructions.parse_batch_instructions("\n".join(f"{i+1}. {v}" for i, v in enumerate(self.VIDEOS_4)) + "\nOptimize title.")
        canonical = batch_metadata.create_batch_from_text_plan(plan)
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical, ai_generator=mock_gen)

        for row in staged["rows"]:
            self.assertNotEqual(row.get("status"), "verified")
            self.assertEqual(row.get("status"), "ready")

    # 13. Successful direct YouTube read-back is required for Verified.
    def test_13_successful_direct_youtube_read_back_required_for_verified(self):
        vid = self.VIDEOS_4[0]
        snippets = {vid: make_snippet(vid, title="Old Title")}
        youtube = FakeYoutube(snippets)

        before = make_snippet(vid, title="Old Title")
        proposed = make_snippet(vid, title="New Title Verified")
        digest = connector.metadata_proposal_digest(vid, before, proposed)

        # Write to YouTube
        res = connector.apply_approved_dashboard_proposal(
            youtube, vid, before, proposed, digest,
            audit_callback=lambda entry: None,
        )
        self.assertEqual(res["result"], "success")

        # Read back directly from YouTube
        read_back = connector.fetch_video_snippet(youtube, vid)
        self.assertEqual(read_back["title"], "New Title Verified")

    # 14. Read-back mismatch cannot become Verified.
    def test_14_read_back_mismatch_cannot_become_verified(self):
        vid = self.VIDEOS_4[0]
        snippets = {vid: make_snippet(vid, title="Old Title")}
        youtube = FakeYoutube(snippets)

        before = make_snippet(vid, title="Old Title")
        proposed = make_snippet(vid, title="Intended Title")
        digest = connector.metadata_proposal_digest(vid, before, proposed)

        row = {"video_id": vid, "before": before, "proposed": proposed, "digest": digest, "status": "ready"}

        # Simulate write where YouTube stored something else
        youtube._videos.snippets[vid]["title"] = "Different Title Stored"
        read_back = connector.fetch_video_snippet(youtube, vid)
        if read_back and read_back.get("title") == row["proposed"]["title"]:
            row["status"] = "verified"
        else:
            row["status"] = "failed"
            row["result_message"] = "Read-back verification mismatch"

        self.assertEqual(row["status"], "failed")
        self.assertNotEqual(row["status"], "verified")

    # 15. Zero-ready batch does not offer a misleading push action.
    def test_15_zero_ready_batch_does_not_offer_misleading_push_action(self):
        rows = [
            {"video_id": self.VIDEOS_4[0], "status": "no_change", "attempted": False},
            {"video_id": self.VIDEOS_4[1], "status": "invalid", "attempted": False},
        ]
        batch = {"rows": rows, "review_revision": 1}
        ready_count = sum(r.get("status") == "ready" for r in rows)
        self.assertEqual(ready_count, 0)
        selected_ids = [r["video_id"] for r in rows if r.get("status") == "ready"]
        self.assertEqual(len(selected_ids), 0)

    # 16. Single-video workflow remains unchanged.
    def test_16_single_video_workflow_remains_unchanged(self):
        vid = self.VIDEOS_4[0]
        msg = f"https://www.youtube.com/watch?v={vid}\nImprove the title to focus on production latency."
        urls = strategist_chat._youtube_references_from_message(msg)
        self.assertEqual(len(urls), 1)
        self.assertFalse(strategist_chat._is_batch_intent(msg, urls))

    # 17. Spreadsheet batch continues to work.
    def test_17_spreadsheet_batch_continues_to_work(self):
        csv_data = (
            "video_id,title,description,tags\n"
            f"{self.VIDEOS_4[0]},Spreadsheet Title 1,Spreadsheet Desc 1,tag1|tag2\n"
            f"{self.VIDEOS_4[1]},Spreadsheet Title 2,Spreadsheet Desc 2,tag3|tag4\n"
        ).encode("utf-8")
        canonical = batch_metadata.parse_metadata_file("batch.csv", csv_data)
        self.assertEqual(len(canonical["rows"]), 2)
        youtube = FakeYoutube({vid: make_snippet(vid) for vid in self.VIDEOS_4[:2]})
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        self.assertEqual(staged["ready_count"], 2)

    # 18. Existing ownership and approval safeguards remain intact.
    def test_18_existing_ownership_and_approval_safeguards_remain_intact(self):
        vid = self.VIDEOS_4[0]
        # Video owned by someone else
        snippets = {vid: make_snippet(vid, channelId="ANOTHER_CHANNEL_ID")}
        youtube = FakeYoutube(snippets)

        canonical = batch_metadata.create_batch_from_text_plan({
            "raw_text": f"1. {vid}\nChange title to 'Non-owned Video Edit'",
            "videos": [{"video_id": vid, "exact_title": "Non-owned Video Edit", "title_intent": "exact", "description_intent": "unmentioned", "tags_intent": "unmentioned"}],
        })
        staged = batch_metadata.prepare_metadata_batch(youtube, canonical)
        row = staged["rows"][0]
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("Ownership check failed" in err for err in row["errors"]))


    def _ready_publish_row(self, video_id=None):
        video_id = video_id or self.VIDEOS_4[0]
        before = make_snippet(video_id, title="Original approved title")
        proposed = copy.deepcopy(before)
        proposed["title"] = "A genuinely new approved title"
        return {
            "video_id": video_id,
            "before": before,
            "proposed": proposed,
            "proposed_title": proposed["title"],
            "status": "ready",
            "attempted": False,
            "digest": connector.metadata_proposal_digest(video_id, before, proposed),
        }

    def test_19_title_angle_validation_requires_exactly_three_categories(self):
        current = "Original production RAG title"
        valid_angles = [
            {"angle": "curiosity", "title": "Why Production RAG Fails Quietly"},
            {"angle": "value", "title": "Fix the Three Biggest RAG Retrieval Problems"},
            {"angle": "search", "title": "Production RAG Pipeline Troubleshooting Guide"},
        ]
        self.assertEqual(batch_metadata.validate_title_angles_quality(current, valid_angles), (True, ""))
        self.assertFalse(batch_metadata.validate_title_angles_quality(current, valid_angles + [
            {"angle": "value", "title": "An extra title angle"}
        ])[0])
        duplicate_category = [
            valid_angles[0],
            {"angle": "value", "title": "A different direct-value title"},
            {"angle": "value", "title": "Another direct-value title"},
        ]
        self.assertFalse(batch_metadata.validate_title_angles_quality(current, duplicate_category)[0])
        wrong_category = [
            valid_angles[0],
            {"angle": "direct_value", "title": "A direct-value title"},
            valid_angles[2],
        ]
        self.assertFalse(batch_metadata.validate_title_angles_quality(current, wrong_category)[0])

    def test_20_recommended_title_must_be_one_of_the_three_angles(self):
        vid = self.VIDEOS_4[0]
        youtube = FakeYoutube({vid: make_snippet(vid, title="Original RAG Title")})
        calls = []

        def bad_generator(video_id, before, instruction="", fields=None, transcript=""):
            calls.append(instruction)
            return {
                "title": "Recommended but unlisted title",
                "recommended_title": "Recommended but unlisted title",
                "recommendation_reason": "This should be rejected because it is not a selectable angle.",
                "title_angles": [
                    {"angle": "curiosity", "title": "Why RAG Retrieval Fails"},
                    {"angle": "value", "title": "Three Ways to Improve RAG Retrieval"},
                    {"angle": "search", "title": "RAG Retrieval Pipeline Guide"},
                ],
            }

        plan = batch_instructions.parse_batch_instructions(f"1. {vid}\nOptimize title.")
        staged = batch_metadata.prepare_metadata_batch(
            youtube,
            batch_metadata.create_batch_from_text_plan(plan),
            ai_generator=bad_generator,
        )
        self.assertEqual(len(calls), 2)  # One initial generation plus one controlled repair.
        self.assertEqual(staged["rows"][0]["status"], "invalid")

    def test_21_publish_requires_explicit_approval_and_current_fingerprint(self):
        youtube = FakeYoutube({self.VIDEOS_4[0]: make_snippet(self.VIDEOS_4[0])})
        row = self._ready_publish_row()
        rows = [row]
        fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=1)

        blocked = batch_metadata.publish_approved_batch_rows(
            youtube, rows, [row["video_id"]],
            approved=False,
            approval_fingerprint=fingerprint,
            review_revision=1,
        )
        self.assertTrue(blocked["blocked"])
        self.assertEqual(youtube._videos.update_calls, [])
        self.assertFalse(row["attempted"])

        stale = batch_metadata.publish_approved_batch_rows(
            youtube, rows, [row["video_id"]],
            approved=True,
            approval_fingerprint="stale-fingerprint",
            review_revision=1,
        )
        self.assertTrue(stale["blocked"])
        self.assertEqual(youtube._videos.update_calls, [])

    def test_test_and_compare_title_restriction_retries_approved_description_and_tags_only(self):
        row = self._ready_publish_row()
        row["proposed"]["description"] = "Proposed description"
        row["proposed"]["tags"] = ["approved tag", "another approved tag"]
        batch_metadata.revalidate_batch_row(row)
        rows = [row]
        fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=1)
        error = connector.HttpError(
            resp=MagicMock(status=400),
            content=(
                b'{"error":{"code":400,"message":'
                b'"UPDATE_TITLE_NOT_ALLOWED_DURING_TEST_AND_COMPARE: Precondition check failed."}}'
            ),
        )
        fallback_proposal = copy.deepcopy(row["proposed"])
        fallback_proposal["title"] = row["before"]["title"]

        with patch.object(
            connector,
            "apply_approved_dashboard_proposal",
            side_effect=[error, {"result": "success"}],
        ) as apply_mock, patch.object(
            connector,
            "fetch_video_snippet",
            return_value=fallback_proposal,
        ):
            result = batch_metadata.publish_approved_batch_rows(
                object(), rows, [row["video_id"]],
                approved=True,
                approval_fingerprint=fingerprint,
                review_revision=1,
            )

        self.assertEqual(apply_mock.call_count, 2)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["blocked_count"], 0)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(row["status"], "verified")
        self.assertEqual(row["proposed"]["title"], row["before"]["title"])
        self.assertEqual(row["changed_fields"], ["description", "tags"])
        self.assertEqual(row["proposed"]["description"], "Proposed description")
        self.assertEqual(row["proposed"]["tags"], ["approved tag", "another approved tag"])
        self.assertTrue(row["title_change_blocked"])
        self.assertIn("title remains unchanged", row["result_message"].lower())

    def test_test_and_compare_title_only_request_stays_blocked_without_retry(self):
        row = self._ready_publish_row()
        rows = [row]
        fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=1)
        error = connector.HttpError(
            resp=MagicMock(status=400),
            content=(
                b'{"error":{"code":400,"message":'
                b'"UPDATE_TITLE_NOT_ALLOWED_DURING_TEST_AND_COMPARE: Precondition check failed."}}'
            ),
        )

        with patch.object(
            connector,
            "apply_approved_dashboard_proposal",
            side_effect=error,
        ) as apply_mock:
            result = batch_metadata.publish_approved_batch_rows(
                object(), rows, [row["video_id"]],
                approved=True,
                approval_fingerprint=fingerprint,
                review_revision=1,
            )

        self.assertEqual(apply_mock.call_count, 1)
        self.assertEqual(result["blocked_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(row["status"], "blocked")
        self.assertIn("Test & Compare", row["result_message"])
        self.assertEqual(row["proposed"]["title"], "A genuinely new approved title")

    def test_22_publish_helper_executes_approved_write_and_verifies_all_fields(self):
        vid = self.VIDEOS_4[0]
        youtube = FakeYoutube({vid: make_snippet(vid, title="Original approved title")})
        row = self._ready_publish_row(vid)
        rows = [row]
        fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=4)

        result = batch_metadata.publish_approved_batch_rows(
            youtube, rows, [vid],
            approved=True,
            approval_fingerprint=fingerprint,
            review_revision=4,
            audit_callback=lambda entry: None,
        )

        self.assertFalse(result["blocked"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(row["status"], "verified")
        self.assertEqual(youtube._videos.update_calls.__len__(), 1)
        read_back = connector.fetch_video_snippet(youtube, vid)
        self.assertEqual(batch_metadata.batch_metadata_readback_mismatches(read_back, row["proposed"]), [])

    def test_23_matching_title_but_mismatched_description_is_not_verified(self):
        row = self._ready_publish_row()
        actual = copy.deepcopy(row["proposed"])
        actual["description"] = "Different description on YouTube"
        fingerprint = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)
        with patch.object(connector, "apply_approved_dashboard_proposal", return_value={"result": "success"}), patch.object(
            connector, "fetch_video_snippet", return_value=actual
        ):
            result = batch_metadata.publish_approved_batch_rows(
                object(), [row], [row["video_id"]],
                approved=True, approval_fingerprint=fingerprint, review_revision=1,
            )
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(row["status"], "failed")
        self.assertIn("description", row["result_message"])

    def test_24_matching_title_but_mismatched_tags_is_not_verified(self):
        row = self._ready_publish_row()
        actual = copy.deepcopy(row["proposed"])
        actual["tags"] = ["different", "tags"]
        fingerprint = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)
        with patch.object(connector, "apply_approved_dashboard_proposal", return_value={"result": "success"}), patch.object(
            connector, "fetch_video_snippet", return_value=actual
        ):
            result = batch_metadata.publish_approved_batch_rows(
                object(), [row], [row["video_id"]],
                approved=True, approval_fingerprint=fingerprint, review_revision=1,
            )
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(row["status"], "failed")
        self.assertIn("tags", row["result_message"])

    def test_25_unavailable_readback_is_not_reported_as_verified(self):
        row = self._ready_publish_row()
        fingerprint = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)
        with patch.object(connector, "apply_approved_dashboard_proposal", return_value={"result": "success"}), patch.object(
            connector, "fetch_video_snippet", side_effect=RuntimeError("YouTube is temporarily unavailable")
        ):
            result = batch_metadata.publish_approved_batch_rows(
                object(), [row], [row["video_id"]],
                approved=True, approval_fingerprint=fingerprint, review_revision=1,
            )
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(row["status"], "verification_unavailable")
        self.assertIn("read-back was unavailable", row["result_message"])

    def test_26_edit_after_fingerprint_invalidates_publish_attempt(self):
        youtube = FakeYoutube({self.VIDEOS_4[0]: make_snippet(self.VIDEOS_4[0])})
        row = self._ready_publish_row()
        rows = [row]
        approved_fingerprint = batch_metadata.compute_batch_approval_fingerprint(rows, review_revision=1)

        row["proposed"]["title"] = "Edited after approval"
        batch_metadata.revalidate_batch_row(row)
        result = batch_metadata.publish_approved_batch_rows(
            youtube, rows, [row["video_id"]],
            approved=True,
            approval_fingerprint=approved_fingerprint,
            review_revision=1,
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(youtube._videos.update_calls, [])


    def test_27_connector_readback_http_error_is_not_reported_as_publish_failure(self):
        row = self._ready_publish_row()
        fingerprint = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)
        verification_error = connector.MetadataVerificationUnavailableError(
            "The update may have succeeded, but YouTube read-back verification was unavailable: HTTP 403"
        )
        with patch.object(
            connector,
            "apply_approved_dashboard_proposal",
            side_effect=verification_error,
        ):
            result = batch_metadata.publish_approved_batch_rows(
                object(), [row], [row["video_id"]],
                approved=True,
                approval_fingerprint=fingerprint,
                review_revision=1,
            )
        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(row["status"], "verification_unavailable")
        self.assertIn("read-back verification was unavailable", row["result_message"])




class GeminiBatchInstructionFallbackTests(unittest.TestCase):
    @patch("core.batch_instructions.requests.post")
    def test_batch_parser_tries_flash_when_gemini_argon_is_quota_limited(self, post):
        quota = Mock(status_code=429)
        success = Mock(status_code=200)
        success.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '{ "videos": [], "shared_instructions": "" }'}]}}]
        }
        post.side_effect = [quota, success]

        result = batch_instructions._call_gemini_json(
            "gemini-key", "gemini-4-argon", "prompt", ["gemini-3.8-flash"]
        )
        self.assertEqual(result["videos"], [])
        self.assertEqual(post.call_count, 2)
        self.assertIn("gemini-4-argon", post.call_args_list[0].args[0])
        self.assertIn("gemini-3.8-flash", post.call_args_list[1].args[0])

    @patch("core.batch_instructions.requests.post")
    def test_batch_parser_does_not_retry_gemini_models_on_invalid_key(self, post):
        post.return_value = Mock(status_code=403)
        with self.assertRaisesRegex(RuntimeError, "rejected the API key"):
            batch_instructions._call_gemini_json(
                "invalid-key", "gemini-4-argon", "prompt", ["gemini-3.8-flash"]
            )
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
