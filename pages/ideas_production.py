"""Page: Ideas & Production (Pipeline, Hook Scoring & Creative Direction).

Organize ideas, multi-concept titles, hook scores, and production status.
- Handles hook evaluator with full dimension extraction.
- Fails closed safely if hook scoring tools are unavailable.
- In Demo Mode, operates purely on in-memory fixtures without modifying owner SQLite database.
- Provides deep-linkable views for Hook Lab and Topic Demand & Research with target video context preservation.
"""

import html
import json
import uuid
from typing import Any, Dict, List, Optional
import streamlit as st

from core.auth import is_demo_mode
from core.creator_db import (
    get_pipeline_items,
    save_pipeline_item,
    update_pipeline_stage,
    get_creator_preferences,
    get_radar_items,
)
from core.linter import score_hook_text
from core.llm import LLMRouter, _clean_json_text
from ui.components import demo_banner, status_pill


def _build_default_topic_demand(query: str) -> Dict[str, Any]:
    """Provide structured baseline search demand and framing angles when LLM is unavailable."""
    q_clean = query.strip() or "AI Engineering"
    return {
        "search_intent": f"Developers are looking for practical, benchmarked step-by-step implementations for '{q_clean}' with working code and minimal cost.",
        "high_demand_queries": [
            f"how to implement {q_clean} step by step",
            f"{q_clean} complete tutorial for developers",
            f"{q_clean} vs alternatives benchmark 2026",
            f"best open source {q_clean} workflow",
        ],
        "framing_angles": [
            {
                "angle_title": f"The Complete Guide to {q_clean} (Step-by-Step)",
                "hook_angle": "Direct Utility & Actionable Setup",
                "why_it_works": "High organic search volume from developers wanting runnable code and concrete steps.",
            },
            {
                "angle_title": f"Why Most Developers Fail at {q_clean} (And How to Fix It)",
                "hook_angle": "Loss Aversion & Pain Elimination",
                "why_it_works": "Curiosity and validation for developers who faced unexpected hurdles or poor performance.",
            },
            {
                "angle_title": f"I Benchmarked {q_clean} on Real Workloads — Here Are the Results",
                "hook_angle": "Empirical Proof & Real Data",
                "why_it_works": "Technical audiences respect concrete latency, memory, and cost data over generic overviews.",
            },
        ],
    }


