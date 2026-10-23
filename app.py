"""Main application shell, authentication gate, theme injector, and multipage router."""

import json
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.auth import verify_owner_access, is_demo_mode
from core.vault import VaultManager
from core.oauth import test_or_refresh_credentials, create_purpose_state, build_flow, YOUTUBE_SCOPES, MEMORY_SCOPES
from ui.theme import inject_theme
import oauth_token_store
import dashboard_security

# Page implementations
from pages.home import render_home
from pages.studio_analysis import render_studio_analysis
from pages.strategist import render_strategist
from pages.channel_dashboard import render_channel_dashboard
from pages.memory_experiments import render_memory_experiments
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
                scopes = YOUTUBE_SCOPES if purpose == "youtube" else MEMORY_SCOPES
                redirect_uri = secrets.get("youtube_oauth", {}).get("redirect_uri", "")
                flow = build_flow(
                    secrets["youtube_oauth"]["client_id"],
                    secrets["youtube_oauth"]["client_secret"],
                    redirect_uri,
                    scopes=scopes,
                    state=returned_state,
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

    # Vault & Token Management
    state_secret = secrets.get("security", {}).get("state_secret", "dummy-secret-for-demo-mode-at-least-32-chars")
    token_store = None
    if len(state_secret) >= 32:
        try:
            token_store = oauth_token_store.OAuthTokenStore(state_secret)
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

    # Multipage Navigation Setup using st.navigation & st.Page with named functions and explicit url_paths
    def show_home():
        render_home(youtube=youtube, secrets=secrets, subject=subject, token_store=token_store)

    def show_analyze():
        render_studio_analysis(youtube=youtube)

    def show_strategist():
        render_strategist(youtube=youtube, secrets=secrets)

    def show_dashboard():
        render_channel_dashboard(youtube=youtube, analytics=analytics, reporting=reporting, credentials=yt_creds)

    def show_memory():
        render_memory_experiments(memory_credentials=memory_creds)

    def show_settings():
        render_settings(secrets=secrets, subject=subject, youtube_creds=yt_creds, memory_creds=memory_creds, token_store=token_store)

    page_home = st.Page(
        show_home,
        title="Home",
        icon="⚡",
        url_path="home",
        default=True,
    )
    page_analyze = st.Page(
        show_analyze,
        title="Find Videos & Analyze",
        icon="🎯",
        url_path="analyze",
    )
    page_strategist = st.Page(
        show_strategist,
        title="Strategist Co-Pilot",
        icon="💬",
        url_path="strategist",
    )
    page_dashboard = st.Page(
        show_dashboard,
        title="Channel Dashboard",
        icon="📊",
        url_path="dashboard",
    )
    page_memory = st.Page(
        show_memory,
        title="Memory & Experiments",
        icon="🧠",
        url_path="memory",
    )
    page_settings = st.Page(
        show_settings,
        title="Settings & Connections",
        icon="⚙️",
        url_path="settings",
    )

    pg = st.navigation({
        "Co-Pilot": [page_home, page_analyze, page_strategist],
        "Telemetry": [page_dashboard, page_memory],
        "System": [page_settings],
    })

    # Programmatic destination routing support
    nav_dest = st.session_state.pop("nav_destination", None)
    if nav_dest:
        dest_map = {
            "home": page_home,
            "analyze": page_analyze,
            "pages/studio_analysis.py": page_analyze,
            "strategist": page_strategist,
            "pages/strategist.py": page_strategist,
            "dashboard": page_dashboard,
            "pages/channel_dashboard.py": page_dashboard,
            "memory": page_memory,
            "pages/memory_experiments.py": page_memory,
            "settings": page_settings,
            "pages/settings.py": page_settings,
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
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px;">
                <div style="background: #FF1E44; color: white; width: 30px; height: 30px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 14px;">▶</div>
                <div style="font-weight: 800; font-size: 1.05rem; letter-spacing: -0.02em;">YouTube Strategist</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if is_demo:
            st.markdown('<span class="status-pill status-connected">● Demo Mode</span>', unsafe_allow_html=True)
        elif yt_creds:
            st.markdown('<span class="status-pill status-connected">● YouTube Connected</span>', unsafe_allow_html=True)
        elif st.session_state.get("youtube_needs_reconnect"):
            st.markdown('<span class="status-pill status-reconnect">● Reconnect Needed</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="status-pill status-disconnected">● YouTube Disconnected</span>', unsafe_allow_html=True)

    pg.run()


if __name__ == "__main__":
    main()
