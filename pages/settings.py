"""Page: Settings & Connections.

Connection health cards, provider setup, demo mode toggle, and diagnostics.
"""

import sys
from typing import Any, Dict, Optional
import streamlit as st

from core.auth import is_demo_mode, set_demo_mode
from core.oauth import create_purpose_state, build_flow, YOUTUBE_SCOPES, MEMORY_SCOPES
from ui.components import demo_banner, status_pill


def render_settings(secrets=None, subject=None, youtube_creds=None, memory_creds=None, token_store=None):
    """Render Settings & Connections health overview."""
    demo_banner()
    secrets = secrets or {}
    is_demo = is_demo_mode()

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <h1 style="font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px;">Settings & Connections</h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Single control center for Google identity, YouTube authorization, AI providers, and diagnostics.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Demo Mode Toggle Card
    with st.container(border=True):
        dcol1, dcol2 = st.columns([3, 1])
        with dcol1:
            st.markdown("### Demo Mode")
            st.caption(
                "Explore all app features with realistic sample data without connecting live YouTube or Google credentials. "
                "Demo mode never makes external API calls."
            )
        with dcol2:
            st.caption(" ")
            new_demo = st.toggle("Enable Demo Mode", value=is_demo, key="toggle_demo_mode")
            if new_demo != is_demo:
                set_demo_mode(new_demo)
                st.rerun()

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 1. Google Sign-In Card
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown("### 1. Google Sign-in Identity")
            user = getattr(st, "user", None)
            email = getattr(user, "email", "Signed out" if not is_demo else "demo-owner@channel.com")
            verified = getattr(user, "email_verified", True if is_demo else False)
            expected_owner = secrets.get("access", {}).get("owner_email", "(Set in secrets)")
            st.caption(f"Authenticated as: **{email}** ({'Verified Owner ✓' if verified else 'Unverified'})")
            st.caption(f"Configured Owner Email: `{expected_owner}`")
        with c2:
            st.caption(" ")
            if getattr(user, "is_logged_in", False):
                if st.button("Sign out", use_container_width=True):
                    st.logout()
            else:
                st.markdown('<span class="status-pill status-connected">● Active Session</span>', unsafe_allow_html=True)

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 2. YouTube Data & Analytics Card
    with st.container(border=True):
        y1, y2 = st.columns([3, 1])
        with y1:
            st.markdown("### 2. YouTube Analytics & Metadata Access")
            st.caption("Grants `yt-analytics.readonly` and `youtube.force-ssl` for channel diagnostics and exact metadata updates.")
            if youtube_creds:
                st.markdown('<span class="status-pill status-connected">● Connected</span>', unsafe_allow_html=True)
                st.caption("Note: Google OAuth in 'Testing' mode expires refresh tokens after 7 days. If an error occurs, click Reconnect below.")
            elif is_demo:
                st.markdown('<span class="status-pill status-connected">● Demo YouTube Connected</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="status-pill status-disconnected">● Disconnected</span>', unsafe_allow_html=True)
        with y2:
            st.caption(" ")
            if youtube_creds:
                if st.button("Disconnect YouTube", use_container_width=True, key="btn_disconnect_yt"):
                    if token_store and subject:
                        token_store.delete_credentials(subject, "youtube")
                    st.session_state.pop("youtube_credentials_json", None)
                    st.session_state.pop("dashboard_data", None)
                    st.rerun()
            elif not is_demo and subject and secrets.get("youtube_oauth"):
                try:
                    yt_oauth = secrets["youtube_oauth"]
                    state, verifier = create_purpose_state(subject, secrets["security"]["state_secret"], "youtube")
                    st.session_state["pending_youtube_oauth_state"] = state
                    flow = build_flow(
                        yt_oauth["client_id"],
                        yt_oauth["client_secret"],
                        yt_oauth["redirect_uri"],
                        YOUTUBE_SCOPES,
                        state=state,
                        code_verifier=verifier,
                    )
                    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", login_hint=email)
                    st.link_button("Connect YouTube", auth_url, type="primary", use_container_width=True)
                except Exception as e:
                    st.caption(f"OAuth config notice: {str(e)}")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 3. Private Memory Google Sheets Card
    with st.container(border=True):
        m1, m2 = st.columns([3, 1])
        with m1:
            st.markdown("### 3. Private Google Sheets Memory")
            st.caption("Stores scan snapshots and approved experiments into your private Google Sheet using `drive.file` scope only.")
            if memory_creds or is_demo:
                st.markdown('<span class="status-pill status-connected">● Connected</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="status-pill status-disconnected">● Not Connected (Optional)</span>', unsafe_allow_html=True)
        with m2:
            st.caption(" ")
            if memory_creds:
                if st.button("Disconnect Sheets", use_container_width=True, key="btn_disconnect_memory"):
                    if token_store and subject:
                        token_store.delete_credentials(subject, "memory")
                    st.session_state.pop("memory_credentials_json", None)
                    st.rerun()
            elif not is_demo and subject and (secrets.get("memory_oauth") or secrets.get("youtube_oauth")):
                try:
                    oauth_cfg = secrets.get("memory_oauth") or secrets.get("youtube_oauth")
                    state, verifier = create_purpose_state(subject, secrets["security"]["state_secret"], "memory")
                    st.session_state["pending_memory_oauth_state"] = state
                    flow = build_flow(
                        oauth_cfg["client_id"],
                        oauth_cfg["client_secret"],
                        oauth_cfg["redirect_uri"],
                        MEMORY_SCOPES,
                        state=state,
                        code_verifier=verifier,
                    )
                    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", login_hint=email)
                    st.link_button("Connect Sheets", auth_url, type="primary", use_container_width=True)
                except Exception as e:
                    st.caption(f"OAuth config notice: {str(e)}")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 4. AI Providers Card
    with st.container(border=True):
        st.markdown("### 4. AI Providers Configuration")
        st.caption("Keys are stored strictly in Streamlit Secrets and never displayed.")

        ai_cols = st.columns(2)
        groq_cfg = secrets.get("groq", {})
        gemini_cfg = secrets.get("gemini", {})

        with ai_cols[0]:
            st.markdown("**Primary: Groq**")
            has_groq = bool(groq_cfg.get("api_key"))
            st.markdown(f"Status: {'✅ Configured' if has_groq else '⚠️ Missing api_key'}")
            st.caption(f"Model: `{groq_cfg.get('model', 'openai/gpt-oss-120b')}`")

        with ai_cols[1]:
            st.markdown("**Fallback: Google Gemini**")
            has_gemini = bool(gemini_cfg.get("api_key"))
            st.markdown(f"Status: {'✅ Configured' if has_gemini else '⚠️ Missing api_key'}")
            st.caption(f"Model: `{gemini_cfg.get('model', 'gemini-2.5-flash')}`")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 5. Non-sensitive Diagnostics
    with st.expander("System Diagnostics & Health Check"):
        st.markdown(
            f"""
            - **Python Version:** `{sys.version.split()[0]}`
            - **Streamlit Version:** `{st.__version__}`
            - **Demo Mode:** `{is_demo}`
            - **Active Session Keys:** `{len(st.session_state)}`
            - **Vault Path:** `~/.ai-simplified-lab-strategist-oauth.sqlite3`
            """
        )
