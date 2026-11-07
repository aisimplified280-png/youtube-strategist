"""Spreadsheet and text-based batch metadata staging and publishing.

Shares one unified pipeline for Excel/CSV/TSV imports and natural-language text instructions.
Enforces strict ownership checks, conditional title angles, fail-closed approval,
sequential publishing, and post-write verification.
"""

import csv
import hashlib
import io
import json
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from openpyxl import load_workbook

import youtube_analytics_connector as connector


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
            "description_intent": item.get("description_intent", "unmentioned"),
            "exact_description": item.get("exact_description"),
            "tags_intent": item.get("tags_intent", "unmentioned"),
            "exact_tags": item.get("exact_tags"),
            "fields_requested": item.get("fields_requested", []),
            "original_instruction": item.get("original_instruction", ""),
            "title_angles": [],
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

        # 1. Title handling
        t_intent = row.get("title_intent")
        if t_intent == "exact" or (t_intent == "unmentioned" and row.get("title") is not None):
            proposed["title"] = row.get("exact_title") or row.get("title") or before.get("title", "")
            row["title_angles"] = []
        elif t_intent == "optimize":
            # AI title optimization requested
            if ai_generator:
                try:
                    ai_result = ai_generator(video_id=video_id, before=before, instruction=row.get("original_instruction", ""))
                    if isinstance(ai_result, dict):
                        proposed["title"] = ai_result.get("title", before.get("title", ""))
                        row["title_angles"] = ai_result.get("title_angles", [])
                        if ai_result.get("rationale"):
                            row["rationale"] = ai_result.get("rationale", [])
                except Exception as err:
                    row["errors"].append(f"AI title optimization failed: {err}")
                    row["status"] = "invalid"
            else:
                # Fallback if no generator provided
                pass
        else:
            # Preserved
            proposed["title"] = before.get("title", "")
            row["title_angles"] = []

        # 2. Description handling
        d_intent = row.get("description_intent")
        if d_intent == "exact" or (d_intent == "unmentioned" and row.get("description") is not None):
            proposed["description"] = row.get("exact_description") if row.get("exact_description") is not None else row.get("description")
        elif d_intent == "clear":
            proposed["description"] = ""
        elif d_intent == "optimize":
            if ai_generator:
                try:
                    ai_result = ai_generator(video_id=video_id, before=before, instruction=row.get("original_instruction", ""))
                    if isinstance(ai_result, dict) and ai_result.get("description"):
                        proposed["description"] = ai_result.get("description")
                except Exception as err:
                    row["errors"].append(f"AI description optimization failed: {err}")
                    row["status"] = "invalid"
        else:
            proposed["description"] = before.get("description", "")

        # 3. Tags handling
        tags_intent = row.get("tags_intent")
        if tags_intent == "exact" or (tags_intent == "unmentioned" and row.get("tags") is not None):
            proposed["tags"] = deepcopy(row.get("exact_tags") if row.get("exact_tags") is not None else row.get("tags"))
        elif tags_intent == "clear":
            proposed["tags"] = []
        elif tags_intent == "optimize":
            if ai_generator:
                try:
                    ai_result = ai_generator(video_id=video_id, before=before, instruction=row.get("original_instruction", ""))
                    if isinstance(ai_result, dict) and ai_result.get("tags"):
                        proposed["tags"] = ai_result.get("tags")
                except Exception as err:
                    row["errors"].append(f"AI tags optimization failed: {err}")
                    row["status"] = "invalid"
        else:
            proposed["tags"] = deepcopy(before.get("tags", []))

        if row.get("errors"):
            row["status"] = "invalid"
            continue

        changed_fields = [
            field for field in ("title", "description", "tags")
            if before.get(field, [] if field == "tags" else "") != proposed.get(field, [] if field == "tags" else "")
        ]
        if not changed_fields and not row.get("errors"):
            row["status"] = "no_change"
            row["errors"].append("The proposed values are identical to the current YouTube metadata.")
            continue

        try:
            connector._validate_metadata(proposed)
        except connector.MetadataValidationError as error:
            row["status"] = "invalid"
            row["errors"].append(str(error))
            continue

        row["before"] = before
        row["proposed"] = proposed
        row["changed_fields"] = changed_fields
        row["digest"] = connector.metadata_proposal_digest(video_id, before, proposed)
        row["current_title"] = before.get("title", "")
        row["proposed_title"] = proposed.get("title", "")
        row["status"] = "ready"
        row["attempted"] = False

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
