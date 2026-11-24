from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import worker


class DiscordApprovedMetadataWorkerTests(unittest.TestCase):
    def test_only_approved_rows_are_claimed_and_published(self):
        store = MagicMock()
        store._request.side_effect = [
            [{
                "id": "audit-1",
                "owner_key": "owner-hash",
                "video_id": "abcdefghijk",
                "video_title": "Test video",
                "before_metadata": {"snippet": {"title": "Before", "categoryId": "27"}},
                "proposed_metadata": {"snippet": {"title": "After", "categoryId": "27"}},
                "approval_digest": "digest",
            }]
        ]
        store.claim_metadata_editor_audit.return_value = {"id": "audit-1"}
        store.update_metadata_editor_audit.return_value = {"id": "audit-1"}
        youtube = MagicMock()
        publish_result = {"result": "success", "resource": {"snippet": {"title": "After"}}}

        with patch.object(worker, "_get_owner_credentials", return_value=object()), \
             patch.object(worker, "build", return_value=youtube), \
             patch.object(worker, "apply_approved_resource_proposal", return_value=publish_result) as apply:
            count = worker.process_approved_metadata_editor_audits(store, object(), limit=5)

        self.assertEqual(count, 1)
        self.assertEqual(store._request.call_args.kwargs["params"]["result->>source"], "eq.discord")
        store.claim_metadata_editor_audit.assert_called_once_with("audit-1")
        apply.assert_called_once()
        self.assertEqual(store.update_metadata_editor_audit.call_args.args[1]["status"], "approved_published")

    def test_unclaimable_row_is_skipped(self):
        store = MagicMock()
        store._request.return_value = [{
            "id": "audit-2", "owner_key": "owner-hash", "video_id": "abcdefghijk",
            "before_metadata": {}, "proposed_metadata": {}, "approval_digest": "digest",
        }]
        store.claim_metadata_editor_audit.return_value = None
        count = worker.process_approved_metadata_editor_audits(store, object(), limit=5)
        self.assertEqual(count, 0)
        store.update_metadata_editor_audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
