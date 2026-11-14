"""Daily owner-authorized YouTube strategy agent and guarded minor-metadata publisher."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

import core.batch_metadata as batch_metadata
import youtube_analytics_connector as connector
from core.channel_strategy_agent import (
    DEFAULT_STRATEGY_FALLBACKS,
    DEFAULT_STRATEGY_MODEL,
    _chunks,
    classify_metadata_change,
    collect_channel_snapshot,
    evaluate_autonomous_policy,
    generate_strategy_report,
)
from core.persistent_store import SupabasePersistentStore
from core.strategy_learning import (
    format_learning_feedback_for_prompt,
    record_channel_evaluations,
    rollback_strategy_action,
)
from metadata_snapshot_worker import resolve_channel_owner
from worker import (
    _cipher,
    _get_owner_credentials,
    _record_live_metadata_snapshot,
    _snapshot_allows_publish,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("youtube-channel-strategy-agent")
OWNER_CHANNEL_ID = connector.CHANNEL_ID


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_metadata(snippet: dict) -> dict:
    return {
        "title": snippet.get("title") or "",
        "description": snippet.get("description") or "",
        "tags": list(snippet.get("tags") or []),
        "categoryId": snippet.get("categoryId") or "",
    }


def _compact_snapshot(snapshot: dict) -> dict:
    """Persist enough provenance for diagnosis without copying the full video catalogue."""
    return {
        key: snapshot.get(key)
        for key in (
            "metrics_period", "catalogue", "channel_metrics",
            "previous_channel_metrics", "period_deltas_percent",
            "channel_metrics_status", "previous_metrics_status",
            "daily_trend_status", "daily_trend", "traffic_sources_status",
            "traffic_sources", "reach_status", "video_analytics_statuses",
            "top_videos_by_views", "top_videos_by_subscriber_conversion",
            "unscored_short_duration_uploads", "opportunities",
        )
    }


def _metadata_candidates(snapshot: dict, strategy_report: dict, limit: int = 3) -> list[dict]:
    """Preserve every Gemini candidate field and only accept evidence-backed packaging opportunities."""
    limit = max(0, min(int(limit), 10))
    if limit == 0:
        return []
    opportunities = snapshot.get("opportunities") or []
    by_id = {str(item.get("video_id")): item for item in opportunities if item.get("video_id")}
    selected = []
    for item in strategy_report.get("metadata_candidates") or []:
        video_id = str(item.get("video_id") or "")
        evidence = by_id.get(video_id)
        if not evidence:
            continue
        reasons = evidence.get("reasons") or []
        has_ctr_evidence = any(
            isinstance(reason, str) and reason.startswith("Thumbnail CTR is ")
            for reason in reasons
        )
        impressions = evidence.get("impressions")
        if not has_ctr_evidence or not isinstance(impressions, (int, float)) or impressions < 50:
            continue
        # Preserve EVERY field from Gemini's structured candidate without discarding
        merged = {
            **evidence,
            **item,
            "evidence_reasons": reasons,
            "reasons": reasons,
            "title": evidence.get("title") or item.get("video_title") or "",
            # The snapshot, not model output, is authoritative for the source metadata Gemini saw.
            "current_metadata": evidence.get("current_metadata") or {},
        }
        selected.append(merged)
        if len(selected) >= limit:
            break

    # If Gemini omitted a candidate despite an objective packaging bottleneck,
    # keep the deterministic evidence-ranked shortlist for manual review ONLY.
    # Fail-closed: decision must be 'review', confidence must be None, never auto_publish.
    if not selected:
        for evidence in opportunities:
            reasons = evidence.get("reasons") or []
            impressions = evidence.get("impressions")
            if (
                any(isinstance(reason, str) and reason.startswith("Thumbnail CTR is ") for reason in reasons)
                and isinstance(impressions, (int, float))
                and impressions >= 50
            ):
                selected.append({
                    **evidence,
                    "confidence": None,
                    "decision": "review",
                    "diagnosis": "packaging",
                    "fields_to_modify": [],
                    "proposed_title": "",
                    "proposed_description": "",
                    "proposed_tags": [],
                    "proposed_hashtags": [],
                    "factual_evidence": "",
                    "rationale": "Deterministic fallback: packaging bottleneck identified but Gemini proposal was absent. Fail-closed: held for review.",
                })
            if len(selected) >= limit:
                break
    return selected


def _prepare_gemini_metadata_proposals(youtube, candidates: list[dict]) -> list[dict]:
    """Validate Gemini's proposal against the exact source metadata included in its prompt."""
    if not candidates:
        return []

    video_ids = [str(c.get("video_id")) for c in candidates if c.get("video_id")]
    snippets_by_id = {}
    fetch_errors = {}
    for batch in _chunks(video_ids, 50):
        try:
            resp = youtube.videos().list(part="snippet", id=",".join(batch)).execute()
            for item in resp.get("items") or []:
                video_id = str(item.get("id") or "")
                if video_id:
                    snippets_by_id[video_id] = item.get("snippet") or {}
        except Exception as error:
            message = f"Could not fetch fresh YouTube metadata: {type(error).__name__}: {str(error)[:500]}"
            for video_id in batch:
                fetch_errors[video_id] = message
            log.warning("%s for batch %s", message, batch)

    rows = []
    for candidate in candidates:
        video_id = str(candidate.get("video_id") or "")
        source_metadata = candidate.get("current_metadata")

        def failed(message):
            return {
                "video_id": video_id,
                "status": "failed",
                "before": {},
                "proposed": {},
                "fields_to_modify": [],
                "errors": [message],
            }

        if not video_id:
            rows.append(failed("Candidate is missing a video_id."))
            continue

        if video_id in fetch_errors:
            rows.append(failed(fetch_errors[video_id]))
            continue

        live_snippet = snippets_by_id.get(video_id)
        if not isinstance(live_snippet, dict) or not live_snippet.get("title"):
            rows.append(failed("Fresh live YouTube metadata was not returned for this video; refusing to build a proposal from incomplete or stale context."))
            continue

        if not isinstance(source_metadata, dict):
            rows.append(failed("Gemini source metadata is missing; the proposal cannot be grounded to the prompt context."))
            continue

        required_source_keys = {"title", "description", "tags"}
        if not required_source_keys.issubset(source_metadata):
            rows.append(failed("Gemini source metadata is incomplete (title, description, and tags are required)."))
            continue

        source_tags = source_metadata.get("tags")
        if not isinstance(source_tags, list):
            rows.append(failed("Gemini source metadata tags are not a list; refusing to infer the original tags."))
            continue

        before_full = {
            "title": str(live_snippet.get("title") or ""),
            "description": str(live_snippet.get("description") or ""),
            "tags": [str(tag).strip() for tag in (live_snippet.get("tags") or []) if str(tag).strip()],
            "categoryId": str(live_snippet.get("categoryId") or source_metadata.get("category_id") or "22"),
        }
        expected_source = {
            "title": str(source_metadata.get("title") or ""),
            "description": str(source_metadata.get("description") or ""),
            "tags": [str(tag).strip() for tag in source_tags if str(tag).strip()],
        }
        actual_source = {
            "title": before_full["title"],
            "description": before_full["description"],
            "tags": before_full["tags"],
        }
        if actual_source != expected_source:
            rows.append(failed("Live YouTube metadata changed after Gemini received its context; discarding this stale proposal. Rerun the strategy worker to generate against the current metadata."))
            continue

        raw_fields = candidate.get("fields_to_modify")
        if not isinstance(raw_fields, list):
            rows.append(failed("Gemini must return fields_to_modify as an explicit list; no fields were inferred."))
            continue

        fields = []
        invalid_fields = []
        for raw_field in raw_fields:
            field_name = str(raw_field).strip().lower()
            if field_name not in {"title", "description", "tags"}:
                invalid_fields.append(field_name or repr(raw_field))
            elif field_name not in fields:
                fields.append(field_name)
        if invalid_fields:
            rows.append(failed("Gemini returned unsupported fields_to_modify value(s): " + ", ".join(invalid_fields)))
            continue

        proposed_full = {
            "title": before_full["title"],
            "description": before_full["description"],
            "tags": list(before_full["tags"]),
            "categoryId": before_full["categoryId"],
        }

        if "title" in fields:
            proposed_title = candidate.get("proposed_title")
            if not isinstance(proposed_title, str) or not proposed_title.strip():
                rows.append(failed("Gemini selected the title field but did not provide a non-empty proposed_title."))
                continue
            proposed_full["title"] = proposed_title.strip()

        if "description" in fields:
            proposed_description = candidate.get("proposed_description")
            if not isinstance(proposed_description, str) or not proposed_description.strip():
                rows.append(failed("Gemini selected the description field but did not provide a non-empty proposed_description."))
                continue
            desc = proposed_description.strip()
            hashtags = candidate.get("proposed_hashtags") or []
            if isinstance(hashtags, list):
                normalized_hashtags = [
                    str(tag).strip() for tag in hashtags if str(tag).strip()
                ]
                if normalized_hashtags and not any(tag.lower() in desc.lower() for tag in normalized_hashtags):
                    hashtag_text = " ".join(
                        tag if tag.startswith("#") else f"#{tag}"
                        for tag in normalized_hashtags
                    )
                    desc = f"{desc}\n\n{hashtag_text}".strip()
            proposed_full["description"] = desc

        if "tags" in fields:
            proposed_tags = candidate.get("proposed_tags")
            if not isinstance(proposed_tags, list):
                rows.append(failed("Gemini selected the tags field but did not provide proposed_tags as a list."))
                continue
            proposed_full["tags"] = [
                str(tag).strip() for tag in proposed_tags if str(tag).strip()
            ]

        rows.append({
            "video_id": video_id,
            "status": "ready",
            "before": before_full,
            "proposed": proposed_full,
            "fields_to_modify": fields,
            "errors": [],
        })
    return rows

