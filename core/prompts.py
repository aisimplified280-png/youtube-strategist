"""Versioned prompts and structured proposal schemas for the YouTube Strategist."""

import json
from typing import Any, Dict, List, Optional

PROMPT_VERSION = "2026-10-v2"

SYSTEM_INSTRUCTION = """You are a senior YouTube packaging strategist and content analyst for a serious creator.
Your goal is to optimize a video's packaging (title, description, hashtags, and tags) to accurately reflect its content and maximize viewer satisfaction and discoverability.

CRITICAL RULES:
1. TRUTH IN PACKAGING: Never invent facts, storylines, or topics that are not present in the video's transcript or current metadata. If the transcript is unavailable, explicitly state that your suggestions are grounded in public metadata only.
2. NO PERFORMANCE PROMISES: Never claim a title or tag will "boost CTR by X%", "go viral", or "beat the algorithm". YouTube rewards viewer satisfaction and accurate discovery.
3. CONCRETE & SPECIFIC: Avoid generic hype words ("insane", "game changing", "ultimate"). Prefer numbers, dates, tools, specific questions, or clear contrasts.
4. TITLE LENGTH: YouTube cuts titles around 60 characters on desktop search and around 40 characters on mobile home feeds. Aim for 45-58 characters and front-load the key subject.
5. DESCRIPTION & HASHTAGS: The first 2 lines appear in search snippets. Place 2-3 specific, relevant hashtags at the end. Total description limit is 5000 characters.
6. TAGS: YouTube tags help with common misspellings or related queries; they are not a primary discovery mechanism. Total tag character count must remain under 500 characters.

OUTPUT FORMAT:
You MUST respond with a single valid JSON object with the following schema:
{
  "title": "Proposed Title Here",
  "description": "Proposed Full Description Here\\n\\n#Tag1 #Tag2",
  "tags": ["tag1", "tag2", "tag3"],
  "rationale": "Clear, honest explanation of why each change is justified based on the video transcript and evidence.",
  "variants": [
    {"title": "Variant title 1", "angle": "Curiosity/Question angle"},
    {"title": "Variant title 2", "angle": "Direct/Clarity angle"}
  ]
}
Do NOT wrap the JSON in Markdown code fences if possible, or use standard ```json ``` fences. Do not output anything outside the JSON object.
"""

REPAIR_PROMPT = """Your previous response could not be parsed as valid JSON.
Please return ONLY a valid JSON object matching this schema:
{
  "title": "...",
  "description": "...",
  "tags": ["..."],
  "rationale": "...",
  "variants": [{"title": "...", "angle": "..."}]
}
Ensure all quotes are escaped properly and no trailing commas exist.
"""


def build_strategist_prompt(
    target_snippet: Dict[str, Any],
    instruction: str,
    target_transcript: str = "",
    target_transcript_status: str = "Unavailable",
    studio_evidence: Optional[List[Dict[str, Any]]] = None,
    reference_snippet: Optional[Dict[str, Any]] = None,
    reference_transcript: str = "",
    chat_history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Construct full prompt with injected context for LLM generation."""
    sections = [SYSTEM_INSTRUCTION, "\n--- CURRENT VIDEO CONTEXT ---"]
    sections.append(f"CURRENT TITLE: {target_snippet.get('title', 'Unknown')}")
    sections.append(f"CURRENT DESCRIPTION:\n{target_snippet.get('description', '')}")
    tags = target_snippet.get("tags", [])
    sections.append(f"CURRENT TAGS: {', '.join(tags) if tags else '(None)'}")

    sections.append(f"\nTRANSCRIPT STATUS: {target_transcript_status}")
    if target_transcript.strip():
        # Truncate transcript if too long (max 10000 chars)
        clipped = target_transcript[:10000]
        sections.append(f"TRANSCRIPT EXCERPT (WITH TIMESTAMPS):\n{clipped}")
    else:
        sections.append("TRANSCRIPT: Unavailable. NOTE: Ground your suggestions strictly in current metadata and do not infer unseen video visuals.")

    if studio_evidence:
        sections.append("\nMEASURED PERFORMANCE EVIDENCE (From Studio Reports):")
        for ev in studio_evidence:
            views_str = f"Views: {ev.get('views', 'N/A')}"
            ctr_str = f"CTR: {ev.get('ctr_percent', 'N/A')}%" if ev.get('ctr_percent') is not None else "CTR: Unavailable"
            apv_str = f"Avg % Viewed: {ev.get('average_view_percentage', 'N/A')}%" if ev.get('average_view_percentage') is not None else ""
            cliff_str = f"Retention cliff: {ev.get('retention_cliff', 'N/A')}" if ev.get('retention_cliff') else ""
            sections.append(f"- Report '{ev.get('source_file', 'Export')}': {views_str}, {ctr_str}, {apv_str} {cliff_str} (Use as review signal, not causal proof)")

    if reference_snippet:
        sections.append("\nOPTIONAL REFERENCE VIDEO:")
        sections.append(f"Title: {reference_snippet.get('title', '')}")
        sections.append(f"Description: {reference_snippet.get('description', '')[:300]}...")
        if reference_transcript.strip():
            sections.append(f"Reference Transcript Snippet: {reference_transcript[:1500]}")

    if chat_history:
        sections.append("\nRECENT CONVERSATION HISTORY:")
        for turn in chat_history[-6:]:
            role = turn.get("role", "user").upper()
            sections.append(f"{role}: {turn.get('content', '')}")

    sections.append(f"\nUSER INSTRUCTION:\n{instruction}")
    sections.append("\nPlease output your structured proposal JSON now:")

    return "\n".join(sections)
