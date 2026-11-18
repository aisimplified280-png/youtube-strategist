"""Evidence-grounded metadata health checks and growth suggestions."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from core.channel_strategy_agent import _youtube_analytics_today
from core.llm import LLMError, LLMRouter, _clean_json_text
import youtube_analytics_connector as connector


ANALYTICS_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)
SAFE_FIELDS = {"title", "description", "tags"}


def _tag_character_count(tags: list[str]) -> int:
    values = [str(tag).strip() for tag in tags if str(tag).strip()]
    encoded = sum(len(tag) + (2 if " " in tag else 0) for tag in values)
    return encoded + max(0, len(values) - 1)


def metadata_standard_checks(resource: dict) -> list[dict]:
    """Check API-enforced metadata constraints and common quality issues."""
    snippet = resource.get("snippet") or {}
    title = str(snippet.get("title") or "")
    description = str(snippet.get("description") or "")
    tags = [str(tag).strip() for tag in (snippet.get("tags") or []) if str(tag).strip()]
    checks = []

    title_chars = len(title)
    checks.append({
        "field": "title",
        "status": "error" if not title.strip() or title_chars > 100 or "<" in title or ">" in title else (
            "warning" if title_chars < 25 else "pass"
        ),
        "detail": (
            "A title is required." if not title.strip()
            else f"Title is {title_chars} characters; YouTube allows up to 100."
            if title_chars > 100
            else "YouTube titles cannot contain < or > characters."
            if "<" in title or ">" in title
            else f"Title uses {title_chars}/100 characters. Very short titles may omit useful context; this is an editorial prompt, not an API violation."
            if title_chars < 25
            else f"Title length is within YouTube's 100-character limit ({title_chars}/100)."
        ),
    })

    description_bytes = len(description.encode("utf-8"))
    checks.append({
        "field": "description",
        "status": "error" if description_bytes > 5000 or "<" in description or ">" in description else (
            "warning" if not description.strip() else "pass"
        ),
        "detail": (
            f"Description is {description_bytes} UTF-8 bytes; YouTube allows up to 5,000 bytes."
            if description_bytes > 5000
            else "YouTube descriptions cannot contain < or > characters."
            if "<" in description or ">" in description
            else "Description is empty."
            if not description.strip()
            else f"Description uses {description_bytes}/5,000 bytes."
        ),
    })

    tag_chars = _tag_character_count(tags)
    duplicate_tags = sorted({
        tag.casefold() for tag in tags if sum(1 for other in tags if other.casefold() == tag.casefold()) > 1
    })
    checks.append({
        "field": "tags",
        "status": "error" if tag_chars > 500 else ("warning" if duplicate_tags else "pass"),
        "detail": (
            f"Tags use {tag_chars} characters including spaces/commas; YouTube allows up to 500."
            if tag_chars > 500
            else "Duplicate tags: " + ", ".join(duplicate_tags)
            if duplicate_tags
            else f"Tags use {tag_chars}/500 characters. YouTube says tags play a minimal role except for common misspellings."
        ),
    })

    category_id = str(snippet.get("categoryId") or "").strip()
    checks.append({
        "field": "category",
        "status": "pass" if category_id else "warning",
        "detail": f"Video category ID: {category_id}." if category_id else "No category ID is available; category is required when updating the snippet.",
    })

    language = str(snippet.get("defaultLanguage") or "").strip()
    checks.append({
        "field": "language",
        "status": "pass" if language else "info",
        "detail": f"Default metadata language: {language}." if language else "Default metadata language is not set. Only set it when the title and description language is known.",
    })
    return checks


def fetch_video_performance(analytics, video_id: str, *, today: date | None = None) -> dict:
    """Fetch one video's mature 28-day analytics window and the preceding 28 days."""
    if analytics is None:
        return {"status": "unavailable", "reason": "YouTube Analytics API is not connected."}
    today = today or _youtube_analytics_today()
    current_end = today - timedelta(days=3)
    current_start = current_end - timedelta(days=27)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=27)

    try:
        current_rows = connector._query_analytics(
            analytics, current_start.isoformat(), current_end.isoformat(),
            ANALYTICS_METRICS, video_id=video_id, max_results=10,
        )
        previous_rows = connector._query_analytics(
            analytics, previous_start.isoformat(), previous_end.isoformat(),
            ANALYTICS_METRICS, video_id=video_id, max_results=10,
        )
    except Exception as error:
        return {
            "status": "unavailable",
            "reason": f"{type(error).__name__}: {str(error)[:500]}",
            "current_period": [current_start.isoformat(), current_end.isoformat()],
            "previous_period": [previous_start.isoformat(), previous_end.isoformat()],
        }

    current = current_rows[0] if current_rows else {}
    previous = previous_rows[0] if previous_rows else {}
    metrics = {}
    for key in (
        "views", "estimatedMinutesWatched", "averageViewDuration",
        "averageViewPercentage", "likes", "comments", "shares",
        "subscribersGained", "subscribersLost",
    ):
        value = current.get(key)
        prior = previous.get(key)
        delta = None
        if (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and isinstance(prior, (int, float)) and not isinstance(prior, bool)
            and prior != 0
        ):
            delta = round(((value - prior) / abs(prior)) * 100, 2)
        metrics[key] = {"current": value, "previous": prior, "delta_percent": delta}
    return {
        "status": "available" if current_rows or previous_rows else "no_data",
        "source": "YouTube Analytics API (channel==MINE, filtered to this video)",
        "current_period": [current_start.isoformat(), current_end.isoformat()],
        "previous_period": [previous_start.isoformat(), previous_end.isoformat()],
        "analytics_delay_days": 3,
        "metrics": metrics,
        "reach_metrics_note": "Impressions and thumbnail CTR are not included in this Analytics report. They require a separate YouTube Reporting reach report.",
    }


