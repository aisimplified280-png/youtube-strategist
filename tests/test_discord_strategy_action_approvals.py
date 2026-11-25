from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import worker


class DiscordApprovedStrategyActionTests(unittest.TestCase):
    def test_only_discord_approved_strategy_actions_are_claimed_and_published(self):
        store = MagicMock()
        store._request.side_effect = [
            [{
                "id": "action-1",
                "owner_key": "owner-hash",
                "video_id": "abcdefghijk",
                "video_title": "Test strategy proposal",
                "before_metadata": {"title": "Before", "description": "Description", "tags": ["AI"], "categoryId": "27"},
                "proposed_metadata": {"title": "After", "description": "Description", "tags": ["AI"], "categoryId": "27"},
                "approval_digest": "a" * 64,
            }],
            [{"id": "action-1"}],
            [],
        ]
        youtube = MagicMock()
        publish_result = {"result": "success", "resource": {"title": "After"}, "updated_parts": ["snippet"]}

        with patch.object(worker, "_get_owner_credentials", return_value=object()), \
             patch.object(worker, "build", return_value=youtube), \
             patch.object(worker.connector, "apply_approved_dashboard_proposal", return_value=publish_result) as apply:
            count = worker.process_discord_approved_strategy_actions(store, object(), limit=5)

        self.assertEqual(count, 1)
        self.assertEqual(store._request.call_args_list[0].args[:2], ("GET", "channel_strategy_actions"))
        self.assertEqual(store._request.call_args_list[1].kwargs["params"]["status"], "eq.approved")
        apply.assert_called_once()
        self.assertEqual(store._request.call_args_list[2].kwargs["payload"]["status"], "approved_published")

    def test_unclaimable_strategy_action_is_not_published(self):
        store = MagicMock()
        store._request.side_effect = [[{
            "id": "action-2", "owner_key": "owner-hash", "video_id": "abcdefghijk",
            "before_metadata": {}, "proposed_metadata": {}, "approval_digest": "b" * 64,
        }], []]
        with patch.object(worker.connector, "apply_approved_dashboard_proposal") as apply:
            count = worker.process_discord_approved_strategy_actions(store, object(), limit=5)
        self.assertEqual(count, 0)
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
