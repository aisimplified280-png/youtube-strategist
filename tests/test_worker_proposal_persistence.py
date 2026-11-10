import unittest
from unittest.mock import patch

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



    def test_worker_preloads_snippets_once_for_a_25_video_batch(self):
        video_ids = [f"V{index:010d}" for index in range(25)]
        records = [{
            "id": f"row-{index}",
            "row_number": index + 1,
            "video_id": video_id,
            "status": "queued",
            "attempt_count": 0,
            "input_payload": {
                "video_id": video_id,
                "status": "parsed",
                "title": f"Updated title {index + 1}",
                "exact_title": f"Updated title {index + 1}",
                "title_intent": "exact",
                "description_intent": "unmentioned",
                "tags_intent": "unmentioned",
                "errors": [],
            },
        } for index, video_id in enumerate(video_ids)]

        class BatchStore:
            def __init__(self, rows):
                self.rows = rows
                self.job_updates = []

            def claim_next_job(self, worker_id, lease_seconds=600):
                return {"id": "job-25", "owner_key": "owner-1"}

            def _request(self, method, path, *, params=None, payload=None, prefer=None):
                if method == "GET" and path == "batch_job_rows":
                    return self.rows
                if method == "PATCH" and path == "batch_job_rows":
                    row_id = str((params or {}).get("id", "")).removeprefix("eq.")
                    for row in self.rows:
                        if str(row["id"]) == row_id:
                            row.update(payload or {})
                    return []
                if method == "PATCH" and path == "batch_jobs":
                    self.job_updates.append(payload or {})
                    return []
                return []

        store = BatchStore(records)
        cached_snippets = {video_id: {"title": f"Current {index}"} for index, video_id in enumerate(video_ids)}

        def prepared_batch(youtube, one_batch, **kwargs):
            source = one_batch["rows"][0]
            return {"rows": [{
                "status": "ready",
                "video_id": source["video_id"],
                "current_title": f"Current {source['video_id']}",
                "before": {"title": f"Current {source['video_id']}", "description": "", "tags": []},
                "proposed": {"title": source["exact_title"], "description": "", "tags": []},
                "proposed_title": source["exact_title"],
                "digest": f"digest-{source['video_id']}",
                "changed_fields": ["title"],
                "errors": [],
            }]}

        with (
            patch.object(worker, "_get_owner_credentials", return_value=object()),
            patch.object(worker, "build", return_value=object()),
            patch.object(worker, "build_batch_metadata_generator", return_value=None),
            patch.object(
                worker.batch_metadata,
                "fetch_owner_verified_snippets",
                return_value=(cached_snippets, {}),
            ) as fetch_snippets,
            patch.object(
                worker.batch_metadata,
                "prepare_metadata_batch",
                side_effect=prepared_batch,
            ) as prepare_batch,
            patch.object(
                worker.batch_metadata,
                "publish_approved_batch_rows",
                side_effect=lambda youtube, rows, selected_ids, **kwargs: (
                    rows[0].update(status="verified", result_message="Verified on YouTube")
                    or {"success_count": 1, "failure_count": 0}
                ),
            ) as publish_batch,
        ):
            self.assertTrue(worker.process_one_job(store, object(), "", ""))

        fetch_snippets.assert_called_once()
        self.assertEqual(len(fetch_snippets.call_args.args[1]), 25)
        self.assertEqual(prepare_batch.call_count, 25)
        self.assertEqual(publish_batch.call_count, 25)
        self.assertTrue(all(call.kwargs["approved"] is True for call in publish_batch.call_args_list))
        self.assertTrue(all(row["status"] == "verified" for row in store.rows))
        self.assertTrue(all(row["attempt_count"] == 1 for row in store.rows))
        self.assertEqual(store.job_updates[-1]["status"], "completed")
        self.assertEqual(store.job_updates[-1]["processed_count"], 25)
        self.assertEqual(store.job_updates[-1]["failed_count"], 0)

if __name__ == "__main__":
    unittest.main()