def _validate_recommendations(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    output = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if field not in SAFE_FIELDS:
            continue
        value = item.get("suggested_value")
        if field in {"title", "description"} and not isinstance(value, str):
            continue
        if field == "tags" and (
            not isinstance(value, list) or not all(isinstance(tag, str) for tag in value)
        ):
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0
        if not 0 <= confidence <= 1:
            continue
        if field == "title" and (not value.strip() or len(value) > 100 or "<" in value or ">" in value):
            continue
        if field == "description" and (len(value.encode("utf-8")) > 5000 or "<" in value or ">" in value):
            continue
        if field == "tags" and _tag_character_count(value) > 500:
            continue
        output.append({
            "field": field,
            "issue": str(item.get("issue") or "")[:400],
            "recommendation": str(item.get("recommendation") or "")[:500],
            "evidence": str(item.get("evidence") or "")[:500],
            "suggested_value": value,
            "confidence": confidence,
        })
    return output


def generate_metadata_insights(
    secrets: dict,
    resource: dict,
    performance: dict,
) -> dict:
    """Create a grounded health summary plus safe text-only recommendations.

    Failure of the model does not hide deterministic YouTube standards checks.
    """
    checks = metadata_standard_checks(resource)
    snippet = resource.get("snippet") or {}
    current_metadata = {
        "title": snippet.get("title") or "",
        "description": snippet.get("description") or "",
        "tags": snippet.get("tags") or [],
        "category_id": snippet.get("categoryId") or "",
        "default_language": snippet.get("defaultLanguage") or "",
        "default_audio_language": snippet.get("defaultAudioLanguage") or "",
    }
    evidence = {
        "current_metadata": current_metadata,
        "performance": performance,
        "standard_checks": checks,
    }
    prompt = f"""You are an evidence-first YouTube metadata auditor for AI Simplified Lab.
Analyze this ONE already-published video using only the supplied metadata, checks and video-level Analytics.

Return ONLY JSON with:
summary (one paragraph),
findings (array of {{field, severity [high|medium|low|info], issue, evidence, recommendation}}),
recommendations (array of at most 4 objects {{field [title|description|tags], issue, recommendation, evidence, suggested_value, confidence [0..1]}}).

Rules:
- The video content/transcript is not supplied. Never invent what happens in the video, its tools, benchmarks, results, pricing, dates or promises.
- Only recommend a title, description or tags when the proposed wording is fully supported by the existing metadata/evidence. Otherwise give advice without a suggested_value and omit that recommendation.
- Do not recommend bulk keyword stuffing. YouTube says tags have a minimal discovery role and are primarily useful for common misspellings; prioritize the title and description.
- Do not claim that metadata edits guarantee views, CTR, ranking, recommendations or subscribers.
- Analytics period data is historical and delayed; do not call it real-time.
- Impressions and thumbnail CTR are not supplied by this report. Never infer them.
- A change in views or watch time does not prove metadata caused it.
- Never recommend changing visibility, child audience settings, synthetic-media declarations, copyright/license, scheduled publishing, paid placement, localization or other sensitive fields.
- Keep title suggestions within 100 characters and descriptions within 5,000 UTF-8 bytes. Exclude < and >.
- Return empty recommendations when no evidence-backed improvement is justified.
- Keep wording simple and natural for a Telugu-speaking audience that may use English auto-dubbing.

Evidence:
{json.dumps(evidence, ensure_ascii=False)[:18000]}
"""
    try:
        raw, provider, model = LLMRouter(secrets or {}).generate(prompt)
        parsed = json.loads(_clean_json_text(raw))
        if not isinstance(parsed, dict):
            raise ValueError("AI output must be a JSON object.")
        recommendations = _validate_recommendations(parsed.get("recommendations"))
        return {
            "summary": str(parsed.get("summary") or "Metadata checks completed.")[:1000],
            "findings": [
                item for item in (parsed.get("findings") or [])[:8]
                if isinstance(item, dict)
            ],
            "recommendations": recommendations,
            "standard_checks": checks,
            "performance": performance,
            "provider": provider,
            "model": model,
            "analysis_status": "ai_and_rules",
        }
    except (LLMError, ValueError, TypeError, json.JSONDecodeError) as error:
        failed = [item for item in checks if item["status"] in {"error", "warning"}]
        return {
            "summary": (
                "The deterministic YouTube metadata checks completed. AI growth suggestions are unavailable right now; "
                "the checks below remain valid."
            ),
            "findings": [],
            "recommendations": [],
            "standard_checks": checks,
            "performance": performance,
            "provider": None,
            "model": None,
            "analysis_status": "rules_only",
            "error": f"{type(error).__name__}: {str(error)[:500]}",
        }


def resource_fingerprint(resource: dict) -> str:
    canonical = json.dumps(resource or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
