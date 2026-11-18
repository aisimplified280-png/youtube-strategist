import unittest
from unittest.mock import Mock, patch

from core.persistent_store import SupabasePersistentStore


class PersistentStoreBatchQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = SupabasePersistentStore(
            "https://example.supabase.co",
            "service-role-test-key",
            "verified-owner-subject",
        )

    def _response(self, rows=None):
        response = Mock()
        rows = rows or []
        response.content = b"[]" if not rows else b"[{}]"
        response.json.return_value = rows
        return response

    def test_list_jobs_uses_compact_status_projection(self):
        with patch(
            "core.persistent_store.requests.request",
            return_value=self._response(),
        ) as request:
            self.store.list_jobs(limit=20)

        selected = request.call_args.kwargs["params"]["select"]
        self.assertIn("status", selected)
        self.assertIn("processed_count", selected)
        self.assertIn("conversation_id", selected)
        self.assertNotIn("input_payload", selected)
        self.assertNotIn("instruction_text", selected)

    def test_conversation_list_uses_recent_page_and_cursor(self):
        with patch(
            "core.persistent_store.requests.request",
            return_value=self._response(),
        ) as request:
            self.store.list_conversations()
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["limit"], "15")
        self.assertEqual(params["order"], "updated_at.desc")

        with patch(
            "core.persistent_store.requests.request",
            return_value=self._response(),
        ) as request:
            self.store.list_conversations(limit=15, before="2026-11-18T10:00:00Z")
        self.assertEqual(
            request.call_args.kwargs["params"]["updated_at"],
            "lt.2026-11-18T10:00:00Z",
        )

    def test_message_loader_fetches_newest_page_then_restores_chronology(self):
        rows = [
            {"id": "new", "created_at": "2026-11-18T10:02:00Z"},
            {"id": "old", "created_at": "2026-11-18T10:01:00Z"},
        ]
        with patch(
            "core.persistent_store.requests.request",
            return_value=self._response(rows),
        ) as request:
            messages = self.store.load_messages("conversation-1", limit=50)
        self.assertEqual([row["id"] for row in messages], ["old", "new"])
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["limit"], "50")
        self.assertEqual(params["order"], "created_at.desc")

    def test_get_job_rows_uses_only_review_fields(self):
        with patch(
            "core.persistent_store.requests.request",
            return_value=self._response(),
        ) as request:
            self.store.get_job_rows("job-123")

        selected = request.call_args.kwargs["params"]["select"]
        self.assertIn("proposed_metadata", selected)
        self.assertIn("approval_digest", selected)
        self.assertNotIn("input_payload", selected)
        self.assertNotIn("owner_key", selected)


if __name__ == "__main__":
    unittest.main()
