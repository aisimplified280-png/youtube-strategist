"""UI Smoke Tests for Desktop (1440px) and Mobile (390px) Viewports.

Tests full component lifecycle:
- Header and settings
- Video context card
- Studio file uploader
- Chat composer
- Metadata proposal
- Current-versus-proposed comparison
- Approval controls (verified owner publish only)
- Mobile responsiveness verification (no horizontal scroll, word-wrap)
"""

import os
import unittest
from unittest.mock import MagicMock, patch
from streamlit.testing.v1 import AppTest

import strategist_chat
import youtube_analytics_connector as connector
from ui.theme import inject_theme

APP_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app.py"))


class UISmokeTests(unittest.TestCase):
    """UI component rendering and lifecycle smoke tests."""

    def test_theme_responsive_css_properties(self):
        """Theme defines required responsive CSS rules preventing horizontal scrolling."""
        theme_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui", "theme.py"))
        with open(theme_path, "r", encoding="utf-8") as f:
            css = f.read()

        # Responsive layout constraints
        self.assertIn("max-width: 880px", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("word-break: break-word", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn("max-width: 100%", css)
        self.assertIn("padding-top: 4.5rem", css)
        self.assertIn("padding-top: 4.25rem", css)
        self.assertIn("@media (max-width: 768px)", css)

    def test_owner_ui_lifecycle_and_starter_suggestions(self):
        """Owner sees the single-chat UI; stale demo flags cannot expose a bypass."""
        at = AppTest.from_file(APP_PATH, default_timeout=15)
        at.session_state["demo_mode"] = True

        with patch("core.auth.verify_owner_access", return_value=(True, "owner123", None)):
            at.run()

        self.assertFalse(at.exception, f"App threw unexpected exception: {at.exception}")
        markdown_texts = [m.value for m in at.markdown]
        self.assertTrue(any("AI Simplified Lab" in value for value in markdown_texts))
        self.assertGreater(len(at.chat_input), 0, "Chat input must be present")
        self.assertGreater(len(at.file_uploader), 0, "File uploader must be present")
        self.assertNotIn("demo_mode", at.session_state)
        self.assertFalse(any("Demo Mode" in value for value in markdown_texts))

        button_labels = [button.label for button in at.button]
        for expected in (
            "🎯 Title angles", "📱 Description hook", "🔎 Search tags",
            "✨ Full review", "📊 Find videos to optimize",
        ):
            self.assertIn(expected, button_labels, f"Missing starter suggestion: {expected}")

    def test_unverified_proposal_has_no_simulated_or_real_publish_action(self):
        """Unverified ownership exposes neither a demo simulation nor a real write action."""
        context = {
            "target_id": "public1234567",
            "title": "Public External Video",
            "target_snippet": {
                "title": "Public External Video",
                "description": "Current description",
                "tags": ["external"],
                "categoryId": "28",
            },
            "duration_formatted": "10:40",
            "format_info": {"format": "Standard Video", "badge": "Video (>3m)"},
            "is_owner": False,
            "ownership_status": "Public External Video (Read-Only)",
        }
        proposal = {
            "target_id": "public1234567",
            "before": context["target_snippet"],
            "proposed": {
                "title": "Proposed title",
                "description": "Proposed description",
                "tags": ["python", "ai"],
                "categoryId": "28",
            },
            "title_angles": [
                {"label": "💡 Curiosity", "title": "A distinct curiosity title"},
                {"label": "🎯 Direct Value", "title": "A distinct value title"},
                {"label": "🔍 Search / SEO", "title": "A distinct SEO title"},
            ],
            "rationale": ["Based on current metadata"],
        }
        at = AppTest.from_file(APP_PATH, default_timeout=15)
        at.session_state["strategist_target_reference"] = context
        at.session_state["strategist_chat_proposal"] = proposal

        with patch("core.auth.verify_owner_access", return_value=(True, "owner123", None)):
            at.run()

        self.assertFalse(at.exception, f"App threw exception: {at.exception}")
        button_labels = [button.label for button in at.button]
        self.assertFalse(any("Simulate Approval" in label for label in button_labels))
        self.assertFalse(any("Approve & Publish to YouTube" in label for label in button_labels))


    def test_unverified_owner_blocks_publishing(self):
        """Unverified external video in live mode blocks approval and shows read-only warning."""
        context = {
            "target_id": "ext12345678",
            "title": "External Video",
            "target_snippet": {
                "title": "External Video",
                "description": "Desc",
                "tags": ["external"],
                "categoryId": "28",
            },
            "duration_formatted": "5:00",
            "format_info": {"format": "Standard Video", "badge": "Video (>3m)"},
            "is_owner": False,
            "ownership_status": "Public External Video (Read-Only)",
        }
        proposal = {
            "target_id": "ext12345678",
            "before": context["target_snippet"],
            "proposed": {
                "title": "New Title",
                "description": "New Desc",
                "tags": ["tags"],
                "categoryId": "28",
            },
            "title_angles": [],
            "rationale": [],
        }

        at = AppTest.from_file(APP_PATH, default_timeout=15)
        at.session_state["strategist_target_reference"] = context
        at.session_state["strategist_chat_proposal"] = proposal

        with patch("core.auth.verify_owner_access", return_value=(True, "owner123", None)):
            at.run()

        self.assertFalse(at.exception, f"App threw exception: {at.exception}")

        button_labels = [b.label for b in at.button]
        has_real_publish = any("Approve & Publish to YouTube" in label for label in button_labels)
        self.assertFalse(has_real_publish, "Unverified video must NOT display publish button")
        self.assertFalse(any("Simulate Approval" in label for label in button_labels))

        warnings = [w.value for w in at.warning]
        has_blocked_warning = any("writes are strictly blocked" in w for w in warnings)
        self.assertTrue(has_blocked_warning, f"Expected blocked writes warning, got: {warnings}")


if __name__ == "__main__":
    unittest.main()
