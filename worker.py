"""Independent worker for durable metadata proposal jobs.

Required environment:
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, YOUTUBE_STRATEGIST_STATE_SECRET,
GROQ_API_KEY (preferred), GEMINI_API_KEY (fallback).
This worker ONLY prepares proposals. It never publishes metadata.
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
            "ready", "invalid", "no_change", "failed", "verification_unavailable"
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
            "groq": {"api_key": groq_key, "model": "openai/gpt-oss-120b"},
            "gemini": {"api_key": gemini_key, "model": "gemini-2.5-flash"},
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
                store._request("PATCH", "batch_job_rows", params={
                    "id": f"eq.{record['id']}", "job_id": f"eq.{job_id}",
                    "owner_key": f"eq.{owner_key}",
                }, payload={
                    "status": "running",
                    "attempt_count": int(record.get("attempt_count") or 0) + 1,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, prefer="return=minimal")
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

        final_status = "review" if any(
            record.get("status") == "ready" for record in records
        ) else ("failed" if failed else "completed")
        store._request("PATCH", "batch_jobs", params={
            "id": f"eq.{job_id}", "owner_key": f"eq.{owner_key}",
        }, payload={
            "status": final_status, "processed_count": processed, "failed_count": failed,
            "worker_id": None, "lease_expires_at": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, prefer="return=minimal")
        log.info("Finished batch job %s: %s (%s/%s)", job_id, final_status, processed, len(records))
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
    if not groq_key and not gemini_key:
        raise RuntimeError("Configure GROQ_API_KEY or GEMINI_API_KEY for metadata generation.")
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


if __name__ == "__main__":
    main()
