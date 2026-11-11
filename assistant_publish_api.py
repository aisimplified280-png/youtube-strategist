"""Private API bridge for direct ChatGPT and autonomous-monitor publishing.

Run separately from Streamlit (for example, as a small private API service).
Only this server holds the service-role key and YouTube OAuth decryption secret.
"""
from __future__ import annotations

import hmac
import os
import re
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from googleapiclient.discovery import build

import youtube_analytics_connector as connector
from core.persistent_store import SupabasePersistentStore
from worker import _cipher, _get_owner_credentials

app = FastAPI(title="AI Simplified Lab Publishing API", version="1.0.0")


class PublishBody(BaseModel):
    proposed_metadata: dict[str, Any] = Field(min_length=1)
    publish_confirmed: bool = True
    idempotency_key: str | None = Field(default=None, max_length=160)


class MonitorPolicyBody(BaseModel):
    enabled: bool
    max_daily_publishes: int = Field(default=5, ge=1, le=25)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=503, detail=f"Server configuration missing: {name}")
    return value


def _authorize(token: str | None, env_name: str):
    expected = _required_env(env_name)
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid API credentials.")


def _store() -> SupabasePersistentStore:
    return SupabasePersistentStore(
        _required_env("SUPABASE_URL"),
        _required_env("SUPABASE_SERVICE_ROLE_KEY"),
        _required_env("YOUTUBE_STRATEGIST_OWNER_SUBJECT"),
    )


def _youtube(store: SupabasePersistentStore):
    cipher = _cipher(_required_env("YOUTUBE_STRATEGIST_STATE_SECRET"))
    credentials = _get_owner_credentials(store, store.owner_key, cipher)
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    channels = youtube.channels().list(part="id", mine=True).execute()
    owned = {item.get("id") for item in (channels.get("items") or [])}
    if connector.CHANNEL_ID not in owned:
        raise HTTPException(status_code=403, detail="Connected YouTube account does not own the configured channel.")
    return youtube


def _video_before(youtube, video_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        raise HTTPException(status_code=422, detail="Invalid YouTube video ID.")
    response = youtube.videos().list(part="snippet", id=video_id).execute()
    items = response.get("items") or []
    if len(items) != 1 or items[0].get("id") != video_id:
        raise HTTPException(status_code=404, detail="Video not found or unavailable.")
    snippet = items[0].get("snippet") or {}
    if snippet.get("channelId") != connector.CHANNEL_ID:
        raise HTTPException(status_code=403, detail="Only videos owned by the configured channel can be changed.")
    return {
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "tags": snippet.get("tags") or [],
        "categoryId": snippet.get("categoryId"),
        "channelId": snippet.get("channelId"),
    }


def _validate_patch(before: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    if not patch or set(patch) - {"title", "description", "tags"}:
        raise HTTPException(status_code=422, detail="Only title, description, and tags may be changed.")
    proposed = dict(before)
    proposed.update(patch)
    try:
        connector._validate_metadata(proposed)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return patch


@app.get("/health")
def health():
    return {"ok": True, "service": "ai-simplified-lab-publishing"}


@app.get("/v1/videos/{video_id}")
def read_video(video_id: str, authorization: str | None = Header(default=None)):
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    _authorize(token, "CHATGPT_PUBLISH_API_TOKEN")
    store = _store()
    youtube = _youtube(store)
    return {"video_id": video_id, "metadata": _video_before(youtube, video_id)}


@app.post("/v1/videos/{video_id}/publish")
def chatgpt_publish(video_id: str, body: PublishBody,
                    authorization: str | None = Header(default=None)):
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    _authorize(token, "CHATGPT_PUBLISH_API_TOKEN")
    if body.publish_confirmed is not True:
        raise HTTPException(status_code=409, detail="Explicit user publish confirmation is required.")
    store = _store()
    youtube = _youtube(store)
    before = _video_before(youtube, video_id)
    patch = _validate_patch(before, body.proposed_metadata)
    try:
        queued = store.enqueue_chat_publish_request(
            video_id=video_id, before_metadata=before, proposed_metadata=patch,
            publish_confirmed=True, authorized_by="chatgpt_explicit_user_command",
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"queued": True, "request_id": queued.get("id"), "source": "chatgpt",
            "message": "Queued for backend publishing and YouTube verification."}


@app.post("/v1/monitor/videos/{video_id}/publish")
def monitor_publish(video_id: str, body: PublishBody,
                    x_monitor_token: str | None = Header(default=None)):
    _authorize(x_monitor_token, "MONITOR_PUBLISH_API_TOKEN")
    store = _store()
    youtube = _youtube(store)
    before = _video_before(youtube, video_id)
    patch = _validate_patch(before, body.proposed_metadata)
    try:
        queued = store.enqueue_monitor_publish_request(
            video_id=video_id, before_metadata=before, proposed_metadata=patch,
            authorized_by="authorized_monitor", idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"queued": True, "request_id": queued.get("id"), "source": "monitor",
            "message": "Queued under the owner's autonomous monitor policy."}


@app.get("/v1/publish-requests/{request_id}")
def publish_status(request_id: str, authorization: str | None = Header(default=None)):
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    _authorize(token, "CHATGPT_PUBLISH_API_TOKEN")
    result = _store().get_publish_request(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="Publishing request not found.")
    return result


@app.put("/v1/monitor/policy")
def set_monitor_policy(body: MonitorPolicyBody,
                       authorization: str | None = Header(default=None)):
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    _authorize(token, "CHATGPT_PUBLISH_API_TOKEN")
    result = _store().set_monitor_publish_policy(
        enabled=body.enabled, max_daily_publishes=body.max_daily_publishes,
    )
    return {"monitor_auto_publish_enabled": body.enabled,
            "max_daily_publishes": body.max_daily_publishes,
            "saved": bool(result)}
