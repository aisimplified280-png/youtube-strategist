"""Programmatic metadata and packaging linter for YouTube titles, descriptions, and tags.

Integrates stdlib tools from skills/yt-package/title.py and skills/yt-script/hookscore.py.
"""

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add skills to path so stdlib tools can be imported cleanly
_SKILLS_PATH = Path(__file__).resolve().parent.parent / "skills"
if str(_SKILLS_PATH) not in sys.path:
    sys.path.insert(0, str(_SKILLS_PATH))

try:
    from yt_package import title as title_tool
except ImportError:
    try:
        # Direct import attempt if module path varies
        import importlib.util
        _spec = importlib.util.spec_from_file_location("title_tool", _SKILLS_PATH / "yt-package" / "title.py")
        title_tool = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(title_tool)
    except Exception:
        title_tool = None

try:
    import importlib.util
    _hook_spec = importlib.util.spec_from_file_location("hookscore_tool", _SKILLS_PATH / "yt-script" / "hookscore.py")
    hookscore_tool = importlib.util.module_from_spec(_hook_spec)
    _hook_spec.loader.exec_module(hookscore_tool)
except Exception:
    hookscore_tool = None

BANNED_MISLEADING_PATTERNS = [
    re.compile(r"\b(guaranteed\s+(?:views|ctr|subscribers|growth))\b", re.I),
    re.compile(r"\b(viral\s+(?:trick|hack|secret|cheat))\b", re.I),
    re.compile(r"\b(100%\s+(?:guaranteed|success|viral))\b", re.I),
    re.compile(r"\b(beat\s+the\s+algorithm)\b", re.I),
    re.compile(r"\b(instant\s+(?:views|traffic|fame))\b", re.I),
]


def lint_title(title: str, thumbnail_concept: Optional[str] = None) -> Dict[str, Any]:
    """Lint a video title against YouTube desktop/mobile limits, specificity, and thumbnail pairing."""
    t = (title or "").strip()
    n = len(t)
    warnings: List[str] = []
    passes: List[str] = []

    if n == 0:
        return {"length": 0, "warnings": ["Title is empty."], "passes": [], "score": 0}

    # Hard & Soft Limits
    if n > 100:
        warnings.append(f"Title exceeds YouTube's hard limit of 100 characters ({n}/100).")
    elif n > 60:
        warnings.append(f"Title ({n} chars) will truncate on desktop search (around 60 chars). Tail may be hidden.")
    else:
        passes.append(f"Title length ({n} chars) fits comfortably within desktop search limit (60 chars).")

    if n > 40:
        truncated_mobile = t[:40].rsplit(" ", 1)[0]
        warnings.append(f'On mobile feeds, title cuts around 40 chars: "{truncated_mobile}...". Ensure the core subject comes first.')
    else:
        passes.append("Title is fully visible on mobile feeds without truncation.")

    # Check for all-caps shouting
    caps_words = [w for w in t.split() if len(w) > 2 and w.isupper()]
    if len(caps_words) > 2:
        warnings.append(f"{len(caps_words)} words in ALL-CAPS. Limit emphasis to at most one or two words to avoid spam appearance.")

    # Check for banned misleading claims
    for pat in BANNED_MISLEADING_PATTERNS:
        match = pat.search(t)
        if match:
            warnings.append(f'Misleading promise detected: "{match.group(0)}". YouTube values viewer satisfaction; remove sensationalist guarantees.')

    # Use title_tool if available
    if title_tool and hasattr(title_tool, "check"):
        try:
            tool_res = title_tool.check(t, thumbnail_concept)
            for _, issue_text in tool_res.get("issues", []):
                if issue_text not in warnings and not any(issue_text in w for w in warnings):
                    warnings.append(issue_text)
            for good_text in tool_res.get("good", []):
                if good_text not in passes:
                    passes.append(good_text)
        except Exception:
            pass

    score = max(0, min(100, 100 - (18 * len(warnings)) + (5 * len(passes))))
    return {
        "title": t,
        "length": n,
        "warnings": warnings,
        "passes": passes,
        "score": score,
    }


