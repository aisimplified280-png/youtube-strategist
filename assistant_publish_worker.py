"""Process explicitly approved assistant-originated YouTube metadata writes.

This worker is intentionally separate from the monitor and proposal-generation worker.
It does not consult the monitor cooldown. It requires an approved queue record, verifies
channel ownership and the exact before-state, publishes through the existing guarded
YouTube Data API helper, then verifies and records the resulting live snapshot.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

import youtube_analytics_connector as connector
import core.batch_metadata as batch_metadata
from worker import _cipher, _get_owner_credentials, _record_live_metadata_snapshot
from core.persistent_store import SupabasePersistentStore

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("assistant-youtube-publisher")


def _same_tags(left, right):
    return {str(item).casefold() for item in (left or [])} == {
        str(item).casefold() for item in (right or [])
    }


def live_matches_before(live: dict, before: dict) -> bool:
    """Reject a queued write if any reviewed mutable value changed after review."""
    if not isinstance(live, dict) or not isinstance(before, dict):
        return False
    for field in ("title", "description"):
        if field in before and live.get(field, "") != before.get(field, ""):
            return False
    if "tags" in before and not _same_tags(live.get("tags"), before.get("tags")):
        return False
    if "categoryId" in before and live.get("categoryId") != before.get("categoryId"):
        return False
    return True


def _finish(store, request_id: str, status: str, result: dict, digest: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    store._request(
        "PATCH",
        "assistant_publish_requests",
        params={"id": f"eq.{request_id}", "status": "eq.processing"},
        payload={
            "status": status,
            "approval_digest": digest or None,
            "result": result,
            "worker_id": None,
            "lease_expires_at": None,
            "finished_at": now,
            "updated_at": now,
        },
        prefer="return=minimal",
    )


def process_one_request(store, cipher, worker_id: str | None = None) -> bool:
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    rows = store._request(
        "POST",
        "rpc/claim_next_assistant_publish_request",
        payload={"p_worker_id": worker_id, "p_lease_seconds": 300},
    )
    if not rows:
        return False

    request = rows[0]
    request_id = request["id"]
    owner_key = request["owner_key"]
    video_id = request["video_id"]
    digest = ""
    try:
        if not request.get("approved_at") or not str(request.get("approved_by") or "").strip():
            raise RuntimeError("Publishing request has no explicit approval record.")

        credentials = _get_owner_credentials(store, owner_key, cipher)
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

        channel_response = youtube.channels().list(part="id", mine=True).execute()
        owned_channels = {
            item.get("id") for item in (channel_response.get("items") or [])
        }
        if connector.CHANNEL_ID not in owned_channels:
            raise RuntimeError("The stored YouTube OAuth grant does not own the configured AI Simplified Lab channel.")

        response = youtube.videos().list(part="snippet", id=video_id).execute()
        items = response.get("items") or []
        if len(items) != 1:
            _finish(store, request_id, "stale", {"message": "Video is missing or unavailable; nothing was changed."})
            return True
        live = items[0].get("snippet") or {}
        if items[0].get("id") != video_id or live.get("channelId") != connector.CHANNEL_ID:
            _finish(store, request_id, "stale", {"message": "Video ownership could not be verified; nothing was changed."})
            return True

        before = request.get("before_metadata") or {}
        if not live_matches_before(live, before):
            _finish(store, request_id, "stale", {
                "message": "Live YouTube metadata changed after review. Refresh and approve a new proposal."
            })
            return True

        patch = request.get("proposed_metadata") or {}
        if not isinstance(patch, dict) or not patch:
            raise ValueError("The approved metadata proposal is empty.")
        if set(patch) - {"title", "description", "tags"}:
            raise ValueError("Only title, description, and tags may be changed by this publishing route.")

        proposed = dict(live)
        for field, value in patch.items():
            proposed[field] = value
        connector._validate_metadata(proposed)

        row = {
            "video_id": video_id,
            "status": "ready",
            "before": dict(live),
            "proposed": proposed,
        }
        digest = connector.metadata_proposal_digest(video_id, row["before"], row["proposed"])
        row["digest"] = digest
        fingerprint = batch_metadata.compute_batch_approval_fingerprint([row], review_revision=1)

        # Explicit approval is the immutable queue record created after the user confirms
        # this exact before/after proposal. No monitor cooldown is consulted here.
        publish_result = batch_metadata.publish_approved_batch_rows(
            youtube,
            [row],
            [video_id],
            approved=True,
            approval_fingerprint=fingerprint,
            review_revision=1,
        )
        if row.get("status") != "verified":
            status = "verification_unavailable" if row.get("status") == "verification_unavailable" else "failed"
            _finish(store, request_id, status, {
                "message": row.get("result_message") or publish_result.get("message") or "YouTube update was not verified.",
                "publish_result": publish_result,
                "row_status": row.get("status"),
            }, digest)
            return True

        # The manual write is allowed regardless of monitor eligibility. Recording its
        # new live state restarts the monitor's observation window for future automation.
        readback = youtube.videos().list(part="snippet", id=video_id).execute()
        after_items = readback.get("items") or []
        if len(after_items) != 1 or after_items[0].get("snippet", {}).get("channelId") != connector.CHANNEL_ID:
            _finish(store, request_id, "verification_unavailable", {
                "message": "YouTube reported success, but the final owner-channel read-back was unavailable.",
                "publish_result": publish_result,
            }, digest)
            return True

        after = after_items[0].get("snippet") or {}
        _record_live_metadata_snapshot(store, owner_key, video_id, after, source="manual")
        _finish(store, request_id, "verified", {
            "message": "Metadata update published and verified against YouTube.",
            "video_id": video_id,
            "title": after.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "changed_fields": [field for field in ("title", "description", "tags") if live.get(field) != after.get(field)],
        }, digest)
        return True
    except Exception as exc:
        log.exception("Assistant publishing request %s failed", request_id)
        _finish(store, request_id, "failed", {
            "message": f"{type(exc).__name__}: {str(exc)[:500]}"
        }, digest)
        return True


def main():
    store = SupabasePersistentStore.worker_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )
    cipher = _cipher(os.environ["YOUTUBE_STRATEGIST_STATE_SECRET"])
    max_requests = max(1, min(int(os.getenv("ASSISTANT_PUBLISH_MAX_REQUESTS", "10")), 50))
    processed = 0
    while processed < max_requests and process_one_request(store, cipher):
        processed += 1
    log.info("Processed %s assistant publishing request(s).", processed)


if __name__ == "__main__":
    main()
