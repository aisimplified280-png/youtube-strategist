---
name: repo-architecture-audit
description: >-
  Audit the local YouTube strategist codebase, mapping dependencies, unused modules,
  OAuth and LLM integrations, state persistence, UI routes, and missing tests.
  Use when conducting an architectural review, diagnosing dead code, or refactoring flows.
---

# Repo Architecture Audit

Procedures for auditing and maintaining the structural integrity of the YouTube Strategist codebase.

## Audit Workflow

1. **Map Entry Points and Page Routing**:
   - Inspect `app.py` and all registered pages in `pages/`.
   - Confirm explicit `url_path` and function bindings for all `st.Page` definitions.
   - Detect legacy or orphaned page implementations (e.g., `strategist_chat.py` vs `pages/strategist.py`).

2. **Map API Clients and Scopes**:
   - Verify YouTube Data API v3 and Analytics API clients in `youtube_analytics_connector.py`.
   - Ensure narrowest required scopes:
     - YouTube: `https://www.googleapis.com/auth/yt-analytics.readonly`, `https://www.googleapis.com/auth/youtube.force-ssl`
     - Memory: `https://www.googleapis.com/auth/drive.file` isolated from YouTube tokens.

3. **Verify State and Token Persistence**:
   - Audit `core/vault.py` and `oauth_token_store.py` for PBKDF2/AES-GCM encryption of persisted tokens.
   - Verify `content_memory.py` SQLite and optional Google Sheets backup storage.

4. **Verify LLM and Prompt Routing**:
   - Verify `core/llm.py` and `core/prompts.py` for Gemini and OpenRouter fallback configuration.
   - Ensure keys use official headers (`x-goog-api-key`) and fail-closed structured parsing.

## Acceptance Criteria

- No unencrypted tokens stored on disk.
- Zero duplicate or conflicting Streamlit state keys.
- All dead code or unused legacy modules cataloged with deprecation plans.
