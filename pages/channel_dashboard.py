"""Page: Channel Dashboard.

Analytics, trend charts, period-over-period comparisons, and evidence drawer.
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional
import streamlit as st

from core.auth import is_demo_mode
from core.demo_data import DEMO_CHANNEL_SUMMARY, DEMO_VIDEOS
from core.analytics_logic import get_period_preset, calculate_delta, rank_candidates, calculate_custom_period
from ui.components import demo_banner, metric_tile, evidence_chip, status_pill
import youtube_dashboard


def render_channel_dashboard(youtube=None, analytics=None, reporting=None, credentials=None):
    """Render the Channel Dashboard workspace."""
    demo_banner()
    is_demo = is_demo_mode()
    has_yt = youtube is not None or is_demo

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <h1 style="font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px;">Channel Performance & Reach</h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Measured channel analytics, peer-grounded performance, and thumbnail reach metrics.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Date range presets
    filter_cols = st.columns([1.5, 1, 1, 1])
    with filter_cols[0]:
        preset = st.selectbox("Date Range Preset", ["Last 28 Days", "Last 7 Days", "Last 90 Days", "Custom Range"], index=0)
    
    preset_key = "28d" if "28" in preset else "7d" if "7" in preset else "90d"
    cur_start, cur_end, prior_start, prior_end = get_period_preset(preset_key)

    if preset == "Custom Range":
        with filter_cols[1]:
            cur_start = st.date_input("From", cur_start, max_value=date.today() - timedelta(days=1))
        with filter_cols[2]:
            cur_end = st.date_input("Through", cur_end, max_value=date.today() - timedelta(days=1))
        cur_start, cur_end, prior_start, prior_end = calculate_custom_period(cur_start, cur_end)

    with filter_cols[3]:
        st.caption(" ")
        refresh_clicked = st.button("Refresh data", use_container_width=True)
        if refresh_clicked:
            st.session_state.pop("dashboard_data", None)

    st.caption(f"Comparing current period ({cur_start} to {cur_end}) against prior period ({prior_start} to {prior_end}).")

    # Load or Fetch Data
    if is_demo:
        s = DEMO_CHANNEL_SUMMARY
        videos = DEMO_VIDEOS
        reach_status = "Available (demo channel reach report)"
    elif has_yt and analytics:
        dashboard_data = st.session_state.get("dashboard_data")
        if not dashboard_data or refresh_clicked:
            with st.spinner("Fetching channel reports from YouTube Analytics and Reporting APIs..."):
                try:
                    dashboard_data = youtube_dashboard.load_dashboard_data(
                        youtube,
                        analytics,
                        cur_start,
                        cur_end,
                        video_limit=100,
                        reporting=reporting,
                        credentials=credentials,
                    )
                    st.session_state["dashboard_data"] = dashboard_data
                except Exception as e:
                    st.error(f"Error loading analytics: {str(e)}")
                    return

        s = dashboard_data.get("summary") or {}
        videos = dashboard_data.get("video_rows", [])
        reach_status = dashboard_data.get("reach_status", "Unavailable")
    else:
        st.warning("Connect YouTube in Settings or turn on Demo Mode to inspect channel analytics.")
        return

    # Metric Cards with period-over-period comparison
    mcols = st.columns(4)
    with mcols[0]:
        val = s.get("views")
        views_str = f"{val:,}" if val is not None else "Unavailable"
        delta_str = s.get("views_delta", "+14.2% vs prior") if is_demo else None
        delta_type = s.get("views_delta_type", "positive") if is_demo else "neutral"
        metric_tile("Views", views_str, delta_str, delta_type)

    with mcols[1]:
        wt = s.get("estimatedMinutesWatched")
        wt_str = f"{int(wt/60):,} hrs" if wt is not None else "Unavailable"
        delta_str = s.get("watch_time_delta", "+8.5% vs prior") if is_demo else None
        delta_type = s.get("watch_time_delta_type", "positive") if is_demo else "neutral"
        metric_tile("Watch Time", wt_str, delta_str, delta_type)

    with mcols[2]:
        avd = s.get("averageViewDuration")
        avd_str = f"{avd}s" if avd is not None else "Unavailable"
        delta_str = s.get("avd_delta", "+12s vs prior") if is_demo else None
        delta_type = s.get("avd_delta_type", "positive") if is_demo else "neutral"
        metric_tile("Avg View Duration", avd_str, delta_str, delta_type)

    with mcols[3]:
        gained = s.get("subscribersGained", 0) or 0
        lost = s.get("subscribersLost", 0) or 0
        net = gained - lost if not is_demo else s.get("net_subscribers", 1380)
        delta_str = s.get("subscribers_delta", "+220 vs prior") if is_demo else None
        delta_type = s.get("subscribers_delta_type", "positive") if is_demo else "neutral"
        metric_tile("Net Subscribers", f"+{net:,}", delta_str, delta_type)

    st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)

    # Reach Metrics Status & Limitations
    with st.expander("Reach Metrics & Reporting API Status", expanded=("Unavailable" in reach_status and not is_demo)):
        st.markdown(f"**Thumbnail Reach Status:** `{reach_status}`")
        st.markdown(
            """
            *Why thumbnail reach matters:*
            - YouTube Analytics `reports.query` API **does not** return thumbnail impressions or CTR.
            - Those metrics require the **YouTube Reporting API** `channel_reach_basic_a1` daily batch report.
            - If reach is Unavailable:
              1. Ensure **YouTube Reporting API** is enabled in your Google Cloud Project.
              2. Google requires up to 48 hours to generate the first report job file.
              3. HTTP 403 indicates missing Reporting API permissions or project configuration.
            """
        )

    # Evidence Drawer
    with st.expander("How Rankings & Peer Groups Are Computed (Evidence Drawer)"):
        st.markdown(
            """
            **Peer Comparison Criteria:**
            - **Minimum Sample:** At least 3 age- and format-matched peers are required before an underperformance flag is raised.
            - **Duration Classification:** Videos up to 180s are categorized as *Short candidates*; videos >180s are *Long-form*.
            - **Upload Age Buckets:** Uploads are compared only against uploads of comparable maturity (e.g. 0-14 days vs 15-60 days).
            - **No Causal Claim:** A lower view count or CTR indicates a review opportunity; it does *not* prove title/thumbnail caused the outcome.
            """
        )

    st.markdown("### Video Performance Table")
    fmt_col, search_col = st.columns([1, 2])
    with fmt_col:
        fmt_filter = st.selectbox("Format Filter", ["All Formats", "Long-form (>180s)", "Short candidates (<=180s)"])
    with search_col:
        query = st.text_input("Search videos by title", placeholder="Filter by keyword...")

    filtered_videos = []
    for v in videos:
        dur = v.get("duration_seconds", 0)
        is_short = dur <= 180
        if fmt_filter == "Long-form (>180s)" and is_short:
            continue
        if fmt_filter == "Short candidates (<=180s)" and not is_short:
            continue
        if query and query.lower() not in v.get("title", "").lower():
            continue
        filtered_videos.append(v)

    # Table View
    display_rows = []
    for v in filtered_videos:
        ctr_val = f"{v['ctr']}%" if v.get("ctr") is not None else "Unavailable"
        imp_val = f"{v['impressions']:,}" if v.get("impressions") is not None else "Unavailable"
        display_rows.append({
            "Video Title": v.get("title", "Untitled"),
            "Published": v.get("published_at", ""),
            "Format": v.get("format", ""),
            "Views": f"{v.get('views', 0):,}",
            "Watch Time (min)": f"{v.get('estimatedMinutesWatched', 0):,}",
            "Avg View Duration": f"{v.get('averageViewDuration', 0)}s",
            "Impressions": imp_val,
            "CTR": ctr_val,
            "ID": v.get("video_id"),
        })

    st.dataframe(display_rows, hide_index=True, use_container_width=True)

    # Quick action for ranked candidates
    ranked = [v for v in videos if v.get("opportunity_candidate")]
    if ranked:
        st.markdown("### High-Priority Optimization Candidates")
        for cand in ranked[:3]:
            with st.container(border=True):
                c_cols = st.columns([3, 1])
                with c_cols[0]:
                    st.markdown(f"**{cand.get('title')}**")
                    st.caption(cand.get("opportunity_evidence", "Below peer median."))
                with c_cols[1]:
                    if st.button("Review in Strategist", key=f"dash_rev_{cand.get('video_id')}", use_container_width=True):
                        st.session_state["pending_optimizer_video_id"] = cand.get("video_id")
                        st.session_state["pending_optimizer_video_title"] = cand.get("title")
                        st.session_state["nav_destination"] = "strategist"
                        st.rerun()
