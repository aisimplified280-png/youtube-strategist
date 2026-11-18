import unittest
from unittest.mock import patch

from core.llm import LLMError, LLMRouter


class LLMRouterProviderOrderTests(unittest.TestCase):
    def test_groq_is_primary_and_gemini_is_not_called_on_success(self):
        router = LLMRouter({
            "groq": {"api_key": "groq-key", "model": "groq-primary", "fallback_models": ["groq-fallback"]},
            "gemini": {"api_key": "gemini-key", "model": "gemini-primary"},
        })
        with patch("core.llm.call_groq_api", return_value="Groq response") as groq, patch(
            "core.llm.call_gemini_api"
        ) as gemini:
            result = router.generate("prompt")

        self.assertEqual(result, ("Groq response", "Groq", "groq-primary"))
        groq.assert_called_once_with("groq-key", "groq-primary", "prompt")
        gemini.assert_not_called()

    def test_groq_fallback_models_run_before_gemini(self):
        router = LLMRouter({
            "groq": {"api_key": "groq-key", "model": "groq-primary", "fallback_models": ["groq-fallback"]},
            "gemini": {"api_key": "gemini-key", "model": "gemini-primary"},
        })
        with patch(
            "core.llm.call_groq_api",
            side_effect=[
                LLMError("rate limited", provider="groq", recoverable=True),
                LLMError("model unavailable", provider="groq", recoverable=False),
            ],
        ) as groq, patch(
            "core.llm.call_gemini_api", return_value="Gemini fallback"
        ) as gemini:
            result = router.generate("prompt")

        self.assertEqual(result, ("Gemini fallback", "Gemini", "gemini-primary"))
        self.assertEqual(groq.call_count, 2)
        gemini.assert_called_once_with("gemini-key", "gemini-primary", "prompt")

    def test_gemini_is_used_when_groq_key_is_not_configured(self):
        router = LLMRouter({
            "groq": {},
            "gemini": {"api_key": "gemini-key", "model": "gemini-primary"},
        })
        with patch("core.llm.call_groq_api") as groq, patch(
            "core.llm.call_gemini_api", return_value="Gemini response"
        ) as gemini:
            result = router.generate("prompt")

        self.assertEqual(result, ("Gemini response", "Gemini", "gemini-primary"))
        groq.assert_not_called()
        gemini.assert_called_once()

    def test_all_missing_keys_fail_with_provider_details(self):
        router = LLMRouter({"groq": {}, "gemini": {}})
        with patch("core.llm.call_groq_api") as groq, patch("core.llm.call_gemini_api") as gemini:
            with self.assertRaisesRegex(LLMError, "No API key configured"):
                router.generate("prompt")
        groq.assert_not_called()
        gemini.assert_not_called()


if __name__ == "__main__":
    unittest.main()
