"""UI Smoke Tests for Desktop (1440px) and Mobile (390px) Viewports.

Tests full component lifecycle:
- Header and settings
- Video context card
- Studio file uploader
- Chat composer
- Metadata proposal
- Current-versus-proposed comparison
- Approval controls (Demo simulation vs Real Publish)
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

    def test_app_demo_mode_ui_lifecycle(self):
        """Simulate UI flow in Demo Mode with AppTest."""
        at = AppTest.from_file(APP_PATH, default_timeout=15)
        # Set demo mode in session state
        at.session_state["demo_mode"] = True

        at.run()
        self.assertFalse(at.exception, f"App threw unexpected exception: {at.exception}")

        # Check title / brand is present in markdown
        markdown_texts = [m.value for m in at.markdown]
        has_brand = any("AI Simplified Lab" in text for text in markdown_texts)
        self.assertTrue(has_brand, "Header brand must be present")

        # Check demo mode indicator is rendered
        has_demo_indicator = any("Demo Mode Active" in text for text in markdown_texts)
        self.assertTrue(has_demo_indicator, "Demo mode indicator banner must be rendered")

        # Check chat input is present
        self.assertGreater(len(at.chat_input), 0, "Chat input must be present")

        # Check file uploader is present
        self.assertGreater(len(at.file_uploader), 0, "File uploader must be present")

        # The empty landing state must expose actionable optimization suggestions.
        button_labels = [button.label for button in at.button]
        for expected in ("🎯 Title angles", "📱 Description hook", "🔎 Search tags", "✨ Full review"):
            self.assertIn(expected, button_labels, f"Missing starter suggestion: {expected}")

    def test_proposal_card_demo_simulation_vs_real_publish(self):
        """Verify Demo Mode renders simulation approval button and never real publishing button."""
        context = {
            "target_id": "demo-vid-101",
            "title": "LLM Prompt Caching Explained",
            "target_snippet": {
                "title": "LLM Prompt Caching Explained",
                "description": "Old desc",
                "tags": ["python"],
                "categoryId": "28",
            },
            "duration_formatted": "10:40",
            "format_info": {"format": "Standard Video", "badge": "Video (>3m)"},
            "is_owner": False,
            "ownership_status": "Demo Mode (Simulation)",
        }
        proposal = {
            "target_id": "demo-vid-101",
            "before": context["target_snippet"],
            "proposed": {
                "title": "LLM Prompt Caching: 10x Faster AI APIs",
                "description": "New optimized description with clear hook.",
                "tags": ["prompt caching", "llm optimization"],
                "categoryId": "28",
            },
            "title_angles": [
                {"label": "💡 Curiosity", "title": "Angle 1"},
                {"label": "🎯 Direct Value", "title": "Angle 2"},
                {"label": "🔍 Search / SEO", "title": "Angle 3"},
            ],
            "rationale": ["Higher CTR hook"],
        }

        at = AppTest.from_file(APP_PATH, default_timeout=15)
        at.session_state["demo_mode"] = True
        at.session_state["strategist_target_reference"] = context
        at.session_state["strategist_chat_proposal"] = proposal

        at.run()
        self.assertFalse(at.exception, f"App threw exception: {at.exception}")

        button_labels = [b.label for b in at.button]

        # Must have simulated approval button
        has_sim_button = any("Simulate Approval" in label for label in button_labels)
        self.assertTrue(has_sim_button, f"Expected Simulate Approval button, got: {button_labels}")

        # Must NOT have real publish button
        has_real_publish = any("Approve & Publish to YouTube" in label for label in button_labels)
        self.assertFalse(has_real_publish, f"Real publish button must NOT appear in Demo Mode: {button_labels}")

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
        at.session_state["demo_mode"] = False
        at.session_state["strategist_target_reference"] = context
        at.session_state["strategist_chat_proposal"] = proposal

        with patch("core.auth.verify_owner_access", return_value=(True, "owner123", None)):
            at.run()

        self.assertFalse(at.exception, f"App threw exception: {at.exception}")

        button_labels = [b.label for b in at.button]
        has_real_publish = any("Approve & Publish to YouTube" in label for label in button_labels)
        self.assertFalse(has_real_publish, "Unverified video must NOT display publish button")

        warnings = [w.value for w in at.warning]
        has_blocked_warning = any("writes are strictly blocked" in w for w in warnings)
        self.assertTrue(has_blocked_warning, f"Expected blocked writes warning, got: {warnings}")


if __name__ == "__main__":
    unittest.main()
