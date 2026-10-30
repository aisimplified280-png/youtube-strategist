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


def get_auth_url(purpose: str, subject: str, secrets: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return (auth_url, error_message) for the given purpose ('youtube' or 'memory')."""
    if not subject:
        return None, "Sign-in required to initialize OAuth state."
    signing_secret = secrets.get("security", {}).get("state_secret", "")
    if len(signing_secret) < 32:
        return None, "State secret must be at least 32 characters."

    oauth_cfg = secrets.get(f"{purpose}_oauth") or (secrets.get("youtube_oauth") if purpose == "memory" else None)
    if not oauth_cfg:
        return None, f"OAuth config for {purpose} is missing in secrets."

    try:
        import streamlit as st
        state, verifier = create_purpose_state(subject, signing_secret, purpose)
        st.session_state[f"pending_{purpose}_oauth_state"] = state
        st.session_state[f"pending_{purpose}_code_verifier"] = verifier

        scopes = YOUTUBE_SCOPES if purpose == "youtube" else MEMORY_SCOPES
        flow = build_flow(
            oauth_cfg["client_id"],
            oauth_cfg["client_secret"],
            oauth_cfg["redirect_uri"],
            scopes=scopes,
            state=state,
            code_verifier=verifier,
        )
        user = getattr(st, "user", None)
        email = getattr(user, "email", None) if user else None
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", login_hint=email)
        return auth_url, None
    except Exception as err:
        return None, str(err)
