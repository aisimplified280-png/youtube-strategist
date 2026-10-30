"""Page: Agent Workspace (Research, Creative Formulation & Exact Approval Execution).

Dedicated hub where the creator reviews, refines, and safely applies metadata optimizations.
Guarantees:
- Never writes without explicit approval of the exact proposal.
- Re-fetches current YouTube metadata immediately before update.
- Verifies post-write state.
- Editing invalidates pending approval hash.
- Immutable snippet fields are preserved.
- Public writes are strictly disabled in Demo Mode.
"""

import copy
import html
import json
from typing import Any, Dict, List, Optional
import streamlit as st

from core.auth import is_demo_mode
from core.demo_data import DEMO_HERO_ACTION
from core.llm import LLMRouter, LLMError
from core.linter import lint_proposal
from core.prompts import build_strategist_prompt
from core.metadata_apply import compute_proposal_digest, apply_proposal
from core.creator_db import record_proposal_decision, get_creator_preferences, mark_growth_task_applied, get_latest_daily_briefing
from ui.components import demo_banner, diff_viewer, char_counter, status_pill, error_state, connection_status_banner
import strategist_chat
import youtube_analytics_connector as connector


def _clear_workspace_proposal_state():
    """Reset proposal, approval digest, and widget input state cleanly."""
    st.session_state.pop("workspace_proposal", None)
    st.session_state.pop("workspace_approved_digest", None)
    st.session_state.pop("prop_title_in", None)
    st.session_state.pop("prop_desc_in", None)
    st.session_state.pop("prop_tags_in", None)


