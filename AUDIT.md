# Comprehensive Audit: YouTube Strategist

**Audit Date:** 2026-10-23  
**Status:** Completed Phase 0 Audit; Proceeding to Phase 1–5 Implementation  
**Target:** Private, owner-only AI co-pilot for YouTube creators deployed on Streamlit Community Cloud

---

## 1. Executive Summary & Prior Art Assessment

A review of `UX_AUDIT_AND_PRODUCT_PLAN.md` (dated 18 October 2026) revealed solid foundational principles (strict owner check, separate OAuth grants for YouTube vs Drive memory, exact-proposal approval, and zero metric hallucination). However, the existing implementation suffers from structural and usability debts:
- **Monolithic architecture:** `streamlit_app.py` alone is ~3,000 lines; `strategist_chat.py` (~1,230 lines), `studio_export_analyzer.py` (~1,220 lines), and `youtube_analytics_connector.py` (~1,400 lines) contain significant overlapping presentation and data-parsing logic.
- **Cognitive overload on landing:** When a user lands on the app, they are presented with dense options, multiple expanders, and disconnected workspaces rather than a clear 5-second answer to *"What should I do today?"*.
- **Navigation limitations:** The app relies on ad-hoc buttons and radio selectors nested inside expanders, resulting in sub-optimal URL routing and state loss on full-page reruns. Streamlit 1.36+ `st.navigation` and `st.Page` provide native, clean, bookmarkable multipage routing.
- **Performance & rerun traps:** Lack of `st.fragment` for chat and proposal interactions triggers full-page recalculations.
- **Model identifiers & config bugs:** The default Gemini model was specified as `gemini-3.8-flash`, an invalid Google AI Studio model ID.
- **Ephemeral token vault & OAuth testing expiry:** The encrypted SQLite vault in `/tmp` is wiped whenever Streamlit Community Cloud restarts a container, and Google OAuth Testing mode expires refresh tokens every 7 days. These must surface as friendly "Reconnect needed" states, not confusing crashes.

---

## 2. Detailed Findings

### A. UX Friction & Information Architecture
1. **Clicks to Value:** Reaching an actionable optimization proposal currently requires 6–8 distinct clicks (connecting, selecting workspace, entering a date range, running scan, reviewing table, clicking candidate, waiting for chat load). The product goal requires reaching a proposal in **3 clicks or fewer** from landing or upload.
2. **5-Second Landing Clarity:** A first-time or returning creator has no hero card or primary call-to-action on the Home screen. They see empty cards or complex date filters.
3. **Studio Export Integration:** Studio file uploads were buried under an expander in Analytics rather than being front-and-center in a dedicated "Analyze / Find Videos" workflow.
4. **Strategist Chat vs Proposal Co-Pilot:** The proposal was rendered below or interspersed with chat history, making comparison cumbersome. A split side-by-side layout (chat left, live interactive proposal right, collapsing cleanly on mobile) is required.
5. **Mobile Viewport (360px):** Multi-column metric rows and wide tables overflow horizontally on 360px screens.

### B. Architecture & Code Organization (Monoliths to Modularize)
1. **`streamlit_app.py` (~3,000 lines):** Combines app bootstrapping, custom CSS injection, secrets validation, OAuth callback handling, token storage hydration, date filter logic, table rendering, candidate cards, and chat glue.
   - *Target:* Refactor into `app.py` (root shell with auth gate and navigation), `pages/` (home, studio_analysis, strategist, channel_dashboard, memory_experiments, settings), `core/` (auth, oauth, vault, youtube_api, reporting, metadata_apply, llm, memory, analytics_logic), and `ui/` (theme, components).
   - Keep a backward-compatible shim in `streamlit_app.py` importing the new modular app shell.
2. **Duplicated & Overlapping Logic:**
   - Video title resolution: Re-implemented across `streamlit_app.py`, `youtube_dashboard.py`, and `strategist_chat.py`.
   - Metrics formatting and "Unavailable" messaging: Duplicated across dashboard and export analyzer.
   - Proposal diff calculation: Diff logic repeated between `youtube_analytics_connector.py` and `strategist_chat.py`.
