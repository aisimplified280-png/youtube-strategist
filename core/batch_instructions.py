"""Batch natural-language instructions parser and structured plan generator.

Interprets ordinary-language paragraphs, numbered lists, or structured text
describing which YouTube videos to change and what to change.
Uses strict JSON schema validation, safe reference resolution, and error isolation.
"""

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import youtube_analytics_connector as connector
from core.batch_metadata import VIDEO_ID_RE, parse_tags_cell, parse_video_reference


MAX_BATCH_TEXT_LENGTH = 20_000
MAX_BATCH_VIDEOS = 25

# Matches common YouTube URLs and explicit video ID patterns
YOUTUBE_URL_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s\"'>)]+|shorts/[A-Za-z0-9_-]{11}|embed/[A-Za-z0-9_-]{11}|live/[A-Za-z0-9_-]{11})|youtu\.be/[A-Za-z0-9_-]{11}[^\s\"'>)]*)",
    re.IGNORECASE,
)

EXPLICIT_ID_PATTERN = re.compile(
    r"(?:video\s*(?:id|#)?\s*[:=]?\s*|\bid:\s*)([A-Za-z0-9_-]{11})\b",
    re.IGNORECASE,
)


def extract_video_references_from_text(text: str) -> List[Dict[str, Any]]:
    """Extract YouTube video references (URLs, IDs, or named video entries) from text."""
    if not text or not isinstance(text, str):
        return []

    found_refs = []
    seen_ids = set()

    # 1. URL patterns
    for match in YOUTUBE_URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;!?:")
        try:
            vid = parse_video_reference(url)
            found_refs.append({
                "raw_reference": url,
                "video_id": vid,
                "type": "url",
                "start": match.start(),
                "end": match.end(),
            })
            seen_ids.add(vid)
        except ValueError:
            found_refs.append({
                "raw_reference": url,
                "video_id": None,
                "type": "unresolved_url",
                "start": match.start(),
                "end": match.end(),
            })

    # 2. Explicit ID labels like "Video ID: cxsx2ZppQvg" or "id: cxsx2ZppQvg"
    for match in EXPLICIT_ID_PATTERN.finditer(text):
        vid = match.group(1)
        if vid not in seen_ids:
            found_refs.append({
                "raw_reference": vid,
                "video_id": vid,
                "type": "id",
                "start": match.start(),
                "end": match.end(),
            })
            seen_ids.add(vid)

    # Sort by appearance in text
    found_refs.sort(key=lambda r: r["start"])
    return found_refs


