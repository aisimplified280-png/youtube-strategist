"""Channel analytics comparisons, period presets, and ranking logic."""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import youtube_dashboard


def get_period_preset(preset: str) -> Tuple[date, date, date, date]:
    """Calculate current and equal-length prior period date ranges.
    
    Returns:
        (current_start, current_end, prior_start, prior_end)
    """
    yesterday = date.today() - timedelta(days=1)
    days_map = {"7d": 7, "28d": 28, "90d": 90}
    num_days = days_map.get(preset, 28)

    current_end = yesterday
    current_start = current_end - timedelta(days=num_days - 1)

    prior_end = current_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=num_days - 1)

    return current_start, current_end, prior_start, prior_end


def calculate_custom_period(start_date: date, end_date: date) -> Tuple[date, date, date, date]:
    """Calculate current and equal-length prior period for custom start/end dates.
    
    Returns:
        (sanitized_start, sanitized_end, prior_start, prior_end)
    """
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    num_days = (end_date - start_date).days + 1
    prior_end = start_date - timedelta(days=1)
    prior_start = prior_end - timedelta(days=num_days - 1)
    return start_date, end_date, prior_start, prior_end


def calculate_delta(current_val: Optional[float], prior_val: Optional[float], is_percent: bool = False) -> Tuple[Optional[str], str]:
    """Calculate human-readable delta and trend type ('positive', 'negative', 'neutral')."""
    if current_val is None or prior_val is None:
        return None, "neutral"

    diff = current_val - prior_val
    if prior_val == 0:
        if diff == 0:
            return "0%", "neutral"
        return f"+{diff:g}", "positive" if diff > 0 else "negative"

    pct = (diff / abs(prior_val)) * 100
    sign = "+" if pct > 0 else ""
    formatted = f"{sign}{pct:.1f}%" if not is_percent else f"{sign}{diff:.1f} pts"
    delta_type = "positive" if diff > 0 else "negative" if diff < 0 else "neutral"
    return formatted, delta_type


def rank_candidates(video_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank optimization candidates using conservative peer comparisons.
    
    Requires at least 3 format- and age-matched peers.
    """
    return youtube_dashboard.rank_optimization_candidates(video_rows)
