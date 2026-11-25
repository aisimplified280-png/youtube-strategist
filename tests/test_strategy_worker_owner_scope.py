"""Regression guard for the strategy worker's owner-scoped learning loop."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "channel_strategy_worker.py").read_text(encoding="utf-8")
RUNNER = SOURCE.split("def run_strategy_agent(", 1)[1].split("\ndef main(", 1)[0]


class StrategyWorkerOwnerScopeTests(unittest.TestCase):
    def test_verified_owner_is_bound_before_learning_queries(self):
        bind = RUNNER.index("store.owner_key = owner_key")
        evaluation = RUNNER.index("record_channel_evaluations(")
        feedback = RUNNER.index("format_learning_feedback_for_prompt(")
        self.assertLess(bind, evaluation)
        self.assertLess(bind, feedback)


if __name__ == "__main__":
    unittest.main()
