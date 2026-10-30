"""Page: Ideas & Production (Pipeline, Hook Scoring & Creative Direction).

Organize ideas, multi-concept titles, hook scores, and production status.
- Handles hook evaluator with full dimension extraction.
- Fails closed safely if hook scoring tools are unavailable.
- In Demo Mode, operates purely on in-memory fixtures without modifying owner SQLite database.
"""

import html
import uuid
import streamlit as st

from core.auth import is_demo_mode
from core.creator_db import (
    get_pipeline_items,
    save_pipeline_item,
    update_pipeline_stage,
    get_creator_preferences,
)
from core.linter import score_hook_text
from ui.components import demo_banner, status_pill


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

    tab_pipeline, tab_hook_lab = st.tabs(["📋 Production Pipeline", "🎯 Hook & Opening Review"])

    with tab_pipeline:
        col_btn1, col_btn2 = st.columns([3, 1])
        with col_btn2:
            if is_demo:
                st.caption("Pipeline edits disabled in Demo Mode")
            else:
                if st.button("➕ New Concept", use_container_width=True, type="primary"):
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

    with tab_hook_lab:
        st.subheader("Script Hook & Opening 15-Second Evaluator")
        st.caption("Score your opening lines against cognitive interest, clarity, and retention cliff prevention.")

        hook_sample = st.text_area(
            "Paste Opening Script / First 15-30 Seconds",
            height=140,
            placeholder="In this video, I'll show you how to cut your LLM latency by 80% using prompt caching...",
            key="hook_eval_input",
        )

        if st.button("Evaluate Hook", type="primary"):
            if not hook_sample.strip():
                st.warning("Please paste an opening hook to score.")
            else:
                eval_res = score_hook_text(hook_sample)
                if eval_res is None:
                    st.warning("⚠️ Hook scoring tool is currently unavailable in this environment.")
                else:
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
