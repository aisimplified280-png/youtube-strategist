"""Approval-gated metadata write, stale-check, and verification engine."""

from typing import Any, Callable, Dict, List, Optional
from googleapiclient.discovery import Resource

import youtube_analytics_connector as connector


def compute_proposal_digest(video_id: str, before_snippet: Dict[str, Any], proposed_snippet: Dict[str, Any]) -> str:
    """Compute HMAC digest for exact approved proposal.
    
    Uses canonical parameter names: video_id, before_snippet, proposed_snippet.
    """
    return connector.metadata_proposal_digest(video_id, before_snippet, proposed_snippet)


def apply_proposal(
    youtube: Optional[Resource],
    video_id: str,
    before_snippet: Dict[str, Any],
    proposed_snippet: Dict[str, Any],
    approved_digest: str,
    audit_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    is_demo: bool = False,
    **kwargs,
) -> Dict[str, Any]:
    """Execute approval-gated update to YouTube Data API with stale check and verify.
    
    1. Public write actions are disabled in Demo Mode (fails closed).
    2. Verifies approved digest matches proposal.
    3. Re-fetches current live snippet from YouTube (stale-check).
    4. Writes ONLY title, description, tags, preserving all immutable fields.
    5. Re-fetches to verify updated values.
    6. Dispatches audit record.
    """
    if "proposal" in kwargs and not proposed_snippet:
        proposed_snippet = kwargs["proposal"]

    # In Demo Mode, public writes are strictly disabled
    if is_demo:
        return {
            "status": "error",
            "message": "Public write actions are disabled in Demo Mode. Connect your YouTube channel in Settings to apply updates.",
        }

    if youtube is None:
        return {
            "status": "error",
            "message": "YouTube authorization is missing or disconnected. Please connect YouTube in Settings.",
        }

    # Ensure immutable snippet fields from before_snippet are preserved
    mutable_fields = {"title", "description", "tags"}
    for field, val in before_snippet.items():
        if field not in mutable_fields and field not in proposed_snippet:
            proposed_snippet[field] = val

    try:
        updated_result = connector.apply_approved_dashboard_proposal(
            youtube,
            video_id,
            before_snippet,
            proposed_snippet,
            approved_digest,
            audit_callback=audit_callback,
        )
        return {
            "status": "success",
            "result": updated_result.get("result", "success"),
            "video_id": updated_result.get("video_id", video_id),
            "title": updated_result.get("title", proposed_snippet.get("title", "")),
            "url": updated_result.get("url", f"https://www.youtube.com/watch?v={video_id}"),
            "message": f"Verified YouTube Data API update for '{updated_result.get('title', '')}'.",
            "snippet": proposed_snippet,
        }
    except Exception as err:
        return {
            "status": "error",
            "message": str(err),
        }
