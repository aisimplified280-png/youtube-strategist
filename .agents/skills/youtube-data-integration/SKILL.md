---
name: youtube-data-integration
description: >-
  Manage YouTube Data API v3, YouTube Analytics API, and YouTube Reporting API
  channel_reach_basic_a1 reports with rate-limiting, robust caching, and accurate metric distinction.
  Use when fetching video data, reach reports, comments, or optimizing YouTube API calls.
---

# YouTube Data Integration

Procedures for reliably integrating YouTube Data, Analytics, and Reporting APIs.

## Integration Protocols

1. **API Separation**:
   - **Data API v3**: Retrieve snippet, contentDetails (duration), status, and statistics (views, likes, comments).
   - **Analytics API**: Query aggregated views, watch time, avg view duration, sub gains/losses.
   - **Reporting API**: Download asynchronous `channel_reach_basic_a1` reports for true impressions and thumbnail CTR.

2. **Metric Precision**:
   - Never represent missing reach data as `0%` CTR or `0` impressions. Explicitly report `Unavailable: report pending or missing`.
   - Classify videos <= 180 seconds as "Short candidate", not confirmed Short, because orientation is not exposed in public snippet data.

3. **Safe Write Operations**:
   - Only modify `snippet.title`, `snippet.description`, and `snippet.tags`.
   - Never overwrite categoryId or defaultLanguage with empty strings.
   - Always refetch snippet immediately before writing to verify it has not changed since approval.

## Acceptance Criteria

- Caching for repeated API queries within the session.
- Graceful handling of HTTP 401, 403 (quota), and 404 responses.
- Accurate distinction between zero performance and unavailable reporting data.
