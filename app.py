"""Main application shell, authentication gate, theme injector, and multipage router."""

import json
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.auth import verify_owner_access, is_demo_mode, set_demo_mode
from core.creator_db import get_db_connection, claim_legacy_owner_records
from core.vault import VaultManager
from core.oauth import test_or_refresh_credentials, create_purpose_state, build_flow, YOUTUBE_SCOPES, MEMORY_SCOPES, get_auth_url
from ui.theme import inject_theme
import oauth_token_store
import dashboard_security

# Page implementations for the 5 Creator Growth Agent destinations
from pages.today import render_today
from pages.workspace import render_workspace
from pages.ideas_production import render_ideas_production
from pages.memory import render_memory
from pages.settings import render_settings


def _read_secrets_safe():
    try:
        return st.secrets
    except Exception:
        return {}


def _handle_oauth_callback(secrets, subject, token_store):
    """Handle incoming OAuth redirect codes from YouTube or Memory flow."""
    query = st.query_params
    code = query.get("code")
    returned_state = query.get("state")
    if not code or not returned_state:
        return

    # Check which flow was initiated
    signing_secret = secrets.get("security", {}).get("state_secret", "")
    for purpose, pending_key in (("youtube", "pending_youtube_oauth_state"), ("memory", "pending_memory_oauth_state")):
        expected = st.session_state.get(pending_key)
        purpose_secret = dashboard_security.oauth_purpose_secret(signing_secret, purpose)
        if dashboard_security.consume_oauth_state(returned_state, expected, subject, purpose_secret):
            try:
                oauth_cfg = secrets.get(f"{purpose}_oauth") or secrets.get("youtube_oauth", {})
                client_id = oauth_cfg.get("client_id", "")
                client_secret = oauth_cfg.get("client_secret", "")
                redirect_uri = oauth_cfg.get("redirect_uri", "")

                scopes = YOUTUBE_SCOPES if purpose == "youtube" else MEMORY_SCOPES
                verifier = st.session_state.get(f"pending_{purpose}_code_verifier")
                if not verifier:
                    verifier = dashboard_security.create_oauth_code_verifier(returned_state, purpose_secret)

                flow = build_flow(
                    client_id,
                    client_secret,
                    redirect_uri,
                    scopes=scopes,
                    state=returned_state,
                    code_verifier=verifier,
                )
                flow.fetch_token(code=code)
                creds = flow.credentials
                creds_json = creds.to_json()
                if token_store:
                    token_store.save_credentials_json(subject, purpose, creds_json)
                st.session_state[f"{purpose}_credentials_json"] = creds_json
                st.toast(f"{purpose.title()} connected successfully!")
            except Exception as e:
                st.error(f"OAuth error: {str(e)}")
            finally:
                st.session_state.pop(pending_key, None)
                st.session_state.pop(f"pending_{purpose}_code_verifier", None)
                query.clear()
                st.rerun()


