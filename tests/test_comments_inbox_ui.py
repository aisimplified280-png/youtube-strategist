import os
import unittest

from ui.comments_inbox import _comment_pattern_summary


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
