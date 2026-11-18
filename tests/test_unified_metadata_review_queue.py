import copy
import os
import unittest
from unittest.mock import patch

import youtube_analytics_connector as connector
from ui.strategy_activity import _publish_metadata_editor_audit


class FakeStore:
    owner_key = "hashed-owner"

    def __init__(self):
        self.updates = []
        self.claimed = []

    def update_metadata_editor_audit(self, audit_id, values):
        self.updates.append((audit_id, values))
        return {"id": audit_id, **values}

    def claim_metadata_editor_audit(self, audit_id):
        self.claimed.append(audit_id)
        return {"id": audit_id, "status": "publishing"}


def metadata_fixture():
    before = {
        "snippet": {
            "title": "Before title",
            "description": "Description",
            "tags": ["ai"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": "public"},
    }
    proposed = copy.deepcopy(before)
    proposed["snippet"]["title"] = "After title"
    digest = connector.metadata_proposal_digest("abcdefghijk", before, proposed)
    audit = {
        "id": "audit-1",
        "video_id": "abcdefghijk",
        "video_title": "After title",
        "status": "pending_review",
        "before_metadata": before,
        "proposed_metadata": proposed,
        "approval_digest": digest,
    }
    return before, proposed, audit


class UnifiedMetadataReviewQueueTests(unittest.TestCase):
    def test_metadata_editor_proposal_uses_manual_approval_claim_and_verification(self):
        _, proposed, audit = metadata_fixture()
        store = FakeStore()
        with patch(
            "ui.strategy_activity.apply_approved_resource_proposal",
            return_value={"result": "success", "resource": proposed, "updated_parts": ["snippet"]},
        ) as publish:
            success, message = _publish_metadata_editor_audit(store, object(), audit)

        self.assertTrue(success)
        self.assertIn("verified", message)
        self.assertEqual(store.claimed, ["audit-1"])
        self.assertEqual(publish.call_count, 1)
        statuses = [values.get("status") for _, values in store.updates]
        self.assertEqual(statuses, ["approved", "approved_published"])

    def test_metadata_editor_proposal_with_wrong_digest_is_marked_stale(self):
        _, _, audit = metadata_fixture()
        audit["approval_digest"] = "incorrect"
        store = FakeStore()
        with patch("ui.strategy_activity.apply_approved_resource_proposal") as publish:
            success, message = _publish_metadata_editor_audit(store, object(), audit)
        self.assertFalse(success)
        self.assertIn("fresh draft", message)
        self.assertEqual(store.updates[-1][1]["status"], "stale")
        publish.assert_not_called()

    def test_review_queue_contains_both_proposal_sources_and_shared_bulk_action(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "strategy_activity.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("list_metadata_editor_audits", source)
        self.assertIn("_publish_metadata_editor_audit", source)
        self.assertIn("Publish all", source)
        self.assertIn("claim_metadata_editor_audit", source)

    def test_metadata_editor_routes_drafts_to_central_review_queue(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "metadata_editor.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Your draft is saved in the central Review & Publishing queue", source)
        self.assertIn('st.session_state["strategy_active_section"] = "Review queue"', source)


if __name__ == "__main__":
    unittest.main()
