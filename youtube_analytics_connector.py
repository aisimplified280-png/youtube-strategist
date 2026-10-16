
import argparse
import csv
import difflib
import hashlib
import hmac
import json
import re
import sys
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse

import httplib2
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PRIVATE_DIR = Path.home() / ".ai-simplified-youtube"
CLIENT_FILE = PRIVATE_DIR / "client_secret.json"
TOKEN_FILE = PRIVATE_DIR / "token.json"
AUDIT_LOG_FILE = PRIVATE_DIR / "metadata_audit.jsonl"
PENDING_PROPOSALS_DIR = PRIVATE_DIR / "pending_proposals"
CHANNEL_ID = "UCS6Sb5rfaEnWTVPGyQlnHFQ"
ANALYTICS_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,likes,comments,"
    "shares,subscribersGained,subscribersLost"
)
YOUTUBE_MAX_SHORT_DURATION_SECONDS = 180
VERIFICATION_RETRY_DELAYS_SECONDS = (1, 2, 4, 8)

OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "youtube_analytics"
SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    # Used only for the app-owned Strategist Memory spreadsheet.
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class MetadataValidationError(ValueError):
    """Raised when a proposed video metadata change is unsafe or invalid."""


class VideoNotFoundError(LookupError):
    """Raised when the YouTube API does not return the requested video."""


def authorize():
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    if not CLIENT_FILE.exists():
        raise FileNotFoundError(
            f"Put your downloaded OAuth JSON here: {CLIENT_FILE}"
        )

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))

    if creds and creds.has_scopes(SCOPES):
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                if creds.valid:
                    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
                    return creds
            except RefreshError:
                print(
                    "The saved Google authorization could not be refreshed. "
                    "Starting the normal browser sign-in flow.",
                    file=sys.stderr,
                )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_FILE), SCOPES
    )
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return creds


def fetch_video_snippet(youtube, video_id):
    response = youtube.videos().list(
        part="snippet",
        id=video_id,
    ).execute()
    items = response.get("items", [])
    if not items:
        raise VideoNotFoundError(f"Video not found or inaccessible: {video_id}")

    item = items[0]
    snippet = item.get("snippet")
    if item.get("id") != video_id or not isinstance(snippet, dict):
        raise VideoNotFoundError(f"Video snippet unavailable: {video_id}")
    if snippet.get("channelId") != CHANNEL_ID:
        raise VideoNotFoundError(
            f"Video {video_id} is not owned by the configured channel."
        )
    if not snippet.get("categoryId"):
        raise MetadataValidationError(
            f"Video {video_id} has no categoryId; refusing to update its snippet."
        )
    return snippet


def _parse_video_reference(reference):
    value = reference.strip()
    if not value:
        raise ValueError("Provide a YouTube video URL, video ID, or exact title.")

    url_value = value
    if re.match(r"^(?:www\.)?(?:youtube\.com|youtu\.be)/", value, flags=re.I):
        url_value = "https://" + value
    parsed = urlparse(url_value)
    if parsed.scheme or parsed.netloc:
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            raise ValueError("Only youtube.com and youtu.be video references are supported.")
        if hostname == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
        elif parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            path_parts = [part for part in parsed.path.split("/") if part]
            video_id = (
                path_parts[1]
                if len(path_parts) > 1
                and path_parts[0] in {"shorts", "embed", "live"}
                else ""
            )
        if not video_id:
            raise ValueError("The YouTube URL does not contain a video ID.")
        return video_id

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    return None


def _channel_uploads_playlist(youtube):
    response = youtube.channels().list(
        part="snippet,contentDetails",
        id=CHANNEL_ID,
    ).execute()
    items = response.get("items", [])
    if not items:
        raise VideoNotFoundError(
            f"Configured channel {CHANNEL_ID} is not available to this account."
        )
    channel = items[0]
    if channel.get("id") != CHANNEL_ID:
        raise RuntimeError("YouTube returned a channel that does not match the configured ID.")
    return channel["contentDetails"]["relatedPlaylists"]["uploads"]


