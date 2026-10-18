# YouTube Studio Export Analysis

The hosted dashboard's **Analytics** workspace includes an optional uploader for CSV/TSV reports exported from YouTube Studio. Use it when Studio contains a metric or breakdown that is missing from the live API dashboard.

## How to export from Studio

YouTube's current help page explains that exports are available in Analytics → Advanced mode / See more → Export current view:

https://support.google.com/youtube/answer/9717005

The Studio interface is being updated gradually, so labels may differ slightly by account. Choose the video/channel, date range, breakdown and metrics first, then export the current view as CSV (or TSV when offered). Studio's Advanced Mode exports are limited in row count; use narrower date windows or split by video when the report is too large.

## Recommended report types

### 1. Content / video performance
Select columns such as video title, video ID when available, views, watch time, average view duration, average percentage viewed, impressions, impressions click-through rate, subscribers gained/lost, likes, comments, and shares.

The analyser can compare CTR and average percentage viewed within the uploaded video's set. It only ranks CTR rows with at least 100 measured impressions, as a minimum sample safeguard used by this app—not as an official YouTube threshold. A below-median result is a reason to inspect title/thumbnail alignment, not proof that metadata caused the result.

### 2. Audience retention
Export the retention report for a specific video. Files with an elapsed-video-time column and audience-retention values are plotted as a curve. The analyser identifies large adjacent drops for inspection; it cannot infer what the viewer saw or why a person left from the curve alone.

Relative audience retention is displayed as a comparison index, not as the percentage of your viewers still watching. If the export doesn't identify its video by title or ID, attach the curve with the video URL, ID or exact title.

### 3. Daily performance
Include a date/day dimension with views and, when useful, watch time. Reports with several days receive a first-half-versus-second-half daily-average comparison where there is enough data. The result is a trend signal; it is not a claim that one change caused the trend.

### 4. Traffic source, search term, geography, demographics or device breakdown
Keep the relevant breakdown column with views, watch time or viewer percentage. The app summarizes the leading reported segment as a research lead. Small or missing rows should not be interpreted as zero, and audience breakdowns do not prove causality.

## What the app does after upload

- It finds the table header even when Studio has added report-title/date-range lines before it.
- It parses supported columns and displays which columns were recognized and which were ignored.
- It keeps separate files separate, warns when a changed file selection has not been re-analysed, and does not silently sum multiple report totals (their date ranges may overlap).
- It provides a prioritized action list, a per-video metrics table, and a cleaned CSV of the analysed video table.
- It can attach selected video metrics to the Strategist context so the AI can use numeric performance evidence while preparing a metadata proposal. A retention curve can also be attached manually to a selected video.
- It does not automatically edit, publish, or change any YouTube setting. The existing exact-proposal approval gate still applies.

## Privacy and evidence boundaries

Uploaded bytes are parsed in the current Streamlit session and this feature does not write source files to disk or Google Drive. Normalized metrics remain in server-side session state until the session ends or the analysis is cleared. **When you choose Review in Strategist, the compact evidence for that video (including source filename/date range and recognized metrics) is sent with the prompt to the configured AI provider (Groq first, Gemini fallback when configured). The raw uploaded file is not sent by this workflow.** Do not upload data you aren't comfortable processing through the app's configured AI provider.

Only recognized metrics are used. Unknown columns are disclosed rather than guessed. CTR is never estimated from views. A metric correlation or relative comparison is never described as proof that a title, thumbnail, edit, or upload time caused a performance result.

## Current scope

- Accepted: CSV, TSV and text-delimited report exports.
- Not yet supported: Excel workbooks (.xlsx), ZIP bundles, direct YouTube Studio account access, raw video-file uploads, thumbnail uploads, or automatic Studio synchronization.
- The parser currently focuses on audience growth and content performance; it does not interpret estimated revenue or monetization-specific columns.