def main():
    st.set_page_config(
        page_title="YouTube Strategist",
        page_icon="▶",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Inject visual styling
    inject_theme()

    secrets = _read_secrets_safe()
    is_demo = is_demo_mode()

    # Access Gate
    authorized, subject, error_msg = verify_owner_access(secrets)

    # Signed-out or unauthorized visitor landing
    if not authorized:
        st.markdown(
            """
            <div style="text-align: center; max-width: 650px; margin: 40px auto;">
                <div style="display: inline-block; background: #FF1E44; color: white; width: 44px; height: 44px; border-radius: 12px; font-size: 22px; font-weight: 900; line-height: 44px; margin-bottom: 16px;">▶</div>
                <h1 style="font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 8px;">YouTube Strategist</h1>
                <p style="color: #94A3B8; font-size: 1.05rem; line-height: 1.6; margin-bottom: 24px;">
                    A private, owner-only AI co-pilot for serious YouTube creators. Reads real analytics and Studio retention data, identifies the ONE most valuable packaging opportunity, and updates YouTube only after explicit approval.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            st.markdown("### Channel Owner Sign-in")
            st.caption("Sign in with your configured Google Account to access channel telemetry and metadata co-pilot.")
            if error_msg:
                st.error(error_msg)

            col1, col2 = st.columns([1, 1])
            with col1:
                # Use st.login if configured with streamlit[auth]
                try:
                    st.button("Sign in with Google", type="primary", on_click=st.login, use_container_width=True)
                except Exception:
                    st.caption("Streamlit auth provider not configured in local environment.")
            with col2:
                if st.button("Explore Demo Mode (No Sign-in)", use_container_width=True):
                    st.session_state["demo_mode"] = True
                    st.rerun()
        return

    # Claim legacy owner records if authorized owner is logged in
    if authorized and subject and not is_demo:
        try:
            claim_legacy_owner_records(subject)
        except Exception:
            pass

    # Vault & Token Management
    state_secret = secrets.get("security", {}).get("state_secret", "dummy-secret-for-demo-mode-at-least-32-chars")
    token_store = None
    if len(state_secret) >= 32:
        try:
            token_store = oauth_token_store.OAuthTokenStore(
                state_secret,
                connection_factory=lambda: get_db_connection(secrets),
            )
        except Exception:
            token_store = None

    # Handle incoming OAuth callback
    if subject and token_store and not is_demo:
        _handle_oauth_callback(secrets, subject, token_store)

    # Hydrate stored credentials
    yt_creds = None
    memory_creds = None
    if subject and token_store and not is_demo:
        yt_json = st.session_state.get("youtube_credentials_json")
        if not yt_json:
            yt_rec = token_store.get_credentials_record(subject, "youtube")
            if yt_rec.get("status") == "active":
                yt_json = yt_rec.get("credentials_json")
                st.session_state["youtube_credentials_json"] = yt_json

        if yt_json:
            try:
                raw_creds = Credentials.from_authorized_user_info(json.loads(yt_json))
                yt_creds, status = test_or_refresh_credentials(raw_creds)
                if status == "needs_reconnect":
                    st.session_state["youtube_needs_reconnect"] = True
                else:
                    st.session_state.pop("youtube_needs_reconnect", None)
            except Exception:
                st.session_state["youtube_needs_reconnect"] = True

        mem_json = st.session_state.get("memory_credentials_json")
        if not mem_json:
            mem_rec = token_store.get_credentials_record(subject, "memory")
            if mem_rec.get("status") == "active":
                mem_json = mem_rec.get("credentials_json")
                st.session_state["memory_credentials_json"] = mem_json
        if mem_json:
            try:
                raw_mem = Credentials.from_authorized_user_info(json.loads(mem_json))
                memory_creds, _ = test_or_refresh_credentials(raw_mem)
            except Exception:
                pass

    # Build API clients
    youtube = analytics = reporting = None
    if yt_creds:
        try:
            youtube = build("youtube", "v3", credentials=yt_creds, cache_discovery=False)
            analytics = build("youtubeAnalytics", "v2", credentials=yt_creds, cache_discovery=False)
            reporting = build("youtubereporting", "v1", credentials=yt_creds, cache_discovery=False)
        except Exception as e:
            st.sidebar.warning(f"YouTube client notice: {str(e)}")

    # Multipage Navigation Setup: 5 Core Creator Growth Destinations
    def show_today():
        render_today(
            youtube=youtube,
            analytics=analytics,
            reporting=reporting,
            credentials=yt_creds,
            secrets=secrets,
            subject=subject,
            memory_creds=memory_creds,
        )

    def show_workspace():
        render_workspace(youtube=youtube, secrets=secrets, subject=subject, memory_creds=memory_creds)

    def show_ideas():
        render_ideas_production(youtube=youtube, secrets=secrets, subject=subject)

    def show_memory():
        render_memory(memory_credentials=memory_creds, subject=subject)

    def show_connections():
        render_settings(secrets=secrets, subject=subject, youtube_creds=yt_creds, memory_creds=memory_creds, token_store=token_store)

    page_today = st.Page(
        show_today,
        title="Today",
        icon="⚡",
        url_path="today",
        default=True,
    )
    page_workspace = st.Page(
        show_workspace,
        title="Agent Workspace",
        icon="🛠️",
        url_path="workspace",
    )
    page_ideas = st.Page(
        show_ideas,
        title="Ideas & Production",
        icon="💡",
        url_path="ideas",
    )
    page_memory = st.Page(
        show_memory,
        title="Memory",
        icon="🧠",
        url_path="memory",
    )
    page_connections = st.Page(
        show_connections,
        title="Connections",
        icon="⚙️",
        url_path="connections",
    )

    pg = st.navigation({
        "Operating Assistant": [page_today, page_workspace, page_ideas],
        "Intelligence & Settings": [page_memory, page_connections],
    })

    # Programmatic destination routing support
    nav_dest = st.session_state.pop("nav_destination", None)
    if nav_dest:
        dest_map = {
            "today": page_today,
            "home": page_today,
            "workspace": page_workspace,
            "agent_workspace": page_workspace,
            "strategist": page_workspace,
            "pages/strategist.py": page_workspace,
            "ideas": page_ideas,
            "production": page_ideas,
            "memory": page_memory,
            "pages/memory_experiments.py": page_memory,
            "connections": page_connections,
            "settings": page_connections,
            "pages/settings.py": page_connections,
        }
        target_page = dest_map.get(nav_dest)
        if target_page:
            try:
                st.switch_page(target_page)
            except Exception:
                pass

    # Sidebar branding & quick status
    with st.sidebar:
        st.markdown(
            """
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px;">
                <div style="background: #FF1E44; color: white; width: 30px; height: 30px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 14px;">▶</div>
                <div style="font-weight: 800; font-size: 1.05rem; letter-spacing: -0.02em;">YouTube Strategist</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Connection Controls Card in Sidebar
        with st.container(border=True):
            st.markdown("<div style='font-size:0.8rem; font-weight:700; color:#94A3B8; text-transform:uppercase; margin-bottom:8px;'>Integrations & Access</div>", unsafe_allow_html=True)
            
            # YouTube Status & Connect Button
            if yt_creds:
                st.markdown('<span class="status-pill status-connected">● YouTube Connected</span>', unsafe_allow_html=True)
                if st.button("Disconnect YouTube", key="sb_disconnect_yt", use_container_width=True):
                    if token_store and subject:
                        token_store.delete_credentials(subject, "youtube")
                    st.session_state.pop("youtube_credentials_json", None)
                    st.rerun()
            else:
                if is_demo:
                    st.markdown('<span class="status-pill status-connected">● Demo Mode</span>', unsafe_allow_html=True)
                elif st.session_state.get("youtube_needs_reconnect"):
                    st.markdown('<span class="status-pill status-reconnect">● Reconnect Needed</span>', unsafe_allow_html=True)
                else:
                    st.markdown('<span class="status-pill status-disconnected">● YouTube Disconnected</span>', unsafe_allow_html=True)
                
                # Direct Connect Button
                yt_auth_url, yt_err = get_auth_url("youtube", subject or "demo-owner-subject-12345", secrets)
                if yt_auth_url:
                    btn_text = "🔄 Reconnect YouTube" if st.session_state.get("youtube_needs_reconnect") else "▶ Connect YouTube"
                    st.link_button(btn_text, yt_auth_url, type="primary", use_container_width=True)
                elif yt_err and not is_demo:
                    st.caption(f"OAuth notice: {yt_err}")

            st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)

            # Memory (Google Sheets / Drive) Status & Connect Button
            if memory_creds:
                st.markdown('<span class="status-pill status-connected">● Sheets Memory Connected</span>', unsafe_allow_html=True)
                if st.button("Disconnect Sheets", key="sb_disconnect_mem", use_container_width=True):
                    if token_store and subject:
                        token_store.delete_credentials(subject, "memory")
                    st.session_state.pop("memory_credentials_json", None)
                    st.rerun()
            else:
                if is_demo:
                    st.markdown('<span class="status-pill status-connected">● Demo Memory Active</span>', unsafe_allow_html=True)
                else:
                    st.markdown('<span class="status-pill status-disconnected">● Sheets Memory Disconnected</span>', unsafe_allow_html=True)
                
                mem_auth_url, _ = get_auth_url("memory", subject or "demo-owner-subject-12345", secrets)
                if mem_auth_url:
                    st.link_button("📊 Connect Sheets", mem_auth_url, use_container_width=True)

            st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

            # Quick Demo Mode Toggle
            demo_toggled = st.toggle("🎮 Demo Mode", value=is_demo, key="sb_demo_toggle", help="Toggle realistic simulated channel data without live API calls")
            if demo_toggled != is_demo:
                set_demo_mode(demo_toggled)
                st.rerun()

    pg.run()


if __name__ == "__main__":
    main()
