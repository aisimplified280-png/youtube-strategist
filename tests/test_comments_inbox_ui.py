import os
import unittest
from unittest.mock import Mock, patch


from ui.comments_inbox import _comment_pattern_summary, _run_comment_automation


class CommentsInboxUITests(unittest.TestCase):
    def test_comments_workspace_is_paginated_and_manual_approval_gated(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "ui", "comments_inbox.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('st.form("comments_search_form")', source)
        self.assertIn('max_results=25', source)
        self.assertIn('"Load more comments"', source)
        self.assertIn('"Analyze comment & draft a reply"', source)
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

    def test_shadow_comment_automation_analyzes_without_saving_or_publishing(self):
        store = Mock()
        store.list_comment_reply_drafts.return_value = []
        item = {
            "thread_id": "thread-1",
            "top_level_comment_id": "comment-1",
            "text": "Great video!",
            "author_display_name": "Viewer",
            "can_reply": True,
            "total_reply_count": 0,
            "moderation_status": "published",
        }
        analysis = {
            "category": "praise",
            "sentiment": "positive",
            "confidence": 0.95,
            "reply_recommendation": "reply",
            "needs_fact_check": False,
            "suggested_reply": "Thanks for watching! Glad you found it helpful.",
        }
        with patch("ui.comments_inbox.analyze_comment_thread", return_value=analysis), patch(
            "ui.comments_inbox.publish_approved_comment_reply"
        ) as publish:
            result = _run_comment_automation(
                object(), store, {}, [item],
                {"comments_mode": "shadow", "emergency_pause": False},
            )
        self.assertEqual(result["analyzed"], 1)
        self.assertEqual(result["published"], 0)
        store.create_comment_reply_draft.assert_not_called()
        publish.assert_not_called()

    def test_review_comment_automation_saves_draft_but_never_publishes(self):
        store = Mock()
        store.list_comment_reply_drafts.return_value = []
        store.create_comment_reply_draft.return_value = {"id": "draft-1"}
        item = {
            "thread_id": "thread-1",
            "top_level_comment_id": "comment-1",
            "text": "Great video!",
            "author_display_name": "Viewer",
            "can_reply": True,
            "total_reply_count": 0,
            "moderation_status": "published",
        }
        analysis = {
            "category": "question",
            "sentiment": "neutral",
            "confidence": 0.97,
            "reply_recommendation": "reply",
            "needs_fact_check": False,
            "suggested_reply": "Which part would you like me to explain first?",
        }
        with patch("ui.comments_inbox.analyze_comment_thread", return_value=analysis), patch(
            "ui.comments_inbox.publish_approved_comment_reply"
        ) as publish:
            result = _run_comment_automation(
                object(), store, {}, [item],
                {"comments_mode": "review", "emergency_pause": False},
            )
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["published"], 0)
        store.create_comment_reply_draft.assert_called_once()
        publish.assert_not_called()

    def test_autonomous_comment_mode_replies_only_to_high_confidence_praise(self):
        store = Mock()
        store.list_comment_reply_drafts.return_value = []
        store.create_comment_reply_draft.return_value = {"id": "draft-1"}
        store.update_comment_reply_draft.return_value = {"id": "draft-1"}
        store.claim_comment_reply_draft.return_value = {
            "id": "draft-1",
            "status": "publishing",
            "approved_by": "automation:comments",
        }
        praise = {
            "thread_id": "thread-1",
            "top_level_comment_id": "comment-1",
            "text": "Great video!",
            "author_display_name": "Viewer",
            "can_reply": True,
            "total_reply_count": 0,
            "moderation_status": "published",
        }
        question = {
            **praise,
            "thread_id": "thread-2",
            "top_level_comment_id": "comment-2",
            "text": "Is this tool free?",
        }
        praise_analysis = {
            "category": "praise",
            "sentiment": "positive",
            "confidence": 0.97,
            "reply_recommendation": "reply",
            "needs_fact_check": False,
            "suggested_reply": "Thanks for watching! Glad you found it helpful.",
        }
        question_analysis = {
            "category": "question",
            "sentiment": "neutral",
            "confidence": 0.99,
            "reply_recommendation": "verify_first",
            "needs_fact_check": True,
            "suggested_reply": "Thanks for asking! I don't want to guess.",
        }
        with patch(
            "ui.comments_inbox.analyze_comment_thread",
            side_effect=[praise_analysis, question_analysis],
        ), patch(
            "ui.comments_inbox.publish_approved_comment_reply",
            return_value={"status": "published", "comment_id": "reply-1"},
        ) as publish:
            result = _run_comment_automation(
                object(), store, {}, [praise, question],
                {
                    "comments_mode": "autonomous",
                    "comments_daily_cap": 3,
                    "minimum_confidence": 0.92,
                    "emergency_pause": False,
                },
            )
        self.assertEqual(result["published"], 1)
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(publish.call_count, 1)
        self.assertTrue(publish.call_args.kwargs["allow_automation"])

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
