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
from strategist_chat import build_batch_metadata_generator
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
    """Only package-review opportunities with measured CTR evidence can trigger metadata proposals."""
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
        selected.append({**evidence, "strategist_reason": item.get("reason") or ""})
        if len(selected) >= limit:
            break

    # If Gemini omitted a candidate despite an objective packaging bottleneck,
    # keep the deterministic evidence-ranked shortlist; Gemini is the interpreter,
    # not the source of the underlying performance measurements.
    if not selected:
        for evidence in opportunities:
            reasons = evidence.get("reasons") or []
            impressions = evidence.get("impressions")
            if (
                any(isinstance(reason, str) and reason.startswith("Thumbnail CTR is ") for reason in reasons)
                and isinstance(impressions, (int, float))
                and impressions >= 50
            ):
                selected.append(evidence)
            if len(selected) >= limit:
                break
    return selected


def _prepare_metadata_proposals(youtube, secrets, candidates):
    if not candidates:
        return []
    ai_generator = build_batch_metadata_generator(secrets, youtube=youtube)
    if ai_generator is None:
        raise RuntimeError("No configured AI metadata generator is available.")

    plan_rows = []
    for index, candidate in enumerate(candidates, 1):
        ctr = candidate.get("ctr_percent")
        impressions = candidate.get("impressions")
        reasons = candidate.get("reasons") or []
        performance_evidence = " ".join(str(reason) for reason in reasons)
        instruction = (
            "Review and optimize the complete metadata package only for an evidence-backed packaging issue. "
            "The measured channel evidence is: "
            f"{performance_evidence} Impressions in the measured period: {impressions}. "
            f"Measured CTR: {ctr}. Do not claim the edit guarantees higher views or subscribers. "
            "Use only the supplied title, description and accessible transcript as factual evidence. "
            "Keep technical names, product names, URLs, timestamps and existing factual claims accurate."
        )
        plan_rows.append({
            "video_id": candidate["video_id"],
            "context_title": candidate.get("title") or "",
            "title_intent": "optimize",
            "description_intent": "optimize",
            "tags_intent": "optimize",
            "fields_requested": ["title", "description", "tags"],
            "original_instruction": instruction,
            "status": "parsed",
            "errors": [],
        })

    canonical = batch_metadata.create_batch_from_text_plan({"raw_text": "Scheduled channel strategy review", "videos": plan_rows})
    preloaded_snippets, preloaded_errors = batch_metadata.fetch_owner_verified_snippets(youtube, canonical["rows"])
    prepared = batch_metadata.prepare_metadata_batch(
        youtube,
        canonical,
        secrets=secrets,
        ai_generator=ai_generator,
        preloaded_snippets=preloaded_snippets,
        preloaded_fetch_errors=preloaded_errors,
    )
    return prepared.get("rows") or []


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
        "confidence": candidate.get("confidence", 0.85),
        "decision": candidate.get("decision", "auto_publish"),
        "rationale": candidate.get("rationale") or candidate.get("reason") or "",
        "cadence": cadence,
        "diagnosis": candidate.get("diagnosis", "packaging"),
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

    dedup_key = f"{OWNER_CHANNEL_ID}_{cadence}_{period_key}"

    # Check for active running jobs in last 25 minutes
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    active_runs = store._request("GET", "channel_strategy_runs", params={
        "owner_key": f"eq.{owner_key}",
        "status": "eq.running",
        "started_at": f"gte.{stale_cutoff}",
        "select": "id,cadence,started_at",
        "limit": "1",
    })
    if active_runs and trigger_source == "schedule":
        log.warning("Another strategy run is currently active (%s). Concurrency guard holding execution.", active_runs[0].get("id"))
        return {"status": "concurrency_held", "message": "Another strategy run is currently active."}

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

        catalogue_limit_raw = os.getenv("STRATEGY_CATALOGUE_LIMIT", "500").strip()
        catalogue_limit = None if catalogue_limit_raw.lower() in {"none", "0", "all", "unlimited"} else int(catalogue_limit_raw)

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
        proposal_model = model_used if model_used != "deterministic" else model
        metadata_secrets = {
            "gemini": {
                "api_key": gemini_key,
                "model": model,
                "fallback_models": fallback_models,
            },
            "groq": {
                "api_key": os.getenv("GROQ_API_KEY", "").strip(),
                "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            },
        }

        if candidates:
            daily_count = _daily_auto_count(store, owner_key)
            cadence_count = _cadence_auto_count(store, owner_key, cadence)
            try:
                rows = _prepare_metadata_proposals(youtube, metadata_secrets, candidates)
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
                actual_provider = row.get("ai_provider")
                actual_model = row.get("ai_model")
                metadata_model = (
                    f"{actual_provider}:{actual_model}"
                    if actual_provider and actual_model
                    else proposal_model
                )
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
