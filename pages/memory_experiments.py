"""Page: Memory & Experiments.

Durable timeline of verified YouTube metadata experiments, weekly scan snapshots, and Google Sheets status.
"""

from typing import Any, Dict, List, Optional
import streamlit as st

from core.auth import is_demo_mode
from core.demo_data import DEMO_EXPERIMENTS
from ui.components import demo_banner, status_pill, empty_state
import content_memory


def render_memory_experiments(memory_credentials=None):
    """Render the Memory & Experiments workspace."""
    demo_banner()
    is_demo = is_demo_mode()

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <h1 style="font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px;">Strategist Memory & Experiments</h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Verified before/after records of applied packaging changes and weekly scan snapshots.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Google Sheets Connection Strip
    with st.container(border=True):
        mcol1, mcol2 = st.columns([3, 1])
        with mcol1:
            st.markdown("**Google Sheets 'Strategist Memory' Integration**")
            st.caption(
                "Persists weekly scans and verified experiments to your private Google Drive spreadsheet "
                "using the scoped `drive.file` permission. Drafts and rejected proposals are never recorded."
            )
        with mcol2:
            if is_demo:
                st.markdown('<span class="status-pill status-connected">● Demo Memory Active</span>', unsafe_allow_html=True)
            elif memory_credentials:
                if st.session_state.get("content_memory_store_verified", False):
                    st.markdown('<span class="status-pill status-connected">● Sheets Connected</span>', unsafe_allow_html=True)
                    url = st.session_state.get("content_memory_spreadsheet_url")
                    if url:
                        st.link_button("Open Spreadsheet", url, use_container_width=True)
                else:
                    st.markdown('<span class="status-pill status-reconnect">● Needs Setup/Retry</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="status-pill status-disconnected">● Not Connected</span>', unsafe_allow_html=True)
                st.caption("Connect in Settings")

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # Tabs: Approved Experiments vs Weekly Scans
    tab_exp, tab_scans = st.tabs(["🎯 Approved Experiments Timeline", "📋 Weekly Scan Snapshots"])

    with tab_exp:
        experiments = DEMO_EXPERIMENTS if is_demo else st.session_state.get("session_audit_records", [])

        if not experiments:
            empty_state(
                "No experiments recorded yet",
                "When you review a proposal in Strategist and click 'Approve and apply to YouTube', the verified change will appear here with before/after comparisons.",
                icon="🔬",
            )
        else:
            for exp in experiments:
                with st.container(border=True):
                    exp_top = st.columns([3, 1])
                    with exp_top[0]:
                        st.markdown(f"### {exp.get('after_title', exp.get('title', 'Applied Change'))}")
                        st.caption(f"Applied: {exp.get('applied_at', exp.get('timestamp', 'Recently'))} | Video ID: `{exp.get('video_id')}`")
                    with exp_top[1]:
                        st.markdown('<span class="status-pill status-connected">● Verified on YouTube</span>', unsafe_allow_html=True)

                    st.markdown("**Before vs After Title Comparison:**")
                    b_col, a_col = st.columns(2)
                    with b_col:
                        st.caption("BEFORE")
                        st.markdown(f"> *{exp.get('before_title', 'Original title')}*")
                    with a_col:
                        st.caption("AFTER (APPLIED)")
                        st.markdown(f"> **{exp.get('after_title', exp.get('title', ''))}**")

                    if exp.get("views_before") and exp.get("views_after_7d"):
                        st.markdown("---")
                        metric_cols = st.columns(2)
                        with metric_cols[0]:
                            st.metric("Views (Pre vs 7d Post)", f"{exp['views_after_7d']:,}", f"+{exp['views_after_7d'] - exp['views_before']:,}")
                        with metric_cols[1]:
                            st.metric("CTR (Pre vs 7d Post)", f"{exp.get('ctr_after_7d')}%", f"+{round(exp.get('ctr_after_7d',0) - exp.get('ctr_before',0), 1)} pts")

    with tab_scans:
        scans = st.session_state.get("session_scan_history", [])
        if is_demo:
            scans = [
                {
                    "saved_at": "2026-10-18 10:00 UTC",
                    "period": "2026-09-18 through 2026-10-17",
                    "uploads": 6,
                    "measured_views": 6,
                    "usable_ctr": 4,
                    "candidates_count": 2,
                }
            ]

        if not scans:
            empty_state(
                "No scan snapshots recorded",
                "Periodic upload scans will record channel snapshots here so you can track how your shortlist changes over time.",
                icon="📊",
            )
        else:
            for scan in scans:
                with st.container(border=True):
                    st.markdown(f"**Scan Period:** {scan.get('period', 'Recent period')}")
                    st.caption(f"Saved: {scan.get('saved_at', 'Session')}")
                    sc_cols = st.columns(4)
                    with sc_cols[0]:
                        st.metric("Uploads in Window", scan.get("uploads", 0))
                    with sc_cols[1]:
                        st.metric("Measured Views", scan.get("measured_views", 0))
                    with sc_cols[2]:
                        st.metric("Usable CTR Rows", scan.get("usable_ctr", 0))
                    with sc_cols[3]:
                        st.metric("Priority Candidates", scan.get("candidates_count", 0))
