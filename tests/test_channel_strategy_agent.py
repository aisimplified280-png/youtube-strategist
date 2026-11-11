"""Safety tests for the scheduled channel strategist."""

import unittest

from core.channel_strategy_agent import classify_metadata_change


class ChannelStrategyClassificationTests(unittest.TestCase):
    def test_title_case_punctuation_and_whitespace_is_minor(self):
        classification, _, changed = classify_metadata_change(
            {"title": "Build AI Agents in Python!", "description": "Same", "tags": []},
            {"title": "BUILD AI AGENTS IN PYTHON", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "minor")
        self.assertEqual(changed, ["title"])

    def test_title_word_rewrite_is_major(self):
        classification, _, changed = classify_metadata_change(
            {"title": "Build AI Agents in Python", "description": "Same", "tags": []},
            {"title": "Create Better AI Agents with Python", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")
        self.assertEqual(changed, ["title"])

    def test_emoji_change_is_major_even_when_words_match(self):
        classification, _, _ = classify_metadata_change(
            {"title": "Build AI Agents 🚀", "description": "Same", "tags": []},
            {"title": "Build AI Agents 🔥", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")

    def test_number_change_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "5 AI tools for students", "description": "Same", "tags": []},
            {"title": "7 AI tools for students", "description": "Same", "tags": []},
        )
        self.assertEqual(classification, "major")

    def test_two_source_grounded_added_tags_are_minor(self):
        classification, _, changed = classify_metadata_change(
            {"title": "AI tools", "description": "Learn Gemini and Python workflows", "tags": ["AI"]},
            {"title": "AI tools", "description": "Learn Gemini and Python workflows", "tags": ["AI", "Gemini", "Python"]},
            source_text="AI tools Learn Gemini and Python workflows",
        )
        self.assertEqual(classification, "minor")
        self.assertEqual(changed, ["tags"])

    def test_unsupported_tag_addition_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI"]},
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI", "Guaranteed income"]},
            source_text="AI tools Learn Gemini",
        )
        self.assertEqual(classification, "major")

    def test_tag_removal_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI", "Gemini"]},
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI"]},
            source_text="AI tools Learn Gemini",
        )
        self.assertEqual(classification, "major")

    def test_combined_field_change_is_major(self):
        classification, _, _ = classify_metadata_change(
            {"title": "AI tools!", "description": "Learn Gemini", "tags": ["AI"]},
            {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI", "Gemini"]},
            source_text="AI tools Learn Gemini",
        )
        self.assertEqual(classification, "major")

    def test_identical_metadata_is_no_change(self):
        current = {"title": "AI tools", "description": "Learn Gemini", "tags": ["AI"]}
        self.assertEqual(classify_metadata_change(current, current)[0], "none")


if __name__ == "__main__":
    unittest.main()
