import json

import pytest

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


def test_credentials_are_shared_by_purpose_and_owner(tmp_path):
    store = OAuthTokenStore(SECRET, tmp_path / "shared.sqlite3")
    youtube = payload("youtube-refresh-secret")
    memory = payload("memory-refresh-secret")

    store.save_credentials_json(SUBJECT, "youtube", youtube)
    store.save_credentials_json(SUBJECT, "memory", memory)

    assert store.get_credentials_json(SUBJECT, "youtube") == youtube
    assert store.get_credentials_json(SUBJECT, "memory") == memory
    assert store.get_credentials_json("different-owner", "youtube") is None


def test_stored_values_are_encrypted_at_rest(tmp_path):
    path = tmp_path / "shared.sqlite3"
    store = OAuthTokenStore(SECRET, path)
    secret_value = "unique-sensitive-refresh-token-value"
    store.save_credentials_json(SUBJECT, "youtube", payload(secret_value))

    assert secret_value.encode("utf-8") not in path.read_bytes()
    assert store.get_credentials_json(SUBJECT, "youtube") is not None


def test_deleting_one_grant_does_not_delete_the_other(tmp_path):
    store = OAuthTokenStore(SECRET, tmp_path / "shared.sqlite3")
    youtube = payload("youtube-refresh-secret")
    memory = payload("memory-refresh-secret")
    store.save_credentials_json(SUBJECT, "youtube", youtube)
    store.save_credentials_json(SUBJECT, "memory", memory)

    store.delete_credentials(SUBJECT, "memory")

    assert store.get_credentials_json(SUBJECT, "memory") is None
    assert store.get_credentials_json(SUBJECT, "youtube") == youtube


def test_different_app_secret_cannot_read_another_vault_namespace(tmp_path):
    path = tmp_path / "shared.sqlite3"
    store = OAuthTokenStore(SECRET, path)
    store.save_credentials_json(SUBJECT, "youtube", payload("refresh-secret"))

    other_app = OAuthTokenStore(SECRET + "-different", path)
    assert other_app.get_credentials_json(SUBJECT, "youtube") is None


def test_invalid_purposes_and_non_object_json_are_rejected(tmp_path):
    store = OAuthTokenStore(SECRET, tmp_path / "shared.sqlite3")

    with pytest.raises(ValueError):
        store.get_credentials_json(SUBJECT, "all-scopes")
    with pytest.raises(ValueError):
        store.save_credentials_json(SUBJECT, "memory", '["not", "an", "object"]')
