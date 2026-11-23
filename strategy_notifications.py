"""Send mobile-first ntfy alerts for the latest scheduled channel-strategy run."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

import requests

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("strategy-notifications")
DEFAULT_APP_URL = "https://youtube-strategist-gvysp46objgfmgmdemckwe.streamlit.app/"


def build_notifications(run: dict | None, actions: list[dict], workflow_conclusion: str, app_url: str = DEFAULT_APP_URL) -> list[dict]:
    """Create concise actionable alerts; quiet successful no-op runs stay silent."""
    messages = []
    pending = [item for item in actions if item.get("status") == "pending_review"]
    published = [item for item in actions if item.get("status") == "auto_published"]
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

    if failed:
        lines = []
        for item in failed[:5]:
            label = str(item.get("video_title") or item.get("video_id") or "Video")[:90]
            # ntfy topics are public-by-name on the hosted service. Keep raw API
            # exceptions out of push payloads; point the owner to authenticated logs.
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


def send_ntfy_message(message: dict, *, topic: str, server: str = "https://ntfy.sh", token: str = "", timeout_seconds: int = 12) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", topic or ""):
        raise ValueError("NTFY_TOPIC must be a private, random 16-128 character topic name.")
    if not str(server).startswith("https://"):
        raise ValueError("NTFY_SERVER must use HTTPS.")
    headers = {
        "Title": str(message.get("title") or "YouTube Strategist")[:180],
        "Priority": str(message.get("priority") or "3"),
        "Tags": str(message.get("tags") or "information_source"),
    }
    if message.get("click"):
        headers["Click"] = str(message["click"])
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.post(
        f"{server.rstrip('/')}/{quote(topic, safe='')}",
        data=str(message.get("message") or "").encode("utf-8"),
        headers=headers,
        timeout=timeout_seconds,
    )
    response.raise_for_status()


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
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        log.info("NTFY_TOPIC is not configured; skipping mobile notifications.")
        return 0
    if os.getenv("NTFY_TEST_NOTIFICATION", "").strip() == "1":
        try:
            send_ntfy_message({
                "title": "AI Simplified Lab ntfy test",
                "message": "Success! Your Android notification channel is connected. Strategy review alerts, publish summaries, and worker failures can now reach this device.",
                "priority": "3",
                "tags": "white_check_mark",
                "click": os.getenv("STRATEGIST_APP_URL", DEFAULT_APP_URL),
            }, topic=topic, server=os.getenv("NTFY_SERVER", "https://ntfy.sh"), token=os.getenv("NTFY_TOKEN", ""))
            log.info("Sent the requested ntfy test notification.")
        except Exception:
            log.exception("Could not send the requested ntfy test notification.")
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
                send_ntfy_message({
                    "title": "YouTube Strategist worker failed",
                    "message": "The workflow failed before run details could be loaded. Check GitHub Actions.",
                    "priority": "5", "tags": "rotating_light",
                    "click": "https://github.com/aisimplified280-png/youtube-strategist/actions",
                }, topic=topic, server=os.getenv("NTFY_SERVER", "https://ntfy.sh"), token=os.getenv("NTFY_TOKEN", ""))
            except Exception:
                log.exception("Could not send ntfy failure alert.")
        return 0

    run = None
    actions = []
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
        if run and run.get("id"):
            actions = _supabase_get(base_url, service_key, "channel_strategy_actions", {
                "select": "id,video_id,video_title,status,reason,error,confidence,result,created_at",
                "run_id": f"eq.{run['id']}",
                "order": "created_at.asc",
                "limit": "30",
            })
        messages = build_notifications(run, actions, conclusion, app_url)
        for message in messages:
            send_ntfy_message(
                message,
                topic=topic,
                server=os.getenv("NTFY_SERVER", "https://ntfy.sh"),
                token=os.getenv("NTFY_TOKEN", ""),
            )
        log.info("Sent %s ntfy notification(s) for strategy run %s.", len(messages), (run or {}).get("id") or "unresolved")
    except Exception:
        # Notification failures must never change the worker's publishing outcome.
        log.exception("Notification delivery failed; the strategy worker result is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
