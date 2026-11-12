"""Learning loop, multi-window performance evaluation, and safe rollback for YouTube metadata."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import youtube_analytics_connector as connector

log = logging.getLogger("youtube-strategy-learning")

EVALUATION_WINDOWS = (7, 14, 28)
VIDEO_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_metric_deltas(
    baseline: Dict[str, Any],
    post: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute absolute and percentage deltas between baseline and post-change metrics."""
    deltas: Dict[str, Any] = {}

    # CTR
    base_ctr = baseline.get("ctr_percent")
    post_ctr = post.get("ctr_percent")
    if _number(base_ctr) and _number(post_ctr):
        deltas["ctr_delta_absolute"] = round(post_ctr - base_ctr, 3)
        if base_ctr > 0:
            deltas["ctr_growth_percent"] = round(((post_ctr - base_ctr) / base_ctr) * 100, 2)

    # Impressions
    base_imp = baseline.get("impressions")
    post_imp = post.get("impressions")
    if _number(base_imp) and _number(post_imp):
        deltas["impressions_delta"] = post_imp - base_imp
        if base_imp > 0:
            deltas["impressions_growth_percent"] = round(((post_imp - base_imp) / base_imp) * 100, 2)

    # Views
    base_views = baseline.get("views")
    post_views = post.get("views")
    if _number(base_views) and _number(post_views):
        deltas["views_delta"] = post_views - base_views
        if base_views > 0:
            deltas["views_growth_percent"] = round(((post_views - base_views) / base_views) * 100, 2)

    # Average view percentage
    base_avp = baseline.get("average_view_percentage")
    post_avp = post.get("average_view_percentage")
    if _number(base_avp) and _number(post_avp):
        deltas["avp_delta_absolute"] = round(post_avp - base_avp, 2)

    # Subscribers gained
    base_subs = baseline.get("subscribers_gained")
    post_subs = post.get("subscribers_gained")
    if _number(base_subs) and _number(post_subs):
        deltas["subs_delta"] = post_subs - base_subs

    return deltas


def classify_evaluation_outcome(
    baseline: Dict[str, Any],
    post: Dict[str, Any],
    deltas: Dict[str, Any],
) -> str:
    """Classify optimization outcome into: improved, no_change, declined, or inconclusive.

    - improved: CTR delta >= +0.3% absolute OR views growth >= +15% with non-negative CTR delta.
    - declined: CTR delta <= -0.5% absolute OR views growth <= -20%.
    - inconclusive: impressions < 50 in post window or metric data unavailable.
    - no_change: metrics remain within normal variance band.
    """
    post_imp = post.get("impressions")
    if not _number(post_imp) or post_imp < 50:
        # Without at least 50 post-impressions, sample size is insufficient to judge
        if not _number(post.get("views")) or post.get("views", 0) < 10:
            return "inconclusive"

    ctr_delta = deltas.get("ctr_delta_absolute")
    views_growth = deltas.get("views_growth_percent")

    # If CTR improved significantly
    if _number(ctr_delta) and ctr_delta >= 0.3:
        return "improved"

    # If views grew significantly without CTR dropping significantly
    if _number(views_growth) and views_growth >= 15.0:
        if ctr_delta is None or ctr_delta >= -0.2:
            return "improved"

    # If CTR suffered
    if _number(ctr_delta) and ctr_delta <= -0.5:
        return "declined"

    # If views dropped significantly
    if _number(views_growth) and views_growth <= -20.0:
        return "declined"

    return "no_change"


