import unittest

from ui.strategy_activity import _automation_policy_summary


class StrategyActivityPolicySummaryTests(unittest.TestCase):
    def test_autonomous_mode_explains_every_publish_gate(self):
        summary = _automation_policy_summary({
            "automation_mode": "autonomous",
            "emergency_pause": False,
            "min_confidence_auto_publish": 0.88,
        })
        self.assertEqual(summary["state"], "Autonomous")
        self.assertEqual(summary["threshold"], 0.88)
        self.assertIn("Gemini explicitly chooses auto_publish", summary["explanation"])
        self.assertIn("seven-day cooldown", summary["explanation"])
        self.assertIn("caps remain", summary["explanation"])
        self.assertIn("metadata-quality audits", summary["explanation"])

    def test_emergency_pause_overrides_autonomous_mode(self):
        summary = _automation_policy_summary({
            "automation_mode": "autonomous",
            "emergency_pause": True,
            "min_confidence_auto_publish": 0.86,
        })
        self.assertEqual(summary["state"], "Emergency pause")
        self.assertTrue(summary["emergency_pause"])
        self.assertIn("blocked", summary["explanation"])

    def test_shadow_and_paused_modes_are_described_separately(self):
        shadow = _automation_policy_summary({"automation_mode": "shadow"})
        paused = _automation_policy_summary({"automation_mode": "paused"})
        self.assertEqual(shadow["state"], "Shadow mode")
        self.assertIn("does not write metadata", shadow["explanation"])
        self.assertEqual(paused["state"], "Paused")
        self.assertIn("cannot publish automatically", paused["explanation"])

    def test_invalid_mode_and_threshold_fail_to_conservative_defaults(self):
        summary = _automation_policy_summary({
            "automation_mode": "unknown",
            "min_confidence_auto_publish": "invalid",
        })
        self.assertEqual(summary["mode"], "shadow")
        self.assertEqual(summary["threshold"], 0.85)
        self.assertEqual(summary["state"], "Shadow mode")


if __name__ == "__main__":
    unittest.main()
