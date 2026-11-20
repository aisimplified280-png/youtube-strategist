"""Channel-wide comments inbox with on-demand analysis and manually approved replies."""
from __future__ import annotations

from datetime import datetime, timezone
import re

import streamlit as st

from core.comments.analyzer import analyze_comment_thread
from youtube_analytics_connector import CHANNEL_ID, fetch_video_snippet

from core.comments.youtube_comments import (
    CommentVerificationUnavailable,
    CommentModerationPending,
    compute_comment_reply_digest,
    list_channel_comment_threads,
    list_latest_unanswered_comment_threads,
    list_thread_replies,
    publish_approved_comment_reply,
)


MODERATION_LABELS = {
    "all": "All comments", "published": "Published",
    "heldForReview": "Held for review", "likelySpam": "Likely spam",
    "heldReply": "Contains held reply", "spamReply": "Contains likely-spam reply",
}


def _comment_pattern_summary(items: list[dict]) -> dict:
    """Summarize observable patterns in the currently loaded comment sample."""
    sample = list(items or [])
    texts = [str(item.get("text") or "").strip() for item in sample]
    questions = sum(
        1 for value in texts
        if "?" in value or re.search(r"^\s*(how|what|why|when|where|which|can|could|does|do|will|would|is it|are you)\b", value.casefold())
    )
    unanswered = sum(
        1 for item in sample
        if int(item.get("total_reply_count") or 0) == 0
    )
    positive_terms = ("thank", "thanks", "great", "awesome", "helpful", "love this", "excellent")
    positive = sum(
        1 for value in texts
        if any(term in value.casefold() for term in positive_terms)
    )

    topic_rules = [
        ("AI tools and plans", ("gemini", "chatgpt", "claude", "tool", "free", "plan", "price")),
        ("AI agents and automation", ("agent", "agents", "automation", "workflow", "multi-agent")),
        ("Setup and troubleshooting", ("install", "setup", "error", "not working", "issue", "login")),
        ("Tutorial requests", ("tutorial", "explain", "how to", "step by step", "cover")),
    ]
    topic_counts = []
    for label, keywords in topic_rules:
        count = sum(
            1 for value in texts
            if any(keyword in value.casefold() for keyword in keywords)
        )
        if count:
            topic_counts.append({"topic": label, "count": count})
    topic_counts.sort(key=lambda item: (-item["count"], item["topic"]))

    patterns = []
    if questions:
        patterns.append(f"{questions} of {len(sample)} sampled comments ask a question.")
    if unanswered:
        patterns.append(f"{unanswered} comments have no replies yet.")
    if positive:
        patterns.append(f"{positive} comments contain positive feedback or thanks.")
    if topic_counts:
        top = topic_counts[0]
        patterns.append(
            f"The most visible topic in this sample is {top['topic'].lower()} ({top['count']} comments)."
        )
    if not patterns and sample:
        patterns.append("No strong repeated theme is visible in this small sample.")
    return {
        "sample_size": len(sample),
        "questions": questions,
        "unanswered": unanswered,
        "positive": positive,
        "topics": topic_counts,
        "patterns": patterns,
    }