def _daily_auto_count(store, owner_key: str) -> int:
    # Match the configured owner timezone rather than UTC midnight.
    local_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start = datetime.combine(local_today, time.min, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).isoformat()
    rows = store._request("GET", "channel_strategy_actions", params={
        "owner_key": f"eq.{owner_key}",
        "status": "in.(auto_published,auto_publishing,verification_unavailable,failed)",
        "executed_at": f"gte.{start}",
        "select": "id",
        "limit": "100",
    })
    return len(rows or [])


def _cadence_auto_count(store, owner_key: str, cadence: str) -> int:
    local_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if cadence == "monthly":
        period_start = date(local_today.year, local_today.month, 1)
    elif cadence == "weekly":
        period_start = local_today - timedelta(days=local_today.weekday())
    else:
        period_start = local_today
    start = datetime.combine(period_start, time.min, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).isoformat()
    rows = store._request("GET", "channel_strategy_actions", params={
        "owner_key": f"eq.{owner_key}",
        "cadence": f"eq.{cadence}",
        "status": "in.(auto_published,auto_publishing,verification_unavailable,failed)",
        "executed_at": f"gte.{start}",
        "select": "id",
        "limit": "100",
    })
    return len(rows or [])


def _update_action(store, action_id: str, owner_key: str, values: dict):
    store.update_strategy_action(action_id, owner_key, values)


