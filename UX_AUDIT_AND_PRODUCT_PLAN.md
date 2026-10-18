# YouTube Strategist UX audit and product plan

**Audit date:** 18 October 2026  
**Scope:** the hosted Streamlit app, owner sign-in, YouTube authorization, Strategist, Find videos, Analytics, metadata approval, and optional Google Sheets memory.

## Product rule

The app should help the channel owner decide **which video to improve, what to change, why the suggestion is justified, and whether to apply it**. Keep three primary destinations only. Account and connection management are secondary controls, not a fourth workspace. Never fabricate CTR, claim metadata caused a performance change, or update YouTube without explicit approval of the exact proposal.

## Proposed information architecture

1. **Strategist** — the one-video workspace. Paste the video's URL or 11-character ID, state the goal, optionally add one reference URL, review a single coherent proposal, compare it with current metadata, then approve or revise through chat.
2. **Find videos** — the evidence-backed shortlist. Choose a reporting window, see why each upload surfaced, inspect only available measurements, and open the chosen video directly in Strategist.
3. **Analytics** — the channel overview and searchable performance table. Lead with views, watch time, average view duration, and net subscribers. Keep secondary metrics, coverage detail, and traffic-source detail available but collapsed until requested.

The primary navigation is a row of three normal Streamlit buttons with additional top spacing below Streamlit's own sticky header. The segmented control was removed after it appeared underneath the host navigation. The sidebar is for **Connections** and **Account**. New chat appears only inside the Strategist workflow. Private Google Sheets memory remains optional and collapsed by default.

## Flow audit

### 1. Signed-out visitor

**Expected:** explain what the app does, show the three safe capability summaries, and offer Google sign-in. Do not load channel data, show a video list, or expose metadata controls before the configured owner is authenticated.

**Change in this branch:** replace the bare sign-in prompt with a small product landing view. It previews the three workspaces without leaking private data.

### 2. Google identity sign-in

**Expected:** Google OpenID Connect identifies the user, then the app validates the verified owner email and stable subject. A non-owner sees an access-denied message and a sign-out action. No YouTube API calls or private channel content should be available to an unapproved account.

**Important distinction:** identity sign-in is not the same as granting YouTube Analytics/metadata permissions. Do not merge these permissions or request the optional Drive grant alongside the YouTube scopes.

### 3. Owner signed in, YouTube API access not yet connected

**Previous UX issue:** the app returned before rendering navigation and left only the connection call to action. That made it feel as if login had blocked the app.

**Change in this branch:** render navigation and the selected workspace even without a YouTube API grant. Show one contextual connection panel for the selected workspace, explain that channel data remains locked until authorization succeeds, and provide a refresh action after Google consent. The OAuth link opens a new tab, so the original workspace can stay open.

### 4. YouTube authorization in a separate tab

1. Start the YouTube scope grant from the selected workspace.
2. Google returns to the app callback.
3. The callback validates signed purpose-specific state and owner identity, exchanges the code, and saves the grant to the encrypted shared vault.
4. The callback tab can show the connected dashboard; the original tab can refresh its connection status and restore the grant.

**Failure states:** invalid/expired/mismatched state, insufficient granted scopes, refresh errors, and API-client initialization failures must show a safe retry/reconnect path. Never expose tokens in UI messages or logs.

### 5. Optional private Strategist Memory

**Expected:** memory must never be a prerequisite for chat, channel analysis, or metadata approval. Connect it only when the owner wants scan history and approved experiment records persisted. Authorization uses the separate limited `drive.file` grant. The UI must say “connected” only after the app verifies the workbook.

**Change in this branch:** move memory setup, retry, disconnect, and spreadsheet links into the collapsed Connections section. Remove the duplicated global memory setup error so the optional integration does not dominate every workspace.

**Remaining limitation:** memory retains scan snapshots and approved experiments, not durable chat history. Session-only conversation and audit records can disappear when the Streamlit session expires.

### 6. Strategist: one video to an approved change

**Expected flow:** paste a target URL or ID → load current metadata and any accessible transcript → optionally include a public reference URL → ask for title, description/hashtags, or tag help → inspect exact proposal and rationale → compare current values → approve only the shown proposal. If a transcript is unavailable, state that the proposal is based on metadata rather than pretending the video was watched.

**Change in this branch:** the proposal labels now distinguish **Description + hashtags** from **YouTube tags (separate field)** and show title, description, and tag-character counts with relevant limits. Bare 11-character video IDs are now accepted alongside URLs.

**Existing safety:** exact-proposal approval, stale-metadata checks, restricted snippet updates, and post-update verification remain required.

### 7. Find videos: measured opportunities

**Expected flow:** choose dates → scan measured uploads and available reach data → review candidate evidence and confidence → open a candidate directly in Strategist. Impressions/CTR must only be shown from actual reporting rows; reporting data can be delayed or unavailable.

**Existing behavior:** the shortlist is bounded to recent uploads, uses peer comparisons where possible, and makes the evidence limitation explicit. The scan does not prove metadata caused underperformance and does not forecast performance.

**Keep:** date range, upload comparison limit, candidate metrics, reason/evidence, next measurement, and one “optimize this video” action.

### 8. Analytics: understand then act

**Previous usability issue:** the page led with many metrics at once, the video table was too wide, and ranked optimization candidates were links without a direct route into the metadata workflow.

**Change in this branch:** place four primary metrics first; move secondary engagement/subscriber metrics into an expander; reduce the default video table to the decision-useful fields; put secondary per-video engagement figures behind an expander; and add a direct “Review metadata” action for ranked candidates.

