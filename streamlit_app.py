"""Streamlit Community Cloud entry point for AI Simplified Lab YouTube Strategist."""

import copy
import csv
import difflib
import html
import io
import json
import re
from datetime import date, timedelta

import httplib2
import requests
import streamlit as st
from google.auth.exceptions import GoogleAuthError, OAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from streamlit.errors import StreamlitSecretNotFoundError

import content_memory
import dashboard_security
import oauth_token_store
import youtube_analytics_connector as connector
import youtube_dashboard
import strategist_chat
import studio_export_analyzer

# Google rejects combined YouTube + Drive-file authorization requests.
# Keep these as two separate grants and keep the tokens separate as well.
MEMORY_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

st.set_page_config(
    page_title="AI Simplified Lab YouTube Strategist",
    page_icon="📊",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container {
        max-width: 980px;
        /* Keep workspace controls clear of Streamlit's sticky top header. */
        padding-top: 4.25rem;
        padding-bottom: 5.5rem;
    }
    .asl-workspace-nav {
        margin: .2rem 0 .45rem;
        padding-bottom: 0;
    }
    .asl-workspace-nav-label {
        font-size: .68rem;
        font-weight: 720;
        letter-spacing: .13em;
        opacity: .62;
        margin: 0 0 .45rem .1rem;
    }
    [data-testid="stSidebar"] {
        border-right: 1px solid rgba(128, 128, 128, .22);
    }
    [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding-top: 1rem;
    }
    .asl-sidebar-brand {
        font-size: .98rem;
        font-weight: 720;
        letter-spacing: -.025em;
        line-height: 1.25;
        margin: .2rem .15rem 1.1rem;
    }
    .asl-sidebar-overline {
        font-size: .68rem;
        font-weight: 720;
        letter-spacing: .13em;
        opacity: .64;
        margin: .85rem .1rem .35rem;
    }
    .asl-oauth-link {
        display: flex;
        align-items: center;
        justify-content: center;
        box-sizing: border-box;
        width: 100%;
        min-height: 2.65rem;
        margin: .25rem 0 .5rem;
        padding: .65rem .8rem;
        border: 1px solid var(--primary-color);
        border-radius: .7rem;
        background: var(--primary-color);
        color: white !important;
        font-weight: 650;
        line-height: 1.25;
        text-align: center;
        text-decoration: none !important;
        overflow-wrap: anywhere;
    }
    .asl-oauth-link:hover { filter: brightness(.94); }
    .asl-oauth-link:focus-visible { outline: 2px solid var(--text-color); outline-offset: 2px; }
    .asl-page-heading { margin: .15rem 0 1.25rem; }
    .asl-page-title {
        font-size: clamp(1.45rem, 3.3vw, 1.95rem);
        line-height: 1.18;
        font-weight: 700;
        letter-spacing: -.035em;
        margin: 0;
    }
    .asl-page-subtitle {
        margin-top: .4rem;
        margin-bottom: 0;
        opacity: .7;
        font-size: .91rem;
    }
    .asl-proposal-field {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: normal;
        background: var(--secondary-background-color);
        color: var(--text-color);
        border: 1px solid rgba(128, 128, 128, .2);
        border-radius: .75rem;
        padding: .85rem .9rem;
        line-height: 1.55;
        font-size: .95rem;
    }
    [data-testid="stButton"] button {
        min-height: 2.65rem;
        white-space: normal;
        line-height: 1.25;
        border-radius: .7rem;
    }
    [data-testid="stChatInput"] {
        border-radius: 1rem;
    }
    [data-testid="stChatMessage"] {
        border: none;
        background: transparent;
        padding: .45rem .15rem;
    }
    [data-testid="stExpander"] {
        border-radius: .75rem;
    }
    @media (max-width: 640px) {
        .block-container {
            /* Streamlit's header still occupies space on narrow screens. */
            padding-top: 3.65rem;
            padding-bottom: 4.8rem;
            padding-left: .8rem;
            padding-right: .8rem;
        }
        .asl-workspace-nav {
            margin-bottom: 1rem;
        }
        .asl-workspace-nav [data-testid="stButton"] button {
            padding-left: .25rem;
            padding-right: .25rem;
            font-size: .84rem;
        }
        .asl-page-heading { margin-bottom: 1rem; }
        .asl-page-subtitle { font-size: .87rem; }
        .asl-proposal-field { padding: .75rem; font-size: .9rem; }
        [data-testid="stButton"] button { min-height: 2.85rem; }
        [data-testid="stChatMessage"] { padding: .3rem 0; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)



def _read_secrets():
    required = {
        "auth": ("client_id", "client_secret", "cookie_secret", "redirect_uri",
                 "server_metadata_url"),
        "access": ("owner_email",),
        "youtube_oauth": ("client_id", "client_secret", "redirect_uri"),
        "security": ("state_secret",),
    }
    try:
        secret_store = st.secrets
        tuple(secret_store)
    except StreamlitSecretNotFoundError:
        st.error(
            "Streamlit Secrets are not configured. Add the required Web OAuth, "
            "owner identity, and security settings before using this dashboard."
        )
        st.stop()

    missing = []
    for section, keys in required.items():
        if section not in secret_store:
            missing.append(f"[{section}]")
            continue
        missing.extend(
            f"[{section}].{key}"
            for key in keys
            if not isinstance(secret_store[section].get(key), str)
            or not secret_store[section].get(key).strip()
        )
    if (
        "auth" in secret_store
        and "youtube_oauth" in secret_store
        and secret_store["auth"].get("client_id")
        != secret_store["youtube_oauth"].get("client_id")
    ):
        missing.append("matching OAuth client IDs in [auth] and [youtube_oauth]")
    if (
        "auth" in secret_store
        and "youtube_oauth" in secret_store
        and secret_store["auth"].get("client_secret")
        != secret_store["youtube_oauth"].get("client_secret")
    ):
        missing.append("matching OAuth client secrets in [auth] and [youtube_oauth]")
    if "auth" in secret_store:
        cookie_secret = secret_store["auth"].get("cookie_secret", "")
        if isinstance(cookie_secret, str) and len(cookie_secret) < 32:
            missing.append("[auth].cookie_secret (at least 32 characters)")
    if "security" in secret_store:
        state_secret = secret_store["security"].get("state_secret", "")
        if isinstance(state_secret, str) and len(state_secret) < 32:
            missing.append("[security].state_secret (at least 32 characters)")
    if missing:
        st.error(
            "Dashboard secrets are incomplete or inconsistent. Check "
            "`.streamlit/secrets.toml.example`; missing/invalid settings: "
            + ", ".join(missing)
        )
        st.stop()
    return secret_store


def _owner_identity(secrets):
    user = st.user
    email = getattr(user, "email", "")
    verified = getattr(user, "email_verified", False)
    expected_owner = secrets["access"].get("owner_email", "")
    if not dashboard_security.owner_is_authorized(email, verified, expected_owner):
        st.error(
            "Access denied. Sign in with the configured, verified Google owner account."
        )
        if getattr(user, "is_logged_in", False):
            st.button("Sign out", on_click=st.logout)
        st.stop()
    subject = getattr(user, "sub", "")
    if not subject:
        st.error("Google did not provide a stable account subject; access is blocked.")
        st.stop()
    return subject


def _flow_config(secrets):
    oauth = secrets["youtube_oauth"]
    return {
        "web": {
            "client_id": oauth["client_id"],
            "client_secret": oauth["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [oauth["redirect_uri"]],
        }
    }


def _oauth_purpose_secret(secrets, purpose):
    return dashboard_security.oauth_purpose_secret(
        secrets["security"]["state_secret"], purpose
    )


def _new_google_flow(secrets, *, scopes=None, state=None, code_verifier=None):
    oauth = secrets["youtube_oauth"]
    return Flow.from_client_config(
        _flow_config(secrets),
        scopes=list(scopes or connector.SCOPES),
        state=state,
        redirect_uri=oauth["redirect_uri"],
        code_verifier=code_verifier,
    )


def _render_oauth_link(label, authorization_url, help_text):
    """Use Streamlit's supported link widget to open OAuth outside the app iframe."""
    st.link_button(
        label,
        authorization_url,
        type="primary",
        use_container_width=True,
        help=help_text,
    )
    st.caption(
        "Google authorization opens in a new tab. After consent, the encrypted shared "
        "token store makes the grant available to the other app tab as well."
    )


def _restore_shared_oauth_credentials(token_store, subject):
    """Hydrate grants created in other browser tabs into this Streamlit session."""
    if token_store is None:
        return
    try:
        saved = {
            "youtube": token_store.get_credentials_record(subject, "youtube"),
            "memory": token_store.get_credentials_record(subject, "memory"),
        }
    except Exception:
        st.session_state["oauth_token_store_error"] = (
            "The encrypted shared authorization store could not be read. "
            "This tab may not see permissions completed in another tab; retry the connection "
            "or check the app's writable temporary storage."
        )
        return

    # A token already in the shared vault is authoritative and may have been refreshed
    # in another tab. Do not erase a valid in-session grant if a preceding write failed.
    for purpose, session_key in (
        ("youtube", "youtube_credentials_json"),
        ("memory", "memory_credentials_json"),
    ):
        record = saved[purpose]
        if record["status"] == "active":
            st.session_state[session_key] = record["credentials_json"]
        elif record["status"] in {"revoked", "invalid"}:
            st.session_state.pop(session_key, None)


def _save_shared_oauth_credentials(token_store, subject, purpose, credentials_json):
    """Persist a successful grant without ever logging its access or refresh tokens."""
    if token_store is None:
        st.session_state["oauth_token_store_error"] = (
            "Shared authorization storage is unavailable, so Google permissions may not "
            "carry between browser tabs. The current tab can still use its own connection."
        )
        return False
    try:
        token_store.save_credentials_json(subject, purpose, credentials_json)
    except Exception:
        st.session_state["oauth_token_store_error"] = (
            "Google authorization succeeded, but the encrypted shared authorization store "
            "could not save the grant. The connection may be limited to this tab; retry after "
            "checking the app's writable temporary storage."
        )
        return False
    st.session_state.pop("oauth_token_store_error", None)
    return True


def _resolve_api_oauth_state(returned_state, secrets, subject):
    """Identify exactly one signed scope purpose without mixing scope grants."""
    if not returned_state:
        return None
    attempts = (
        ("youtube", "pending_youtube_oauth_state"),
        ("memory", "pending_memory_oauth_state"),
    )
    for purpose, pending_key in attempts:
        purpose_secret = _oauth_purpose_secret(secrets, purpose)
        expected = st.session_state.get(pending_key)
        if dashboard_security.consume_oauth_state(
            returned_state,
            expected,
            subject,
            purpose_secret,
        ):
            return purpose, pending_key, purpose_secret
    return None


def _handle_youtube_callback(secrets, subject, token_store=None):
    """Dispatch OAuth callbacks to independent YouTube or memory credentials."""
    query = st.query_params
    code = query.get("code")
    returned_state = query.get("state")
    oauth_error = query.get("error")

    if not code and not returned_state and not oauth_error:
        return

    resolved = _resolve_api_oauth_state(returned_state, secrets, subject)
    if oauth_error:
        if resolved:
            purpose, pending_key, _ = resolved
            st.session_state.pop(pending_key, None)
            if purpose == "memory":
                st.session_state["memory_auth_error"] = (
                    "Private memory authorization was not completed. "
                    "YouTube access remains separate and unchanged."
                )
            else:
                st.session_state["youtube_auth_error"] = (
                    "YouTube authorization was not completed. Try connecting YouTube again."
                )
        else:
            st.session_state["youtube_auth_error"] = (
                "Google could not validate the authorization response. Start the connection again."
            )
        query.clear()
        st.rerun()
        return

    if not code or not returned_state:
        st.session_state.pop("pending_youtube_oauth_state", None)
        st.session_state.pop("pending_memory_oauth_state", None)
        query.clear()
        st.error("Google returned an incomplete authorization response. Start that connection again.")
        return

    if not resolved:
        query.clear()
        st.error(
            "Google returned an invalid, expired, or mismatched authorization response. "
            "Start the connection again from this signed-in session."
        )
        return

    purpose, pending_key, purpose_secret = resolved
    st.session_state.pop(pending_key, None)
    scopes = connector.SCOPES if purpose == "youtube" else MEMORY_SCOPES
    verifier = dashboard_security.create_oauth_code_verifier(
        returned_state, purpose_secret
    )
    try:
        flow = _new_google_flow(
            secrets,
            scopes=scopes,
            state=returned_state,
            code_verifier=verifier,
        )
        # The flow already has its configured redirect URI; do not pass it again.
        flow.fetch_token(code=code)
    except (
        ValueError,
        GoogleAuthError,
        OAuthError,
        OAuth2Error,
        requests.RequestException,
    ) as error:
        error_code = getattr(error, "error", None)
        if not isinstance(error_code, str) and isinstance(error, requests.RequestException):
            response = getattr(error, "response", None)
            if response is not None:
                try:
                    response_body = response.json()
                except (ValueError, TypeError):
                    response_body = {}
                if isinstance(response_body, dict):
                    error_code = response_body.get("error")
        safe_codes = {
            "invalid_client": "Google rejected the OAuth client credentials. Verify that the Web application client ID and secret belong to the same Google Cloud OAuth client.",
            "invalid_grant": "Google rejected this authorization code or PKCE verifier. Start a fresh authorization attempt and verify the redirect URI exactly matches the configured URI.",
            "redirect_uri_mismatch": "The redirect URI does not match. Verify the exact redirect URI in Google Cloud and Streamlit Secrets.",
            "unauthorized_client": "This OAuth client is not permitted to use the requested flow. Verify that it is configured as a Web application client.",
        }
        query.clear()
        if not (
            isinstance(error_code, str)
            and 0 < len(error_code) <= 64
            and all(
                character.isascii()
                and (character.isalnum() or character in "._-")
                for character in error_code
            )
        ):
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            error_code = f"HTTP {status}" if isinstance(status, int) else type(error).__name__
        message = safe_codes.get(
            error_code,
            "Verify the OAuth client and redirect URI, then retry this connection.",
        )
        if purpose == "memory":
            st.session_state["memory_auth_error"] = message
        else:
            st.session_state["youtube_auth_error"] = message
        query.clear()
        st.rerun()
        return
    except HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        query.clear()
        message = f"Google authorization exchange failed (HTTP {status})."
        if purpose == "memory":
            st.session_state["memory_auth_error"] = message
        else:
            st.session_state["youtube_auth_error"] = message
        st.rerun()
        return

    credentials = flow.credentials
    if not credentials.has_scopes(scopes):
        query.clear()
        if purpose == "memory":
            st.session_state["memory_auth_error"] = (
                "Google did not grant the limited Drive-file permission needed for the "
                "private memory spreadsheet. Reauthorize memory and approve that permission."
            )
        else:
            st.session_state["youtube_auth_error"] = (
                "Google did not grant all required YouTube Analytics and metadata scopes. "
                "Reconnect YouTube and approve the requested YouTube permissions."
            )
        st.rerun()
        return

    credentials_json = credentials.to_json()
    # A shared encrypted store is essential here: OAuth callbacks can arrive in a
    # different Streamlit session from the tab that initiated authorization.
    persisted = _save_shared_oauth_credentials(
        token_store, subject, purpose, credentials_json
    )
    if purpose == "youtube":
        st.session_state["youtube_credentials_json"] = credentials_json
        st.session_state["oauth_connected_message"] = True
    else:
        st.session_state["memory_credentials_json"] = credentials_json
        # Do not show "connected" until a workbook is created/opened and verified.
        st.session_state["memory_connection_pending_verification"] = True
        st.session_state["memory_store_setup_attempted"] = False
        st.session_state.pop("content_memory_store_verified", None)
        st.session_state.pop("memory_auth_error", None)
        st.session_state.pop("memory_store_setup_error", None)
    if not persisted:
        st.session_state["oauth_shared_store_fallback"] = True
    query.clear()
    st.rerun()


def _credentials_from_session(
    session_key="youtube_credentials_json",
    required_scopes=None,
    label="YouTube",
    token_store=None,
):
    required_scopes = list(required_scopes or connector.SCOPES)
    credentials_json = st.session_state.get(session_key)
    if not credentials_json:
        return None
    try:
        credentials = Credentials.from_authorized_user_info(
            json.loads(credentials_json),
            required_scopes,
        )
    except (ValueError, KeyError, TypeError):
        st.session_state.pop(session_key, None)
        purpose = "memory" if label != "YouTube" else "youtube"
        subject = st.session_state.get("oauth_owner_subject")
        if token_store is not None and subject:
            try:
                token_store.delete_credentials(subject, purpose)
            except Exception:
                st.session_state["oauth_token_store_error"] = (
                    "An invalid saved authorization could not be cleared from shared storage."
                )
        return None
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            refreshed_json = credentials.to_json()
            st.session_state[session_key] = refreshed_json
            purpose = "memory" if label != "YouTube" else "youtube"
            subject = st.session_state.get("oauth_owner_subject")
            if token_store is not None and subject:
                _save_shared_oauth_credentials(token_store, subject, purpose, refreshed_json)
            return credentials
        except (GoogleAuthError, requests.RequestException):
            st.session_state.pop(session_key, None)
            purpose = "memory" if label != "YouTube" else "youtube"
            subject = st.session_state.get("oauth_owner_subject")
            if token_store is not None and subject:
                try:
                    token_store.delete_credentials(subject, purpose)
                except Exception:
                    st.session_state["oauth_token_store_error"] = (
                        "An expired authorization could not be cleared from shared storage."
                    )
            warning_key = "youtube_auth_warning" if label == "YouTube" else "memory_auth_warning"
            st.session_state[warning_key] = (
                f"Saved {label} authorization could not be refreshed. "
                "Reconnect that permission when ready."
            )
            return None
    if credentials.expired:
        st.session_state.pop(session_key, None)
        purpose = "memory" if label != "YouTube" else "youtube"
        subject = st.session_state.get("oauth_owner_subject")
        if token_store is not None and subject:
            try:
                token_store.delete_credentials(subject, purpose)
            except Exception:
                st.session_state["oauth_token_store_error"] = (
                    "An expired authorization could not be cleared from shared storage."
                )
        warning_key = "youtube_auth_warning" if label == "YouTube" else "memory_auth_warning"
        st.session_state[warning_key] = (
            f"Saved {label} authorization expired without a refresh token. "
            "Reconnect that permission when ready."
        )
        return None
    st.session_state.pop(session_key, None)
    purpose = "memory" if label != "YouTube" else "youtube"
    subject = st.session_state.get("oauth_owner_subject")
    if token_store is not None and subject:
        try:
            token_store.delete_credentials(subject, purpose)
        except Exception:
            st.session_state["oauth_token_store_error"] = (
                "An invalid authorization could not be cleared from shared storage."
            )
    return None


def _start_youtube_authorization(secrets, subject):
    signing_secret = _oauth_purpose_secret(secrets, "youtube")
    pending_state = st.session_state.get("pending_youtube_oauth_state")
    state = dashboard_security.reuse_or_create_oauth_state(
        pending_state,
        subject,
        signing_secret,
    )
    st.session_state["pending_youtube_oauth_state"] = state
    verifier = dashboard_security.create_oauth_code_verifier(state, signing_secret)
    flow = _new_google_flow(
        secrets,
        scopes=connector.SCOPES,
        state=state,
        code_verifier=verifier,
    )
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        login_hint=getattr(st.user, "email", ""),
    )
    _render_oauth_link(
        "Authorize YouTube Analytics and metadata access",
        authorization_url,
        "Google opens in a new tab. This grant covers YouTube Analytics and metadata "
        "permissions only.",
    )
    st.caption(
        "YouTube authorization is stored separately from the optional private-memory connection."
    )


def _start_memory_authorization(secrets, subject):
    signing_secret = _oauth_purpose_secret(secrets, "memory")
    pending_state = st.session_state.get("pending_memory_oauth_state")
    state = dashboard_security.reuse_or_create_oauth_state(
        pending_state,
        subject,
        signing_secret,
    )
    st.session_state["pending_memory_oauth_state"] = state
    verifier = dashboard_security.create_oauth_code_verifier(state, signing_secret)
    flow = _new_google_flow(
        secrets,
        scopes=MEMORY_SCOPES,
        state=state,
        code_verifier=verifier,
    )
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        login_hint=getattr(st.user, "email", ""),
    )
    _render_oauth_link(
        "Connect private Strategist Memory",
        authorization_url,
        "Google opens in a new tab. This optional grant requests only the limited drive.file "
        "permission for the private spreadsheet; it does not request YouTube scopes in the "
        "same consent.",
    )

def _verify_private_memory_workbook(memory_credentials):
    """Create/locate the spreadsheet and prove the granted token can read and write it."""
    if memory_credentials is None or st.session_state.get("memory_store_setup_attempted"):
        return

    st.session_state["memory_store_setup_attempted"] = True
    try:
        store = content_memory.GoogleSheetsMemoryStore.from_credentials(memory_credentials)
        pending_snapshot = st.session_state.get("pending_memory_snapshot")
        if pending_snapshot:
            store.append_scan(pending_snapshot)
            # Remove immediately after a successful append to prevent duplicate writes.
            st.session_state.pop("pending_memory_snapshot", None)
        recent_scans = store.list_recent_scans(limit=8)
    except Exception as error:
        message = content_memory.describe_memory_error(error)
        st.session_state["content_memory_store_verified"] = False
        st.session_state["content_memory_write_status"] = "session_only"
        st.session_state["content_memory_write_error"] = message
        st.session_state["memory_store_setup_error"] = message
        return

    st.session_state["content_memory_store_verified"] = True
    st.session_state["content_memory_spreadsheet_url"] = store.spreadsheet_url
    st.session_state["content_memory_recent_scans"] = recent_scans
    st.session_state["content_memory_write_status"] = "saved"
    st.session_state.pop("content_memory_write_error", None)
    st.session_state.pop("memory_store_setup_error", None)
    if st.session_state.pop("memory_connection_pending_verification", False):
        st.session_state["memory_connected_message"] = True


def _display_metric(value):
    return "Unavailable" if value is None else f"{value:,}"


def _visible_video_rows(rows, format_filter):
    if format_filter == "All":
        return rows
    return [row for row in rows if row["format"] == format_filter]


def _proposal_digest(video_id, before, proposed):
    return connector.metadata_proposal_digest(video_id, before, proposed)


def _append_session_audit(entry):
    records = st.session_state.setdefault("session_audit_records", [])
    records.insert(0, entry)
    del records[20:]


def _selected_analytics_details(analytics, video_id, start_date, end_date):
    key = (video_id, start_date.isoformat(), end_date.isoformat())
    cached = st.session_state.get("selected_analytics_details")
    if cached and cached["key"] == key:
        return cached
    traffic_rows, traffic_status = youtube_dashboard.fetch_video_traffic_sources(
        analytics,
        video_id,
        start_date,
        end_date,
    )
    audience_rows, audience_status = (
        youtube_dashboard.fetch_video_audience_demographics(
            analytics,
            video_id,
            start_date,
            end_date,
        )
    )
    details = {
        "key": key,
        "traffic_rows": traffic_rows,
        "traffic_status": traffic_status,
        "audience_rows": audience_rows,
        "audience_status": audience_status,
    }
    st.session_state["selected_analytics_details"] = details
    return details


def _render_proposal(video_row, youtube):
    video_id = video_row["video_id"]
    snippet = video_row["snippet"]
    rejected_video_ids = st.session_state.setdefault("rejected_video_ids", set())
    if video_id in rejected_video_ids:
        st.info("This video's proposal was rejected. No YouTube changes were made.")
        if st.button(
            "Prepare a new proposal",
            key=f"new_proposal_{video_id}",
        ):
            rejected_video_ids.remove(video_id)
            st.rerun()
        return

    pending = st.session_state.get("pending_metadata_proposal")
    if pending and pending["video_id"] != video_id:
        st.session_state.pop("pending_metadata_proposal", None)
        pending = None
    if pending is None:
        try:
            generated = youtube_dashboard.generate_metadata_proposal(snippet)
        except connector.MetadataValidationError as error:
            st.warning(
                "The current metadata did not allow a safe automatic suggestion: "
                f"{error} You can edit a proposal below; it must pass validation "
                "before it can be approved."
            )
            generated = {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "tags": snippet.get("tags", []),
                "explanation": "No automatic changes were suggested.",
            }
        before = copy.deepcopy(snippet)
        proposed = copy.deepcopy(before)
        for field in ("title", "description", "tags"):
            proposed[field] = generated[field]
        pending = {
            "video_id": video_id,
            "before": before,
            "proposed": proposed,
            "explanation": generated["explanation"],
            "digest": _proposal_digest(video_id, before, proposed),
        }
        st.session_state["pending_metadata_proposal"] = pending

    proposed = pending["proposed"]
    st.subheader("Metadata proposal")
    st.caption(pending["explanation"])
    current_column, proposed_column = st.columns(2)
    with current_column:
        st.markdown("**Current metadata**")
        st.text_area(
            "Current title",
            pending["before"].get("title", ""),
            disabled=True,
            key=f"current_title_{video_id}",
        )
        st.text_area(
            "Current description",
            pending["before"].get("description", ""),
            disabled=True,
            key=f"current_description_{video_id}",
        )
        st.text_area(
            "Current YouTube tags (one per line)",
            "\n".join(pending["before"].get("tags", [])),
            disabled=True,
            key=f"current_tags_{video_id}",
        )
    with proposed_column:
        st.markdown("**Proposed metadata — edit these fields, then save before approval**")
        title_key = f"proposed_title_{video_id}"
        description_key = f"proposed_description_{video_id}"
        tags_key = f"proposed_tags_{video_id}"
        st.session_state.setdefault(title_key, proposed.get("title", ""))
        st.session_state.setdefault(
            description_key, proposed.get("description", "")
        )
        st.session_state.setdefault(tags_key, "\n".join(proposed.get("tags", [])))
        title_value = st.text_area("Proposed title", key=title_key, height=80)
        description_value = st.text_area(
            "Proposed complete description", key=description_key, height=240
        )
        tags_value = st.text_area(
            "Proposed YouTube tags (one per line)", key=tags_key, height=120
        )

    proposed_tags = [
        tag.strip()
        for tag in tags_value.splitlines()
        if tag.strip()
    ]
    live_proposed = copy.deepcopy(pending["before"])
    live_proposed.update({
        "title": title_value,
        "description": description_value,
        "tags": proposed_tags,
    })
    live_digest = _proposal_digest(video_id, pending["before"], live_proposed)
    has_unsaved_edits = live_digest != pending["digest"]
    validation_error = None
    try:
        connector._validate_metadata(live_proposed)
    except connector.MetadataValidationError as error:
        validation_error = str(error)
        st.warning(f"Proposal validation: {validation_error}")
    if has_unsaved_edits:
        st.warning(
            "The displayed proposal has been edited. Save the edits to create a new "
            "proposal; any previous approval is invalidated."
        )

    with st.expander("Readable metadata diff", expanded=True):
        before_text = json.dumps(
            connector._metadata_snapshot(pending["before"]),
            indent=2,
            ensure_ascii=False,
        ).splitlines()
        after_text = json.dumps(
            connector._metadata_snapshot(live_proposed),
            indent=2,
            ensure_ascii=False,
        ).splitlines()
        diff = difflib.unified_diff(
            before_text,
            after_text,
            fromfile="current",
            tofile="proposed",
            lineterm="",
        )
        st.code("\n".join(diff) or "(no metadata changes)", language="diff")

    if st.button(
        "Save edits as a new proposal",
        key=f"save_proposal_{video_id}",
        disabled=not has_unsaved_edits,
    ):
        try:
            connector._validate_metadata(live_proposed)
        except connector.MetadataValidationError as error:
            st.error(str(error))
        else:
            pending["proposed"] = live_proposed
            pending["digest"] = live_digest
            pending["explanation"] = (
                "Edited by the owner. Re-review this exact proposal before applying."
            )
            st.session_state["pending_metadata_proposal"] = pending
            st.rerun()

    approve_column, reject_column = st.columns(2)
    with approve_column:
        approved = st.button(
            "Approve and apply this exact proposal",
            type="primary",
            key=f"approve_proposal_{video_id}",
            disabled=has_unsaved_edits or validation_error is not None,
        )
    with reject_column:
        rejected = st.button(
            "Reject proposal",
            key=f"reject_proposal_{video_id}",
        )

    if rejected:
        st.session_state.pop("pending_metadata_proposal", None)
        rejected_video_ids.add(video_id)
        for key in (title_key, description_key, tags_key):
            st.session_state.pop(key, None)
        st.info("Proposal rejected. No YouTube changes were made.")
        st.rerun()

    if approved:
        if live_digest != pending["digest"]:
            st.error("The proposal changed. Save and review it before approving.")
            return
        try:
            result = connector.apply_approved_dashboard_proposal(
                youtube,
                video_id,
                pending["before"],
                pending["proposed"],
                pending["digest"],
                audit_callback=_append_session_audit,
            )
        except HttpError as error:
            status = getattr(error.resp, "status", "unknown")
            st.error(
                f"YouTube API rejected the approved update (HTTP {status}). "
                "Check access, OAuth scopes, or quota; no success is claimed."
            )
        except (
            GoogleAuthError,
            httplib2.HttpLib2Error,
            OSError,
            requests.RequestException,
        ):
            st.error(
                "The YouTube update could not be completed because the Google API "
                "request failed. No automatic retry was attempted."
            )
        except (
            connector.MetadataValidationError,
            connector.VideoNotFoundError,
            RuntimeError,
            ValueError,
        ) as error:
            st.error(str(error))
        else:
            st.session_state["pending_metadata_proposal"] = None
            st.session_state["last_update_result"] = result
            st.session_state.pop("dashboard_data_key", None)
            st.rerun()




def _save_scan_snapshot_to_memory(memory_credentials, snapshot):
    """Append one previously scanned snapshot using only the memory OAuth token."""
    try:
        store = content_memory.GoogleSheetsMemoryStore.from_credentials(memory_credentials)
        store.append_scan(snapshot)
    except Exception as error:
        message = content_memory.describe_memory_error(error)
        st.session_state["pending_memory_snapshot"] = snapshot
        st.session_state["content_memory_store_verified"] = False
        st.session_state["content_memory_write_status"] = "session_only"
        st.session_state["content_memory_write_error"] = message
        st.session_state["memory_store_setup_error"] = message
        st.session_state.pop("content_memory_recent_scans", None)
        return

    # The append is confirmed. Clear the pending row before the history read so a
    # transient read failure can never cause the successful append to be duplicated.
    st.session_state.pop("pending_memory_snapshot", None)
    st.session_state["content_memory_spreadsheet_url"] = store.spreadsheet_url
    st.session_state["content_memory_store_verified"] = True
    st.session_state["content_memory_write_status"] = "saved"
    st.session_state.pop("content_memory_write_error", None)
    st.session_state.pop("memory_store_setup_error", None)
    try:
        st.session_state["content_memory_recent_scans"] = store.list_recent_scans(limit=8)
        st.session_state.pop("content_memory_history_error", None)
    except Exception as error:
        st.session_state["content_memory_recent_scans"] = []
        st.session_state["content_memory_history_error"] = (
            content_memory.describe_memory_error(error)
        )


def _persist_opportunity_scan(
    memory_credentials,
    start_date,
    end_date,
    recent_ids,
    measured_recent,
    ctr_available,
    candidates,
):
    """Keep a session fallback and save only via the separate memory token."""
    snapshot = content_memory.build_scan_snapshot(
        start_date=start_date,
        end_date=end_date,
        upload_count=len(recent_ids),
        measured_views_count=len(measured_recent),
        usable_ctr_count=len(ctr_available),
        candidates=candidates,
    )
    session_history = st.session_state.setdefault("session_scan_history", [])
    session_history.insert(0, snapshot)
    del session_history[20:]

    if memory_credentials is None:
        st.session_state["pending_memory_snapshot"] = snapshot
        st.session_state["content_memory_write_status"] = "not_connected"
        st.session_state.pop("content_memory_write_error", None)
        st.session_state.pop("content_memory_recent_scans", None)
        return
    _save_scan_snapshot_to_memory(memory_credentials, snapshot)


def _render_recent_opportunities(
    youtube, analytics, reporting, credentials, memory_credentials, secrets, subject
):
    """Scan recent uploads and present measured, evidence-based metadata-review candidates."""
    st.markdown(
        '<div class="asl-page-heading">'
        '<div class="asl-page-title">Find videos to improve</div>'
        '<p class="asl-page-subtitle">Use channel data to shortlist uploads worth reviewing. '
        'No guessed CTR and no automatic publishing.</p></div>',
        unsafe_allow_html=True,
    )

    yesterday = date.today() - timedelta(days=1)
    this_week_start = date.today() - timedelta(days=date.today().weekday())
    default_start = this_week_start - timedelta(days=7)
    default_end = yesterday

    if "opportunity_start_date" not in st.session_state:
        st.session_state["opportunity_start_date"] = default_start
    if "opportunity_end_date" not in st.session_state:
        st.session_state["opportunity_end_date"] = default_end
    if "opportunity_video_limit" not in st.session_state:
        st.session_state["opportunity_video_limit"] = 50

    with st.form("opportunity_scan_form"):
        date_columns = st.columns(2)
        with date_columns[0]:
            start_date = st.date_input(
                "From",
                min_value=date(2005, 1, 1),
                max_value=yesterday,
                key="opportunity_start_date",
            )
        with date_columns[1]:
            end_date = st.date_input(
                "Through",
                min_value=date(2005, 1, 1),
                max_value=yesterday,
                key="opportunity_end_date",
            )
        video_limit = st.selectbox(
            "Compare against recent uploads",
            options=(25, 50, 100),
            key="opportunity_video_limit",
        )
        scan_clicked = st.form_submit_button(
            "Scan uploads",
            type="primary",
            use_container_width=True,
        )

    auto_run = st.session_state.pop("opportunity_auto_run", False)
    request_text = st.session_state.pop("channel_analysis_request", None)
    if request_text:
        st.caption("Starting from your request in chat.")

    if start_date > end_date:
        st.error("Choose a start date on or before the end date.")
        return
    if end_date >= date.today():
        st.error("The reporting period must end yesterday or earlier.")
        return

    query_key = (start_date.isoformat(), end_date.isoformat(), video_limit)
    cached_key = st.session_state.get("opportunity_data_key")
    should_scan = scan_clicked or auto_run
    if should_scan:
        with st.spinner("Checking upload performance and available thumbnail CTR…"):
            try:
                data = youtube_dashboard.load_dashboard_data(
                    youtube,
                    analytics,
                    start_date,
                    end_date,
                    video_limit=video_limit,
                    reporting=reporting,
                    credentials=credentials,
                )
            except HttpError as error:
                status = getattr(getattr(error, "resp", None), "status", "unknown")
                if status == 403:
                    st.error(
                        "YouTube denied an Analytics/Reporting request (HTTP 403). "
                        "Check API enablement, channel authorization, reporting access, and quota."
                    )
                else:
                    st.error(f"YouTube API request failed (HTTP {status}).")
                return
            except (
                GoogleAuthError,
                httplib2.HttpLib2Error,
                OSError,
                requests.RequestException,
            ):
                st.error(
                    "Google could not complete the report request. Reconnect YouTube if needed, "
                    "then retry the scan."
                )
                return
            except (
                connector.VideoNotFoundError,
                connector.MetadataValidationError,
                RuntimeError,
                ValueError,
            ) as error:
                st.error(str(error))
                return
        st.session_state["opportunity_data"] = data
        st.session_state["opportunity_data_key"] = query_key
    elif cached_key != query_key:
        st.info("Select the reporting dates, then choose **Scan uploads** to analyse them.")
        return

    data = st.session_state.get("opportunity_data")
    if not data or st.session_state.get("opportunity_data_key") != query_key:
        st.info("Choose a date range and scan the uploads to see measured opportunities.")
        return

    recent_ids = [
        row["video_id"]
        for row in data["video_rows"]
        if row.get("published_at")
        and start_date.isoformat() <= row["published_at"][:10] <= end_date.isoformat()
    ]
    candidates = youtube_dashboard.rank_recent_video_opportunities(
        data["video_rows"],
        recent_ids,
        limit=8,
    )
    measured_recent = [
        row for row in data["video_rows"]
        if row.get("video_id") in recent_ids
        and isinstance(row.get("views"), (int, float))
    ]
    ctr_available = [
        row for row in data["video_rows"]
        if row.get("video_id") in recent_ids
        and isinstance(row.get("ctr"), (int, float))
        and isinstance(row.get("impressions"), (int, float))
        and row["impressions"] >= 100
    ]

    if should_scan:
        _persist_opportunity_scan(
            memory_credentials,
            start_date,
            end_date,
            recent_ids,
            measured_recent,
            ctr_available,
            candidates,
        )
    elif memory_credentials and st.session_state.get("pending_memory_snapshot"):
        _save_scan_snapshot_to_memory(
            memory_credentials,
            st.session_state["pending_memory_snapshot"],
        )

    st.caption(
        f"{len(recent_ids)} uploads in the selected weeks · "
        f"{len(measured_recent)} with measured views · "
        f"{len(ctr_available)} with usable measured CTR"
    )
    st.caption(
        "CTR comparisons require actual impressions and at least three comparable videos. "
        "If that evidence is unavailable, the scan uses measured views or labels results as a "
        "limited-evidence shortlist."
    )
    if request_text:
        if not recent_ids:
            assistant_summary = (
                f"I checked uploads dated {start_date.isoformat()} through "
                f"{end_date.isoformat()}, but no uploads in the selected catalogue "
                "were published during that period. Try a wider date range."
            )
        elif not measured_recent:
            assistant_summary = (
                f"I checked {len(recent_ids)} recent uploads from "
                f"{start_date.isoformat()} through {end_date.isoformat()}, but YouTube "
                "returned no measured view rows. I didn't invent a ranking."
            )
        elif candidates:
            first_titles = "; ".join(
                str(item.get("title", "(untitled)"))
                for item in candidates[:3]
            )
            ctr_message = (
                "Measured CTR was available for some candidates."
                if ctr_available else
                "Reliable CTR was unavailable; the shortlist uses views/peer data instead."
            )
            assistant_summary = (
                f"I checked {len(recent_ids)} uploads published between "
                f"{start_date.isoformat()} and {end_date.isoformat()} and found "
                f"{len(candidates)} metadata-review candidate(s). "
                f"{ctr_message} First candidates: {first_titles}. "
                "Open Find videos to review the evidence, then optimize one video at a time."
            )
        else:
            assistant_summary = (
                f"I checked {len(recent_ids)} uploads published between "
                f"{start_date.isoformat()} and {end_date.isoformat()}, but the measured "
                "data did not establish a clear underperformer. No estimated CTR was used."
            )
        history = st.session_state.setdefault("strategist_chat_history", [])
        strategist_chat._append_history(history, "assistant", assistant_summary)

    if candidates:
        st.subheader("Action brief for the selected period")
        st.caption(
            "Start with these evidence-backed actions. The signal ranks what to review; "
            "it does not prove that metadata caused the performance gap."
        )
        for index, action in enumerate(content_memory.build_weekly_action_brief(candidates), 1):
            with st.container(border=True):
                st.markdown(f"**{index}. {action['title']}**")
                st.caption(f"{action['signal']} · {action['confidence']}")
                st.write(action["first_action"])
                st.caption(f"Evidence: {action['evidence']}")
                st.caption(f"Measure next: {action['success_metric']}")

    memory_status = st.session_state.get("content_memory_write_status")
    memory_url = st.session_state.get("content_memory_spreadsheet_url")
    memory_error = st.session_state.get("content_memory_write_error")
    if memory_status == "saved" and memory_url:
        st.caption(f"Private Strategist Memory saved · [Open memory spreadsheet]({memory_url})")
        history = st.session_state.get("content_memory_recent_scans", [])
        if history:
            with st.expander("Previous channel reviews"):
                st.dataframe(
                    [
                        {
                            "Saved (UTC)": item.get("saved_at_utc"),
                            "Period": (
                                f"{item.get('period_start', '')} – "
                                f"{item.get('period_end', '')}"
                            ),
                            "Uploads": item.get("uploads_in_window"),
                            "Measured views": item.get("measured_views_count"),
                            "Usable CTR": item.get("usable_ctr_count"),
                            "Candidates": item.get("candidate_count"),
                        }
                        for item in history
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
    elif memory_status == "not_connected":
        st.info(
            "The scan is ready in this session. Connect private Strategist Memory from the "
            "sidebar to keep scan history and approved experiments between sessions."
        )
    elif memory_status == "session_only" and memory_error:
        st.warning(
            "This scan is available in this session, but durable memory was not saved. "
            + memory_error
        )
        st.caption(
            "If the memory token is already authorized, use Retry spreadsheet setup. "
            "Otherwise, connect private Strategist Memory from the sidebar."
        )

    if not ctr_available:
        st.info(
            "Reliable thumbnail CTR was not available for these uploads at the required "
            "impression threshold. I will not estimate it; any candidates below use measured "
            "view counts and clearly stated peer evidence."
        )

    if not recent_ids:
        st.info(
            "No uploads were published during this period within the selected catalogue. "
            "Choose a wider date range or increase the upload comparison limit."
        )
        return
    if not measured_recent:
        st.warning(
            "YouTube Analytics returned no measured view rows for these uploads. "
            "No underperformer can be identified from this report."
        )
        return
    if not candidates:
        st.success(
            "No recent upload had enough measured evidence to stand out as a metadata-review "
            "candidate. That is not proof that every upload is performing well."
        )
        return

    for candidate in candidates:
        with st.container(border=True):
            st.write(candidate.get("title", "(untitled)"))
            st.caption(
                f"Published {candidate.get('published_at', 'date unavailable')[:10]} · "
                f"{candidate.get('format', 'Format unavailable')}"
            )
            views = candidate.get("views")
            impressions = candidate.get("impressions")
            ctr = candidate.get("ctr")
            metrics_line = (
                f"Views: {views:,.0f}" if isinstance(views, (int, float))
                else "Views: unavailable"
            )
            metrics_line += (
                f"   ·   CTR: {ctr:.2f}%"
                if isinstance(ctr, (int, float))
                and isinstance(impressions, (int, float))
                and impressions >= 100
                else "   ·   CTR: unavailable"
            )
            metrics_line += (
                f"   ·   Impressions: {impressions:,.0f}"
                if isinstance(impressions, (int, float))
                else "   ·   Impressions: unavailable"
            )
            st.caption(metrics_line)
            st.markdown(f"**{candidate['opportunity_type']}**")
            st.write(candidate["opportunity_evidence"])
            if st.button(
                "Optimize title, description and tags",
                key=f"optimize_opportunity_{candidate['video_id']}",
                type="primary" if candidate is candidates[0] else "secondary",
                use_container_width=True,
            ):
                st.session_state["pending_optimizer_video_url"] = candidate["url"]
                st.session_state["asl_pending_workspace"] = "Strategist"
                st.rerun()



def _render_studio_export_analysis():
    """Upload, inspect, and analyse Studio exports locally within the current app session."""
    st.subheader("Upload YouTube Studio exports")
    st.caption(
        "Use exported Analytics reports to add real CTR, watch-time, average-viewed and "
        "retention evidence that may not be available in the connected API reports."
    )
    st.caption(
        "Files are analysed in this session and are not saved to Drive, OneDrive, or disk. "
        "Excel/OneDrive files must be downloaded and uploaded here; direct Microsoft account "
        "access is not connected yet. If you choose Do it, only that video's compact metrics, "
        "source label, and date range are sent to the configured AI provider—the raw file is not sent."
    )
    with st.expander("How to export a report from YouTube Studio", expanded=False):
        st.markdown(
            "1. Open [YouTube Studio](https://studio.youtube.com/) and select **Analytics**.\n"
            "2. Open **Advanced mode** or **See more**.\n"
            "3. Choose the video, date range, breakdown and metrics you want.\n"
            "4. Select **Export current view** and download CSV (or TSV if offered).\n\n"
            "Useful reports: Content/video performance with Views, Impressions, CTR, "
            "Average view duration and Average percentage viewed; a daily report for trends; "
            "and the audience-retention report for the video's retention curve."
        )
        st.caption(
            "Official instructions: [YouTube Help — Advanced mode and exports]"
            "(https://support.google.com/youtube/answer/9717005)."
        )

    uploaded_files = st.file_uploader(
        "Choose Studio reports (CSV, TSV, or XLSX)",
        type=["csv", "tsv", "txt", "xlsx"],
        accept_multiple_files=True,
        key="studio_export_uploads",
        help="Select CSV/TSV reports or Excel .xlsx workbooks. Files are parsed in memory.",
        max_upload_size=10,
    )
    button_columns = st.columns([1, 1, 2])
    with button_columns[0]:
        analyze_clicked = st.button(
            "Analyze exports",
            type="primary",
            use_container_width=True,
            disabled=not bool(uploaded_files),
            key="analyze_studio_exports",
        )
    with button_columns[1]:
        clear_clicked = st.button(
            "Clear analysis",
            use_container_width=True,
            disabled=not bool(st.session_state.get("studio_export_reports")),
            key="clear_studio_exports",
        )
    if clear_clicked:
        st.session_state.pop("studio_export_reports", None)
        st.session_state.pop("studio_export_evidence_index", None)
        st.session_state.pop("studio_export_skipped_actions", None)
        st.session_state.pop("studio_export_started_actions", None)
        st.rerun()

    if analyze_clicked and uploaded_files:
        results = []
        errors = []
        for uploaded in uploaded_files:
            try:
                parsed = studio_export_analyzer.parse_studio_export(
                    uploaded.name, uploaded.getvalue()
                )
                results.append(studio_export_analyzer.analyze_studio_export(parsed))
            except studio_export_analyzer.StudioExportError as error:
                errors.append(f"**{uploaded.name}:** {error}")
            except Exception:
                errors.append(
                    f"**{uploaded.name}:** The file couldn't be analysed safely. "
                    "Try exporting it again as CSV/TSV."
                )
        st.session_state["studio_export_reports"] = results
        st.session_state["studio_export_evidence_index"] = (
            studio_export_analyzer.build_evidence_index(results)
        )
        st.session_state.pop("studio_export_skipped_actions", None)
        st.session_state.pop("studio_export_started_actions", None)
        st.session_state["studio_export_upload_errors"] = errors
        st.rerun()

    for error in st.session_state.pop("studio_export_upload_errors", []):
        st.warning(error)

    reports = st.session_state.get("studio_export_reports", [])
    if reports and uploaded_files:
        cached_names = [report.get("filename") for report in reports]
        selected_names = [uploaded.name for uploaded in uploaded_files]
        if cached_names != selected_names:
            st.warning(
                "Your selected files differ from the last analysis. Select **Analyze exports** "
                "to replace the results with the current upload selection."
            )
    elif reports and not uploaded_files:
        st.caption(
            "Showing the last processed analysis from this session. Upload and analyse another "
            "set, or clear the analysis to remove the displayed results."
        )
    if not reports:
        st.info(
            "Upload one or more YouTube Studio CSV/TSV reports or .xlsx workbooks and select "
            "**Analyze exports**. You can still use the live Analytics dashboard below without uploading anything."
        )
        return

    video_count = sum(report.get("video_count", 0) for report in reports)
    st.caption(
        f"{len(reports)} report(s) analysed · {video_count} video/report matches · "
        "source files are not saved by this app"
    )

    st.caption(
        "Each report stays separate: exported date ranges can overlap, so the app does not "
        "automatically combine totals from different files."
    )

    priority_order = {"High": 0, "Review": 1, "Opportunity": 2}
    export_actions = []
    for report in reports:
        for recommendation in report.get("recommendations", []):
            priority = recommendation.get("priority", "Context")
            video_id = recommendation.get("video_id")
            video_title = recommendation.get("video_title")
            if priority not in priority_order or not (video_id or video_title):
                continue
            if video_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)):
                video_id = None
            if not video_id and not str(video_title or "").strip():
                continue
            measured = recommendation.get("measured_metrics") or {}
            reach = (
                measured.get("impressions")
                if measured.get("ctr_percent") is not None
                else measured.get("views")
            )
            try:
                reach = float(reach) if reach is not None else 0.0
            except (TypeError, ValueError):
                reach = 0.0
            action_id = json.dumps(
                [
                    report.get("filename", "Studio export"),
                    video_id or "",
                    str(video_title or ""),
                    recommendation.get("title", ""),
                    recommendation.get("evidence", ""),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            export_actions.append({
                "id": action_id,
                "priority_rank": priority_order[priority],
                "priority": priority,
                "reach": reach,
                "source_file": report.get("filename", "Studio export"),
                "video_id": video_id,
                "video_title": video_title,
                "recommendation": recommendation,
            })

    # Within a priority tier, measured reach breaks ties; this is ordering, not a growth score.
    export_actions.sort(
        key=lambda item: (
            item["priority_rank"],
            -item["reach"],
            item["source_file"],
            item["recommendation"].get("title", ""),
        )
    )
    skipped_actions = set(st.session_state.get("studio_export_skipped_actions", []))
    started_actions = set(st.session_state.get("studio_export_started_actions", []))
    remaining_actions = [
        action for action in export_actions
        if action["id"] not in skipped_actions and action["id"] not in started_actions
    ]
    if remaining_actions:
        action = remaining_actions[0]
        recommendation = action["recommendation"]
        measured = recommendation.get("measured_metrics") or {}
        st.subheader("Your next best action")
        with st.container(border=True):
            st.markdown(f"### {recommendation.get('title', 'Review this video')}")
            st.caption(
                f"{action['priority']} priority · Evidence from {action['source_file']}"
            )
            st.write(recommendation.get("evidence", ""))
            st.markdown("**Suggested next step**")
            st.write(recommendation.get("action", "Review this video using its measured performance."))
            if measured.get("ctr_percent") is not None and measured.get("impressions") is not None:
                median_ctr = measured.get("median_ctr_percent")
                comparison = (
                    f" · upload-set median {median_ctr:.2f}%"
                    if isinstance(median_ctr, (int, float)) else ""
                )
                st.caption(
                    f"Measured: {measured['impressions']:,.0f} impressions · "
                    f"{measured['ctr_percent']:.2f}% CTR{comparison}. "
                    "Used to prioritize review, not to predict a CTR increase."
                )
            elif measured.get("average_view_percentage") is not None:
                median_viewed = measured.get("median_average_view_percentage")
                comparison = (
                    f" · upload-set median {median_viewed:.1f}%"
                    if isinstance(median_viewed, (int, float)) else ""
                )
                st.caption(
                    f"Measured: {measured.get('views', 0):,.0f} views · "
                    f"{measured['average_view_percentage']:.1f}% average viewed{comparison}."
                )
            st.caption(
                "Do it opens Strategist and drafts a proposal. Nothing on YouTube changes until "
                "you review the exact title, description, and tags and select **Approve and apply**."
            )
            do_col, skip_col, rest_col = st.columns([1.5, 1, 2])
            with do_col:
                do_action = st.button(
                    "Do it — build proposal",
                    key="studio_next_action_do_it",
                    type="primary",
                    use_container_width=True,
                )
            with skip_col:
                skip_action = st.button(
                    "Skip",
                    key="studio_next_action_skip",
                    use_container_width=True,
                )
            with rest_col:
                st.caption(
                    f"{len(remaining_actions)} actionable suggestion(s) remain. "
                    "Skip advances to the next one."
                )

            if do_action:
                if action["video_id"]:
                    reference = f"https://www.youtube.com/watch?v={action['video_id']}"
                else:
                    safe_title = str(action["video_title"]).replace("”", "").replace("“", "")
                    reference = f'video title: “{safe_title}”'
                signal = recommendation.get("title", "performance review")
                started_actions.add(action["id"])
                st.session_state["studio_export_started_actions"] = list(started_actions)
                st.session_state["pending_optimizer_video_url"] = reference
                st.session_state["studio_export_pending_instruction"] = (
                    "Create a complete, ready-to-review optimization proposal for this video. "
                    f"The highest-priority measured signal is: {signal}. "
                    f"Evidence: {recommendation.get('evidence', '')} "
                    "Use the attached Studio metrics only as numeric evidence, not as proof of cause. "
                    "Use the video's current metadata and target transcript for content facts. "
                    "Propose a title, description with relevant hashtags, and separate YouTube tags where justified. "
                    "If the signal concerns retention or pacing, explain that metadata alone may not fix it "
                    "and include a practical content-level recommendation; do not pretend the app can edit "
                    "the video or its thumbnail. Do not apply anything. I must approve the exact metadata proposal."
                )
                st.session_state["asl_workspace"] = "Strategist"
                st.rerun()
            if skip_action:
                skipped_actions.add(action["id"])
                st.session_state["studio_export_skipped_actions"] = list(skipped_actions)
                st.rerun()
    elif export_actions:
        st.info("You've acted on or skipped every actionable suggestion in this upload set.")
        if st.button("Review suggestions again", key="studio_reset_skipped_actions"):
            st.session_state.pop("studio_export_skipped_actions", None)
            st.rerun()
    else:
        st.info(
            "I couldn't rank a video-specific action from these files yet. Upload a Content/Reach "
            "report with a video title or ID, Views, Impressions, CTR, Average view duration, and "
            "Average percentage viewed where available. Missing metrics stay unavailable; they are not zero."
        )

    table_rows = []
    for report_index, report in enumerate(reports):
        labels = {
            "retention": "Audience retention",
            "video_performance": "Video performance / reach",
            "daily_trend": "Daily trend",
            "traffic_sources": "Traffic sources",
            "breakdown": "Audience / search breakdown",
            "analytics_table": "Analytics table",
        }
        label = labels.get(report.get("report_type"), "Analytics table")
        title = f"{report['filename']} · {label} · {report['record_count']} rows"
        with st.expander(title, expanded=False):
            source_metrics = ", ".join(report.get("fields", []))
            source_sheet = report.get("source_sheet")
            sheet_note = f" · Worksheet: {source_sheet}" if source_sheet else ""
            st.caption(f"Detected report type: {label} · Header row: {report['header_row']}{sheet_note}")
            st.caption(f"Recognized fields: {source_metrics or 'none'}")
            if report.get("ignored_headers"):
                st.caption(
                    "Other columns not interpreted automatically: "
                    + ", ".join(report["ignored_headers"][:16])
                )

            if report.get("summary"):
                summary_items = list(report["summary"].items())
                summary_columns = st.columns(min(4, len(summary_items)))
                for column, (metric, value) in zip(summary_columns, summary_items):
                    human_label = {
                        "views": "Views",
                        "watch_time_minutes": "Watch time (min)",
                        "impressions": "Impressions",
                        "likes": "Likes",
                        "comments": "Comments",
                        "shares": "Shares",
                        "subscribers_gained": "Subscribers gained",
                        "subscribers_lost": "Subscribers lost",
                    }.get(metric, metric.replace("_", " ").title())
                    column.metric(human_label, f"{value:,.0f}")

            retention_rows = report.get("retention_rows", [])
            absolute_curve = [
                {
                    "Elapsed time": row.get("elapsed_label") or str(row.get("elapsed_seconds")),
                    "Audience retention (%)": row.get("retention_percent"),
                }
                for row in retention_rows
                if isinstance(row.get("retention_percent"), (int, float))
            ]
            relative_curve = [
                {
                    "Elapsed time": row.get("elapsed_label") or str(row.get("elapsed_seconds")),
                    "Relative retention index": row.get("relative_retention_index"),
                }
                for row in retention_rows
                if isinstance(row.get("relative_retention_index"), (int, float))
            ]
            if absolute_curve:
                st.markdown("**Audience retention curve**")
                st.line_chart(
                    absolute_curve,
                    x="Elapsed time",
                    y="Audience retention (%)",
                    use_container_width=True,
                )
            if relative_curve:
                st.markdown("**Relative retention index**")
                st.line_chart(
                    relative_curve,
                    x="Elapsed time",
                    y="Relative retention index",
                    use_container_width=True,
                )
                st.caption(
                    "Relative retention is an index against typical viewing behaviour, not "
                    "the literal percentage of your viewers still watching."
                )

            videos = report.get("video_rows", [])
            if videos:
                st.markdown("**Video-level metrics found**")
                visible_metrics = (
                    ("video_title", "Video"),
                    ("video_id", "Video ID"),
                    ("views", "Views"),
                    ("watch_time_minutes", "Watch time (min)"),
                    ("average_view_duration_seconds", "Avg. view duration (sec)"),
                    ("average_view_percentage", "Avg. viewed (%)"),
                    ("impressions", "Impressions"),
                    ("ctr_percent", "CTR (%)"),
                    ("subscribers_gained", "Subs gained"),
                    ("subscribers_lost", "Subs lost"),
                    ("date_start", "From"),
                    ("date_end", "Through"),
                )
                selected_metrics = [
                    (key, label) for key, label in visible_metrics
                    if any(row.get(key) is not None for row in videos)
                ]
                st.dataframe(
                    [
                        {
                            label: (
                                f"{row[key]:,.2f}" if isinstance(row.get(key), float)
                                else f"{row[key]:,}" if isinstance(row.get(key), (int, float))
                                else row.get(key)
                            )
                            for key, label in selected_metrics
                        }
                        for row in videos
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
                table_rows.extend(
                    {
                        "Source file": report["filename"],
                        "Video": row.get("video_title"),
                        "Video ID": row.get("video_id"),
                        "Views": row.get("views"),
                        "Watch time (min)": row.get("watch_time_minutes"),
                        "Avg view duration (sec)": row.get("average_view_duration_seconds"),
                        "Avg viewed (%)": row.get("average_view_percentage"),
                        "Impressions": row.get("impressions"),
                        "CTR (%)": row.get("ctr_percent"),
                        "Subs gained": row.get("subscribers_gained"),
                        "Subs lost": row.get("subscribers_lost"),
                        "From": row.get("date_start"),
                        "Through": row.get("date_end"),
                    }
                    for row in videos
                )
                st.markdown("**Send a selected video to Strategist**")
                for row_index, row in enumerate(videos[:10]):
                    action_columns = st.columns([4, 1])
                    action_columns[0].markdown(
                        f"**{row.get('video_title') or row.get('video_id') or 'Video row'}**"
                    )
                    facts = []
                    if isinstance(row.get("views"), (int, float)):
                        facts.append(f"{row['views']:,.0f} views")
                    if isinstance(row.get("ctr_percent"), (int, float)):
                        facts.append(f"{row['ctr_percent']:.2f}% CTR")
                    if isinstance(row.get("average_view_percentage"), (int, float)):
                        facts.append(f"{row['average_view_percentage']:.1f}% average viewed")
                    if facts:
                        action_columns[0].caption(" · ".join(facts))
                    if action_columns[1].button(
                        "Review",
                        key=f"studio_export_review_{report_index}_{row_index}",
                        use_container_width=True,
                    ):
                        video_id = row.get("video_id")
                        video_title = str(row.get("video_title") or "")
                        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id)):
                            reference = f"https://www.youtube.com/watch?v={video_id}"
                        elif video_title:
                            safe_title = video_title.replace("”", "").replace("“", "")
                            reference = f'video title: “{safe_title}”'
                        else:
                            st.warning(
                                "This report row has neither a video ID nor a title that can "
                                "identify an owned upload. Use a video URL in Strategist instead."
                            )
                            continue
                        st.session_state["pending_optimizer_video_url"] = reference
                        st.session_state["studio_export_pending_instruction"] = (
                            "Review the title, description, relevant hashtags and separate tags "
                            "using the uploaded Studio performance evidence attached to this video. "
                            "Explain what the metrics support and what they cannot prove. Do not "
                            "claim that a metric proves a metadata or thumbnail change caused it."
                        )
                        st.session_state["asl_workspace"] = "Strategist"
                        st.rerun()

    if table_rows:
        output = io.StringIO()
        fields = list(table_rows[0])
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([
            {
                key: studio_export_analyzer.safe_csv_export_value(value)
                for key, value in row.items()
            }
            for row in table_rows
        ])
        st.download_button(
            "Download analyzed video table (CSV)",
            data=output.getvalue().encode("utf-8-sig"),
            file_name="studio_export_analysis.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_studio_export_analysis",
        )
    st.caption(
        "Analysis uses only detected columns. Missing CTR is not inferred or treated as zero, and "
        "correlation is not treated as causation. Uploaded workbooks and extracted metrics stay in "
        "this app session; the app does not save them back to Google Drive or OneDrive."
    )


def _render_workspace_connection_gate(
    page, secrets, subject, api_client_error=None, show_heading=True
):
    """Keep navigation visible while the owner completes or repairs YouTube authorization."""
    copy = {
        "Strategist": (
            "Connect your channel to review a video's current metadata and prepare an "
            "approval-gated title, description, hashtag, and tag proposal."
        ),
        "Find videos": (
            "Connect your channel to compare recent uploads and identify which videos "
            "have enough measured evidence to deserve a metadata review."
        ),
        "Analytics": (
            "Connect your channel to load measured views, watch time, available reach "
            "metrics, traffic sources, and upload-level performance."
        ),
    }
    if show_heading:
        st.markdown(
            '<div class="asl-page-heading"><div class="asl-page-title">'
            + html.escape(page)
            + '</div><p class="asl-page-subtitle">'
            + html.escape(copy.get(page, copy["Strategist"]))
            + "</p></div>",
            unsafe_allow_html=True,
        )
    with st.container(border=True):
        st.subheader("Connect YouTube to continue")
        st.write(
            "You are signed in, but Google sign-in and YouTube API permission are separate. "
            "Your workspace is available now; channel data and metadata actions unlock after "
            "the YouTube permission is completed."
        )
        if api_client_error:
            st.warning(api_client_error)
        _start_youtube_authorization(secrets, subject)
        st.caption(
            "Authorization opens in a new tab. Keep this workspace open. After you finish "
            "Google consent, return here and refresh the connection status."
        )
        if st.button(
            "Refresh connection status",
            key="refresh_youtube_connection",
            use_container_width=True,
        ):
            st.rerun()


def main():
    secrets = _read_secrets()
    if not getattr(st.user, "is_logged_in", False):
        st.markdown(
            '<div class="asl-page-heading"><div class="asl-page-title">'
            'AI Simplified Lab</div><p class="asl-page-subtitle">'
            'Your private YouTube optimization workspace.</p></div>',
            unsafe_allow_html=True,
        )
        st.write(
            "Review your channel, find videos worth improving, and prepare safer metadata "
            "changes—with your approval required before anything is updated."
        )
        feature_columns = st.columns(3)
        with feature_columns[0]:
            st.markdown("**Strategist**")
            st.caption("Improve titles, descriptions, hashtags, and separate YouTube tags.")
        with feature_columns[1]:
            st.markdown("**Find videos**")
            st.caption("Surface uploads worth reviewing using measured channel evidence.")
        with feature_columns[2]:
            st.markdown("**Analytics**")
            st.caption("Understand views, watch time, audience response, and available reach data.")
        st.info(
            "Sign in with the configured, verified Google account. Channel data stays hidden "
            "until you sign in and connect YouTube."
        )
        login_columns = st.columns([1, 2, 1])
        with login_columns[1]:
            st.button(
                "Sign in with Google",
                on_click=st.login,
                type="primary",
                use_container_width=True,
            )
        return

    subject = _owner_identity(secrets)
    st.session_state["oauth_owner_subject"] = subject
    try:
        token_store = oauth_token_store.OAuthTokenStore(
            secrets["security"]["state_secret"]
        )
    except (TypeError, ValueError):
        token_store = None
        st.session_state["oauth_token_store_error"] = (
            "Encrypted shared authorization storage could not be initialized. "
            "Permissions may be limited to one browser tab."
        )
    _restore_shared_oauth_credentials(token_store, subject)

    pending_workspace = st.session_state.pop("asl_pending_workspace", None)
    if pending_workspace in {"Strategist", "Find videos", "Analytics"}:
        st.session_state["asl_workspace"] = pending_workspace

    if st.session_state.pop("oauth_connected_message", False):
        st.success("YouTube authorization connected for this session.")
    youtube_auth_error = st.session_state.pop("youtube_auth_error", None)
    if youtube_auth_error:
        st.error(youtube_auth_error)
    memory_auth_error = st.session_state.pop("memory_auth_error", None)
    if memory_auth_error:
        st.error("Private memory authorization failed: " + memory_auth_error)

    _handle_youtube_callback(secrets, subject, token_store)
    token_store_error = st.session_state.pop("oauth_token_store_error", None)
    if token_store_error:
        st.warning(token_store_error)
    credentials = _credentials_from_session(
        "youtube_credentials_json", connector.SCOPES, "YouTube", token_store
    )
    memory_credentials = _credentials_from_session(
        "memory_credentials_json", MEMORY_SCOPES, "private memory", token_store
    )
    _verify_private_memory_workbook(memory_credentials)
    if st.session_state.pop("memory_connected_message", False):
        memory_url = st.session_state.get("content_memory_spreadsheet_url")
        st.success("Private Strategist Memory connected and spreadsheet verified.")
        if memory_url:
            st.markdown(f"[Open your Strategist Memory spreadsheet]({memory_url})")
    auth_warning = st.session_state.pop("youtube_auth_warning", None)
    if auth_warning:
        st.warning(auth_warning)
    memory_auth_warning = st.session_state.pop("memory_auth_warning", None)
    if memory_auth_warning:
        st.warning(memory_auth_warning)

    with st.sidebar:
        st.markdown(
            '<div class="asl-sidebar-brand">✳ AI Simplified Lab</div>',
            unsafe_allow_html=True,
        )
        if st.session_state.get("asl_workspace", "Strategist") == "Strategist" and st.button(
            "New chat", key="asl_new_chat", use_container_width=True
        ):
            for key in (
                "strategist_target_reference",
                "strategist_chat_history",
                "strategist_chat_proposal",
                "strategist_chat_digest",
                "strategist_pending_instruction",
                "channel_analysis_request",
                "opportunity_auto_run",
            ):
                st.session_state.pop(key, None)
            st.session_state["asl_pending_workspace"] = "Strategist"
            st.rerun()

    api_client_error = None
    youtube = analytics = reporting = None
    if credentials:
        st.session_state["session_access_warning_shown"] = True
        try:
            youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
            analytics = build(
                "youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False
            )
            reporting = build(
                "youtubereporting", "v1", credentials=credentials, cache_discovery=False
            )
        except (ValueError, HttpError):
            api_client_error = (
                "Google's YouTube API clients could not be initialized. "
                "Refresh the connection or reconnect YouTube."
            )

    pending_video_url = (
        st.session_state.get("pending_optimizer_video_url") if youtube else None
    )
    if pending_video_url:
        st.session_state.pop("pending_optimizer_video_url", None)
        try:
            _, load_message = strategist_chat._load_conversation_videos(
                youtube, secrets, pending_video_url
            )
            if load_message:
                st.session_state["optimizer_open_error"] = load_message
            else:
                default_optimizer_instruction = (
                    "Create a proposal to improve this video's title, description, "
                    "relevant hashtags, and tags. Use only facts supported by the target "
                    "transcript and existing metadata. Do not promise CTR or invent chapters. "
                    "Keep the title and description natural and useful for a viewer."
                )
                st.session_state["strategist_pending_instruction"] = (
                    st.session_state.pop(
                        "studio_export_pending_instruction", default_optimizer_instruction
                    )
                )
        except Exception:
            st.session_state["optimizer_open_error"] = (
                "Could not open this video for optimization. Check YouTube access and try again."
            )
        st.session_state["asl_workspace"] = "Strategist"

    if st.session_state.get("asl_workspace") not in {"Strategist", "Find videos", "Analytics"}:
        st.session_state["asl_workspace"] = "Strategist"
    page = st.session_state["asl_workspace"]
    st.markdown(
        '<div class="asl-workspace-nav"><div class="asl-workspace-nav-label">'
        'WORKSPACES</div></div>',
        unsafe_allow_html=True,
    )
    nav_columns = st.columns(3, gap="small")
    for column, workspace in zip(nav_columns, ("Strategist", "Find videos", "Analytics")):
        with column:
            if st.button(
                workspace,
                key=f"asl_workspace_nav_{workspace.casefold().replace(' ', '_')}",
                type="primary" if page == workspace else "secondary",
                use_container_width=True,
            ):
                st.session_state["asl_workspace"] = workspace
                st.rerun()

    with st.sidebar:
        st.caption("YouTube: Connected" if credentials else "YouTube: Not connected")
        with st.expander("Connections", expanded=False):
            if credentials:
                st.success("YouTube access connected.")
                if st.button(
                    "Disconnect YouTube",
                    key="asl_sidebar_disconnect",
                    use_container_width=True,
                ):
                    try:
                        if token_store is not None:
                            token_store.delete_credentials(subject, "youtube")
                    except Exception:
                        st.session_state["oauth_token_store_error"] = (
                            "YouTube was disconnected in this tab, but the shared token "
                            "store could not be updated. Reopen the app and verify its connection state."
                        )
                    for key in (
                        "youtube_credentials_json",
                        "dashboard_data",
                        "opportunity_data",
                        "opportunity_data_key",
                        "dashboard_data_key",
                    ):
                        st.session_state.pop(key, None)
                    st.rerun()
            else:
                st.caption(
                    "Connect YouTube from the selected workspace. It stays visible while "
                    "Google authorization runs in a separate tab."
                )

            st.divider()
            if memory_credentials:
                if st.session_state.get("content_memory_store_verified"):
                    st.caption("Private Strategist Memory: connected and verified")
                    memory_url = st.session_state.get("content_memory_spreadsheet_url")
                    if memory_url:
                        st.link_button(
                            "Open memory spreadsheet",
                            memory_url,
                            use_container_width=True,
                        )
                else:
                    st.warning(
                        "Private memory is authorized, but its spreadsheet needs attention."
                    )
                    memory_setup_error = st.session_state.get("memory_store_setup_error")
                    if memory_setup_error:
                        st.caption(memory_setup_error)
                    if st.button("Retry spreadsheet setup", key="retry_memory_setup"):
                        st.session_state["memory_store_setup_attempted"] = False
                        st.rerun()
                if st.button(
                    "Disconnect private memory",
                    key="asl_memory_disconnect",
                    use_container_width=True,
                ):
                    try:
                        if token_store is not None:
                            token_store.delete_credentials(subject, "memory")
                    except Exception:
                        st.session_state["oauth_token_store_error"] = (
                            "Private memory was disconnected in this tab, but the shared token "
                            "store could not be updated. Reopen the app and verify its connection state."
                        )
                    for key in (
                        "memory_credentials_json",
                        "memory_store_setup_attempted",
                        "content_memory_store_verified",
                        "content_memory_spreadsheet_url",
                        "content_memory_recent_scans",
                        "memory_connection_pending_verification",
                        "memory_connected_message",
                        "memory_store_setup_error",
                    ):
                        st.session_state.pop(key, None)
                    st.session_state["content_memory_write_status"] = "not_connected"
                    st.rerun()
            elif credentials:
                st.caption(
                    "Optional: use Google Sheets to keep scan history and approved metadata "
                    "experiments beyond this session."
                )
                memory_url = st.session_state.get("content_memory_spreadsheet_url")
                if memory_url:
                    st.link_button(
                        "Open last memory spreadsheet",
                        memory_url,
                        use_container_width=True,
                    )
                _start_memory_authorization(secrets, subject)
                st.caption(
                    "Uses a separate, limited Google permission. Conversation history "
                    "remains session-only."
                )
            else:
                st.caption(
                    "Private Strategist Memory is optional and becomes useful after "
                    "YouTube is connected."
                )

        with st.expander("Account", expanded=False):
            st.caption(st.user.email)
            if st.button("Sign out", key="asl_sidebar_signout", use_container_width=True):
                st.logout()

    if not credentials or api_client_error:
        if page == "Analytics":
            st.markdown(
                '<div class="asl-page-heading"><div class="asl-page-title">'
                'Channel analytics</div><p class="asl-page-subtitle">'
                'Upload your own Studio reports for deeper, evidence-based analysis.</p></div>',
                unsafe_allow_html=True,
            )
            _render_studio_export_analysis()
            _render_workspace_connection_gate(
                page, secrets, subject, api_client_error=api_client_error,
                show_heading=False,
            )
        else:
            _render_workspace_connection_gate(
                page, secrets, subject, api_client_error=api_client_error
            )
        return

    optimizer_error = st.session_state.pop("optimizer_open_error", None)
    if optimizer_error:
        st.error(optimizer_error)

    experiment_memory_error = st.session_state.pop("content_memory_last_error", None)
    if experiment_memory_error:
        st.warning(
            "The YouTube metadata update was verified, but the experiment was not saved "
            "to durable memory. " + experiment_memory_error
        )

    if page == "Strategist":
        st.markdown(
            '<div class="asl-page-heading"><div class="asl-page-title">'
            'YouTube Strategist</div><p class="asl-page-subtitle">'
            'A focused workspace for improving one video at a time.</p></div>',
            unsafe_allow_html=True,
        )
        strategist_chat.render_chat(
            youtube,
            secrets,
            credentials=credentials,
            memory_credentials=memory_credentials,
        )
    elif page == "Find videos":
        _render_recent_opportunities(
            youtube,
            analytics,
            reporting,
            credentials,
            memory_credentials,
            secrets,
            subject,
        )
    else:
        st.markdown(
            '<div class="asl-page-heading"><div class="asl-page-title">'
            'Channel analytics</div><p class="asl-page-subtitle">'
            'Measured channel performance and reporting coverage.</p></div>',
            unsafe_allow_html=True,
        )
        st.caption("Read-only performance reports and optimization evidence.")
        _render_studio_export_analysis()
        yesterday = date.today() - timedelta(days=1)
        default_start, default_end = youtube_dashboard.default_date_range()
        with st.container():
            filter_columns = st.columns([1.2, 1.2, 1, 1])
            with filter_columns[0]:
                start_date = st.date_input(
                    "From",
                    value=default_start,
                    max_value=yesterday,
                    key="dashboard_start_date",
                )
            with filter_columns[1]:
                end_date = st.date_input(
                    "Through",
                    value=default_end,
                    max_value=yesterday,
                    key="dashboard_end_date",
                )
            with filter_columns[2]:
                video_limit = st.selectbox(
                    "Recent uploads",
                    options=(25, 50, 100, 200, 250),
                    index=2,
                )
            with filter_columns[3]:
                st.caption(" ")
                refresh_reports = st.button(
                    "Refresh reports",
                    use_container_width=True,
                )
        if refresh_reports:
            st.session_state.pop("dashboard_data_key", None)
        if start_date > end_date:
            st.error("Choose a start date on or before the end date.")
            return
        if end_date >= date.today():
            st.error("YouTube Analytics data is queried only through yesterday.")
            return

        data_key = (start_date.isoformat(), end_date.isoformat(), video_limit)
        if st.session_state.get("dashboard_data_key") != data_key:
            with st.spinner("Fetching channel reports and recent video performance..."):
                try:
                    dashboard_data = youtube_dashboard.load_dashboard_data(
                        youtube,
                        analytics,
                        start_date,
                        end_date,
                        video_limit=video_limit,
                        reporting=reporting,
                        credentials=credentials,
                    )
                except HttpError as error:
                    status = getattr(error.resp, "status", "unknown")
                    if status == 403:
                        st.error(
                            "Google denied an Analytics or Data API request (HTTP 403). "
                            "Check the authorized Google account, API enablement, scopes, "
                            "and quota."
                        )
                    else:
                        st.error(f"YouTube API request failed (HTTP {status}).")
                    return
                except (
                    GoogleAuthError,
                    httplib2.HttpLib2Error,
                    OSError,
                    requests.RequestException,
                ):
                    st.error(
                        "The Google API request failed. Check the network and reconnect "
                        "YouTube if the authorization expired."
                    )
                    return
                except (
                    connector.VideoNotFoundError,
                    connector.MetadataValidationError,
                    RuntimeError,
                    ValueError,
                ) as error:
                    st.error(str(error))
                    return
            st.session_state["dashboard_data"] = dashboard_data
            st.session_state["dashboard_data_key"] = data_key
            st.session_state.pop("pending_metadata_proposal", None)
            st.session_state.pop("requested_video_row", None)

        data = st.session_state["dashboard_data"]
        st.caption(
            f"Channel `{data['channel_id']}` · {data['period']['start']} through "
            f"{data['period']['end']}"
        )
        if data["summary"] is None:
            st.warning(f"Channel summary: {data['summary_status']}")
        else:
            summary = data["summary"]
            gained = summary.get("subscribersGained")
            lost = summary.get("subscribersLost")
            net_subscribers = (
                gained - lost
                if isinstance(gained, (int, float)) and isinstance(lost, (int, float))
                else None
            )
            primary_metrics = (
                ("Views", summary.get("views")),
                ("Watch time (minutes)", summary.get("estimatedMinutesWatched")),
                ("Avg. view duration (seconds)", summary.get("averageViewDuration")),
                ("Net subscribers", net_subscribers),
            )
            metric_columns = st.columns(4)
            for column, (label, value) in zip(metric_columns, primary_metrics):
                column.metric(label, _display_metric(value))
            with st.expander("More channel metrics"):
                detail_metrics = (
                    "subscribersGained", "subscribersLost", "likes", "comments", "shares"
                )
                detail_columns = st.columns(len(detail_metrics))
                for column, metric in zip(detail_columns, detail_metrics):
                    column.metric(
                        youtube_dashboard.METRIC_LABELS[metric],
                        _display_metric(summary.get(metric)),
                    )

        with st.expander("Analytics coverage and limitations"):
            st.write("Thumbnail reach report:", data["reach_status"])
            st.write(
                "The YouTube Reporting API reach report supplies thumbnail impressions "
                "and CTR independently of Analytics reports.query. Daily reports are "
                "asynchronous; first availability may take up to 48 hours. Reach values "
                "sum returned video/date rows only; missing rows are not converted to zero."
            )
            st.write("Daily report:", data["daily_status"])
            st.write("Traffic-source report:", data["traffic_status"])
            st.write("Video catalogue:", data["catalogue_note"])
            st.write(
                "Format is inferred from duration only: <=180 seconds is a short-duration "
                "candidate, not a confirmed Short; video orientation is not exposed here."
            )
            for warning in data["api_warnings"]:
                st.warning(warning)

        st.subheader("Channel views by day")
        if data["daily_rows"]:
            st.line_chart({
                row["day"]: row["views"]
                for row in data["daily_rows"]
                if row.get("day") is not None and row.get("views") is not None
            })
        else:
            st.caption(data["daily_status"])

        with st.expander("Channel traffic sources"):
            if data["traffic_sources"]:
                st.dataframe(data["traffic_sources"], hide_index=True, use_container_width=True)
            else:
                st.caption(data["traffic_status"])

        st.subheader("Video and short-duration candidate performance")
        format_filter = st.selectbox(
            "Format filter",
            options=(
                "All",
                "Short-duration candidate (<=180 sec)",
                "Long-form (>180 sec)",
            ),
        )
        visible_rows = _visible_video_rows(data["video_rows"], format_filter)
        table_columns = (
            "title",
            "published_at",
            "format",
            "views",
            "estimatedMinutesWatched",
            "averageViewDuration",
            "impressions",
            "ctr",
        )
        st.dataframe(
            [
                {
                    column: (
                        f"{row[column]:,}"
                        if column == "impressions" and row.get(column) is not None
                        else f"{row[column]:g}%"
                        if column == "ctr" and row.get(column) is not None
                        else "Unavailable"
                        if column in {"impressions", "ctr"}
                        else row.get(column)
                    )
                    for column in table_columns
                }
                for row in visible_rows
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "title": st.column_config.TextColumn("Video"),
                "views": st.column_config.NumberColumn("Views"),
                "published_at": st.column_config.TextColumn("Published"),
                "format": st.column_config.TextColumn("Format (duration-based)"),
                "duration_seconds": st.column_config.NumberColumn("Duration (sec)"),
                "averageViewDuration": st.column_config.NumberColumn(
                    "Avg. view duration (sec)"
                ),
                "estimatedMinutesWatched": st.column_config.NumberColumn(
                    "Watch time (min)"
                ),
                "impressions": st.column_config.TextColumn("Impressions"),
                "ctr": st.column_config.TextColumn("CTR"),
            },
        )
        st.caption(
            "Blank Analytics cells mean the report returned no row for that video/date range; "
            "they are not treated as zero. CTR is YouTube's reported percentage, not an estimate."
        )
        with st.expander("More video metrics: likes, comments, shares, subscribers"):
            st.dataframe(
                [
                    {
                        "Video": row.get("title"),
                        "Likes": row.get("likes"),
                        "Comments": row.get("comments"),
                        "Shares": row.get("shares"),
                        "Subscribers gained": row.get("subscribersGained"),
                        "Subscribers lost": row.get("subscribersLost"),
                    }
                    for row in visible_rows
                ],
                hide_index=True,
                use_container_width=True,
            )
        with st.expander("Impressions and CTR availability by video"):
            for row in visible_rows:
                if row.get("impressions") is None or row.get("ctr") is None:
                    st.markdown(f"**{row['title']}**")
                    if row.get("impressions") is None:
                        st.write("Impressions:", row["impressions_status"])
                    if row.get("ctr") is None:
                        st.write("CTR:", row["ctr_status"])

        st.subheader("Videos worth reviewing")
        ranked = youtube_dashboard.rank_optimization_candidates(data["video_rows"])
        if ranked:
            for candidate in ranked:
                with st.container(border=True):
                    action_columns = st.columns([3, 1])
                    action_columns[0].markdown(
                        f"**[{candidate['title']}]({candidate['url']})**"
                    )
                    action_columns[0].caption(candidate["recommendation_evidence"])
                    if action_columns[1].button(
                        "Review metadata",
                        key=f"analytics_optimize_{candidate['video_id']}",
                        use_container_width=True,
                    ):
                        st.session_state["pending_optimizer_video_url"] = candidate["url"]
                        st.session_state["asl_workspace"] = "Strategist"
                        st.rerun()
        else:
            st.info(
                "No measured video has at least three age- and format-matched peers with a "
                "lower view count than its peer median in this date range. No opportunity "
                "is inferred from missing data."
            )

        if not visible_rows:
            st.info("No videos are available in this catalogue/filter.")

        with st.expander("This session's metadata audit entries"):
            audit_entries = st.session_state.get("session_audit_records", [])
            if audit_entries:
                safe_entries = [
                    {
                        "timestamp": entry["timestamp"],
                        "video_id": entry["video_id"],
                        "url": entry["video_url"],
                        "proposal_id": entry["proposal_id"],
                        "result": entry["result"],
                    }
                    for entry in audit_entries
                ]
                st.dataframe(safe_entries, hide_index=True, use_container_width=True)
            else:
                st.caption("No metadata update has been approved in this session.")
            st.caption(
                "Audit entries are in-memory session state only and are lost when this "
                "session expires. Configure durable private storage before relying on audit history."
            )

        last_result = st.session_state.pop("last_update_result", None)
        if last_result:
            st.success(
                f"Verified update for **{last_result['title']}**: "
                f"{last_result['url']}"
            )


main()
