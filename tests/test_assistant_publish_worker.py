import unittest
from unittest.mock import MagicMock, patch

import assistant_publish_worker as publisher


class AssistantPublishWorkerTests(unittest.TestCase):
    def test_live_before_check_detects_title_description_and_tag_changes(self):
        before = {"title": "Old", "description": "Desc", "tags": ["ai", "tools"], "categoryId": "28"}
        self.assertTrue(publisher.live_matches_before(dict(before), before))
        self.assertFalse(publisher.live_matches_before({**before, "title": "Changed"}, before))
        self.assertFalse(publisher.live_matches_before({**before, "description": "Changed"}, before))
        self.assertFalse(publisher.live_matches_before({**before, "tags": ["ai"]}, before))
        self.assertFalse(publisher.live_matches_before({**before, "categoryId": "27"}, before))

    def test_manual_publish_does_not_call_monitor_cooldown_gate(self):
        store = MagicMock()
        store._request.side_effect = [
            [{
                "id": "request-1",
                "owner_key": "owner-hash",
                "video_id": "AbCdEfGhIjK",
                "approved_by": "chatgpt_user_confirmation",
                "approved_at": "2026-11-11T10:00:00Z",
                "before_metadata": {"title": "Old", "description": "Old desc", "tags": ["old"], "categoryId": "28"},
                "proposed_metadata": {"title": "New", "description": "New desc", "tags": ["new"]},
            }],
            [],
        ]
        live = {
            "title": "Old", "description": "Old desc", "tags": ["old"],
            "categoryId": "28", "channelId": "UC" + "A" * 22,
        }
        youtube = MagicMock()
        youtube.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC" + "A" * 22}]
        }
        youtube.videos.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "AbCdEfGhIjK", "snippet": dict(live)}]},
            {"items": [{"id": "AbCdEfGhIjK", "snippet": {
                **live, "title": "New", "description": "New desc", "tags": ["new"]
            }}]},
        ]
        with (
            patch.object(publisher, "_get_owner_credentials", return_value=object()),
            patch.object(publisher, "build", return_value=youtube),
            patch.object(publisher, "_record_live_metadata_snapshot") as snapshot,
            patch.object(publisher.batch_metadata, "publish_approved_batch_rows",
                         side_effect=lambda yt, rows, ids, **kwargs: rows[0].update(status="verified") or {"success_count": 1}),
        ):
            self.assertTrue(publisher.process_one_request(store, object(), "test-worker"))
        snapshot.assert_called_once()
        self.assertEqual(snapshot.call_args.kwargs["source"], "manual")
        self.assertTrue(any(call.args[1] == "assistant_publish_requests" for call in store._request.call_args_list))

    def test_missing_explicit_approval_fails_without_youtube_write(self):
        store = MagicMock()
        store._request.return_value = [{
            "id": "request-2", "owner_key": "owner-hash", "video_id": "AbCdEfGhIjK",
            "approved_by": "", "approved_at": None,
        }]
        with patch.object(publisher, "_get_owner_credentials") as credentials:
            self.assertTrue(publisher.process_one_request(store, object(), "test-worker"))
        credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
