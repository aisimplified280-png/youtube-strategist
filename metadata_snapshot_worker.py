"""Daily owner-authorized scan of public uploads to maintain live metadata cooldowns."""
from __future__ import annotations

import os
import logging

from googleapiclient.discovery import build

import youtube_analytics_connector as connector
from core.persistent_store import SupabasePersistentStore
from worker import _cipher, _get_owner_credentials, _record_live_metadata_snapshot

log = logging.getLogger("youtube-metadata-snapshot-worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

PAGE_SIZE = 50


def list_upload_video_ids(youtube, channel_id: str) -> list[str]:
    channel_response = youtube.channels().list(
        part="contentDetails", id=channel_id
    ).execute()
    channels = channel_response.get("items") or []
    if not channels or channels[0].get("id") != channel_id:
        raise RuntimeError("The connected YouTube account does not resolve to the configured channel.")
    uploads_playlist = (
        channels[0].get("contentDetails", {})
        .get("relatedPlaylists", {}).get("uploads")
    )
    if not uploads_playlist:
        raise RuntimeError("YouTube did not return the channel uploads playlist.")

    video_ids: list[str] = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads_playlist,
            maxResults=PAGE_SIZE, pageToken=page_token,
        ).execute()
        for item in response.get("items") or []:
            video_id = (item.get("contentDetails") or {}).get("videoId")
            if video_id:
                video_ids.append(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return list(dict.fromkeys(video_ids))


def snapshot_public_uploads(youtube, store, owner_key: str, channel_id: str) -> dict:
    video_ids = list_upload_video_ids(youtube, channel_id)
    snapshotted = 0
    non_public = 0
    wrong_channel = 0
    missing = 0
    errors: list[str] = []

    for start in range(0, len(video_ids), PAGE_SIZE):
        chunk = video_ids[start:start + PAGE_SIZE]
        try:
            response = youtube.videos().list(
                part="snippet,status", id=",".join(chunk)
            ).execute()
        except Exception as exc:
            errors.append(f"video batch {start // PAGE_SIZE + 1}: {type(exc).__name__}: {str(exc)[:300]}")
            continue

        returned = {item.get("id"): item for item in (response.get("items") or [])}
        missing += len(set(chunk) - set(returned))
        for video_id in chunk:
            item = returned.get(video_id)
            if not item:
                continue
            snippet = item.get("snippet") or {}
            status = item.get("status") or {}
            if snippet.get("channelId") != channel_id:
                wrong_channel += 1
                continue
            if status.get("privacyStatus") != "public":
                non_public += 1
                continue
            try:
                decision = _record_live_metadata_snapshot(
                    store, owner_key, video_id, snippet, source="worker"
                )
                if decision.get("video_id") != video_id:
                    raise RuntimeError("Cooldown ledger returned a mismatched video ID.")
                snapshotted += 1
            except Exception as exc:
                errors.append(f"{video_id}: {type(exc).__name__}: {str(exc)[:300]}")

    summary = {
        "catalogue_ids": len(video_ids),
        "snapshotted_public": snapshotted,
        "non_public": non_public,
        "wrong_channel": wrong_channel,
        "missing_or_unavailable": missing,
        "errors": errors,
    }
    log.info("Daily metadata snapshot summary: %s", summary)
    if errors:
        raise RuntimeError(
            f"Metadata snapshot scan incomplete: {len(errors)} errors; "
            f"{snapshotted} public videos snapshotted."
        )
    return summary


def main() -> None:
    url = os.environ["SUPABASE_URL"]
    service_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    state_secret = os.environ["YOUTUBE_STRATEGIST_STATE_SECRET"]
    owner_key = os.environ["YOUTUBE_OWNER_KEY"]
    store = SupabasePersistentStore.worker_client(url, service_key)
    cipher = _cipher(state_secret)
    credentials = _get_owner_credentials(store, owner_key, cipher)
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    snapshot_public_uploads(youtube, store, owner_key, connector.CHANNEL_ID)


if __name__ == "__main__":
    main()
