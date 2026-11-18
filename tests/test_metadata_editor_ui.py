import os
import unittest

from ui.metadata_editor import _changed_parts, _parse_video_id


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
