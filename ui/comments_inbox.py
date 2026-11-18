"""Channel-wide comments inbox with on-demand analysis and manually approved replies."""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core.comments.analyzer import analyze_comment_thread
from youtube_analytics_connector import CHANNEL_ID, fetch_video_snippet

from core.comments.youtube_comments import (
    CommentVerificationUnavailable,
    compute_comment_reply_digest,
    list_channel_comment_threads,
    list_thread_replies,
    publish_approved_comment_reply,
)


MODERATION_LABELS = {
    "published": "Published",
    "heldForReview": "Held for review",
    "likelySpam": "Likely spam",
}


def _comment_pattern_summary(items: list[dict]) -> dict:
    """Build transparent, deterministic patterns from the loaded published-comment sample."""
    comments = [str(item.get("text") or "").strip() for item in items if str(item.get("text") or "").strip()]
    texts = [value.casefold() for value in comments]
    if not texts:
        return {"sample_size": 0, "questions": 0, "unanswered": 0, "positive": 0, "patterns": []}

    def count_terms(terms):
        return sum(any(term in text for term in terms) for text in texts)

    patterns = [
        ("How-to and setup", ["how to", "how do", "tutorial", "setup", "install", "start using"]),
        ("Pricing and free access", ["free", "price", "cost", "subscription", "plan", "paid", "limit"]),
        ("AI tools and models", ["chatgpt", "gemini", "claude", "llm", "model", "ai agent", "rag"]),
        ("Errors and troubleshooting", ["error", "not working", "issue", "problem", "bug", "fix", "failed"]),
        ("Requests for examples", ["example", "demo", "show us", "explain", "cover", "deep dive"]),
    ]
    ranked = []
    for label, terms in patterns:
        matches = [comments[i] for i, text in enumerate(texts) if any(term in text for term in terms)]
        if matches:
            ranked.append({"label": label, "count": len(matches), "example": matches[0][:180]})
    ranked.sort(key=lambda item: (-item["count"], item["label"]))
    return {
        "sample_size": len(texts),
        "questions": sum("?" in value for value in comments),
        "unanswered": sum(int(item.get("total_reply_count") or 0) == 0 for item in items),
        "positive": count_terms(["thanks", "thank you", "great", "awesome", "helpful", "love this", "amazing"]),
        "patterns": ranked[:4],
    }


def _load_comment_page(youtube, status: str, search_terms: str, page_token: str | None = None):
    return list_channel_comment_threads(
        youtube,
        CHANNEL_ID,
        moderation_status=status,
        page_token=page_token,
        max_results=25,
        search_terms=search_terms or None,
        order="time",
    )


