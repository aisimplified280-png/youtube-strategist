"""Approval-gated metadata write, stale-check, and verification engine."""

from typing import Any, Callable, Dict, List, Optional
from googleapiclient.discovery import Resource

import youtube_analytics_connector as connector


def compute_proposal_digest(video_id: str, before_snippet: Dict[str, Any], proposed_snippet: Dict[str, Any]) -> str:
    """Compute HMAC digest for exact approved proposal."""
    return connector.metadata_proposal_digest(video_id, before_snippet, proposed_snippet)


def apply_proposal(
    youtube: Resource,
    video_id: str,
    before_snippet: Dict[str, Any],
    proposed_snippet: Dict[str, Any],
    approved_digest: str,
    audit_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Execute approval-gated update to YouTube Data API with stale check and verify.
    
    1. Verifies approved digest matches proposal.
    2. Re-fetches current live snippet from YouTube (stale-check).
    3. Writes ONLY title, description, tags.
    4. Re-fetches to verify updated values.
    5. Dispatches audit record.
    """
    return connector.apply_approved_dashboard_proposal(
        youtube,
        video_id,
        before_snippet,
        proposed_snippet,
        approved_digest,
        audit_callback=audit_callback,
    )