3. **Prompts Dispersal:** LLM prompts are embedded inside large string literals in `strategist_chat.py` without schema enforcement or structured output validation.
   - *Target:* Move to versioned `core/prompts.py` with Pydantic JSON schema output and automated linting.

### C. Specific Verifications & Fixes
1. **Gemini Model Identifier:**
   - *Issue:* `gemini-3.8-flash` in `.streamlit/secrets.toml.example`, `README.md`, and `strategist_chat.py`.
   - *Fix:* Set safe default to `gemini-2.5-flash` with fallbacks `gemini-2.0-flash` and `gemini-1.5-flash`. Make fully configurable via secrets.
2. **Ephemeral SQLite Vault in Temp Directory:**
   - *Issue:* `_DEFAULT_PATH = Path(tempfile.gettempdir()) / "ai-simplified-lab-strategist-oauth.sqlite3"`. On Streamlit Community Cloud, container reboots clear `/tmp`.
   - *Fix:* Explicitly handle missing or unreadable vault states. When tokens are missing or invalid, render a friendly "Connection expired or reset — 1-click Reconnect" status pill and card instead of failing with internal error messages.
3. **7-Day OAuth Refresh Expiry in Google Testing Mode:**
   - *Issue:* When `creds.refresh()` fails with `google.auth.exceptions.RefreshError`, the app can raise or display unhandled auth exceptions.
   - *Fix:* Catch `RefreshError` specifically, treat as `"needs_reconnect"`, remove stale credentials from session and vault, and present a clear "Authorization expired (Google 7-day test limit) — Click to reconnect" card.
4. **README Audit Documentation Inconsistency:**
   - *Issue:* `README.md` lines 151-155 claimed audit records are saved to `~/.ai-simplified-youtube/metadata_audit.jsonl`, while lines 253-256 and 310 stated audit records are session-only.
   - *Reality:* The CLI tool (`youtube_analytics_connector.py` standalone) wrote to `~/.ai-simplified-youtube/metadata_audit.jsonl`. The hosted Streamlit app maintains session-scoped audit records and optionally appends verified experiments to Google Sheets "Strategist Memory".
   - *Fix:* Reconcile documentation to clearly distinguish local CLI agent behavior from hosted Streamlit cloud dashboard behavior.

### D. Security & Owner Verification Review
1. **Owner-Only Gate:** Strict verification of `st.user.email` + `st.user.email_verified` against `secrets.access.owner_email` via constant-time `hmac.compare_digest` occurs before any API client is instantiated. This must remain non-negotiable.
2. **OAuth Isolation:** Two distinct flows (YouTube Analytics + `youtube.force-ssl` vs Google Sheets `drive.file`) remain strictly decoupled. Scopes are never merged.
3. **OAuth State:** Signed HMAC with 10-minute TTL, nonce, and subject hash. State is single-use and purpose-specific.
4. **No Secrets/Tokens in Logs:** Token encryption via Fernet using key derived from `state_secret`. No tokens printed or exposed in query params or UI.

---

## 3. Implementation Plan (Phases 1 to 5)

| Phase | Milestone | Key Deliverables |
| :--- | :--- | :--- |
| **Phase 1** | Information Architecture & App Shell | Convert to `st.navigation` / `st.Page`. Create modular structure: `app.py`, `pages/`, `core/`, `ui/`. Preserve `streamlit_app.py` shim. |
| **Phase 2** | UI/UX & Visual Design | Dark-first theme in `.streamlit/config.toml`, custom tokens in `ui/theme.py`, components in `ui/components.py` (hero card, metric tile, evidence chip, diff view, mobile 360px responsiveness). |
| **Phase 3** | AI Quality & Prompts | Modular prompts in `core/prompts.py`, structured proposal output, programmatic linter (title length, duplicates, tag limits), integrate existing skills (`title.py`, `hookscore.py`). |
| **Phase 4** | Engineering & Testing | Type hints, comprehensive tests for parser, ranking, linter, router, and OAuth states. Add Demo Mode for evaluation without live credentials. |
| **Phase 5** | Documentation & Verification | Update `README.md`, `.streamlit/secrets.toml.example`, `CHANGELOG.md`, `ROADMAP.md`. Verify all tests pass. |
