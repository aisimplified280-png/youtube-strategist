"""Discord webhook delivery for AI Simplified Lab strategy alerts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

log = logging.getLogger("discord-notifications")

PRIORITY_COLORS = {
    "1": 0x5865F2,
    "2": 0x5865F2,
    "3": 0x2ECC71,
    "4": 0xF1C40F,
    "5": 0xE74C3C,
}


def build_discord_payload(message: dict) -> dict:
    """Convert a shared strategy alert into a safe Discord embed."""
    title = str(message.get("title") or "YouTube Strategist")[:256]
    description = str(message.get("message") or "Open the YouTube Strategist for details.")[:4000]
    embed = {
        "title": title,
        "description": description,
        "color": PRIORITY_COLORS.get(str(message.get("priority") or "3"), 0x5865F2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "AI Simplified Lab · YouTube Strategist"},
    }
    click_url = str(message.get("click") or "")
    if click_url.startswith("https://"):
        embed["url"] = click_url
    tags = str(message.get("tags") or "").strip()
    if tags:
        embed["fields"] = [{"name": "Status", "value": tags.replace("_", " ")[:100], "inline": True}]
    return {
        "username": "YouTube Strategist",
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }


def send_discord_message(message: dict, *, webhook_url: str, timeout_seconds: int = 12) -> None:
    """Post one notification to a Discord webhook; never log the secret URL."""
    parsed = urlsplit(str(webhook_url or "").strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"discord.com", "discordapp.com"}
        or not parsed.path.startswith("/api/webhooks/")
        or not parsed.path.rstrip("/").split("/")[-1]
    ):
        raise ValueError("DISCORD_WEBHOOK_URL must be a valid HTTPS Discord webhook URL.")
    response = requests.post(
        webhook_url.strip(),
        json=build_discord_payload(message),
        timeout=timeout_seconds,
    )
    response.raise_for_status()


def main() -> int:
    import os

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.error("DISCORD_WEBHOOK_URL is required for the explicit Discord test.")
        return 1
    try:
        send_discord_message(
            {
                "title": "AI Simplified Lab Discord test",
                "message": "Success! Your Discord notification channel is connected. Strategy review alerts, publish summaries, and worker failures can now reach this channel.",
                "priority": "3",
                "tags": "white_check_mark",
                "click": os.getenv(
                    "STRATEGIST_APP_URL",
                    "https://youtube-strategist-gvysp46objgfmgmdemckwe.streamlit.app/",
                ),
            },
            webhook_url=webhook_url,
        )
    except Exception:
        log.exception("Could not send the requested Discord test notification.")
        return 1
    log.info("Sent the requested Discord test notification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
