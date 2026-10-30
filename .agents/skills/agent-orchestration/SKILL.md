---
name: agent-orchestration
description: >-
  Orchestrate deterministic analytics gathering, research radars, and LLM reasoning
  into an automated daily briefing and proactive creator recommendations without an always-running swarm.
  Use when coordinating agentic workflows, briefing generators, or task execution.
---

# Agent Orchestration

Guidelines for managing deterministic analysis and LLM-augmented strategy generation.

## Orchestration Lifecycle

1. **Deterministic Gathering**:
   - Query YouTube API and Reporting data directly without LLM hallucination risk.
   - Fetch verified uploads, view velocity, format-matched peers, and reach metrics.
   - Run AI research scanners and comment scrapers through deterministic scrapers/APIs.

2. **Conservative Candidate Ranking**:
   - Rank optimization candidates based on concrete thresholds (e.g. CTR lower than 3-peer median with high retention).
   - Require format (Shorts vs Long-form) and age matching before diagnosing performance.

3. **LLM Evidence Interpretation**:
   - Feed deterministic facts and evidence into LLM prompts.
   - Require structured JSON output containing:
     - `finding`: Concise observation grounded in data.
     - `evidence`: Specific metric comparison (e.g. "CTR 2.1% vs peer median 4.8%").
     - `confidence`: High, Medium, or Low based on peer count.
     - `action`: Specific title, packaging, or script tweak.
     - `caveat`: Explanations of missing data (e.g. reach report pending).

4. **Approval Gate Enforcement**:
   - The agent proposes; the owner disposes.
   - Stage exact proposals with cryptographic hashes (`compute_proposal_digest`).
   - Invalidate approval immediately if metadata or proposal changes.

## Acceptance Criteria

- No infinite polling or unconstrained background agent loops.
- Re-run idempotency enforced via daily scan IDs and local locks.
- Every action item links to exact evidence and source metrics.
