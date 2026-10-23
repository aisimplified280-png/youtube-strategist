"""Page: Home ("What should I do today?").

Delivers 5-second clarity for first-time and returning channel owners.
"""

from datetime import date, timedelta
import streamlit as st

from core.auth import is_demo_mode
from core.demo_data import DEMO_HERO_ACTION, DEMO_CHANNEL_SUMMARY, DEMO_EXPERIMENTS
from ui.components import demo_banner, stepper, metric_tile, evidence_chip, status_pill


def render_home(youtube=None, secrets=None, subject=None, token_store=None):
    """Render the Home workspace."""
    demo_banner()

    is_demo = is_demo_mode()
    has_yt = youtube is not None or is_demo

    # Status strip (3 steps)
    current_step = 3 if has_yt else 2
    stepper(["1. Sign in", "2. Connect YouTube", "3. Act on next opportunity"], current_step)

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <h1 style="font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px;">What should I do today?</h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">Your channel's single highest-leverage packaging opportunity, justified by real evidence.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 1. Hero Card: The single next-best action
    hero = None
    if is_demo:
        hero = DEMO_HERO_ACTION
    elif st.session_state.get("studio_analysis_hero"):
        hero = st.session_state["studio_analysis_hero"]
    elif st.session_state.get("opportunity_candidates"):
        cands = st.session_state["opportunity_candidates"]
        if cands:
            c = cands[0]
            hero = {
                "video_id": c.get("video_id"),
                "title": c.get("title"),
                "recommendation_reason": c.get("recommendation_evidence") or c.get("opportunity_evidence", "Measured underperformance vs peers."),
                "ctr": c.get("ctr"),
                "peer_median_ctr": c.get("peer_median_ctr"),
                "views": c.get("views"),
                "duration_seconds": c.get("duration_seconds", 0),
            }

    if hero:
        chips = []
        if hero.get("ctr") is not None:
            baseline = hero.get("peer_median_ctr", 4.5)
            chips.append(evidence_chip("Measured CTR", f"{hero['ctr']}% (vs {baseline}% baseline)", "warning"))
        if hero.get("average_view_percentage") is not None:
            chips.append(evidence_chip("Avg % Viewed", f"{hero['average_view_percentage']}%", "neutral"))
        if hero.get("retention_cliff"):
            chips.append(evidence_chip("Retention Cliff", str(hero["retention_cliff"]), "alert"))
        chips_html = "".join(chips)

        st.markdown(
            f"""
            <div class="hero-action-card">
                <div class="hero-tag">🔥 Priority Opportunity</div>
                <div class="hero-title">{hero.get('title', 'Video packaging review')}</div>
                <div class="hero-subtitle">{hero.get('recommendation_reason', '')}</div>
                <div style="margin-bottom: 16px;">{chips_html}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_act1, col_act2 = st.columns([1, 3])
        with col_act1:
            if st.button("Do it — build proposal", type="primary", use_container_width=True, key="home_hero_do_it"):
                st.session_state["pending_optimizer_video_id"] = hero.get("video_id")
                st.session_state["pending_optimizer_video_title"] = hero.get("title")
                if hero.get("proposed_snippet"):
                    st.session_state["strategist_chat_proposal"] = hero["proposed_snippet"]
                st.session_state["nav_destination"] = "strategist"
                st.rerun()
        with col_act2:
            st.caption("Opens Strategist chat with measured evidence pre-loaded. Nothing changes until you explicitly approve the exact proposed metadata.")
    else:
        with st.container(border=True):
            st.markdown(
                """
                <div style="padding: 16px 8px;">
                    <h3 style="margin-top:0; color:#FFFFFF;">No scan data loaded yet</h3>
                    <p style="color:#94A3B8; font-size:0.92rem;">
                        Upload your YouTube Studio CSV/Excel exports or scan your recent uploads to surface your top packaging opportunity.
                    </p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Upload Studio exports", type="primary", use_container_width=True):
                    st.session_state["nav_destination"] = "pages/studio_analysis.py"
                    st.rerun()
            with col2:
                if st.button("Explore in Demo Mode", use_container_width=True):
                    st.session_state["demo_mode"] = True
                    st.rerun()

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # 2. Channel KPI Tiles with period-over-period deltas
    st.markdown("### Channel Pulse (Last 28 Days)")
    kpi_cols = st.columns(4)

    if is_demo:
        s = DEMO_CHANNEL_SUMMARY
        with kpi_cols[0]:
            metric_tile("Views", f"{s['views']:,}", s["views_delta"], s["views_delta_type"])
        with kpi_cols[1]:
            metric_tile("Watch Time (Hours)", f"{int(s['estimatedMinutesWatched'] / 60):,}", s["watch_time_delta"], s["watch_time_delta_type"])
        with kpi_cols[2]:
            metric_tile("Avg View Duration", f"{s['averageViewDuration']}s", s["avd_delta"], s["avd_delta_type"])
        with kpi_cols[3]:
            metric_tile("Net Subscribers", f"+{s['net_subscribers']:,}", s["subscribers_delta"], s["subscribers_delta_type"])
    elif has_yt and st.session_state.get("dashboard_data"):
        data = st.session_state["dashboard_data"]
        summary = data.get("summary") or {}
        gained = summary.get("subscribersGained", 0) or 0
        lost = summary.get("subscribersLost", 0) or 0
        net_subs = gained - lost
        views = summary.get("views")
        watch_min = summary.get("estimatedMinutesWatched")
        avd = summary.get("averageViewDuration")

        with kpi_cols[0]:
            metric_tile("Views", f"{views:,}" if views is not None else "Unavailable")
        with kpi_cols[1]:
            metric_tile("Watch Time (Hours)", f"{int(watch_min/60):,}" if watch_min is not None else "Unavailable")
        with kpi_cols[2]:
            metric_tile("Avg View Duration", f"{avd}s" if avd is not None else "Unavailable")
        with kpi_cols[3]:
            metric_tile("Net Subscribers", f"+{net_subs:,}")
    else:
        with kpi_cols[0]:
            metric_tile("Views", "—", "Connect YouTube to view")
        with kpi_cols[1]:
            metric_tile("Watch Time", "—", "Connect YouTube to view")
        with kpi_cols[2]:
            metric_tile("Avg View Duration", "—", "Connect YouTube to view")
        with kpi_cols[3]:
            metric_tile("Net Subscribers", "—", "Connect YouTube to view")

    st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)

    # 3. Recent experiments from memory
    st.markdown("### Recent Experiments & Applied Changes")
    experiments = DEMO_EXPERIMENTS if is_demo else st.session_state.get("session_audit_records", [])

    if experiments:
        for exp in experiments[:3]:
            with st.container(border=True):
                exp_cols = st.columns([3, 1])
                with exp_cols[0]:
                    st.markdown(f"**{exp.get('after_title', exp.get('title', 'Applied Change'))}**")
                    if exp.get("before_title"):
                        st.caption(f"Original title: {exp.get('before_title')}")
                with exp_cols[1]:
                    st.markdown(f'<span class="status-pill status-connected">● Verified on YouTube</span>', unsafe_allow_html=True)
                    st.caption(exp.get("applied_at", exp.get("timestamp", "")))
    else:
        st.info("No metadata experiments approved yet in this session. When you approve and apply changes in Strategist, verified records will appear here.")
