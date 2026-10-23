"""Resilient token vault interface with clear reconnect states."""

import json
from typing import Any, Dict, Optional, Tuple

import streamlit as st

import oauth_token_store


class VaultManager:
    """Manages the encrypted OAuth token store with graceful error and restart handling."""

    def __init__(self, signing_secret: str, path: Optional[str] = None):
        self._store = oauth_token_store.OAuthTokenStore(signing_secret, path=path)

    def get_token_record(self, subject: str, purpose: str) -> Dict[str, Any]:
        """Fetch token record with normalized status: 'active', 'missing', 'needs_reconnect', 'revoked'.
        
        If a host restart or decryption mismatch occurred, returns 'needs_reconnect'.
        """
        try:
            record = self._store.get_credentials_record(subject, purpose)
            status = record.get("status")
            if status == "invalid":
                return {"status": "needs_reconnect", "credentials_json": None}
            return record
        except Exception:
            return {"status": "needs_reconnect", "credentials_json": None}

    def save_credentials(self, subject: str, purpose: str, credentials_json: str) -> bool:
        """Save credentials encrypted at rest."""
        try:
            self._store.save_credentials_json(subject, purpose, credentials_json)
            return True
        except Exception:
            return False

    def revoke_credentials(self, subject: str, purpose: str) -> bool:
        """Mark credentials revoked."""
        try:
            self._store.delete_credentials(subject, purpose)
            return True
        except Exception:
            return False
