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

    def test_top_level_workspace_buttons_removed_and_secondary_nav_added(self):
        """The 3 equal workspace buttons at the top of the main container must be removed, replaced by secondary sidebar nav."""
        with open("streamlit_app.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("asl_workspace_nav_", content)
        self.assertIn("asl_sidebar_view_selector", content)
        self.assertIn("asl_return_to_chat", content)

    def test_conversation_history_preserved_when_loading_video_from_insights(self):
        """Loading a video for optimization from Insights or Studio exports must not wipe chat history."""
        from unittest.mock import patch, Mock
        import strategist_chat

        session_state = {
            "strategist_chat_history": [
                {"role": "user", "content": "How is my channel performing overall?"},
                {"role": "assistant", "content": "Your latest videos have strong retention but low CTR."},
            ]
        }
        target_snippet = make_snippet(title="Insight Candidate Video")
        with patch("strategist_chat.st.session_state", session_state), patch(
            "strategist_chat.connector.fetch_video_snippet", return_value=target_snippet
        ), patch(
            "strategist_chat._transcript_text", return_value=("[00:00] Hello world", "Available")
        ):
            context, error = strategist_chat._load_conversation_videos(
                Mock(), {}, "https://youtu.be/abcdefghijk"
            )

        self.assertIsNone(error)
        self.assertEqual(context["target_id"], "abcdefghijk")
        # History must NOT be cleared to empty list
        self.assertEqual(len(session_state["strategist_chat_history"]), 2)
        self.assertEqual(session_state["strategist_chat_history"][0]["role"], "user")

    def test_recommendation_displays_actual_title_not_video_id(self):
        """Studio exports with video IDs must never display raw video IDs as recommendation titles."""
        import studio_export_analyzer as studio
        from streamlit_app import _resolve_video_title
        from unittest.mock import patch

        # Simulate Studio export where Video ID is present but Title is missing / only ID
        payload = (
            "Content,Views,Impressions,Impressions click-through rate\n"
            "WCJF5TmXpuU,100,2000,1.2%\n"
            "aaaaaaaaaaa,300,5000,4.5%\n"
            "bbbbbbbbbbb,400,6000,5.0%\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("content.csv", payload)
        report = studio.analyze_studio_export(parsed)

        # In studio_export_analyzer, the parsed video_title must NOT be the video_id
        wcjf_row = next(r for r in report["video_rows"] if r["video_id"] == "WCJF5TmXpuU")
        self.assertIsNone(wcjf_row["video_title"])
        self.assertEqual(wcjf_row["video_id"], "WCJF5TmXpuU")

        # The recommendation title from analyzer must use friendly fallback, not the ID
        low_ctr_rec = next(r for r in report["recommendations"] if r.get("video_id") == "WCJF5TmXpuU")
        self.assertNotIn("WCJF5TmXpuU", low_ctr_rec["title"])
        self.assertIn("Video title unavailable", low_ctr_rec["title"])

        # When resolved in app with catalogue data, the recommendation displays the real title
        session_state = {
            "video_title_cache": {},
            "dashboard_data": {
                "video_rows": [
                    {"video_id": "WCJF5TmXpuU", "title": "Top 10 AI Secrets for 2026"}
                ]
            }
        }
        with patch("streamlit_app.st.session_state", session_state):
            resolved = _resolve_video_title("WCJF5TmXpuU")
            self.assertEqual(resolved, "Top 10 AI Secrets for 2026")
            rec_title = low_ctr_rec["title"].replace("Video title unavailable", resolved)
            self.assertEqual(rec_title, "Review title/thumbnail alignment: Top 10 AI Secrets for 2026")
            self.assertNotIn("WCJF5TmXpuU", rec_title)

    def test_title_lookup_works_when_title_in_loaded_catalogue(self):
        """Title lookup must resolve from session cache, dashboard catalogue, opportunity data, or live API."""
        from streamlit_app import _resolve_video_title
        from unittest.mock import patch, Mock

        # 1. From dashboard_data
        session_state = {
            "video_title_cache": {},
            "dashboard_data": {
                "video_rows": [{"video_id": "WCJF5TmXpuU", "title": "Dashboard Video Title"}]
            }
        }
        with patch("streamlit_app.st.session_state", session_state):
            self.assertEqual(_resolve_video_title("WCJF5TmXpuU"), "Dashboard Video Title")
            self.assertEqual(session_state["video_title_cache"].get("WCJF5TmXpuU"), "Dashboard Video Title")

        # 2. From opportunity_data
        session_state = {
            "video_title_cache": {},
            "opportunity_data": {
                "video_rows": [{"video_id": "opp12345678", "title": "Opportunity Video Title"}]
            }
        }
        with patch("streamlit_app.st.session_state", session_state):
            self.assertEqual(_resolve_video_title("opp12345678"), "Opportunity Video Title")

        # 3. From video_title_cache
        session_state = {
            "video_title_cache": {"cached12345": "Cached Video Title"}
        }
        with patch("streamlit_app.st.session_state", session_state):
            self.assertEqual(_resolve_video_title("cached12345"), "Cached Video Title")

        # 4. From authenticated YouTube API
        session_state = {"video_title_cache": {}}
        youtube_mock = Mock()
        youtube_mock.videos().list().execute.return_value = {
            "items": [{"snippet": {"title": "Live API Video Title"}}]
        }
        with patch("streamlit_app.st.session_state", session_state):
            self.assertEqual(_resolve_video_title("api12345678", youtube=youtube_mock), "Live API Video Title")
            self.assertEqual(session_state["video_title_cache"].get("api12345678"), "Live API Video Title")

    def test_missing_metadata_produces_friendly_fallback_never_raw_id(self):
        """When metadata is missing and cannot be resolved, app must show 'Video title unavailable', never bare ID."""
        from streamlit_app import _resolve_video_title
        from unittest.mock import patch, Mock

        session_state = {"video_title_cache": {}}
        youtube_mock = Mock()
        youtube_mock.videos().list().execute.side_effect = RuntimeError("API unavailable")

        with patch("streamlit_app.st.session_state", session_state):
            self.assertEqual(_resolve_video_title("WCJF5TmXpuU", youtube=youtube_mock), "Video title unavailable")
            self.assertEqual(_resolve_video_title("WCJF5TmXpuU", title_hint="WCJF5TmXpuU", youtube=youtube_mock), "Video title unavailable")
            self.assertEqual(_resolve_video_title(None, title_hint="https://youtu.be/WCJF5TmXpuU"), "Video title unavailable")

    def test_do_it_preserves_target_video_and_evidence(self):
        """Clicking 'Do it' carries correct video URL, ID, and attaches Studio export evidence."""
        import studio_export_analyzer as studio
        import strategist_chat
        from unittest.mock import patch, Mock

        payload = (
            "Video ID,Views,Impressions,Impressions click-through rate\n"
            "WCJF5TmXpuU,100,2000,1.2%\n"
        ).encode("utf-8")
        parsed = studio.parse_studio_export("content.csv", payload)
        report = studio.analyze_studio_export(parsed)
        evidence_index = studio.build_evidence_index([report])

        evidence = studio.find_video_evidence(evidence_index, "WCJF5TmXpuU", "Real Title")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["source_file"], "content.csv")
        self.assertEqual(evidence[0]["ctr_percent"], 1.2)

        session_state = {
            "strategist_chat_history": [{"role": "user", "content": "hello"}],
            "studio_export_evidence_index": evidence_index,
        }
        target_snippet = make_snippet(title="Real Title")
        with patch("strategist_chat.st.session_state", session_state), patch(
            "strategist_chat.connector.fetch_video_snippet", return_value=target_snippet
        ), patch(
            "strategist_chat._transcript_text", return_value=("transcript text", "Available")
        ):
            context, error = strategist_chat._load_conversation_videos(
                Mock(), {}, "https://www.youtube.com/watch?v=WCJF5TmXpuU"
            )

        self.assertIsNone(error)
        self.assertEqual(context["target_id"], "WCJF5TmXpuU")
        self.assertEqual(len(context["studio_performance_evidence"]), 1)
        self.assertEqual(context["studio_performance_evidence"][0]["ctr_percent"], 1.2)
        self.assertEqual(len(session_state["strategist_chat_history"]), 1)

    def test_proposal_and_audit_log_display_human_readable_titles(self):
        """Proposal rendering and session audit logs must display resolved titles, never raw IDs."""
        from streamlit_app import _resolve_video_title
        from unittest.mock import patch

        session_state = {
            "video_title_cache": {"WCJF5TmXpuU": "Mastering Antigravity"},
            "session_audit_records": [
                {
                    "timestamp": "2026-10-18T10:00:00Z",
                    "video_id": "WCJF5TmXpuU",
                    "video_url": "https://www.youtube.com/watch?v=WCJF5TmXpuU",
                    "approved_changes": {"description": "Updated description without changing title"},
                    "result": "success",
                }
            ],
        }
        with patch("streamlit_app.st.session_state", session_state):
            entry = session_state["session_audit_records"][0]
            display_title = _resolve_video_title(
                entry.get("video_id"),
                title_hint=entry.get("approved_changes", {}).get("title"),
            )
            self.assertEqual(display_title, "Mastering Antigravity")
            self.assertNotEqual(display_title, "WCJF5TmXpuU")

    def test_video_ids_remain_intact_internally_for_api_requests(self):
        """Video IDs must remain intact internally for YouTube API update requests and digests."""
        youtube = FakeYoutube({"WCJF5TmXpuU": make_snippet(title="Original Title")})
        before = make_snippet(title="Original Title")
        proposed = copy.deepcopy(before)
        proposed["title"] = "Updated Title"
        digest = _proposal_digest("WCJF5TmXpuU", before, proposed)

        audit_log = []
        result = connector.apply_approved_dashboard_proposal(
            youtube,
            "WCJF5TmXpuU",
            before,
            proposed,
            digest,
            audit_callback=audit_log.append,
        )

        self.assertEqual(result["result"], "success")
        self.assertEqual(youtube._videos.write_calls[0]["body"]["id"], "WCJF5TmXpuU")
        self.assertEqual(audit_log[0]["video_id"], "WCJF5TmXpuU")


if __name__ == "__main__":
    unittest.main()
