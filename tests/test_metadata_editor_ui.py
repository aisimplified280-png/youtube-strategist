import os
import unittest

from ui.metadata_editor import _changed_parts, _metadata_growth_review, _parse_video_id


class MetadataEditorUITests(unittest.TestCase):
    def test_parse_video_id_accepts_id_watch_shorts_and_short_links(self):
        video_id = "abcdefghijk"
        self.assertEqual(_parse_video_id(video_id), video_id)
        self.assertEqual(
            _parse_video_id(f"https://www.youtube.com/watch?v={video_id}&feature=share"),
            video_id,
        )
        self.assertEqual(
            _parse_video_id(f"https://youtube.com/shorts/{video_id}"),
            video_id,
        )
        self.assertEqual(_parse_video_id(f"https://youtu.be/{video_id}"), video_id)

    def test_parse_video_id_rejects_untrusted_or_invalid_references(self):
        self.assertIsNone(_parse_video_id("not a video"))
        self.assertIsNone(_parse_video_id("https://example.com/watch?v=abcdefghijk"))
        self.assertIsNone(_parse_video_id("https://youtube.com/watch?v=too-short"))

    def test_metadata_growth_review_checks_youtube_limits_and_field_quality(self):
        resource = {
            "snippet": {
                "title": "A" * 105,
                "description": "A useful opening.\n\nMore detail.",
                "tags": ["ai", "chatgpt"],
            }
        }
        review = _metadata_growth_review(resource)
        self.assertEqual(review["title_length"], 105)
        self.assertTrue(any(level == "Fix" for level, _, _ in review["recommendations"]))
        self.assertTrue(any("opening" in heading.lower() for _, heading, _ in review["recommendations"]))

    def test_growth_review_uses_utf8_byte_and_quoted_tag_budgets(self):
        description = "తెలుగు" * 300
        tags = [f"topic {index}" for index in range(60)]
        review = _metadata_growth_review({
            "snippet": {
                "title": "Telugu metadata byte test",
                "description": description,
                "tags": tags,
            }
        })
        self.assertLess(review["description_length"], 5000)
        self.assertGreater(review["description_bytes"], 5000)
        self.assertGreater(review["tag_characters"], 500)
        self.assertGreater(review["tag_bytes"], 500)
        fixes = [heading.lower() for level, heading, _ in review["recommendations"] if level == "Fix"]
        self.assertTrue(any("utf-8 bytes" in heading or "characters /" in heading for heading in fixes))
        self.assertTrue(any("tags use" in heading for heading in fixes))

    def test_changed_parts_returns_only_modified_resource_parts(self):
        before = {
            "snippet": {"title": "Before"},
            "status": {"privacyStatus": "public"},
            "localizations": {"te": {"title": "పాత"}},
        }
        draft = {
            "snippet": {"title": "After"},
            "status": {"privacyStatus": "public"},
            "localizations": {"te": {"title": "పాత"}},
        }
        self.assertEqual(_changed_parts(before, draft), ["snippet"])

    def test_metadata_editor_routes_approval_to_central_review_queue(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "metadata_editor.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("central Review & Publishing queue", source)
        self.assertNotIn("_render_pending_review(", source)

    def test_empty_advanced_parts_do_not_create_unrelated_updates(self):
        before = {"snippet": {"title": "Title"}, "status": {"privacyStatus": "public"}}
        draft = {
            "snippet": {"title": "Title"},
            "status": {"privacyStatus": "public"},
            "brandPartner": {},
            "recordingDetails": {},
            "localizations": {},
        }
        self.assertEqual(_changed_parts(before, draft), [])


if __name__ == "__main__":
    unittest.main()
