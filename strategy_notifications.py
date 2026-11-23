"""Send Discord alerts for the latest scheduled channel-strategy run."""

from __future__ import annotations

import logging
import os

import requests

from discord_notifications import send_discord_message

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("strategy-notifications")
DEFAULT_APP_URL = "https://youtube-strategist-gvysp46objgfmgmdemckwe.streamlit.app/"


def build_notifications(run: dict | None, actions: list[dict], workflow_conclusion: str, app_url: str = DEFAULT_APP_URL) -> list[dict]:
    """Create concise actionable alerts; quiet successful no-op runs stay silent."""
    messages = []
    pending = [item for item in actions if item.get("status") == "pending_review"]
    published = [item for item in actions if item.get("status") == "auto_published"]
    shadow = [item for item in actions if item.get("status") == "shadow_published"]
    failed = [item for item in actions if item.get("status") in {"failed", "verification_unavailable"}]

    for item in pending:
        title = str(item.get("video_title") or "Untitled video")[:110]
        video_id = str(item.get("video_id") or "")
        confidence = item.get("confidence")
        confidence_text = f"{float(confidence):.0%}" if isinstance(confidence, (int, float)) else "Unavailable"
        body = (
            f"Confidence: {confidence_text}\n"
            f"Reason: {str(item.get('reason') or 'Owner review requested.')[:700]}\n"
            + (f"Video: https://www.youtube.com/watch?v={video_id}\n" if video_id else "")
            + "Open the YouTube Strategist to review the exact proposal."
        )
        messages.append({
            "title": f"Review required: {title}",
            "message": body,
            "priority": "4",
            "tags": "warning",
            "click": app_url,
        })

    if published:
        lines = [f"Successfully published {len(published)} metadata update(s)."]
        for item in published[:5]:
            lines.append(f"• {str(item.get('video_title') or item.get('video_id') or 'Video')[:100]}")
        messages.append({
            "title": "YouTube metadata auto-published",
            "message": "\n".join(lines),
            "priority": "3",
            "tags": "white_check_mark",
            "click": app_url,
        })

    if shadow:
        lines = [f"Shadow mode generated {len(shadow)} proposal(s). Nothing was published to YouTube."]
        for item in shadow[:5]:
            lines.append(f"• {str(item.get('video_title') or item.get('video_id') or 'Video')[:100]}")
        messages.append({
            "title": "Metadata proposals ready (shadow mode)",
            "message": "\n".join(lines),
            "priority": "3",
            "tags": "eyes",
            "click": app_url,
        })

    if failed:
        lines = []
        for item in failed[:5]:
            label = str(item.get("video_title") or item.get("video_id") or "Video")[:90]
            # Keep raw API exceptions out of notifications; point the owner to authenticated logs.
            state = "verification unavailable" if item.get("status") == "verification_unavailable" else "publish failed"
            lines.append(f"{label}: {state}. Open the dashboard and GitHub Actions logs for details.")
        messages.append({
            "title": f"Strategist needs attention: {len(failed)} issue(s)",
            "message": "\n".join(lines),
            "priority": "5",
            "tags": "rotating_light",
            "click": app_url,
        })

    run_status = str((run or {}).get("status") or "").lower()
    conclusion = str(workflow_conclusion or "").lower()
    if run_status == "failed" or conclusion in {"failure", "cancelled", "timed_out"}:
        if not failed:
            messages.append({
                "title": "YouTube Strategist worker failed",
                "message": f"GitHub Actions concluded: {conclusion or 'failed'}. Open the workflow logs for the private error details.",
                "priority": "5",
                "tags": "rotating_light",
                "click": "https://github.com/aisimplified280-png/youtube-strategist/actions",
            })
    elif run_status == "partial" and not failed:
        messages.append({
            "title": "YouTube Strategist completed partially",
            "message": str((run or {}).get("error") or "The run completed with partial results. Check the dashboard and workflow logs."),
            "priority": "4",
            "tags": "warning",
            "click": app_url,
        })
    return messages


def _supabase_get(base_url: str, service_key: str, table: str, params: dict) -> list[dict]:
    response = requests.get(
        f"{base_url.rstrip('/')}/rest/v1/{table}",
        params=params,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"Supabase returned an unexpected payload for {table}.")
    return payload



def main() -> int:
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    discord_test_mode = os.getenv("DISCORD_TEST_NOTIFICATION", "").strip() == "1"
    if not discord_webhook:
        if discord_test_mode:
            log.error("DISCORD_WEBHOOK_URL is required for the explicit Discord test.")
            return 1
        log.info("DISCORD_WEBHOOK_URL is not configured; skipping Discord notifications.")
        return 0

    if discord_test_mode:
        try:
            send_discord_message({
                "title": "AI Simplified Lab Discord test",
                "message": "Success! Your Discord notification channel is connected. Strategy review alerts, publish summaries, and worker failures can now reach you.",
                "priority": "3",
                "tags": "white_check_mark",
                "click": os.getenv("STRATEGIST_APP_URL", DEFAULT_APP_URL),
            }, webhook_url=discord_webhook)
            log.info("Sent the requested Discord test notification.")
        except Exception:
            log.exception("Could not send the requested Discord test notification.")
            return 1
        return 0

    base_url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    started_at = os.getenv("STRATEGY_RUN_STARTED_AT", "").strip()
    conclusion = os.getenv("STRATEGY_WORKFLOW_CONCLUSION", "unknown").strip()
    app_url = os.getenv("STRATEGIST_APP_URL", DEFAULT_APP_URL).strip()
    if not base_url or not service_key:
        log.error("Supabase credentials unavailable; cannot resolve the latest strategy run.")
        if conclusion in {"failure", "cancelled", "timed_out"}:
            try:
                send_discord_message({
                    "title": "YouTube Strategist worker failed",
                    "message": "The workflow failed before run details could be loaded. Check GitHub Actions.",
                    "priority": "5",
                    "tags": "rotating_light",
                    "click": "https://github.com/aisimplified280-png/youtube-strategist/actions",
                }, webhook_url=discord_webhook)
            except Exception:
                log.exception("Could not send Discord failure alert.")
        return 0

    try:
        run_params = {
            "select": "id,status,cadence,started_at,finished_at,result,error,trigger_source",
            "order": "started_at.desc",
            "limit": "1",
        }
        if started_at:
            run_params["started_at"] = f"gte.{started_at}"
        else:
            log.warning("Workflow start marker missing; skipping run lookup to avoid alerting on stale activity.")
        run_rows = (
            _supabase_get(base_url, service_key, "channel_strategy_runs", run_params)
            if started_at else []
        )
        run = run_rows[0] if run_rows else None
        actions = []
        if run and run.get("id"):
            actions = _supabase_get(base_url, service_key, "channel_strategy_actions", {
                "select": "id,video_id,video_title,status,reason,error,confidence,result,created_at",
                "run_id": f"eq.{run['id']}",
                "order": "created_at.asc",
                "limit": "30",
            })
        messages = build_notifications(run, actions, conclusion, app_url)
        for message in messages:
            send_discord_message(message, webhook_url=discord_webhook)
        log.info("Sent %s Discord notification(s) for strategy run %s.", len(messages), (run or {}).get("id") or "unresolved")
    except Exception:
        # Notification failures must never change the worker's publishing outcome.
        log.exception("Discord notification delivery failed; the strategy worker result is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
