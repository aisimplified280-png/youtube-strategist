"""Channel-wide comments inbox with on-demand analysis and manually approved replies."""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core.comments.analyzer import analyze_comment_thread
from youtube_analytics_connector import CHANNEL_ID

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
            st.write(draft.get("draft_text") or "")
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
                analysis = analyze_comment_thread(secrets, thread, replies)
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
            st.warning("This comment requires fact-checking. No reply draft was created.")
        elif analysis["reply_recommendation"] == "ignore":
            st.info("Recommended action: do not reply to this comment.")
        elif analysis.get("suggested_reply") and thread.get("can_reply"):
            draft_key = f"_comment_reply_text_{thread_id}"
            reply_text = st.text_area(
                "Edit suggested reply",
                value=st.session_state.get(draft_key, analysis["suggested_reply"]),
                key=f"comment_reply_editor_{thread_id}",
                height=100,
            )
            if st.button("Save reply draft for approval", key=f"comment_save_reply_{thread_id}"):
                digest = compute_comment_reply_digest(
                    store.owner_key,
                    str(thread_id or ""),
                    str(parent_id or ""),
                    str(thread.get("text") or ""),
                    reply_text,
                )
                try:
                    saved = store.create_comment_reply_draft({
                        "channel_id": "UCS6Sb5rfaEnWTVPGyQlnHFQ",
                        "video_id": thread.get("video_id"),
                        "video_title": thread.get("video_title") or "",
                        "thread_id": thread_id,
                        "parent_comment_id": parent_id,
                        "comment_text_snapshot": str(thread.get("text") or "")[:5000],
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

    col_status, col_search = st.columns([1, 2])
    with col_status:
        status = st.selectbox(
            "Comment status",
            options=list(MODERATION_LABELS),
            format_func=lambda value: MODERATION_LABELS[value],
            key="comments_moderation_status",
        )
    with col_search:
        search_terms = st.text_input("Search comments", key="comments_search_terms")
    cache_key = f"_comments_page_{status}_{search_terms.strip().casefold()}"
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
        visible = items if selected_video == "All loaded videos" else [
            item for item in items
            if (item.get("video_id") or "Channel comment") == selected_video
        ]
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
            st.session_state[cache_key] = {
                "items": items + next_page["items"],
                "next_page_token": next_page.get("next_page_token"),
                "total_results": next_page.get("total_results") or page.get("total_results"),
                "moderation_status": status,
            }
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
