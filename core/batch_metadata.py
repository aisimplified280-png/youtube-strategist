"""Spreadsheet and text-based batch metadata staging and publishing.

Shares one unified pipeline for Excel/CSV/TSV imports and natural-language text instructions.
Enforces strict ownership checks, conditional title angles, fail-closed approval,
sequential publishing, and post-write verification.
"""

import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from openpyxl import load_workbook

import youtube_analytics_connector as connector

logger = logging.getLogger("batch_metadata")

VALID_ROW_STATUSES = {
    "ready",
    "approved",
    "publishing",
    "verified",
    "no_change",
    "invalid",
    "failed",
    "verification_unavailable",
}


def normalize_title_for_comparison(title: str) -> str:
    """Normalize title by removing emojis, punctuation, symbols, extra whitespace, and lowercasing."""
    if not title or not isinstance(title, str):
        return ""
    clean = "".join(ch for ch in title if unicodedata.category(ch)[0] not in ('S', 'P', 'C'))
    clean = re.sub(r"[^\w\s]", " ", clean)
    return re.sub(r"\s+", " ", clean).strip().casefold()


def validate_title_angles_quality(cur_title: str, angles: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """Require exactly one distinct, non-trivial title for each approved angle."""
    required_angles = {"curiosity", "value", "search"}
    if not isinstance(angles, list) or len(angles) != 3:
        return False, "AI title optimization must produce exactly 3 title angles (curiosity, value, search)."

    norm_current = normalize_title_for_comparison(cur_title)
    seen_types = set()
    seen_titles = set()

    for idx, angle in enumerate(angles):
        if not isinstance(angle, dict):
            return False, f"Title angle #{idx + 1} is not a valid dictionary."
        angle_type = str(angle.get("angle", "")).strip().casefold()
        if angle_type not in required_angles:
            return False, f"Title angle #{idx + 1} has unsupported category '{angle_type}'. Use curiosity, value, or search."
        if angle_type in seen_types:
            return False, f"Duplicate title angle category '{angle_type}'."
        seen_types.add(angle_type)

        title = angle.get("title")
        if not isinstance(title, str) or not title.strip():
            return False, f"Title angle #{idx + 1} has an empty or invalid title."
        title = title.strip()
        if len(title) > 100:
            return False, f"Title angle #{idx + 1} exceeds 100 characters ({len(title)} chars)."

        normalized = normalize_title_for_comparison(title)
        if not normalized:
            return False, f"Title angle #{idx + 1} contains no meaningful title text."
        if norm_current and normalized == norm_current:
            return False, f"Generated title '{title}' is only a punctuation, emoji, symbol, or casing variation of the current title."
        if normalized in seen_titles:
            return False, f"Generated title angle '{title}' duplicates another angle."
        seen_titles.add(normalized)

    missing = required_angles - seen_types
    if missing:
        return False, "Missing required title angle categories: " + ", ".join(sorted(missing)) + "."
    return True, ""


def validate_title_optimization_result(cur_title: str, result: Dict[str, Any]) -> Tuple[bool, str]:
    """Validate the complete AI title recommendation, not just the angle array."""
    if not isinstance(result, dict):
        return False, "AI title optimization did not return a valid object."

    angles = result.get("title_angles")
    valid, error = validate_title_angles_quality(cur_title, angles)
    if not valid:
        return False, error

    title = result.get("title")
    recommended = result.get("recommended_title")
    if not isinstance(title, str) or not title.strip():
        return False, "AI title optimization did not provide a primary title."
    if not isinstance(recommended, str) or not recommended.strip():
        return False, "AI title optimization did not provide the required recommended_title."
    title = title.strip()
    recommended = recommended.strip()

    if title != recommended:
        return False, "The primary title and recommended_title do not match."
    angle_titles = {angle["title"].strip() for angle in angles}
    if recommended not in angle_titles:
        return False, "recommended_title must exactly match one of the three validated title angles."

    reason = result.get("recommendation_reason")
    if not isinstance(reason, str) or not reason.strip():
        rationale = result.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            result["recommendation_reason"] = rationale.strip()
        else:
            return False, "AI title optimization did not explain why the recommended title is strongest."
    return True, ""


BATCH_MAX_ROWS = 25

BATCH_MAX_ROWS = 25
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_HEADER_ALIASES = {
    "video_id": {"video id", "video_id", "youtube id", "youtube_id", "id"},
    "video_url": {"video url", "video_url", "youtube url", "youtube_url", "url", "link"},
    "title": {"title", "new title", "new_title", "proposed title"},
    "description": {"description", "new description", "new_description", "proposed description"},
    "tags": {"tags", "new tags", "new_tags", "youtube tags", "proposed tags"},
}


def _normalise_header(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def parse_video_reference(value):
    """Return an 11-character video ID from a YouTube ID or supported URL."""
    value = _cell_text(value)
    if VIDEO_ID_RE.fullmatch(value):
        return value

    candidate = value if "://" in value else "https://" + value
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("Use an 11-character YouTube video ID or a youtube.com / youtu.be URL.")

    if hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        video_id = parts[1] if len(parts) > 1 and parts[0] in {"shorts", "embed", "live"} else ""

    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("The YouTube URL does not contain a valid video ID.")
    return video_id


def parse_tags_cell(value):
    """Parse tag cells as JSON arrays or simple comma/semicolon/pipe-separated text."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = _cell_text(value)
    if not text:
        return None
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("Tags beginning with '[' must be a valid JSON array.") from error
        if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
            raise ValueError("The tags JSON value must be an array of strings.")
        return [item.strip() for item in decoded if item.strip()]
    separator = ";" if ";" in text else ("|" if "|" in text else ",")
    return [part.strip() for part in text.split(separator) if part.strip()]


def _read_table(file_name, payload):
    suffix = str(file_name or "").lower().rsplit(".", 1)[-1]
    if suffix == "xlsx":
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        try:
            sheet = workbook.active
            values = list(sheet.iter_rows(values_only=True))
        finally:
            workbook.close()
        return values
    if suffix not in {"csv", "tsv"}:
        raise ValueError("Upload a .csv, .tsv, or .xlsx metadata file.")
    try:
        decoded = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("The file must be UTF-8 encoded.") from error
    first_line = next((line for line in decoded.splitlines() if line.strip()), "")
    if suffix == "tsv":
        delimiter = "\t"
    elif first_line.count(";") > first_line.count(","):
        delimiter = ";"
    else:
        delimiter = ","
    return list(csv.reader(io.StringIO(decoded), delimiter=delimiter))


def parse_metadata_file(file_name, payload):
    """Parse a metadata change file. Blank metadata cells mean 'keep current value'."""
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise ValueError("The uploaded metadata file is empty.")
    if len(payload) > 2_000_000:
        raise ValueError("Metadata import files must be 2 MB or smaller.")
    values = _read_table(file_name, bytes(payload))
    if not values:
        raise ValueError("The metadata file has no rows.")

    headers = [_normalise_header(value) for value in values[0]]
    header_indices = {}
    for canonical, aliases in _HEADER_ALIASES.items():
        for index, header in enumerate(headers):
            if header in aliases:
                header_indices[canonical] = index
                break

    if "video_id" not in header_indices and "video_url" not in header_indices:
        raise ValueError("The file needs a video_id or video_url column.")
    if not any(field in header_indices for field in ("title", "description", "tags")):
        raise ValueError("Add at least one metadata column: title, description, or tags.")

    rows = []
    for row_number, values_row in enumerate(values[1:], start=2):
        if not any(_cell_text(value) for value in values_row):
            continue
        if len(rows) >= BATCH_MAX_ROWS:
            raise ValueError(
                f"This batch contains more than {BATCH_MAX_ROWS} data rows. Split it into smaller files."
            )

        def get_cell(field):
            index = header_indices.get(field)
            return values_row[index] if index is not None and index < len(values_row) else None

        raw_id = _cell_text(get_cell("video_id")) or _cell_text(get_cell("video_url"))
        record = {
            "row_number": row_number,
            "video_id": None,
            "title": None,
            "description": None,
            "tags": None,
            "title_intent": "unmentioned",
            "description_intent": "unmentioned",
            "tags_intent": "unmentioned",
            "status": "invalid",
            "errors": [],
            "attempted": False,
        }
        try:
            record["video_id"] = parse_video_reference(raw_id)
        except ValueError as error:
            record["errors"].append(str(error))

        title = _cell_text(get_cell("title"))
        description_value = get_cell("description")
        description = _cell_text(description_value) if description_value is not None else ""
        tags_value = get_cell("tags")

        record["title"] = title or None
        if record["title"] is not None:
            record["title_intent"] = "exact"
            record["exact_title"] = record["title"]

        record["description"] = description if description else None
        if record["description"] is not None:
            record["description_intent"] = "exact"
            record["exact_description"] = record["description"]

        try:
            record["tags"] = parse_tags_cell(tags_value)
            if record["tags"] is not None:
                record["tags_intent"] = "exact"
                record["exact_tags"] = record["tags"]
        except ValueError as error:
            record["errors"].append(str(error))

        if not any(record[field] is not None for field in ("title", "description", "tags")):
            record["errors"].append("Provide at least one change. Blank cells keep the current metadata.")

        if record["video_id"] and not record["errors"]:
            record["status"] = "parsed"
        rows.append(record)

    if not rows:
        raise ValueError("No metadata rows were found beneath the header row.")

    occurrences = {}
    for row in rows:
        if row["video_id"]:
            occurrences.setdefault(row["video_id"], []).append(row)
    for video_id, matches in occurrences.items():
        if len(matches) > 1:
            for row in matches:
                row["errors"].append(f"Duplicate video ID {video_id} appears more than once in this file.")
                row["status"] = "invalid"

    fingerprint = hashlib.sha256(bytes(payload)).hexdigest()
    return {
        "file_name": str(file_name or "metadata upload"),
        "fingerprint": fingerprint,
        "rows": rows,
    }


def create_batch_from_text_plan(text_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a parsed natural-language batch plan into canonical batch structure."""
    rows = []
    videos = text_plan.get("videos", [])

    for idx, item in enumerate(videos, start=1):
        record = {
            "row_number": idx,
            "video_id": item.get("video_id"),
            "reference": item.get("reference", ""),
            "title": item.get("exact_title"),
            "description": item.get("exact_description"),
            "tags": item.get("exact_tags"),
            "title_intent": item.get("title_intent", "unmentioned"),
            "exact_title": item.get("exact_title"),
            "context_title": item.get("context_title", ""),
            "description_intent": item.get("description_intent", "unmentioned"),
            "exact_description": item.get("exact_description"),
            "tags_intent": item.get("tags_intent", "unmentioned"),
            "exact_tags": item.get("exact_tags"),
            "fields_requested": item.get("fields_requested", []),
            "original_instruction": item.get("original_instruction", ""),
            "title_angles": [],
            "recommended_title": "",
            "recommendation_reason": "",
            "rationale": [],
            "status": "parsed" if (item.get("video_id") and not item.get("errors")) else "invalid",
            "errors": list(item.get("errors", [])),
            "attempted": False,
        }
        rows.append(record)

    raw_text = text_plan.get("raw_text", "")
    fingerprint = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return {
        "file_name": "Pasted text instructions",
        "fingerprint": fingerprint,
        "rows": rows,
        "is_text_batch": True,
    }


def revalidate_batch_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute changed fields, validate against YouTube constraints, and update row digest and status."""
    before = row.get("before") or {}
    proposed = row.get("proposed") or {}
    video_id = row.get("video_id") or ""

    changed_fields = [
        field for field in ("title", "description", "tags")
        if before.get(field, [] if field == "tags" else "") != proposed.get(field, [] if field == "tags" else "")
    ]
    row["changed_fields"] = changed_fields

    if not changed_fields:
        row["status"] = "no_change"
        row["errors"] = ["The proposed values are identical to the current YouTube metadata."]
        row.pop("digest", None)
        return row

    try:
        connector._validate_metadata(proposed)
    except (connector.MetadataValidationError, ValueError) as error:
        row["status"] = "invalid"
        row["errors"] = [str(error)]
        row.pop("digest", None)
        return row

    row["status"] = "ready"
    row["errors"] = []  # Clear stale errors on fix
    row["proposed_title"] = proposed.get("title", "")
    row["digest"] = connector.metadata_proposal_digest(video_id, before, proposed)
    return row


def compute_batch_approval_fingerprint(
    selected_rows: List[Dict[str, Any]],
    review_revision: int = 1,
) -> str:
    """Calculate a stable cryptographic fingerprint of all currently selected proposals and revision state."""
    hasher = hashlib.sha256()
    hasher.update(str(review_revision).encode("utf-8"))
    for row in sorted(selected_rows, key=lambda r: str(r.get("video_id", ""))):
        vid = str(row.get("video_id", ""))
        digest = str(row.get("digest", ""))
        hasher.update(vid.encode("utf-8"))
        hasher.update(digest.encode("utf-8"))
    return hasher.hexdigest()[:16]


def batch_metadata_readback_mismatches(actual: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    """Return every approved metadata field that does not match a fresh YouTube snippet."""
    if not isinstance(actual, dict):
        return ["snippet"]
    mismatches = []
    if actual.get("title") != expected.get("title"):
        mismatches.append("title")
    if actual.get("description", "") != expected.get("description", ""):
        mismatches.append("description")
    actual_tags = {str(tag).casefold() for tag in actual.get("tags", [])}
    expected_tags = {str(tag).casefold() for tag in expected.get("tags", [])}
    if actual_tags != expected_tags:
        mismatches.append("tags")
    if actual.get("categoryId") != expected.get("categoryId"):
        mismatches.append("categoryId")
    return mismatches


def publish_approved_batch_rows(
    youtube,
    rows: List[Dict[str, Any]],
    selected_ids,
    *,
    approved: bool = False,
    approval_fingerprint: Optional[str] = None,
    review_revision: int = 1,
    audit_callback=None,
) -> Dict[str, Any]:
    """Publish selected READY rows only after an explicit, current approval fingerprint."""
    result = {
        "success_count": 0,
        "failure_count": 0,
        "blocked_count": 0,
        "blocked": False,
        "message": "",
    }
    selected = list(dict.fromkeys(str(video_id) for video_id in (selected_ids or []) if video_id))
    if not approved:
        result.update(blocked=True, message="Publishing requires explicit approval of the exact reviewed proposals.")
        return result
    if not selected:
        result.update(blocked=True, message="Select at least one ready video proposal before publishing.")
        return result

    selected_set = set(selected)
    selected_rows = [row for row in rows if str(row.get("video_id", "")) in selected_set and row.get("status") == "ready"]
    if len(selected_rows) != len(selected):
        result.update(
            blocked=True,
            message="Publishing was blocked because one or more selected rows are no longer READY. Refresh and review the batch again.",
        )
        return result

    current_fingerprint = compute_batch_approval_fingerprint(
        selected_rows,
        review_revision=review_revision,
    )
    if not approval_fingerprint or approval_fingerprint != current_fingerprint:
        result.update(
            blocked=True,
            message="Publishing was blocked because the approval fingerprint is stale or does not match the current proposals. Review and approve the latest values again.",
        )
        return result

    # Record the explicit approval state before any API operation begins.
    for row in selected_rows:
        row["status"] = "approved"
        row["approved_fingerprint"] = current_fingerprint

    for row in selected_rows:
        row["attempted"] = True
        row["status"] = "publishing"
        video_id = row["video_id"]
        expected_digest = connector.metadata_proposal_digest(
            video_id,
            row.get("before", {}),
            row.get("proposed", {}),
        )
        if row.get("digest") != expected_digest:
            row["status"] = "failed"
            row["result_message"] = "Publish blocked: proposal digest mismatch. The proposal changed after approval."
            result["failure_count"] += 1
            continue

        try:
            apply_result = connector.apply_approved_dashboard_proposal(
                youtube,
                video_id,
                row["before"],
                row["proposed"],
                row["digest"],
                audit_callback=audit_callback or (lambda entry: None),
            )
        except connector.MetadataVerificationUnavailableError as error:
            row["status"] = "verification_unavailable"
            row["result_message"] = str(error) or "The metadata update may have succeeded, but YouTube read-back verification was unavailable."
            result["failure_count"] += 1
            continue
        except connector.HttpError as error:
            details = connector.extract_http_error_details(error)
            status = details.get("status")
            err_msg = f"HTTP {status}" if status else "HTTP error"
            if details.get("reasons"):
                err_msg += f" ({', '.join(details['reasons'])})"
            err_msg += f": {details.get('message')}"
            if details.get("locations"):
                err_msg += f" [Field: {', '.join(details['locations'])}]"

            # YouTube rejects title edits while a Test & Compare experiment owns
            # the title. Keep the complete proposal visible and do not retry
            # description/tags as a second, separate write.
            error_text = f"{details.get('message', '')} {error}".upper()
            if "UPDATE_TITLE_NOT_ALLOWED_DURING_TEST_AND_COMPARE" in error_text:
                row["status"] = "blocked"
                row["result_message"] = (
                    "YouTube blocked this metadata request because a Test & Compare title experiment "
                    "is preventing title changes. The request was rejected; no successful metadata "
                    "update was confirmed. End or resolve the experiment in YouTube Studio, then "
                    "refresh this batch and review the proposal again. Description and tags will not "
                    "be retried separately."
                )
                result["blocked_count"] += 1
            else:
                row["status"] = "failed"
                row["result_message"] = err_msg
            result["failure_count"] += 1
            continue
        except Exception as error:
            message = str(error)
            lowered = message.casefold()
            if any(term in lowered for term in (
                "could not be reached for verification",
                "verification unavailable",
                "verification failed",
                "outcome is unknown",
                "may have succeeded",
            )) and not any(term in lowered for term in ("verification mismatch", "found different metadata")):
                row["status"] = "verification_unavailable"
            else:
                row["status"] = "failed"
            row["result_message"] = message or type(error).__name__
            result["failure_count"] += 1
            continue

        if not isinstance(apply_result, dict) or apply_result.get("result") != "success":
            row["status"] = "no_change" if isinstance(apply_result, dict) and apply_result.get("result") == "no changes" else "failed"
            row["result_message"] = "YouTube did not confirm a successful metadata update."
            result["failure_count"] += 1
            continue

        try:
            verified = connector.fetch_video_snippet(youtube, video_id)
        except Exception as error:
            row["status"] = "verification_unavailable"
            row["result_message"] = (
                "The update call succeeded, but a fresh YouTube read-back was unavailable: "
                f"{error}"
            )
            result["failure_count"] += 1
            continue

        mismatches = batch_metadata_readback_mismatches(verified, row["proposed"])
        if mismatches:
            row["status"] = "failed"
            row["result_message"] = (
                "YouTube read-back did not match the approved proposal. Mismatched fields: "
                + ", ".join(mismatches)
                + "."
            )
            result["failure_count"] += 1
            continue

        row["status"] = "verified"
        row["result_message"] = (
            f"Verified on YouTube: {apply_result.get('title', row.get('proposed_title', 'Updated video'))}"
        )
        result["success_count"] += 1

    return result


def prepare_metadata_batch(
    youtube,
    parsed_batch: Dict[str, Any],
    secrets: Optional[Dict[str, Any]] = None,
    ai_generator: Optional[Any] = None,
) -> Dict[str, Any]:
    """Fetch owner-verified live snippets in API batches and stage immutable row proposals."""
    if youtube is None:
        raise RuntimeError("Connect the verified YouTube owner account before reviewing metadata changes.")

    rows = parsed_batch.get("rows", [])
    valid_ids = list(dict.fromkeys(
        row["video_id"] for row in rows
        if row.get("status") == "parsed" and row.get("video_id")
    ))
    snippets = {}
    fetch_errors = {}

    for start in range(0, len(valid_ids), 50):
        chunk = valid_ids[start:start + 50]
        try:
            response = youtube.videos().list(
                part="snippet",
                id=",".join(chunk),
            ).execute()
            for item in response.get("items", []):
                video_id = item.get("id")
                snippet = item.get("snippet")
                if video_id not in chunk or not isinstance(snippet, dict):
                    continue
                if snippet.get("channelId") != connector.CHANNEL_ID:
                    fetch_errors[video_id] = "Ownership check failed: this video is not on the configured owner channel."
                    continue
                if not snippet.get("categoryId"):
                    fetch_errors[video_id] = "YouTube did not return a category ID; this row cannot be safely updated."
                    continue
                snippets[video_id] = deepcopy(snippet)
        except Exception as error:
            message = f"Could not load current YouTube metadata: {type(error).__name__}: {error}"
            for video_id in chunk:
                fetch_errors[video_id] = message

    for row in rows:
        if row.get("status") != "parsed":
            continue
        video_id = row["video_id"]
        if video_id in fetch_errors:
            row["status"] = "invalid"
            row["errors"].append(fetch_errors[video_id])
            continue
        before = snippets.get(video_id)
        if before is None:
            row["status"] = "invalid"
            row["errors"].append("Video not found, inaccessible, or not owned by the configured channel.")
            continue

        proposed = deepcopy(before)

        # Check which fields need AI optimization
        t_intent = row.get("title_intent")
        d_intent = row.get("description_intent")
        tags_intent = row.get("tags_intent")

        # Fail closed: an exact description must contain the user's supplied
        # non-empty value. Never turn a missing parser payload into an empty
        # description and risk publishing an unintended clear operation.
        if d_intent == "exact":
            exact_description = row.get("exact_description")
            if not isinstance(exact_description, str) or not exact_description.strip():
                row["status"] = "invalid"
                row["errors"].append(
                    "An exact description was requested, but no description text was extracted. "
                    "Nothing was staged for this video; check the pasted Description: block."
                )
                continue

        fields_to_optimize = []
        if t_intent == "optimize":
            fields_to_optimize.append("title")
        if d_intent == "optimize":
            fields_to_optimize.append("description")
        if tags_intent == "optimize":
            fields_to_optimize.append("tags")

        ai_result = {}
        if fields_to_optimize:
            if not ai_generator:
                row["status"] = "invalid"
                row["errors"].append("AI optimization was requested for this video, but no AI generator is available.")
                continue

            try:
                transcript = ""
                if youtube:
                    try:
                        transcript = connector.fetch_video_transcript(youtube, video_id)
                    except Exception:
                        transcript = ""

                import inspect
                sig = inspect.signature(ai_generator)
                has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                gen_kwargs = {
                    "video_id": video_id,
                    "before": before,
                    "instruction": row.get("original_instruction", ""),
                }
                if "fields" in sig.parameters or has_var_keyword:
                    gen_kwargs["fields"] = fields_to_optimize
                if "transcript" in sig.parameters or has_var_keyword:
                    gen_kwargs["transcript"] = transcript
                if "context_title" in sig.parameters or has_var_keyword:
                    gen_kwargs["context_title"] = row.get("context_title", "")

                logger.info(
                    "Batch AI generator invoked for video_id=%s, title_intent=%s, fields=%s",
                    video_id,
                    t_intent,
                    fields_to_optimize,
                )

                ai_result = ai_generator(**gen_kwargs)
                if not isinstance(ai_result, dict):
                    row["status"] = "invalid"
                    row["errors"].append("AI generator did not return a valid metadata proposal dictionary.")
                    continue
            except Exception as err:
                row["status"] = "invalid"
                row["errors"].append(f"AI metadata optimization failed: {err}")
                continue

        # 1. Title handling
        if t_intent == "exact" or (t_intent == "unmentioned" and row.get("title") is not None):
            proposed["title"] = row.get("exact_title") or row.get("title") or before.get("title", "")
            row["title_angles"] = []
        elif t_intent == "optimize":
            is_valid_result, err_msg = validate_title_optimization_result(
                before.get("title", ""),
                ai_result,
            )
            if not is_valid_result and ai_generator:
                # One controlled repair attempt; a second invalid response fails closed.
                try:
                    logger.info(
                        "Performing controlled title repair for video_id=%s: %s",
                        video_id,
                        err_msg,
                    )
                    repair_kwargs = deepcopy(gen_kwargs)
                    repair_kwargs["instruction"] = (
                        f"{row.get('original_instruction', '')}\\n\\n"
                        f"CONTROLLED REPAIR ATTEMPT: The previous title output was rejected: {err_msg}\\n"
                        "Return exactly three distinct angles, one each for curiosity, value, and search. "
                        "Each title must differ meaningfully from the current title and from the other two. "
                        "Set title and recommended_title to exactly one of those three titles, and explain "
                        "the recommendation in recommendation_reason."
                    )
                    repaired_result = ai_generator(**repair_kwargs)
                    repaired_valid, repaired_error = validate_title_optimization_result(
                        before.get("title", ""),
                        repaired_result,
                    )
                    if repaired_valid:
                        ai_result = repaired_result
                        is_valid_result = True
                    else:
                        err_msg = repaired_error
                except Exception as repair_error:
                    err_msg = f"{err_msg}; controlled repair failed: {repair_error}"

            if not is_valid_result:
                row["status"] = "invalid"
                row["errors"].append(f"AI title optimization failed quality validation: {err_msg}")
                logger.warning("Video %s title optimization rejected: %s", video_id, err_msg)
                continue

            angles = ai_result["title_angles"]
            opt_title = ai_result.get("recommended_title") or ai_result["title"]
            proposed["title"] = opt_title
            row["proposed_title"] = opt_title
            row["recommended_title"] = opt_title
            row["recommendation_reason"] = ai_result.get("recommendation_reason", "")
            row["title_angles"] = angles
            if ai_result.get("rationale"):
                row["rationale"] = ai_result.get("rationale")
        else:
            proposed["title"] = before.get("title", "")
            row["title_angles"] = []

        # 2. Description handling
        if d_intent == "exact" or (d_intent == "unmentioned" and row.get("description") is not None):
            proposed["description"] = row.get("exact_description") if row.get("exact_description") is not None else row.get("description")
        elif d_intent == "clear":
            proposed["description"] = ""
        elif d_intent == "optimize":
            proposed["description"] = ai_result.get("description", before.get("description", ""))
        else:
            proposed["description"] = before.get("description", "")

        # 3. Tags handling
        if tags_intent == "exact" or (tags_intent == "unmentioned" and row.get("tags") is not None):
            proposed["tags"] = deepcopy(row.get("exact_tags") if row.get("exact_tags") is not None else row.get("tags"))
        elif tags_intent == "clear":
            proposed["tags"] = []
        elif tags_intent == "optimize":
            proposed["tags"] = deepcopy(ai_result.get("tags", before.get("tags", [])))
        else:
            proposed["tags"] = deepcopy(before.get("tags", []))

        if row.get("errors"):
            row["status"] = "invalid"
            continue

        row["before"] = before
        row["proposed"] = proposed
        row["current_title"] = before.get("title", "")
        row["proposed_title"] = proposed.get("title", "")
        row["attempted"] = False

        # Revalidate snippet, compute changed fields, check constraints and digest
        revalidate_batch_row(row)
        logger.info(
            "Staging result for video_id=%s: status=%s, title_intent=%s, valid_angles=%d, changed_fields=%s",
            video_id,
            row.get("status"),
            t_intent,
            len(row.get("title_angles", [])),
            row.get("changed_fields", []),
        )

    parsed_batch["ready_count"] = sum(row.get("status") == "ready" for row in rows)
    parsed_batch["invalid_count"] = sum(row.get("status") == "invalid" for row in rows)
    parsed_batch["no_change_count"] = sum(row.get("status") == "no_change" for row in rows)
    return parsed_batch


def build_results_csv(batch: Dict[str, Any]) -> str:
    """Create an expanded, safe CSV summary report for the completed batch."""
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    headers = [
        "video_id",
        "video_url",
        "current_title",
        "proposed_title",
        "current_description",
        "proposed_description",
        "current_tags",
        "proposed_tags",
        "changed_fields",
        "status",
        "verification_result",
        "details",
    ]
    writer.writerow(headers)

    for row in batch.get("rows", []):
        video_id = row.get("video_id") or ""
        video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        before = row.get("before") or {}
        proposed = row.get("proposed") or {}

        cur_title = before.get("title") or row.get("current_title") or ""
        prop_title = proposed.get("title") or row.get("proposed_title") or row.get("title") or ""

        cur_desc = before.get("description", "")
        prop_desc = proposed.get("description", "")

        cur_tags = "; ".join(before.get("tags") or [])
        prop_tags = "; ".join(proposed.get("tags") or row.get("tags") or [])

        changed = ", ".join(row.get("changed_fields", []))
        status = row.get("status", "")

        verif_result = (
            "Verified" if status == "verified"
            else "Blocked by YouTube" if status == "blocked"
            else "Failed" if status == "failed"
            else "Pending"
        )
        details = "; ".join(row.get("errors", [])) or row.get("result_message", "")

        writer.writerow([
            video_id,
            video_url,
            cur_title,
            prop_title,
            cur_desc,
            prop_desc,
            cur_tags,
            prop_tags,
            changed,
            status,
            verif_result,
            details,
        ])
    return output.getvalue()
