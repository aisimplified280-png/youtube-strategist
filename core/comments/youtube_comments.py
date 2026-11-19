"""Owner-channel YouTube comment retrieval, replies and manual publish verification."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from googleapiclient.errors import HttpError


MODERATION_STATUSES = {"all", "published", "heldForReview", "likelySpam"}
MAX_COMMENT_PAGE_SIZE = 100
MAX_REPLY_LENGTH = 10000


class CommentAPIError(RuntimeError):
    pass


class CommentVerificationUnavailable(CommentAPIError):
    pass


class CommentModerationPending(CommentAPIError):
    """YouTube accepted a reply but placed it in a moderation queue."""

    def __init__(self, comment_id: str, moderation_status: str):
        self.comment_id = comment_id
        self.moderation_status = moderation_status
        label = "held for review" if moderation_status == "heldForReview" else "classified as likely spam"
        super().__init__(f"YouTube accepted reply {comment_id}, but it is {label}. Review it in YouTube Studio; do not retry.")


def _normalise_comment(comment: dict) -> dict:
    snippet = comment.get("snippet") or {}
    return {
        "comment_id": comment.get("id"),
        "parent_id": snippet.get("parentId"),
        "author_display_name": snippet.get("authorDisplayName") or "Unknown viewer",
        "author_channel_id": (snippet.get("authorChannelId") or {}).get("value") if isinstance(snippet.get("authorChannelId"), dict) else None,
        "text": snippet.get("textDisplay") or snippet.get("textOriginal") or "",
        "published_at": snippet.get("publishedAt"),
        "updated_at": snippet.get("updatedAt"),
        "like_count": int(snippet.get("likeCount") or 0),
        "moderation_status": snippet.get("moderationStatus") or "published",
    }


def _normalise_thread(thread: dict, matched_status: str | None = None) -> dict:
    snippet = thread.get("snippet") or {}
    top = snippet.get("topLevelComment") or {}
    comment = _normalise_comment(top)
    replies = [_normalise_comment(item) for item in ((thread.get("replies") or {}).get("comments") or [])]
    statuses = {matched_status} if matched_status else set()
    top_status = comment.get("moderation_status")
    if top_status in {"published", "heldForReview", "likelySpam"}:
        statuses.add(top_status)
    reply_statuses = {item.get("moderation_status") for item in replies if item.get("moderation_status") in {"heldForReview", "likelySpam"}}
    statuses.update(reply_statuses)
    review_status = None
    if top_status in {"heldForReview", "likelySpam"}:
        review_status = top_status
    elif "heldForReview" in statuses:
        review_status = "heldReply"
    elif "likelySpam" in statuses:
        review_status = "spamReply"
    return {
        "thread_id": thread.get("id"), "video_id": snippet.get("videoId"),
        "channel_id": snippet.get("channelId"), "top_level_comment_id": comment.get("comment_id"),
        "author_display_name": comment.get("author_display_name"), "author_channel_id": comment.get("author_channel_id"),
        "text": comment.get("text"), "published_at": comment.get("published_at"),
        "updated_at": comment.get("updated_at"), "like_count": comment.get("like_count", 0),
        "moderation_status": top_status, "review_status": review_status,
        "matched_moderation_statuses": sorted(statuses),
        "has_held_content": bool({"heldForReview", "likelySpam"} & statuses),
        "replies": replies, "total_reply_count": int(snippet.get("totalReplyCount") or 0),
        "can_reply": bool(snippet.get("canReply", False)),
    }


def list_channel_comment_threads(
    youtube, channel_id: str, *, moderation_status: str = "published",
    page_token: str | dict | None = None, max_results: int = 25,
    search_terms: str | None = None, order: str = "time",
) -> dict:
    """Fetch channel comments, optionally merging all moderation states."""
    if youtube is None:
        raise CommentAPIError("YouTube API client is not connected.")
    if not channel_id:
        raise ValueError("A verified channel ID is required.")
    if moderation_status not in MODERATION_STATUSES:
        raise ValueError("Unsupported comment moderation status.")
    size = max(1, min(int(max_results), MAX_COMMENT_PAGE_SIZE))
    if order not in {"time", "relevance"}:
        raise ValueError("Comment order must be time or relevance.")
    if moderation_status == "all":
        cursors = page_token if isinstance(page_token, dict) else {}
        merged: dict[str, dict] = {}
        next_tokens, totals = {}, 0
        for state in ("published", "heldForReview", "likelySpam"):
            if state in cursors and cursors[state] is None:
                next_tokens[state] = None
                continue
            page = list_channel_comment_threads(
                youtube, channel_id, moderation_status=state, page_token=cursors.get(state),
                max_results=size, search_terms=search_terms, order=order,
            )
            totals += int(page.get("total_results") or 0)
            next_tokens[state] = page.get("next_page_token")
            for item in page.get("items") or []:
                key = str(item.get("thread_id") or "")
                if not key:
                    continue
                if key not in merged:
                    merged[key] = item
                else:
                    existing = merged[key]
                    statuses = set(existing.get("matched_moderation_statuses") or [])
                    statuses.update(item.get("matched_moderation_statuses") or [])
                    existing["matched_moderation_statuses"] = sorted(statuses)
                    existing["has_held_content"] = bool({"heldForReview", "likelySpam"} & statuses)
                    if item.get("review_status") in {"heldForReview", "likelySpam", "heldReply", "spamReply"}:
                        existing["review_status"] = item["review_status"]
                    if item.get("replies"):
                        by_id = {r.get("comment_id"): r for r in existing.get("replies") or []}
                        by_id.update({r.get("comment_id"): r for r in item["replies"]})
                        existing["replies"] = list(by_id.values())
        items = list(merged.values())
        items.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
        return {"items": items, "next_page_token": next_tokens if any(next_tokens.values()) else None,
                "total_results": totals, "moderation_status": "all"}
    params = {
        "part": "snippet,replies", "allThreadsRelatedToChannelId": channel_id,
        "moderationStatus": moderation_status, "order": order, "maxResults": size,
        "textFormat": "plainText",
    }
    if page_token:
        params["pageToken"] = page_token
    if search_terms and search_terms.strip():
        params["searchTerms"] = search_terms.strip()[:100]
    response = youtube.commentThreads().list(**params).execute()
    return {
        "items": [_normalise_thread(item, moderation_status) for item in response.get("items") or []],
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



def list_latest_unanswered_comment_threads(youtube, channel_id: str, *, target_count: int = 10, scan_limit: int = 300) -> dict:
    """Find recent published threads without an owner reply; uncertain identities fail closed."""
    if youtube is None:
        raise CommentAPIError("YouTube API client is not connected.")
    if not channel_id:
        raise ValueError("A verified channel ID is required.")
    target = max(1, min(int(target_count), 50))
    maximum = max(target, min(int(scan_limit), 1000))
    matches, scanned, unknown, page_token = [], 0, 0, None

    while scanned < maximum and len(matches) < target:
        page = list_channel_comment_threads(
            youtube, channel_id, moderation_status="published", page_token=page_token,
            max_results=min(MAX_COMMENT_PAGE_SIZE, maximum-scanned), order="time",
        )
        threads = page.get("items") or []
        if not threads:
            break
        for thread in threads:
            scanned += 1
            top_level_author_id = str(thread.get("author_channel_id") or "").strip()
            if not top_level_author_id:
                unknown += 1
                continue
            if top_level_author_id == channel_id:
                continue
            reply_count = int(thread.get("total_reply_count") or 0)
            if reply_count == 0:
                thread["replies"] = []
                thread["owner_unanswered"] = True
                matches.append(thread)
            else:
                replies, reply_token = [], None
                owner_replied, authors_known, complete = False, True, False
                for _ in range(10):  # Bounded to 1,000 replies per thread.
                    reply_page = list_thread_replies(
                        youtube, str(thread.get("top_level_comment_id") or ""),
                        page_token=reply_token, max_results=100,
                    )
                    current = reply_page.get("items") or []
                    replies.extend(current)
                    for reply in current:
                        author_id = str(reply.get("author_channel_id") or "").strip()
                        if not author_id:
                            authors_known = False
                        elif author_id == channel_id:
                            owner_replied = True
                    if owner_replied:
                        break
                    reply_token = reply_page.get("next_page_token")
                    if not reply_token or len(replies) >= reply_count:
                        complete = True
                        break
                if owner_replied:
                    continue
                if complete and len(replies) >= reply_count and authors_known:
                    thread["replies"] = replies
                    thread["owner_unanswered"] = True
                    matches.append(thread)
                else:
                    unknown += 1
            if len(matches) >= target or scanned >= maximum:
                break
        page_token = page.get("next_page_token")
        if len(matches) >= target or not page_token:
            break
    return {
        "items": matches, "scanned_count": scanned, "unknown_reply_threads": unknown,
        "next_page_token": page_token, "scan_limit_reached": scanned >= maximum and len(matches) < target,
        "moderation_status": "published",
    }





def compute_comment_reply_digest(
    owner_key: str,
    thread_id: str,
    parent_comment_id: str,
    comment_text_snapshot: str,
    reply_text: str,
) -> str:
    payload = {
        "owner_key": owner_key,
        "thread_id": thread_id,
        "parent_comment_id": parent_comment_id,
        "comment_text_snapshot": comment_text_snapshot,
        "reply_text": str(reply_text or "").strip(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def publish_approved_comment_reply(youtube, draft: dict, owner_key: str) -> dict:
    """Server-side manual-approval gate for publishing one persisted reply draft."""
    if not isinstance(draft, dict) or draft.get("status") != "publishing":
        raise PermissionError("The reply draft has not passed the explicit approval transition.")
    if not owner_key or draft.get("approved_by") != owner_key or not draft.get("approved_at"):
        raise PermissionError("The reply draft has no approval from the verified channel owner.")
    expected = compute_comment_reply_digest(
        owner_key,
        str(draft.get("thread_id") or ""),
        str(draft.get("parent_comment_id") or ""),
        str(draft.get("comment_text_snapshot") or ""),
        str(draft.get("draft_text") or ""),
    )
    if not hmac.compare_digest(expected, str(draft.get("approval_digest") or "")):
        raise PermissionError("The approved reply text or comment context changed after approval.")
    return publish_comment_reply(
        youtube,
        str(draft.get("parent_comment_id") or ""),
        str(draft.get("draft_text") or ""),
        can_reply=bool(draft.get("can_reply")),
    )

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

    try:
        created = youtube.comments().insert(
            part="snippet",
            body={"snippet": {"parentId": parent_comment_id, "textOriginal": text}},
        ).execute()
    except HttpError as error:
        status = getattr(getattr(error, "resp", None), "status", 0) or 0
        if status >= 500:
            raise CommentVerificationUnavailable(
                "The reply request returned a server error; its final state is uncertain. Do not retry blindly."
            ) from error
        raise
    except Exception as error:
        raise CommentVerificationUnavailable(
            f"The reply request outcome is uncertain due to a transport error. Do not retry blindly: {error}"
        ) from error
    created_id = created.get("id")
    if not created_id:
        raise CommentVerificationUnavailable(
            "YouTube did not return a reply ID. Do not retry automatically."
        )

    # Look up the exact resource returned by insert; scanning parent reply pages can
    # miss newly-created replies and cannot reliably expose their moderation state.
    try:
        response = youtube.comments().list(
            part="snippet",
            id=created_id,
            textFormat="plainText",
        ).execute()
    except Exception as error:
        raise CommentVerificationUnavailable(
            f"YouTube accepted reply {created_id}, but exact-ID verification failed: {error}. Do not retry blindly."
        ) from error

    matches = [
        item for item in (response.get("items") or [])
        if item.get("id") == created_id
    ]
    if not matches:
        raise CommentVerificationUnavailable(
            f"YouTube accepted reply {created_id}, but the exact-ID lookup returned no comment. Do not retry blindly."
        )

    verified = matches[0]
    snippet = verified.get("snippet") or {}
    if snippet.get("parentId") != parent_comment_id:
        raise CommentVerificationUnavailable(
            f"YouTube returned reply {created_id}, but its parent did not match the approved thread. Do not retry blindly."
        )
    returned_text = snippet.get("textOriginal") or snippet.get("textDisplay") or ""
    if returned_text.strip() != text:
        raise CommentVerificationUnavailable(
            f"YouTube returned reply {created_id}, but its text did not match the approved reply. Do not retry blindly."
        )

    moderation_status = snippet.get("moderationStatus")
    if moderation_status == "heldForReview":
        raise CommentModerationPending(created_id, moderation_status)
    if moderation_status == "likelySpam":
        raise CommentModerationPending(created_id, moderation_status)
    if moderation_status != "published":
        raise CommentVerificationUnavailable(
            f"YouTube returned reply {created_id}, but its moderation status was unavailable or unexpected. Do not retry blindly."
        )

    return {
        "status": "published",
        "comment_id": created_id,
        "parent_comment_id": parent_comment_id,
        "reply_text": text,
        "moderation_status": moderation_status,
    }
