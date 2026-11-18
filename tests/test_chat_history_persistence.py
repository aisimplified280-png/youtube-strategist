import unittest
from unittest.mock import Mock, patch

from strategist_chat import _append_history


class ChatHistoryPersistenceTests(unittest.TestCase):
    def test_every_saved_message_invalidates_recent_conversation_cache(self):
        store = Mock()
        session = {
            "_copilot_persistent_store": store,
            "_copilot_conversation_id": "conversation-1",
            "_copilot_conversations_cache": {"items": [{"id": "old"}]},
            "_copilot_title_initialized_conversation-1": True,
        }
        history = []
        with patch("strategist_chat.st.session_state", session):
            _append_history(history, "assistant", "A response.")
        store.append_message.assert_called_once_with(
            "conversation-1", "assistant", "A response."
        )
        self.assertNotIn("_copilot_conversations_cache", session)
        self.assertEqual(history[-1], {"role": "assistant", "content": "A response."})

    def test_first_user_message_initializes_title_and_invalidates_saved_history(self):
        store = Mock()
        session = {
            "_copilot_persistent_store": store,
            "_copilot_conversation_id": "conversation-2",
            "_copilot_conversations_cache": {"items": []},
        }
        history = []
        with patch("strategist_chat.st.session_state", session):
            _append_history(history, "user", "Review my latest video")
        store.append_message.assert_called_once()
        store.update_conversation_title.assert_called_once_with(
            "conversation-2", "Review my latest video"
        )
        self.assertNotIn("_copilot_conversations_cache", session)


if __name__ == "__main__":
    unittest.main()
