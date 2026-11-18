import json
import unittest
from unittest.mock import Mock, patch

from core.llm import LLMError
from core.comments.analyzer import (
    CommentAnalysisError,
    _parse_analysis,
    analyze_comment_thread,
)


def valid_analysis(**overrides):
    value = {
        "category": "question",
        "sentiment": "neutral",
        "confidence": 0.92,
        "reason": "The viewer asks a direct question.",
        "reply_recommendation": "reply",
        "needs_fact_check": False,
        "suggested_reply": "Yes, here is how to get started.",
    }
    value.update(overrides)
    return value


class CommentAnalyzerTests(unittest.TestCase):
    def test_valid_analysis_is_normalized(self):
        result = _parse_analysis(json.dumps(valid_analysis()))
        self.assertEqual(result["category"], "question")
        self.assertEqual(result["reply_recommendation"], "reply")
        self.assertAlmostEqual(result["confidence"], 0.92)

    def test_spam_and_abuse_never_receive_a_reply_draft(self):
        result = _parse_analysis(json.dumps(valid_analysis(
            category="spam",
            suggested_reply="A reply that should be removed.",
        )))
        self.assertEqual(result["reply_recommendation"], "ignore")
        self.assertEqual(result["suggested_reply"], "")

    def test_uncertain_current_facts_require_verification(self):
        result = _parse_analysis(json.dumps(valid_analysis(
            needs_fact_check=True,
            suggested_reply="It is free forever.",
        )))
        self.assertEqual(result["reply_recommendation"], "verify_first")
        self.assertEqual(result["suggested_reply"], "")

    def test_low_confidence_analysis_never_proposes_a_publishable_reply(self):
        result = _parse_analysis(json.dumps(valid_analysis(
            confidence=0.5,
            suggested_reply="A reply that should not be sent.",
        )))
        self.assertEqual(result["reply_recommendation"], "verify_first")
        self.assertEqual(result["suggested_reply"], "")

    def test_invalid_category_confidence_and_long_reply_are_rejected(self):
        self.assertIsNone(_parse_analysis(json.dumps(valid_analysis(category="insult"))))
        self.assertIsNone(_parse_analysis(json.dumps(valid_analysis(confidence=1.5))))
        self.assertIsNone(_parse_analysis(json.dumps(valid_analysis(suggested_reply="word " * 36))))
        self.assertIsNone(_parse_analysis("not json"))

    def test_groq_is_primary_and_gemini_is_fallback(self):
        router = Mock()
        router.groq_key = "groq-key"
        router.groq_model = "groq-model"
        router.groq_fallbacks = []
        router.gemini_key = "gemini-key"
        router.gemini_model = "gemini-model"
        router.gemini_fallbacks = []
        with patch("core.comments.analyzer.LLMRouter", return_value=router), patch(
            "core.comments.analyzer.call_groq_api",
            side_effect=LLMError("rate limited", provider="groq", recoverable=True),
        ) as groq, patch(
            "core.comments.analyzer.call_gemini_api",
            return_value="{}",
        ) as gemini:
            from core.comments.analyzer import _generate_groq_first
            result = _generate_groq_first({}, "prompt")
        self.assertEqual(result, ("{}", "Gemini", "gemini-model"))
        groq.assert_called_once_with("groq-key", "groq-model", "prompt")
        gemini.assert_called_once_with("gemini-key", "gemini-model", "prompt")

    def test_analyze_selected_thread_uses_router_and_returns_provider_metadata(self):
        raw = json.dumps(valid_analysis())
        with patch(
            "core.comments.analyzer._generate_groq_first",
            return_value=(raw, "Groq", "test-model"),
        ) as generate:
            result = analyze_comment_thread(
                {"groq": {"api_key": "test"}},
                {"video_title": "AI tools", "text": "Can I use this tool for free?"},
                [{"author_display_name": "Creator", "text": "Some plans are free."}],
            )
        self.assertEqual(result["provider"], "Groq")
        self.assertEqual(result["model"], "test-model")
        prompt = generate.call_args.args[1]
        self.assertIn("Can I use this tool for free?", prompt)
        self.assertIn("Some plans are free.", prompt)

    def test_invalid_ai_output_fails_closed_after_repair(self):
        router = unittest.mock.Mock()
        router.generate.side_effect = [
            ("not json", "Groq", "model"),
            ("still not json", "Groq", "model"),
        ]
        with patch("core.comments.analyzer.LLMRouter", return_value=router):
            with self.assertRaises(CommentAnalysisError):
                analyze_comment_thread(
                    {"groq": {"api_key": "test"}},
                    {"text": "A question"},
                )
        self.assertEqual(router.generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
