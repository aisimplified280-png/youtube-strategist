import unittest
from unittest.mock import MagicMock

from core.persistent_store import SupabasePersistentStore, PersistentStoreError


VIDEO_ID = "AbCdEfGhIjK"
BEFORE = {"title": "Old title", "description": "Old description", "tags": ["old"], "categoryId": "28"}
PATCH = {"title": "New title", "description": "New description", "tags": ["new"]}


class SourceAwarePublishingQueueTests(unittest.TestCase):
    def make_store(self):
        store = object.__new__(SupabasePersistentStore)
        store.owner_key = "verified-owner-hash"
        store._request = MagicMock(return_value=[{"id": "request-1", "status": "queued"}])
        return store

    def test_chat_publish_requires_explicit_user_command(self):
        store = self.make_store()
        with self.assertRaisesRegex(ValueError, "explicit publish confirmation"):
            store.enqueue_chat_publish_request(
                video_id=VIDEO_ID, before_metadata=BEFORE,
                proposed_metadata=PATCH, publish_confirmed=False,
            )
        store._request.assert_not_called()

    def test_chat_publish_queues_source_and_explicit_authorization(self):
        store = self.make_store()
        store.enqueue_chat_publish_request(
            video_id=VIDEO_ID, before_metadata=BEFORE,
            proposed_metadata=PATCH, publish_confirmed=True,
            authorized_by="chatgpt_explicit_user_command", idempotency_key="chat-turn-1",
        )
        args = store._request.call_args
        self.assertEqual(args.args[:2], ("POST", "assistant_publish_requests"))
        payload = args.kwargs["payload"]
        self.assertEqual(payload["request_source"], "chatgpt")
        self.assertEqual(payload["authorization_mode"], "explicit_user_command")
        self.assertEqual(payload["approved_by"], "chatgpt_explicit_user_command")
        self.assertEqual(payload["idempotency_key"], "chat-turn-1")

    def test_chat_publish_rejects_invalid_id_and_unapproved_fields(self):
        store = self.make_store()
        with self.assertRaisesRegex(ValueError, "11-character"):
            store.enqueue_chat_publish_request(
                video_id="recommended", before_metadata=BEFORE,
                proposed_metadata=PATCH, publish_confirmed=True,
            )
        with self.assertRaisesRegex(ValueError, "Only non-empty"):
            store.enqueue_chat_publish_request(
                video_id=VIDEO_ID, before_metadata=BEFORE,
                proposed_metadata={"channelId": "other"}, publish_confirmed=True,
            )
        store._request.assert_not_called()

    def test_monitor_uses_atomic_policy_rpc_and_source_marker(self):
        store = self.make_store()
        store.enqueue_monitor_publish_request(
            video_id=VIDEO_ID, before_metadata=BEFORE,
            proposed_metadata=PATCH, authorized_by="authorized_monitor",
            idempotency_key="monitor-video-1",
        )
        args = store._request.call_args
        self.assertEqual(args.args[:2], ("POST", "rpc/enqueue_monitor_publish_request"))
        payload = args.kwargs["payload"]
        self.assertEqual(payload["p_owner_key"], "verified-owner-hash")
        self.assertEqual(payload["p_video_id"], VIDEO_ID)
        self.assertEqual(payload["p_idempotency_key"], "monitor-video-1")

    def test_worker_client_cannot_enqueue_owner_scoped_chat_publish(self):
        store = object.__new__(SupabasePersistentStore)
        store.owner_key = ""
        store._request = MagicMock()
        with self.assertRaises(PersistentStoreError):
            store.enqueue_chat_publish_request(
                video_id=VIDEO_ID, before_metadata=BEFORE,
                proposed_metadata=PATCH, publish_confirmed=True,
            )
        store._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
