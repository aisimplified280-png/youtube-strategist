"""Google OAuth flow and state management for YouTube and Drive Memory."""

from typing import Any, Dict, List, Optional, Tuple
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

import dashboard_security
import youtube_analytics_connector as connector

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
MEMORY_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]


def get_oauth_purpose_secret(signing_secret: str, purpose: str) -> str:
    """Derive HMAC secret per purpose so callback state cannot be crossed."""
    return dashboard_security.oauth_purpose_secret(signing_secret, purpose)


def create_purpose_state(subject: str, signing_secret: str, purpose: str) -> Tuple[str, str]:
    """Generate purpose-bound OAuth state and PKCE verifier."""
    purpose_secret = get_oauth_purpose_secret(signing_secret, purpose)
    state = dashboard_security.create_oauth_state(subject, purpose_secret)
    verifier = dashboard_security.create_oauth_code_verifier(state, purpose_secret)
    return state, verifier


def build_flow(client_id: str, client_secret: str, redirect_uri: str, scopes: List[str], state: Optional[str] = None, code_verifier: Optional[str] = None) -> Flow:
    """Construct google_auth_oauthlib Flow object."""
    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=scopes,
        state=state,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )


def test_or_refresh_credentials(credentials: Credentials) -> Tuple[Optional[Credentials], str]:
    """Validate credentials and refresh if expired.
    
    Returns:
        (valid_credentials or None, status: 'active' | 'needs_reconnect' | 'expired')
    """
    if not credentials:
        return None, "missing"

    if credentials.valid:
        return credentials, "active"

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            if credentials.valid:
                return credentials, "active"
        except RefreshError:
            # Refresh token revoked or hit the 7-day external testing limit
            return None, "needs_reconnect"
        except Exception:
            return None, "needs_reconnect"

    return None, "needs_reconnect"
