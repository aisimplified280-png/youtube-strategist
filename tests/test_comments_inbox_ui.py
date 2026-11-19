import os
import unittest

from unittest.mock import patch

from ui.comments_inbox import _comment_pattern_summary, _generate_latest_unanswered_reply_drafts


class CommentsInboxUITests(unittest.TestCase):
    def test_comments_workspace_is_paginated_and_manual_approval_gated(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "comments_inbox.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('st.form("comments_search_form")', source)
        self.assertIn('max_results=25', source)
        self.assertIn('"Load more comments"', source)
        self.assertIn('"Load thread & generate reply suggestion"', source)
        self.assertIn('"Approve & Publish reply"', source)
        self.assertIn("claim_comment_reply_draft", source)
        self.assertIn("Show replies needing verification", source)
        self.assertNotIn("comments().insert(", source)

    def test_comment_pattern_summary_identifies_questions_unanswered_and_topics(self):
        summary = _comment_pattern_summary([
            {
                "text": "Is Gemini free? How do I get started?",
                "total_reply_count": 0,
            },
            {
                "text": "Great video, thanks for explaining AI agents!",
                "total_reply_count": 1,
            },
            {
                "text": "This tool is not working after install.",
                "total_reply_count": 0,
            },
        ])
        self.assertEqual(summary["sample_size"], 3)
        self.assertEqual(summary["questions"], 1)
        self.assertEqual(summary["unanswered"], 2)
        self.assertEqual(summary["positive"], 1)
        self.assertTrue(summary["patterns"])

    def test_comment_summary_is_visible_before_inbox_filters_and_safe_suggestions_are_available(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "comments_inbox.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Comment pattern summary", source)
        self.assertIn("_comment_pattern_summary", source)
        self.assertIn("Recent sample:", source)
        self.assertIn("Suggested reply (edit before saving)", source)
        self.assertIn("comment_snapshot = str(thread.get(\"text\") or \"\")[:5000]", source)
        self.assertIn('"channel_id": CHANNEL_ID', source)
        self.assertNotIn('"channel_id": "UCS6Sb5rfaEnWTVPGyQlnHFQ"', source)


    def test_latest_unanswered_bulk_action_stages_drafts_without_publishing(self):
        thread = {
            "thread_id": "thread-1", "top_level_comment_id": "comment-1", "video_id": None,
            "text": "Can you explain this?", "author_display_name": "Viewer",
            "can_reply": True, "owner_unanswered": True, "replies": [],
        }
        class Store:
            owner_key = "owner-key"
            def __init__(self): self.created = []
            def list_comment_reply_drafts(self, limit=100): return []
            def create_comment_reply_draft(self, values):
                self.created.append(values)
                return {"id": "draft-1"}
        store = Store()
        with patch(
            "ui.comments_inbox.list_latest_unanswered_comment_threads",
            return_value={"items": [thread], "scanned_count": 1, "unknown_reply_threads": 0},
        ), patch(
            "ui.comments_inbox.analyze_comment_thread",
            return_value={"suggested_reply": "Thanks for asking! Which part should I explain?", "reply_recommendation": "reply"},
        ):
            result = _generate_latest_unanswered_reply_drafts(object(), store, {}, limit=1)
        self.assertEqual(result["staged"], 1)
        self.assertEqual(store.created[0]["status"], "pending_review")
        self.assertEqual(store.created[0]["parent_comment_id"], "comment-1")
        self.assertEqual(len(store.created), 1)

    def test_comments_inbox_has_latest_unanswered_generation_control(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "comments_inbox.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("Generate replies for latest unanswered comments", source)
        self.assertIn("Only threads with no replies at all", source)
        self.assertIn("list_latest_unanswered_comment_threads", source)



    def test_thread_analysis_and_reply_suggestion_render_inside_open_comment_card(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "comments_inbox.py"), encoding="utf-8") as handle:
            source = handle.read()
        card_start = source.index('        for thread in visible:')
        card_end = source.index('    if page.get("next_page_token")', card_start)
        card = source[card_start:card_end]
        self.assertIn('_render_selected_thread(youtube, persistent_store, secrets, thread)', card)
        self.assertIn('"Load thread & generate reply suggestion"', source)
        self.assertNotIn('selected_thread = st.session_state.get("_selected_comment_thread")', source)
        self.assertNotIn('st.subheader("Selected comment thread")', source)

    def test_reply_management_is_separated_from_the_comment_feed(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "comments_inbox.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('options=["Inbox", "Reply drafts", "Needs verification"]', source)
        self.assertIn('if workspace_view == "Reply drafts":', source)
        self.assertIn('if workspace_view == "Needs verification":', source)
        self.assertNotIn('st.button("Show pending replies"', source)

    def test_comment_reply_publish_gate_is_in_backend_not_only_the_ui(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "core", "comments", "youtube_comments.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('draft.get("status") != "publishing"', source)
        self.assertIn('draft.get("approved_by") != owner_key', source)
        self.assertIn("compare_digest", source)
        self.assertIn("publish_comment_reply(", source)


if __name__ == "__main__":
    unittest.main()
