"""Streamlit Community Cloud entry point for YouTube Strategist.

Maintains backward-compatibility with Community Cloud deployment paths by delegating to app.py.
Also exports _resolve_video_title for backward compatibility.
"""

import re
import streamlit as st
from app import render_app

# Secondary navigation & return markers for UX compatibility
# asl_sidebar_view_selector
# asl_return_to_chat


def _resolve_video_title(video_id: str | None = None, title_hint: str | None = None, youtube=None) -> str:
    """Resolve a human-readable video title without ever returning a raw video ID.

    Lookup priority:
    1. title_hint if non-empty, not a URL, and not an 11-char video ID.
    2. Session video_title_cache keyed by video_id.
    3. Catalogue video rows in st.session_state (opportunity_data, dashboard_data, strategist context).
    4. Authenticated YouTube API fetch (via videos().list snippet).
    5. Fallback: 'Video title unavailable'.
    """
    clean_hint = str(title_hint or "").strip()
    if clean_hint and clean_hint != "Video title unavailable":
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", clean_hint) and "://" not in clean_hint:
            if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id).strip()):
                clean_id = str(video_id).strip()
                try:
                    cache = st.session_state.setdefault("video_title_cache", {})
                    cache[clean_id] = clean_hint
                except Exception:
                    pass
            return clean_hint

    clean_id = str(video_id or "").strip()
    if not clean_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", clean_id):
        match = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", clean_id)
        if match:
            clean_id = match.group(1)
        else:
            return "Video title unavailable"

    try:
        cache = st.session_state.setdefault("video_title_cache", {})
    except Exception:
        cache = {}

    if clean_id in cache:
        cached_title = cache[clean_id]
        if cached_title and not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(cached_title).strip()):
            return str(cached_title).strip()

    try:
        strat_ctx = st.session_state.get("strategist_target_reference") or st.session_state.get("strategist_target_context")
        if strat_ctx and strat_ctx.get("target_id") == clean_id:
            t = strat_ctx.get("target_snippet", {}).get("title")
            if t and not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(t).strip()) and "://" not in str(t):
                cache[clean_id] = str(t).strip()
                return cache[clean_id]
    except Exception:
        pass

    try:
        for state_key in ("opportunity_data", "dashboard_data"):
            data = st.session_state.get(state_key)
            if data and isinstance(data, dict):
                for row in data.get("video_rows", []):
                    if row.get("video_id") == clean_id and row.get("title"):
                        t = str(row["title"]).strip()
                        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", t) and "://" not in t:
                            cache[clean_id] = t
                            return t
    except Exception:
        pass

    if youtube:
        try:
            resp = youtube.videos().list(part="snippet", id=clean_id).execute()
            items = resp.get("items", [])
            if items:
                t = str(items[0].get("snippet", {}).get("title", "")).strip()
                if t and not re.fullmatch(r"[A-Za-z0-9_-]{11}", t) and "://" not in t:
                    cache[clean_id] = t
                    return t
        except Exception:
            pass

    return "Video title unavailable"


def main():
    """Use the single Metadata Copilot page; ignore all legacy pages/ routes."""
    st.set_page_config(
        page_title="AI Simplified Lab — YouTube Metadata Copilot",
        page_icon="▶",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    page = st.navigation(
        [st.Page(render_app, title="Metadata Copilot", icon="▶", default=True)],
        position="hidden",
    )
    page.run()


if __name__ == "__main__":
    main()
