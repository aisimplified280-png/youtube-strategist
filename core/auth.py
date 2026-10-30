"""Owner authorization and access-control gate."""

import hmac
from typing import Any, Dict, Optional, Tuple

import streamlit as st

import dashboard_security


def is_demo_mode() -> bool:
    """Legacy compatibility shim: Demo Mode is permanently disabled."""
    return False


def set_demo_mode(enabled: bool):
    """Reject attempts to re-enable the retired Demo Mode."""
    st.session_state.pop("demo_mode", None)
    if enabled:
        raise RuntimeError("Demo Mode has been removed. Sign in with the configured Google owner account.")


def verify_owner_access(secrets: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Verify that current session user is the configured, verified Google owner account.
    
    Returns:
        (is_authorized, subject, error_message)
    """
    user = getattr(st, "user", None)
    if not user:
        return False, None, "Please sign in with Google to access this channel co-pilot."

    email = getattr(user, "email", "")
    email_verified = getattr(user, "email_verified", False)
    expected_owner = secrets.get("access", {}).get("owner_email", "")

    if not dashboard_security.owner_is_authorized(email, email_verified, expected_owner):
        if getattr(user, "is_logged_in", False):
            return False, None, f"Access denied for {email}. Only the configured channel owner ({expected_owner}) may access this app."
        return False, None, "Google sign-in required."

    subject = getattr(user, "sub", "")
    if not subject:
        return False, None, "Google did not return a stable subject identifier. Access is blocked."

    return True, subject, None
