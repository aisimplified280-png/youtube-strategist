"""Owner-channel YouTube comment retrieval, replies and manual publish verification."""
from __future__ import annotations

from typing import Any


MODERATION_STATUSES = {"published", "heldForReview", "likelySpam"}
MAX_COMMENT_PAGE_SIZE = 100
MAX_REPLY_LENGTH = 10000


class CommentAPIError(RuntimeError):
    pass


class CommentVerificationUnavailable(CommentAPIError):
    pass


def _normalise_comment(comment: dict) -> dict:
    snippet = comment.get("snippet") or {}
    return {
        "comment_id": comment.get("id"),
        "parent_id": snippet.get("parentId"),
        "author_display_name": snippet.get("authorDisplayName") or "Unknown viewer",
        "text": snippet.get("textDisplay") or snippet.get("textOriginal") or "",
        "published_at": snippet.get("publishedAt"),
        "updated_at": snippet.get("updatedAt"),
        "like_count": int(snippet.get("likeCount") or 0),
        "moderation_status": snippet.get("moderationStatus") or "published",
    }


def _normalise_thread(thread: dict) -> dict:
    snippet = thread.get("snippet") or {}
    top = snippet.get("topLevelComment") or {}
    comment = _normalise_comment(top)
    return {
        "thread_id": thread.get("id"),
        "video_id": snippet.get("videoId"),
        "channel_id": snippet.get("channelId"),
        "top_level_comment_id": comment.get("comment_id"),
        "author_display_name": comment.get("author_display_name"),
        "text": comment.get("text"),
        "published_at": comment.get("published_at"),
        "updated_at": comment.get("updated_at"),
        "like_count": comment.get("like_count", 0),
        "moderation_status": comment.get("moderation_status"),
        "total_reply_count": int(snippet.get("totalReplyCount") or 0),
        "can_reply": bool(snippet.get("canReply", False)),
    }


def list_channel_comment_threads(
    youtube,
    channel_id: str,
    *,
    moderation_status: str = "published",
    page_token: str | None = None,
    max_results: int = 25,
    search_terms: str | None = None,
    order: str = "time",
) -> dict:
    """Fetch one channel-wide page; older pages are requested only on user action."""
    if youtube is None:
        raise CommentAPIError("YouTube API client is not connected.")
    if not channel_id:
        raise ValueError("A verified channel ID is required.")
    if moderation_status not in MODERATION_STATUSES:
        raise ValueError("Unsupported comment moderation status.")
    size = max(1, min(int(max_results), MAX_COMMENT_PAGE_SIZE))
    if order not in {"time", "relevance"}:
        raise ValueError("Comment order must be time or relevance.")

    params = {
        "part": "snippet",
        "allThreadsRelatedToChannelId": channel_id,
        "moderationStatus": moderation_status,
        "order": order,
        "maxResults": size,
        "textFormat": "plainText",
    }
    if page_token:
        params["pageToken"] = page_token
    if search_terms and search_terms.strip():
        params["searchTerms"] = search_terms.strip()[:100]

    response = youtube.commentThreads().list(**params).execute()
    return {
        "items": [_normalise_thread(item) for item in response.get("items") or []],
        "next_page_token": response.get("nextPageToken"),
        "total_results": (response.get("pageInfo") or {}).get("totalResults"),
        "moderation_status": moderation_status,
    }


def list_thread_replies(
    youtube,
    parent_comment_id: str,
    *,
    page_token: str | None = None,
    max_results: int = 25,
) -> dict:
    """Load replies only when a user opens a thread; thread-list replies may be partial."""
    if youtube is None:
        raise CommentAPIError("YouTube API client is not connected.")
    if not parent_comment_id:
        raise ValueError("A top-level comment ID is required.")
    params = {
        "part": "snippet",
        "parentId": parent_comment_id,
        "maxResults": max(1, min(int(max_results), MAX_COMMENT_PAGE_SIZE)),
        "textFormat": "plainText",
    }
    if page_token:
        params["pageToken"] = page_token
    response = youtube.comments().list(**params).execute()
    return {
        "items": [_normalise_comment(item) for item in response.get("items") or []],
        "next_page_token": response.get("nextPageToken"),
    }


def publish_comment_reply(
    youtube,
    parent_comment_id: str,
    reply_text: str,
    *,
    can_reply: bool,
) -> dict:
    """Publish one already-approved reply, then verify it through the parent thread."""
    if youtube is None:
        raise CommentAPIError("YouTube API client is not connected.")
    if not can_reply:
        raise PermissionError("YouTube reports that this comment thread cannot be replied to.")
    text = str(reply_text or "").strip()
    if not text:
        raise ValueError("Reply text cannot be empty.")
    if len(text) > MAX_REPLY_LENGTH:
        raise ValueError(f"Reply text exceeds the {MAX_REPLY_LENGTH}-character limit.")
    if not parent_comment_id:
        raise ValueError("A top-level parent comment ID is required.")

    created = youtube.comments().insert(
        part="snippet",
        body={"snippet": {"parentId": parent_comment_id, "textOriginal": text}},
    ).execute()
    created_id = created.get("id")
    if not created_id:
        raise CommentVerificationUnavailable(
            "YouTube did not return a reply ID. Do not retry automatically."
        )

    # A successful insert response is retained, then checked against the parent thread.
    try:
        page_token = None
        for _ in range(5):
            page = list_thread_replies(
                youtube, parent_comment_id, page_token=page_token, max_results=100
            )
            if any(item.get("comment_id") == created_id for item in page["items"]):
                return {
                    "status": "published",
                    "comment_id": created_id,
                    "parent_comment_id": parent_comment_id,
                    "reply_text": text,
                }
            page_token = page.get("next_page_token")
            if not page_token:
                break
    except Exception as error:
        raise CommentVerificationUnavailable(
            f"YouTube accepted reply {created_id}, but read-back verification failed: {error}"
        ) from error

    raise CommentVerificationUnavailable(
        f"YouTube accepted reply {created_id}, but it was not visible in the verification read. Do not retry blindly."
    )
