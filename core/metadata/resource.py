"""Full YouTube video metadata resource snapshots and approval-gated updates.

This module extends the existing title/description/tags publisher without changing
its established call sites. It deliberately supports only documented writable
fields and sends only the resource parts that actually changed.
"""
from __future__ import annotations

import copy
import hmac
import re
from datetime import datetime
from typing import Any

import youtube_analytics_connector as connector


SNIPPET_FIELDS = {
    "title", "description", "tags", "categoryId",
    "defaultLanguage", "defaultAudioLanguage",
}
STATUS_FIELDS = {
    "privacyStatus", "license", "embeddable", "publicStatsViewable",
    "selfDeclaredMadeForKids", "containsSyntheticMedia", "publishAt",
}
RECORDING_DETAILS_FIELDS = {"recordingDate"}
LOCALIZATION_FIELDS = {"title", "description"}
SUPPORTED_PARTS = ("snippet", "status", "recordingDetails", "localizations")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def _pick(source: dict, fields: set[str]) -> dict:
    return {key: copy.deepcopy(source[key]) for key in fields if key in source}


def normalize_video_resource(resource: dict) -> dict:
    """Return a stable, writable-only snapshot, excluding read-only API fields."""
    if not isinstance(resource, dict):
        raise connector.MetadataValidationError("YouTube returned an invalid video resource.")
    snapshot = {}
    for part, fields in (
        ("snippet", SNIPPET_FIELDS),
        ("status", STATUS_FIELDS),
        ("recordingDetails", RECORDING_DETAILS_FIELDS),
    ):
        value = resource.get(part)
        if isinstance(value, dict):
            snapshot[part] = _pick(value, fields)
    localizations = resource.get("localizations")
    if isinstance(localizations, dict):
        snapshot["localizations"] = {
            str(language): _pick(value, LOCALIZATION_FIELDS)
            for language, value in localizations.items()
            if isinstance(value, dict)
        }
    return snapshot


def fetch_video_resource(youtube, video_id: str) -> dict:
    """Fetch owner-editable metadata parts and verify the video belongs to this channel."""
    if youtube is None:
        raise RuntimeError("YouTube API client is not connected.")
    response = youtube.videos().list(
        part="snippet,status,recordingDetails,localizations",
        id=video_id,
    ).execute()
    items = response.get("items") or []
    if not items or items[0].get("id") != video_id:
        raise connector.VideoNotFoundError(f"Video not found or inaccessible: {video_id}")
    snippet = items[0].get("snippet") or {}
    if snippet.get("channelId") != connector.CHANNEL_ID:
        raise connector.VideoNotFoundError(
            f"Video {video_id} is not owned by the configured channel."
        )
    return normalize_video_resource(items[0])