def render_workspace(youtube=None, secrets=None, subject=None, memory_creds=None):
    """Render the Agent Workspace."""
    demo_banner()
    is_demo = is_demo_mode()
    secrets = secrets or {}

    # Reset proposal inputs cleanly at the start of rerun BEFORE widgets are rendered
    if st.session_state.pop("_deferred_workspace_reset", False):
        st.session_state.pop("prop_title_in", None)
        st.session_state.pop("prop_desc_in", None)
        st.session_state.pop("prop_tags_in", None)
        st.session_state.pop("workspace_proposal", None)
        st.session_state.pop("workspace_approved_digest", None)

    # Display applied success banner across reruns if present
    applied_notice = st.session_state.pop("_applied_success_notice", None)
    if applied_notice:
        st.success(applied_notice)
        st.balloons()

    # Actionable outside connection banner when disconnected
    connection_status_banner(secrets, subject=subject, yt_creds=youtube, memory_creds=memory_creds, is_demo=is_demo)

    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <div style="color: #6366F1; font-weight: 700; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px;">
                Execution & Approval Hub
            </div>
            <h1 style="font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; margin: 0 0 6px 0;">
                Agent Workspace
            </h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Research evidence, formulate packaging options, and execute approved metadata updates with post-write verification.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 1. Video Selection & State Resolution
    target_video_id = st.session_state.get("pending_optimizer_video_id")
    target_video_title = st.session_state.get("pending_optimizer_video_title")

    if not target_video_id and is_demo:
        target_video_id = DEMO_HERO_ACTION["video_id"]
        target_video_title = DEMO_HERO_ACTION["title"]

    last_active_id = st.session_state.get("workspace_active_video_id")
    if target_video_id and last_active_id and target_video_id != last_active_id:
        st.session_state.pop("workspace_target_context", None)
        _clear_workspace_proposal_state()
    if target_video_id:
        st.session_state["workspace_active_video_id"] = target_video_id

    # Selection bar
    with st.expander("Target Video Selector", expanded=not bool(target_video_id)):
        # Recent opportunities / uploads options (ensure persistent briefing is loaded across pages)
        recent_options = []
        daily_b = st.session_state.get("daily_briefing")
        if not daily_b and not is_demo:
            daily_b = get_latest_daily_briefing(channel_id="UCS6Sb5rfaEnWTVPGyQlnHFQ", owner_id=subject, is_demo=False)
            if daily_b:
                st.session_state["daily_briefing"] = daily_b
        daily_b = daily_b or {}
        briefing_actions = daily_b.get("prioritized_actions", [])
        for act in briefing_actions:
            if act.get("video_id") and act.get("title"):
                recent_options.append((act["video_id"], f"Priority #{act.get('priority', '')}: {act['title']}"))

        # From dashboard video rows if available
        d_rows = st.session_state.get("dashboard_data", {}).get("video_rows", [])
        for r in d_rows[:10]:
            rid = r.get("video_id")
            rtitle = r.get("title")
            if rid and rtitle and not any(opt[0] == rid for opt in recent_options):
                recent_options.append((rid, f"Upload: {rtitle}"))

        if not recent_options and is_demo:
            recent_options = [
                ("demo-vid-101", "Demo: Agentic Coding with Gemini 1.5 Pro: Architecture Guide"),
                ("demo-vid-102", "Demo: Evaluating Multi-Agent Systems in Python: Benchmarks"),
                ("demo-vid-103", "Demo: 3 Secrets to Agent Reliability (Short)"),
            ]

        if recent_options:
            selected_tuple = st.selectbox(
                "Select from recommended opportunities or recent uploads:",
                options=recent_options,
                format_func=lambda x: x[1],
                key="ws_recent_select",
            )
            if selected_tuple and selected_tuple[0] != target_video_id:
                if st.button(f"Load '{selected_tuple[1][:45]}...'", key="btn_load_recent", use_container_width=True):
                    st.session_state["pending_optimizer_video_id"] = selected_tuple[0]
                    st.session_state.pop("pending_optimizer_video_title", None)
                    st.session_state.pop("workspace_target_context", None)
                    _clear_workspace_proposal_state()
                    st.rerun()

            st.markdown("<div style='text-align:center; color:#64748B; font-size:0.8rem; margin:6px 0;'>— OR ENTER DIRECT URL / ID —</div>", unsafe_allow_html=True)

        col_s1, col_s2 = st.columns([3, 1])
        with col_s1:
            video_input = st.text_input(
                "YouTube Video URL or 11-char ID",
                value=target_video_id or "",
                placeholder="e.g. https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                key="workspace_input_url",
            )
        with col_s2:
            st.write("")
            st.write("")
            if st.button("Load Video", use_container_width=True, type="primary"):
                try:
                    resolved_id = strategist_chat.parse_video_id(video_input)
                    st.session_state["pending_optimizer_video_id"] = resolved_id
                    st.session_state.pop("pending_optimizer_video_title", None)
                    st.session_state.pop("workspace_target_context", None)
                    _clear_workspace_proposal_state()
                    st.rerun()
                except ValueError as err:
                    st.error(str(err))

    if not target_video_id:
        st.info("Select an action card from Today, choose from the dropdown, or enter a video URL above to begin optimization.")
        return

    render_video_optimizer(
        video_id=target_video_id,
        youtube=youtube,
        secrets=secrets,
        subject=subject,
        memory_creds=memory_creds,
        is_demo=is_demo,
        key_prefix="ws",
        inline_mode=False,
    )


