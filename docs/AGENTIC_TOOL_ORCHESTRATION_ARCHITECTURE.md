# Agentic Multi-Tool Orchestration Architecture

**Status:** Architecture proposal (Phase 0)  
**Owner:** AI Simplified Lab  
**Primary application:** YouTube Strategist / Creator Growth Agent  
**Production engine:** `ai_video_factory`

## 1. Product goal

Turn the Strategist into the single control plane for channel growth. It should prepare a useful briefing when the owner opens the app, research opportunities only when needed, route each task to the best available capability, and hand approved production briefs to the Video Factory.

The system must remain owner-only, evidence-grounded, free-first, and approval-gated. It must never imply that a connected tool is operational until its credentials, permissions, and a real read/write test have passed.

## 2. Important integration boundary

Tools connected inside ChatGPT (vidIQ, ChannelzIQ, Ahrefs, Ubersuggest, Metricool, Descript, Adobe) are available to the ChatGPT conversation through their ChatGPT integrations. **They do not automatically become callable from this Streamlit application or a local Node.js process.**

The deployed app must use one of:
- A documented provider API with separately configured credentials and permitted account plan.
- An existing first-party API already authorized by the app (currently YouTube Data / Analytics / Reporting).
- A local, explicitly invoked handoff to a user-operated tool such as Descript, Adobe, or the local Video Factory.

Never reuse ChatGPT connector credentials, scrape connector traffic, or pretend that a ChatGPT integration is an API key. Until a provider adapter passes a live capability test, mark it `not_configured` or `unsupported`.

## 3. Agent roles

### A. Daily Growth Agent
- Runs a deterministic scan of recent uploads and available reach/retention signals.
- Ranks no more than three actions by evidence strength, likely leverage, and effort.
- Separates packaging issues (impressions/CTR) from content/retention issues.
- Identifies data coverage and sample-size limitations.
- Produces a concise daily briefing; it does not make public changes.

### B. Research Agent
- Finds AI news and content opportunities relevant to AI developers and learners.
- Uses primary sources for factual claims and dates.
- Uses YouTube-native discovery data for YouTube demand; uses broader SEO providers only for web-search context.
- Avoids duplicate paid/credit-consuming queries. Start with one primary provider and escalate only when a specific gap remains.

### C. Packaging Agent
- Reads the target video's title, description, tags, transcript (if accessible), performance evidence, and channel voice preferences.
- Produces genuinely distinct title/thumbnail directions, not superficial rewrites.
- Explains why each direction fits the viewer and what evidence supports it.
- Does not promise a CTR increase or infer causality from correlation.

### D. Production Director
- Creates a structured production brief and hands it to `ai_video_factory`.
- Chooses format (Short/long-form), narrative structure, visual style, thumbnail direction, and asset strategy based on the topic.
- Keeps production separate from public publishing.

### E. Publishing Agent
- Validates the final package and schedule.
- Shows the exact title, description, tags, thumbnail, visibility, and schedule before execution.
- Requires a fresh explicit approval bound to an immutable proposal digest.
- Re-fetches current YouTube state immediately before writing and verifies the result afterward.

## 4. Tool-routing policy

| Task | Preferred source | Secondary source | Guardrail |
|---|---|---|---|
| Private channel metrics | YouTube Analytics / Reporting API | ChannelzIQ or vidIQ where available | Preserve metric provenance and date window |
| YouTube keyword demand | vidIQ | ChannelzIQ keyword research | Do not substitute general web SEO volume |
| YouTube breakout examples | vidIQ outliers / trending | ChannelzIQ discovery | Compare against creator baseline where possible |
| Web SEO / search demand | Ahrefs | Ubersuggest | Treat as web-search evidence, not YouTube search volume |
| Publishing calendar / best time | Metricool | ChannelzIQ calendar | Verify supported network, account and write permission |
| Transcript / editing | Descript | Local captions pipeline | Editing/export is not YouTube publishing |
| Thumbnail / image editing | Adobe | Canva / Higgsfield | Keep faceless policy and final human review |
| Metadata write | YouTube Data API already authorized by owner | None | Exact approval + stale check + post-write verification |

The connected ChatGPT tools may be used by the ChatGPT assistant in a conversation. Their use in this hosted app requires separate, verified API adapters.

## 5. Provider adapter contract