def _render_pending_replies(youtube, store):
    st.subheader("Pending reply drafts")
    st.caption("Publishing a reply uses 50 YouTube Data API quota units. Every reply requires your approval.")
    drafts = store.list_comment_reply_drafts(status="pending_review", limit=20)
    if not drafts:
        st.caption("No replies are waiting for your approval.")
        return
    for draft in drafts:
        draft_id = draft["id"]
        with st.container(border=True):
            st.caption(
                f"{draft.get('video_title') or 'Video'} · "
                f"{draft.get('comment_author_display_name') or 'Viewer'}"
            )
            st.write(draft.get("comment_text_snapshot") or "")
            st.markdown("**Proposed reply**")
            with st.form(f"comment_pending_reply_form_{draft_id}"):
                edited_reply = st.text_area(
                    "Edit reply before approval",
                    value=draft.get("draft_text") or "",
                    key=f"comment_pending_reply_edit_{draft_id}",
                    height=90,
                )
                save_edits = st.form_submit_button("Save reply edits")
            if save_edits:
                digest = compute_comment_reply_digest(
                    store.owner_key,
                    str(draft.get("thread_id") or ""),
                    str(draft.get("parent_comment_id") or ""),
                    str(draft.get("comment_text_snapshot") or ""),
                    edited_reply,
                )
                store.update_comment_reply_draft(draft_id, {
                    "draft_text": edited_reply.strip(),
                    "approval_digest": digest,
                    "status": "pending_review",
                    "approved_by": None,
                    "approved_at": None,
                    "error": None,
                })
                st.success("Reply edits saved. Any previous approval has been invalidated.")
                st.rerun()
            if not draft.get("can_reply"):
                st.warning("YouTube no longer reports this thread as replyable.")
                continue
            confirmed = st.checkbox(
                "I approve this exact reply",
                key=f"comment_reply_approve_{draft_id}_{str(draft.get('approval_digest') or '')[:8]}",
            )
            if st.button(
                "Approve & Publish reply",
                type="primary",
                disabled=not confirmed,
                key=f"comment_reply_publish_{draft_id}",
            ):
                try:
                    approved = store.update_comment_reply_draft(draft_id, {
                        "status": "approved",
                        "approved_by": store.owner_key,
                        "approved_at": datetime.now(timezone.utc).isoformat(),
                        "error": None,
                    })
                    if not approved:
                        raise RuntimeError("Could not persist reply approval.")
                    claimed = store.claim_comment_reply_draft(draft_id)
                    if not claimed:
                        raise RuntimeError("This reply was already claimed or is no longer awaiting approval.")
                    result = publish_approved_comment_reply(youtube, claimed, store.owner_key)
                    store.update_comment_reply_draft(draft_id, {
                        "status": "published",
                        "published_comment_id": result["comment_id"],
                        "result": result,
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "error": None,
                    })
                    st.success("Reply published and verified.")
                    st.rerun()
                except CommentVerificationUnavailable as error:
                    store.update_comment_reply_draft(draft_id, {
                        "status": "verification_unavailable",
                        "error": str(error)[:1000],
                    })
                    st.error(f"The reply may have been published, but verification was unavailable. Do not retry blindly. {error}")
                except Exception as error:
                    store.update_comment_reply_draft(draft_id, {
                        "status": "failed",
                        "error": str(error)[:1000],
                    })
                    st.error(f"Reply publishing failed: {error}")