def render_video_optimizer(
    video_id: str,
    youtube=None,
    secrets=None,
    subject=None,
    memory_creds=None,
    is_demo: bool = False,
    key_prefix: str = "ws",
    inline_mode: bool = False,
):
    """Render creative formulation, linter, diff, approval gate, and publish execution."""
    secrets = secrets or {}

    # State key names (strict backward-compatibility for workspace tests when key_prefix == 'ws')
    prop_key = "workspace_proposal" if key_prefix == "ws" else f"{key_prefix}_proposal"
    digest_key = "workspace_approved_digest" if key_prefix == "ws" else f"{key_prefix}_approved_digest"
    title_in_key = "prop_title_in" if key_prefix == "ws" else f"{key_prefix}_title_in"
    desc_in_key = "prop_desc_in" if key_prefix == "ws" else f"{key_prefix}_desc_in"
    tags_in_key = "prop_tags_in" if key_prefix == "ws" else f"{key_prefix}_tags_in"
    ctx_key = "workspace_target_context" if key_prefix == "ws" else f"{key_prefix}_context"

    # Context extraction & caching
    ctx = st.session_state.get(ctx_key)
    # Evict corrupted or error cache entries automatically
    if ctx and (ctx.get("snippet", {}).get("title") == "Error loading video" or ctx.get("error")):
        st.session_state.pop(ctx_key, None)
        ctx = None

    if not ctx:
        with st.spinner("Retrieving video metadata and channel peer evidence..."):
            ctx = _fetch_workspace_context(video_id, youtube=youtube, is_demo=is_demo)
            st.session_state[ctx_key] = ctx

    if ctx.get("error") or not ctx.get("snippet"):
        st.error(f"❌ Could not load video metadata from YouTube for video ID `{video_id}`: {ctx.get('error', 'Metadata unavailable')}")
        st.info("Please verify your YouTube channel connection in Settings or enter another valid video ID.")
        return

    current_snippet = ctx.get("snippet", {})

    # Display video banner in full workspace mode
    if not inline_mode:
        st.markdown(
            f"""
            <div style="background: #1E293B; border: 1px solid #334155; border-radius: 12px; padding: 14px 18px; margin-bottom: 20px;">
                <div style="font-size: 0.78rem; font-weight: 700; color: #818CF8; text-transform: uppercase;">Active Target Video</div>
                <div style="font-size: 1.15rem; font-weight: 700; color: #FFFFFF; margin: 2px 0 4px 0;">{html.escape(current_snippet.get('title', 'Unknown Title'))}</div>
                <div style="font-size: 0.85rem; color: #94A3B8;">
                    Video ID: <code>{html.escape(video_id)}</code> • Duration: {ctx.get('duration_str', 'N/A')} • Views: {ctx.get('metrics', {}).get('views', 0):,}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Two-Column Layout: Left = Creative Formulation & Linting; Right = Exact Diff & Approval Gate
    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.subheader("1. Formulate Concepts")

        # Load or generate candidate proposal
        proposal = st.session_state.get(prop_key)
        if not proposal:
            proposal = {
                "title": current_snippet.get("title", ""),
                "description": current_snippet.get("description", ""),
                "tags": current_snippet.get("tags", []),
                "rationale": "Base snippet loaded. Click 'Generate AI Concepts' to formulate optimized angles.",
            }
            st.session_state[prop_key] = proposal

        col_g1, col_g2 = st.columns(2)
        with col_g1:
            if st.button("⚡ Generate AI Concepts", key=f"{key_prefix}_btn_gen_ai", use_container_width=True, type="primary"):
                with st.spinner("Generating evidence-grounded options..."):
                    router = LLMRouter(secrets=secrets)
                    prefs = get_creator_preferences(owner_id=subject, is_demo=is_demo)
                    prompt = build_strategist_prompt(
                        target_snippet=current_snippet,
                        instruction=f"Optimize packaging for AI tech audience. Tone: {prefs.get('voice_tone')}. Prohibited: {prefs.get('prohibited_words')}",
                    )
                    try:
                        parsed, provider, model = router.generate_structured_proposal(prompt)
                        if parsed:
                            st.session_state[prop_key] = parsed
                            st.session_state.pop(digest_key, None)
                            st.session_state.pop(title_in_key, None)
                            st.session_state.pop(desc_in_key, None)
                            st.session_state.pop(tags_in_key, None)
                            st.rerun()
                        else:
                            st.error("AI returned non-standard format. Failing closed without altering proposal.")
                    except LLMError as err:
                        st.error(f"LLM formulation error ({err.provider}): {str(err)}")

        with col_g2:
            if st.button("Reset to Current", key=f"{key_prefix}_btn_reset_cur", use_container_width=True):
                st.session_state[prop_key] = {
                    "title": current_snippet.get("title", ""),
                    "description": current_snippet.get("description", ""),
                    "tags": current_snippet.get("tags", []),
                    "rationale": "Reset to YouTube published values.",
                }
                st.session_state.pop(digest_key, None)
                st.session_state.pop(title_in_key, None)
                st.session_state.pop(desc_in_key, None)
                st.session_state.pop(tags_in_key, None)
                st.rerun()

        # Editable fields
        st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)
        edit_title = st.text_input("Proposed Title", value=proposal.get("title", ""), key=title_in_key)
        st.markdown(char_counter(len(edit_title), 100, warn_len=60), unsafe_allow_html=True)

        edit_desc = st.text_area("Proposed Description", value=proposal.get("description", ""), height=180, key=desc_in_key)
        st.markdown(char_counter(len(edit_desc), 5000, warn_len=4500), unsafe_allow_html=True)

        tags_raw = ", ".join(proposal.get("tags", [])) if isinstance(proposal.get("tags"), list) else str(proposal.get("tags", ""))
        edit_tags_str = st.text_input("Proposed Tags (comma-separated)", value=tags_raw, key=tags_in_key)
        st.markdown(char_counter(len(edit_tags_str), 500, warn_len=400), unsafe_allow_html=True)
        edit_tags = [t.strip() for t in edit_tags_str.split(",") if t.strip()]

        # Update proposal state if user typed edits
        if (
            edit_title != proposal.get("title")
            or edit_desc != proposal.get("description")
            or edit_tags != proposal.get("tags")
        ):
            proposal["title"] = edit_title
            proposal["description"] = edit_desc
            proposal["tags"] = edit_tags
            st.session_state[prop_key] = proposal
            # Invalidate approval hash on ANY modification
            st.session_state.pop(digest_key, None)

        # Real-time Linter checks
        lint_results = lint_proposal(proposal, thumbnail_concept=None)
        with st.expander("Packaging Linter Analysis", expanded=True):
            warnings = lint_results.get("warnings", [])
            passes = lint_results.get("passes", [])
            if not warnings:
                st.success("✅ Clean packaging passed all mobile, truncation, and character limits.")
            else:
                for warn in warnings:
                    st.warning(f"⚠️ {warn}")
            if passes:
                with st.expander("Passed Verifications", expanded=False):
                    for p in passes:
                        st.caption(f"✓ {p}")

    # Build proposed snippet preserving all immutable snippet fields
    proposed_snip = copy.deepcopy(current_snippet)
    proposed_snip["title"] = proposal.get("title", "")
    proposed_snip["description"] = proposal.get("description", "")
    proposed_snip["tags"] = proposal.get("tags", [])

    with col_right:
        st.subheader("2. Review Diff & Approval Gate")

        # Display side-by-side diff using 3-argument signature
        st.markdown("**Title Comparison**")
        diff_viewer(before=current_snippet.get("title", ""), after=proposal.get("title", ""), label="Title Comparison")

        st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
        with st.expander("Description Comparison", expanded=False):
            diff_viewer(before=current_snippet.get("description", ""), after=proposal.get("description", ""), label="Description Comparison")

        with st.expander("Tags Comparison", expanded=False):
            st.write("**Current Tags:**", current_snippet.get("tags", []))
            st.write("**Proposed Tags:**", proposal.get("tags", []))

        # Compute deterministic digest using canonical parameters
        current_digest = compute_proposal_digest(
            video_id=video_id,
            before_snippet=current_snippet,
            proposed_snippet=proposed_snip,
        )

        st.markdown(
            f"""
            <div style="background: #0F172A; border-radius: 8px; padding: 10px 14px; margin: 16px 0; border: 1px solid #334155;">
                <div style="font-size: 0.75rem; color: #94A3B8; text-transform: uppercase; font-weight: 700;">Proposal Cryptographic Digest</div>
                <code style="color: #6366F1; font-size: 0.82rem;">{current_digest}</code>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Approval State Handling
        approved_digest = st.session_state.get(digest_key)
        is_approved = (approved_digest == current_digest)

        col_app1, col_app2 = st.columns(2)
        with col_app1:
            if not is_approved:
                if st.button("✅ Approve This Exact Proposal", key=f"{key_prefix}_btn_approve", use_container_width=True, type="primary"):
                    st.session_state[digest_key] = current_digest
                    if not is_demo:
                        record_proposal_decision(
                            video_id=video_id,
                            proposal_digest=current_digest,
                            status="approved",
                            proposal_dict=proposal,
                            feedback="Creator approved proposal digest in Agent Workspace.",
                            owner_id=subject,
                            is_demo=False,
                        )
                    st.toast("Proposal approved! Ready for YouTube write.")
                    st.rerun()
            else:
                st.success("Proposal Approved! Verified hash locked.")
                if st.button("Revoke Approval", key=f"{key_prefix}_btn_revoke", use_container_width=True):
                    st.session_state.pop(digest_key, None)
                    st.rerun()

        with col_app2:
            if st.button("❌ Decline Proposal", key=f"{key_prefix}_btn_decline", use_container_width=True):
                st.session_state.pop(digest_key, None)
                if not is_demo:
                    record_proposal_decision(
                        video_id=video_id,
                        proposal_digest=current_digest,
                        status="declined",
                        proposal_dict=proposal,
                        feedback="Declined in Workspace.",
                        owner_id=subject,
                        is_demo=False,
                    )
                st.warning("Proposal declined and recorded.")
                st.rerun()

        st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

        # 4. Final Write to YouTube (Strictly Approval Gated and Disabled in Demo Mode)
        if is_approved:
            with st.container(border=True):
                st.markdown("<h4 style='margin:0 0 8px 0; color:#FFFFFF;'>Ready to Apply to YouTube</h4>", unsafe_allow_html=True)
                st.caption("Writes to YouTube Data API v3 snippet (title, description, tags). Pre-write check verifies current metadata has not changed.")

                if is_demo:
                    st.info("🔒 Public write actions are disabled in Demo Mode. Connect your channel in Settings to apply changes to YouTube.")
                    st.button("🚀 Push to YouTube Now (Disabled in Demo Mode)", key=f"{key_prefix}_btn_push_demo", disabled=True, use_container_width=True)
                else:
                    if st.button("🚀 Push to YouTube Now", key=f"{key_prefix}_btn_push_live", type="primary", use_container_width=True):
                        with st.spinner("Verifying snippet and updating YouTube..."):
                            res = apply_proposal(
                                youtube=youtube,
                                video_id=video_id,
                                before_snippet=current_snippet,
                                proposed_snippet=proposed_snip,
                                approved_digest=approved_digest,
                                is_demo=False,
                            )
                            if res.get("status") == "success":
                                success_msg = f"🎉 Update Applied and Verified! {res.get('message', '')}"
                                st.success(success_msg)
                                st.balloons()
                                # Record as APPLIED in Creator Memory and transition growth task to applied_monitoring
                                mark_growth_task_applied(
                                    video_id=video_id,
                                    proposal_dict=proposed_snip,
                                    proposal_digest=approved_digest or compute_proposal_digest(video_id, ctx.get("snippet", {}), proposed_snip),
                                    notes=f"Applied via {key_prefix} studio and post-write verified",
                                    baseline_metrics=ctx.get("metrics", {}),
                                    owner_id=subject,
                                    is_demo=is_demo,
                                )
                                # Signal briefing refresh so Today reflects the applied status
                                st.session_state["force_briefing_refresh"] = True

                                if memory_creds:
                                    try:
                                        from content_memory import GoogleSheetsMemoryStore
                                        mem_store = GoogleSheetsMemoryStore.from_credentials(memory_creds)
                                        mem_store.append_experiment(
                                            video_id=video_id,
                                            video_url=f"https://www.youtube.com/watch?v={video_id}",
                                            title=proposed_snip.get("title", ""),
                                            experiment_type="metadata_packaging",
                                            baseline_metrics=ctx.get("metrics", {}),
                                            notes=proposal.get("rationale", "Applied via Agent Workspace"),
                                        )
                                        st.toast("Experiment logged to Google Sheets memory!")
                                    except Exception:
                                        pass
                                if key_prefix == "ws":
                                    st.session_state["_deferred_workspace_reset"] = True
                                    st.session_state["_applied_success_notice"] = success_msg
                                    st.session_state.pop(ctx_key, None)
                                else:
                                    # Defer widget reset to next rerun to avoid modifying instantiated widget keys
                                    st.session_state[f"_deferred_inline_reset_{video_id}"] = key_prefix
                                    st.session_state[f"_applied_inline_notice_{video_id}"] = success_msg
                                st.rerun()
                            else:
                                st.error(f"Apply failed: {res.get('message', 'Unknown error')}")


def _fetch_workspace_context(video_id: str, youtube=None, is_demo: bool = False) -> Dict[str, Any]:
    """Fetch video context, duration, and metrics."""
    is_demo_id = str(video_id or "").startswith("demo-")

    # 1. Demo Mode: Permitted only when is_demo=True or when a demo video is selected in demo mode
    if is_demo:
        return {
            "snippet": {
                "title": DEMO_HERO_ACTION["title"],
                "description": "Learn how to build multi-agent workflows in Python with deterministic evaluation.",
                "tags": ["ai", "agents", "python", "tutorial"],
                "categoryId": "28",
            },
            "duration_str": "14:22",
            "metrics": {
                "views": DEMO_HERO_ACTION["views"],
                "ctr": DEMO_HERO_ACTION["ctr"],
            },
        }

    # 2. Real Mode: Fail closed if YouTube connection is missing
    if youtube is None:
        return {
            "error": "YouTube connection is unavailable. Please connect your YouTube account in Settings.",
            "snippet": None,
            "duration_str": "N/A",
            "metrics": {"views": 0},
        }

    # 3. Real Mode: Reject demo video IDs when not in demo mode
    if is_demo_id:
        return {
            "error": f"Demo video ID '{video_id}' cannot be loaded in production mode.",
            "snippet": None,
            "duration_str": "N/A",
            "metrics": {"views": 0},
        }

    # 4. Check if the video snippet is already available in dashboard_data / briefing
    d_rows = st.session_state.get("dashboard_data", {}).get("video_rows", [])
    found_row = next((r for r in d_rows if r.get("video_id") == video_id), None)
    if found_row and found_row.get("snippet") and isinstance(found_row.get("snippet"), dict):
        snippet = found_row["snippet"]
        views = found_row.get("views", 0)
        duration_sec = found_row.get("duration_seconds", 0)
        m, s = divmod(duration_sec, 60)
        h, m = divmod(m, 60)
        duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        return {
            "snippet": copy.deepcopy(snippet),
            "duration_str": duration_str,
            "metrics": {"views": views, "ctr": found_row.get("ctr")},
        }

    try:
        snippet = connector.fetch_video_snippet(youtube, video_id)
        views = 0
        try:
            stat_resp = youtube.videos().list(part="statistics,contentDetails", id=video_id).execute()
            items = stat_resp.get("items", [])
            if items:
                views = int(items[0].get("statistics", {}).get("viewCount", 0))
                duration_raw = items[0].get("contentDetails", {}).get("duration", "PT0S")
                duration_str = duration_raw.replace("PT", "").lower()
            else:
                duration_str = "N/A"
        except Exception:
            duration_str = "N/A"

        return {
            "snippet": snippet,
            "duration_str": duration_str,
            "metrics": {"views": views},
        }
    except Exception as e:
        return {
            "error": str(e),
            "snippet": None,
            "duration_str": "N/A",
            "metrics": {"views": 0},
        }
