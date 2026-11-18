import os
import unittest


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
