"""Batch natural-language instructions parser and structured plan generator.

Interprets ordinary-language paragraphs, numbered lists, or structured text
describing which YouTube videos to change and what to change.
Uses Groq/Gemini structured interpretation with strict JSON schema validation,
fallback recovery, and deterministic reference reconciliation.
"""

import copy
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

import requests

import youtube_analytics_connector as connector
from core.batch_metadata import VIDEO_ID_RE, parse_tags_cell, parse_video_reference


MAX_BATCH_TEXT_LENGTH = 20_000
MAX_BATCH_VIDEOS = 25

GROQ_CHAT_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

YOUTUBE_URL_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s\"'>)]+|shorts/[A-Za-z0-9_-]{11}|embed/[A-Za-z0-9_-]{11}|live/[A-Za-z0-9_-]{11})|youtu\.be/[A-Za-z0-9_-]{11}[^\s\"'>)]*)",
    re.IGNORECASE,
)

EXPLICIT_ID_PATTERN = re.compile(
    r"(?:video\s*(?:id|#)?\s*[:=]?\s*|\bid:\s*)([A-Za-z0-9_-]{11})\b",
    re.IGNORECASE,
)

LIST_OR_BARE_ID_PATTERN = re.compile(
    # Bare IDs are only accepted as a complete line (optionally numbered/bulleted),
    # or as an ID followed by an explicit separator and descriptive title text.
    # Anchoring prevents ordinary 11-character words such as "Exact-Match" in a
    # video title from being misidentified as an additional YouTube video ID.
    r"^[ \t]*(?:(?:\d+[\.\)]|[-*•]|\[\d+\])[ \t]*)?(?:video\s*(?:id|#)?\s*[:=]?\s*|\bid:\s*)?([A-Za-z0-9_-]{11})\b(?:[ \t]*(?:[—\-:][ \t]*[^\r\n]*)?)?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

EXCLUDED_ID_TOKENS = {
    "description", "instruction", "suggestions", "conclusions", "preferences",
    "information", "replacement", "categorized", "spreadsheet", "performance",
    "programming", "development", "placeholder", "optimization", "interactive",
    "application", "transformer", "engineering", "environment", "architecture",
    "distributed", "reinforce", "integration",
}


def _is_probable_video_id(token: str) -> bool:
    """Determine if an 11-char token is a likely base64 YouTube video ID rather than an English word."""
    if len(token) != 11 or not VIDEO_ID_RE.fullmatch(token):
        return False
    if token.lower() in EXCLUDED_ID_TOKENS:
        return False
    # Hyphenated or underscored English words without digits (e.g. "Exact-Match", "multi-agent")
    if "-" in token or "_" in token:
        parts = re.split(r"[-_]", token)
        if not any(c.isdigit() for c in token) and all(
            p.isalpha() and (p.islower() or p.istitle() or p.isupper())
            for p in parts if p
        ):
            return False
    # Title-cased alphabetical English words without digits (e.g. "Placeholder", "Development")
    if token.istitle() and token.isalpha():
        return False
    # If it contains digits, it has high entropy distinguishing it from normal English words
    if any(c.isdigit() for c in token):
        return True
    # If it contains underscores or dashes alongside mixed-case letters
    if any(c in "_-" for c in token):
        return True
    # If it contains interior uppercase letters with sufficient casing entropy
    # (e.g. JlxukCkusZc has uppercase 'J', 'C', 'Z' and lowercase 'lxuk', 'kus', 'c')
    upper_count = sum(1 for c in token if c.isupper())
    if upper_count >= 2 and any(c.isupper() for c in token[1:]):
        return True
    # Isolated all-lowercase non-dictionary candidate IDs (e.g. bare test ID abcdefghijk)
    if token.islower() and token.isalpha() and token not in EXCLUDED_ID_TOKENS:
        return True
    return False


def extract_video_references_from_text(text: str) -> List[Dict[str, Any]]:
    """Extract all YouTube video references (URLs, IDs) from text with their positions."""
    if not text or not isinstance(text, str):
        return []

    found_refs = []
    seen_spans = set()

    def _add_ref(raw_ref: str, vid: Optional[str], ref_type: str, start: int, end: int):
        span = (start, end)
        if any(s[0] <= start and end <= s[1] for s in seen_spans):
            return
        seen_spans.add(span)
        found_refs.append({
            "raw_reference": raw_ref,
            "video_id": vid,
            "type": ref_type,
            "start": start,
            "end": end,
        })

    # 1. YouTube URLs
    for match in YOUTUBE_URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;!?:")
        try:
            vid = parse_video_reference(url)
            _add_ref(url, vid, "url", match.start(), match.end())
        except ValueError:
            _add_ref(url, None, "unresolved_url", match.start(), match.end())

    # 2. Explicit ID labels (e.g. "VIDEO ID: JlxukCkusZc", "id: JlxukCkusZc")
    for match in EXPLICIT_ID_PATTERN.finditer(text):
        vid = match.group(1)
        if vid.lower() not in EXCLUDED_ID_TOKENS:
            _add_ref(vid, vid, "id", match.start(1), match.end(1))

    # 3. Labeled or separated IDs (e.g. "JlxukCkusZc CURRENT TITLE:", "JlxukCkusZc - title")
    field_pattern = re.compile(
        r"\b([A-Za-z0-9_-]{11})\b\s*(?:[-—:]|current\s+title|new\s+title|title\s*:|description\s*:|tags\s*:)",
        re.IGNORECASE,
    )
    for match in field_pattern.finditer(text):
        vid = match.group(1)
        if _is_probable_video_id(vid):
            _add_ref(vid, vid, "id", match.start(1), match.end(1))

    # 4. Line-anchored bare IDs
    for match in LIST_OR_BARE_ID_PATTERN.finditer(text):
        vid = match.group(1)
        if _is_probable_video_id(vid):
            _add_ref(vid, vid, "id", match.start(1), match.end(1))

    # 5. Probable YouTube IDs in lists or paragraphs (e.g. "JlxukCkusZc and uBH2f6sKZcc", "JlxukCkusZc, uBH2f6sKZcc")
    list_pattern = re.compile(
        r"(?:^|[\s,;*•\[\]\(\)])(?:\d+[\.\)]\s*)?([A-Za-z0-9_-]{11})(?=(?:[\s,;*•\[\]\(\)]|$))",
    )
    for match in list_pattern.finditer(text):
        vid = match.group(1)
        if _is_probable_video_id(vid):
            _add_ref(vid, vid, "id", match.start(1), match.end(1))

    found_refs.sort(key=lambda r: r["start"])
    return found_refs


def resolve_channel_video_by_title(
    title_query: str,
    channel_videos: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve a video title against the authenticated owner's channel uploads.
    
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
            exact_matches.append({"id": v_id, "title": v_title, "publishedAt": vid.get("publishedAt", "")})
        elif v_title.casefold() == clean_query:
            casefold_matches.append({"id": v_id, "title": v_title, "publishedAt": vid.get("publishedAt", "")})

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
        match_descriptions = [f"ID {m['id']} ('{m['title']}', published {m.get('publishedAt', 'N/A')[:10]})" for m in matches]
        details = "; ".join(match_descriptions)
        return {
            "status": "ambiguous",
            "video_id": None,
            "title": title_query,
            "error": f"Title matches {len(matches)} uploads on your channel: {details}. Specify the exact video ID or URL.",
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


def extract_field_values_and_intents(text: str) -> Dict[str, Any]:
    """Deterministically extract field change intent, exact values, and clearing instructions.
    
    Robust against apostrophes, multiline descriptions, decimal tags, JSON tag arrays, and quotes.
    """
    text_content = str(text or "")
    lower = text_content.casefold()

    title_intent = "unmentioned"
    exact_title = None

    desc_intent = "unmentioned"
    exact_desc = None

    tags_intent = "unmentioned"
    exact_tags = None

    NEXT_FIELD_LOOKAHEAD = (
        r"(?=\s*(?:"
        r"\b(?:new|proposed|current|existing|original|target|old)\s+titles?\s*[:=]|"
        r"(?<!current\s)(?<!existing\s)(?<!original\s)(?<!target\s)(?<!old\s)\btitles?\s*[:=]|"
        r"\bdescriptions?\s*[:=]|"
        r"\btags\s*[:=]|"
        r"\b(?:video|target|reference)\s*(?:id|#)?\s*[:=]|"
        r"[\r\n]|$"
        r"))"
    )

    # 1. Title intent & extraction
    context_title = None
    # Identify context/current title labels:
    # Lines labelled "Current title:", "Existing title:", or "<ID> — <Title>" provide
    # existing metadata for context. They must NOT be interpreted as exact replacement titles.
    ctx_match = re.search(
        r"\b(?:current|existing|original|target|old)\s+titles?\s*[:=]\s*['\"“”]{0,3}(.+?)(?:['\"“”]{0,3}\s*" + NEXT_FIELD_LOOKAHEAD + r")",
        text_content,
        re.IGNORECASE,
    )
    if not ctx_match:
        ctx_match = re.search(
            r"\b(?:current|existing|original|target|old)\s+titles?\s*[:=]\s*([^\n\r]+)",
            text_content,
            re.IGNORECASE,
        )
    if ctx_match:
        context_title = ctx_match.group(1).strip().strip("'\"“”")
    else:
        dash_match = re.search(
            r"\b[A-Za-z0-9_-]{11}\s*[—\-]\s*([^\n\r]+)",
            text_content,
        )
        if dash_match:
            cand = dash_match.group(1).strip().strip("'\"“”")
            if not re.search(r"\b(?:change|set|update|optimize|improve|rewrite)\b", cand, re.IGNORECASE):
                context_title = cand

    # Check for explicit title-only requests
    title_only = bool(re.search(
        r"\b(?:optimize|improve|change|update)\s+(?:the\s+)?titles?\s+only\b|"
        r"\btitles?\s+only\b|"
        r"\bonly\s+(?:the\s+)?titles?\b|"
        r"\boptimize\s+titles?\b[^\.\n\r;]{0,50}?\b(?:preserve|keep)\s+(?:the\s+)?(?:descriptions?|tags?|both)\b",
        lower,
    ))

    # Check for title preservation intent
    title_preserves = bool(re.search(
        r"\b(?:keep|preserve|leave|don't change|do not change|without changing)\s+(?:the\s+)?(?:current\s+|existing\s+)?titles?\b|"
        r"\btitles?\s+(?:unchanged|as[- ]is|intact|remain unchanged|should remain unchanged)\b|"
        r"\b(?:keep|preserve)\s+all\s+fields\b",
        lower,
    ))

    # Check for title optimization intent
    optimize_title_match = bool(re.search(
        r"\b(?:optimize|improve|rewrite|generate|craft|suggest|recommend|new|better)\s+(?:the\s+)?(?:video\s+)?titles?\b|"
        r"\btitles?\s+(?:to\s+be\s+)?(?:optimized|improved|rewritten|generated)\b|"
        r"\b(?:3|three)\s+(?:different\s+)?(?:title\s+)?angles\b|"
        r"\brecommend\s+(?:the\s+)?strongest\s+title\b",
        lower,
    ))

    # Check for exact title: triple quotes, double/single quotes, or labeled Title (excluding Current title)
    exact_title_match = re.search(
        r"(?:change|set|update|rename)\s+(?:the\s+)?title\s*(?:to|:|=)\s*['\"“”]{1,3}([\s\S]*?)['\"“”]{1,3}",
        text_content,
        re.IGNORECASE,
    )
    if not exact_title_match:
        exact_title_match = re.search(
            r"\b(?:new|proposed)\s+title\s*[:=]\s*['\"“”]{0,3}(.+?)(?:['\"“”]{0,3}\s*" + NEXT_FIELD_LOOKAHEAD + r")",
            text_content,
            re.IGNORECASE,
        )
    if not exact_title_match and not context_title:
        bare_title_match = re.search(
            r"(?<!current\s)(?<!existing\s)(?<!original\s)(?<!target\s)(?<!old\s)\bTitle\s*:\s*['\"“”]{0,3}(.+?)(?:['\"“”]{0,3}\s*" + NEXT_FIELD_LOOKAHEAD + r")",
            text_content,
            re.IGNORECASE,
        )
        if bare_title_match:
            exact_title_match = bare_title_match

    if title_preserves:
        title_intent = "preserve"
        exact_title = None
    elif exact_title_match and exact_title_match.group(1).strip():
        cand = exact_title_match.group(1).strip().strip("'\"“”")
        if not re.search(r"\b(?:optimize|improve|rewrite|keep|preserve)\b", cand, re.IGNORECASE) and cand != context_title:
            title_intent = "exact"
            exact_title = cand
        elif optimize_title_match or title_only:
            title_intent = "optimize"
            exact_title = None
    elif optimize_title_match or title_only:
        title_intent = "optimize"
        exact_title = None

    # 2. Description intent & extraction
    if re.search(r"\b(?:clear|empty|remove|delete)\s+(?:the\s+)?descriptions?\b", lower) or re.search(r"\bdescription\s*:\s*(?:""(?!")|''(?!'))", lower):
        desc_intent = "clear"
        exact_desc = ""
    else:
        # Check triple quotes, or quotes, or multiline block
        desc_triple = re.search(
            # Use an explicit delimiter alternative so triple-quoted blocks are
            # not confused with the single-quote fallback below.
            r"(?:change|set|update)?\s*(?:the\s+)?description\s*(?:to|:|=)\s*(?P<quote>\"\"\"|''')([\s\S]*?)(?P=quote)",
            text_content,
            re.IGNORECASE,
        )
        desc_quoted = re.search(
            r"(?:change|set|update)?\s*(?:the\s+)?description\s*(?:to|:|=)\s*[\"']([\s\S]*?)[\"']",
            text_content,
            re.IGNORECASE,
        )
        desc_block = re.search(
            # A description is a free-form, multi-paragraph field. Do not stop at
            # blank lines: that silently drops most of a description. Stop only
            # when the next explicitly labelled metadata field begins.
            r"\bDescription\s*:\s*([\s\S]+?)(?=\s*(?:\bTags\s*[:=]|\bTitle\s*[:=]|\bNew\s+title\s*[:=]|\bVideo\s*(?:id|\d+)\s*[:=]|$))",
            text_content,
            re.IGNORECASE,
        )

        if desc_triple and desc_triple.group(2).strip():
            desc_intent = "exact"
            exact_desc = desc_triple.group(2).strip()
        elif desc_quoted and desc_quoted.group(1).strip():
            desc_intent = "exact"
            exact_desc = desc_quoted.group(1).strip()
        elif desc_block and desc_block.group(1).strip() and not re.search(r"^(?:improve|optimize|keep|preserve)\b", desc_block.group(1).strip(), re.I):
            desc_intent = "exact"
            exact_desc = desc_block.group(1).strip()
        elif bool(re.search(
            r"\b(?:keep|preserve|leave|don't change|do not change)\s+(?:the\s+)?(?:current\s+|existing\s+)?descriptions?\b|"
            r"\bdescriptions?\s+(?:unchanged|as[- ]is|intact)\b|"
            r"\b(?:keep|preserve)\s+all\s+fields\b",
            lower,
        )):
            desc_intent = "preserve"
        elif re.search(r"\b(?:improve|optimize|rewrite|update|craft|new)\b[\s\S]{0,30}?\bdescriptions?\b|\bdescriptions?\b[\s\S]{0,30}?\b(?:improve|optimize|update)\b", lower):
            desc_intent = "optimize"

    # 3. Tags intent & extraction
    if re.search(r"\b(?:clear|empty|remove|delete)\s+(?:all\s+)?(?:the\s+)?tags\b", lower) or "tags: []" in lower or "tags:[]" in lower:
        tags_intent = "clear"
        exact_tags = []
    else:
        # Check JSON array first
        tags_json_match = re.search(r"(?:tags\s*[:=]|use\s+tags\s*[:=]?)\s*(\[[^\]]*\])", text_content, re.IGNORECASE)
        tags_text_match = re.search(
            r"(?:use\s+(?:these\s+)?tags|tags\s*[:=]|set\s+tags\s+to)\s*[:=]?\s*(.+?)(?=\s*(?:\bTitle\s*[:=]|\bDescription\s*[:=]|\bVideo\s*(?:id|\d+)\s*[:=]|[\r\n]|$))",
            text_content,
            re.IGNORECASE,
        )
        if not tags_text_match:
            tags_text_match = re.search(
                r"(?:use\s+(?:these\s+)?tags|tags\s*[:=]|set\s+tags\s+to)\s*[:=]?\s*([^\n\r]+)",
                text_content,
                re.IGNORECASE,
            )
        if tags_json_match:
            try:
                parsed_t = parse_tags_cell(tags_json_match.group(1))
                if parsed_t is not None:
                    tags_intent = "exact"
                    exact_tags = parsed_t
            except ValueError:
                pass
        elif tags_text_match:
            raw_tags = tags_text_match.group(1).strip().rstrip(".,;")
            if not re.search(r"^(?:improve|optimize|keep|preserve)\b", raw_tags, re.I):
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

    # If user explicitly said "preserve description and tags" or "optimize title only":
    preserve_desc_tags = bool(re.search(
        r"\b(?:preserve|keep|leave)\s+(?:the\s+)?(?:descriptions?|tags?|both|current\s+metadata)\b|"
        r"\b(?:descriptions?|tags?)\s+(?:remain\s+)?(?:unchanged|intact|as[- ]is)\b",
        lower,
    ))
    if title_only or preserve_desc_tags:
        if desc_intent == "unmentioned":
            desc_intent = "preserve"
        if tags_intent == "unmentioned":
            tags_intent = "preserve"

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
        "context_title": context_title,
        "description_intent": desc_intent,
        "exact_description": exact_desc,
        "tags_intent": tags_intent,
        "exact_tags": exact_tags,
        "fields_requested": fields_requested,
    }


def parse_instruction_intent_deterministic(instruction: str) -> Dict[str, Any]:
    """Compatibility wrapper for extract_field_values_and_intents."""
    return extract_field_values_and_intents(instruction)


def _call_groq_json(api_key: str, model: str, prompt: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(
        GROQ_CHAT_ENDPOINT,
        headers=headers,
        json={
            "model": model or DEFAULT_GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 4096,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    content_str = payload["choices"][0]["message"]["content"]
    return json.loads(content_str)


def _call_gemini_json(api_key: str, model: str, prompt: str) -> Dict[str, Any]:
    url = f"{GEMINI_ENDPOINT.format(model=model or DEFAULT_GEMINI_MODEL)}?key={api_key}"
    headers = {"Content-Type": "application/json"}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    response = requests.post(url, headers=headers, json=body, timeout=60)
    response.raise_for_status()
    payload = response.json()
    text = payload["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def _interpret_batch_instructions_with_ai(
    text: str,
    groq_key: Optional[str] = None,
    groq_model: Optional[str] = None,
    gemini_key: Optional[str] = None,
    gemini_model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Use Groq-first, Gemini-fallback AI to interpret natural-language instructions into structured JSON."""
    prompt = f"""You are the structured parser for YouTube metadata batch instructions.
Interpret the following user instruction text into a strict structured JSON batch plan.

USER TEXT:
\"\"\"
{text}
\"\"\"

Return ONLY a JSON object matching this schema:
{{
  "shared_instructions": "string (instructions that apply to all videos)",
  "videos": [
    {{
      "reference": "string (the video URL, ID, or title mentioned)",
      "video_id": "11-character video ID if identifiable, else null",
      "title_intent": "exact" | "optimize" | "preserve" | "unmentioned",
      "exact_title": "string or null",
      "description_intent": "exact" | "optimize" | "preserve" | "clear" | "unmentioned",
      "exact_description": "string or null",
      "tags_intent": "exact" | "optimize" | "preserve" | "clear" | "unmentioned",
      "exact_tags": ["string"] or null,
      "video_specific_instructions": "string",
      "clarification_required": "string or null"
    }}
  ]
}}

Rules:
1. Every distinct video reference mentioned in the text must have an entry in 'videos'.
2. If exact text was supplied by the user (in quotes or labeled), set intent to "exact" and preserve the text accurately (do not modify apostrophes or quotes).
3. If user asked to clear description or tags (e.g. "clear tags", "tags: []"), set intent to "clear" and exact_tags to [] or exact_description to "".
4. If user asked to optimize, improve, or craft a new field, set intent to "optimize".
5. If user asked to keep or preserve a field, set intent to "preserve".
6. If a field was not mentioned, set intent to "unmentioned".
"""

    if groq_key:
        try:
            return _call_groq_json(groq_key, groq_model, prompt)
        except Exception:
            pass

    if gemini_key:
        try:
            return _call_gemini_json(gemini_key, gemini_model, prompt)
        except Exception:
            pass

    return None


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
    secrets: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Parse natural-language batch instructions into a structured batch plan.
    
    Uses structured LLM interpretation when credentials are provided, with strict
    schema validation and deterministic reconciliation against every extracted URL and ID.
    """
    if not text or not isinstance(text, str):
        raise ValueError("Please provide batch instructions text.")

    if len(text) > MAX_BATCH_TEXT_LENGTH:
        raise ValueError(
            f"Batch instructions text exceeds maximum allowed length of {MAX_BATCH_TEXT_LENGTH:,} characters. "
            f"(Supplied {len(text):,} characters). Please split into smaller batches."
        )

    channel_videos = channel_videos or []
    if secrets:
        groq_section = secrets.get("groq") or secrets.get("chatgroq") or {}
        gemini_section = secrets.get("gemini", {})
        groq_config = groq_section if isinstance(groq_section, Mapping) else {}
        gemini_config = gemini_section if isinstance(gemini_section, Mapping) else {}
        groq_key = (
            groq_key or groq_config.get("api_key")
            or secrets.get("GROQ_API_KEY") or secrets.get("groq_api_key")
            or secrets.get("CHATGROQ_API_KEY") or secrets.get("chatgroq_api_key")
        )
        groq_model = (
            groq_model or groq_config.get("model")
            or secrets.get("GROQ_MODEL") or secrets.get("groq_model")
        )
        gemini_key = (
            gemini_key or gemini_config.get("api_key")
            or secrets.get("GEMINI_API_KEY") or secrets.get("gemini_api_key")
        )
        gemini_model = (
            gemini_model or gemini_config.get("model")
            or secrets.get("GEMINI_MODEL") or secrets.get("gemini_model")
        )

    # 1. First, extract ALL direct video references appearing in the text
    all_refs = extract_video_references_from_text(text)

    # 2. Try structured AI interpretation if credentials are provided
    ai_plan = None
    if groq_key or gemini_key:
        candidate_plan = _interpret_batch_instructions_with_ai(
            text, groq_key=groq_key, groq_model=groq_model, gemini_key=gemini_key, gemini_model=gemini_model
        )
        if candidate_plan:
            is_valid, _ = validate_batch_instructions_schema(candidate_plan)
            if is_valid and candidate_plan.get("videos"):
                ai_plan = candidate_plan

    # 3. Deterministic parsing (used standalone or as reconciliation baseline)
    # Extract shared instructions: text before first reference or after all references
    first_start = all_refs[0]["start"] if all_refs else len(text)
    last_end = all_refs[-1]["end"] if all_refs else 0

    shared_prefix = text[:first_start].strip()
    shared_suffix = text[last_end:].strip()
    combined_shared = f"{shared_prefix}\n{shared_suffix}".strip()
    shared_intent = extract_field_values_and_intents(combined_shared)

    raw_candidates: List[Dict[str, Any]] = []

    # Build deterministic per-reference context even when the LLM returns a complete plan.
    # Explicit user intent must outrank the LLM parser: otherwise a model can silently turn
    # "optimize title only" into "unmentioned"/"preserve" for some or all videos.
    deterministic_by_id: Dict[str, Dict[str, Any]] = {}
    for ref_index, ref in enumerate(all_refs):
        ref_id = ref.get("video_id")
        if not ref_id:
            continue
        segment_start = ref["start"]
        segment_end = all_refs[ref_index + 1]["start"] if ref_index + 1 < len(all_refs) else len(text)
        segment_text = text[segment_start:segment_end].strip()
        deterministic_by_id[ref_id] = {
            "intent": extract_field_values_and_intents(segment_text),
            "segment": segment_text,
        }

    if ai_plan:
        # Use AI for interpretation only where deterministic text rules do not find an
        # explicit local or shared instruction. Local instructions override shared ones.
        for item in ai_plan.get("videos", []):
            raw_ref = item.get("reference", "")
            item_video_id = item.get("video_id")
            if not item_video_id and raw_ref:
                try:
                    item_video_id = parse_video_reference(raw_ref)
                except ValueError:
                    item_video_id = None

            local_context = deterministic_by_id.get(item_video_id, {})
            local_intent = local_context.get("intent", {})

            def _merged_field_intent(intent_key: str, exact_key: str):
                # AI is the primary interpreter of natural-language intent.
                # Deterministic extraction is a safety/recovery layer: when it has
                # captured a literal exact value, that original user text wins over
                # any AI paraphrase. Empty "exact" payloads are never accepted.
                sources = (item, local_intent, shared_intent)
                valid_exact = []
                for source_index, source in enumerate(sources):
                    candidate = source.get(intent_key, "unmentioned") or "unmentioned"
                    value = source.get(exact_key)
                    has_value = (
                        value is not None
                        and (not isinstance(value, str) or bool(value.strip()))
                    )
                    if candidate == "exact" and has_value:
                        valid_exact.append((source_index, value))

                if valid_exact:
                    # Prefer literal text extracted from the user's input (local,
                    # then shared) over an AI-normalized or shortened copy.
                    deterministic_values = [entry for entry in valid_exact if entry[0] in (1, 2)]
                    _, exact_value = (deterministic_values or valid_exact)[0]
                    return "exact", exact_value

                # With no exact payload, let AI intent lead, then use deterministic
                # intent as a fallback. This supports ordinary language without
                # making the user conform to a rigid text template.
                for source in sources:
                    candidate = source.get(intent_key, "unmentioned") or "unmentioned"
                    value = source.get(exact_key)
                    if candidate == "unmentioned":
                        continue
                    if candidate == "exact":
                        continue
                    return candidate, value
                return "unmentioned", None

            title_intent, exact_title = _merged_field_intent("title_intent", "exact_title")
            description_intent, exact_description = _merged_field_intent(
                "description_intent", "exact_description"
            )
            tags_intent, exact_tags = _merged_field_intent("tags_intent", "exact_tags")

            specific_instruction = str(item.get("video_specific_instructions") or "").strip()
            shared_instruction = "\n".join(
                line
                for line in combined_shared.splitlines()
                if not re.match(
                    r"^\s*(?:current|existing|original|target|old)\s+titles?\s*[:=]",
                    line,
                    re.IGNORECASE,
                )
            ).strip()
            instruction_parts = []
            for instruction_part in (specific_instruction, shared_instruction):
                if instruction_part and instruction_part not in instruction_parts:
                    instruction_parts.append(instruction_part)

            raw_candidates.append({
                "reference": raw_ref,
                "video_id": item_video_id,
                "title_intent": title_intent,
                "exact_title": exact_title if title_intent == "exact" else None,
                "context_title": local_intent.get("context_title") or item.get("context_title", ""),
                "description_intent": description_intent,
                "exact_description": exact_description if description_intent in {"exact", "clear"} else None,
                "tags_intent": tags_intent,
                "exact_tags": exact_tags if tags_intent in {"exact", "clear"} else None,
                "original_instruction": "\n\n".join(instruction_parts) or local_context.get("segment", combined_shared),
                "errors": [],
            })

    # Deterministic reconciliation: ensure EVERY reference in all_refs is present
    parsed_ids_or_urls = {
        c.get("video_id") or c.get("reference")
        for c in raw_candidates if (c.get("video_id") or c.get("reference"))
    }

    # If no AI plan or some references were omitted by AI, process segments
    if not ai_plan or len(raw_candidates) < len(all_refs):
        # Segment text per reference: each reference gets surrounding context up to next reference
        for i, ref in enumerate(all_refs):
            ref_id = ref.get("video_id")
            raw_url = ref.get("raw_reference")
            if ref_id in parsed_ids_or_urls or raw_url in parsed_ids_or_urls:
                continue

            seg_start = ref["start"]
            seg_end = all_refs[i + 1]["start"] if (i + 1 < len(all_refs)) else len(text)
            seg_text = text[seg_start:seg_end].strip()

            local_intent = extract_field_values_and_intents(seg_text)

            # Combine with shared intent (local overrides shared)
            final_title_intent = local_intent["title_intent"] if local_intent["title_intent"] != "unmentioned" else shared_intent["title_intent"]
            final_exact_title = local_intent["exact_title"] if local_intent["exact_title"] is not None else shared_intent["exact_title"]
            final_context_title = local_intent.get("context_title") or shared_intent.get("context_title")

            final_desc_intent = local_intent["description_intent"] if local_intent["description_intent"] != "unmentioned" else shared_intent["description_intent"]
            final_exact_desc = local_intent["exact_description"] if local_intent["exact_description"] is not None else shared_intent["exact_description"]

            final_tags_intent = local_intent["tags_intent"] if local_intent["tags_intent"] != "unmentioned" else shared_intent["tags_intent"]
            final_exact_tags = local_intent["exact_tags"] if local_intent["exact_tags"] is not None else shared_intent["exact_tags"]

            raw_candidates.append({
                "reference": raw_url,
                "video_id": ref_id,
                "title_intent": final_title_intent,
                "exact_title": final_exact_title,
                "context_title": final_context_title,
                "description_intent": final_desc_intent,
                "exact_description": final_exact_desc,
                "tags_intent": final_tags_intent,
                "exact_tags": final_exact_tags,
                "original_instruction": seg_text or combined_shared,
                "errors": [],
            })

    # If no URLs/IDs were found, check if non-YouTube/invalid URLs or channel video titles are present
    if not raw_candidates:
        any_url_pattern = re.compile(r"https?://[^\s\"'>)]+", re.IGNORECASE)
        for m in any_url_pattern.finditer(text):
            url = m.group(0).rstrip(".,;!?:")
            raw_candidates.append({
                "reference": url,
                "video_id": None,
                "is_unresolved": True,
                "errors": [f"Invalid or unsupported video URL: '{url}'"],
                "original_instruction": text,
                "title_intent": shared_intent["title_intent"],
                "exact_title": shared_intent["exact_title"],
                "description_intent": shared_intent["description_intent"],
                "exact_description": shared_intent["exact_description"],
                "tags_intent": shared_intent["tags_intent"],
                "exact_tags": shared_intent["exact_tags"],
            })

    if not raw_candidates and channel_videos:
        # Check quoted terms for channel video titles
        quoted_terms = re.findall(r"['\"]([^'\"]{3,100})['\"]", text)
        matched_quoted = False
        for term in quoted_terms:
            res = resolve_channel_video_by_title(term, channel_videos)
            if res["status"] in {"resolved", "ambiguous"}:
                matched_quoted = True
                local_intent = extract_field_values_and_intents(text)
                raw_candidates.append({
                    "reference": term,
                    "video_id": res["video_id"],
                    "title_intent": local_intent["title_intent"] if local_intent["title_intent"] != "unmentioned" else shared_intent["title_intent"],
                    "exact_title": local_intent["exact_title"] if local_intent["exact_title"] is not None else shared_intent["exact_title"],
                    "description_intent": local_intent["description_intent"] if local_intent["description_intent"] != "unmentioned" else shared_intent["description_intent"],
                    "exact_description": local_intent["exact_description"] if local_intent["exact_description"] is not None else shared_intent["exact_description"],
                    "tags_intent": local_intent["tags_intent"] if local_intent["tags_intent"] != "unmentioned" else shared_intent["tags_intent"],
                    "exact_tags": local_intent["exact_tags"] if local_intent["exact_tags"] is not None else shared_intent["exact_tags"],
                    "original_instruction": text,
                    "is_ambiguous": res["status"] == "ambiguous",
                    "errors": [res["error"]] if res.get("error") else [],
                })

        if not matched_quoted:
            lines = [l.strip().strip("-*•").strip() for l in text.splitlines() if l.strip()]
            for line in lines:
                if line.startswith("http") or len(line) < 3:
                    continue
                res = resolve_channel_video_by_title(line, channel_videos)
                if res["status"] in {"resolved", "ambiguous"}:
                    raw_candidates.append({
                        "reference": line,
                        "video_id": res["video_id"],
                        "title_intent": shared_intent["title_intent"],
                        "exact_title": shared_intent["exact_title"],
                        "description_intent": shared_intent["description_intent"],
                        "exact_description": shared_intent["exact_description"],
                        "tags_intent": shared_intent["tags_intent"],
                        "exact_tags": shared_intent["exact_tags"],
                        "original_instruction": combined_shared,
                        "is_ambiguous": res["status"] == "ambiguous",
                        "errors": [res["error"]] if res.get("error") else [],
                    })

    if not raw_candidates:
        raise ValueError("No video links, IDs, or recognizable channel titles were found in the batch instructions.")

    if len(raw_candidates) > MAX_BATCH_VIDEOS:
        raise ValueError(
            f"This batch contains more than {MAX_BATCH_VIDEOS} videos ({len(raw_candidates)} identified). "
            "Please split your instructions into smaller batches."
        )

    # Resolution, deduplication, conflict checking
    final_videos: List[Dict[str, Any]] = []
    seen_videos: Dict[str, Dict[str, Any]] = {}

    for row in raw_candidates:
        ref_str = str(row.get("reference") or "")
        vid = row.get("video_id")

        if not vid and ref_str:
            try:
                vid = parse_video_reference(ref_str)
                row["video_id"] = vid
            except ValueError:
                if channel_videos:
                    res = resolve_channel_video_by_title(ref_str, channel_videos)
                    if res["status"] == "resolved":
                        vid = res["video_id"]
                        row["video_id"] = vid
                    elif res["status"] == "ambiguous":
                        row["is_ambiguous"] = True
                        row["errors"].append(res["error"])
                    else:
                        row["is_unresolved"] = True
                        row["errors"].append(f"Unresolved title reference: '{ref_str}'")
                else:
                    row["is_unresolved"] = True
                    row["errors"].append(f"Invalid YouTube URL or unresolvable title: '{ref_str}'")

        fields_requested = [
            f for f in ("title", "description", "tags")
            if (f == "title" and row.get("title_intent") in {"exact", "optimize"})
            or (f == "description" and row.get("description_intent") in {"exact", "optimize", "clear"})
            or (f == "tags" and row.get("tags_intent") in {"exact", "optimize", "clear"})
        ]
        row["fields_requested"] = fields_requested
        row["is_ambiguous"] = row.get("is_ambiguous", False)
        row["is_unresolved"] = row.get("is_unresolved", False)
        row["conflict_detected"] = False

        if vid:
            if vid in seen_videos:
                existing = seen_videos[vid]
                if (
                    existing.get("title_intent") != row.get("title_intent")
                    or existing.get("exact_title") != row.get("exact_title")
                    or existing.get("description_intent") != row.get("description_intent")
                    or existing.get("exact_description") != row.get("exact_description")
                    or existing.get("tags_intent") != row.get("tags_intent")
                    or existing.get("exact_tags") != row.get("exact_tags")
                ):
                    existing["conflict_detected"] = True
                    row["conflict_detected"] = True
                    err_msg = f"Conflicting instructions detected for duplicate video ID {vid}."
                    if err_msg not in existing["errors"]:
                        existing["errors"].append(err_msg)
                    row["errors"].append(err_msg)
            else:
                seen_videos[vid] = row

        final_videos.append(row)

    return {
        "raw_text": text,
        "shared_instructions": combined_shared,
        "videos": final_videos,
        "total_identified": len(final_videos),
        "errors": [],
    }
