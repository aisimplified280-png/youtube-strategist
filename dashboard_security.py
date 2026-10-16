"""Owner checks and account-bound OAuth state for the hosted dashboard."""

import base64
import hashlib
import hmac
import secrets
import time


OAUTH_STATE_TTL_SECONDS = 600


def owner_is_authorized(email, email_verified, configured_owner_email):
    return (
        isinstance(email, str)
        and bool(email.strip())
        and email_verified is True
        and isinstance(configured_owner_email, str)
        and bool(configured_owner_email.strip())
        and hmac.compare_digest(
            email.strip().casefold(),
            configured_owner_email.strip().casefold(),
        )
    )


def _encode_state_payload(payload):
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_state_payload(payload):
    padded = payload + "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def create_oauth_state(subject, secret, *, now=None, nonce=None):
    if not subject or not secret:
        raise ValueError("OAuth state requires an authenticated subject and signing secret.")
    issued_at = int(time.time() if now is None else now)
    nonce = nonce or secrets.token_urlsafe(24)
    subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    payload = f"{issued_at}.{nonce}.{subject_hash}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_encode_state_payload(payload)}.{signature}"


def reuse_or_create_oauth_state(state, subject, secret, *, now=None):
    if state and verify_oauth_state(state, subject, secret, now=now):
        return state
    return create_oauth_state(subject, secret, now=now)


def consume_oauth_state(returned_state, expected_state, subject, secret, *, now=None):
    """Validate callback state even when Streamlit session state did not survive a redirect.

    When the initiating session is still available, require an exact state match.
    When it is unavailable, the signed state still has to be valid, unexpired, and
    bound to the authenticated owner. The authorization code is exchanged immediately
    and Google only permits a code to be redeemed once.
    """
    if not isinstance(returned_state, str):
        return False
    if expected_state is not None:
        if not isinstance(expected_state, str):
            return False
        if not hmac.compare_digest(returned_state, expected_state):
            return False
    return verify_oauth_state(returned_state, subject, secret, now=now)


def verify_oauth_state(state, subject, secret, *, now=None):
    try:
        encoded_payload, supplied_signature = state.split(".", 1)
        payload = _decode_state_payload(encoded_payload)
        issued_text, nonce, expected_subject_hash = payload.split(".", 2)
        issued_at = int(issued_text)
    except (AttributeError, ValueError, UnicodeDecodeError):
        return False
    current_time = int(time.time() if now is None else now)
    if not nonce or issued_at > current_time:
        return False
    if current_time - issued_at > OAUTH_STATE_TTL_SECONDS:
        return False
    actual_subject_hash = hashlib.sha256(
        (subject or "").encode("utf-8")
    ).hexdigest()
    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        hmac.compare_digest(expected_signature, supplied_signature)
        and hmac.compare_digest(expected_subject_hash, actual_subject_hash)
    )
