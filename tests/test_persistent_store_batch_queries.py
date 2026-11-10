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

    def _response(self):
        response = Mock()
        response.content = b"[]"
        response.json.return_value = []
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
