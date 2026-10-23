# Changelog

All notable changes to the **YouTube Strategist** project are documented here.

## [2.0.0] - 2026-10-23

### Added
- **Multipage Navigation Architecture:** Split monolithic ~3,000-line `streamlit_app.py` into a modern multipage app (`app.py`) using `st.navigation` and `st.Page` across 6 focused workspaces:
  - `pages/home.py`: "What should I do today?" hero action card, 3-step status strip, channel pulse tiles with deltas, recent experiments.
  - `pages/studio_analysis.py`: Drag-and-drop Studio CSV/TSV/XLSX uploader, detected reports summary, and sequential candidate cards with Skip/Undo.
  - `pages/strategist.py`: Conversational packaging co-pilot with side-by-side proposal diffs, inline linter warnings, suggested prompt chips, and approval gate.
  - `pages/channel_dashboard.py`: Date presets (7d, 28d, 90d), period-over-period comparison, sortable/filterable video performance table, and evidence drawer.
  - `pages/memory_experiments.py`: Verified before/after experiments timeline and weekly scan snapshots.
  - `pages/settings.py`: Centralized connection health cards (Google sign-in, YouTube, Sheets memory, AI providers, diagnostics, and Demo Mode toggle).
- **Interactive Packaging Linter (`core/linter.py`):**
  - Character counter & truncation checker (40 chars mobile cutoff, 60 chars desktop search cutoff, 100 chars hard limit).
  - Hashtag count check (optimal 1-3, warns if >5 or >15) and total description limit (5000 chars).
  - Tag character budget tracker (sums comma-separated tags against YouTube's 500-char limit).
  - Banned misleading claims detector (sensationalist guarantees like "guaranteed CTR", "viral trick").
  - Integrated stdlib tools from `skills/yt-package/title.py` and `skills/yt-script/hookscore.py`.
- **AI Router & Schema Enforcer (`core/llm.py` & `core/prompts.py`):**
  - Groq primary (`openai/gpt-oss-120b`) with Google Gemini fallback (`gemini-2.5-flash`).
  - Strict JSON proposal schema enforcement with 1-shot automatic repair prompt on malformed outputs.
  - Transparent UI badge indicating provider and exact model answering each query.
- **Demo Mode (`core/demo_data.py`):**
  - Full-featured evaluation mode with realistic channel telemetry, Studio export datasets, and candidate video proposals.
  - Persistent banner when active; guarantees zero external API calls without credentials.
- **Custom Visual Theme (`ui/theme.py` & `.streamlit/config.toml`):**
  - Dark-first aesthetic with typography scale (Plus Jakarta Sans & JetBrains Mono), cards, badges, chips, and responsive CSS (tested down to 360px mobile width).
- **Core Test Suite (`tests/test_core_features.py`):**
  - 11 new unit tests covering linter, LLM router fallback, prompt construction, auth gate, period deltas, and vault resilience.

### Changed
- **Gemini Model Configuration:** Updated default Gemini model from invalid `gemini-3.8-flash` to current `gemini-2.5-flash` with configurable fallbacks (`gemini-2.0-flash`, `gemini-1.5-flash`).
- **Resilient Token Vault:** Ephemeral SQLite vault in host temp directory now surfaces a clean "Reconnect needed" status pill and card instead of internal errors when restarted.
- **7-Day Google Testing Expiry Handling:** Gracefully catches `RefreshError` and transitions state to 1-click reconnect without breaking session state.
- **README Documentation Reconciliation:** Disentangled standalone CLI agent logging (`metadata_audit.jsonl`) from hosted Streamlit Community Cloud session audit state and Sheets memory.
- **Entry Point Shim:** Maintained backward-compatible `streamlit_app.py` forwarding to `app.py` for seamless Community Cloud deployments.
