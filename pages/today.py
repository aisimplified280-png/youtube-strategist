"""Page: Today — Automatic Creator Daily Briefing.

First screen after owner login:
1. 3 Prioritized, evidence-backed action cards (What needs attention? Why today? Evidence? Next step?).
2. AI Research Radar updates with authoritative sources.
3. System status and freshness metadata with one-click refresh.
"""

from datetime import date, datetime
import html
import streamlit as st

from core.auth import is_demo_mode
from core.daily_briefing import get_or_create_daily_briefing
from core.creator_db import get_all_growth_tasks, record_growth_task_state, save_pipeline_item
from ui.components import demo_banner, metric_tile, evidence_chip, status_pill, connection_status_banner
from pages.workspace import render_video_optimizer


def _process_deferred_inline_resets():
    """Clean up state and close inline studios for videos with pending deferred resets."""
    for k in list(st.session_state.keys()):
        if k.startswith("_deferred_inline_reset_"):
            vid = k[len("_deferred_inline_reset_"):]
            kp = st.session_state.pop(k, None) or f"today_opt_{vid}"
            st.session_state.pop(f"{kp}_proposal", None)
            st.session_state.pop(f"{kp}_approved_digest", None)
            st.session_state.pop(f"{kp}_title_in", None)
            st.session_state.pop(f"{kp}_desc_in", None)
            st.session_state.pop(f"{kp}_tags_in", None)
            st.session_state.pop(f"{kp}_context", None)
            st.session_state.pop("workspace_target_context", None)
            st.session_state.pop(f"active_today_optimizer_{vid}", None)