def resolve_channel_video_by_title(
    title_query: str,
    channel_videos: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve a video title against the authenticated owner's channel videos.
    
    Returns a dict with 'status' ('resolved', 'ambiguous', 'unresolved') and details.
    """
    clean_query = str(title_query or "").strip().casefold()
    if not clean_query:
        return {
            "status": "unresolved",
            "video_id": None,
            "error": "Empty video title query.",
            "matches": [],
        }

    exact_matches = []
    casefold_matches = []

    for vid in channel_videos:
        v_title = str(vid.get("title", "")).strip()
        v_id = vid.get("id") or vid.get("video_id")
        if not v_id:
            continue
        if v_title == title_query.strip():
            exact_matches.append({"id": v_id, "title": v_title})
        elif v_title.casefold() == clean_query:
            casefold_matches.append({"id": v_id, "title": v_title})

    matches = exact_matches or casefold_matches
    if len(matches) == 1:
        return {
            "status": "resolved",
            "video_id": matches[0]["id"],
            "title": matches[0]["title"],
            "error": None,
            "matches": matches,
        }
    elif len(matches) > 1:
        return {
            "status": "ambiguous",
            "video_id": None,
            "title": title_query,
            "error": f"Title matches {len(matches)} uploads on your channel. Please specify the exact video ID or URL.",
            "matches": matches,
        }
    else:
        return {
            "status": "unresolved",
            "video_id": None,
            "title": title_query,
            "error": f"Could not find a video titled '{title_query}' on your channel.",
            "matches": [],
        }


def parse_instruction_intent_deterministic(instruction: str) -> Dict[str, Any]:
    """Deterministically extract field change intent, exact values, and clearing instructions."""
    text = instruction or ""
    lower = text.casefold()

    title_intent = "unmentioned"
    exact_title = None

    desc_intent = "unmentioned"
    exact_desc = None

    tags_intent = "unmentioned"
    exact_tags = None

    # Title preservation detection
    title_preserves = bool(re.search(
        r"\b(?:keep|preserve|leave|don't change|do not change|without changing)\s+(?:the\s+)?(?:current\s+|existing\s+)?titles?\b|"
        r"\btitles?\s+(?:unchanged|as[- ]is|intact|remain unchanged|should remain unchanged)\b|"
        r"\b(?:keep|preserve)\s+all\s+fields\b",
        lower,
    ))

    # Exact title extraction: e.g. change the title to '...' or title: "..."
    title_match = re.search(
        r"(?:change|set|update)?\s*(?:the\s+)?title\s*(?:to|:|=)\s*['\"]([^'\"]+)['\"]",
        text,
        re.IGNORECASE,
    )
    if title_match:
        title_intent = "exact"
        exact_title = title_match.group(1).strip()
    elif title_preserves:
        title_intent = "preserve"
    elif re.search(r"\b(?:improve|optimize|rewrite|generate|craft|new)\s+(?:the\s+)?titles?\b", lower):
        title_intent = "optimize"

    # Description detection
    # Check for explicit clearing: empty description
    if re.search(r"\b(?:clear|empty|remove|delete)\s+(?:the\s+)?descriptions?\b", lower):
        desc_intent = "clear"
        exact_desc = ""
    else:
        # Check for exact description in quotes or code block
        desc_exact_match = re.search(
            r"(?:change|set|update)?\s*(?:the\s+)?description\s*(?:to|:|=)\s*['\"]{1,3}([\s\S]*?)['\"]{1,3}",
            text,
            re.IGNORECASE,
        )
        if desc_exact_match and desc_exact_match.group(1).strip():
            desc_intent = "exact"
            exact_desc = desc_exact_match.group(1).strip()
        elif bool(re.search(
            r"\b(?:keep|preserve|leave|don't change|do not change)\s+(?:the\s+)?(?:current\s+|existing\s+)?descriptions?\b|"
            r"\bdescriptions?\s+(?:unchanged|as[- ]is|intact)\b|"
            r"\b(?:keep|preserve)\s+all\s+fields\b",
            lower,
        )):
            desc_intent = "preserve"
        elif re.search(r"\b(?:improve|optimize|rewrite|update|craft|new)\b[\s\S]{0,30}?\bdescriptions?\b|\bdescriptions?\b[\s\S]{0,30}?\b(?:improve|optimize|update)\b", lower):
            desc_intent = "optimize"

    # Tags detection
    # Check for explicit clearing: e.g. "tags: []" or "clear tags"
    if re.search(r"\b(?:clear|empty|remove|delete)\s+(?:all\s+)?(?:the\s+)?tags\b", lower) or "tags: []" in lower or "tags:[]" in lower:
        tags_intent = "clear"
        exact_tags = []
    else:
        # Check for exact tags e.g. tags: tag1, tag2, tag3 or use these tags: a, b, c
        tags_match = re.search(
            r"(?:use\s+(?:these\s+)?tags|tags\s*[:=]|set\s+tags\s+to)\s*[:=]?\s*(\[[^\]]*\]|[^.\n]+)",
            text,
            re.IGNORECASE,
        )
        if tags_match:
            raw_tags = tags_match.group(1).strip().rstrip(".,;")
            try:
                parsed_t = parse_tags_cell(raw_tags)
                if parsed_t is not None:
                    tags_intent = "exact"
                    exact_tags = parsed_t
            except ValueError:
                pass

        if tags_intent == "unmentioned":
            if bool(re.search(
                r"\b(?:keep|preserve|leave|don't change|do not change)\s+(?:the\s+)?(?:current\s+|existing\s+)?tags\b|"
                r"\btags\s+(?:unchanged|as[- ]is|intact)\b|"
                r"\b(?:keep|preserve)\s+all\s+fields\b",
                lower,
            )):
                tags_intent = "preserve"
            elif re.search(r"\b(?:improve|optimize|update|generate|new)\b[\s\S]{0,30}?\btags\b|\btags\b[\s\S]{0,30}?\b(?:improve|optimize|update)\b", lower):
                tags_intent = "optimize"

    fields_requested = []
    if title_intent in {"exact", "optimize"}:
        fields_requested.append("title")
    if desc_intent in {"exact", "optimize", "clear"}:
        fields_requested.append("description")
    if tags_intent in {"exact", "optimize", "clear"}:
        fields_requested.append("tags")

    return {
        "title_intent": title_intent,
        "exact_title": exact_title,
        "description_intent": desc_intent,
        "exact_description": exact_desc,
        "tags_intent": tags_intent,
        "exact_tags": exact_tags,
        "fields_requested": fields_requested,
    }


def split_text_into_video_sections(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Split batch instructions text into shared instructions and individual video sections."""
    lines = text.splitlines()
    shared_lines = []
    video_sections = []

    # Regex for section headers like: "Video 1:", "Video 2:", "1.", "2.", "Video https://..."
    section_header_re = re.compile(
        r"^(?:(?:video\s*\d+|video\s*[A-Za-z0-9_-]{11}|\d+[\.\)])\s*[:\-]?\s*|\bfor\s+video\s+)",
        re.IGNORECASE,
    )

    current_section = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_section:
                current_section["lines"].append(line)
            continue

        # Check if line indicates start of a video section
        is_header = bool(section_header_re.match(stripped)) or YOUTUBE_URL_PATTERN.search(stripped)
        # But if it's in the middle and no section started yet, treat as shared or first section
        has_url_or_id = bool(YOUTUBE_URL_PATTERN.search(stripped) or EXPLICIT_ID_PATTERN.search(stripped))

        if is_header and has_url_or_id:
            if current_section:
                video_sections.append(current_section)
            current_section = {
                "header": stripped,
                "lines": [line],
            }
        elif current_section:
            current_section["lines"].append(line)
        else:
            shared_lines.append(line)

    if current_section:
        video_sections.append(current_section)

    shared_text = "\n".join(shared_lines).strip()
    return shared_text, video_sections


def validate_batch_instructions_schema(data: Any) -> Tuple[bool, List[str]]:
    """Validate structured batch instruction schema."""
    errors = []
    if not isinstance(data, dict):
        return False, ["Batch instruction response must be a JSON object."]

    items = data.get("videos")
    if not isinstance(items, list):
        return False, ["Response missing required 'videos' list."]

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"Item #{i+1} must be an object.")
            continue
        ref = item.get("reference")
        if not ref or not isinstance(ref, str):
            errors.append(f"Item #{i+1} missing valid 'reference' string.")

        t_intent = item.get("title_intent")
        if t_intent not in {"exact", "optimize", "preserve", "unmentioned", None}:
            errors.append(f"Item #{i+1} has invalid title_intent: {t_intent}")

        d_intent = item.get("description_intent")
        if d_intent not in {"exact", "optimize", "preserve", "clear", "unmentioned", None}:
            errors.append(f"Item #{i+1} has invalid description_intent: {d_intent}")

        tags_intent = item.get("tags_intent")
        if tags_intent not in {"exact", "optimize", "preserve", "clear", "unmentioned", None}:
            errors.append(f"Item #{i+1} has invalid tags_intent: {tags_intent}")

        if item.get("exact_tags") is not None and not isinstance(item.get("exact_tags"), list):
            errors.append(f"Item #{i+1} exact_tags must be an array of strings or null.")

    return len(errors) == 0, errors


