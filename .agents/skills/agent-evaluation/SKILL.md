---
name: agent-evaluation
description: >-
  Evaluate agent recommendations, ranking precision, fail-closed safety, and regression defense
  across unit, integration, and security test suites.
  Use when writing test fixtures, evaluating LLM outputs, or validating approval gates.
---

# Agent Evaluation

Standards for testing and evaluating the Creator Growth Agent.

## Evaluation Dimensions

1. **Safety and Fail-Closed Verification**:
   - Verify that unparseable or malformed LLM outputs fail closed (raise `LLMError`) instead of inserting placeholder/dummy text.
   - Verify that modifying a title, description, or tag invalidates any pending approval hash.

2. **Deterministic Candidate Ranking**:
   - Test ranking logic with known synthetic candidate rows (e.g. video with 1.8% CTR vs 5.2% peer median).
   - Verify that videos with fewer than 3 matched peers are categorized as low-confidence.

3. **Memory & State Invariants**:
   - Verify that accepted/rejected proposals persist across session teardown and reload.
   - Test that rejecting a proposal prevents the agent from suggesting the identical change in the next briefing.

4. **Security & Input Sanitization**:
   - Test HTML injection vectors in video titles, descriptions, and comments rendered in the UI.
   - Verify that Gemini API keys are passed via headers (`x-goog-api-key`) and not logged in request URLs.

## Acceptance Criteria

- 100% pass rate on regression test suite.
- Comprehensive test coverage for all proposal lifecycle states (Generated -> Staged -> Approved/Declined -> Verified).
