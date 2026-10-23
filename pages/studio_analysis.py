"""Page: Find Videos & Studio Analysis.

Upload Studio CSV/TSV/XLSX exports, detect metrics, and review one next-best action at a time.
"""

from typing import Any, Dict, List, Optional
import streamlit as st

from core.auth import is_demo_mode
from core.demo_data import DEMO_STUDIO_ANALYSIS
import studio_export_analyzer as analyzer
from ui.components import demo_banner, evidence_chip, empty_state


def render_studio_analysis(youtube=None):
    """Render Studio export analysis workspace."""
    demo_banner()
    is_demo = is_demo_mode()

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <h1 style="font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px;">Find Videos & Studio Export Analysis</h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Drag and drop your YouTube Studio CSV or Excel exports for deep audience retention and CTR comparison.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 1. Multi-file uploader
    with st.expander("Upload YouTube Studio Exports (CSV, TSV, XLSX)", expanded=not is_demo):
        uploaded_files = st.file_uploader(
            "Select one or more exports from YouTube Studio Advanced Mode",
            type=["csv", "tsv", "txt", "xlsx"],
            accept_multiple_files=True,
            key="studio_file_uploader",
        )
        st.caption(
            "Supported reports: Content/Video performance, Audience retention curves, Daily view trends. "
            "Files are parsed in-memory and never written to disk or cloud drives. "
            "See [STUDIO_EXPORT_GUIDE.md](file:///c:/Users/USER/Desktop/Youtube-AI_Simplified/Yotube%20strategist/youtube-agent-skill/STUDIO_EXPORT_GUIDE.md) for export steps."
        )

    # In Demo Mode, simulate parsed files if nothing is uploaded
    analysis_data = None
    if uploaded_files:
        with st.spinner("Parsing reports and detecting metrics..."):
            try:
                parsed_bundle = analyzer.parse_uploaded_files(uploaded_files)
                st.session_state["parsed_studio_bundle"] = parsed_bundle
            except Exception as e:
                st.error(f"Error parsing uploaded files: {str(e)}")

    if st.session_state.get("parsed_studio_bundle"):
        bundle = st.session_state["parsed_studio_bundle"]
        # Format detected summary
        reports_summary = []
        for f in bundle.get("files", []):
            reports_summary.append({
                "Filename": f.get("name"),
                "Type": f.get("report_type", "Unknown"),
                "Rows": f.get("row_count", 0),
                "Range": f.get("date_range", "Lifetime / Unspecified"),
            })
        candidates = bundle.get("ranked_actions", [])
        recognized_cols = bundle.get("recognized_columns", [])
        ignored_cols = bundle.get("ignored_columns", [])
    elif is_demo:
        demo = DEMO_STUDIO_ANALYSIS
        reports_summary = demo["detected_reports"]
        candidates = demo["candidates"]
        recognized_cols = demo["recognized_columns"]
        ignored_cols = demo["ignored_columns"]
    else:
        empty_state(
            "No Studio reports uploaded",
            "Export Content or Retention reports from YouTube Studio Analytics (Advanced mode) to discover videos with click-through and retention gaps.",
            icon="📁",
        )
        return

    # 2. What We Detected Summary
    with st.container(border=True):
        st.markdown("### What We Detected")
        col_det1, col_det2 = st.columns(2)
        with col_det1:
            st.markdown(f"**Recognized Reports:** {len(reports_summary)}")
            for r in reports_summary:
                st.caption(f"• `{r.get('Filename', r.get('filename'))}` ({r.get('Type', r.get('type'))})")
        with col_det2:
            st.markdown(f"**Columns Recognized ({len(recognized_cols)}):**")
            st.caption(", ".join(recognized_cols[:8]) + ("..." if len(recognized_cols) > 8 else ""))
            if ignored_cols:
                st.caption(f"**Ignored ({len(ignored_cols)}):** " + ", ".join(ignored_cols[:4]))

    st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)

    # 3. ONE Next-Best Action Card at a time
    if not candidates:
        st.info("No actionable underperformance candidates found in the uploaded data.")
        return

    # Index management with Skip & Undo
    candidate_idx = st.session_state.setdefault("studio_candidate_idx", 0)
    if candidate_idx >= len(candidates):
        candidate_idx = len(candidates) - 1
        st.session_state["studio_candidate_idx"] = candidate_idx

    current_cand = candidates[candidate_idx]
    total_candidates = len(candidates)

    # Progress indicator
    st.markdown(f"**Reviewing candidate {candidate_idx + 1} of {total_candidates}**")
    st.progress((candidate_idx + 1) / total_candidates)

    evidence_dict = current_cand.get("evidence", {})
    chips = []
    if "ctr" in evidence_dict:
        chips.append(evidence_chip("CTR vs Baseline", str(evidence_dict["ctr"]), "warning"))
    if "apv" in evidence_dict:
        chips.append(evidence_chip("Avg % Viewed", str(evidence_dict["apv"]), "neutral"))
    if "cliff" in evidence_dict:
        chips.append(evidence_chip("Retention Cliff", str(evidence_dict["cliff"]), "alert"))
    chips_html = "".join(chips)

    st.markdown(
        f"""
        <div class="hero-action-card">
            <div class="hero-tag">🎯 Candidate #{candidate_idx + 1}</div>
            <div class="hero-title">{current_cand.get('title', 'Video Optimization Candidate')}</div>
            <div class="hero-subtitle">{current_cand.get('why', 'Identified as having high potential reach with room for packaging improvements.')}</div>
            <div style="margin-bottom: 16px;">{chips_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_btn1, col_btn2, col_btn3, col_btn4 = st.columns([1.5, 1, 1, 1])

    with col_btn1:
        if st.button("Do it — build proposal", type="primary", use_container_width=True, key=f"studio_do_it_{candidate_idx}"):
            st.session_state["pending_optimizer_video_id"] = current_cand.get("video_id")
            st.session_state["pending_optimizer_video_title"] = current_cand.get("title")
            st.session_state["nav_destination"] = "pages/strategist.py"
            st.rerun()

    with col_btn2:
        if st.button("Skip", use_container_width=True, key=f"studio_skip_{candidate_idx}"):
            if candidate_idx < total_candidates - 1:
                st.session_state["studio_candidate_idx"] = candidate_idx + 1
                st.session_state["can_undo_skip"] = True
                st.rerun()
            else:
                st.toast("Reached the end of candidates.")

    with col_btn3:
        if st.session_state.get("can_undo_skip", False) and candidate_idx > 0:
            if st.button("Undo Skip", use_container_width=True, key="studio_undo_skip"):
                st.session_state["studio_candidate_idx"] = candidate_idx - 1
                st.rerun()

    with col_btn4:
        with st.popover("Why this?"):
            st.markdown(
                f"""
                **Ranking Logic:**
                - Minimum sample check: Evaluates videos with sufficient impressions/views.
                - Peer comparison: Ranks videos where CTR or watch time is below channel baseline.
                - Retention cliffs: Identifies drops in audience retention where viewers exited.
                - *Honesty note:* Prioritizes review; does not prove metadata caused performance.
                """
            )
