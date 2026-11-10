import unittest

import worker


class RecordingStore:
    def __init__(self):
        self.calls = []

    def _request(self, method, path, *, params=None, payload=None, prefer=None):
        self.calls.append({
            "method": method,
            "path": path,
            "params": params,
            "payload": payload,
            "prefer": prefer,
        })
        return []


class WorkerProposalPersistenceTests(unittest.TestCase):
    def test_persists_all_title_angles_and_recommendation_context(self):
        store = RecordingStore()
        row_record = {"id": "row-123"}
        row = {
            "status": "ready",
            "video_id": "AbCdEfGhIjK",
            "before": {"title": "Old title", "description": "Old description", "tags": ["old"]},
            "proposed": {"title": "Recommended title", "description": "New description #AI", "tags": ["ai"]},
            "digest": "approval-digest",
            "title_angles": [
                {"angle": "curiosity", "label": "Curiosity / Intrigue", "title": "Curious title"},
                {"angle": "value", "label": "Direct Value / Outcome", "title": "Value title"},
                {"angle": "search", "label": "Search / SEO", "title": "Search title"},
            ],
            "recommended_title": "Recommended title",
            "recommendation_reason": "It has the clearest outcome and strongest search intent.",
            "rationale": ["Specific", "Accurate"],
            "changed_fields": ["title", "description", "tags"],
        }

        worker._persist_row(store, "job-123", "owner-hash", row_record, row)

        self.assertEqual(len(store.calls), 1)
        payload = store.calls[0]["payload"]
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["proposed_metadata"], row["proposed"])
        self.assertEqual(payload["approval_digest"], "approval-digest")
        self.assertEqual(payload["proposal_details"]["title_angles"], row["title_angles"])
        self.assertEqual(payload["proposal_details"]["recommended_title"], "Recommended title")
        self.assertEqual(
            payload["proposal_details"]["recommendation_reason"],
            "It has the clearest outcome and strongest search intent.",
        )
        self.assertEqual(payload["proposal_details"]["rationale"], ["Specific", "Accurate"])
        self.assertEqual(payload["proposal_details"]["changed_fields"], ["title", "description", "tags"])


if __name__ == "__main__":
    unittest.main()
