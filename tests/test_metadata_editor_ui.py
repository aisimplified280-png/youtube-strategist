import os
import unittest
from unittest.mock import Mock

from ui.metadata_editor import _changed_parts, _parse_video_id, _run_metadata_automation


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

    def test_metadata_editor_shows_insights_and_per_workspace_automation_controls(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "metadata_editor.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Metadata health & channel-growth insights", source)
        self.assertIn("Run metadata automation for this video", source)
        self.assertIn("metadata_mode", source)
        self.assertIn("seven-day per-video cooldown", source)

    def test_shadow_metadata_automation_never_writes(self):
        store = Mock()
        message, audit_id = _run_metadata_automation(
            object(), store, "abcdefghijk",
            {"snippet": {"title": "Before"}},
            {"snippet": {"title": "Before"}},
            None,
            {"recommendations": [{"field": "title", "confidence": 0.99, "suggested_value": "After"}]},
            {"metadata_mode": "shadow", "emergency_pause": False},
        )
        self.assertIn("Shadow run", message)
        self.assertIsNone(audit_id)
        store.create_metadata_editor_audit.assert_not_called()

    def test_review_metadata_automation_stages_high_confidence_text_fields(self):
        class Store:
            def create_metadata_editor_audit(self, values):
                self.values = values
                return {"id": "audit-1"}

        store = Store()
        before = {
            "snippet": {
                "title": "Before title",
                "description": "Description",
                "tags": ["ai"],
                "categoryId": "28",
            }
        }
        draft = dict(before)
        message, audit_id = _run_metadata_automation(
            None, store, "abcdefghijk", before, draft, None,
            {"recommendations": [{
                "field": "title",
                "confidence": 0.99,
                "suggested_value": "After title",
            }]},
            {"metadata_mode": "review", "minimum_confidence": 0.92, "emergency_pause": False},
        )
        self.assertIn("saved in Review", message)
        self.assertEqual(audit_id, "audit-1")
        self.assertEqual(store.values["proposed_metadata"]["snippet"]["title"], "After title")

    def test_autonomous_metadata_publishes_only_after_confidence_and_caps_pass(self):
        from unittest.mock import patch

        import youtube_analytics_connector as connector

        class FakeStore:
            owner_key = "owner-hash"

            def __init__(self):
                self.audit = None
                self.updates = []

            def list_metadata_editor_audits(self, **kwargs):
                return []

            def create_metadata_editor_audit(self, values):
                self.audit = {"id": "audit-1", **values}
                return self.audit

            def get_metadata_editor_audit(self, audit_id):
                return self.audit

            def update_metadata_editor_audit(self, audit_id, values):
                self.audit.update(values)
                self.updates.append(dict(values))
                return dict(self.audit)

            def claim_metadata_editor_audit(self, audit_id):
                self.audit["status"] = "publishing"
                return dict(self.audit)

        store = FakeStore()
        before = {
            "snippet": {
                "title": "Before title",
                "description": "Description",
                "tags": ["ai"],
                "categoryId": "28",
            }
        }
        draft = {"snippet": dict(before["snippet"])}
        insight = {"recommendations": [{
            "field": "title",
            "confidence": 0.99,
            "suggested_value": "After title",
        }]}
        with patch(
            "ui.metadata_editor.apply_approved_resource_proposal",
            return_value={"result": "success", "resource": {
                "snippet": {**before["snippet"], "title": "After title"}
            }},
        ) as publish:
            message, audit_id = _run_metadata_automation(
                object(), store, "abcdefghijk", before, draft, None, insight,
                {
                    "metadata_mode": "autonomous",
                    "metadata_daily_cap": 1,
                    "minimum_confidence": 0.92,
                    "emergency_pause": False,
                },
            )
        self.assertEqual(audit_id, "audit-1")
        self.assertIn("published and verified", message)
        self.assertEqual(store.audit["status"], "approved_published")
        self.assertEqual(store.audit["approved_by"], "automation:metadata")
        publish.assert_called_once()

    def test_autonomous_metadata_daily_attempt_cap_blocks_before_staging(self):
        store = Mock()
        store.list_metadata_editor_audits.side_effect = [
            [{
                "status": "verification_unavailable",
                "result": {"automation_mode": "autonomous"},
            }]
        ]
        before = {
            "snippet": {
                "title": "Before title",
                "description": "Description",
                "tags": ["ai"],
                "categoryId": "28",
            }
        }
        message, audit_id = _run_metadata_automation(
            object(), store, "abcdefghijk", before, {"snippet": dict(before["snippet"])},
            None,
            {"recommendations": [{
                "field": "title", "confidence": 0.99, "suggested_value": "After title"
            }]},
            {
                "metadata_mode": "autonomous",
                "metadata_daily_cap": 1,
                "minimum_confidence": 0.92,
                "emergency_pause": False,
            },
        )
        self.assertIn("attempt cap reached", message)
        self.assertIsNone(audit_id)
        store.create_metadata_editor_audit.assert_not_called()

    def test_uncertain_previous_metadata_outcome_blocks_another_auto_attempt(self):
        store = Mock()
        store.list_metadata_editor_audits.side_effect = [
            [],
            [{"status": "verification_unavailable", "video_id": "abcdefghijk"}],
        ]
        before = {
            "snippet": {
                "title": "Before title",
                "description": "Description",
                "tags": ["ai"],
                "categoryId": "28",
            }
        }
        message, audit_id = _run_metadata_automation(
            object(), store, "abcdefghijk", before, {"snippet": dict(before["snippet"])},
            None,
            {"recommendations": [{
                "field": "title", "confidence": 0.99, "suggested_value": "After title"
            }]},
            {
                "metadata_mode": "autonomous",
                "metadata_daily_cap": 2,
                "minimum_confidence": 0.92,
                "emergency_pause": False,
            },
        )
        self.assertIn("uncertain outcome", message)
        self.assertIsNone(audit_id)
        store.create_metadata_editor_audit.assert_not_called()

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
