"""Independent worker for durable metadata proposal jobs.

Required environment:
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, YOUTUBE_STRATEGIST_STATE_SECRET,
GROQ_API_KEY (primary), GEMINI_API_KEY (batch fallback).
This worker prepares metadata proposals for owner review and processes explicitly approved full-metadata editor audits. It never publishes unapproved batch proposals.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.persistent_store import SupabasePersistentStore
from core.metadata.resource import apply_approved_resource_proposal
import youtube_analytics_connector as connector
from strategist_chat import build_batch_metadata_generator
import core.batch_metadata as batch_metadata

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("youtube-strategist-worker")


def _cipher(secret: str) -> Fernet:
    if len(secret) < 32:
        raise RuntimeError("YOUTUBE_STRATEGIST_STATE_SECRET must be at least 32 characters.")
    digest = hashlib.sha256(("ai-simplified-lab-oauth-token-store:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _get_owner_credentials(store, owner_key: str, cipher: Fernet):
    rows = store._request("GET", "owner_oauth_credentials", params={
        "owner_key": f"eq.{owner_key}", "purpose": "eq.youtube",
        "select": "encrypted_payload,revoked", "limit": "1",
    })
    if not rows or rows[0].get("revoked"):
        raise RuntimeError("YouTube owner OAuth grant is missing or revoked. Reconnect YouTube in the app.")
    try:
        raw = cipher.decrypt(rows[0]["encrypted_payload"].encode("ascii")).decode("utf-8")
        info = json.loads(raw)
        creds = Credentials.from_authorized_user_info(info)
    except (InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Stored YouTube OAuth credentials could not be decrypted.") from exc
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        encrypted = cipher.encrypt(creds.to_json().encode("utf-8")).decode("ascii")
        store._request("PATCH", "owner_oauth_credentials", params={
            "owner_key": f"eq.{owner_key}", "purpose": "eq.youtube",
        }, payload={"encrypted_payload": encrypted, "updated_at": datetime.now(timezone.utc).isoformat()},
           prefer="return=minimal")
    if not creds.valid:
        raise RuntimeError("YouTube OAuth credentials are expired. Reconnect YouTube in the app.")
    return creds


def _record_live_metadata_snapshot(store, owner_key: str, video_id: str, snippet: dict, source: str = "worker") -> dict:
    """Record the exact live title/description/tags and return the cooldown decision."""
    rows = store._request("POST", "rpc/record_video_metadata_snapshot", payload={
        "p_owner_key": owner_key,
        "p_video_id": video_id,
        "p_title": snippet.get("title") or "",
        "p_description": snippet.get("description") or "",
        "p_tags": snippet.get("tags") or [],
        "p_source": source,
    })
    if not rows or not isinstance(rows[0], dict):
        raise RuntimeError("Metadata cooldown ledger returned no decision.")
    return rows[0]


def _snapshot_allows_publish(snapshot: dict) -> bool:
    """Fail closed unless a prior baseline is unchanged and the full hold elapsed."""
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("eligible") is True
        and snapshot.get("is_baseline") is False
        and snapshot.get("changed_since_previous") is False
    )


def _persist_row(store, job_id: str, owner_key: str, row_record: dict, row: dict):
    proposed = row.get("proposed") or {}
    before = row.get("before") or {}
    digest = row.get("digest") or row.get("approval_digest") or ""
    status = row.get("status") or "failed"
    store._request("PATCH", "batch_job_rows", params={
        "id": f"eq.{row_record['id']}", "job_id": f"eq.{job_id}",
        "owner_key": f"eq.{owner_key}",
    }, payload={
        "status": status if status in {
            "ready", "verified", "invalid", "no_change", "failed", "verification_unavailable"
        } else "failed",
        "video_title": row.get("current_title") or before.get("title"),
        "before_metadata": before,
        "proposed_metadata": proposed,
        "proposal_details": {
            "title_angles": row.get("title_angles") or [],
            "recommended_title": row.get("recommended_title") or proposed.get("title", ""),
            "recommendation_reason": row.get("recommendation_reason") or "",
            "rationale": row.get("rationale") or [],
            "changed_fields": row.get("changed_fields") or [],
            "result_message": row.get("result_message") or "",
            "proposed_title": row.get("proposed_title") or proposed.get("title", ""),
        },
        "errors": row.get("errors") or ([row.get("result_message")] if row.get("result_message") else []),
        "approval_digest": digest,
        "attempt_count": int(row_record.get("attempt_count") or 0) + 1,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, prefer="return=minimal")


def process_one_job(store, cipher, groq_key, gemini_key):
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    job = store.claim_next_job(worker_id, lease_seconds=600)
    if not job:
        return False

    job_id = job["id"]
    owner_key = job["owner_key"]
    log.info("Claimed batch job %s", job_id)
    try:
        creds = _get_owner_credentials(store, owner_key, cipher)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        secrets = {
            "groq": {"api_key": groq_key, "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")},
            "gemini": {
                "api_key": gemini_key,
                "batch_model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                "batch_fallback_models": [
                    m.strip()
                    for m in os.getenv(
                        "GEMINI_FALLBACK_MODELS",
                        "gemini-3.5-flash,gemini-3.6-flash",
                    ).split(",")
                    if m.strip()
                ],
            },
        }
        generator = build_batch_metadata_generator(secrets, youtube=youtube)
        records = store._request("GET", "batch_job_rows", params={
            "job_id": f"eq.{job_id}", "owner_key": f"eq.{owner_key}",
            "select": "id,row_number,video_id,video_title,status,input_payload,attempt_count",
            "order": "row_number.asc",
        })
        pending_payloads = [
            record.get("input_payload") or {}
            for record in records
            if record.get("status") in {"queued", "running"}
        ]
        preloaded_snippets, preloaded_fetch_errors = batch_metadata.fetch_owner_verified_snippets(
            youtube, pending_payloads
        )
        log.info(
            "Preloaded YouTube snippets for %s video rows (%s owner-verified, %s rejected)",
            len(pending_payloads),
            len(preloaded_snippets),
            len(preloaded_fetch_errors),
        )

        processed = 0
        failed = 0
        for record in records:
            if record.get("status") not in {"queued", "running"}:
                processed += 1
                if record.get("status") in {"invalid", "failed", "verification_unavailable"}:
                    failed += 1
                continue
            payload = record.get("input_payload") or {}
            one_batch = {"rows": [payload]}
            try:
                prepared = batch_metadata.prepare_metadata_batch(
                    youtube,
                    one_batch,
                    secrets=secrets,
                    ai_generator=generator,
                    preloaded_snippets=preloaded_snippets,
                    preloaded_fetch_errors=preloaded_fetch_errors,
                )
                row = (prepared.get("rows") or [{}])[0]
                _persist_row(store, job_id, owner_key, record, row)
                record["status"] = row.get("status") or "failed"
                if record["status"] in {"invalid", "failed", "verification_unavailable"}:
                    failed += 1
            except Exception as exc:
                failed += 1
                record["status"] = "failed"
                store._request("PATCH", "batch_job_rows", params={
                    "id": f"eq.{record['id']}", "owner_key": f"eq.{owner_key}",
                }, payload={
                    "status": "failed", "errors": [f"{type(exc).__name__}: {str(exc)[:500]}"],
                    "attempt_count": int(record.get("attempt_count") or 0) + 1,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, prefer="return=minimal")
            processed += 1
            store._request("PATCH", "batch_jobs", params={
                "id": f"eq.{job_id}", "owner_key": f"eq.{owner_key}",
            }, payload={
                "processed_count": processed, "failed_count": failed,
                "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, prefer="return=minimal")

        # Re-read statuses before finalizing: the owner may have approved
        # already-ready rows while this worker continued staging later rows.
        final_rows = store._request("GET", "batch_job_rows", params={
            "job_id": f"eq.{job_id}", "owner_key": f"eq.{owner_key}",
            "select": "id,status", "order": "row_number.asc",
        })
        final_statuses = [
            str(item.get("status") or "failed")
            for item in (final_rows or records)
        ]
        pending_count = sum(status in {"queued", "running"} for status in final_statuses)
        ready_count = sum(status == "ready" for status in final_statuses)
        processed = len(final_statuses) - pending_count
        failed = sum(status in {"invalid", "failed", "verification_unavailable"} for status in final_statuses)
        if pending_count:
            final_status = "running"
        elif ready_count:
            final_status = "review"
        elif any(status in {"verified", "no_change"} for status in final_statuses):
            final_status = "completed"
        else:
            final_status = "failed" if failed else "completed"
        store._request("PATCH", "batch_jobs", params={
            "id": f"eq.{job_id}", "owner_key": f"eq.{owner_key}",
        }, payload={
            "status": final_status, "processed_count": processed, "failed_count": failed,
            "worker_id": None, "lease_expires_at": None,
            "finished_at": None if pending_count else datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, prefer="return=minimal")
        log.info("Finished batch job %s: %s (%s/%s)", job_id, final_status, processed, len(final_statuses))
    except Exception as exc:
        log.exception("Batch job %s failed", job_id)
        store._request("PATCH", "batch_jobs", params={
            "id": f"eq.{job_id}", "owner_key": f"eq.{owner_key}",
        }, payload={
            "status": "failed", "last_error": f"{type(exc).__name__}: {str(exc)[:1000]}",
            "worker_id": None, "lease_expires_at": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, prefer="return=minimal")
    return True


def main():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    state_secret = os.environ["YOUTUBE_STRATEGIST_STATE_SECRET"]
    groq_key = os.getenv("GROQ_API_KEY", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    store = SupabasePersistentStore.worker_client(url, key)
    cipher = _cipher(state_secret)
    max_jobs = int(os.getenv("WORKER_MAX_JOBS", "10"))
    idle_seconds = int(os.getenv("WORKER_IDLE_SECONDS", "15"))
    processed = 0
    while processed < max_jobs:
        if process_one_job(store, cipher, groq_key, gemini_key):
            processed += 1
        else:
            if processed:
                break
            time.sleep(idle_seconds)
            # One idle poll is enough for scheduled Actions invocations.
            break

    # Discord approvals enter the same durable metadata audit queue as Streamlit.
    # The background worker remains the only component that performs YouTube writes.
    try:
        approved_count = process_approved_metadata_editor_audits(store, cipher, limit=max_jobs)
        if approved_count:
            log.info("Claimed %s approved metadata editor proposal(s).", approved_count)
    except Exception:
        log.exception("Could not process approved metadata editor proposals.")

    try:
        strategy_count = process_discord_approved_strategy_actions(store, cipher, limit=max_jobs)
        if strategy_count:
            log.info("Claimed %s Discord-approved channel strategy proposal(s).", strategy_count)
    except Exception:
        log.exception("Could not process Discord-approved channel strategy proposals.")


def process_discord_approved_strategy_actions(store, cipher, limit: int = 10) -> int:
    """Publish only strategy actions explicitly approved through the Discord interaction."""
    rows = store._request("GET", "channel_strategy_actions", params={
        "status": "eq.approved",
        "result->>source": "eq.discord",
        "select": "id,owner_key,video_id,video_title,before_metadata,proposed_metadata,approval_digest",
        "order": "created_at.asc",
        "limit": str(max(1, min(int(limit), 25))),
    })
    processed = 0
    for action in rows or []:
        action_id = str(action.get("id") or "")
        owner_key = str(action.get("owner_key") or "")
        if not action_id or not owner_key:
            continue
        # Compare-and-set claim prevents two scheduled workers from publishing the same action.
        claimed = store._request("PATCH", "channel_strategy_actions", params={
            "id": f"eq.{action_id}", "owner_key": f"eq.{owner_key}",
            "status": "eq.approved", "result->>source": "eq.discord",
        }, payload={"status": "auto_publishing", "updated_at": datetime.now(timezone.utc).isoformat()},
           prefer="return=representation")
        if not claimed:
            continue
        processed += 1
        try:
            credentials = _get_owner_credentials(store, owner_key, cipher)
            youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
            result = connector.apply_approved_dashboard_proposal(
                youtube,
                str(action.get("video_id") or ""),
                action.get("before_metadata") or {},
                action.get("proposed_metadata") or {},
                str(action.get("approval_digest") or ""),
                audit_callback=lambda entry: store._request(
                    "PATCH", "channel_strategy_actions",
                    params={"id": f"eq.{action_id}", "owner_key": f"eq.{owner_key}",
                            "status": "eq.auto_publishing"},
                    payload={"audit_log": entry, "updated_at": datetime.now(timezone.utc).isoformat()},
                    prefer="return=minimal",
                ),
            )
            if not isinstance(result, dict) or result.get("result") != "success":
                raise RuntimeError("YouTube did not confirm the approved strategy metadata update.")
            store._request("PATCH", "channel_strategy_actions", params={
                "id": f"eq.{action_id}", "owner_key": f"eq.{owner_key}",
                "status": "eq.auto_publishing",
            }, payload={
                "status": "approved_published",
                "after_metadata": result.get("resource") or {},
                "result": {"source": "discord", "action": "approve",
                           "video_id": action.get("video_id"),
                           "url": f"https://www.youtube.com/watch?v={action.get('video_id')}",
                           "updated_parts": result.get("updated_parts") or [],
                           "verified": True},
                "error": None,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, prefer="return=minimal")
        except Exception as exc:
            if isinstance(exc, connector.MetadataVerificationUnavailableError):
                status = "verification_unavailable"
            elif isinstance(exc, RuntimeError) and "changed after review" in str(exc).casefold():
                status = "skipped"
            else:
                status = "failed"
            store._request("PATCH", "channel_strategy_actions", params={
                "id": f"eq.{action_id}", "owner_key": f"eq.{owner_key}",
                "status": "eq.auto_publishing",
            }, payload={
                "status": status,
                "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
                "result": {"source": "discord", "action": "approve", "verified": False},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, prefer="return=minimal")
            log.exception("Discord-approved strategy action %s ended with status %s", action_id, status)
    return processed


def process_approved_metadata_editor_audits(store, cipher, limit: int = 10) -> int:
    """Publish only Discord-approved metadata-editor audits."""
    rows = store._request("GET", "metadata_editor_audits", params={
        "status": "eq.approved",
        "result->>source": "eq.discord",
        "select": "id,owner_key,video_id,video_title,before_metadata,proposed_metadata,approval_digest,result",
        "order": "created_at.asc",
        "limit": str(max(1, min(int(limit), 25))),
    })
    processed = 0
    for audit in rows or []:
        audit_id = str(audit.get("id") or "")
        owner_key = str(audit.get("owner_key") or "")
        if not audit_id or not owner_key:
            continue
        store.owner_key = owner_key
        if not store.claim_metadata_editor_audit(audit_id):
            continue
        processed += 1
        try:
            credentials = _get_owner_credentials(store, owner_key, cipher)
            youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
            result = apply_approved_resource_proposal(
                youtube,
                str(audit.get("video_id") or ""),
                audit.get("before_metadata") or {},
                audit.get("proposed_metadata") or {},
                str(audit.get("approval_digest") or ""),
            )
            if result.get("result") != "success":
                store.update_metadata_editor_audit(audit_id, {
                    "status": "failed",
                    "result": result,
                    "error": "The approved proposal contained no metadata changes.",
                })
                continue
            store.update_metadata_editor_audit(audit_id, {
                "status": "approved_published",
                "after_metadata": result.get("resource") or {},
                "result": {
                    "result": "success",
                    "source": "discord",
                    "updated_parts": result.get("updated_parts") or [],
                    "video_id": audit.get("video_id"),
                    "url": f"https://www.youtube.com/watch?v={audit.get('video_id')}",
                },
                "error": None,
                "published_at": datetime.now(timezone.utc).isoformat(),
            })
            log.info("Published Discord-approved metadata audit %s", audit_id)
        except Exception as exc:
            if isinstance(exc, connector.MetadataVerificationUnavailableError):
                status = "verification_unavailable"
            elif isinstance(exc, RuntimeError) and "changed after review" in str(exc).casefold():
                status = "stale"
            else:
                status = "failed"
            store.update_metadata_editor_audit(audit_id, {
                "status": status,
                "error": f"{type(exc).__name__}: {str(exc)[:1200]}",
                "result": {"source": "discord", "worker": "background-worker"},
            })
            log.exception("Discord-approved metadata audit %s ended with status %s", audit_id, status)
    return processed



if __name__ == "__main__":
    main()