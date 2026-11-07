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
    """Validate that title angles are distinct, under 100 chars, and not merely typographical variations."""
    if not isinstance(angles, list) or len(angles) < 3:
        return False, "AI title optimization did not produce the required 3 title angles (curiosity, value, search)."

    norm_current = normalize_title_for_comparison(cur_title)
    norm_angles = []
    seen_angles = set()

    for idx, angle in enumerate(angles):
        if not isinstance(angle, dict):
            return False, f"Title angle #{idx+1} is not a valid dictionary."
        t = str(angle.get("title", "")).strip()
        if not t:
            return False, f"Title angle #{idx+1} has an empty title."
        if len(t) > 100:
            return False, f"Title angle #{idx+1} exceeds 100 characters ({len(t)} chars): '{t[:40]}...'"
        norm_t = normalize_title_for_comparison(t)
        if norm_current and norm_t == norm_current:
            return False, f"Generated title '{t}' is merely a trivial punctuation, emoji, or casing change of current title '{cur_title}'."
        if norm_t in seen_angles:
            return False, f"Generated title angle '{t}' duplicates another generated angle."
        seen_angles.add(norm_t)
        norm_angles.append(norm_t)

    return True, ""


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
            angles = ai_result.get("title_angles")
            is_valid_angles = True
            err_msg = ""
            if angles is not None:
                is_valid_angles, err_msg = validate_title_angles_quality(before.get("title", ""), angles)
                if not is_valid_angles and ai_generator:
                    # One controlled repair attempt
                    try:
                        logger.info("Performing controlled repair attempt for video_id=%s: %s", video_id, err_msg)
                        repair_kwargs = deepcopy(gen_kwargs)
                        repair_kwargs["instruction"] = (
                            f"{row.get('original_instruction', '')}\n\n"
                            f"CONTROLLED REPAIR ATTEMPT: The proposed title angles were rejected: {err_msg}.\n"
                            f"Generate 3 genuinely distinct title angles with different wording from '{before.get('title', '')}':\n"
                            "1. Curiosity / Intrigue, 2. Direct Value / Outcome, 3. Search / SEO."
                        )
                        repaired_result = ai_generator(**repair_kwargs)
                        if isinstance(repaired_result, dict) and repaired_result.get("title_angles"):
                            r_valid, r_err = validate_title_angles_quality(before.get("title", ""), repaired_result.get("title_angles", []))
                            if r_valid:
                                ai_result = repaired_result
                                angles = repaired_result.get("title_angles")
                                is_valid_angles = True
                            else:
                                err_msg = r_err
                    except Exception as r_exc:
                        err_msg = f"{err_msg}; repair attempt failed: {r_exc}"

            if not is_valid_angles:
                row["status"] = "invalid"
                row["errors"].append(f"AI title optimization failed quality validation: {err_msg}")
                logger.warning("Video %s title angle validation failed: %s", video_id, err_msg)
                continue

            opt_title = ai_result.get("recommended_title") or ai_result.get("title") or ""
            # If the recommended title is identical to the current title after normalization, pick the first angle that differs
            if normalize_title_for_comparison(opt_title) == normalize_title_for_comparison(before.get("title", "")) and angles:
                for ang in angles:
                    if normalize_title_for_comparison(ang.get("title", "")) != normalize_title_for_comparison(before.get("title", "")):
                        opt_title = ang.get("title", "")
                        break

            proposed["title"] = opt_title
            row["proposed_title"] = opt_title
            row["recommended_title"] = opt_title
            row["recommendation_reason"] = ai_result.get("recommendation_reason") or ai_result.get("rationale") or ""
            row["title_angles"] = angles or []
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

        verif_result = "Verified" if status == "verified" else ("Failed" if status == "failed" else "Pending")
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