def _render_selected_thread(youtube, store, secrets, thread: dict):
    thread_id = thread.get("thread_id")
    parent_id = thread.get("top_level_comment_id")
    st.divider()
    st.subheader("Selected comment thread")
    st.caption(f"Video ID: {thread.get('video_id') or 'Channel comment'}")
    st.write(thread.get("text") or "")
    st.caption(
        f"{thread.get('author_display_name') or 'Viewer'} · "
        f"{thread.get('published_at') or 'Date unavailable'} · "
        f"{thread.get('total_reply_count', 0)} replies"
    )
    if not parent_id:
        st.warning("The top-level comment ID is unavailable.")
        return

    replies_key = f"_comment_replies_{thread_id}"
    if st.button("Load replies", key=f"comment_load_replies_{thread_id}"):
        try:
            page = list_thread_replies(youtube, parent_id, max_results=25)
            st.session_state[replies_key] = page
            st.rerun()
        except Exception as error:
            st.error(f"Could not load replies: {error}")

    replies_page = st.session_state.get(replies_key)
    replies = (replies_page or {}).get("items") or []
    if replies_page:
        st.markdown("**Replies**")
        if replies:
            for reply in replies:
                with st.container(border=True):
                    st.caption(reply.get("author_display_name") or "Viewer")
                    st.write(reply.get("text") or "")
        else:
            st.caption("No replies were returned for this thread.")
        next_reply_token = replies_page.get("next_page_token")
        if next_reply_token and st.button(
            "Load older replies", key=f"comment_load_older_replies_{thread_id}"
        ):
            try:
                older = list_thread_replies(
                    youtube, parent_id, page_token=next_reply_token, max_results=25
                )
                combined = replies + older["items"]
                st.session_state[replies_key] = {
                    "items": combined,
                    "next_page_token": older.get("next_page_token"),
                }
                st.rerun()
            except Exception as error:
                st.error(f"Could not load older replies: {error}")

    if st.button("Analyze comment & draft a reply", type="primary", key=f"comment_analyze_{thread_id}"):
        try:
            with st.spinner("Analyzing this comment thread..."):
                analysis_thread = dict(thread)
                video_id = thread.get("video_id")
                if video_id:
                    title_key = f"_comment_video_title_{video_id}"
                    video_title = st.session_state.get(title_key)
                    if not video_title:
                        try:
                            snippet = fetch_video_snippet(youtube, video_id)
                            video_title = snippet.get("title") or ""
                            st.session_state[title_key] = video_title
                        except Exception:
                            video_title = ""
                    analysis_thread["video_title"] = video_title
                analysis = analyze_comment_thread(secrets, analysis_thread, replies)
            st.session_state[f"_comment_analysis_{thread_id}"] = analysis
        except Exception as error:
            st.error(f"Comment analysis failed: {error}")

    analysis = st.session_state.get(f"_comment_analysis_{thread_id}")
    if analysis:
        left, right = st.columns(2)
        with left:
            st.metric("Category", analysis["category"].replace("_", " ").title())
            st.metric("Sentiment", analysis["sentiment"].title())
        with right:
            st.metric("Confidence", f"{analysis['confidence']:.0%}")
            st.metric("Recommendation", analysis["reply_recommendation"].replace("_", " ").title())
        st.caption(analysis.get("reason") or "")
        if analysis.get("needs_fact_check"):
            st.warning(
                "The comment asks for facts that are not verified from this thread. "
                "The reply below is a safe clarification, not an answer to the factual claim."
            )
        if analysis["reply_recommendation"] == "ignore":
            st.info("Recommended action: do not reply to this comment.")
        elif analysis.get("suggested_reply") and thread.get("can_reply"):
            if analysis.get("safe_fallback"):
                st.caption("AI providers were unavailable or uncertain; showing a conservative clarification reply.")
            elif analysis.get("needs_fact_check"):
                st.caption("Safe clarification draft · no unverified product or pricing claim")
            else:
                st.caption(f"Suggested by {analysis.get('provider', 'AI')} · {analysis.get('model', '')}")
            reply_text = st.text_area(
                "Suggested reply (edit before saving)",
                value=analysis["suggested_reply"],
                key=f"comment_reply_editor_{thread_id}",
                height=100,
            )
            if st.button("Save reply draft for approval", key=f"comment_save_reply_{thread_id}"):
                comment_snapshot = str(thread.get("text") or "")[:5000]
                digest = compute_comment_reply_digest(
                    store.owner_key,
                    str(thread_id or ""),
                    str(parent_id or ""),
                    comment_snapshot,
                    reply_text,
                )
                try:
                    saved = store.create_comment_reply_draft({
                        "channel_id": CHANNEL_ID,
                        "video_id": thread.get("video_id"),
                        "video_title": thread.get("video_title") or st.session_state.get("_comment_video_title_" + str(thread.get("video_id") or ""), ""),
                        "thread_id": thread_id,
                        "parent_comment_id": parent_id,
                        "comment_text_snapshot": comment_snapshot,
                        "comment_author_display_name": thread.get("author_display_name") or "",
                        "can_reply": bool(thread.get("can_reply")),
                        "draft_text": reply_text.strip(),
                        "approval_digest": digest,
                        "status": "pending_review",
                    })
                    st.success("Reply draft saved. It will not publish until you approve it.")
                    st.session_state["_comment_last_saved_draft_id"] = saved["id"]
                    st.rerun()
                except Exception as error:
                    st.error(f"Could not save reply draft: {error}")
        elif not thread.get("can_reply"):
            st.warning("YouTube does not allow replies to this thread.")


def _render_uncertain_replies(store):
    st.subheader("Replies needing manual verification")
    drafts = store.list_comment_reply_drafts(status="verification_unavailable", limit=20)
    if not drafts:
        st.caption("No replies are awaiting manual verification.")
        return
    st.warning(
        "These replies had an uncertain API outcome. Check the exact thread in YouTube Studio before resolving one. "
        "The application will not retry them automatically."
    )
    for draft in drafts:
        draft_id = draft["id"]
        with st.container(border=True):
            st.caption(draft.get("video_title") or draft.get("video_id") or "Channel comment")
            st.markdown("**Original comment**")
            st.write(draft.get("comment_text_snapshot") or "")
            st.markdown("**Reply that may have been published**")
            st.write(draft.get("draft_text") or "")
            if st.button(
                "I found this reply in YouTube Studio",
                key=f"comment_uncertain_found_{draft_id}",
            ):
                store.update_comment_reply_draft(draft_id, {
                    "status": "published",
                    "result": {
                        "manual_verification": True,
                        "verification_note": "Owner confirmed the reply is visible in YouTube Studio.",
                    },
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "error": None,
                })
                st.success("Marked as manually verified.")
                st.rerun()
            if st.button(
                "I confirmed this reply was not published",
                key=f"comment_uncertain_not_found_{draft_id}",
            ):
                store.update_comment_reply_draft(draft_id, {
                    "status": "failed",
                    "result": {
                        "manual_verification": True,
                        "verification_note": "Owner confirmed the reply is not present in YouTube Studio.",
                    },
                    "error": "Owner confirmed no reply was published.",
                })
                st.success("Resolved. You can create a new draft for this comment.")
                st.rerun()