**Keep:** date range, daily views trend, format filter based on the actual duration-based label, available impressions/CTR, traffic sources, coverage status, candidate evidence, and session audit records.

### 9. Studio export upload and deeper evidence

**Expected flow:** sign in as the owner → open Analytics → upload a Studio CSV/TSV or `.xlsx` workbook → see one next-best video-specific action ranked by priority and measured reach → choose **Do it** to draft an exact proposal or **Skip** to see the next action → approve the exact proposal before a YouTube metadata write.

**Implemented in this branch:** multi-file in-memory CSV/TSV/TXT and `.xlsx` workbook parsing with preamble/header detection, recognized-versus-ignored column reporting, video performance comparisons, measured CTR reviews, average percentage viewed comparisons, higher-performing examples worth studying, daily view trend comparisons, source/keyword/geography/audience breakdowns, retention-curve charts and adjacent-drop inspection, a manual attachment flow for retention-only files, and a clean CSV download of the analysed per-video table. The action queue shows one highest-priority issue, sorts same-priority candidates by measured reach, supports Skip, and routes Do it into the existing approval-gated proposal flow. Overlapping report totals are not silently combined; report data stays split by source and range. Raw files are not written to disk or Google Drive. The AI provider receives only the selected video's compact metrics, source label, and date range when the owner explicitly opens a video in Strategist.

**Limitations:** CSV/TSV/TXT and `.xlsx` workbooks are supported; ZIP bundles and direct Microsoft Excel/OneDrive account browsing are not. For a cloud-stored workbook, download and upload it; direct Microsoft access needs a separate consent/configuration. Unknown fields are reported but not interpreted. Revenue/monetization-specific columns are not currently analysed. A curve only reveals where audience retention changed, not why viewers left. See STUDIO_EXPORT_GUIDE.md for export instructions and evidence boundaries.

### 10. Metadata proposal approval, success, and failure

**Expected:** clearly show current vs proposed values; allow revisions through chat; any revision invalidates prior approval; approve the exact final proposal; refetch current metadata before writing; update only title, description, and tags; verify saved values; show success only after verification. If the API result is uncertain, direct the user to reload metadata rather than blindly approving a second time.

**Explicit scope boundary:** the current app does not update thumbnails, publish videos, change visibility, modify audience settings, or change channel settings. A thumbnail workflow is a separate product feature with its own upload, preview, and confirmation design.

## Remaining gaps, in priority order

### P1 — useful for channel optimization
- **Search and choose any owned video by title.** URL/ID input and the recent-upload shortlist cover common flows, but there is no dedicated owned-channel title search. The Analytics catalogue is capped to the most recent 250 uploads.
- **Offer multiple title candidates.** The current AI proposal returns one title at a time. A good follow-on is 3 clearly distinct, factual title variants with a short rationale and a way to promote one into the approved proposal; do not imply that a “score” predicts CTR.
- **Thumbnail review workflow.** No thumbnail comparison, generation, upload, or thumbnail-change approval flow exists in the app today. It needs a separate design before adding controls; do not present it as already supported.
- **Retention analysis.** The current Analytics view has average view duration and engagement measures, not a full audience-retention curve or drop-off analysis.
- **Improve the first-run Analytics state.** Clearly differentiate “no report yet”, delayed Reporting API reach data, missing rows, and genuine zero values. Never fill missing CTR with an estimate.
- **Expand Studio metric families.** CSV/TSV/TXT and `.xlsx` imports are supported; ZIP bundles and revenue-specific analysis remain future work.

### P2 — trust and repeat use
- **Durable conversation and audit history.** Current chat/audit state is session-bound; optional sheet memory covers scans and approved experiments, not every interaction.
- **Peer-period context.** Add simple prior-period comparisons only where equivalent data exists, with explicit sample sizes and no unsupported causation.
- **Video-type controls.** The Data API does not identify Shorts orientation; continue labeling duration-based categories as candidates rather than asserting every video under 180 seconds is a Short.
- **Copy/export affordances.** Add copy buttons for each final metadata field only if they can be tested across desktop/mobile without adding more clutter.

## Out of scope unless explicitly chosen

- Auto-publishing, scheduled uploads, visibility/audience changes, or automatic metadata writes.
- Predicted CTR, guaranteed growth claims, arbitrary “SEO scores”, and tags presented as a growth lever.
- An extra Settings workspace or duplicated top tabs and sidebar navigation.
- Treating a connected Google Sheet as a durable database for all app state.

## Acceptance checklist

- [ ] Signed-out screen shows capability summaries but no channel data.
- [ ] Wrong Google account cannot access any workspace data or API actions.
- [ ] Owner can see the three workspaces before YouTube API authorization.
- [ ] YouTube consent opens in a new tab; the original app tab can refresh connection status.
- [ ] YouTube and Drive memory consent remain separate; memory stays optional.
- [ ] Strategist accepts URLs and bare video IDs; unavailable transcript is never represented as inspected video content.
- [ ] Proposal labels distinguish description hashtags from separate tags, and lengths are visible.
- [ ] No metadata write occurs before approval of the exact proposal.
- [ ] Find videos and Analytics both provide a direct route to review the selected video in Strategist.
- [ ] Missing CTR/rows stay unavailable and have a clear explanation.
- [ ] Studio CSV/TSV/TXT and `.xlsx` exports can be analysed. One prioritized action is shown at a time; Do it drafts, Skip advances, and YouTube only changes after exact-proposal approval.
- [ ] Retention-only reports can be attached to an exact video ID, URL, or title; the curve is not treated as a causal explanation.
- [ ] Mobile navigation buttons remain below the host header and do not render underneath it.
