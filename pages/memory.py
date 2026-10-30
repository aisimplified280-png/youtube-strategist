"""Page: Memory (Creator Preferences, Voice Guidelines & Historical Decisions).

Persistent memory destination:
- Voice profile & tone parameters
- Prohibited terms
- Historical proposals (accepted/declined with feedback)
- Optional Google Drive / Sheets mirror sync
"""

import html
import streamlit as st

from core.auth import is_demo_mode
from core.creator_db import (
    get_creator_preferences,
    set_creator_preference,
    get_recent_proposal_history,
)
from ui.components import demo_banner


def render_memory(memory_credentials=None):
    """Render the Creator Memory destination."""
    demo_banner()
    is_demo = is_demo_mode()

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <div style="color: #6366F1; font-weight: 700; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;">
                Durable Context
            </div>
            <h1 style="font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; margin: 0 0 6px 0;">
                Creator Memory
            </h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Configure channel voice guidelines, prohibited phrases, and review historical proposal outcomes.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab_voice, tab_history = st.tabs(["🎙️ Channel Voice & Preferences", "📜 Proposal & Decision History"])

    with tab_voice:
        st.subheader("Creator Voice & Audience Persona")
        st.caption("These rules directly ground the LLM prompt generator, preventing generic hype or off-brand phrasing.")

        prefs = get_creator_preferences()

        with st.form("creator_pref_form"):
            new_tone = st.text_area(
                "Voice & Tone Profile",
                value=prefs.get("voice_tone", ""),
                height=90,
                help="How you speak to your audience (e.g. direct, practical, technical without buzzwords)."
            )
            new_prohibited = st.text_input(
                "Prohibited / Overused Phrases (comma-separated)",
                value=prefs.get("prohibited_words", ""),
                help="Words the agent will never use in generated titles or descriptions."
            )
            new_audience = st.text_input(
                "Target Audience Persona",
                value=prefs.get("target_audience", ""),
                help="Primary audience profile."
            )
            new_format = st.text_input(
                "Primary Content Format",
                value=prefs.get("primary_format", ""),
                help="Core video formats produced."
            )

            saved = st.form_submit_button("Save Preferences", type="primary")
            if saved:
                set_creator_preference("voice_tone", new_tone)
                set_creator_preference("prohibited_words", new_prohibited)
                set_creator_preference("target_audience", new_audience)
                set_creator_preference("primary_format", new_format)
                st.toast("Creator preferences updated and persisted!")
                st.rerun()

    with tab_history:
        st.subheader("Historical Proposal Log")
        st.caption("Decisions made in the Agent Workspace are recorded here to prevent repeating declined patterns.")

        history = get_recent_proposal_history(limit=25)
        if not history:
            st.info("No proposals logged yet. Once you approve or decline suggestions in the Agent Workspace, they will appear here.")
        else:
            for item in history:
                status = item.get("status", "unknown").upper()
                badge_color = "#10B981" if status == "APPROVED" else "#EF4444" if status == "DECLINED" else "#6366F1"
                with st.container(border=True):
                    col_h1, col_h2 = st.columns([3, 1])
                    with col_h1:
                        st.markdown(
                            f"""
                            <span style="background: {badge_color}22; color: {badge_color}; border: 1px solid {badge_color}44; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; font-weight: 700;">
                                {status}
                            </span>
                            <span style="color: #94A3B8; font-size: 0.8rem; margin-left: 8px;">{item.get('updated_at')}</span>
                            <div style="font-weight: 700; font-size: 1.05rem; margin-top: 6px; color: #FFFFFF;">
                                {html.escape(item.get('proposal', {}).get('title', 'Video Proposal'))}
                            </div>
                            <div style="font-size: 0.85rem; color: #94A3B8; margin-top: 4px;">
                                Video ID: <code>{item.get('video_id')}</code> • Digest: <code>{item.get('proposal_digest')[:12]}...</code>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        if item.get("feedback"):
                            st.caption(f"Feedback: {item.get('feedback')}")
                    with col_h2:
                        st.write("")
