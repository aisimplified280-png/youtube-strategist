# Creator Studio UX flow and product plan

**Audit date:** 18 October 2026  
**Scope:** owner sign-in, YouTube authorization, report imports, evidence-ranked actions, inline metadata proposal and approval, channel analytics, and optional private memory.

## Product principle

The creator needs four answers in order: **What should I improve first? Why? What exact change would you make? Do I approve it?** The default screen should lead with one high-priority action, not a list of modes, raw IDs, or a wall of metrics. The app may prepare edits after the creator chooses **Do it**, but must never write a YouTube change without approval of the exact displayed proposal.

## Information architecture

### Primary surface: Creator Studio

One scrollable workflow, with no top-level mode switching.

1. **Top of page:** a compact heading, connection status, and one next-best action.
2. **Action card:** human-readable video title, action category, priority, evidence and sample size, measured values, one practical next step, and **Do it / Skip**.
3. **Inline proposal:** once **Do it** is chosen, load the selected video in place. Show its title, current metadata, optional transcript/reference context, and one generated proposal. Keep description + hashtags distinct from the separate YouTube tags field. Do not display internal video IDs as labels.
4. **Approval:** editable exact title, description and tags; optional current-vs-proposed comparison; explicit **Approve and apply**. After verification, show a persistent success state and a **Continue to next recommendation** action. Cancel before approval restores the active item to the queue.
5. **Import details:** when no export is present, keep the upload panel open. After analysis, collapse it under **Import or refresh YouTube Studio reports** so the measured action is easier to find.
6. **Channel overview:** basic metrics remain available below the prioritized workflow; detailed coverage explanations, wide tables, retention details, and audit details are secondary.
7. **Secondary controls:** YouTube connection, optional private memory and account sign-out stay in the sidebar. Free-form optimization is available through **Optimize another video**, not a separate page.

### Recommendation rules

Recommendations are sorted by evidence priority and then measured reach within a priority tier. They are review candidates, not growth predictions. CTR is compared only when measured impressions are present; a 100-impression minimum is this app's sample safeguard, not an official YouTube threshold. Relative audience retention is an index, not an audience-retained percentage. Missing columns/rows remain unavailable and are never coerced to zero. Overlapping source files are not silently summed.

## User scenarios

### 1. First visit / authorization

The owner signs in with Google and grants YouTube access separately. A verified owner check gates channel data and all write actions. Connections and Account stay in the sidebar. An unconnected YouTube account sees a contextual connection path and does not see private channel information.

### 2. Returning creator with previously imported reports

Creator Studio immediately shows the next actionable item, not a list of every recommendation. The importer remains collapsed until the creator opens **Import or refresh YouTube Studio reports**. The title should come from the report or, when missing, from a safe YouTube Data API title lookup. If no title can be retrieved, display **Video title unavailable** rather than a raw system ID.

### 3. Creator chooses Do it

The app retains the current page and selected action. It loads current YouTube metadata and any transcript available for that video, adds only compact numeric evidence from the selected export to the AI prompt, and proposes a complete title, description and separate tags. It does not treat performance metrics as evidence of video content or proof of cause.

### 4. Creator revises or approves

Every displayed field is the exact proposal submitted for approval. Edits/revisions invalidate the earlier approval state. On approval, the connector refetches the current snippet, rejects stale proposals, updates only title/description/tags, and verifies the result. Success must persist across Streamlit reruns. After success the creator can move to the next recommendation without leaving the current screen. Cancel before approval must not lose the queued recommendation.

### 5. Creator skips

Skip removes that suggestion from the current session queue and immediately promotes the next ranked item. It does not mutate YouTube metadata. The creator can reset the queue for a re-review.

### 6. Creator starts from a specific video

**Optimize another video** opens a conversational input in the same surface. The owner can send a video URL/ID and an instruction in one message. The current title/description/tags are rendered as human-readable text; identifiers are used only for API requests and links.

### 7. Creator has richer Studio data in Excel

CSV/TSV/TXT and `.xlsx` can be uploaded and analysed in memory (10 MB per file). Currently, files saved to OneDrive are not automatically accessible to the app. Direct Microsoft access requires an explicit, separate Microsoft Graph/OAuth integration and app registration. Do not show a fake connected state or imply that Google sign-in also grants Microsoft permissions. Until that integration exists, download the workbook and upload it manually.

## Privacy and trust

- Never show a success message until YouTube confirms the write and the result is verified.
- Never write metadata automatically in response to a recommendation or AI generation.
- Do not display raw video IDs or proposal digests as creator-facing labels.
- Keep raw uploaded reports in memory; do not save them to Drive/disk.
- Only send compact, selected-video metrics to the configured AI provider when the creator chooses **Do it**; do not send the source workbook.
- Do not infer causation, promise CTR/views, or imply that an AI response visually inspected the video.
- The optional Google Sheets memory grant is separate from YouTube scopes and never a prerequisite for analysis or approval.

## Acceptance checklist

- [ ] The top of Creator Studio contains no **WORKSPACES** label or three-mode navigation row.
- [ ] Only one ranked export recommendation is presented as the immediate next action.
- [ ] The priority card displays a human-readable title instead of a raw video ID where a title can be retrieved.
- [ ] Import controls collapse after a successful analysis; opening them remains possible without clearing data.
- [ ] Choosing **Do it** keeps the creator in the same view and reveals title/description/tags for review.
- [ ] Choosing **Skip** advances the queue and performs no YouTube write.
- [ ] Cancel before approval returns the unapproved action to the queue.
- [ ] Approval applies only the exact displayed proposal, validates current metadata and verifies the update.
- [ ] The successful update message survives Streamlit reruns and offers a clear next-action CTA.
- [ ] Human-facing tables/audit surfaces do not expose video IDs or proposal IDs as primary labels.
- [ ] Direct OneDrive import is clearly described as unavailable until Microsoft Graph/OAuth configuration exists.
- [ ] CSV/TSV/TXT and `.xlsx` imports continue to be covered by unit tests.
- [ ] Owner authentication, separate OAuth purposes, and optional memory safeguards remain unchanged.
