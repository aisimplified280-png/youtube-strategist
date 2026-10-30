"""Reusable UI components for YouTube Strategist."""

import difflib
import html
from typing import Any, Callable, Dict, List, Optional, Tuple

import streamlit as st


def demo_banner():
    """Render a prominent banner indicating Demo Mode is active."""
    if st.session_state.get("demo_mode", False):
        st.markdown(
            """
            <div class="demo-banner">
                <span>⚡ <strong>DEMO MODE ACTIVE</strong> — Displaying realistic sample channel data. Real APIs are not contacted.</span>
                <span style="font-size: 0.8rem; opacity: 0.85;">Toggle off in Settings</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


def stepper(steps: List[str], current_step: int):
    """Render a 3-step or n-step status strip.
    
    current_step is 1-indexed.
    """
    html_items = []
    for i, step_name in enumerate(steps, start=1):
        if i < current_step:
            state_cls = "completed"
            num_display = "✓"
        elif i == current_step:
            state_cls = "active"
            num_display = str(i)
        else:
            state_cls = ""
            num_display = str(i)

        item_html = f"""
        <div class="step-item {state_cls}">
            <div class="step-num">{num_display}</div>
            <div>{html.escape(step_name)}</div>
        </div>
        """
        html_items.append(item_html)

    stepper_html = f"""
    <div class="stepper-container">
        {'<div class="step-arrow">→</div>'.join(html_items)}
    </div>
    """
    st.markdown(stepper_html, unsafe_allow_html=True)


def metric_tile(label: str, value: Any, delta: Optional[str] = None, delta_type: str = "neutral", help_text: Optional[str] = None):
    """Render a high-contrast metric card with period-over-period delta."""
    delta_class = f"delta-{delta_type}"
    delta_html = f'<div class="metric-delta {delta_class}">{html.escape(delta)}</div>' if delta else ""
    help_attr = f'title="{html.escape(help_text)}"' if help_text else ""

    st.markdown(
        f"""
        <div class="metric-card" {help_attr}>
            <div class="metric-label">{html.escape(label)}</div>
            <div class="metric-value">{html.escape(str(value))}</div>
            {delta_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def evidence_chip(label: str, value: str, chip_type: str = "neutral") -> str:
    """Return HTML string for an evidence chip."""
    return f'<span class="evidence-chip chip-{chip_type}">{html.escape(label)}: <strong>{html.escape(value)}</strong></span>'


def status_pill(status: str, label: str):
    """Render a status pill (connected, reconnect, disconnected)."""
    cls_map = {
        "connected": "status-connected",
        "reconnect": "status-reconnect",
        "disconnected": "status-disconnected",
    }
    pill_cls = cls_map.get(status, "status-disconnected")
    st.markdown(
        f'<span class="status-pill {pill_cls}">● {html.escape(label)}</span>',
        unsafe_allow_html=True,
    )


def char_counter(current_len: int, max_len: int, warn_len: Optional[int] = None) -> str:
    """Return HTML string for character count badge with warnings.
    
    warn_len: Optional integer threshold above which to display a warning.
    """
    warn_threshold = warn_len if isinstance(warn_len, (int, float)) else None
    label_suffix = f" &bull; {html.escape(str(warn_len))}" if isinstance(warn_len, str) else ""

    if current_len > max_len:
        cls = "char-over"
    elif warn_threshold is not None and current_len > warn_threshold:
        cls = "char-warn"
    else:
        cls = "char-ok"
    return f'<span class="char-counter {cls}">{current_len}/{max_len} chars{label_suffix}</span>'


def word_level_diff(before: str, after: str) -> str:
    """Generate inline HTML word-level diff showing additions and deletions."""
    before_words = before.split()
    after_words = after.split()
    matcher = difflib.SequenceMatcher(None, before_words, after_words)
    diff_parts = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            diff_parts.append(html.escape(" ".join(before_words[i1:i2])))
        elif tag == "delete":
            deleted = html.escape(" ".join(before_words[i1:i2]))
            diff_parts.append(f'<span class="diff-removed">{deleted}</span>')
        elif tag == "insert":
            inserted = html.escape(" ".join(after_words[j1:j2]))
            diff_parts.append(f'<span class="diff-added">{inserted}</span>')
        elif tag == "replace":
            deleted = html.escape(" ".join(before_words[i1:i2]))
            inserted = html.escape(" ".join(after_words[j1:j2]))
            diff_parts.append(f'<span class="diff-removed">{deleted}</span> <span class="diff-added">{inserted}</span>')

    return " ".join(diff_parts)


def diff_viewer(before: str = "", after: str = "", label: str = "Diff", *args, **kwargs):
    """Render a word-level visual diff container.
    
    Accepts 3-argument signature: before, after, label="Diff".
    Also handles 4-argument calls gracefully: (lbl1, val1, lbl2, val2).
    """
    if args and len(args) == 1 and isinstance(after, str):
        # Called as diff_viewer(lbl1, val1, lbl2, val2)
        diff_label = label or "Diff"
        diff_html = word_level_diff(after or "", args[0] or "")
    else:
        diff_label = label
        diff_html = word_level_diff(before or "", after or "")
    st.markdown(
        f"""
        <div class="diff-panel">
            <div style="font-size:0.75rem; color:#94A3B8; font-weight:700; text-transform:uppercase; margin-bottom:6px;">{html.escape(diff_label)}</div>
            <div>{diff_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def empty_state(title: str, description: str, icon: str = "🔍", action_label: Optional[str] = None, on_action: Optional[Callable] = None):
    """Render a clean empty state card."""
    with st.container(border=True):
        st.markdown(
            f"""
            <div style="text-align: center; padding: 24px 16px;">
                <div style="font-size: 2.2rem; margin-bottom: 8px;">{icon}</div>
                <div style="font-size: 1.15rem; font-weight: 700; color: #FFFFFF; margin-bottom: 6px;">{html.escape(title)}</div>
                <div style="font-size: 0.9rem; color: #94A3B8; max-width: 480px; margin: 0 auto 16px;">{html.escape(description)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if action_label and on_action:
            col1, col2, col3 = st.columns([1, 2, 1])
            with col2:
                if st.button(action_label, type="primary", use_container_width=True):
                    on_action()


def error_state(title: str, reason: str, icon: str = "⚠️", retry_label: Optional[str] = "Retry", on_retry: Optional[Callable] = None):
    """Render a structured error message with explanation and action."""
    with st.container(border=True):
        st.markdown(
            f"""
            <div style="padding: 12px 14px;">
                <div style="display: flex; align-items: center; gap: 8px; color: #EF4444; font-weight: 700; font-size: 1rem; margin-bottom: 6px;">
                    <span>{icon}</span> <span>{html.escape(title)}</span>
                </div>
                <div style="font-size: 0.88rem; color: #CBD5E1; line-height: 1.5; margin-bottom: 12px;">
                    {html.escape(reason)}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if retry_label and on_retry:
            if st.button(retry_label, key=f"btn_retry_{title[:10]}"):
                on_retry()


def connection_status_banner(secrets: Dict[str, Any], subject: Optional[str] = None, yt_creds=None, memory_creds=None, is_demo: bool = False):
    """Render prominent, actionable connection banner at top of main workspace/briefing."""
    if is_demo:
        return

    from core.oauth import get_auth_url
    from core.auth import set_demo_mode

    # Case 1: YouTube Disconnected
    if not yt_creds:
        with st.container(border=True):
            col_b1, col_b2, col_b3 = st.columns([2.5, 1, 1])
            with col_b1:
                st.markdown(
                    """
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span style="font-size:1.6rem;">⚡</span>
                        <div>
                            <div style="font-weight:700; color:#FFFFFF; font-size:1.02rem;">Connect YouTube Channel</div>
                            <div style="color:#94A3B8; font-size:0.86rem;">Connect AI Simplified Lab via Google OAuth to enable automated daily briefs and 1-click verified metadata updates.</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with col_b2:
                st.write("")
                auth_url, _ = get_auth_url("youtube", subject or "", secrets)
                if auth_url:
                    st.link_button("▶ Connect YouTube", auth_url, type="primary", use_container_width=True)
                else:
                    st.caption("OAuth not configured")
            with col_b3:
                st.write("")
                if st.button("🎮 Try Demo Mode", key="banner_try_demo", use_container_width=True, help="Explore with realistic sample channel data"):
                    set_demo_mode(True)
                    st.rerun()
        st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)

    # Case 2: Memory Disconnected (YouTube is connected, but Memory is not)
    elif not memory_creds:
        with st.container(border=True):
            col_m1, col_m2 = st.columns([3, 1])
            with col_m1:
                st.markdown(
                    """
                    <div style="display:flex; align-items:center; gap:10px;">
                        <span style="font-size:1.4rem;">📊</span>
                        <div>
                            <div style="font-weight:600; color:#CBD5E1; font-size:0.92rem;">Google Sheets Memory Disconnected</div>
                            <div style="color:#94A3B8; font-size:0.82rem;">Connect your Google Sheets via Drive scope to record metadata experiments and preserve channel voice guidelines across sessions.</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with col_m2:
                st.write("")
                mem_url, _ = get_auth_url("memory", subject or "", secrets)
                if mem_url:
                    st.link_button("📊 Connect Sheets", mem_url, use_container_width=True)
        st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)
