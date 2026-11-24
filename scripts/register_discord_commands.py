"""Register the AI Simplified Lab /approvals Discord slash command.

Run only after the Discord application and bot secrets are configured:
  DISCORD_APPLICATION_ID=... DISCORD_BOT_TOKEN=... python scripts/register_discord_commands.py
"""
from __future__ import annotations

import os
import requests


def main() -> None:
    application_id = os.environ["DISCORD_APPLICATION_ID"].strip()
    bot_token = os.environ["DISCORD_BOT_TOKEN"].strip()
    if not application_id.isdigit() or not bot_token:
        raise SystemExit("DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN are required.")

    url = f"https://discord.com/api/v10/applications/{application_id}/commands"
    response = requests.put(
        url,
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
        json=[{
            "name": "approvals",
            "description": "Review pending AI Simplified Lab metadata proposals",
            "type": 1,
        }],
        timeout=20,
    )
    if not response.ok:
        raise SystemExit(f"Discord command registration failed ({response.status}): {response.text[:500]}")
    commands = response.json()
    print("Registered:", ", ".join(f"/{item['name']}" for item in commands))


if __name__ == "__main__":
    main()