def _list_recent_upload_ids(youtube, limit):
    playlist_id = _channel_uploads_playlist(youtube)
    video_ids = []
    page_token = None
    while len(video_ids) < limit:
        response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=min(50, limit - len(video_ids)),
            pageToken=page_token,
        ).execute()
        video_ids.extend(
            item["contentDetails"]["videoId"]
            for item in response.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return video_ids[:limit]


def _parse_duration_seconds(duration):
    match = re.fullmatch(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        duration,
    )
    if not match:
        raise ValueError(f"Unrecognized YouTube video duration: {duration}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _fetch_video_details(youtube, video_ids):
    videos = {}
    for index in range(0, len(video_ids), 50):
        batch = video_ids[index:index + 50]
        response = youtube.videos().list(
            part="snippet,contentDetails,statistics",
            id=",".join(batch),
        ).execute()
        for item in response.get("items", []):
            if item.get("snippet", {}).get("channelId") != CHANNEL_ID:
                continue
            videos[item["id"]] = item
    return videos


def _format_assessment(video):
    duration_seconds = _parse_duration_seconds(
        video.get("contentDetails", {}).get("duration", "")
    )
    if duration_seconds > YOUTUBE_MAX_SHORT_DURATION_SECONDS:
        return {
            "duration_seconds": duration_seconds,
            "format": "long-form by duration",
            "short_classification": "not a Short by the current 3-minute duration limit",
            "orientation": "unavailable from the YouTube Data API",
        }
    return {
        "duration_seconds": duration_seconds,
        "format": "undetermined",
        "short_classification": (
            "Short candidate by duration only; the YouTube Data API does not expose "
            "the Shorts designation or video orientation"
        ),
        "orientation": "unavailable from the YouTube Data API",
    }


def _resolve_video(youtube, reference):
    video_id = _parse_video_reference(reference)
    if video_id:
        details = _fetch_video_details(youtube, [video_id])
        if video_id not in details:
            raise VideoNotFoundError(
                f"Video {video_id} is unavailable or is not on channel {CHANNEL_ID}."
            )
        return details[video_id]

    recent_ids = _list_recent_upload_ids(youtube, 200)
    recent_videos = _fetch_video_details(youtube, recent_ids)
    normalized = reference.strip().casefold()
    exact_matches = [
        item for item in recent_videos.values()
        if item.get("snippet", {}).get("title", "").casefold() == normalized
    ]
    matches = exact_matches or [
        item for item in recent_videos.values()
        if normalized in item.get("snippet", {}).get("title", "").casefold()
    ]
    if not matches:
        response = youtube.search().list(
            part="id,snippet",
            channelId=CHANNEL_ID,
            type="video",
            q=reference,
            maxResults=10,
        ).execute()
        search_ids = [
            item.get("id", {}).get("videoId")
            for item in response.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        searched = _fetch_video_details(youtube, search_ids)
        exact_matches = [
            item for item in searched.values()
            if item.get("snippet", {}).get("title", "").casefold() == normalized
        ]
        matches = exact_matches or [
            item for item in searched.values()
            if normalized in item.get("snippet", {}).get("title", "").casefold()
        ]
    if len(matches) != 1:
        if matches:
            choices = "; ".join(
                f"{item['id']}: {item['snippet'].get('title', '')}"
                for item in matches[:10]
            )
            raise ValueError(f"Title is ambiguous; matching videos: {choices}")
        raise VideoNotFoundError(
            f"No channel video matched that title within the 200 most recent uploads."
        )
    return matches[0]


def _query_analytics(analytics, start_date, end_date, metrics, *, video_id=None,
                     dimensions=None, max_results=200):
    params = {
        "ids": "channel==MINE",
        "startDate": start_date,
        "endDate": end_date,
        "metrics": metrics,
        "maxResults": max_results,
    }
    if video_id:
        params["filters"] = f"video=={video_id}"
    if dimensions:
        params["dimensions"] = dimensions
    response = analytics.reports().query(**params).execute()
    headers = [column["name"] for column in response.get("columnHeaders", [])]
    rows = [
        dict(zip(headers, row))
        for row in response.get("rows", [])
    ]
    return rows


def _try_query_analytics(analytics, start_date, end_date, metrics, **kwargs):
    try:
        return _query_analytics(
            analytics,
            start_date,
            end_date,
            metrics,
            **kwargs,
        ), None
    except HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        return [], (
            f"Unavailable: YouTube Analytics API rejected this report "
            f"(HTTP {status}): {error.reason}"
        )


def _analytics_window(video):
    yesterday = date.today() - timedelta(days=1)
    published_at = video.get("snippet", {}).get("publishedAt")
    published_on = date.fromisoformat(published_at[:10]) if published_at else yesterday
    return max(yesterday - timedelta(days=89), published_on), yesterday


def _cached_video_ids():
    video_ids = []
    for filename in (
        "video_performance_with_titles.csv",
        "video_performance.csv",
    ):
        export_file = OUTPUT_DIR / filename
        if not export_file.is_file():
            continue
        with export_file.open(newline="", encoding="utf-8-sig") as csv_file:
            for row in csv.DictReader(csv_file):
                video_id = row.get("video", "")
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                    video_ids.append(video_id)
    return video_ids


def _collect_video_analysis(youtube, analytics, video):
    video_id = video["id"]
    snippet = video["snippet"]
    period_start, period_end = _analytics_window(video)
    start = period_start.isoformat()
    end = period_end.isoformat()
    performance_rows = _query_analytics(
        analytics,
        start,
        end,
        ANALYTICS_METRICS,
        video_id=video_id,
    )
    traffic_rows, traffic_error = _try_query_analytics(
        analytics,
        start,
        end,
        "views,estimatedMinutesWatched",
        video_id=video_id,
        dimensions="insightTrafficSourceType",
    )
    audience_rows, audience_error = _try_query_analytics(
        analytics,
        start,
        end,
        "viewerPercentage",
        video_id=video_id,
        dimensions="ageGroup,gender",
    )
    target_format = _format_assessment(video)
    target_age_days = max(
        1,
        (period_end - date.fromisoformat(snippet["publishedAt"][:10])).days,
    )
    candidate_ids = list(dict.fromkeys(
        _list_recent_upload_ids(youtube, 200) + _cached_video_ids()
    ))
    candidate_ids = [
        candidate_id for candidate_id in candidate_ids
        if candidate_id != video_id
    ]
    peer_videos = _fetch_video_details(youtube, candidate_ids)
    matched_peers = []
    for candidate_id in candidate_ids:
        peer = peer_videos.get(candidate_id)
        if not peer:
            continue
        peer_format = _format_assessment(peer)
        if peer_format["duration_seconds"] > YOUTUBE_MAX_SHORT_DURATION_SECONDS:
            if target_format["duration_seconds"] <= YOUTUBE_MAX_SHORT_DURATION_SECONDS:
                continue
        elif target_format["duration_seconds"] > YOUTUBE_MAX_SHORT_DURATION_SECONDS:
            continue

        peer_published = date.fromisoformat(peer["snippet"]["publishedAt"][:10])
        peer_age_days = max(1, (period_end - peer_published).days)
        age_tolerance_days = max(14, int(target_age_days * 0.5))
        if abs(peer_age_days - target_age_days) > age_tolerance_days:
            continue
        matched_peers.append(
            (abs(peer_age_days - target_age_days), peer["id"], peer)
        )
    matched_peers = [
        peer for _, _, peer in sorted(matched_peers)[:10]
    ]

    cohort = []
    peer_errors = []
    for peer in matched_peers:
        peer_rows, peer_error = _try_query_analytics(
            analytics,
            start,
            end,
            ANALYTICS_METRICS,
            video_id=peer["id"],
        )
        if peer_error:
            peer_errors.append(peer_error)
        elif peer_rows:
            cohort.extend(peer_rows)

    performance = performance_rows[0] if performance_rows else {}
    result = {
        "video": {
            "id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": snippet.get("title"),
            "description": snippet.get("description", ""),
            "tags": snippet.get("tags", []),
            "category_id": snippet.get("categoryId"),
            "published_at": snippet.get("publishedAt"),
            "duration": target_format,
            "public_statistics": video.get("statistics", {}),
        },
        "analytics_period": {"start_date": start, "end_date": end},
        "analytics": performance,
        "traffic_sources": {
            "rows": traffic_rows,
            "availability": traffic_error or (
                "No rows returned for this period."
                if not traffic_rows
                else "Available"
            ),
        },
        "audience_demographics": {
            "rows": audience_rows,
            "availability": audience_error or (
                "No demographic rows returned for this period."
                if not audience_rows
                else "Available"
            ),
        },
        "comparison": {
            "candidate_count": len(matched_peers),
            "available_peer_rows": len(cohort),
            "matched_cohort_rows": len(cohort),
            "availability": (
                peer_errors[0]
                if peer_errors and not cohort
                else "Available"
                if cohort
                else "No age/duration-matched peer video data returned for this period."
            ),
            "scope": (
                "Own-channel videos in the same duration group and analytics date range; "
                "age matching uses upload dates. Candidate IDs may come from local analytics "
                "exports and recent uploads; peer metrics are refetched for this date range."
            ),
            "metrics_median": {
                metric: median(
                    row[metric] for row in cohort
                    if metric in row and isinstance(row[metric], (int, float))
                )
                for metric in (
                    "views",
                    "estimatedMinutesWatched",
                    "averageViewDuration",
                    "subscribersGained",
                )
                if any(
                    metric in row and isinstance(row[metric], (int, float))
                    for row in cohort
                )
            },
        },
        "impressions": {
            "value": None,
            "availability": (
                "Unavailable: the connected YouTube Analytics API reports endpoint "
                "does not provide an impressions metric."
            ),
        },
        "click_through_rate": {
            "value": None,
            "availability": (
                "Unavailable: the connected YouTube Analytics API reports endpoint "
                "does not provide a thumbnail impressions CTR metric."
            ),
        },
    }
    return result


def analyze_videos(youtube, analytics, reference=None, *, latest_short_candidates=0):
    if reference and latest_short_candidates:
        raise ValueError("Choose a video reference or recent short candidates, not both.")
    inspected_upload_count = 0
    if reference:
        selected = [_resolve_video(youtube, reference)]
    elif latest_short_candidates > 0:
        recent_ids = _list_recent_upload_ids(youtube, 200)
        inspected_upload_count = len(recent_ids)
        recent_videos = _fetch_video_details(youtube, recent_ids)
        selected = []
        for video_id in recent_ids:
            video = recent_videos.get(video_id)
            if not video:
                continue
            if (
                _parse_duration_seconds(
                    video.get("contentDetails", {}).get("duration", "")
                )
                <= YOUTUBE_MAX_SHORT_DURATION_SECONDS
            ):
                selected.append(video)
                if len(selected) == latest_short_candidates:
                    break
        if not selected:
            raise VideoNotFoundError(
                "No uploads within the 200 most recent videos matched the "
                "3-minute Short-candidate duration."
            )
    else:
        raise ValueError("Provide a video reference or a positive Short candidate count.")

    return {
        "channel_id": CHANNEL_ID,
        "format_classification_note": (
            "The Data API exposes duration but not vertical orientation or an authoritative "
            "Shorts classification. Videos at most 180 seconds are candidates, not confirmed Shorts."
        ),
        "videos": [
            _collect_video_analysis(youtube, analytics, video)
            for video in selected
        ],
        "selection_note": (
            f"Found {len(selected)} videos at or below the 180-second candidate threshold "
            f"among the {inspected_upload_count} most recent "
            "uploads inspected; these are not confirmed Shorts."
            if latest_short_candidates and len(selected) < latest_short_candidates
            else None
        ),
    }


def _validate_hashtags(title, description):
    hashtag_tokens = re.findall(
        r"(?<![\w#])#([^\s#]+)", f"{title}\n{description}"
    )
    hashtags = [token.rstrip(".,!?;:)]}") for token in hashtag_tokens]
    if any(
        not re.fullmatch(r"[\w]+", hashtag, flags=re.UNICODE)
        for hashtag in hashtags
    ):
        raise MetadataValidationError(
            "Hashtags must contain only letters, numbers, or underscores, with no spaces."
        )
    if len(hashtags) > 15:
        raise MetadataValidationError(
            "A video may have at most 15 hashtags in its title and description."
        )


def _validate_metadata(snippet):
    title = snippet.get("title")
    description = snippet.get("description", "")
    tags = snippet.get("tags", [])

    if not isinstance(title, str) or not title.strip():
        raise MetadataValidationError(
            "Title must contain at least one non-space character."
        )
    if len(title) > 100:
        raise MetadataValidationError("Title must be no longer than 100 characters.")
    if not isinstance(description, str):
        raise MetadataValidationError("Description must be text.")
    if len(description) > 5000:
        raise MetadataValidationError(
            "Description must be no longer than 5000 characters."
        )
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in tags
    ):
        raise MetadataValidationError("Tags must be a list of non-empty strings.")
    if len({tag.casefold() for tag in tags}) != len(tags):
        raise MetadataValidationError("Tags must not contain duplicates.")
    if len(",".join(tags)) > 500:
        raise MetadataValidationError(
            "Combined tags must be no longer than 500 characters."
        )

    _validate_hashtags(title, description)


def prepare_metadata_update(youtube, change):
    if not isinstance(change, dict):
        raise MetadataValidationError("Each update must be a JSON object.")
    allowed_keys = {
        "id",
        "title",
        "description",
        "description_append",
        "tags",
        "allow_description_replacement",
        "allow_tag_removal",
    }
    unexpected_keys = set(change) - allowed_keys
    if unexpected_keys:
        raise MetadataValidationError(
            "Only title, description, and tags can be updated; "
            f"unsupported fields: {', '.join(sorted(unexpected_keys))}"
        )

    video_id = change.get("id")
    if not isinstance(video_id, str) or not video_id.strip():
        raise MetadataValidationError("Each update needs a non-empty video id.")
    if "description" in change and "description_append" in change:
        raise MetadataValidationError(
            "Use either description replacement or description_append, not both."
        )
    if "description_append" in change and not isinstance(
        change["description_append"], str
    ):
        raise MetadataValidationError("description_append must be text.")
    if "tags" in change and (
        not isinstance(change["tags"], list)
        or any(not isinstance(tag, str) for tag in change["tags"])
    ):
        raise MetadataValidationError("tags must be a list of strings.")
    if "tags" in change and any(not tag.strip() for tag in change["tags"]):
        raise MetadataValidationError("Tags must be a list of non-empty strings.")
    if "tags" in change and len(
        {tag.casefold() for tag in change["tags"]}
    ) != len(change["tags"]):
        raise MetadataValidationError("Tags must not contain duplicates.")

    current = fetch_video_snippet(youtube, video_id)
    proposed = deepcopy(current)

    if "title" in change:
        proposed["title"] = change["title"]

    if "description" in change:
        if (
            current.get("description")
            and change["description"] != current.get("description")
            and change.get("allow_description_replacement") is not True
        ):
            raise MetadataValidationError(
                f"Video {video_id}: replacing a non-empty description requires "
                "allow_description_replacement: true."
            )
        proposed["description"] = change["description"]
    elif "description_append" in change:
        addition = change["description_append"]
        if addition:
            existing = current.get("description", "")
            proposed["description"] = (
                f"{existing}\n\n{addition}" if existing else addition
            )

    if "tags" in change:
        current_tags = current.get("tags", [])
        proposed_tags = change["tags"]
        proposed_tag_keys = {tag.casefold() for tag in proposed_tags}
        removed_tags = [
            tag for tag in current_tags
            if tag.casefold() not in proposed_tag_keys
        ]
        if removed_tags and change.get("allow_tag_removal") is not True:
            raise MetadataValidationError(
                f"Video {video_id}: removing existing tags requires "
                "allow_tag_removal: true."
            )
        proposed["tags"] = proposed_tags

    _validate_metadata(proposed)
    return {
        "id": video_id,
        "current_snippet": current,
        "proposed_snippet": proposed,
    }


def _metadata_snapshot(snippet):
    return {
        "categoryId": snippet.get("categoryId"),
        "title": snippet.get("title"),
        "description": snippet.get("description", ""),
        "tags": snippet.get("tags", []),
    }


def print_metadata_plan(plan):
    video_id = plan["id"]
    current = _metadata_snapshot(plan["current_snippet"])
    proposed = _metadata_snapshot(plan["proposed_snippet"])
    print(f"\nVideo {video_id}")
    print("Current metadata:")
    print(json.dumps(current, indent=2, ensure_ascii=False))
    print("Proposed metadata:")
    print(json.dumps(proposed, indent=2, ensure_ascii=False))
    diff = difflib.unified_diff(
        json.dumps(current, indent=2, ensure_ascii=False).splitlines(),
        json.dumps(proposed, indent=2, ensure_ascii=False).splitlines(),
        fromfile="current",
        tofile="proposed",
        lineterm="",
    )
    print("Diff:")
    print("\n".join(diff) if current != proposed else "(no changes)")


def _write_audit_entry(
    audit_log,
    plan,
    result,
    after_snippet=None,
    proposal_id=None,
    audit_callback=None,
):
    before = _metadata_snapshot(plan["current_snippet"])
    proposed = _metadata_snapshot(plan["proposed_snippet"])
    after = _metadata_snapshot(after_snippet) if after_snippet is not None else None
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "video_id": plan["id"],
        "video_url": f"https://www.youtube.com/watch?v={plan['id']}",
        "proposal_id": proposal_id,
        "approved_changes": {
            field: proposed[field]
            for field in ("title", "description", "tags")
            if before[field] != proposed[field]
        },
        "before": before,
        "after": after,
        "result": result,
    }
    if audit_callback is not None:
        audit_callback(deepcopy(entry))
    if audit_log is not None:
        audit_path = Path(audit_log)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as audit_file:
            audit_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _writable_snippet(snippet):
    writable_fields = (
        "title",
        "description",
        "tags",
        "categoryId",
        "defaultLanguage",
        "defaultAudioLanguage",
    )
    return {
        field: deepcopy(snippet[field])
        for field in writable_fields
        if field in snippet
    }


def _metadata_matches(actual, expected):
    if actual.get("categoryId") != expected.get("categoryId"):
        return False
    if actual.get("title") != expected.get("title"):
        return False
    if actual.get("description", "") != expected.get("description", ""):
        return False
    actual_tags = {tag.casefold() for tag in actual.get("tags", [])}
    expected_tags = {tag.casefold() for tag in expected.get("tags", [])}
    return actual_tags == expected_tags


def _apply_metadata_update(
    youtube,
    plan,
    audit_log=AUDIT_LOG_FILE,
    proposal_id=None,
    audit_callback=None,
):
    video_id = plan["id"]
    current_metadata = _metadata_snapshot(plan["current_snippet"])
    proposed_metadata = _metadata_snapshot(plan["proposed_snippet"])
    if current_metadata == proposed_metadata:
        return "no changes"

    try:
        # videos.update replaces mutable fields in every part listed.
        youtube.videos().update(
            part="snippet",
            body={
                "id": video_id,
                "snippet": _writable_snippet(plan["proposed_snippet"]),
            },
        ).execute()
    except HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        _write_audit_entry(
            audit_log,
            plan,
            f"update failed (HTTP {status})",
            proposal_id=proposal_id,
            audit_callback=audit_callback,
        )
        raise
    except (GoogleAuthError, httplib2.HttpLib2Error, OSError) as error:
        _write_audit_entry(
            audit_log,
            plan,
            "update outcome unknown (transport failure)",
            proposal_id=proposal_id,
            audit_callback=audit_callback,
        )
        raise RuntimeError(
            f"The update request for {video_id} failed without a conclusive response. "
            "Its outcome is unknown; reload YouTube metadata before retrying."
        ) from error

    if proposal_id:
        proposal_file = PENDING_PROPOSALS_DIR / f"{proposal_id}.json"
        if proposal_file.exists():
            proposal_file.unlink()

    try:
        verified = None
        for attempt in range(len(VERIFICATION_RETRY_DELAYS_SECONDS) + 1):
            verified = fetch_video_snippet(youtube, video_id)
            if _metadata_matches(verified, plan["proposed_snippet"]):
                break
            if attempt < len(VERIFICATION_RETRY_DELAYS_SECONDS):
                time.sleep(VERIFICATION_RETRY_DELAYS_SECONDS[attempt])
    except (HttpError, VideoNotFoundError, MetadataValidationError) as error:
        status = getattr(getattr(error, "resp", None), "status", "unavailable")
        _write_audit_entry(
            audit_log,
            plan,
            f"verification failed (HTTP {status})",
            proposal_id=proposal_id,
            audit_callback=audit_callback,
        )
        raise
    except (GoogleAuthError, httplib2.HttpLib2Error, OSError) as error:
        _write_audit_entry(
            audit_log,
            plan,
            "verification failed (transport failure; update outcome unknown)",
            proposal_id=proposal_id,
            audit_callback=audit_callback,
        )
        raise RuntimeError(
            f"The update for {video_id} may have succeeded, but YouTube could not be "
            "reached for verification. Reload metadata; do not retry blindly."
        ) from error

    verified_matches = _metadata_matches(verified, plan["proposed_snippet"])
    result = "success" if verified_matches else "verification mismatch"
    _write_audit_entry(
        audit_log,
        plan,
        result,
        verified,
        proposal_id=proposal_id,
        audit_callback=audit_callback,
    )
    if not verified_matches:
        raise RuntimeError(
            f"YouTube accepted the update for {video_id}, but verification "
            "found different metadata."
        )
    return "success"


def execute_metadata_updates(
    youtube, plans, *, dry_run=True, confirmed_ids=(), audit_log=AUDIT_LOG_FILE
):
    if dry_run:
        return {plan["id"]: "dry run" for plan in plans}

    plan_ids = {plan["id"] for plan in plans}
    selected_ids = set(confirmed_ids)
    if not selected_ids or not selected_ids.issubset(plan_ids):
        raise ValueError("Applying updates requires confirmation for selected video IDs.")

    results = {}
    for plan in plans:
        if plan["id"] in selected_ids:
            results[plan["id"]] = _apply_metadata_update(youtube, plan, audit_log)
    return results


def _canonical_digest(video_id, before_snippet, proposed_snippet):
    content = json.dumps(
        {
            "video_id": video_id,
            "before_snippet": before_snippet,
            "proposed_snippet": proposed_snippet,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def metadata_proposal_digest(video_id, before_snippet, proposed_snippet):
    """Return the stable approval token for one complete metadata proposal."""
    return _canonical_digest(video_id, before_snippet, proposed_snippet)


def apply_approved_dashboard_proposal(
    youtube,
    video_id,
    before_snippet,
    proposed_snippet,
    approved_digest,
    *,
    audit_callback,
):
    if not isinstance(before_snippet, dict) or not isinstance(proposed_snippet, dict):
        raise MetadataValidationError("The approved metadata proposal is incomplete.")
    if not isinstance(approved_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}",
        approved_digest,
    ):
        raise MetadataValidationError("The approval digest is invalid.")
    actual_digest = _canonical_digest(video_id, before_snippet, proposed_snippet)
    if not hmac.compare_digest(actual_digest, approved_digest):
        raise MetadataValidationError(
            "The approval does not match this exact metadata proposal."
        )

    mutable_fields = {"title", "description", "tags"}
    for field in before_snippet:
        if field not in mutable_fields and (
            before_snippet[field] != proposed_snippet.get(field)
        ):
            raise MetadataValidationError(
                f"The proposal attempts to change unsupported snippet field: {field}."
            )
    for field in proposed_snippet:
        if field not in before_snippet and field not in mutable_fields:
            raise MetadataValidationError(
                f"The proposal adds unsupported snippet field: {field}."
            )
    _validate_metadata(proposed_snippet)

    current = fetch_video_snippet(youtube, video_id)
    if current != before_snippet:
        raise RuntimeError(
            "Video metadata changed after review. Reload the video and approve a new proposal."
        )
    plan = {
        "id": video_id,
        "current_snippet": current,
        "proposed_snippet": deepcopy(proposed_snippet),
    }
    result = _apply_metadata_update(
        youtube,
        plan,
        audit_log=None,
        proposal_id=approved_digest[:20],
        audit_callback=audit_callback,
    )
    return {
        "result": result,
        "video_id": video_id,
        "title": proposed_snippet.get("title", ""),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def stage_metadata_proposal(youtube, video_id, proposal):
    if not isinstance(proposal, dict):
        raise MetadataValidationError("A proposal must be a JSON object.")
    expected_fields = {"title", "description", "tags"}
    if set(proposal) != expected_fields:
        raise MetadataValidationError(
            "A complete proposal must contain exactly title, description, and tags."
        )
    if not isinstance(proposal["tags"], list):
        raise MetadataValidationError("Proposed tags must be a list.")

    plan = prepare_metadata_update(
        youtube,
        {
            "id": video_id,
            "title": proposal["title"],
            "description": proposal["description"],
            "tags": proposal["tags"],
            "allow_description_replacement": True,
            "allow_tag_removal": True,
        },
    )
    current = deepcopy(plan["current_snippet"])
    proposed = deepcopy(plan["proposed_snippet"])
    digest = _canonical_digest(video_id, current, proposed)
    proposal_id = digest[:20]
    record = {
        "proposal_id": proposal_id,
        "video_id": video_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "digest": digest,
        "before_snippet": current,
        "proposed_snippet": proposed,
    }
    PENDING_PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    for previous_file in PENDING_PROPOSALS_DIR.glob("*.json"):
        previous = json.loads(previous_file.read_text(encoding="utf-8"))
        if previous.get("video_id") == video_id:
            previous_file.unlink()
    proposal_file = PENDING_PROPOSALS_DIR / f"{proposal_id}.json"
    proposal_file.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    staged_plan = {
        "id": video_id,
        "current_snippet": current,
        "proposed_snippet": proposed,
    }
    print_metadata_plan(staged_plan)
    print(f"\nProposal ID: {proposal_id}")
    print(
        "This ID approves only this video's exact displayed title, description, and tags. "
        "If the proposal is revised, the prior ID is invalidated."
    )
    return proposal_id


def apply_staged_proposal(youtube, proposal_id, audit_log=AUDIT_LOG_FILE):
    if not re.fullmatch(r"[a-f0-9]{20}", proposal_id):
        raise ValueError("Invalid proposal ID.")
    proposal_file = PENDING_PROPOSALS_DIR / f"{proposal_id}.json"
    if not proposal_file.is_file():
        raise FileNotFoundError(
            "Proposal not found or no longer current; prepare and approve the latest proposal."
        )

    record = json.loads(proposal_file.read_text(encoding="utf-8"))
    video_id = record.get("video_id")
    before = record.get("before_snippet")
    proposed = record.get("proposed_snippet")
    digest = _canonical_digest(video_id, before, proposed)
    if (
        record.get("proposal_id") != proposal_id
        or record.get("digest") != digest
        or digest[:20] != proposal_id
    ):
        raise MetadataValidationError(
            "The stored proposal was altered or is not bound to this approval ID."
        )
    if not isinstance(before, dict) or not isinstance(proposed, dict):
        raise MetadataValidationError("The stored proposal is incomplete.")
    _validate_metadata(proposed)
    for field in before:
        if field not in {"title", "description", "tags"} and (
            before[field] != proposed.get(field)
        ):
            raise MetadataValidationError(
                f"The proposal attempts to change unsupported snippet field: {field}."
            )
    for field in proposed:
        if field not in before and field not in {"title", "description", "tags"}:
            raise MetadataValidationError(
                f"The proposal adds unsupported snippet field: {field}."
            )

    current = fetch_video_snippet(youtube, video_id)
    if current != before:
        raise RuntimeError(
            "The YouTube metadata changed since this proposal was shown. "
            "Refresh the analysis and request approval for a new proposal."
        )
    plan = {
        "id": video_id,
        "current_snippet": current,
        "proposed_snippet": proposed,
    }
    result = _apply_metadata_update(
        youtube,
        plan,
        audit_log=audit_log,
        proposal_id=proposal_id,
    )
    return {
        "result": result,
        "video_id": video_id,
        "title": proposed.get("title", ""),
    }


def _load_metadata_updates(filename):
    with Path(filename).open(encoding="utf-8") as update_file:
        updates = json.load(update_file)
    if not isinstance(updates, list) or not updates:
        raise MetadataValidationError(
            "The metadata updates file must contain a non-empty JSON array."
        )
    video_ids = []
    for change in updates:
        if not isinstance(change, dict):
            raise MetadataValidationError("Each update must be a JSON object.")
        video_id = change.get("id")
        if not isinstance(video_id, str) or not video_id.strip():
            raise MetadataValidationError("Each update needs a non-empty video id.")
        video_ids.append(video_id)
    if len(set(video_ids)) != len(video_ids):
        raise MetadataValidationError("The updates file contains duplicate video IDs.")
    return updates


def _run_metadata_updates(youtube, updates, *, apply=False):
    plans = [prepare_metadata_update(youtube, change) for change in updates]
    for plan in plans:
        print_metadata_plan(plan)

    if not apply:
        print("\nDry run only. No metadata was written to YouTube.")
        return

    eligible_ids = {
        plan["id"]
        for plan in plans
        if _metadata_snapshot(plan["current_snippet"])
        != _metadata_snapshot(plan["proposed_snippet"])
    }
    if not eligible_ids:
        print("\nNo changes to apply.")
        return

    print("\nEnter the exact video IDs to update, separated by commas.")
    print("Available IDs: " + ", ".join(sorted(eligible_ids)))
    selected_ids = {
        value.strip()
        for value in input("Selected video IDs: ").split(",")
        if value.strip()
    }
    if not selected_ids or not selected_ids.issubset(eligible_ids):
        raise ValueError("Selection was empty or included an unlisted video ID.")
    confirmation = input(
        "Review the listed changes above. Type APPLY to write these selected "
        "videos: "
    )
    if confirmation != "APPLY":
        print("Confirmation not received. No metadata was written to YouTube.")
        return

    results = execute_metadata_updates(
        youtube,
        plans,
        dry_run=False,
        confirmed_ids=selected_ids,
    )
    for video_id, result in results.items():
        print(f"{video_id}: {result}")


def export_report(api, filename, start_date, end_date,
                  metrics, dimensions, sort=None):
    request = api.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics=metrics,
        dimensions=dimensions,
        sort=sort,
        maxResults=200,
    )
    result = request.execute()

    headers = [col["name"] for col in result.get("columnHeaders", [])]
    rows = result.get("rows", [])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / filename

    with output_file.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    print(f"Saved {output_file} ({len(rows)} rows)")
    return headers, rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="YouTube analytics and metadata connector")
    workflow = parser.add_mutually_exclusive_group()
    workflow.add_argument(
        "--analyze",
        metavar="VIDEO_URL_ID_OR_TITLE",
        help="Analyze a channel video using read-only YouTube APIs.",
    )
    workflow.add_argument(
        "--latest-short-candidates",
        type=int,
        metavar="COUNT",
        help=(
            "Analyze recent videos of at most three minutes. The Data API cannot "
            "confirm Shorts orientation."
        ),
    )
    workflow.add_argument(
        "--stage-proposal",
        metavar="VIDEO_ID",
        help="Stage exact title/description/tags JSON read from stdin; performs no write.",
    )
    workflow.add_argument(
        "--apply-proposal",
        metavar="PROPOSAL_ID",
        help="Apply only the exact previously displayed proposal identified by this ID.",
    )
    workflow.add_argument(
        "--metadata-updates",
        metavar="JSON_FILE",
        help="Preview or apply title, description, and tag changes from a JSON array.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Prompt to select and confirm videos before applying metadata updates.",
    )
    args = parser.parse_args(argv)

    if args.apply and not args.metadata_updates:
        parser.error("--apply requires --metadata-updates.")
    if args.latest_short_candidates is not None and args.latest_short_candidates <= 0:
        parser.error("--latest-short-candidates must be greater than zero.")

    if args.stage_proposal:
        proposal = json.load(sys.stdin)
        creds = authorize()
        youtube = build("youtube", "v3", credentials=creds)
        stage_metadata_proposal(youtube, args.stage_proposal, proposal)
        return

    if args.apply_proposal:
        creds = authorize()
        youtube = build("youtube", "v3", credentials=creds)
        result = apply_staged_proposal(youtube, args.apply_proposal)
        print(
            f"Verified {result['result']} for {result['title']}: "
            f"https://www.youtube.com/watch?v={result['video_id']}"
        )
        return

    if args.analyze or args.latest_short_candidates is not None:
        creds = authorize()
        youtube = build("youtube", "v3", credentials=creds)
        analytics = build("youtubeAnalytics", "v2", credentials=creds)
        result = analyze_videos(
            youtube,
            analytics,
            args.analyze,
            latest_short_candidates=args.latest_short_candidates or 0,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.metadata_updates:
        updates = _load_metadata_updates(args.metadata_updates)
        creds = authorize()
        youtube = build("youtube", "v3", credentials=creds)
        _run_metadata_updates(youtube, updates, apply=args.apply)
        return

    creds = authorize()

    youtube = build("youtube", "v3", credentials=creds)
    analytics = build("youtubeAnalytics", "v2", credentials=creds)

    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=89)

    start = start_date.isoformat()
    end = end_date.isoformat()

    print(f"Fetching channel reports: {start} through {end}")

    # Video-level performance
    headers, rows = export_report(
        analytics,
        "video_performance.csv",
        start,
        end,
        "views,estimatedMinutesWatched,averageViewDuration,"
        "likes,comments,shares,subscribersGained,subscribersLost",
        "video",
        "-views",
    )

    # Attach video titles to the video-level report
    video_index = headers.index("video")
    video_ids = list(dict.fromkeys(str(row[video_index]) for row in rows))
    titles = {}

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        response = youtube.videos().list(
            part="snippet",
            id=",".join(batch),
        ).execute()

        for item in response.get("items", []):
            titles[item["id"]] = item["snippet"]["title"]

    titled_file = OUTPUT_DIR / "video_performance_with_titles.csv"
    with titled_file.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["videoTitle"] + headers)
        for row in rows:
            writer.writerow([titles.get(str(row[video_index]), "")] + row)

    print(f"Saved {titled_file}")

    # Channel trend over time
    export_report(
        analytics,
        "daily_performance.csv",
        start,
        end,
        "views,estimatedMinutesWatched,subscribersGained,subscribersLost",
        "day",
        "day",
    )

    # Where viewers discover the channel
    export_report(
        analytics,
        "traffic_sources.csv",
        start,
        end,
        "views,estimatedMinutesWatched",
        "insightTrafficSourceType",
        "-views",
    )

    print("\nConnector finished.")
    print(f"CSV reports are in: {OUTPUT_DIR}")


if __name__ == "__main__":
    try:
        main()
    except HttpError as error:
        status = getattr(error.resp, "status", "unknown")
        if status == 401:
            message = (
                "Google rejected the saved authorization. Check the OAuth client "
                "configuration and complete the browser sign-in again."
            )
        elif status == 403:
            message = (
                "Google denied this API request. The channel/account may lack access, "
                "or the API quota/permission may be unavailable."
            )
        elif status == 404:
            message = "The requested YouTube resource was not found or is unavailable."
        else:
            message = f"YouTube API request failed (HTTP {status})."
        print(message, file=sys.stderr)
        sys.exit(2)
    except (RefreshError, FileNotFoundError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
