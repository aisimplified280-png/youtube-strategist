"""Page: Strategist (Conversational AI Co-Pilot & Exact Metadata Approval)."""

import copy
import json
from typing import Any, Dict, List, Optional
import streamlit as st

from core.auth import is_demo_mode
from core.demo_data import DEMO_HERO_ACTION
from core.llm import LLMRouter, LLMError
from core.linter import lint_proposal, score_hook_text
from core.prompts import build_strategist_prompt
from core.metadata_apply import compute_proposal_digest, apply_proposal
from ui.components import demo_banner, diff_viewer, char_counter, status_pill, error_state
import strategist_chat


def render_strategist(youtube=None, secrets=None):
    """Render the split-screen Strategist chat and live approval workspace."""
    demo_banner()
    is_demo = is_demo_mode()
    secrets = secrets or {}

    st.markdown(
        """
        <div style="margin-bottom: 16px;">
            <h1 style="font-size: 2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px;">Strategist Co-Pilot</h1>
            <p style="color: #94A3B8; font-size: 0.95rem; margin: 0;">
                Conversational packaging optimizer. Review side-by-side proposal diffs and approve changes before writing to YouTube.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Resolve active video
    target_video_id = st.session_state.get("pending_optimizer_video_id")
    target_video_title = st.session_state.get("pending_optimizer_video_title")

    if not target_video_id and is_demo:
        target_video_id = DEMO_HERO_ACTION["video_id"]
        target_video_title = DEMO_HERO_ACTION["title"]

    # Invalidate video-scoped state if active video has changed
    last_active_id = st.session_state.get("strategist_active_video_id")
    if target_video_id and last_active_id and target_video_id != last_active_id:
        st.session_state.pop("strategist_target_context", None)
        st.session_state.pop("strategist_chat_proposal", None)
        st.session_state.pop("strategist_chat_history", None)
        st.session_state.pop("llm_last_provider", None)
    if target_video_id:
        st.session_state["strategist_active_video_id"] = target_video_id

    # Target video selector or URL input
    with st.expander("Target Video Selection", expanded=not bool(target_video_id)):
        col_in1, col_in2 = st.columns([3, 1])
        with col_in1:
            video_input = st.text_input(
                "YouTube Video URL or 11-char ID",
                value=target_video_id or "",
                placeholder="https://www.youtube.com/watch?v=... or bare ID",
                key="strategist_url_input",
            )
        with col_in2:
            st.caption(" ")
            if st.button("Load Video", type="primary", use_container_width=True):
                if video_input:
                    try:
                        clean_id = strategist_chat.parse_video_id(video_input)
                        st.session_state["pending_optimizer_video_id"] = clean_id
                        st.session_state["strategist_active_video_id"] = clean_id
                        st.session_state.pop("strategist_target_context", None)
                        st.session_state.pop("strategist_chat_proposal", None)
                        st.session_state.pop("strategist_chat_history", None)
                        st.session_state.pop("llm_last_provider", None)
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    # Initialize video context
    context = st.session_state.get("strategist_target_context")
    if context and target_video_id and context.get("target_id") != target_video_id:
        context = None
        st.session_state.pop("strategist_target_context", None)
    if not context:
        if is_demo or target_video_id == DEMO_HERO_ACTION["video_id"]:
            context = {
                "target_id": DEMO_HERO_ACTION["video_id"],
                "target_snippet": DEMO_HERO_ACTION["current_snippet"],
                "target_transcript": "[00:00] In this video we benchmark prompt caching...\n[02:15] Notice how cold requests take 2.4s but cached requests take 400ms.",
                "target_transcript_status": "Timestamped transcript available.",
            }
            st.session_state["strategist_target_context"] = context
        elif target_video_id and youtube:
            try:
                snippet = youtube.videos().list(part="snippet", id=target_video_id).execute()["items"][0]["snippet"]
                trans_text, trans_status = strategist_chat._transcript_text(target_video_id)
                context = {
                    "target_id": target_video_id,
                    "target_snippet": snippet,
                    "target_transcript": trans_text,
                    "target_transcript_status": trans_status,
                }
                st.session_state["strategist_target_context"] = context
            except Exception as e:
                st.error(f"Could not load YouTube video: {str(e)}")
                return

    if not context:
        st.info("Enter a YouTube video URL or ID above, or select a video from Home or Find Videos.")
        return

    target_snippet = context["target_snippet"]
    transcript_status = context.get("target_transcript_status", "Unavailable")

    # Status Bar
    status_cols = st.columns([2, 1, 1])
    with status_cols[0]:
        st.caption(f"Target: **{target_snippet.get('title')}** (`{context['target_id']}`) ")
    with status_cols[1]:
        if "Timestamped transcript" in transcript_status:
            st.markdown('<span class="status-pill status-connected">● Transcript Loaded</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="status-pill status-disconnected">● Metadata Only</span>', unsafe_allow_html=True)
    with status_cols[2]:
        provider_info = st.session_state.get("llm_last_provider", "Groq / Gemini Router")
        st.markdown(f'<span class="provider-badge provider-groq">{provider_info}</span>', unsafe_allow_html=True)

    # Main Split Screen Layout (Chat Left, Proposal Right)
    chat_col, proposal_col = st.columns([1, 1], gap="medium")

    # LEFT COLUMN: Chat & Prompts
    with chat_col:
        st.markdown("### Conversation")
        chat_container = st.container(height=420)
        chat_history = st.session_state.setdefault("strategist_chat_history", [])

        with chat_container:
            if not chat_history:
                st.caption("AI Co-Pilot ready. Ask for specific packaging revisions or click a suggested prompt chip below.")
            for msg in chat_history:
                role = msg["role"]
                with st.chat_message(role):
                    st.write(msg["content"])

        # Suggested Prompt Chips
        st.markdown("**Suggested Actions:**")
        chip_col1, chip_col2 = st.columns(2)
        chip_action = None
        with chip_col1:
            if st.button("Make title more specific", use_container_width=True):
                chip_action = "Make the title more specific using real numbers or tools from the transcript. Keep it under 60 chars."
            if st.button("Score opening hook", use_container_width=True):
                chip_action = "Evaluate the first 15 seconds of the transcript using the hook score heuristic."
        with chip_col2:
            if st.button("Shorter hook & curiosity gap", use_container_width=True):
                chip_action = "Give me 2 title variants with a stronger curiosity gap without clickbait."
            if st.button("Lint title + thumbnail concept", use_container_width=True):
                chip_action = "Lint the current title and proposed thumbnail pairing for duplicates and feed truncation."

        user_input = st.chat_input("Ask the Strategist to revise title, description, or tags...")
        active_instruction = chip_action or user_input

        if active_instruction:
            chat_history.append({"role": "user", "content": active_instruction})
            with chat_container:
                with st.chat_message("user"):
                    st.write(active_instruction)

            # Generate AI proposal
            with st.spinner("Consulting AI packaging co-pilot..."):
                if is_demo:
                    # Provide realistic simulated response in Demo Mode
                    demo_prop = DEMO_HERO_ACTION["proposed_snippet"]
                    st.session_state["strategist_chat_proposal"] = copy.deepcopy(demo_prop)
                    st.session_state["llm_last_provider"] = "Groq: openai/gpt-oss-120b"
                    reply_text = f"Drafted an updated proposal focusing on latency reduction.\n\n**Rationale:** {DEMO_HERO_ACTION['rationale']}"
                    chat_history.append({"role": "assistant", "content": reply_text})
                    st.rerun()
                else:
                    try:
                        router = LLMRouter(secrets)
                        prompt = build_strategist_prompt(
                            target_snippet=target_snippet,
                            instruction=active_instruction,
                            target_transcript=context.get("target_transcript", ""),
                            target_transcript_status=transcript_status,
                            chat_history=chat_history,
                        )
                        parsed, provider, model = router.generate_structured_proposal(prompt)
                        st.session_state["strategist_chat_proposal"] = {
                            "title": parsed["title"],
                            "description": parsed["description"],
                            "tags": parsed["tags"],
                            "categoryId": target_snippet.get("categoryId", "28"),
                        }
                        st.session_state["llm_last_provider"] = f"{provider}: {model}"
                        chat_history.append({"role": "assistant", "content": parsed.get("rationale", "Updated proposal drafted.")})
                        st.rerun()
                    except LLMError as e:
                        st.error(f"AI Generation Failed: {str(e)}")

    # RIGHT COLUMN: Live Interactive Proposal Panel
    with proposal_col:
        st.markdown("### Live Proposal & Diff")
        proposal = st.session_state.get("strategist_chat_proposal")
        if not proposal and is_demo:
            proposal = DEMO_HERO_ACTION["proposed_snippet"]
            st.session_state["strategist_chat_proposal"] = proposal

        if not proposal:
            st.info("No proposal generated yet. Chat on the left or select a suggested prompt to create a proposal.")
            return

        # Linting Check
        lint_results = lint_proposal(proposal)

        # Title Section
        st.markdown(f"**Title** {char_counter(len(proposal.get('title', '')), 100, warn_len=60)}", unsafe_allow_html=True)
        diff_viewer(target_snippet.get("title", ""), proposal.get("title", ""), label="Title Diff")

        # Description Section
        st.markdown(f"**Description & Hashtags** {char_counter(len(proposal.get('description', '')), 5000)}", unsafe_allow_html=True)
        with st.expander("Inspect Description Diff", expanded=False):
            diff_viewer(target_snippet.get("description", ""), proposal.get("description", ""), label="Description Diff")

        # Tags Section
        tags_raw = ", ".join(proposal.get("tags", []))
        st.markdown(f"**YouTube Tags (Separate Field)** {char_counter(len(tags_raw), 500)}", unsafe_allow_html=True)
        st.caption(f"`{tags_raw}`" if tags_raw else "(No tags)")

        # Inline Linter Warnings
        if lint_results.get("warnings"):
            with st.expander(f"⚠️ Packaging Warnings ({len(lint_results['warnings'])})", expanded=True):
                for w in lint_results["warnings"]:
                    st.caption(f"• {w}")

        st.markdown("---")

        # Approval & Apply Gate
        st.markdown("#### Exact Proposal Approval Gate")
        st.caption("Review the exact metadata shown above. Revising the proposal invalidates prior approval.")

        # Strict Proposal Validation
        val_errors = []
        prop_title = str(proposal.get("title", "")).strip()
        if not prop_title:
            val_errors.append("Title cannot be empty.")
        elif len(prop_title) > 100:
            val_errors.append(f"Title exceeds YouTube 100-character limit ({len(prop_title)} chars).")
        elif "Title refinement needed" in prop_title:
            val_errors.append("Proposal title is incomplete or malformed. Refine in chat before approving.")

        prop_desc = str(proposal.get("description", ""))
        if len(prop_desc) > 5000:
            val_errors.append(f"Description exceeds YouTube 5,000-character limit ({len(prop_desc)} chars).")

        prop_tags_str = ", ".join(proposal.get("tags", []))
        if len(prop_tags_str) > 500:
            val_errors.append(f"Tags field exceeds YouTube 500-character limit ({len(prop_tags_str)} chars).")

        is_proposal_valid = len(val_errors) == 0
        if not is_proposal_valid:
            for ve in val_errors:
                st.error(f"Validation Blocker: {ve}")

        # Compute exact proposal digest
        proposal_digest = compute_proposal_digest(context["target_id"], target_snippet, proposal)
        st.caption(f"Proposal Digest: `{proposal_digest[:16]}...` (approval is strictly bound to this revision)")

        # Unique confirmation key per proposal digest
        confirmation_key = f"confirm_approval_{context['target_id']}_{proposal_digest[:16]}"
        review_confirmed = st.checkbox(
            "I have reviewed the exact proposed title, description, and tags and confirm they are accurate.",
            key=confirmation_key,
            disabled=not is_proposal_valid,
        )

        # Result display
        last_result = st.session_state.get(f"apply_result_{context['target_id']}")
        if last_result:
            if last_result.get("simulated"):
                st.info(f"🎭 **Demo Simulation**: Proposal simulated. Simulated title: **{last_result.get('title')}** *(No live write performed to YouTube in Demo Mode)*.")
            elif last_result.get("result") == "success":
                st.success(f"✓ Verified updated on YouTube! Title: {last_result.get('title')}")
            else:
                st.error(f"Update failed: {last_result.get('error')}")

        btn_cols = st.columns([2, 1])
        with btn_cols[0]:
            apply_clicked = st.button(
                "Approve and apply to YouTube",
                type="primary",
                disabled=not (review_confirmed and is_proposal_valid),
                use_container_width=True,
                key="btn_apply_proposal",
            )
        with btn_cols[1]:
            if st.button("Reset Draft", use_container_width=True):
                st.session_state.pop("strategist_chat_proposal", None)
                st.session_state.pop(confirmation_key, None)
                st.rerun()

        if apply_clicked:
            if not (review_confirmed and is_proposal_valid):
                st.error("Exact proposal review confirmation and valid metadata are required.")
                return

            if is_demo:
                st.session_state[f"apply_result_{context['target_id']}"] = {
                    "result": "simulated_success",
                    "title": proposal["title"],
                    "url": f"https://www.youtube.com/watch?v={context['target_id']}",
                    "simulated": True,
                }
                st.toast("Demo Mode: Simulated proposal approval. (No live YouTube write performed).")
                st.rerun()
            elif youtube:
                with st.spinner("Validating proposal, checking for stale metadata, and updating YouTube..."):
                    try:
                        audit_records = st.session_state.setdefault("session_audit_records", [])
                        res = apply_proposal(
                            youtube=youtube,
                            video_id=context["target_id"],
                            before_snippet=target_snippet,
                            proposed_snippet=proposal,
                            approved_digest=proposal_digest,
                            audit_callback=audit_records.append,
                        )
                        st.session_state[f"apply_result_{context['target_id']}"] = res
                        st.session_state["target_snippet"] = copy.deepcopy(proposal)
                        st.toast("Metadata update verified on YouTube!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Application error: {str(e)}")