def parse_batch_instructions(
    text: str,
    channel_videos: Optional[List[Dict[str, Any]]] = None,
    groq_key: Optional[str] = None,
    groq_model: Optional[str] = None,
    gemini_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Main entrypoint to parse natural-language batch instructions into a structured batch plan.
    
    Returns a dictionary containing:
      - 'raw_text': str
      - 'videos': list of structured video instruction dicts
      - 'shared_instructions': str
      - 'total_identified': int
      - 'errors': list of batch-level errors
    """
    if not text or not isinstance(text, str):
        raise ValueError("Please provide batch instructions text.")

    if len(text) > MAX_BATCH_TEXT_LENGTH:
        raise ValueError(
            f"Batch instructions text exceeds maximum allowed length of {MAX_BATCH_TEXT_LENGTH:,} characters. "
            f"(Supplied {len(text):,} characters). Please split into smaller batches."
        )

    channel_videos = channel_videos or []
    shared_text, video_sections = split_text_into_video_sections(text)
    shared_intent = parse_instruction_intent_deterministic(shared_text)

    # 1. First, extract all direct video references from the text
    refs = extract_video_references_from_text(text)

    # If there are video sections, process each section
    parsed_videos: List[Dict[str, Any]] = []
    seen_references: Dict[str, Dict[str, Any]] = {}

    if video_sections:
        for section in video_sections:
            sec_text = "\n".join(section["lines"])
            sec_refs = extract_video_references_from_text(sec_text)
            sec_intent = parse_instruction_intent_deterministic(sec_text)

            # Fallback to shared intent if field unmentioned in section
            final_title_intent = sec_intent["title_intent"] if sec_intent["title_intent"] != "unmentioned" else shared_intent["title_intent"]
            final_exact_title = sec_intent["exact_title"] if sec_intent["exact_title"] is not None else shared_intent["exact_title"]

            final_desc_intent = sec_intent["description_intent"] if sec_intent["description_intent"] != "unmentioned" else shared_intent["description_intent"]
            final_exact_desc = sec_intent["exact_description"] if sec_intent["exact_description"] is not None else shared_intent["exact_description"]

            final_tags_intent = sec_intent["tags_intent"] if sec_intent["tags_intent"] != "unmentioned" else shared_intent["tags_intent"]
            final_exact_tags = sec_intent["exact_tags"] if sec_intent["exact_tags"] is not None else shared_intent["exact_tags"]

            # Target reference
            if sec_refs:
                primary_ref = sec_refs[0]
                raw_ref = primary_ref["raw_reference"]
                vid = primary_ref["video_id"]
            else:
                raw_ref = section["header"]
                vid = None

            video_entry = {
                "reference": raw_ref,
                "video_id": vid,
                "title_query": None,
                "original_instruction": sec_text.strip(),
                "title_intent": final_title_intent,
                "exact_title": final_exact_title,
                "description_intent": final_desc_intent,
                "exact_description": final_exact_desc,
                "tags_intent": final_tags_intent,
                "exact_tags": final_exact_tags,
                "fields_requested": [
                    f for f in ("title", "description", "tags")
                    if (f == "title" and final_title_intent in {"exact", "optimize"})
                    or (f == "description" and final_desc_intent in {"exact", "optimize", "clear"})
                    or (f == "tags" and final_tags_intent in {"exact", "optimize", "clear"})
                ],
                "is_ambiguous": False,
                "is_unresolved": False,
                "conflict_detected": False,
                "errors": [],
            }
            parsed_videos.append(video_entry)
    elif refs:
        # Multiple refs listed with shared instructions
        for ref in refs:
            video_entry = {
                "reference": ref["raw_reference"],
                "video_id": ref["video_id"],
                "title_query": None,
                "original_instruction": shared_text,
                "title_intent": shared_intent["title_intent"],
                "exact_title": shared_intent["exact_title"],
                "description_intent": shared_intent["description_intent"],
                "exact_description": shared_intent["exact_description"],
                "tags_intent": shared_intent["tags_intent"],
                "exact_tags": shared_intent["exact_tags"],
                "fields_requested": list(shared_intent["fields_requested"]),
                "is_ambiguous": False,
                "is_unresolved": False,
                "conflict_detected": False,
                "errors": [],
            }
            parsed_videos.append(video_entry)
    else:
        # Check if references are exact titles
        # Lines starting with quotes or video names
        title_candidates = [l.strip().strip("-*•").strip() for l in text.splitlines() if l.strip()]
        for candidate in title_candidates:
            if candidate.startswith("http") or len(candidate) < 3:
                continue
            res = resolve_channel_video_by_title(candidate, channel_videos)
            if res["status"] in {"resolved", "ambiguous"}:
                parsed_videos.append({
                    "reference": candidate,
                    "video_id": res["video_id"],
                    "title_query": candidate,
                    "original_instruction": shared_text,
                    "title_intent": shared_intent["title_intent"],
                    "exact_title": shared_intent["exact_title"],
                    "description_intent": shared_intent["description_intent"],
                    "exact_description": shared_intent["exact_description"],
                    "tags_intent": shared_intent["tags_intent"],
                    "exact_tags": shared_intent["exact_tags"],
                    "fields_requested": list(shared_intent["fields_requested"]),
                    "is_ambiguous": res["status"] == "ambiguous",
                    "is_unresolved": False,
                    "conflict_detected": False,
                    "errors": [res["error"]] if res.get("error") else [],
                })

    # If no videos could be extracted at all
    if not parsed_videos:
        raise ValueError("No video links, IDs, or recognizable channel titles were found in the batch instructions.")

    # Check 25 video limit
    if len(parsed_videos) > MAX_BATCH_VIDEOS:
        raise ValueError(
            f"This batch contains more than {MAX_BATCH_VIDEOS} videos ({len(parsed_videos)} identified). "
            "Please split your instructions into smaller batches."
        )

    # Resolve references and check for duplicates / conflicts
    deduped_videos: List[Dict[str, Any]] = []
    seen_by_id: Dict[str, Dict[str, Any]] = {}

    for row in parsed_videos:
        # If video_id not yet resolved, try resolving by URL or channel title
        if not row["video_id"] and row["reference"]:
            try:
                row["video_id"] = parse_video_reference(row["reference"])
            except ValueError:
                if channel_videos:
                    res = resolve_channel_video_by_title(row["reference"], channel_videos)
                    if res["status"] == "resolved":
                        row["video_id"] = res["video_id"]
                    elif res["status"] == "ambiguous":
                        row["is_ambiguous"] = True
                        row["errors"].append(res["error"])
                    else:
                        row["is_unresolved"] = True
                        row["errors"].append(f"Unresolved reference: '{row['reference']}'")
                else:
                    row["is_unresolved"] = True
                    row["errors"].append(f"Invalid YouTube URL or ID: '{row['reference']}'")

        vid = row["video_id"]
        if vid:
            if vid in seen_by_id:
                existing = seen_by_id[vid]
                # Check for conflicting instructions
                if (
                    existing["title_intent"] != row["title_intent"]
                    or existing["exact_title"] != row["exact_title"]
                    or existing["description_intent"] != row["description_intent"]
                    or existing["exact_description"] != row["exact_description"]
                    or existing["tags_intent"] != row["tags_intent"]
                    or existing["exact_tags"] != row["exact_tags"]
                ):
                    existing["conflict_detected"] = True
                    row["conflict_detected"] = True
                    err_msg = f"Conflicting instructions detected for duplicate video ID {vid}."
                    if err_msg not in existing["errors"]:
                        existing["errors"].append(err_msg)
                    row["errors"].append(err_msg)
                else:
                    # Harmless duplicate reference, note deduplication
                    pass
                deduped_videos.append(row)
            else:
                seen_by_id[vid] = row
                deduped_videos.append(row)
        else:
            deduped_videos.append(row)

    return {
        "raw_text": text,
        "shared_instructions": shared_text,
        "videos": deduped_videos,
        "total_identified": len(deduped_videos),
        "errors": [],
    }