def _recover_interrupted_actions(store, owner_key: str, *, stale_minutes: int = 20) -> int:
    """Never retry a possibly-sent write after a worker crash; mark it for manual verification."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).isoformat()
    rows = store._request("GET", "channel_strategy_actions", params={
        "owner_key": f"eq.{owner_key}",
        "status": "eq.auto_publishing",
        "executed_at": f"lt.{cutoff}",
        "select": "id,video_title,video_id",
        "limit": "100",
    })
    recovered = 0
    for item in rows or []:
        action_id = item.get("id")
        if not action_id:
            continue
        _update_action(store, action_id, owner_key, {
            "status": "verification_unavailable",
            "error": "The scheduled worker was interrupted during a metadata write. The request was not retried because YouTube may already have accepted it. Reload the live video metadata and verify manually before taking another action.",
            "updated_at": _utc_now(),
        })
        recovered += 1
    return recovered


def _process_candidate(
    store,
    youtube,
    owner_key: str,
    run_id: str,
    candidate: dict,
    row: dict,
    settings: dict,
    *,
    cadence: str = "daily",
    daily_count: int = 0,
    cadence_count: int = 0,
    model_name: str = "",
) -> tuple[str, int, int]:
    video_id = str(row.get("video_id") or candidate.get("video_id") or "")
    before_full = row.get("before") or {}
    proposed_full = row.get("proposed") or {}
    if row.get("status") != "ready" or not before_full or not proposed_full:
        error = "; ".join(str(item) for item in (row.get("errors") or [])) or row.get("result_message") or "No valid metadata proposal was returned."
        store.create_strategy_action(owner_key, {
            "run_id": run_id,
            "video_id": video_id,
            "video_title": candidate.get("title") or row.get("current_title") or "Untitled video",
            "priority": int(candidate.get("opportunity_score") or 0),
            "action_type": "metadata_optimization",
            "classification": "major",
            "status": "failed",
            "cadence": cadence,
            "confidence": candidate.get("confidence"),
            "diagnosis": candidate.get("diagnosis"),
            "reason": "The metadata proposal could not be validated.",
            "evidence": {"reasons": candidate.get("reasons") or []},
            "before_metadata": before_full if before_full else {},
            "proposed_metadata": {},
            "error": str(error)[:1500],
            "model_name": model_name,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        return "failed", daily_count, cadence_count

    before = _safe_metadata(before_full)
    proposed = _safe_metadata(proposed_full)

    # Check cooldown ledger
    try:
        cooldown = _record_live_metadata_snapshot(
            store, owner_key, video_id, before_full, source="worker"
        )
        cooldown_allowed = _snapshot_allows_publish(cooldown)
        cooldown_reason = "" if cooldown_allowed else "The seven-day metadata baseline/cooldown is not satisfied."
    except Exception as error:
        cooldown = {}
        cooldown_allowed = False
        cooldown_reason = f"Could not check cooldown ledger: {error}"

    candidate_decision = {
        "confidence": candidate.get("confidence"),
        "decision": candidate.get("decision"),
        "rationale": candidate.get("rationale") or candidate.get("reason") or "",
        "cadence": cadence,
        "diagnosis": candidate.get("diagnosis", "packaging"),
        "fields_to_modify": candidate.get("fields_to_modify", []),
        "factual_evidence": candidate.get("factual_evidence", ""),
    }

    policy_decision, policy_reason, changed_fields = evaluate_autonomous_policy(
        settings,
        candidate_decision,
        before,
        proposed,
        cooldown_allows_publish=cooldown_allowed,
        cooldown_reason=cooldown_reason,
        daily_count=daily_count,
        cadence_count=cadence_count,
    )

    if policy_decision == "no_change":
        store.create_strategy_action(owner_key, {
            "run_id": run_id,
            "video_id": video_id,
            "video_title": before.get("title") or candidate.get("title") or "Untitled video",
            "priority": int(candidate.get("opportunity_score") or 0),
            "action_type": "metadata_optimization",
            "classification": "none",
            "status": "no_change",
            "cadence": cadence,
            "confidence": candidate.get("confidence"),
            "diagnosis": candidate.get("diagnosis"),
            "reason": policy_reason,
            "evidence": {"reasons": candidate.get("reasons") or []},
            "before_metadata": before_full,
            "proposed_metadata": proposed_full,
            "model_name": model_name,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        return "no_change", daily_count, cadence_count

    if policy_decision == "rejected":
        store.create_strategy_action(owner_key, {
            "run_id": run_id,
            "video_id": video_id,
            "video_title": before.get("title") or candidate.get("title") or "Untitled video",
            "priority": int(candidate.get("opportunity_score") or 0),
            "action_type": "metadata_optimization",
            "classification": "major",
            "status": "failed",
            "cadence": cadence,
            "confidence": candidate.get("confidence"),
            "diagnosis": candidate.get("diagnosis"),
            "reason": policy_reason,
            "evidence": {"reasons": candidate.get("reasons") or [], "changed_fields": changed_fields},
            "before_metadata": before_full,
            "proposed_metadata": proposed_full,
            "error": policy_reason[:1500],
            "model_name": model_name,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        return "failed", daily_count, cadence_count

    digest = connector.metadata_proposal_digest(video_id, before_full, proposed_full)
    classification = "minor" if len(changed_fields) == 1 and changed_fields[0] == "tags" else "major"

    if policy_decision == "shadow_publish":
        store.create_strategy_action(owner_key, {
            "run_id": run_id,
            "video_id": video_id,
            "video_title": before.get("title") or candidate.get("title") or "Untitled video",
            "priority": int(candidate.get("opportunity_score") or 0),
            "action_type": "metadata_optimization",
            "classification": classification,
            "status": "shadow_published",
            "cadence": cadence,
            "confidence": candidate.get("confidence"),
            "diagnosis": candidate.get("diagnosis"),
            "reason": policy_reason,
            "evidence": {
                "reasons": candidate.get("reasons") or [],
                "views": candidate.get("views"),
                "impressions": candidate.get("impressions"),
                "ctr_percent": candidate.get("ctr_percent"),
                "average_view_percentage": candidate.get("average_view_percentage"),
                "subscribers_gained": candidate.get("subscribers_gained"),
                "subscribers_per_1000_views": candidate.get("subscribers_per_1000_views"),
                "changed_fields": changed_fields,
                "shadow": True,
                "cooldown_blocked": not cooldown_allowed,
                "cooldown_reason": cooldown_reason if not cooldown_allowed else "",
                "factual_evidence": candidate.get("factual_evidence") or "",
                "rationale": candidate.get("rationale") or "",
            },
            "before_metadata": before_full,
            "proposed_metadata": proposed_full,
            "approval_digest": digest,
            "model_name": model_name,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        return "shadow_published", daily_count, cadence_count

    if policy_decision == "pending_review":
        store.create_strategy_action(owner_key, {
            "run_id": run_id,
            "video_id": video_id,
            "video_title": before.get("title") or candidate.get("title") or "Untitled video",
            "priority": int(candidate.get("opportunity_score") or 0),
            "action_type": "metadata_optimization",
            "classification": classification,
            "status": "pending_review",
            "cadence": cadence,
            "confidence": candidate.get("confidence"),
            "diagnosis": candidate.get("diagnosis"),
            "reason": policy_reason,
            "evidence": {
                "reasons": candidate.get("reasons") or [],
                "views": candidate.get("views"),
                "impressions": candidate.get("impressions"),
                "ctr_percent": candidate.get("ctr_percent"),
                "average_view_percentage": candidate.get("average_view_percentage"),
                "subscribers_gained": candidate.get("subscribers_gained"),
                "subscribers_per_1000_views": candidate.get("subscribers_per_1000_views"),
                "changed_fields": changed_fields,
                "cooldown_blocked": not cooldown_allowed,
                "cooldown_reason": cooldown_reason if not cooldown_allowed else "",
                "factual_evidence": candidate.get("factual_evidence") or "",
                "rationale": candidate.get("rationale") or "",
            },
            "before_metadata": before_full,
            "proposed_metadata": proposed_full,
            "approval_digest": digest,
            "model_name": model_name,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        return "pending_review", daily_count, cadence_count

    # Autonomous write path
    action = store.create_strategy_action(owner_key, {
        "run_id": run_id,
        "video_id": video_id,
        "video_title": before.get("title") or candidate.get("title") or "Untitled video",
        "priority": int(candidate.get("opportunity_score") or 0),
        "action_type": "metadata_optimization",
        "classification": classification,
        "status": "auto_publishing",
        "cadence": cadence,
        "confidence": candidate.get("confidence"),
        "diagnosis": candidate.get("diagnosis"),
        "reason": policy_reason,
        "evidence": {
            "reasons": candidate.get("reasons") or [],
            "views": candidate.get("views"),
            "impressions": candidate.get("impressions"),
            "ctr_percent": candidate.get("ctr_percent"),
            "average_view_percentage": candidate.get("average_view_percentage"),
            "subscribers_gained": candidate.get("subscribers_gained"),
            "subscribers_per_1000_views": candidate.get("subscribers_per_1000_views"),
            "changed_fields": changed_fields,
        },
        "before_metadata": before_full,
        "proposed_metadata": proposed_full,
        "approval_digest": digest,
        "model_name": model_name,
        "executed_at": _utc_now(),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    })
    action_id = action.get("id")

    def audit_callback(entry):
        _update_action(store, action_id, owner_key, {
            "audit_log": entry,
            "updated_at": _utc_now(),
        })

    try:
        applied = connector.apply_approved_dashboard_proposal(
            youtube, video_id, before_full, proposed_full, digest,
            audit_callback=audit_callback,
        )
        if not isinstance(applied, dict) or applied.get("result") != "success":
            raise RuntimeError("YouTube did not confirm a successful metadata update.")
        after = None
        after_snapshot_warning = None
        try:
            after_full = connector.fetch_video_snippet(youtube, video_id)
            after = _safe_metadata(after_full)
        except Exception as snapshot_error:
            after_snapshot_warning = f"{type(snapshot_error).__name__}: {str(snapshot_error)[:800]}"
        _update_action(store, action_id, owner_key, {
            "status": "auto_published",
            "after_metadata": after,
            "result": {
                "result": applied.get("result"),
                "url": applied.get("url"),
                "post_write_snapshot_warning": after_snapshot_warning,
            },
            "error": None,
            "updated_at": _utc_now(),
        })
        return "auto_published", daily_count + 1, cadence_count + 1
    except connector.MetadataVerificationUnavailableError as error:
        _update_action(store, action_id, owner_key, {
            "status": "verification_unavailable",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
            "updated_at": _utc_now(),
        })
        return "verification_unavailable", daily_count, cadence_count
    except Exception as error:
        _update_action(store, action_id, owner_key, {
            "status": "failed",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
            "updated_at": _utc_now(),
        })
        return "failed", daily_count, cadence_count


def run_strategy_agent(store, cipher, *, trigger_source: str = "schedule", cadence: str = "daily") -> dict:
    """Execute one end-to-end cadence-aware analytics, strategy, proposal, and guarded-publish cycle."""
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise RuntimeError("GEMINI_API_KEY is required for the Gemini-first autonomous channel strategist. Configure it as a GitHub Actions repository secret.")

    cadence = str(cadence or os.getenv("STRATEGY_CADENCE", "daily")).strip().lower()
    if cadence not in {"daily", "weekly", "monthly"}:
        cadence = "daily"

    owner_key, _ = resolve_channel_owner(store, cipher, OWNER_CHANNEL_ID)
    credentials = _get_owner_credentials(store, owner_key, cipher)
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    analytics = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)
    reporting = build("youtubereporting", "v1", credentials=credentials, cache_discovery=False)

    settings = store.get_strategy_settings(owner_key)

    # Concurrency and idempotency protection
    local_now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if cadence == "monthly":
        period_key = local_now.strftime("%Y-%m")
    elif cadence == "weekly":
        period_key = local_now.strftime("%Y-W%W")
    else:
        period_key = local_now.strftime("%Y-%m-%d")

    base_dedup_key = f"{OWNER_CHANNEL_ID}_{cadence}_{period_key}"

    # Check for active running jobs in last 25 minutes
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    active_runs = store._request("GET", "channel_strategy_runs", params={
        "owner_key": f"eq.{owner_key}",
        "status": "eq.running",
        "started_at": f"gte.{stale_cutoff}",
        "select": "id,cadence,started_at",
        "limit": "1",
    })
    if active_runs and trigger_source in {"schedule", "supabase_watchdog"}:
        log.warning("Another strategy run is currently active (%s). Concurrency guard holding execution.", active_runs[0].get("id"))
        return {"status": "concurrency_held", "message": "Another strategy run is currently active."}

    existing_period_runs = store._request("GET", "channel_strategy_runs", params={
        "owner_key": f"eq.{owner_key}",
        "dedup_key": f"like.{base_dedup_key}*",
        "select": "id,status,started_at,dedup_key",
        "order": "started_at.desc",
        "limit": "5",
    })

    completed_runs = [r for r in (existing_period_runs or []) if r.get("status") == "completed"]
    if trigger_source == "supabase_watchdog" and existing_period_runs:
        latest = existing_period_runs[0]
        log.info(
            "Supabase watchdog found an existing %s run for %s (%s). Skipping duplicate dispatch.",
            latest.get("status"), cadence, period_key,
        )
        return {"status": "already_started", "message": f"A {cadence} run for {period_key} is already recorded."}
    if completed_runs and trigger_source == "schedule":
        log.info("Strategy run for %s (%s) already completed. Skipping scheduled run.", cadence, period_key)
        return {"status": "already_completed", "message": f"Strategy run for {cadence} {period_key} has already completed."}

    # The watchdog shares the scheduled dedup key, so a race between both triggers
    # is stopped by the existing unique (owner_key, dedup_key) index in Postgres.
    if trigger_source in ("workflow_dispatch", "manual"):
        dedup_key = f"{base_dedup_key}_manual_{datetime.now(timezone.utc).strftime('%H%M%S')}"
    elif trigger_source == "supabase_watchdog":
        dedup_key = base_dedup_key
    elif existing_period_runs and any(r.get("status") in ("failed", "partial") for r in existing_period_runs):
        dedup_key = f"{base_dedup_key}_retry_{datetime.now(timezone.utc).strftime('%H%M%S')}"
    else:
        dedup_key = base_dedup_key

    run = store.create_strategy_run(owner_key, {
        "channel_id": OWNER_CHANNEL_ID,
        "trigger_source": trigger_source,
        "cadence": cadence,
        "dedup_key": dedup_key,
        "status": "running",
        "started_at": _utc_now(),
    })
    run_id = run["id"]
    counters = {
        "proposals": 0, "auto_published": 0, "shadow_published": 0,
        "pending_review": 0, "failed": 0, "no_change": 0, "verification_unavailable": 0,
    }
    try:
        counters["verification_unavailable"] += _recover_interrupted_actions(store, owner_key)

        # Run empirical learning loop pass before strategic evaluation
        try:
            record_channel_evaluations(store, analytics, reporting, credentials, owner_key)
        except Exception as eval_error:
            log.warning("Non-fatal learning loop evaluation check error: %s", eval_error)

        learning_feedback = format_learning_feedback_for_prompt(store, owner_key)

        catalogue_limit_raw = os.getenv("STRATEGY_CATALOGUE_LIMIT", "all").strip()
        catalogue_limit = None if catalogue_limit_raw.lower() in {"none", "0", "all", "unlimited", ""} else int(catalogue_limit_raw)

        snapshot = collect_channel_snapshot(
            youtube, analytics, reporting, credentials, OWNER_CHANNEL_ID,
            catalogue_limit=catalogue_limit,
            cadence=cadence,
        )
        model = os.getenv("GEMINI_MODEL", DEFAULT_STRATEGY_MODEL).strip()
        fallback_models = [
            value.strip() for value in os.getenv(
                "GEMINI_FALLBACK_MODELS", ",".join(DEFAULT_STRATEGY_FALLBACKS)
            ).split(",") if value.strip()
        ]
        strategy_report = {}
        model_used = "deterministic"
        analysis_error = None
        try:
            strategy_report, model_used = generate_strategy_report(
                snapshot, gemini_key,
                model=model,
                fallback_models=fallback_models,
                cadence=cadence,
                learning_feedback=learning_feedback,
            )
        except Exception as error:
            analysis_error = f"{type(error).__name__}: {str(error)[:1200]}"
            strategy_report = {
                "executive_summary": "Gemini strategy analysis was unavailable. Deterministic channel evidence and opportunities were saved; no model-generated content strategy was assumed.",
                "patterns": [],
                "growth_actions": [],
                "content_ideas": [],
                "metadata_candidates": [],
            }

        candidates = _metadata_candidates(
            snapshot, strategy_report,
            limit=max(0, min(int(settings.get("max_metadata_reviews_per_run", 3)), 10)),
        )

        if candidates:
            daily_count = _daily_auto_count(store, owner_key)
            cadence_count = _cadence_auto_count(store, owner_key, cadence)
            try:
                rows = _prepare_gemini_metadata_proposals(youtube, candidates)
            except Exception as proposal_error:
                rows = [
                    {
                        "video_id": candidate["video_id"],
                        "status": "failed",
                        "errors": [f"{type(proposal_error).__name__}: {str(proposal_error)[:1000]}"],
                    }
                    for candidate in candidates
                ]
            for index, candidate in enumerate(candidates):
                row = rows[index] if index < len(rows) else {
                    "video_id": candidate["video_id"],
                    "status": "failed",
                    "errors": ["No generated proposal row was returned."],
                }
                counters["proposals"] += 1
                metadata_model = f"gemini:{model_used}" if model_used else "gemini"
                result, daily_count, cadence_count = _process_candidate(
                    store, youtube, owner_key, run_id, candidate, row, settings,
                    cadence=cadence,
                    daily_count=daily_count,
                    cadence_count=cadence_count,
                    model_name=metadata_model,
                )
                if result in counters:
                    counters[result] += 1

        status = "partial" if analysis_error or counters["failed"] or counters["verification_unavailable"] else "completed"
        stored_snapshot = _compact_snapshot(snapshot)
        store.update_strategy_run(run_id, owner_key, {
            "status": status,
            "cadence": cadence,
            "metrics_period": snapshot.get("metrics_period") or {},
            "metrics_snapshot": stored_snapshot,
            "strategy_report": strategy_report,
            "model_name": model_used,
            "error": analysis_error,
            "result": counters,
            "finished_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        log.info("Channel strategy %s run %s completed: %s", cadence, run_id, counters)
        return {"run_id": run_id, "cadence": cadence, "status": status, **counters}
    except Exception as error:
        store.update_strategy_run(run_id, owner_key, {
            "status": "failed",
            "cadence": cadence,
            "error": f"{type(error).__name__}: {str(error)[:1800]}",
            "finished_at": _utc_now(),
            "updated_at": _utc_now(),
        })
        log.exception("Channel strategy %s run %s failed", cadence, run_id)
        raise


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Autonomous YouTube Metadata Optimization Engine")
    parser.add_argument(
        "--cadence",
        choices=["daily", "weekly", "monthly"],
        default=os.getenv("STRATEGY_CADENCE", "daily"),
        help="Monitoring and optimization cadence (default: daily)",
    )
    parser.add_argument(
        "--trigger-source",
        default=os.getenv("STRATEGY_TRIGGER_SOURCE", "schedule"),
        help="Trigger source provenance (schedule, manual, workflow_dispatch)",
    )
    args = parser.parse_args()

    store = SupabasePersistentStore.worker_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )
    cipher = _cipher(os.environ["YOUTUBE_STRATEGIST_STATE_SECRET"])
    result = run_strategy_agent(store, cipher, trigger_source=args.trigger_source, cadence=args.cadence)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
