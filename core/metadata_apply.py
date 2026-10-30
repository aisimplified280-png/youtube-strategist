"""Approval-gated metadata write, stale-check, and verification engine."""

from typing import Any, Callable, Dict, List, Optional
from googleapiclient.discovery import Resource

import youtube_analytics_connector as connector


def compute_proposal_digest(video_id: str, before_snippet: Dict[str, Any], proposed_snippet: Dict[str, Any]) -> str:
    """Compute HMAC digest for exact approved proposal."""
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
    
    1. Verifies approved digest matches proposal.
    2. Re-fetches current live snippet from YouTube (stale-check).
    3. Writes ONLY title, description, tags.
    4. Re-fetches to verify updated values.
    5. Dispatches audit record.
    """
    # Accept proposal alias if passed
    if "proposal" in kwargs and not proposed_snippet:
        proposed_snippet = kwargs["proposal"]

    # Handle demo mode or mock video IDs
    is_demo_target = is_demo or youtube is None or str(video_id or "").startswith("demo-")
    if is_demo_target:
        expected_digest = compute_proposal_digest(video_id, before_snippet, proposed_snippet)
        if expected_digest != approved_digest:
            return {
                "status": "error",
                "message": "Approval digest does not match proposed metadata. Re-approval required.",
            }
        # Simulate successful audit dispatch
        if audit_callback:
            audit_callback({
                "video_id": video_id,
                "before": before_snippet,
                "proposed": proposed_snippet,
                "digest": approved_digest,
                "status": "demo_verified",
            })
        return {
            "status": "success",
            "message": "Demo Mode: Metadata update simulated and verified successfully.",
            "snippet": proposed_snippet,
        }

    try:
        updated_snippet = connector.apply_approved_dashboard_proposal(
            youtube,
            video_id,
            before_snippet,
            proposed_snippet,
            approved_digest,
            audit_callback=audit_callback,
        )
        return {
            "status": "success",
            "message": f"Verified YouTube Data API update for '{updated_snippet.get('title')}'.",
            "snippet": updated_snippet,
        }
    except Exception as err:
        return {
            "status": "error",
            "message": str(err),
        }