def render_today(
    youtube=None,
    analytics=None,
    reporting=None,
    credentials=None,
    secrets=None,
    subject=None,
    memory_creds=None,
):
    """Render the Today daily briefing destination."""
    demo_banner()
    is_demo = is_demo_mode()
    secrets = secrets or {}

    # Process deferred cleanup for any inline studio executions BEFORE widgets render
    _process_deferred_inline_resets()

    # Actionable outside connection banner when disconnected
    connection_status_banner(secrets, subject=subject, yt_creds=youtube, memory_creds=memory_creds, is_demo=is_demo)

    # Display any applied update notice from inline studio executions
    for k in list(st.session_state.keys()):
        if k.startswith("_applied_inline_notice_"):
            notice = st.session_state.pop(k, None)
            if notice:
                st.success(notice)
                st.balloons()

    force_refresh = st.session_state.pop("force_briefing_refresh", False)

    # Load or compute the briefing
    with st.spinner("Preparing your daily growth briefing..."):
        briefing = get_or_create_daily_briefing(
            youtube=youtube,
            analytics=analytics,
            reporting=reporting,
            credentials=credentials,
            owner_id=subject,
            secrets=secrets,
            force_refresh=force_refresh,
            is_demo=is_demo,
        )

    # Persist briefing to session state so Workspace and cross-page selectors are synchronized
    st.session_state["daily_briefing"] = briefing

    status = briefing.get("status", "ready")
    notice = briefing.get("notice")

    # Header with date, refresh timestamp, and manual refresh button
    today_str = date.today().strftime("%A, %B %d, %Y")
    refreshed_at_str = ""
    if briefing.get("generated_at_utc"):
        try:
            dt = datetime.fromisoformat(briefing["generated_at_utc"])
            refreshed_at_str = f" • Refreshed at {dt.strftime('%H:%M')} UTC"
        except Exception:
            pass

    col_h1, col_h2 = st.columns([3, 1])
    with col_h1:
        st.markdown(
            f"""
            <div style="margin-bottom: 20px;">
                <div style="color: #6366F1; font-weight: 700; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;">
                    Daily Creator Briefing
                </div>
                <h1 style="font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; margin: 0 0 6px 0;">
                    Good morning, Creator
                </h1>
                <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                    {today_str}{refreshed_at_str} • Verified opportunities for AI Simplified Lab
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_h2:
        st.write("")
        if st.button("🔄 Refresh Analysis", use_container_width=True, help="Force a fresh scan of recent uploads and metrics"):
            st.session_state["force_briefing_refresh"] = True
            st.rerun()

    # Explicit status banners for stale, scan failure, connection failure, insufficient metrics, or pending
    if status == "connection_failure":
        st.warning(f"⚠️ {notice or 'YouTube authorization is missing or disconnected. Please connect YouTube in Settings.'}")
    elif status == "scan_failed":
        st.error(f"❌ Channel scan failed: {notice or 'API exception occurred during scan.'}")
    elif status in ("stale", "refresh_failed"):
        st.warning(f"⚠️ **Refresh Notice**: {notice or 'Refresh attempt failed. Using prior briefing.'}")
    elif status in ("insufficient_metrics", "unavailable_metrics"):
        st.warning(f"⚠️ Insufficient Analytics Data: {notice or 'YouTube Analytics metrics are unavailable or delayed for recent uploads.'}")
    elif status == "insufficient_peer_sample":
        st.info(f"ℹ️ {notice or 'Insufficient peer sample. Uploads do not have at least 3 same-format, age-matched peers for comparison.'}")
    elif status == "pending":
        st.info("⏳ Daily briefing scan is currently in progress. Please check back shortly.")
    elif notice:
        st.info(notice)

    # High-level health strip
    summary = briefing.get("summary", {})
    col_m1, col_m2, col_m3 = st.columns(3)
    with col_m1:
        metric_tile("Audited Uploads", f"{summary.get('total_recent_videos', 0)} videos", "Last 90 days")
    with col_m2:
        metric_tile("Ranked Opportunities", f"{summary.get('opportunity_count', 0)} candidates", "Format-matched")
    with col_m3:
        reporting_status = "Active" if summary.get("reach_reporting_active") else "Pending"
        metric_tile("Reach Reporting", reporting_status, "Thumbnail impressions & CTR")

    # Display coverage notes or API warnings if present
    coverage = briefing.get("coverage", {})
    if coverage.get("api_warnings"):
        with st.expander("API Report Notices", expanded=False):
            for warn in coverage["api_warnings"]:
                st.caption(f"• {warn}")

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # Main section: Top 3 Prioritized Action Cards
    st.markdown(
        """
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
            <h2 style="font-size: 1.35rem; font-weight: 700; margin: 0;">Priority Actions for Today</h2>
            <span style="color: #94A3B8; font-size: 0.85rem;">Ranked by leverage & peer underperformance</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    actions = briefing.get("prioritized_actions", [])
    if not actions:
        if status == "pending":
            st.info("⏳ Daily briefing scan is currently in progress. Please check back shortly.")
        elif status == "insufficient_sample":
            st.info(
                f"ℹ️ {notice or 'Insufficient sample size. At least 3 format-matched uploads are needed for peer benchmarking.'}"
            )
        elif status in ("insufficient_metrics", "unavailable_metrics"):
            st.warning(
                f"⚠️ {notice or 'Analytics metrics are unavailable or delayed for recent uploads. Cannot benchmark performance without valid metrics.'}"
            )
        elif status == "insufficient_peer_sample":
            st.info(
                f"ℹ️ {notice or 'Insufficient peer sample. Uploads do not have at least 3 same-format, age-matched peers for comparison.'}"
            )
        elif status == "connection_failure":
            st.info("Connect YouTube in Settings or explore in Demo Mode to view prioritized growth actions.")
        elif status == "scan_failed":
            st.warning("Action cards unavailable due to channel scan failure. Check your API credentials and network connection.")
        elif summary.get("total_recent_videos", 0) == 0:
            st.info("No video uploads detected in the last 90 days.")
        elif summary.get("measured_videos_count", 0) < 3:
            st.warning("⚠️ Analytics metrics are unavailable or delayed for recent uploads. Cannot benchmark performance without valid metrics.")
        elif summary.get("comparable_videos_count", 0) < 1:
            st.info("ℹ️ Insufficient peer coverage: none of the recent uploads have at least 3 same-format, age-matched peers to establish a reliable baseline.")
        else:
            st.info("No underperforming videos detected in the current window. All recent uploads are performing in line with or above peer medians.")
    else:
        all_growth_tasks = get_all_growth_tasks(owner_id=subject, is_demo=is_demo)

        for act in actions:
            p_idx = act.get("priority", 1)
            vid = act.get("video_id")
            title = html.escape(act.get("title", "Untitled Video"))
            action_type = html.escape(act.get("action_type", "Opportunity"))
            why_today = html.escape(act.get("why_today", ""))
            evidence = html.escape(act.get("evidence", ""))
            confidence = html.escape(act.get("confidence", "Medium"))
            effort = html.escape(act.get("estimated_effort", "15m"))
            caveat = html.escape(act.get("caveat", ""))
            next_step = html.escape(act.get("recommended_next_step", ""))
            diagnosis = act.get("diagnosis", "")

            task = all_growth_tasks.get(vid, {})
            task_status = task.get("status", "new")
            metadata_applied = task.get("metadata_applied", False) or task_status == "applied_monitoring"

            if metadata_applied:
                status_badge_html = '<span style="background: rgba(16, 185, 129, 0.2); color: #34D399; border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; margin-left: 8px;">APPLIED — MONITORING</span>'
            elif task_status == "in_progress":
                status_badge_html = '<span style="background: rgba(59, 130, 246, 0.2); color: #60A5FA; border: 1px solid rgba(59, 130, 246, 0.4); border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; margin-left: 8px;">IN PROGRESS</span>'
            elif task_status == "resolved":
                status_badge_html = '<span style="background: rgba(100, 116, 139, 0.2); color: #94A3B8; border: 1px solid rgba(100, 116, 139, 0.4); border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; margin-left: 8px;">RESOLVED</span>'
            elif task_status == "dismissed":
                status_badge_html = '<span style="background: rgba(100, 116, 139, 0.2); color: #64748B; border: 1px solid rgba(100, 116, 139, 0.4); border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; margin-left: 8px;">DISMISSED</span>'
            else:
                status_badge_html = '<span style="background: rgba(99, 102, 241, 0.2); color: #818CF8; border: 1px solid rgba(99, 102, 241, 0.4); border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; margin-left: 8px;">NEW OPPORTUNITY</span>'

            with st.container(border=True):
                col_c1, col_c2 = st.columns([3, 1])
                with col_c1:
                    st.markdown(
                        f"""
                        <div style="margin-bottom: 8px;">
                            <span style="background: rgba(99, 102, 241, 0.15); color: #818CF8; border: 1px solid rgba(99, 102, 241, 0.3); border-radius: 6px; padding: 2px 8px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase;">
                                #{p_idx} • {action_type}
                            </span>
                            {status_badge_html}
                            <span style="color: #94A3B8; font-size: 0.8rem; margin-left: 8px;">Effort: {effort} • Confidence: {confidence}</span>
                        </div>
                        <h3 style="font-size: 1.25rem; font-weight: 700; margin: 0 0 8px 0; color: #FFFFFF;">
                            {title}
                        </h3>
                        <p style="color: #CBD5E1; font-size: 0.95rem; margin-bottom: 12px; line-height: 1.45;">
                            {why_today}
                        </p>
                        """,
                        unsafe_allow_html=True,
                    )

                    # Distinctive status messaging when metadata update was applied
                    if metadata_applied:
                        applied_box = """
                        <div style="background: rgba(16, 185, 129, 0.08); border-left: 3px solid #10B981; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                            <div style="font-weight: 700; color: #34D399; font-size: 0.88rem;">✓ Metadata Applied</div>
                            <div style="font-size: 0.83rem; color: #CBD5E1; margin-top: 2px;">
                                Your YouTube title, description and tags were updated and verified.
                            </div>
                        </div>
                        """
                        st.markdown(applied_box, unsafe_allow_html=True)

                        if diagnosis == "Retention review":
                            attention_box = """
                            <div style="background: rgba(245, 158, 11, 0.08); border-left: 3px solid #F59E0B; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                                <div style="font-weight: 700; color: #FBBF24; font-size: 0.88rem;">⚠️ Still Needs Attention: Opening Hook & Retention</div>
                                <div style="font-size: 0.83rem; color: #CBD5E1; margin-top: 2px;">
                                    Your retention metrics remain below comparable videos. Metadata changes do not fix the video's opening or pacing.
                                </div>
                                <div style="font-size: 0.80rem; color: #94A3B8; margin-top: 5px;">
                                    <strong style="color: #F1F5F9;">Next step:</strong> Open Hook Lab to review the first 15 seconds.
                                    <br/><strong style="color: #F1F5F9;">Performance outcome:</strong> Waiting for sufficient post-update analytics (YouTube Analytics typically updates on a 24–48h delay).
                                </div>
                            </div>
                            """
                            st.markdown(attention_box, unsafe_allow_html=True)
                        elif diagnosis == "Reach review":
                            attention_box = """
                            <div style="background: rgba(168, 85, 247, 0.08); border-left: 3px solid #A855F7; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                                <div style="font-weight: 700; color: #C084FC; font-size: 0.88rem;">🔍 Still Needs Attention: Topic & Distribution Positioning</div>
                                <div style="font-size: 0.83rem; color: #CBD5E1; margin-top: 2px;">
                                    Impressions trail format-matched peers. Evaluate search and browse topic demand in Ideas & Production.
                                </div>
                                <div style="font-size: 0.80rem; color: #94A3B8; margin-top: 5px;">
                                    <strong style="color: #F1F5F9;">Performance outcome:</strong> Waiting for post-update distribution signals (24–48h delay).
                                </div>
                            </div>
                            """
                            st.markdown(attention_box, unsafe_allow_html=True)
                        elif diagnosis == "Engagement review":
                            attention_box = """
                            <div style="background: rgba(236, 72, 153, 0.08); border-left: 3px solid #EC4899; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                                <div style="font-weight: 700; color: #F472B6; font-size: 0.88rem;">💬 Still Needs Attention: Community & Delivery</div>
                                <div style="font-size: 0.83rem; color: #CBD5E1; margin-top: 2px;">
                                    Likes and comments per view remain below peers. Consider pinning a clarifying question or discussion prompt.
                                </div>
                                <div style="font-size: 0.80rem; color: #94A3B8; margin-top: 5px;">
                                    <strong style="color: #F1F5F9;">Performance outcome:</strong> Waiting for audience interaction signals.
                                </div>
                            </div>
                            """
                            st.markdown(attention_box, unsafe_allow_html=True)
                        else:
                            attention_box = """
                            <div style="background: rgba(59, 130, 246, 0.08); border-left: 3px solid #3B82F6; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                                <div style="font-weight: 700; color: #60A5FA; font-size: 0.88rem;">📊 Performance Outcome Monitoring</div>
                                <div style="font-size: 0.83rem; color: #CBD5E1; margin-top: 2px;">
                                    Waiting for sufficient post-update analytics to measure CTR and impressions changes (24–48h delay).
                                </div>
                            </div>
                            """
                            st.markdown(attention_box, unsafe_allow_html=True)

                    # Evidence and Caveat Chips
                    chips = [
                        evidence_chip("Measured Evidence", evidence, "warning" if "CTR" in evidence else "neutral"),
                    ]
                    if caveat:
                        chips.append(evidence_chip("Scope & Context", caveat, "neutral"))
                    st.markdown(f"<div style='margin-bottom: 12px;'>{''.join(chips)}</div>", unsafe_allow_html=True)
                    st.markdown(
                        f"""
                        <div style="font-size: 0.88rem; color: #94A3B8; background: #0F172A; border-radius: 8px; padding: 10px 14px; border-left: 3px solid #6366F1;">
                            <strong style="color: #FFFFFF;">Recommended Next Step:</strong> {next_step}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

                is_optimizing = st.session_state.get(f"active_today_optimizer_{vid}", False)

                with col_c2:
                    st.write("")
                    st.write("")
                    # Action button matched to diagnosis
                    if diagnosis == "Retention review":
                        if st.button("🎙️ Open Hook Lab", key=f"btn_hook_{p_idx}_{vid}", type="primary", use_container_width=True):
                            st.session_state["hook_lab_video_id"] = vid
                            st.session_state["hook_lab_video_title"] = act.get("title")
                            st.session_state["nav_destination"] = "ideas"
                            record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                            st.rerun()

                        if is_optimizing:
                            if st.button("✕ Close Studio", key=f"btn_close_opt_{p_idx}_{vid}", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = False
                                st.rerun()
                        else:
                            if st.button("⚡ Edit Metadata", key=f"btn_act_{p_idx}_{vid}", use_container_width=True, help="Update title, description, or tags (note: metadata changes do not fix retention or pacing)"):
                                st.session_state[f"active_today_optimizer_{vid}"] = True
                                record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                                st.rerun()

                    elif diagnosis == "Reach review":
                        if st.button("🔍 Research Demand", key=f"btn_reach_{p_idx}_{vid}", type="primary", use_container_width=True):
                            st.session_state["nav_destination"] = "ideas"
                            record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                            st.rerun()

                        if is_optimizing:
                            if st.button("✕ Close Studio", key=f"btn_close_opt_{p_idx}_{vid}", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = False
                                st.rerun()
                        else:
                            if st.button("⚡ Optimize Metadata", key=f"btn_act_{p_idx}_{vid}", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = True
                                record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                                st.rerun()

                    elif diagnosis == "Engagement review":
                        st.link_button("💬 Review Comments", f"https://www.youtube.com/watch?v={vid}", use_container_width=True)
                        if is_optimizing:
                            if st.button("✕ Close Studio", key=f"btn_close_opt_{p_idx}_{vid}", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = False
                                st.rerun()
                        else:
                            if st.button("⚡ Edit Metadata", key=f"btn_act_{p_idx}_{vid}", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = True
                                record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                                st.rerun()

                    else:
                        # Packaging review or baseline
                        if is_optimizing:
                            if st.button("✕ Close Studio", key=f"btn_close_opt_{p_idx}_{vid}", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = False
                                st.rerun()
                        else:
                            if st.button("⚡ Optimize & Publish Here", key=f"btn_act_{p_idx}_{vid}", type="primary", use_container_width=True):
                                st.session_state[f"active_today_optimizer_{vid}"] = True
                                record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                                st.rerun()

                        if st.button("Open in Workspace Tab ↗", key=f"btn_open_ws_{p_idx}_{vid}", use_container_width=True):
                            st.session_state["pending_optimizer_video_id"] = vid
                            st.session_state["pending_optimizer_video_title"] = act.get("title")
                            st.session_state["nav_destination"] = "agent_workspace"
                            record_growth_task_state(vid, "in_progress", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=is_demo)
                            st.rerun()

                    # Task management controls
                    if is_demo:
                        st.caption("Task controls disabled in Demo Mode")
                    else:
                        col_t1, col_t2 = st.columns(2)
                        with col_t1:
                            if st.button("Add to Pipeline", key=f"btn_pipe_{p_idx}_{vid}", use_container_width=True):
                                save_pipeline_item(
                                    item_id=f"pipe-{vid}",
                                    title=act.get("title"),
                                    stage="idea",
                                    target_format=act.get("metrics", {}).get("format", "long"),
                                    notes=f"Surfaced from briefing. Evidence: {act.get('evidence')}",
                                    owner_id=subject,
                                    is_demo=False,
                                )
                                st.toast("Saved to Ideas & Production pipeline!")
                        with col_t2:
                            if task_status == "resolved":
                                if st.button("↩ Reopen", key=f"btn_reopen_{p_idx}_{vid}", use_container_width=True):
                                    record_growth_task_state(vid, "new", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=False)
                                    st.rerun()
                            else:
                                if st.button("✓ Resolve", key=f"btn_resolve_{p_idx}_{vid}", use_container_width=True):
                                    record_growth_task_state(vid, "resolved", diagnosis=diagnosis, action_type=action_type, owner_id=subject, is_demo=False)
                                    st.rerun()

                # Inline Execution Studio (everything happens right on Today with no page switching)
                if is_optimizing:
                    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)
                    with st.container(border=True):
                        st.markdown(
                            f"""
                            <div style="background: rgba(99, 102, 241, 0.1); border-left: 3px solid #6366F1; border-radius: 4px; padding: 6px 12px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center;">
                                <span style="font-size: 0.82rem; font-weight: 700; color: #818CF8; text-transform: uppercase;">
                                    ⚡ Inline Execution Studio • Video ID: <code>{html.escape(vid)}</code>
                                </span>
                                <span style="font-size: 0.78rem; color: #94A3B8;">Formulate, review, approve, and publish directly on Today without page switching</span>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        render_video_optimizer(
                            video_id=vid,
                            youtube=youtube,
                            secrets=secrets,
                            subject=subject,
                            memory_creds=memory_creds,
                            is_demo=is_demo,
                            key_prefix=f"today_opt_{vid}",
                            inline_mode=True,
                        )

    # Secondary Section: AI Research Radar
    radar_items = briefing.get("research_radar", [])
    if radar_items:
        st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)
        st.markdown(
            """
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
                <h2 style="font-size: 1.35rem; font-weight: 700; margin: 0;">AI Research Radar</h2>
                <span style="color: #94A3B8; font-size: 0.85rem;">Breakthrough releases & tech angles</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        for r in radar_items:
            r_title = html.escape(r.get("title", ""))
            r_url = html.escape(r.get("source_url", "#"))
            r_summary = html.escape(r.get("summary", ""))
            r_cat = html.escape(r.get("category", "tech").replace("_", " ").title())
            r_date = html.escape(r.get("source_date") or r.get("discovered_date", ""))
            r_cached = r.get("is_cached", False)
            cache_tag = " [Cached]" if r_cached else ""
            is_live = r.get("is_live_ingested", False)
            source_badge = "Live Research" if is_live else "Curated Benchmark"
            badge_color = "#38BDF8" if is_live else "#A78BFA"
            badge_bg = "rgba(56, 189, 248, 0.15)" if is_live else "rgba(167, 139, 250, 0.15)"
            badge_border = "rgba(56, 189, 248, 0.3)" if is_live else "rgba(167, 139, 250, 0.3)"

            with st.container(border=True):
                col_r1, col_r2 = st.columns([3, 1])
                with col_r1:
                    st.markdown(
                        f"""
                        <div style="margin-bottom: 4px; display: flex; align-items: center; gap: 8px;">
                            <span style="background: {badge_bg}; color: {badge_color}; border: 1px solid {badge_border}; border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase;">
                                {source_badge}
                            </span>
                            <span style="background: rgba(16, 185, 129, 0.15); color: #34D399; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase;">
                                {r_cat}
                            </span>
                            <span style="color: #94A3B8; font-size: 0.78rem;">Source Date: {r_date}{cache_tag}</span>
                        </div>
                        <h4 style="margin: 4px 0 6px 0; font-size: 1.1rem; font-weight: 700;">
                            <a href="{r_url}" target="_blank" style="color: #FFFFFF; text-decoration: none;">{r_title} ↗</a>
                        </h4>
                        <p style="color: #CBD5E1; font-size: 0.9rem; margin-bottom: 8px;">{r_summary}</p>
                        """,
                        unsafe_allow_html=True,
                    )
                    if r.get("channel_relevance"):
                        st.markdown(
                            f"<p style='color: #818CF8; font-size: 0.83rem; margin-bottom: 6px;'><strong>Channel Fit:</strong> {html.escape(r.get('channel_relevance'))}</p>",
                            unsafe_allow_html=True,
                        )
                    angles = r.get("video_angles", [])
                    if angles:
                        st.markdown("<strong style='font-size: 0.85rem; color: #94A3B8;'>Creator Video Angles:</strong>", unsafe_allow_html=True)
                        for a in angles:
                            st.markdown(f"- **{html.escape(a.get('angle', ''))}**: {html.escape(a.get('pitch', ''))}")
                with col_r2:
                    st.write("")
                    if is_demo:
                        st.caption("Adoption disabled in Demo Mode")
                    else:
                        if st.button("➕ Adopt Concept", key=f"btn_radar_{r.get('id')}", use_container_width=True):
                            from core.creator_db import save_pipeline_item
                            save_pipeline_item(
                                item_id=f"radar-{r.get('id')}",
                                title=r.get("title"),
                                stage="idea",
                                target_format="long",
                                notes=f"Source: {r.get('source_url')}. Summary: {r.get('summary')}",
                                owner_id=subject,
                                is_demo=False,
                            )
                            st.toast("Adopted concept into Ideas pipeline!")
