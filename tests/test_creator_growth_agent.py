"""Tests for Creator Growth Agent: Daily Briefing, Idempotency, and SQLite Memory."""

from datetime import date
import unittest
from unittest.mock import MagicMock, patch

from core.creator_db import (
    acquire_briefing_lock,
    release_briefing_lock,
    get_latest_daily_briefing,
    save_daily_briefing,
    get_creator_preferences,
    set_creator_preference,
    record_proposal_decision,
    get_recent_proposal_history,
)
from core.daily_briefing import get_or_create_daily_briefing


class TestCreatorGrowthAgent(unittest.TestCase):

    def test_briefing_lock_prevents_concurrent_runs(self):
        lock_key = "test_unique_lock_key"
        token_a = "owner-token-A"
        token_b = "owner-token-B"

        # Acquire lock with token A
        self.assertTrue(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token=token_a))
        
        # Token B should be denied lock while token A holds it
        self.assertFalse(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token=token_b))
        
        # Token A can refresh its own lock
        self.assertTrue(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token=token_a))

        # Release lock
        release_briefing_lock(lock_key, owner_token=token_a)

        # Token B can now acquire it
        self.assertTrue(acquire_briefing_lock(lock_key, ttl_seconds=10, owner_token=token_b))
        release_briefing_lock(lock_key, owner_token=token_b)

    def test_daily_briefing_demo_mode_structure(self):
        briefing = get_or_create_daily_briefing(is_demo=True, force_refresh=True)
        self.assertEqual(briefing.get("status"), "ready")
        self.assertIn("prioritized_actions", briefing)
        self.assertLessEqual(len(briefing["prioritized_actions"]), 3)
        self.assertIn("research_radar", briefing)

        # Check required fields for each action
        for act in briefing["prioritized_actions"]:
            self.assertIn("video_id", act)
            self.assertIn("title", act)
            self.assertIn("why_today", act)
            self.assertIn("evidence", act)
            self.assertIn("confidence", act)
            self.assertIn("caveat", act)
            self.assertIn("estimated_effort", act)
            self.assertIn("recommended_next_step", act)

    def test_creator_memory_persists_preferences_and_history(self):
        # Update voice preference
        set_creator_preference("voice_tone", "High-energy technical builder")
        prefs = get_creator_preferences()
        self.assertEqual(prefs.get("voice_tone"), "High-energy technical builder")

        # Record proposal decision
        record_proposal_decision(
            video_id="test-vid-123",
            proposal_digest="abcdef123456",
            status="approved",
            proposal_dict={"title": "Test Title"},
            feedback="Great hook",
        )
        history = get_recent_proposal_history(limit=5)
        self.assertTrue(any(h["video_id"] == "test-vid-123" and h["status"] == "approved" for h in history))


if __name__ == "__main__":
    unittest.main()