def _render_comment_automation_controls(store):
    settings = store.get_workspace_automation_settings()
    with st.expander("Comment automation settings", expanded=False):
        modes = ["paused", "shadow", "review", "autonomous"]
        mode = st.selectbox(
            "Comment automation mode",
            modes,
            index=modes.index(settings.get("comments_mode", "shadow")),
            key="comments_automation_mode",
            format_func=lambda value: {
                "paused": "Paused",
                "shadow": "Shadow · analyze only",
                "review": "Review · save reply drafts",
                "autonomous": "Autonomous · publish safe praise replies",
            }[value],
        )
        cap = st.number_input(
            "Maximum autonomous replies per UTC day",
            min_value=0,
            max_value=10,
            value=int(settings.get("comments_daily_cap", 3)),
            key="comments_automation_daily_cap",
        )
        confidence = st.slider(
            "Minimum confidence for autonomous replies",
            min_value=0.85,
            max_value=1.0,
            value=float(settings.get("minimum_confidence", 0.92)),
            step=0.01,
            key="comments_automation_confidence",
        )
        autonomous_confirmed = True
        if mode == "autonomous":
            autonomous_confirmed = st.checkbox(
                "I explicitly authorize automatic publishing of limited praise acknowledgements.",
                value=False,
                key="comments_automation_autonomous_confirm",
            )
        paused = st.checkbox(
            "Pause Metadata Editor and Comments Inbox automation",
            value=bool(settings.get("emergency_pause", False)),
            key="comments_automation_emergency_pause",
        )
        st.caption("Channel Strategist has its own independent emergency pause in its Settings section.")
        st.caption(
            "Automation runs only when you press the run button in this inbox; it is not a background monitor. "
            "Shadow mode creates no drafts. Review mode saves drafts only. Autonomous mode may publish a short "
            "thank-you reply only for a published, unanswered praise comment with high confidence. Questions, "
            "pricing, corrections, criticisms, spam and uncertain facts are never auto-replied."
        )
        if st.button(
            "Save comment automation settings",
            key="comments_automation_save",
            disabled=mode == "autonomous" and not autonomous_confirmed,
        ):
            saved = store.set_workspace_automation_settings({
                "comments_mode": mode,
                "comments_daily_cap": cap,
                "minimum_confidence": confidence,
                "emergency_pause": paused,
            })
            st.success(f"Comment automation mode saved: {saved['comments_mode']}.")
            st.rerun()
    return settings


