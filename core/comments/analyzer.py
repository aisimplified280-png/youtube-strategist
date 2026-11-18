"""Grounded AI triage and reply drafting for a selected YouTube comment thread."""
from __future__ import annotations

import json
from typing import Any

from core.llm import (
    LLMError,
    LLMRouter,
    _clean_json_text,
    call_gemini_api,
    call_groq_api,
)


CATEGORIES = {
    "question", "correction", "praise", "suggestion", "confusion",
    "criticism", "spam", "abuse_or_bait", "other",
}
SENTIMENTS = {"positive", "neutral", "negative", "mixed"}
RECOMMENDATIONS = {"reply", "ignore", "verify_first"}


class CommentAnalysisError(RuntimeError):
    pass


def _parse_analysis(raw: str) -> dict | None:
    try:
        data = json.loads(_clean_json_text(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    category = str(data.get("category") or "").strip().lower()
    sentiment = str(data.get("sentiment") or "").strip().lower()
    recommendation = str(data.get("reply_recommendation") or "").strip().lower()
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return None
    needs_fact_check = data.get("needs_fact_check")
    if category not in CATEGORIES or sentiment not in SENTIMENTS or recommendation not in RECOMMENDATIONS:
        return None
    if not 0 <= confidence <= 1 or not isinstance(needs_fact_check, bool):
        return None
    reply = str(data.get("suggested_reply") or "").strip()
    reason = str(data.get("reason") or "").strip()[:400]
    if category in {"spam", "abuse_or_bait"}:
        recommendation = "ignore"
        reply = ""
    if needs_fact_check:
        recommendation = "verify_first"
        reply = ""
    if len(reply.split()) > 35:
        return None
    return {
        "category": category,
        "sentiment": sentiment,
        "confidence": confidence,
        "reason": reason,
        "reply_recommendation": recommendation,
        "needs_fact_check": needs_fact_check,
        "suggested_reply": reply,
    }


def _generate_groq_first(secrets: dict, prompt: str) -> tuple[str, str, str]:
    """Use Groq first, then Gemini, preserving configured model fallback chains."""
    router = LLMRouter(secrets or {})
    errors = []
    if router.groq_key:
        models = [router.groq_model] + [
            model for model in router.groq_fallbacks if model != router.groq_model
        ]
        for model in models:
            try:
                return call_groq_api(router.groq_key, model, prompt), "Groq", model
            except LLMError as error:
                errors.append(f"Groq ({model}): {error}")
                if not error.recoverable:
                    break
    if router.gemini_key:
        models = [router.gemini_model] + [
            model for model in router.gemini_fallbacks if model != router.gemini_model
        ]
        for model in models:
            try:
                return call_gemini_api(router.gemini_key, model, prompt), "Gemini", model
            except LLMError as error:
                errors.append(f"Gemini ({model}): {error}")
                if not error.recoverable:
                    break
    raise CommentAnalysisError(
        "Comment analysis providers are unavailable: " + "; ".join(errors or ["No API keys configured."])
    )


def analyze_comment_thread(
    secrets: dict,
    thread: dict,
    replies: list[dict] | None = None,
) -> dict:
    """Analyze only the selected thread; never publish the generated reply."""
    text = str(thread.get("text") or "").strip()
    if not text:
        raise ValueError("The selected comment has no readable text.")
    context = {
        "video_title": str(thread.get("video_title") or ""),
        "comment": text[:5000],
        "replies": [
            {
                "author": str(reply.get("author_display_name") or "Viewer")[:80],
                "text": str(reply.get("text") or "")[:1500],
            }
            for reply in (replies or [])[:20]
        ],
    }
    prompt = f"""You are the comment triage assistant for AI Simplified Lab, a YouTube channel about AI tools, tutorials and news.
Analyze this one comment thread and return ONLY a JSON object with:
category (one of {sorted(CATEGORIES)}),
sentiment (one of {sorted(SENTIMENTS)}),
confidence (number from 0 to 1),
reason (short explanation),
reply_recommendation (reply, ignore, or verify_first),
needs_fact_check (boolean),
suggested_reply (string).

Channel reply style: friendly, concise, natural, simple English that is easy for Telugu-speaking viewers to understand. Keep replies under 30 words.
Rules:
- Never invent product features, prices, free-plan limits, dates, availability, benchmark results or claims.
- If the viewer asks for current facts that are not present in this thread, set needs_fact_check=true and reply_recommendation="verify_first"; leave suggested_reply empty.
- For spam, abuse or bait, recommend ignore and leave suggested_reply empty.
- For corrections, do not agree or disagree unless the thread itself proves the point; use verify_first when uncertain.
- For praise, suggest a specific but brief human-sounding acknowledgement.
- Never promise a future video or action.
- Do not publish anything. This is a draft only.

Thread:
{json.dumps(context, ensure_ascii=False)}
"""
    raw, provider, model = _generate_groq_first(secrets, prompt)
    parsed = _parse_analysis(raw)
    if not parsed:
        repair = (
            prompt
            + "\nReturn valid JSON only. Repair this response without adding facts:\n"
            + str(raw)[:2000]
        )
        raw, provider, model = _generate_groq_first(secrets, repair)
        parsed = _parse_analysis(raw)
    if not parsed:
        raise CommentAnalysisError("AI could not return a valid, policy-compliant comment analysis.")
    parsed["provider"] = provider
    parsed["model"] = model
    return parsed