def evaluate_action_performance(
    analytics,
    reporting,
    credentials,
    action: Dict[str, Any],
    observation_days: int,
    *,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Measure post-change performance vs matched baseline pre-change window."""
    today = today or date.today()
    executed_str = action.get("executed_at") or action.get("created_at")
    if not executed_str:
        return None

    try:
        if "T" in executed_str:
            exec_date = datetime.fromisoformat(executed_str.replace("Z", "+00:00")).date()
        else:
            exec_date = date.fromisoformat(executed_str[:10])
    except (ValueError, TypeError):
        return None

    days_since = (today - exec_date).days
    # Allow 3 days YouTube Analytics lag
    required_days = observation_days + 3
    if days_since < required_days:
        return None

    video_id = action.get("video_id")
    if not video_id:
        return None

    # Baseline window: [exec_date - observation_days .. exec_date - 1]
    base_start = exec_date - timedelta(days=observation_days)
    base_end = exec_date - timedelta(days=1)

    # Post window: [exec_date + 1 .. exec_date + observation_days]
    post_start = exec_date + timedelta(days=1)
    post_end = exec_date + timedelta(days=observation_days)

    try:
        base_rows = connector._query_analytics(
            analytics, base_start.isoformat(), base_end.isoformat(),
            VIDEO_METRICS, video_ids=[video_id], dimensions="video",
        )
        post_rows = connector._query_analytics(
            analytics, post_start.isoformat(), post_end.isoformat(),
            VIDEO_METRICS, video_ids=[video_id], dimensions="video",
        )
    except Exception as error:
        log.warning("Analytics query failed for video %s evaluation: %s", video_id, error)
        return None

    base_metric = base_rows[0] if base_rows else {}
    post_metric = post_rows[0] if post_rows else {}

    # Reach / CTR queries
    import youtube_dashboard
    try:
        base_reach, base_status = youtube_dashboard.fetch_reach_metrics(
            reporting, credentials, [video_id], base_start, base_end
        )
        post_reach, post_status = youtube_dashboard.fetch_reach_metrics(
            reporting, credentials, [video_id], post_start, post_end
        )
    except Exception as error:
        log.warning("Reporting reach query failed for video %s evaluation: %s", video_id, error)
        base_reach, post_reach = {}, {}

    base_r = base_reach.get(video_id) or {}
    post_r = post_reach.get(video_id) or {}

    baseline_data = {
        "period": f"{base_start.isoformat()}..{base_end.isoformat()}",
        "views": base_metric.get("views"),
        "impressions": base_r.get("impressions"),
        "ctr_percent": base_r.get("ctr"),
        "average_view_percentage": base_metric.get("averageViewPercentage"),
        "subscribers_gained": base_metric.get("subscribersGained"),
    }
    post_data = {
        "period": f"{post_start.isoformat()}..{post_end.isoformat()}",
        "views": post_metric.get("views"),
        "impressions": post_r.get("impressions"),
        "ctr_percent": post_r.get("ctr"),
        "average_view_percentage": post_metric.get("averageViewPercentage"),
        "subscribers_gained": post_metric.get("subscribersGained"),
    }

    deltas = compute_metric_deltas(baseline_data, post_data)
    outcome = classify_evaluation_outcome(baseline_data, post_data, deltas)

    return {
        "action_id": action.get("id"),
        "video_id": video_id,
        "observation_days": observation_days,
        "baseline_metrics": baseline_data,
        "post_metrics": post_data,
        "metric_deltas": deltas,
        "outcome": outcome,
        "evaluated_at": _utc_now(),
    }


def record_channel_evaluations(
    store,
    analytics,
    reporting,
    credentials,
    owner_key: str,
    *,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Scan published actions and record evaluations for mature observation windows."""
    recorded = []
    actions = store.list_strategy_actions(status="auto_published", limit=50)
    actions.extend(store.list_strategy_actions(status="approved_published", limit=50))

    existing_evals = store.list_action_evaluations(limit=100)
    evaluated_keys = {
        (ev.get("action_id"), ev.get("observation_days"))
        for ev in existing_evals
    }

    for action in actions:
        action_id = action.get("id")
        if not action_id:
            continue
        for window in EVALUATION_WINDOWS:
            if (action_id, window) in evaluated_keys:
                continue
            eval_data = evaluate_action_performance(
                analytics, reporting, credentials, action, window, today=today
            )
            if eval_data:
                try:
                    res = store.create_action_evaluation(owner_key, eval_data)
                    recorded.append(res)
                    evaluated_keys.add((action_id, window))
                except Exception as error:
                    log.warning("Could not persist evaluation for action %s window %sd: %s", action_id, window, error)
    return recorded


def format_learning_feedback_for_prompt(
    store,
    owner_key: str,
    limit: int = 6,
) -> str:
    """Format past optimization results as structured learning guidance for Gemini."""
    try:
        evals = store.list_action_evaluations(limit=limit * 2)
    except Exception:
        return ""

    if not evals:
        return ""

    lines = []
    actions_cache = {}
    for ev in evals:
        outcome = ev.get("outcome")
        if outcome not in {"improved", "declined"}:
            continue
        action_id = ev.get("action_id")
        action = actions_cache.get(action_id)
        if not action:
            try:
                action = store.get_strategy_action(action_id) or {}
                actions_cache[action_id] = action
            except Exception:
                action = {}

        title = action.get("video_title") or f"Video {ev.get('video_id')}"
        days = ev.get("observation_days")
        deltas = ev.get("metric_deltas") or {}
        ctr_d = deltas.get("ctr_delta_absolute")
        ctr_str = f"{ctr_d:+.2f}% CTR" if _number(ctr_d) else ""
        views_g = deltas.get("views_growth_percent")
        views_str = f"{views_g:+.1f}% views" if _number(views_g) else ""
        delta_summary = ", ".join(filter(None, [ctr_str, views_str])) or outcome

        proposed = action.get("proposed_metadata") or {}
        prop_title = proposed.get("title") or ""

        lines.append(
            f"- Video '{title}' ({days}-day post test: {outcome.upper()} [{delta_summary}]): "
            f"Tested title: '{prop_title}'. Learn from this outcome: "
            + ("replicate this clarity and packaging formula." if outcome == "improved" else "avoid this phrasing style or misleading framing.")
        )
        if len(lines) >= limit:
            break

    if not lines:
        return ""

    return (
        "Empirical Learning from Past Automated Optimizations on this Channel:\n"
        + "\n".join(lines)
        + "\nUse these real channel outcomes as evidence to calibrate your current proposal angles and confidence.\n"
    )


def rollback_strategy_action(
    store,
    youtube,
    owner_key: str,
    action_id: str,
) -> Tuple[bool, str]:
    """Safely restore a video's stored before_metadata to undo a previous optimization."""
    if not store:
        return False, "Persistent store is required."
    if not youtube:
        return False, "Connected YouTube client is required for rollback."

    action = store.get_strategy_action(action_id)
    if not action:
        return False, f"Action {action_id} not found."

    status = action.get("status")
    if status not in {"auto_published", "approved_published", "shadow_published"}:
        return False, f"Action status '{status}' cannot be rolled back. Only published actions qualify."

    video_id = action.get("video_id")
    before_meta = action.get("before_metadata") or {}
    if not video_id or not before_meta.get("title"):
        return False, "Action does not have valid stored before_metadata to restore."

    # If it was only shadow published, update store without YouTube API call
    if status == "shadow_published":
        store.update_strategy_action(action_id, owner_key, {
            "status": "rolled_back",
            "reason": (action.get("reason") or "") + " (Shadow proposal rolled back/cleared by owner.)",
            "updated_at": _utc_now(),
        })
        return True, "Shadow proposal marked as rolled back."

    # Live rollback
    try:
        current_live = connector.fetch_video_snippet(youtube, video_id)
        current_meta = {
            "title": current_live.get("title") or "",
            "description": current_live.get("description") or "",
            "tags": list(current_live.get("tags") or []),
            "categoryId": current_live.get("categoryId") or "",
        }
    except Exception as error:
        return False, f"Could not fetch current live video metadata: {error}"

    # Target metadata to restore is the original before_metadata
    target_meta = {
        "title": before_meta.get("title") or "",
        "description": before_meta.get("description") or "",
        "tags": list(before_meta.get("tags") or []),
        "categoryId": before_meta.get("categoryId") or current_meta.get("categoryId") or "22",
    }

    digest = connector.metadata_proposal_digest(video_id, current_meta, target_meta)

    def audit_callback(entry):
        store.update_strategy_action(action_id, owner_key, {
            "audit_log": entry,
            "updated_at": _utc_now(),
        })

    try:
        applied = connector.apply_approved_dashboard_proposal(
            youtube, video_id, current_meta, target_meta, digest,
            audit_callback=audit_callback,
        )
        if not isinstance(applied, dict) or applied.get("result") != "success":
            raise RuntimeError("YouTube API did not confirm successful rollback.")

        # Record rollback action record
        store.create_strategy_action(owner_key, {
            "run_id": action.get("run_id"),
            "video_id": video_id,
            "video_title": target_meta["title"],
            "priority": 100,
            "action_type": "metadata_optimization",
            "classification": "major",
            "status": "approved_published",
            "cadence": action.get("cadence") or "daily",
            "rollback_of": action_id,
            "reason": f"Rollback to pre-optimization metadata (restoring original state from action {action_id}).",
            "evidence": {"rollback": True, "original_action_id": action_id},
            "before_metadata": current_meta,
            "proposed_metadata": target_meta,
            "after_metadata": target_meta,
            "approval_digest": digest,
            "model_name": "rollback_system",
            "executed_at": _utc_now(),
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        })

        # Mark original action as rolled_back
        store.update_strategy_action(action_id, owner_key, {
            "status": "rolled_back",
            "reason": (action.get("reason") or "") + f" [Rolled back to original metadata on {_utc_now()}]",
            "updated_at": _utc_now(),
        })

        return True, f"Successfully rolled back video '{target_meta['title']}' to its pre-optimization state."
    except Exception as error:
        return False, f"Rollback failed: {type(error).__name__}: {str(error)[:500]}"