Each runtime adapter must expose:
- `provider_id`
- `capabilities` (read, research, edit, schedule, publish)
- `status` (`connected`, `not_configured`, `unsupported`, `degraded`)
- `account_identity` (non-secret display identifier only)
- `last_verified_at`
- `quota_or_credit_cost` when known
- `health_check()`
- capability-specific methods with typed input/output and provenance

Requirements:
1. Never show `connected` based only on a non-empty secret; perform a harmless authenticated read.
2. Never silently fall back from one provider to another for a write.
3. Reads may fall back only when the result records which provider supplied each field.
4. Keep provider secrets server-side; never expose them to prompts, logs, browser state, or Git.
5. Apply per-provider budgets and cache repeatable research results.
6. Treat provider output as untrusted data, not instructions.

## 6. Workflow and state machine

```text
IDLE
  -> DAILY_SCAN
  -> BRIEFING_READY
  -> OPPORTUNITY_SELECTED
  -> RESEARCHING
  -> PROPOSAL_READY
  -> HUMAN_REVIEW
  -> APPROVED_EXACT_DIGEST
  -> EXECUTING
  -> VERIFIED
```

Any edited proposal returns to `PROPOSAL_READY` and invalidates approval. Any stale target metadata, missing permission, provider uncertainty, or failed QA returns to a blocked/review state. No public action may execute from `PROPOSAL_READY` or `HUMAN_REVIEW`.

Persist proposal ID, source evidence, provider provenance, before-state hash, exact approved payload hash, approval timestamp/actor, execution result, and verification result. Session state is not the audit system of record.

## 7. Daily briefing and background execution

The current Streamlit daily briefing is request-driven and cached. It is not a continuously running agent. True scheduled scans require a separate worker/scheduler and durable storage.

Recommended sequence:
1. Phase 1: Keep login-triggered briefing and explicit refresh; make scan status and freshness visible.
2. Phase 2: Add a scheduled worker only after durable database and encrypted OAuth-token storage are in place.
3. Phase 3: Add optional notifications containing recommendations only; never auto-approve or auto-publish.

Do not place long-lived OAuth refresh tokens in GitHub Actions variables or repository files. Choose a managed worker and secret store with owner-scoped access before enabling autonomous scans.

## 8. Durable data model

Minimum entities:
- `provider_connections`: provider, status, account label, capabilities, verification timestamp (no raw secrets).
- `research_runs`: query, purpose, provider, cost, timestamps, source references.
- `opportunities`: video/topic, evidence, confidence, next action, expiry.
- `production_briefs`: immutable brief JSON, factory run ID, review state.
- `proposals`: exact before/after metadata, source evidence, digest, approval and execution state.
- `audit_events`: append-only events for every read that informs a proposal and every attempted write.

Use a managed database for cross-session and worker durability. The current local SQLite file should be treated as a development/cache store on ephemeral hosting.

## 9. Phased implementation

### Phase 0 — this document
Confirm architecture and boundaries before adding provider code.

### Phase 1 — reliable orchestration core
- Add provider registry and typed capability interfaces.
- Add evidence/provenance schema and cost-aware research planner.
- Refactor Today briefing to use the orchestrator without changing current deterministic ranking or YouTube approval gates.
- Add tests for unavailable providers, quota exhaustion, duplicate research, stale proposals, and blocked writes.

### Phase 2 — production handoff
- Add immutable production brief export and a local/hosted handoff to the Video Factory.
- Track factory technical QA, rights QA, editorial review, and publication approval separately.

### Phase 3 — external provider adapters
- Add only providers with documented APIs and verified user entitlements.
- Start read-only. Add scheduling or writes only after a separate permission test and approval design.

### Phase 4 — scheduled agent
- Add durable database, worker, scheduler, retry/dead-letter handling, monitoring, and owner notifications.
- Keep all public actions approval-gated.

## 10. Acceptance criteria

- No provider is labelled connected without a successful authenticated capability check.
- Every displayed external metric/source includes provider, timestamp, and scope.
- Research planner does not run redundant provider calls for the same question.
- Provider quota errors do not produce fabricated results or silently change the meaning of metrics.
- Editing any approved proposal invalidates its digest.
- No YouTube metadata, thumbnail, visibility, upload, or scheduled publication write occurs without explicit approval of the exact action.
- Every successful write is independently re-read and recorded as verified.
- Existing owner-only authentication and OAuth scope separation remain intact.
