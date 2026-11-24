"""Register the AI Simplified Lab /approvals Discord slash command.

Normally run through the GitHub Actions workflow:
  Actions -> Register Discord slash commands -> Run workflow

For local maintenance only, the same environment variables can be supplied
to this script directly.
"""
from __future__ import annotations

import os
import sys

import requests


def main() -> None:
    application_id = os.environ.get("DISCORD_APPLICATION_ID", "").strip()
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    missing = [
        name
        for name, value in (
            ("DISCORD_APPLICATION_ID", application_id),
            ("DISCORD_BOT_TOKEN", bot_token),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Add them as GitHub Actions secrets and rerun the workflow."
        )
    if not application_id.isdigit():
        raise SystemExit("DISCORD_APPLICATION_ID must contain only digits.")

    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    response = requests.put(
        url,
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        },
        json=[{
            "name": "approvals",
            "description": "Review pending AI Simplified Lab metadata proposals",
            "type": 1,
        }],
        timeout=20,
    )
    if not response.ok:
        # Discord's response is useful for diagnosis; never print request headers.
        raise SystemExit(
            f"Discord command registration failed ({response.status}): "
            f"{response.text[:500]}"
        )

    commands = response.json()
    print("Successfully registered:", ", ".join(f"/{item['name']}" for item in commands))


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        print(f"Discord API request failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
