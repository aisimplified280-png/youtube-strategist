---
name: streamlit-performance
description: >-
  Optimize Streamlit execution cycles, caching patterns, rerun idempotency, and session state
  to prevent duplicate API scans, UI flickering, or multi-tab collisions.
  Use when designing Streamlit pages, caching expensive operations, or fixing rerun loops.
---

# Streamlit Performance

Guidelines for high-performance Streamlit architectures.

## Engineering Rules

1. **Rerun Idempotency**:
   - Streamlit scripts execute from top to bottom on every user interaction.
   - Long-running scans (like the daily briefing) must be guarded with locks, timestamps, and run IDs.
   - Never trigger external API fetches unconditionally inside page render functions.

2. **Smart Caching**:
   - Use `@st.cache_data(ttl=...)` for static or slowly-changing external data (channel info, video snippets).
   - Use `@st.cache_resource` for API clients and LLM router singletons.
   - Do not cache mutable user approval state.

3. **Session State Integrity**:
   - Centralize state initialization in a dedicated helper function.
   - Isolate active video selection so inspecting Video A never leaks context into Video B.
   - Provide explicit `key` parameters for interactive widgets to prevent unintended reset.

4. **Fast UI Rendering**:
   - Render cached data immediately; perform async or background refreshes when necessary.
   - Keep heavy cryptographic and file operations off the critical render loop.

## Acceptance Criteria

- Zero infinite rerun loops.
- Switching between tabs does not re-fetch channel statistics.
- Instantaneous page transitions for cached briefings.