def lint_description(description: str) -> Dict[str, Any]:
    """Lint description length, hashtag count, and structure."""
    d = (description or "").strip()
    n = len(d)
    warnings: List[str] = []
    passes: List[str] = []

    if n == 0:
        warnings.append("Description is empty. The opening 2 lines are critical for viewer context and search.")
    elif n > 5000:
        warnings.append(f"Description exceeds YouTube's 5,000 character limit ({n}/5000).")
    else:
        passes.append(f"Description length is within limits ({n}/5000 chars).")

    hashtags = re.findall(r"#\w+", d)
    if len(hashtags) > 15:
        warnings.append(f"Excessive hashtags ({len(hashtags)}). YouTube recommends no more than 3-5; >15 causes all hashtags to be ignored.")
    elif len(hashtags) > 5:
        warnings.append(f"{len(hashtags)} hashtags detected. Best practice is 3 focused hashtags in the description.")
    elif len(hashtags) >= 1:
        passes.append(f"Optimal hashtag count ({len(hashtags)} hashtags).")

    # Check for misleading phrases
    for pat in BANNED_MISLEADING_PATTERNS:
        match = pat.search(d)
        if match:
            warnings.append(f'Misleading promise in description: "{match.group(0)}".')

    score = max(0, min(100, 100 - (20 * len(warnings)) + (10 * len(passes))))
    return {
        "length": n,
        "hashtag_count": len(hashtags),
        "hashtags": hashtags,
        "warnings": warnings,
        "passes": passes,
        "score": score,
    }


def lint_tags(tags: List[str]) -> Dict[str, Any]:
    """Lint YouTube tags budget (max 500 characters total across all tags)."""
    tags_list = [t.strip() for t in tags if str(t).strip()]
    raw_joined = ",".join(tags_list)
    char_count = len(raw_joined)
    warnings: List[str] = []
    passes: List[str] = []

    if char_count > 500:
        warnings.append(f"Total tag characters ({char_count}/500) exceed YouTube's 500-character limit.")
    else:
        passes.append(f"Tags character budget used: {char_count}/500 characters ({len(tags_list)} tags).")

    # Note honesty on tags
    passes.append("Note: YouTube explicitly states that tags play a minimal role in video discovery compared to title, thumbnail, and description.")

    score = 100 if char_count <= 500 else 40
    return {
        "tag_count": len(tags_list),
        "char_count": char_count,
        "tags": tags_list,
        "warnings": warnings,
        "passes": passes,
        "score": score,
    }


def lint_proposal(proposal: Dict[str, Any], thumbnail_concept: Optional[str] = None) -> Dict[str, Any]:
    """Comprehensive lint of proposed title, description, and tags."""
    title_res = lint_title(proposal.get("title", ""), thumbnail_concept)
    desc_res = lint_description(proposal.get("description", ""))
    tags_res = lint_tags(proposal.get("tags", []))

    all_warnings = title_res["warnings"] + desc_res["warnings"] + tags_res["warnings"]
    all_passes = title_res["passes"] + desc_res["passes"] + tags_res["passes"]
    overall_score = int((title_res["score"] * 0.5) + (desc_res["score"] * 0.3) + (tags_res["score"] * 0.2))

    return {
        "overall_score": overall_score,
        "title": title_res,
        "description": desc_res,
        "tags": tags_res,
        "warnings": all_warnings,
        "passes": all_passes,
        "is_valid": len(title_res["warnings"]) == 0 and len(tags_res["warnings"]) == 0,
    }


def score_hook_text(hook_text: str) -> Optional[Dict[str, Any]]:
    """Score hook text using hookscore_tool if available."""
    if not hookscore_tool:
        return None
    try:
        t = hook_text.strip()
        spec = hookscore_tool.specificity(t)
        addr = hookscore_tool.address(t)
        stk = hookscore_tool.stakes(t)
        cur = hookscore_tool.curiosity(t)
        brv = hookscore_tool.brevity(t)
        props = [spec, addr, stk, cur, brv]
        mean_score = sum(props) / len(props)
        min_score = min(props)
        final_score = int(0.6 * mean_score + 0.4 * min_score)
        return {
            "hook": t,
            "overall_score": final_score,
            "specificity": spec,
            "address": addr,
            "stakes": stk,
            "curiosity": cur,
            "brevity": brv,
            "weakest_dimension": min_score,
            "note": "Heuristic indicator based on 74 measured hooks. Low score = inspect again; high score = not a guarantee.",
        }
    except Exception:
        return None