def _validate_resource(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        raise connector.MetadataValidationError("Metadata resource must be an object.")
    unexpected_parts = set(snapshot) - set(SUPPORTED_PARTS)
    if unexpected_parts:
        raise connector.MetadataValidationError(
            "Unsupported metadata parts: " + ", ".join(sorted(unexpected_parts))
        )

    snippet = snapshot.get("snippet")
    if snippet is not None:
        if not isinstance(snippet, dict):
            raise connector.MetadataValidationError("snippet must be an object.")
        unexpected = set(snippet) - SNIPPET_FIELDS
        if unexpected:
            raise connector.MetadataValidationError(
                "Unsupported snippet fields: " + ", ".join(sorted(unexpected))
            )
        # videos.update requires both title and categoryId whenever snippet is updated.
        if not str(snippet.get("title") or "").strip():
            raise connector.MetadataValidationError("Title is required when updating snippet metadata.")
        if not str(snippet.get("categoryId") or "").strip():
            raise connector.MetadataValidationError("Category is required when updating snippet metadata.")
        connector._validate_metadata({
            "title": snippet.get("title"),
            "description": snippet.get("description", ""),
            "tags": snippet.get("tags", []),
        })
        for key in ("defaultLanguage", "defaultAudioLanguage"):
            value = snippet.get(key)
            if value and not LANGUAGE_RE.fullmatch(str(value)):
                raise connector.MetadataValidationError(f"{key} must be a valid language code.")
    status = snapshot.get("status")
    if status is not None:
        if not isinstance(status, dict):
            raise connector.MetadataValidationError("status must be an object.")
        unexpected = set(status) - STATUS_FIELDS
        if unexpected:
            raise connector.MetadataValidationError(
                "Unsupported status fields: " + ", ".join(sorted(unexpected))
            )
        if status.get("privacyStatus") not in (None, "public", "private", "unlisted"):
            raise connector.MetadataValidationError("privacyStatus must be public, private or unlisted.")
        if status.get("license") not in (None, "youtube", "creativeCommon"):
            raise connector.MetadataValidationError("license must be youtube or creativeCommon.")
        for key in ("embeddable", "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia"):
            if key in status and not isinstance(status[key], bool):
                raise connector.MetadataValidationError(f"{key} must be a boolean.")
        publish_at = status.get("publishAt")
        if publish_at:
            try:
                datetime.fromisoformat(str(publish_at).replace("Z", "+00:00"))
            except ValueError as error:
                raise connector.MetadataValidationError("publishAt must be an ISO-8601 timestamp.") from error
            if status.get("privacyStatus") != "private":
                raise connector.MetadataValidationError("A scheduled publish time requires private privacyStatus.")
    recording = snapshot.get("recordingDetails")
    if recording is not None:
        if not isinstance(recording, dict) or set(recording) - RECORDING_DETAILS_FIELDS:
            raise connector.MetadataValidationError("Only recordingDate is supported in recordingDetails.")
        if recording.get("recordingDate"):
            try:
                datetime.fromisoformat(str(recording["recordingDate"]).replace("Z", "+00:00"))
            except ValueError as error:
                raise connector.MetadataValidationError("recordingDate must be an ISO-8601 timestamp.") from error
    localizations = snapshot.get("localizations")
    if localizations is not None:
        if not isinstance(localizations, dict):
            raise connector.MetadataValidationError("localizations must be an object keyed by language code.")
        default_language = (snippet or {}).get("defaultLanguage")
        if localizations and not default_language:
            raise connector.MetadataValidationError(
                "Set the video's defaultLanguage before adding localized metadata."
            )
        for language, values in localizations.items():
            if not LANGUAGE_RE.fullmatch(str(language)):
                raise connector.MetadataValidationError(f"Invalid localization language code: {language}")
            if not isinstance(values, dict) or set(values) - LOCALIZATION_FIELDS:
                raise connector.MetadataValidationError(f"Invalid localization values for {language}.")
            title = values.get("title")
            description = values.get("description", "")
            if title is not None and (not isinstance(title, str) or len(title) > 100 or "<" in title or ">" in title):
                raise connector.MetadataValidationError(f"Localized title for {language} must be at most 100 valid characters.")
            if not isinstance(description, str) or len(description.encode("utf-8")) > 5000 or "<" in description or ">" in description:
                raise connector.MetadataValidationError(f"Localized description for {language} exceeds YouTube's supported limit.")


def _changed_parts(before: dict, proposed: dict) -> list[str]:
    return [
        part for part in SUPPORTED_PARTS
        if part in proposed and proposed.get(part) != before.get(part)
    ]


def _write_part(part: str, before: dict, proposed: dict) -> dict:
    value = proposed.get(part)
    if not isinstance(value, dict):
        raise connector.MetadataValidationError(f"Proposed {part} metadata must be an object.")
    allowed = {
        "snippet": SNIPPET_FIELDS,
        "status": STATUS_FIELDS,
        "recordingDetails": RECORDING_DETAILS_FIELDS,
        "localizations": set(),
    }[part]
    if part == "localizations":
        return copy.deepcopy(value)
    # The proposal is a complete writable snapshot for this part. Omitted keys
    # are intentionally omitted, allowing supported optional values to be cleared.
    return _pick(value, allowed)


def apply_approved_resource_proposal(
    youtube,
    video_id: str,
    before_resource: dict,
    proposed_resource: dict,
    approved_digest: str,
    *,
    audit_callback=None,
) -> dict:
    """Apply an exact approved multi-part proposal with live stale-check and read-back."""
    if youtube is None:
        raise RuntimeError("YouTube API client is not connected.")
    before = normalize_video_resource(before_resource)
    proposed = normalize_video_resource(proposed_resource)
    expected_digest = connector.metadata_proposal_digest(video_id, before, proposed)
    if not isinstance(approved_digest, str) or not hmac.compare_digest(expected_digest, approved_digest):
        raise connector.MetadataValidationError("The approval does not match this exact metadata proposal.")

    merged_proposal = copy.deepcopy(before)
    for part, value in proposed.items():
        merged_proposal[part] = copy.deepcopy(value)
    _validate_resource(merged_proposal)
    parts = _changed_parts(before, proposed)
    if not parts:
        return {"result": "no changes", "video_id": video_id, "updated_parts": []}

    current = fetch_video_resource(youtube, video_id)
    if current != before:
        raise RuntimeError("Video metadata changed after review. Reload the video and approve a new proposal.")

    body = {"id": video_id}
    for part in parts:
        body[part] = _write_part(part, before, proposed)

    try:
        youtube.videos().update(part=",".join(parts), body=body).execute()
    except Exception as error:
        if audit_callback:
            audit_callback({
                "video_id": video_id,
                "before": before,
                "proposed": proposed,
                "updated_parts": parts,
                "result": "update_failed",
                "error": f"{type(error).__name__}: {str(error)[:1000]}",
            })
        raise

    try:
        verified = fetch_video_resource(youtube, video_id)
    except Exception as error:
        raise connector.MetadataVerificationUnavailableError(
            f"The metadata update for {video_id} may have succeeded, but read-back verification was unavailable: {error}"
        ) from error
    for part in parts:
        expected_part = proposed.get(part, {})
        actual_part = verified.get(part, {})
        if part == "localizations":
            matches = actual_part == expected_part
        else:
            matches = all(actual_part.get(key) == value for key, value in expected_part.items())
            matches = matches and not (set(before.get(part, {})) - set(expected_part)).intersection(actual_part)
        if not matches:
            raise connector.MetadataVerificationUnavailableError(
                f"YouTube did not return the exact approved {part} metadata after update. Reload before retrying."
            )

    audit = {
        "video_id": video_id,
        "before": before,
        "proposed": proposed,
        "after": verified,
        "updated_parts": parts,
        "result": "success",
    }
    if audit_callback:
        audit_callback(copy.deepcopy(audit))
    return {
        "result": "success",
        "video_id": video_id,
        "updated_parts": parts,
        "resource": verified,
    }
