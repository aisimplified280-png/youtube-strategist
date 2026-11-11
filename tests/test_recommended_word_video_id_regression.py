"""Regression tests for ambiguous lowercase prose in batch video ID extraction."""

import unittest

from core.batch_instructions import extract_video_references_from_text


class TestRecommendedWordVideoIdRegression(unittest.TestCase):
    def test_recommended_title_label_is_not_a_video_id(self):
        ids = ["RxoBXkiMjho", "kP1kZ7e4fn0", "ATsZs2iVBU8", "la3OCvyplHk"]
        text = """Optimize the title, description, and tags for these videos.

Video ID: RxoBXkiMjho
Recommended title: 4 AI Agents, 4 Different Jobs — Which One Actually Works?

Video ID: kP1kZ7e4fn0
Recommended title: Why AI Models Miss Rare Cases

Video ID: ATsZs2iVBU8
Recommended title: Don't Just Let AI Code

Video ID: la3OCvyplHk
Recommended title: Can You Still Code Without AI?
"""
        found = [item["video_id"] for item in extract_video_references_from_text(text)]
        self.assertEqual(found, ids)
        self.assertNotIn("recommended", found)

    def test_explicitly_labelled_lowercase_video_id_remains_supported(self):
        text = "Video ID: abcdefghijk\nRecommended title: Example"
        found = [item["video_id"] for item in extract_video_references_from_text(text)]
        self.assertEqual(found, ["abcdefghijk"])


if __name__ == "__main__":
    unittest.main()
