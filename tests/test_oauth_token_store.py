import json
import tempfile
import unittest
from pathlib import Path

from oauth_token_store import OAuthTokenStore


SECRET = "test-only-strong-signing-secret-0123456789"
SUBJECT = "owner-google-subject-123"


def payload(refresh_token):
    return json.dumps(
        {
            "token": "access-token-for-tests",
            "refresh_token": refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test-client-id",
            "scopes": ["https://example.test/scope"],
        }
    )


class OAuthTokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "shared.sqlite3"
        self.store = OAuthTokenStore(SECRET, self.path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_credentials_are_shared_by_purpose_and_owner(self):
        youtube = payload("youtube-refresh-secret")
        memory = payload("memory-refresh-secret")

        self.store.save_credentials_json(SUBJECT, "youtube", youtube)
        self.store.save_credentials_json(SUBJECT, "memory", memory)

        self.assertEqual(self.store.get_credentials_json(SUBJECT, "youtube"), youtube)
        self.assertEqual(self.store.get_credentials_json(SUBJECT, "memory"), memory)
        self.assertIsNone(self.store.get_credentials_json("different-owner", "youtube"))

    def test_separate_instances_share_active_grants_and_revocations(self):
        first_tab = OAuthTokenStore(SECRET, self.path)
        second_tab = OAuthTokenStore(SECRET, self.path)
        youtube = payload("youtube-refresh-secret")
        memory = payload("memory-refresh-secret")

        first_tab.save_credentials_json(SUBJECT, "youtube", youtube)
        second_tab.save_credentials_json(SUBJECT, "memory", memory)

        self.assertEqual(first_tab.get_credentials_json(SUBJECT, "youtube"), youtube)
        self.assertEqual(first_tab.get_credentials_json(SUBJECT, "memory"), memory)

        second_tab.delete_credentials(SUBJECT, "youtube")
        self.assertEqual(
            first_tab.get_credentials_record(SUBJECT, "youtube")["status"], "revoked"
        )
        self.assertIsNone(first_tab.get_credentials_json(SUBJECT, "youtube"))
        self.assertEqual(first_tab.get_credentials_json(SUBJECT, "memory"), memory)

    def test_stored_values_are_encrypted_at_rest(self):
        secret_value = "unique-sensitive-refresh-token-value"
        self.store.save_credentials_json(SUBJECT, "youtube", payload(secret_value))

        self.assertNotIn(secret_value.encode("utf-8"), self.path.read_bytes())
        self.assertIsNotNone(self.store.get_credentials_json(SUBJECT, "youtube"))

    def test_deleting_one_grant_does_not_delete_the_other(self):
        youtube = payload("youtube-refresh-secret")
        memory = payload("memory-refresh-secret")
        self.store.save_credentials_json(SUBJECT, "youtube", youtube)
        self.store.save_credentials_json(SUBJECT, "memory", memory)

        self.store.delete_credentials(SUBJECT, "memory")

        self.assertIsNone(self.store.get_credentials_json(SUBJECT, "memory"))
        self.assertEqual(
            self.store.get_credentials_record(SUBJECT, "memory")["status"], "revoked"
        )
        self.assertEqual(
            self.store.get_credentials_record(SUBJECT, "youtube")["status"], "active"
        )
        self.assertEqual(self.store.get_credentials_json(SUBJECT, "youtube"), youtube)

    def test_different_app_secret_cannot_read_another_vault_namespace(self):
        self.store.save_credentials_json(SUBJECT, "youtube", payload("refresh-secret"))

        other_app = OAuthTokenStore(SECRET + "-different", self.path)
        self.assertIsNone(other_app.get_credentials_json(SUBJECT, "youtube"))

    def test_invalid_purposes_and_non_object_json_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.get_credentials_json(SUBJECT, "all-scopes")
        with self.assertRaises(ValueError):
            self.store.save_credentials_json(
                SUBJECT, "memory", '["not", "an", "object"]'
            )


if __name__ == "__main__":
    unittest.main()