def render_ideas_production(youtube=None, secrets=None, subject=None):
    """Render the Ideas & Production destination."""
    demo_banner()
    is_demo = is_demo_mode()
    secrets = secrets or {}

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <div style="color: #6366F1; font-weight: 700; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;">
                Content Workflow
            </div>
            <h1 style="font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; margin: 0 0 6px 0;">
                Ideas & Production
            </h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Track content concepts from ideation through script hook analysis to publication.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Synchronize deep-link navigation state from Today page
    incoming_tab = st.session_state.pop("ideas_active_tab", None)
    if incoming_tab == "hook_lab":
        st.session_state["ideas_view_selector"] = "🎯 Hook & Opening Review"
    elif incoming_tab == "research_demand":
        st.session_state["ideas_view_selector"] = "🔍 Topic Demand & Research"
    elif incoming_tab == "pipeline":
        st.session_state["ideas_view_selector"] = "📋 Production Pipeline"
    elif "ideas_view_selector" not in st.session_state:
        if st.session_state.get("hook_lab_video_id"):
            st.session_state["ideas_view_selector"] = "🎯 Hook & Opening Review"
        elif st.session_state.get("research_video_id"):
            st.session_state["ideas_view_selector"] = "🔍 Topic Demand & Research"
        else:
            st.session_state["ideas_view_selector"] = "📋 Production Pipeline"

    valid_options = ["📋 Production Pipeline", "🎯 Hook & Opening Review", "🔍 Topic Demand & Research"]
    current_choice = st.session_state.get("ideas_view_selector", valid_options[0])
    if current_choice not in valid_options:
        current_choice = valid_options[0]
        st.session_state["ideas_view_selector"] = current_choice

    current_idx = valid_options.index(current_choice)

    col_nav1, col_nav2 = st.columns([3, 1])
    with col_nav1:
        user_choice = st.radio(
            "Workflow Module",
            options=valid_options,
            index=current_idx,
            horizontal=True,
            label_visibility="collapsed",
        )
        selected_view = user_choice if user_choice in valid_options else current_choice
        st.session_state["ideas_view_selector"] = selected_view
    with col_nav2:
        if st.button("← Return to Today", use_container_width=True, key="ideas_return_today_top"):
            st.session_state["nav_destination"] = "today"
            st.rerun()

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # 1. VIEW: PRODUCTION PIPELINE
    if selected_view == "📋 Production Pipeline":
        col_btn1, col_btn2 = st.columns([3, 1])
        with col_btn2:
            if is_demo:
                st.caption("Pipeline edits disabled in Demo Mode")
            else:
                if st.button("➕ New Concept", use_container_width=True, type="primary", key="btn_new_concept"):
                    st.session_state["show_new_idea_form"] = True

        if st.session_state.get("show_new_idea_form") and not is_demo:
            with st.form("new_idea_form"):
                st.subheader("Add Content Idea")
                new_title = st.text_input("Working Title / Concept", placeholder="e.g. Fine-Tuning Llama 3.2 Locally with Unsloth")
                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    new_stage = st.selectbox("Stage", ["idea", "scripting", "recording", "published"])
                with col_f2:
                    new_format = st.selectbox("Target Format", ["long", "short"])
                new_notes = st.text_area("Concept Notes & Reference Links", placeholder="Key viewer questions or GitHub repo to demonstrate...")
                submitted = st.form_submit_button("Save Concept")
                if submitted and new_title:
                    save_pipeline_item(
                        item_id=f"idea-{uuid.uuid4().hex[:8]}",
                        title=new_title,
                        stage=new_stage,
                        target_format=new_format,
                        notes=new_notes,
                        owner_id=subject,
                        is_demo=False,
                    )
                    st.session_state["show_new_idea_form"] = False
                    st.toast("Concept saved!")
                    st.rerun()

        # Render Pipeline Columns using owner-scoped or demo-isolated data
        stages = [("idea", "💡 Ideas"), ("scripting", "✍️ Scripting"), ("recording", "🎥 Recording"), ("published", "✅ Published")]
        cols = st.columns(len(stages))

        items = get_pipeline_items(owner_id=subject, is_demo=is_demo)

        for idx, (stg_key, stg_label) in enumerate(stages):
            with cols[idx]:
                st.markdown(
                    f"""
                    <div style="background: #0F172A; border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; border-left: 3px solid #6366F1;">
                        <strong style="color: #FFFFFF; font-size: 0.95rem;">{stg_label}</strong>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                stage_items = [it for it in items if it.get("stage") == stg_key]
                if not stage_items:
                    st.caption("No items")
                for it in stage_items:
                    with st.container(border=True):
                        st.markdown(f"**{html.escape(it.get('title', ''))}**")
                        fmt = it.get("target_format", "long").title()
                        st.caption(f"Format: {fmt}")
                        if it.get("notes"):
                            st.markdown(f"<p style='color: #94A3B8; font-size: 0.82rem;'>{html.escape(it.get('notes'))}</p>", unsafe_allow_html=True)
                        if it.get("hook_text"):
                            st.caption(f"Hook: {html.escape(it['hook_text'][:60])}...")

                        # Move to next stage button
                        if stg_key != "published":
                            next_stage_map = {"idea": "scripting", "scripting": "recording", "recording": "published"}
                            next_stage = next_stage_map.get(stg_key)
                            if is_demo:
                                st.caption("Stage changes disabled in Demo Mode")
                            else:
                                if st.button("Advance ➔", key=f"adv_{it['id']}", use_container_width=True):
                                    update_pipeline_stage(it["id"], next_stage, owner_id=subject, is_demo=False)
                                    st.rerun()

    # 2. VIEW: HOOK & OPENING REVIEW
    elif selected_view == "🎯 Hook & Opening Review":
        st.subheader("Script Hook & Opening 15-Second Evaluator")
        st.caption("Score your opening lines against cognitive interest, clarity, and retention cliff prevention.")

        hook_vid_title = st.session_state.get("hook_lab_video_title")
        hook_vid_id = st.session_state.get("hook_lab_video_id")
        if hook_vid_title:
            col_hctx1, col_hctx2 = st.columns([4, 1])
            with col_hctx1:
                st.info(
                    f"🎯 **Target Video:** `{hook_vid_title}` (ID: `{hook_vid_id}`)  \n"
                    "This video was flagged for early viewer drop-off. Paste and score your revised opening 15-30 seconds below to test retention strength."
                )
            with col_hctx2:
                if st.button("Clear Context", key="btn_clear_hook_ctx", use_container_width=True):
                    st.session_state.pop("hook_lab_video_title", None)
                    st.session_state.pop("hook_lab_video_id", None)
                    st.rerun()

        hook_sample = st.text_area(
            "Paste Opening Script / First 15-30 Seconds",
            height=140,
            placeholder="In this video, I'll show you how to cut your LLM latency by 80% using prompt caching...",
            key="hook_eval_input",
        )

        col_h_act1, col_h_act2 = st.columns([1, 1])
        with col_h_act1:
            eval_clicked = st.button("Evaluate Hook", type="primary", use_container_width=True, key="btn_eval_hook")
        with col_h_act2:
            if st.button("← Return to Today", use_container_width=True, key="btn_hook_return_today"):
                st.session_state["nav_destination"] = "today"
                st.rerun()

        if eval_clicked:
            if not hook_sample.strip():
                st.warning("Please paste an opening hook to score.")
            else:
                eval_res = score_hook_text(hook_sample)
                st.session_state["last_hook_eval"] = eval_res

        eval_res = st.session_state.get("last_hook_eval")
        if eval_res is not None:
            overall = eval_res.get("overall_score", 0)
            col_sc1, col_sc2 = st.columns([1, 2])
            with col_sc1:
                score_color = "#10B981" if overall >= 80 else "#F59E0B" if overall >= 60 else "#EF4444"
                st.markdown(
                    f"""
                    <div style="background: #1E293B; border-radius: 12px; padding: 20px; text-align: center; border: 1px solid #334155;">
                        <div style="color: #94A3B8; font-size: 0.85rem; font-weight: 700; text-transform: uppercase;">Hook Score</div>
                        <div style="font-size: 3rem; font-weight: 800; color: {score_color};">{overall}/100</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with col_sc2:
                st.markdown("#### Hook Analysis & Dimensions")
                d_cols = st.columns(5)
                with d_cols[0]:
                    st.metric("Specificity", f"{eval_res.get('specificity', 0)}/100")
                with d_cols[1]:
                    st.metric("Address", f"{eval_res.get('address', 0)}/100")
                with d_cols[2]:
                    st.metric("Stakes", f"{eval_res.get('stakes', 0)}/100")
                with d_cols[3]:
                    st.metric("Curiosity", f"{eval_res.get('curiosity', 0)}/100")
                with d_cols[4]:
                    st.metric("Brevity", f"{eval_res.get('brevity', 0)}/100")

                if eval_res.get("note"):
                    st.caption(eval_res["note"])

            if not is_demo and hook_sample.strip():
                if st.button("➕ Save Scored Hook to Pipeline", use_container_width=True, key="btn_save_hook_pipeline"):
                    save_pipeline_item(
                        item_id=f"hook-{uuid.uuid4().hex[:8]}",
                        title=hook_vid_title or "Optimized Hook Script",
                        stage="scripting",
                        target_format="long",
                        notes=f"Scored Hook ({overall}/100): {hook_sample[:100]}...",
                        hook_text=hook_sample,
                        owner_id=subject,
                        is_demo=False,
                    )
                    st.toast("Saved scored hook to Pipeline under Scripting!")

    # 3. VIEW: TOPIC DEMAND & RESEARCH
    elif selected_view == "🔍 Topic Demand & Research":
        st.subheader("Topic Demand & Search Intent Research")
        st.caption("Identify high-demand audience search intents, framing angles, and keyword affinity to reverse impressions deficits.")

        res_vid_title = st.session_state.get("research_video_title")
        res_vid_id = st.session_state.get("research_video_id")
        res_evidence = st.session_state.get("research_video_evidence")

        if res_vid_title:
            col_rctx1, col_rctx2 = st.columns([4, 1])
            with col_rctx1:
                st.info(
                    f"🔍 **Target Video for Demand Research:** `{res_vid_title}` (ID: `{res_vid_id}`)  \n"
                    f"**Audited Signal:** {res_evidence or 'Impression and reach metrics trail peers. Research search demand and high-performing angles below.'}"
                )
            with col_rctx2:
                if st.button("Clear Context", key="btn_clear_res_ctx", use_container_width=True):
                    st.session_state.pop("research_video_title", None)
                    st.session_state.pop("research_video_id", None)
                    st.session_state.pop("research_video_evidence", None)
                    st.rerun()

        default_query = res_vid_title or ""
        research_query = st.text_input(
            "Topic or Working Title to Research",
            value=default_query,
            placeholder="e.g. Local LLM fine-tuning or AI agent architectures",
            key="topic_demand_query",
        )

        col_q1, col_q2 = st.columns([1, 1])
        with col_q1:
            run_research = st.button("Analyze Topic Demand & Framing", type="primary", use_container_width=True, key="btn_run_demand_research")
        with col_q2:
            if st.button("← Return to Today", use_container_width=True, key="btn_research_return_today"):
                st.session_state["nav_destination"] = "today"
                st.rerun()

        if run_research and research_query.strip():
            with st.spinner("Analyzing audience search intent and packaging angles..."):
                router = LLMRouter(secrets)
                if router.is_available():
                    prompt = (
                        f"Analyze YouTube audience demand for the topic: '{research_query}'.\n"
                        f"Channel niche: AI engineering, local LLMs, developer tutorials.\n"
                        f"Return a JSON object with keys:\n"
                        f"- 'search_intent': A 2-sentence summary of what developers are searching for.\n"
                        f"- 'high_demand_queries': list of 4 specific search queries viewers type.\n"
                        f"- 'framing_angles': list of 3 objects each with 'angle_title', 'hook_angle', and 'why_it_works'.\n"
                    )
                    try:
                        raw_text, _, _ = router.generate(prompt)
                        parsed = json.loads(_clean_json_text(raw_text))
                        if isinstance(parsed, dict) and "framing_angles" in parsed:
                            st.session_state["topic_demand_results"] = parsed
                        else:
                            st.session_state["topic_demand_results"] = _build_default_topic_demand(research_query)
                    except Exception:
                        st.session_state["topic_demand_results"] = _build_default_topic_demand(research_query)
                else:
                    st.session_state["topic_demand_results"] = _build_default_topic_demand(research_query)

        # Display Demand Analysis Results
        demand_data = st.session_state.get("topic_demand_results")
        if demand_data:
            with st.container(border=True):
                st.markdown("#### 🎯 Audience Search Intent")
                st.markdown(f"<p style='color: #CBD5E1; font-size: 0.95rem;'>{html.escape(demand_data.get('search_intent', ''))}</p>", unsafe_allow_html=True)

                st.markdown("#### 🔍 High-Demand Search Queries")
                queries = demand_data.get("high_demand_queries", [])
                q_cols = st.columns(len(queries) if queries else 1)
                for q_idx, q_text in enumerate(queries):
                    with q_cols[q_idx]:
                        st.markdown(
                            f"""
                            <div style="background: #1E293B; border-radius: 6px; padding: 8px 12px; font-size: 0.85rem; color: #38BDF8; border: 1px solid #334155;">
                                🔍 {html.escape(q_text)}
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )

                st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)
                st.markdown("#### 💡 High-Affinity Framing Angles")
                angles = demand_data.get("framing_angles", [])
                for a_idx, ang in enumerate(angles):
                    with st.container(border=True):
                        col_a1, col_a2 = st.columns([3, 1])
                        with col_a1:
                            st.markdown(f"**{html.escape(ang.get('angle_title', ''))}**")
                            st.markdown(f"<span style='color: #818CF8; font-size: 0.83rem;'>Angle Hook: {html.escape(ang.get('hook_angle', ''))}</span>", unsafe_allow_html=True)
                            st.markdown(f"<p style='color: #94A3B8; font-size: 0.85rem; margin-top: 4px;'>{html.escape(ang.get('why_it_works', ''))}</p>", unsafe_allow_html=True)
                        with col_a2:
                            if not is_demo:
                                if st.button("➕ Save to Pipeline", key=f"btn_save_angle_{a_idx}", use_container_width=True):
                                    save_pipeline_item(
                                        item_id=f"demand-{uuid.uuid4().hex[:8]}",
                                        title=ang.get("angle_title", research_query),
                                        stage="idea",
                                        target_format="long",
                                        notes=f"Search Angle: {ang.get('hook_angle')}. Rationale: {ang.get('why_it_works')}",
                                        owner_id=subject,
                                        is_demo=False,
                                    )
                                    st.toast("Saved angle to Production Pipeline!")
                            else:
                                st.caption("Pipeline edits disabled in Demo Mode")

        # Live Research Radar Feed
        st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)
        st.subheader("AI Research Radar (Live & Curated Tech Benchmarks)")
        st.caption("Breakthrough releases and technical hooks aligned with the AI Simplified Lab audience.")

        radar_items = get_radar_items(limit=10, owner_id=subject, is_demo=is_demo)
        if not radar_items:
            st.info("No active radar research items found. Radar items are refreshed during daily briefings.")
        else:
            for r in radar_items:
                r_title = html.escape(r.get("title", ""))
                r_url = html.escape(r.get("source_url", "#"))
                r_summary = html.escape(r.get("summary", ""))
                r_cat = html.escape(r.get("category", "tech").replace("_", " ").title())
                r_date = html.escape(r.get("source_date") or r.get("discovered_date", ""))
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
                                <span style="color: #94A3B8; font-size: 0.78rem;">Source Date: {r_date}</span>
                            </div>
                            <h4 style="margin: 4px 0 6px 0; font-size: 1.05rem; font-weight: 700;">
                                <a href="{r_url}" target="_blank" style="color: #FFFFFF; text-decoration: none;">{r_title} ↗</a>
                            </h4>
                            <p style="color: #CBD5E1; font-size: 0.9rem; margin-bottom: 6px;">{r_summary}</p>
                            """,
                            unsafe_allow_html=True,
                        )
                        if r.get("channel_relevance"):
                            st.markdown(
                                f"<p style='color: #818CF8; font-size: 0.83rem; margin-bottom: 6px;'><strong>Channel Fit:</strong> {html.escape(r.get('channel_relevance'))}</p>",
                                unsafe_allow_html=True,
                            )
                    with col_r2:
                        st.write("")
                        if is_demo:
                            st.caption("Adoption disabled in Demo Mode")
                        else:
                            if st.button("➕ Adopt Angle", key=f"btn_r_adopt_{r.get('id')}", use_container_width=True):
                                save_pipeline_item(
                                    item_id=f"radar-{r.get('id')}",
                                    title=r.get("title"),
                                    stage="idea",
                                    target_format="long",
                                    notes=f"Source: {r.get('source_url')}. Summary: {r.get('summary')}",
                                    owner_id=subject,
                                    is_demo=False,
                                )
                                st.toast("Adopted angle into Ideas pipeline!")