def _run_comment_automation(youtube, store, secrets: dict, items: list[dict], settings: dict) -> dict:
    """On-demand comment scan; autonomous replies are deliberately limited to praise."""
    mode = str(settings.get("comments_mode") or "shadow")
    result = {"mode": mode, "analyzed": 0, "drafted": 0, "published": 0, "skipped": 0, "notes": []}
    if settings.get("emergency_pause"):
        result["notes"].append("Workspace emergency pause is active.")
        return result
    if mode == "paused":
        result["notes"].append("Comment automation is paused.")
        return result

    now = datetime.now(timezone.utc)
    utc_day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    daily_cap = max(0, int(settings.get("comments_daily_cap", 3)))
    minimum_confidence = float(settings.get("minimum_confidence", 0.92))
    already_published = store.list_comment_reply_drafts(
        status="published", published_since=utc_day_start, limit=20
    )
    published_today = len(already_published)
    unresolved = store.list_comment_reply_drafts(
        status="verification_unavailable", limit=20
    )
    if mode == "autonomous" and unresolved:
        result["notes"].append(
            "Autonomous replies are paused because a previous reply outcome is uncertain. "
            "Resolve the item in YouTube Studio under 'Replies needing verification' first."
        )
        return result
    existing_drafts = store.list_comment_reply_drafts(limit=100)
    existing_parent_ids = {
        str(item.get("parent_comment_id"))
        for item in existing_drafts
        if item.get("parent_comment_id")
    }

    for thread in (items or [])[:5]:
        parent_id = str(thread.get("top_level_comment_id") or "")
        thread_id = str(thread.get("thread_id") or "")
        text_value = str(thread.get("text") or "").strip()
        if (
            not parent_id or not thread_id or not text_value
            or not thread.get("can_reply")
            or int(thread.get("total_reply_count") or 0) > 0
            or str(thread.get("moderation_status") or "published") != "published"
        ):
            result["skipped"] += 1
            continue
        if parent_id in existing_parent_ids:
            result["skipped"] += 1
            continue

        try:
            analysis = analyze_comment_thread(secrets, thread, [])
        except Exception as error:
            result["skipped"] += 1
            result["notes"].append(f"Analysis skipped for {parent_id}: {type(error).__name__}")
            continue
        result["analyzed"] += 1
        suggestion = str(analysis.get("suggested_reply") or "").strip()
        category = str(analysis.get("category") or "")
        if analysis.get("reply_recommendation") == "ignore" or not suggestion:
            result["skipped"] += 1
            continue

        if mode == "shadow":
            result["notes"].append(
                f"Shadow suggestion for {parent_id}: {category} · {suggestion[:120]}"
            )
            continue

        comment_snapshot = text_value[:5000]
        digest = compute_comment_reply_digest(
            store.owner_key, thread_id, parent_id, comment_snapshot, suggestion
        )
        try:
            saved = store.create_comment_reply_draft({
                "channel_id": CHANNEL_ID,
                "video_id": thread.get("video_id"),
                "video_title": thread.get("video_title") or "",
                "thread_id": thread_id,
                "parent_comment_id": parent_id,
                "comment_text_snapshot": comment_snapshot,
                "comment_author_display_name": thread.get("author_display_name") or "",
                "can_reply": True,
                "draft_text": suggestion,
                "approval_digest": digest,
                "status": "pending_review",
            })
            existing_parent_ids.add(parent_id)
        except Exception as error:
            result["skipped"] += 1
            result["notes"].append(f"Could not save draft for {parent_id}: {str(error)[:140]}")
            continue

        if mode == "review":
            result["drafted"] += 1
            continue

        # Never autonomously answer questions, correct facts, criticize or moderate.
        eligible = (
            mode == "autonomous"
            and category == "praise"
            and analysis.get("reply_recommendation") == "reply"
            and not analysis.get("needs_fact_check")
            and not analysis.get("safe_fallback")
            and float(analysis.get("confidence") or 0) >= minimum_confidence
            and suggestion == "Thanks for watching! Glad you found it helpful."
        )
        if not eligible:
            result["drafted"] += 1
            continue
        if daily_cap == 0 or published_today + result["published"] >= daily_cap:
            result["drafted"] += 1
            result["notes"].append("Daily autonomous reply cap reached; the remaining suggestion was left for review.")
            continue

        audit_id = saved.get("id")
        if not audit_id:
            result["drafted"] += 1
            continue
        try:
            approved = store.update_comment_reply_draft(audit_id, {
                "status": "approved",
                "approved_by": "automation:comments",
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            })
            claimed = store.claim_comment_reply_draft(audit_id) if approved else None
            if not claimed:
                result["drafted"] += 1
                continue
            published = publish_approved_comment_reply(
                youtube, claimed, store.owner_key, allow_automation=True
            )
            store.update_comment_reply_draft(audit_id, {
                "status": "published",
                "published_comment_id": published["comment_id"],
                "result": {
                    **published,
                    "automation_mode": "autonomous",
                    "manual_owner_approval": False,
                },
                "published_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            })
            result["published"] += 1
        except CommentVerificationUnavailable as error:
            store.update_comment_reply_draft(audit_id, {
                "status": "verification_unavailable",
                "error": str(error)[:1000],
            })
            result["notes"].append(
                f"Reply outcome for {parent_id} is uncertain. Verify it in YouTube Studio; no retry was attempted."
            )
        except Exception as error:
            store.update_comment_reply_draft(audit_id, {
                "status": "failed",
                "error": str(error)[:1000],
            })
            result["notes"].append(f"Autonomous reply stopped for {parent_id}: {str(error)[:160]}")
    return result


def render_comments_inbox(youtube, persistent_store, owner_subject: str, secrets: dict):
    st.header("Comments Inbox")
    st.caption(
        "Channel-wide comments for uploaded videos and channel discussions. "
        "Comments on Community posts are not included by this YouTube API endpoint."
    )
    if youtube is None:
        st.info("Connect the channel owner's YouTube account to access comments.")
        return
    if persistent_store is None:
        st.error("Persistent storage is unavailable. Reply drafts cannot be saved safely.")
        return

    automation_settings = _render_comment_automation_controls(persistent_store)

    # A compact pattern sample is deliberately limited to the latest 25 published
    # threads; the UI states the sample size so it is not mistaken for a full-channel export.
    summary_cache_key = "_comments_pattern_summary_sample"
    summary_page_key = "_comments_page_published_"
    if st.button("Refresh comment pattern summary", key="comments_pattern_summary_refresh"):
        st.session_state.pop(summary_cache_key, None)
        st.session_state.pop(summary_page_key, None)
    if summary_cache_key not in st.session_state:
        try:
            with st.spinner("Scanning recent comments for recurring themes..."):
                summary_page = _load_comment_page(youtube, "published", "")
            st.session_state[summary_cache_key] = summary_page
            st.session_state[summary_page_key] = summary_page
        except Exception as error:
            st.session_state[summary_cache_key] = {"items": [], "summary_error": str(error)}

    summary_page = st.session_state.get(summary_cache_key) or {}
    summary_items = summary_page.get("items") or []
    summary = _comment_pattern_summary(summary_items)
    with st.container(border=True):
        st.subheader("Comment pattern summary")
        st.caption(
            f"Recent sample: {summary['sample_size']} published video/channel comment threads. "
            "This is a sample, not a complete historical channel export."
        )
        summary_cols = st.columns(4)
        summary_cols[0].metric("Questions", summary["questions"])
        summary_cols[1].metric("Unanswered", summary["unanswered"])
        summary_cols[2].metric("Positive feedback", summary["positive"])
        summary_cols[3].metric("Patterns", len(summary["patterns"]))
        if summary_page.get("summary_error"):
            st.warning(f"Pattern sample unavailable: {summary_page['summary_error']}")
        elif summary["patterns"]:
            for item in summary["patterns"]:
                st.markdown(f"**{item['label']}** · {item['count']} comments")
                st.caption(f'Example: "{item["example"]}"')
        else:
            st.caption("No repeated themes were detected in this sample yet.")

    col_status, col_search = st.columns([1, 2])
    with col_status:
        status = st.selectbox(
            "Comment status",
            options=list(MODERATION_LABELS),
            format_func=lambda value: MODERATION_LABELS[value],
            key="comments_moderation_status",
        )
    with col_search:
        with st.form("comments_search_form"):
            search_input = st.text_input(
                "Search comments",
                value=st.session_state.get("comments_search_committed", ""),
                key="comments_search_input",
            )
            search_clicked = st.form_submit_button("Search")
    if search_clicked:
        st.session_state["comments_search_committed"] = search_input.strip()
        st.rerun()
    search_terms = st.session_state.get("comments_search_committed", "")
    cache_key = f"_comments_page_{status}_{search_terms.strip().casefold()}"
    if st.button("Refresh latest comments", key=f"comments_refresh_{status}_{search_terms.strip().casefold()}"):
        try:
            with st.spinner("Refreshing the latest comments..."):
                st.session_state[cache_key] = _load_comment_page(youtube, status, search_terms)
            st.rerun()
        except Exception as error:
            st.error(f"Could not refresh comments: {error}")
    if cache_key not in st.session_state:
        try:
            with st.spinner("Loading the latest channel comments..."):
                st.session_state[cache_key] = _load_comment_page(
                    youtube, status, search_terms
                )
        except Exception as error:
            st.error(f"Could not retrieve channel comments: {error}")
            return

    page = st.session_state.get(cache_key) or {}
    items = page.get("items") or []
    st.caption(f"Showing {len(items)} comments · {MODERATION_LABELS[status]}")
    if st.button(
        "Run comment automation on the latest 5 comments",
        key="comments_run_automation",
        disabled=automation_settings.get("comments_mode") == "paused",
    ):
        with st.spinner("Running the selected comment automation mode..."):
            automation_result = _run_comment_automation(
                youtube, persistent_store, secrets, items, automation_settings
            )
        st.session_state["_comments_last_automation_result"] = automation_result
        if automation_result.get("mode") == "review" and automation_result.get("drafted", 0):
            st.session_state["_comments_show_pending_replies"] = True
        st.success(
            f"{automation_result['mode'].title()} run complete: "
            f"{automation_result['analyzed']} analyzed · "
            f"{automation_result['drafted']} drafts · "
            f"{automation_result['published']} published."
        )
        for note in automation_result.get("notes") or []:
            st.caption(note)
        st.rerun()
    previous_automation = st.session_state.get("_comments_last_automation_result")
    if previous_automation:
        st.caption(
            f"Last run: {previous_automation.get('mode')} · "
            f"{previous_automation.get('analyzed', 0)} analyzed · "
            f"{previous_automation.get('drafted', 0)} drafts · "
            f"{previous_automation.get('published', 0)} published"
        )
        for note in previous_automation.get("notes") or []:
            st.caption(note)
    if not items:
        st.info("No comments were returned for this filter.")
    else:
        video_options = ["All loaded videos"] + sorted({
            item.get("video_id") or "Channel comment" for item in items
        })
        selected_video = st.selectbox(
            "Filter loaded results by video",
            options=video_options,
            key=f"comments_video_filter_{status}_{search_terms.strip().casefold()}",
        )
        only_unanswered = st.checkbox(
            "Only unanswered comments",
            key=f"comments_unanswered_{status}_{search_terms.strip().casefold()}",
        )
        visible = items if selected_video == "All loaded videos" else [
            item for item in items
            if (item.get("video_id") or "Channel comment") == selected_video
        ]
        if only_unanswered:
            visible = [item for item in visible if item.get("total_reply_count", 0) == 0]
        for thread in visible:
            with st.container(border=True):
                st.caption(
                    f"{thread.get('author_display_name') or 'Viewer'} · "
                    f"{thread.get('published_at') or 'Date unavailable'}"
                )
                st.write(thread.get("text") or "")
                video_id = thread.get("video_id")
                if video_id:
                    st.caption(f"Video: https://www.youtube.com/watch?v={video_id}")
                st.caption(
                    f"{thread.get('total_reply_count', 0)} replies · "
                    f"{thread.get('like_count', 0)} likes · "
                    f"{'Reply enabled' if thread.get('can_reply') else 'Replies disabled'}"
                )
                if st.button("Open thread", key=f"comments_open_{thread.get('thread_id')}"):
                    st.session_state["_selected_comment_thread"] = thread
                    st.rerun()

    if page.get("next_page_token") and st.button(
        "Load more comments", key=f"comments_load_more_{status}_{search_terms.strip().casefold()}"
    ):
        try:
            with st.spinner("Loading the next comment page..."):
                next_page = _load_comment_page(
                    youtube, status, search_terms, page_token=page["next_page_token"]
                )
            combined_page = {
                "items": items + next_page["items"],
                "next_page_token": next_page.get("next_page_token"),
                "total_results": next_page.get("total_results") or page.get("total_results"),
                "moderation_status": status,
            }
            st.session_state[cache_key] = combined_page
            if status == "published" and not search_terms:
                st.session_state[summary_cache_key] = combined_page
            st.rerun()
        except Exception as error:
            st.error(f"Could not load more comments: {error}")

    selected_thread = st.session_state.get("_selected_comment_thread")
    if selected_thread:
        _render_selected_thread(youtube, persistent_store, secrets, selected_thread)

    st.divider()
    if st.button("Show pending replies", key="comments_show_pending_replies"):
        st.session_state["_comments_show_pending_replies"] = True
    if st.session_state.get("_comments_show_pending_replies"):
        _render_pending_replies(youtube, persistent_store)
    if st.button("Show replies needing verification", key="comments_show_uncertain_replies"):
        st.session_state["_comments_show_uncertain_replies"] = True
    if st.session_state.get("_comments_show_uncertain_replies"):
        _render_uncertain_replies(persistent_store)
