---
name: creator-memory
description: >-
  Maintain persistent creator preferences, voice profiles, accepted/rejected suggestions,
  production states, and experimental results across sessions.
  Use when reading or writing creator memory, task history, or updating channel voice profiles.
---

# Creator Memory

Architecture and procedures for durable creator context and preference tracking.

## Storage Hierarchy

1. **Local SQLite Primary Store**:
   - Location: User directory or project local storage (`content_memory.sqlite`).
   - Tables:
     - `creator_preferences`: Tone, prohibited phrases, target audience personas, preferred upload cadences.
     - `proposals_history`: Past suggestions, user acceptances/rejections, and feedback rationales.
     - `experiments`: Title/thumbnail A/B tests or metadata refreshes with tracked baselines.
     - `production_pipeline`: Status of ideas (Idea -> Outline -> Script -> Recording -> Published).

2. **Google Drive/Sheets Optional Mirror**:
   - Acts strictly as an optional external backup or creator-facing workbook.
   - The application does not block or fail if Google Sheets is disconnected or unconfigured.

3. **Voice Profile Management**:
   - Reads tone guidelines (e.g. conversational, technical depth, avoiding hype).
   - Informs LLM prompt generation to prevent generic or alien phrasing.

## Acceptance Criteria

- State survives application restarts and machine reboots.
- Rejection reasons are recorded to prevent resurfacing declined ideas.
- Graceful offline fallback when cloud memory sync is unavailable.