def _merge_comment_page_items(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Deduplicate threads across moderation categories and successive pages."""
    merged = {}
    for item in list(existing or []) + list(incoming or []):
        thread_id = str(item.get("thread_id") or "")
        if not thread_id:
            continue
        if thread_id not in merged:
            merged[thread_id] = dict(item)
            continue
        current = merged[thread_id]
        statuses = set(current.get("matched_moderation_statuses") or [])
        statuses.update(item.get("matched_moderation_statuses") or [])
        current["matched_moderation_statuses"] = sorted(statuses)
        current["has_held_content"] = bool(
            current.get("has_held_content") or item.get("has_held_content")
            or {"heldForReview", "likelySpam"} & statuses
        )
        if item.get("review_status") in {"heldForReview", "likelySpam", "heldReply", "spamReply"}:
            current["review_status"] = item["review_status"]
        replies = {reply.get("comment_id"): reply for reply in current.get("replies") or []}
        replies.update({reply.get("comment_id"): reply for reply in item.get("replies") or []})
        current["replies"] = list(replies.values())
    return sorted(
        merged.values(),
        key=lambda item: str(item.get("published_at") or ""),
        reverse=True,
    )


def _load_comment_page(youtube, status: str, search_terms: str, page_token: str | dict | None = None):
    return list_channel_comment_threads(
        youtube,
        CHANNEL_ID,
        moderation_status=status,
        page_token=page_token,
        max_results=25,
        search_terms=search_terms or None,
        order="time",
    )



def _generate_latest_unanswered_reply_drafts(youtube, store, secrets: dict, *, limit: int = 10, scan_limit: int = 300, max_age_days: int = 7) -> dict:
    """Generate editable reply drafts for recent threads not yet answered by the channel."""
    requested = max(1, min(int(limit), 20))
    scan = list_latest_unanswered_comment_threads(
        youtube, CHANNEL_ID, target_count=min(50, requested * 3), scan_limit=scan_limit,
        max_age_days=max_age_days,
    )
    existing = store.list_comment_reply_drafts(limit=100)
    active = {"pending_review", "approved", "publishing", "verification_unavailable", "held_for_review", "likely_spam", "published"}
    known = {str(item.get("parent_comment_id") or "") for item in existing if item.get("status") in active}
    staged = skipped_existing = skipped_not_replyable = no_safe_reply = errors = 0
    for thread in scan.get("items") or []:
        parent_id = str(thread.get("top_level_comment_id") or "").strip()
        thread_id = str(thread.get("thread_id") or "").strip()
        if not parent_id or not thread_id or not str(thread.get("text") or "").strip():
            errors += 1
            continue
        if parent_id in known:
            skipped_existing += 1
            continue
        if not thread.get("can_reply"):
            skipped_not_replyable += 1
            continue
        video_id, video_title = thread.get("video_id"), ""
        if video_id:
            title_key = f"_comment_video_title_{video_id}"
            video_title = st.session_state.get(title_key) or ""
            if not video_title:
                try:
                    video_title = str(fetch_video_snippet(youtube, video_id).get("title") or "")
                    st.session_state[title_key] = video_title
                except Exception:
                    video_title = ""
        try:
            analysis = analyze_comment_thread(secrets, {**thread, "video_title": video_title}, thread.get("replies") or [])
            reply_text = str(analysis.get("suggested_reply") or "").strip()
            if not reply_text or analysis.get("reply_recommendation") == "ignore" or not thread.get("owner_unanswered"):
                no_safe_reply += 1
                continue
            snapshot = str(thread.get("text") or "")[:5000]
            digest = compute_comment_reply_digest(store.owner_key, thread_id, parent_id, snapshot, reply_text)
            store.create_comment_reply_draft({
                "channel_id": CHANNEL_ID, "video_id": video_id, "video_title": video_title,
                "thread_id": thread_id, "parent_comment_id": parent_id,
                "comment_text_snapshot": snapshot,
                "comment_author_display_name": thread.get("author_display_name") or "",
                "can_reply": True, "draft_text": reply_text,
                "approval_digest": digest, "status": "pending_review",
            })
            known.add(parent_id)
            staged += 1
            if staged >= requested:
                break
        except Exception:
            errors += 1
    return {
        "scanned_count": int(scan.get("scanned_count") or 0),
        "candidate_count": len(scan.get("items") or []),
        "unknown_reply_threads": int(scan.get("unknown_reply_threads") or 0),
        "unknown_date_threads": int(scan.get("unknown_date_threads") or 0),
        "age_cutoff_reached": bool(scan.get("age_cutoff_reached")),
        "max_age_days": int(scan.get("max_age_days") or max_age_days),
        "scan_limit_reached": bool(scan.get("scan_limit_reached")),
        "staged": staged, "skipped_existing": skipped_existing,
        "skipped_not_replyable": skipped_not_replyable,
        "no_safe_reply": no_safe_reply, "errors": errors,
    }




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
                except CommentModerationPending as error:
                    moderation_state = (
                        "held_for_review" if error.moderation_status == "heldForReview" else "likely_spam"
                    )
                    store.update_comment_reply_draft(draft_id, {
                        "status": moderation_state,
                        "published_comment_id": error.comment_id,
                        "error": str(error)[:1000],
                    })
                    st.warning(
                        f"YouTube accepted the reply but placed it in "
                        f"{'Held for review' if moderation_state == 'held_for_review' else 'Likely spam'}. "
                        "Check YouTube Studio; this reply will not be submitted again."
                    )
                except CommentVerificationUnavailable as error:
                    store.update_comment_reply_draft(draft_id, {
                        "status": "verification_unavailable",
                        "error": str(error)[:1000],
                    })
                    st.error(f"The app could not verify the reply's final state. Do not retry blindly. {error}")
                except Exception as error:
                    store.update_comment_reply_draft(draft_id, {
                        "status": "failed",
                        "error": str(error)[:1000],
                    })
                    st.error(f"Reply publishing failed: {error}")


def _render_selected_thread(youtube, store, secrets, thread: dict):
    thread_id = thread.get("thread_id")
    parent_id = thread.get("top_level_comment_id")
    st.caption(
        f"{thread.get('author_display_name') or 'Viewer'} · "
        f"{thread.get('published_at') or 'Date unavailable'} · "
        f"{thread.get('total_reply_count', 0)} replies"
    )
    if not parent_id:
        st.warning("The top-level comment ID is unavailable.")
        return

    replies_key = f"_comment_replies_{thread_id}"
    analysis_key = f"_comment_analysis_{thread_id}"
    if st.button(
        "Load thread & generate reply suggestion",
        type="primary",
        key=f"comment_load_analyze_{thread_id}",
    ):
        try:
            with st.spinner("Loading the conversation and preparing a reply suggestion..."):
                page = list_thread_replies(youtube, parent_id, max_results=25)
                st.session_state[replies_key] = page
                replies = (page or {}).get("items") or []
                analysis_thread = dict(thread)
                video_id = thread.get("video_id")
                if video_id:
                    title_key = f"_comment_video_title_{video_id}"
                    video_title = st.session_state.get(title_key)
                    if not video_title:
                        try:
                            video_title = fetch_video_snippet(youtube, video_id).get("title") or ""
                            st.session_state[title_key] = video_title
                        except Exception:
                            video_title = ""
                    analysis_thread["video_title"] = video_title
                analysis = analyze_comment_thread(secrets, analysis_thread, replies)
                st.session_state[analysis_key] = analysis
        except Exception as error:
            st.error(f"Could not load and analyze this thread: {error}")

    replies_page = st.session_state.get(replies_key)
    replies = (replies_page or {}).get("items") or []
    if replies_page:
        st.markdown("**Conversation**")
        if replies:
            for reply in replies:
                with st.container(border=True):
                    reply_status = reply.get("moderation_status") or "published"
                    st.caption(
                        f"{reply.get('author_display_name') or 'Viewer'} · "
                        f"{MODERATION_LABELS.get(reply_status, 'Status unavailable')}"
                    )
                    st.write(reply.get("text") or "")
        else:
            st.caption("No replies yet. This is an unanswered thread.")
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

    analysis = st.session_state.get(analysis_key)
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
                "This comment touches on a fact that must be checked. A safe, non-factual holding reply "
                "may be shown below; verify the answer before sending it."
            )
        if analysis["reply_recommendation"] == "ignore":
            st.info("Recommended action: do not reply to this comment.")
        elif analysis.get("suggested_reply") and thread.get("can_reply"):
            draft_key = f"_comment_reply_text_{thread_id}"
            reply_text = st.text_area(
                "Suggested reply (edit before saving)",
                value=st.session_state.get(draft_key, analysis["suggested_reply"]),
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
        elif analysis["reply_recommendation"] == "verify_first":
            st.warning(
                "The analysis confidence is too low to suggest a reply. Check the context manually before drafting one."
            )


def _render_uncertain_replies(store):
    st.subheader("Replies needing manual verification")
    uncertain = store.list_comment_reply_drafts(status="verification_unavailable", limit=20)
    held = store.list_comment_reply_drafts(status="held_for_review", limit=20)
    spam = store.list_comment_reply_drafts(status="likely_spam", limit=20)
    drafts = [(item, "uncertain") for item in uncertain]
    drafts += [(item, "held") for item in held]
    drafts += [(item, "spam") for item in spam]
    if not drafts:
        st.caption("No replies are awaiting manual verification or moderation review.")
        return
    st.warning(
        "These replies are not eligible for another publish attempt. Check their exact status in YouTube Studio. "
        "The application will not retry them automatically."
    )
    for draft, state in drafts:
        draft_id = draft["id"]
        with st.container(border=True):
            st.caption(draft.get("video_title") or draft.get("video_id") or "Channel comment")
            if state == "held":
                st.warning("YouTube found this reply but placed it in Held for review. It is not confirmed publicly visible.")
            elif state == "spam":
                st.warning("YouTube found this reply but classified it as likely spam. It is not confirmed publicly visible.")
            else:
                st.info("The API accepted the insert request, but the app could not confirm the final state.")
            st.markdown("**Original comment**")
            st.write(draft.get("comment_text_snapshot") or "")
            st.markdown("**Reply**")
            st.write(draft.get("draft_text") or "")
            if draft.get("published_comment_id"):
                st.caption(f"YouTube reply ID: {draft['published_comment_id']}")
            if state == "uncertain":
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
            elif state in {"held", "spam"}:
                if st.button(
                    "I confirmed this reply is now public in YouTube Studio",
                    key=f"comment_moderated_public_{draft_id}",
                ):
                    store.update_comment_reply_draft(draft_id, {
                        "status": "published",
                        "result": {
                            "manual_verification": True,
                            "verification_note": "Owner confirmed the moderated reply is now publicly visible in YouTube Studio.",
                        },
                        "published_at": datetime.now(timezone.utc).isoformat(),
                        "error": None,
                    })
                    st.success("Marked as manually verified.")
                    st.rerun()
                if st.button(
                    "I confirmed this reply is not public",
                    key=f"comment_moderated_not_public_{draft_id}",
                ):
                    store.update_comment_reply_draft(draft_id, {
                        "status": "failed",
                        "result": {
                            "manual_verification": True,
                            "verification_note": "Owner confirmed the reply is not publicly visible in YouTube Studio.",
                        },
                        "error": "Owner confirmed the reply is not public.",
                    })
                    st.success("Resolved. You can create a new draft if you still want to reply.")
                    st.rerun()
            else:
                st.caption("Confirm the reply's final visibility in YouTube Studio before resolving it.");


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

    workspace_view = st.radio(
        "Comments workspace",
        options=["Inbox", "Reply drafts", "Needs verification"],
        horizontal=True,
        key="comments_workspace_view",
        label_visibility="collapsed",
    )
    if workspace_view == "Reply drafts":
        generation_summary = st.session_state.get("_comments_latest_reply_generation_summary")
        if generation_summary:
            st.info(
                f"Latest scan: {generation_summary['staged']} drafts prepared from "
                f"{generation_summary['scanned_count']} scanned threads · "
                f"{generation_summary['skipped_existing']} existing drafts skipped · "
                f"{generation_summary['no_safe_reply']} without a safe suggestion · "
                f"{generation_summary.get('unknown_date_threads', 0)} with unknown dates excluded."
            )
        _render_pending_replies(youtube, persistent_store)
        return
    if workspace_view == "Needs verification":
        _render_uncertain_replies(persistent_store)
        return

    col_status, col_search = st.columns([1, 2])
    with col_status:
        status = st.selectbox(
            "Comment status",
            options=["all", "published", "heldForReview", "likelySpam"],
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
    with st.container(border=True):
        st.subheader("Generate replies for latest unanswered comments")
        st.caption(
            "Only published comments from the last 7 days are eligible. Older comments stay in the Inbox "
            "but are excluded from reply generation. Replies are saved as drafts for your approval."
        )
        reply_limit = st.selectbox(
            "How many reply drafts to prepare?", options=[5, 10, 15, 20],
            index=1, key="comments_latest_reply_limit",
        )
        if st.button(
            "Generate replies for latest unanswered comments",
            type="primary", key="comments_generate_latest_unanswered",
        ):
            try:
                with st.spinner("Checking recent comment threads and preparing reply drafts..."):
                    result = _generate_latest_unanswered_reply_drafts(
                        youtube, persistent_store, secrets, limit=reply_limit
                    )
                st.session_state["_comments_latest_reply_generation_summary"] = result
                st.session_state["_comments_show_pending_replies"] = True
                st.rerun()
            except Exception as error:
                st.error(f"Could not prepare latest unanswered replies: {error}")

    generation_summary = st.session_state.get("_comments_latest_reply_generation_summary")
    if generation_summary:
        st.info(
            f"Prepared {generation_summary['staged']} reply drafts from {generation_summary['scanned_count']} scanned threads. "
            f"Skipped {generation_summary['skipped_existing']} with existing drafts/replies, "
            f"{generation_summary['no_safe_reply']} without a safe suggestion, and "
            f"{generation_summary['unknown_reply_threads']} with incomplete reply-author data. "
            f"{generation_summary['errors']} errors."
        )
        if generation_summary["scan_limit_reached"]:
            st.caption("The scan limit was reached before the requested number of new drafts was found.")

    summary = _comment_pattern_summary(items)
    with st.container(border=True):
        st.subheader("Comment pattern summary")
        st.caption(
            f"Recent sample: {summary['sample_size']} comments from the current "
            f"{MODERATION_LABELS[status].lower()} filter. Patterns describe this loaded sample, not the entire channel."
        )
        summary_cols = st.columns(4)
        summary_cols[0].metric("Questions", summary["questions"])
        summary_cols[1].metric("No replies", summary["unanswered"])
        summary_cols[2].metric("Positive feedback", summary["positive"])
        summary_cols[3].metric("Sample size", summary["sample_size"])
        if summary["patterns"]:
            for pattern in summary["patterns"]:
                st.write(f"• {pattern}")
        else:
            st.caption("Load comments to identify recurring questions, sentiment and topics.")
        if summary["topics"]:
            st.caption("Topics: " + " · ".join(
                f"{item['topic']} ({item['count']})" for item in summary["topics"][:3]
            ))
    st.caption("Select a comment to analyze its thread and prepare a reply suggestion. Every reply requires manual approval.")
    if status == "all":
        held_count = sum(1 for item in items if item.get("has_held_content"))
        st.info(
            f"All moderation states are included in this view. {held_count} of the loaded threads "
            "contain a comment or reply held by YouTube. YouTube Studio groups held-for-review "
            "and likely-spam comments in its Held tab."
        )
    st.caption(f"Showing {len(items)} unique threads · {MODERATION_LABELS[status]}")
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
        filter_col1, filter_col2 = st.columns(2)
        with filter_col1:
            only_unanswered = st.checkbox(
                "Only threads with no replies at all",
                key=f"comments_unanswered_{status}_{search_terms.strip().casefold()}",
            )
        with filter_col2:
            only_held = st.checkbox(
                "Only comments/replies held by YouTube",
                key=f"comments_held_{status}_{search_terms.strip().casefold()}",
                disabled=status != "all",
            )
        visible = items if selected_video == "All loaded videos" else [
            item for item in items
            if (item.get("video_id") or "Channel comment") == selected_video
        ]
        if only_unanswered:
            visible = [item for item in visible if int(item.get("total_reply_count") or 0) == 0]
        if only_held and status == "all":
            visible = [item for item in visible if item.get("has_held_content")]
        for thread in visible:
            thread_id = str(thread.get("thread_id") or "")
            is_open = st.session_state.get("_selected_comment_thread_id") == thread_id
            with st.container(border=True):
                moderation_label = MODERATION_LABELS.get(
                    thread.get("review_status") or thread.get("moderation_status") or "published",
                    "Status unavailable",
                )
                st.caption(
                    f"{thread.get('author_display_name') or 'Viewer'} · "
                    f"{thread.get('published_at') or 'Date unavailable'} · "
                    f"Moderation: {moderation_label}"
                )
                if thread.get("has_held_content"):
                    st.warning(
                        "YouTube has held this comment thread or at least one reply. "
                        "Review it in YouTube Studio; this app will not approve or reject it automatically."
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
                if st.button(
                    "Close thread" if is_open else "Open thread",
                    key=f"comments_open_{thread_id}",
                ):
                    if is_open:
                        st.session_state.pop("_selected_comment_thread_id", None)
                    else:
                        st.session_state["_selected_comment_thread_id"] = thread_id
                        st.session_state["_selected_comment_thread"] = thread
                    st.rerun()
                if is_open:
                    _render_selected_thread(youtube, persistent_store, secrets, thread)

    if page.get("next_page_token") and st.button(
        "Load more comments", key=f"comments_load_more_{status}_{search_terms.strip().casefold()}"
    ):
        try:
            with st.spinner("Loading the next comment page..."):
                next_page = _load_comment_page(
                    youtube, status, search_terms, page_token=page["next_page_token"]
                )
            st.session_state[cache_key] = {
                "items": _merge_comment_page_items(items, next_page["items"]),
                "next_page_token": next_page.get("next_page_token"),
                "total_results": next_page.get("total_results") or page.get("total_results"),
                "moderation_status": status,
            }
            st.rerun()
        except Exception as error:
            st.error(f"Could not load more comments: {error}")


