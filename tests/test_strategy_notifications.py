"""Tests for ntfy notification formatting and fail-closed metadata gatekeeper."""

import unittest
from unittest.mock import Mock, patch

from core.metadata_gatekeeper import evaluate_metadata_gatekeeper
from strategy_notifications import build_notifications, main, send_ntfy_message


class TestStrategyNotifications(unittest.TestCase):
    def test_review_and_publish_events_create_actionable_notifications(self):
        actions = [
            {"status": "pending_review", "video_title": "Review this video", "video_id": "abcdefghijk", "confidence": 0.81, "reason": "Creator review required."},
            {"status": "auto_published", "video_title": "Published video", "video_id": "lmnopqrstuv"},
        ]
        messages = build_notifications({"status": "completed"}, actions, "success")
        self.assertEqual(len(messages), 2)
        self.assertIn("Review required", messages[0]["title"])
        self.assertIn("81%", messages[0]["message"])
        self.assertIn("Successfully published", messages[1]["message"])

    def test_successful_noop_run_is_silent(self):
        self.assertEqual(build_notifications({"status": "completed"}, [], "success"), [])

    def test_failed_run_always_alerts(self):
        messages = build_notifications({"status": "failed", "error": "Worker exception"}, [], "failure")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["priority"], "5")

    @patch("strategy_notifications.requests.post")
    def test_ntfy_uses_https_and_private_topic(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.raise_for_status.return_value = None
        send_ntfy_message({"title": "Hello", "message": "World"}, topic="a" * 32)
        post.assert_called_once()
        self.assertIn("/" + "a" * 32, post.call_args.args[0])

    @patch.dict("os.environ", {
        "NTFY_TOPIC": "a" * 32,
        "NTFY_TEST_NOTIFICATION": "1",
    }, clear=True)
    @patch("strategy_notifications.send_ntfy_message")
    def test_manual_test_mode_sends_push_without_supabase_credentials(self, send_message):
        self.assertEqual(main(), 0)
        send_message.assert_called_once()

    def test_ntfy_rejects_guessable_or_invalid_topic(self):
        with self.assertRaises(ValueError):
            send_ntfy_message({"message": "x"}, topic="channel")

    def test_gatekeeper_holds_when_prechecks_fail_without_calling_gemini(self):
        candidate = {"diagnosis": "metadata_quality", "decision": "review", "confidence": 0.99}
        verdict = evaluate_metadata_gatekeeper(candidate, {}, {}, api_key="test")
        self.assertEqual(verdict["decision"], "HOLD")
        self.assertIn("packaging", verdict["reason"])

    @patch("core.metadata_gatekeeper.requests.post")
    def test_gatekeeper_pass_requires_independent_critic_and_exact_source_quote(self, post):
        candidate = {
            "diagnosis": "packaging", "decision": "review", "confidence": 0.82,
            "impressions": 1000, "ctr_percent": 1.8,
            "reasons": ["Thumbnail CTR is 1.8%, below the same-format median of 4.0%."],
            "current_metadata": {"title": "RAG in production", "description": "Learn why RAG fails in production.", "tags": ["RAG"]},
            "factual_evidence": "RAG in production",
            "fields_to_modify": ["title"],
        }
        before = {"title": "RAG in production", "description": "Learn why RAG fails in production.", "tags": ["RAG"]}
        proposed = {"title": "RAG in Production: The Real Problem", "description": before["description"], "tags": ["RAG"]}
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"decision":"PASS","confidence":0.94,"human_judgment_required":false,"factual_support":true,"reasons":["Supported by source metadata."]}'}]}}]}
        post.return_value = response
        verdict = evaluate_metadata_gatekeeper(candidate, before, proposed, api_key="test")
        self.assertEqual(verdict["decision"], "PASS")
        self.assertEqual(post.call_count, 1)

    @patch("core.metadata_gatekeeper.requests.post", side_effect=RuntimeError("network down"))
    def test_gatekeeper_fails_closed_when_gemini_unavailable(self, post):
        candidate = {
            "diagnosis": "packaging", "decision": "auto_publish", "confidence": 0.9,
            "impressions": 1000, "ctr_percent": 1.8,
            "current_metadata": {"title": "RAG", "description": "RAG production guide", "tags": ["RAG"]},
            "factual_evidence": "RAG production guide",
            "fields_to_modify": ["title"],
        }
        before = {"title": "RAG", "description": "RAG production guide", "tags": ["RAG"]}
        proposed = {"title": "RAG: Production Guide", "description": before["description"], "tags": ["RAG"]}
        verdict = evaluate_metadata_gatekeeper(candidate, before, proposed, api_key="test")
        self.assertEqual(verdict["decision"], "HOLD")


if __name__ == "__main__":
    unittest.main()
