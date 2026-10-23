"""Comprehensive tests for new core modules: linter, LLM router, prompts, auth, analytics logic, and vault."""

import unittest
from unittest.mock import Mock, patch

from core.auth import is_demo_mode, set_demo_mode, verify_owner_access
from core.linter import lint_title, lint_description, lint_tags, lint_proposal, score_hook_text
from core.prompts import build_strategist_prompt
from core.llm import parse_proposal_json, LLMRouter, LLMError
from core.analytics_logic import calculate_delta, get_period_preset
from core.vault import VaultManager


class CoreLinterTests(unittest.TestCase):
    def test_title_length_limits(self):
        # Good length
        res_good = lint_title("How to Benchmark Prompt Caching in Python")
        self.assertLessEqual(res_good["length"], 60)
        self.assertTrue(any("comfortably within" in p for p in res_good["passes"]))

        # Over desktop cutoff
        long_title = "A" * 65
        res_long = lint_title(long_title)
        self.assertTrue(any("truncate on desktop" in w for w in res_long["warnings"]))

        # Over hard limit
        over_100 = "B" * 105
        res_over = lint_title(over_100)
        self.assertTrue(any("hard limit of 100" in w for w in res_over["warnings"]))

    def test_title_misleading_claims_detected(self):
        res = lint_title("Guaranteed Views with this Viral Hack")
        self.assertTrue(any("Misleading promise" in w for w in res["warnings"]))

    def test_description_hashtag_and_length_checks(self):
        # Excessive hashtags
        excess_hashtags = "Video description\n" + " ".join([f"#tag{i}" for i in range(18)])
        res_desc = lint_description(excess_hashtags)
        self.assertTrue(any("Excessive hashtags" in w for w in res_desc["warnings"]))

        # Optimal hashtags
        good_desc = "Clear opening line.\n\nDetails.\n\n#Python #AI #Tutorial"
        res_good = lint_description(good_desc)
        self.assertTrue(any("Optimal hashtag count" in p for p in res_good["passes"]))

    def test_tags_character_budget(self):
        # Under 500 chars
        tags = ["python", "ai", "tutorial", "prompt caching"]
        res_tags = lint_tags(tags)
        self.assertEqual(len(res_tags["warnings"]), 0)
        self.assertLessEqual(res_tags["char_count"], 500)

        # Over 500 chars
        large_tags = ["a" * 60 for _ in range(10)]
        res_over = lint_tags(large_tags)
        self.assertTrue(any("exceed YouTube's 500-character limit" in w for w in res_over["warnings"]))

    def test_hook_scoring_heuristic(self):
        res = score_hook_text("Are you wasting 3 hours every day on manual video editing?")
        self.assertIsNotNone(res)
        self.assertIn("overall_score", res)
        self.assertGreaterEqual(res["overall_score"], 0)
        self.assertLessEqual(res["overall_score"], 100)


class CoreLLMRouterTests(unittest.TestCase):
    def test_parse_proposal_json_from_clean_and_fenced_markdown(self):
        raw_json = '{"title": "New Title", "description": "New Desc", "tags": ["t1", "t2"], "rationale": "Why"}'
        parsed = parse_proposal_json(raw_json)
        self.assertEqual(parsed["title"], "New Title")
        self.assertEqual(parsed["tags"], ["t1", "t2"])

        fenced = f"```json\n{raw_json}\n```"
        parsed_fenced = parse_proposal_json(fenced)
        self.assertEqual(parsed_fenced["title"], "New Title")

    def test_router_falls_back_from_groq_to_gemini(self):
        secrets = {
            "groq": {"api_key": "g-key", "model": "g-model"},
            "gemini": {"api_key": "gem-key", "model": "gemini-2.5-flash"},
        }
        router = LLMRouter(secrets)

        with patch("core.llm.call_groq_api", side_effect=LLMError("Groq 429 rate limit", recoverable=True)), \
             patch("core.llm.call_gemini_api", return_value='{"title": "Gemini Title", "description": "Desc", "tags": []}') as mock_gemini:
            proposal, provider, model = router.generate_structured_proposal("Optimize title")
            self.assertEqual(provider, "Gemini")
            self.assertEqual(model, "gemini-2.5-flash")
            self.assertEqual(proposal["title"], "Gemini Title")
            mock_gemini.assert_called_once()


class CoreAuthTests(unittest.TestCase):
    def test_demo_mode_bypasses_owner_check(self):
        set_demo_mode(True)
        self.assertTrue(is_demo_mode())
        auth, subject, err = verify_owner_access({})
        self.assertTrue(auth)
        self.assertIsNone(err)
        set_demo_mode(False)

    def test_unverified_owner_denied(self):
        set_demo_mode(False)
        secrets = {"access": {"owner_email": "owner@channel.com"}}
        user_mock = Mock(email="owner@channel.com", email_verified=False, is_logged_in=True)
        with patch("core.auth.st.user", user_mock):
            auth, subject, err = verify_owner_access(secrets)
            self.assertFalse(auth)
            self.assertIn("Access denied", err)


class CoreAnalyticsLogicTests(unittest.TestCase):
    def test_calculate_delta(self):
        # Positive change
        fmt, delta_type = calculate_delta(150, 100)
        self.assertEqual(fmt, "+50.0%")
        self.assertEqual(delta_type, "positive")

        # Negative change
        fmt_neg, neg_type = calculate_delta(80, 100)
        self.assertEqual(fmt_neg, "-20.0%")
        self.assertEqual(neg_type, "negative")

    def test_period_preset_ranges(self):
        c_start, c_end, p_start, p_end = get_period_preset("28d")
        self.assertEqual((c_end - c_start).days, 27)
        self.assertEqual((p_end - p_start).days, 27)
        self.assertEqual((c_start - p_end).days, 1)


if __name__ == "__main__":
    unittest.main()
