"""Encrypted, process-shared storage for owner-bound Google OAuth credentials.

Streamlit session state is isolated per browser tab. This small SQLite vault lets the
YouTube and Drive-memory callbacks save different grants without overwriting each
other. Credential payloads are encrypted with a key derived from the app's private
state secret; raw Google subject identifiers are never stored.
"""

import base64
import hashlib
from contextlib import contextmanager
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


_ALLOWED_PURPOSES = {"youtube", "memory"}
_DURABLE_DIR = Path.home() / ".creator_growth_agent"
try:
    _DURABLE_DIR.mkdir(parents=True, exist_ok=True)
    _DEFAULT_PATH = _DURABLE_DIR / "ai-simplified-lab-strategist-oauth.sqlite3"
except Exception:
    _DEFAULT_PATH = Path(tempfile.gettempdir()) / "ai-simplified-lab-strategist-oauth.sqlite3"


class OAuthTokenStore:
    """Store one encrypted credential payload per app, owner, and OAuth purpose."""

    def __init__(self, signing_secret: str, path=None):
        if not isinstance(signing_secret, str) or len(signing_secret) < 32:
            raise ValueError("OAuth token storage requires the configured strong signing secret.")
        self.path = Path(
            path
            or os.environ.get("YOUTUBE_STRATEGIST_TOKEN_STORE", str(_DEFAULT_PATH))
        )
        digest = hashlib.sha256(
            ("ai-simplified-lab-oauth-token-store:" + signing_secret).encode("utf-8")
        ).digest()
        self._cipher = Fernet(base64.urlsafe_b64encode(digest))
        self._app_key = hashlib.sha256(
            ("ai-simplified-lab-oauth-store-namespace:" + signing_secret).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _subject_key(subject: str) -> str:
        if not isinstance(subject, str) or not subject:
            raise ValueError("OAuth token storage requires an authenticated owner subject.")
        return hashlib.sha256(subject.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_purpose(purpose: str) -> None:
        if purpose not in _ALLOWED_PURPOSES:
            raise ValueError("Unsupported OAuth token purpose.")

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_credentials (
                app_key TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                purpose TEXT NOT NULL,
                encrypted_value BLOB NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (app_key, subject_key, purpose)
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(oauth_credentials)")
        }
        if "revoked" not in columns:
            connection.execute(
                "ALTER TABLE oauth_credentials "
                "ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0"
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            # File mode hardening is supported on the hosted Linux deployment;
            # tests or alternate hosts may not expose POSIX permissions.
            pass
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_credentials_record(self, subject: str, purpose: str):
        """Return the grant status and payload so tabs can observe shared disconnects."""
        self._validate_purpose(purpose)
        subject_key = self._subject_key(subject)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT encrypted_value, revoked FROM oauth_credentials
                WHERE app_key = ? AND subject_key = ? AND purpose = ?
                """,
                (self._app_key, subject_key, purpose),
            ).fetchone()
        if row is None:
            return {"status": "missing", "credentials_json": None}
        if bool(row[1]):
            return {"status": "revoked", "credentials_json": None}
        try:
            cleartext = self._cipher.decrypt(bytes(row[0])).decode("utf-8")
            if not isinstance(json.loads(cleartext), dict):
                raise ValueError("Invalid stored credential payload.")
            return {"status": "active", "credentials_json": cleartext}
        except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):
            return {"status": "invalid", "credentials_json": None}

    def get_credentials_json(self, subject: str, purpose: str):
        """Return decrypted credential JSON, or None when no active grant is stored."""
        return self.get_credentials_record(subject, purpose)["credentials_json"]

    def save_credentials_json(self, subject: str, purpose: str, credentials_json: str) -> None:
        """Encrypt and upsert a credential payload without logging it."""
        self._validate_purpose(purpose)
        subject_key = self._subject_key(subject)
        if not isinstance(credentials_json, str) or not isinstance(
            json.loads(credentials_json), dict
        ):
            raise ValueError("OAuth credentials must be a JSON object.")
        ciphertext = self._cipher.encrypt(credentials_json.encode("utf-8"))
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO oauth_credentials
                    (app_key, subject_key, purpose, encrypted_value, revoked, updated_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT (app_key, subject_key, purpose) DO UPDATE SET
                    encrypted_value = excluded.encrypted_value,
                    revoked = 0,
                    updated_at = excluded.updated_at
                """,
                (self._app_key, subject_key, purpose, ciphertext, time.time()),
            )

    def delete_credentials(self, subject: str, purpose: str) -> None:
        """Remove just one permission grant; the other purpose remains untouched."""
        self._validate_purpose(purpose)
        subject_key = self._subject_key(subject)
        with self._connection() as connection:
            # Keep a tombstone so already-open tabs learn that this grant was revoked.
            tombstone = self._cipher.encrypt(b"{}")
            connection.execute(
                """
                INSERT INTO oauth_credentials
                    (app_key, subject_key, purpose, encrypted_value, revoked, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT (app_key, subject_key, purpose) DO UPDATE SET
                    encrypted_value = excluded.encrypted_value,
                    revoked = 1,
                    updated_at = excluded.updated_at
                """,
                (self._app_key, subject_key, purpose, tombstone, time.time()),
            )
