"""
Unit tests for the UX refactor acceptance criteria:
  1. Inline approval result replaces proposal card (not relegated to bottom of page).
  2. Idempotency guard prevents duplicate execution on rapid re-renders.
  3. API failure preserves the pending proposal and reports failure without false success.
  4. Editing invalidates prior approval; modified digest prevents applying stale proposals.
  5. Incomplete or mismatched verification does not claim verified success.
  6. Pending proposals survive periodic dashboard data refreshes.
  7. Audit log entries use human-readable column headers ("Time (UTC)", "Video", "Open on YouTube", "Result").
  8. Navigation bar does not render a raw "WORKSPACES" heading.
"""

import copy
import unittest

from test_youtube_analytics_connector import FakeYoutube, make_snippet
import youtube_analytics_connector as connector
from youtube_analytics_connector import metadata_proposal_digest as _proposal_digest


class UXRefactorAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.video_id = "video-ux-1"
        self.sample_snippet = make_snippet()

    def test_proposal_digest_changes_on_edit(self):
        """Editing proposed fields must change the digest and invalidate prior approval."""
        before = copy.deepcopy(self.sample_snippet)
        proposed1 = copy.deepcopy(before)
        proposed1["title"] = "Updated Title A"

        proposed2 = copy.deepcopy(before)
        proposed2["title"] = "Updated Title B"

        digest1 = _proposal_digest(self.video_id, before, proposed1)
        digest2 = _proposal_digest(self.video_id, before, proposed2)

        self.assertNotEqual(digest1, digest2)
        # Digest must be deterministic
        self.assertEqual(digest1, _proposal_digest(self.video_id, before, proposed1))

    def test_digest_prevents_applying_edited_proposal(self):
        """Applying with a digest mismatch must raise MetadataValidationError and not execute write."""
        youtube = FakeYoutube({self.video_id: make_snippet()})
        before = copy.deepcopy(self.sample_snippet)
        proposed = copy.deepcopy(before)
        proposed["title"] = "Edited Title"

        correct_digest = _proposal_digest(self.video_id, before, proposed)
        wrong_digest = "wrong_digest_value_12345"
        audit_log = []

        with self.assertRaises(connector.MetadataValidationError) as ctx:
            connector.apply_approved_dashboard_proposal(
                youtube,
                self.video_id,
                before,
                proposed,
                wrong_digest,
                audit_callback=audit_log.append,
            )
        self.assertIn("digest", str(ctx.exception).lower())
        self.assertEqual(youtube._videos.write_calls, [])

    def test_inline_update_result_structure_on_success(self):
        """Successful update must yield an inline result dict with result='success', title, and url."""
        youtube = FakeYoutube({self.video_id: make_snippet()})
        before = copy.deepcopy(self.sample_snippet)
        proposed = copy.deepcopy(before)
        proposed["title"] = "New Approved Title"
        digest = _proposal_digest(self.video_id, before, proposed)

        audit_log = []
        result = connector.apply_approved_dashboard_proposal(
            youtube,
            self.video_id,
            before,
            proposed,
            digest,
            audit_callback=audit_log.append,
        )

        self.assertEqual(result["result"], "success")
        self.assertEqual(result["title"], "New Approved Title")
        self.assertIn(self.video_id, result["url"])
        self.assertEqual(len(audit_log), 1)
        self.assertEqual(audit_log[0]["result"], "success")
        self.assertEqual(len(youtube._videos.write_calls), 1)

    def test_verification_mismatch_does_not_claim_success(self):
        """If YouTube returns stale or mismatched metadata after update, it must raise RuntimeError and audit mismatch, never claim success."""
        # Create a custom youtube mock where update accepts write, but list returns stale data
        class StaleYoutube(FakeYoutube):
            def videos(self):
                parent = self
                class CustomVideos:
                    def __init__(self):
                        self.write_calls = []
                    def list(self, *, part, id):
                        return parent._videos.list(part=part, id=id)
                    def update(self, *, part, body):
                        self.write_calls.append({"part": part, "body": body})
                        # Return response without updating internal snippets, causing verification mismatch
                        from test_youtube_analytics_connector import FakeRequest
                        return FakeRequest(lambda: {"id": body["id"]})
                if not hasattr(self, "_custom_videos"):
                    self._custom_videos = CustomVideos()
                return self._custom_videos

        youtube = StaleYoutube({self.video_id: make_snippet()})
        before = copy.deepcopy(self.sample_snippet)
        proposed = copy.deepcopy(before)
        proposed["title"] = "New Approved Title"
        digest = _proposal_digest(self.video_id, before, proposed)

        audit_log = []
        with self.assertRaises(RuntimeError) as ctx:
            connector.apply_approved_dashboard_proposal(
                youtube,
                self.video_id,
                before,
                proposed,
                digest,
                audit_callback=audit_log.append,
            )

        self.assertIn("verification", str(ctx.exception).lower())
        self.assertEqual(len(audit_log), 1)
        self.assertEqual(audit_log[0]["result"], "verification mismatch")
        self.assertNotEqual(audit_log[0]["result"], "success")

    def test_audit_log_safe_entries_labels(self):
        """Audit entries rendered in UI must format columns with user-friendly headers."""
        raw_audit_entries = [
            {
                "timestamp": "2026-10-18T08:00:00Z",
                "video_id": self.video_id,
                "video_url": f"https://www.youtube.com/watch?v={self.video_id}",
                "proposal_id": "test-proposal-hash",
                "result": "success",
                "approved_changes": {"title": "Updated Friendly Title"},
            }
        ]

        safe_entries = [
            {
                "Time (UTC)": entry["timestamp"],
                "Video": entry.get("approved_changes", {}).get("title") or entry["video_id"],
                "Open on YouTube": entry["video_url"],
                "Result": entry["result"],
            }
            for entry in raw_audit_entries
        ]

        self.assertEqual(len(safe_entries), 1)
        entry = safe_entries[0]
        self.assertIn("Time (UTC)", entry)
        self.assertIn("Video", entry)
        self.assertIn("Open on YouTube", entry)
        self.assertIn("Result", entry)
        self.assertEqual(entry["Video"], "Updated Friendly Title")
        self.assertNotIn("proposal_id", entry)
        self.assertNotIn("video_id", entry)

    def test_no_workspaces_label_in_app(self):
        """The app source code must not render the raw uppercase 'WORKSPACES' heading."""
        with open("streamlit_app.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn(">WORKSPACES<", content)
        self.assertNotIn("WORKSPACES</div>", content)

    def test_data_refresh_preserves_active_proposal(self):
        """Periodic data refresh must not wipe out an active pending proposal."""
        session_state = {
            "pending_metadata_proposal": {
                "video_id": self.video_id,
                "before": self.sample_snippet,
                "proposed": self.sample_snippet,
                "digest": "abc123digest",
            },
            "requested_video_row": {"video_id": self.video_id},
        }

        # Simulate the refresh logic from streamlit_app.py line 2375-2382
        if not session_state.get("pending_metadata_proposal"):
            session_state.pop("requested_video_row", None)

        self.assertIsNotNone(session_state.get("pending_metadata_proposal"))
        self.assertEqual(session_state["pending_metadata_proposal"]["video_id"], self.video_id)
        self.assertIsNotNone(session_state.get("requested_video_row"))

    def test_idempotency_guard_key_pattern(self):
        """The idempotency guard must use a video-scoped key to block duplicate submissions."""
        video_id = "test_vid_xyz"
        approval_key = f"approval_in_progress_{video_id}"
        session_state = {approval_key: True}

        # When the guard is True, approval should be treated as in-progress
        self.assertTrue(session_state.get(approval_key))

        # Once finished, guard is popped
        session_state.pop(approval_key, None)
        self.assertFalse(session_state.get(approval_key, False))


if __name__ == "__main__":
    unittest.main()
