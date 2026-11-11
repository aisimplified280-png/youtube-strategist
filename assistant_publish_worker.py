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
        request_source = str(request.get("request_source") or "chatgpt")
        authorization_mode = str(request.get("authorization_mode") or "explicit_user_command")
        authorized_by = str(request.get("authorized_by") or "").strip()
        authorized_at = request.get("authorized_at")

        if request_source == "chatgpt":
            if authorization_mode != "explicit_user_command" or not authorized_by or not authorized_at:
                raise RuntimeError("ChatGPT publishing requires a recorded explicit user publish command.")
        elif request_source == "monitor":
            if authorization_mode != "monitor_policy" or not authorized_by or not authorized_at:
                raise RuntimeError("Monitor publishing requires a recorded autonomous-policy authorization.")
            policies = store._request("GET", "owner_publish_policies", params={
                "owner_key": f"eq.{owner_key}",
                "select": "monitor_auto_publish_enabled,max_daily_publishes",
                "limit": "1",
            })
            policy = policies[0] if policies else {
                "monitor_auto_publish_enabled": True, "max_daily_publishes": 5
            }
            if policy.get("monitor_auto_publish_enabled") is not True:
                raise RuntimeError("Monitor autonomous publishing has been disabled; no YouTube write was attempted.")
            cap = max(1, min(int(policy.get("max_daily_publishes") or 5), 25))
            day_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            todays = store._request("GET", "assistant_publish_requests", params={
                "owner_key": f"eq.{owner_key}", "request_source": "eq.monitor",
                "status": "eq.verified", "created_at": f"gte.{day_start}", "select": "id",
            })
            if len(todays or []) > cap:
                raise RuntimeError(f"Monitor daily publishing limit reached ({cap} per UTC day).")
        else:
            raise RuntimeError("Unknown publishing request source; refusing to write.")

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

        # The immutable queue record carries its trusted source authorization. ChatGPT
        # requires an explicit publish command; monitors require the owner's autonomous policy.
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
        _record_live_metadata_snapshot(
            store, owner_key, video_id, after,
            source="monitor" if request_source == "monitor" else "manual",
        )
        _finish(store, request_id, "verified", {
            "message": "Metadata update published and verified against YouTube.",
            "request_source": request_source,
            "authorized_by": authorized_by,
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
