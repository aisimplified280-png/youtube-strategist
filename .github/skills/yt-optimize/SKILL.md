---
name: yt-optimize
description: >-
  Analyze real AI Simplified Lab YouTube video and Short analytics, optimize metadata,
  review title/description/hashtags/tags, approve an exact proposal, or update metadata.
  Use when asked to optimize a YouTube video or Short, analyze recent Shorts, improve
  YouTube SEO, or apply metadata after conversational approval.
---

# yt-optimize

Use the workspace Python environment to run `youtube_analytics_connector.py`. This workflow
fetches channel data, stages proposals in the local private application directory, and can
update only the exact proposal that the user approves. Do not ask the user to create files or run
commands.

## Analyze

1. For a URL, video ID, or title, run:

   ```powershell
   python .\youtube_analytics_connector.py --analyze "<URL, ID, or title>"
   ```

   For a request such as "analyze my latest 10 Shorts", run:

   ```powershell
   python .\youtube_analytics_connector.py --latest-short-candidates 10
   ```

2. Read the JSON output. It contains current snippet metadata, publication date, duration,
   performance metrics for the specified period, traffic sources, audience demographics where
   available, and a comparison with age/duration-matched videos when data is available.
3. The YouTube Data API does not expose the authoritative Shorts label or video orientation.
   Treat videos of at most 180 seconds as **Short candidates**, not confirmed Shorts. State this
   limitation, and never claim a candidate is a Short based on duration alone.
4. Impressions and thumbnail CTR are reported as unavailable by this API integration. Traffic,
   audience, and peer reports may also be unavailable or empty; use each report's availability
   field. Do not infer, estimate, or fabricate metrics. Distinguish missing data from zero.
5. Base factual claims only on the current title, description, tags, and user-provided context.
   The connector does not retrieve or transcribe audiovisual content. If the metadata is not
   enough to make an accurate proposal, ask for the missing content context rather than inventing
   claims. Use only actual returned analytics for performance explanations, and do not promise a
   performance outcome.

## Prepare and present a proposal

1. Propose one video's title, complete description, and tags. Preserve the existing useful
   description content and every useful link. Make only necessary description edits; keep
   existing tags and add appropriate ones. Put hashtags in the description, not in YouTube's
   separate tags list.
2. Stage the candidate proposal by piping JSON to the connector. This is an internal agent
   operation; do not ask the user to prepare JSON or run a command:

   ```powershell
   @'
   {"title":"PROPOSED TITLE","description":"COMPLETE DESCRIPTION","tags":["existing-tag","new-tag"]}
   '@ | python .\youtube_analytics_connector.py --stage-proposal VIDEO_ID
   ```

   Replace the example values with the candidate proposal. Staging is not a write. The connector
   validates limits, refetches the video, prints the current metadata and diff, and returns a
   20-character proposal ID. Compare the freshly fetched current metadata to the analysis. If it
   changed, regenerate and stage again before showing the user anything. Then present the exact
   staged current and proposed title, complete description (including hashtags), and separate
   tags side by side, with a readable diff and short explanation grounded in returned channel
   data. State that no YouTube write has occurred and include the proposal ID.
3. Ask whether the user approves that exact displayed proposal for that one video. Do not call
   `--apply-proposal` before an unambiguous, explicit approval. Silence, a request for edits, or
   approval for another video is not approval.
4. If the user requests any edit, stage the revised proposal again. Staging invalidates any
   pending proposal for the same video, so the old approval ID cannot be used.

## Apply only after approval

After the user explicitly approves the latest proposal, invoke:

```powershell
python .\youtube_analytics_connector.py --apply-proposal APPROVED_PROPOSAL_ID
```

The connector verifies the exact proposal hash and that YouTube metadata has not changed since
review, updates only the title, description, and tags in the `snippet` part, preserves the
category and other writable snippet fields, refetches the video, verifies saved values, and writes
a local audit entry. If the video changed, the approval is stale: reanalyze and ask again. Never
retry an update blindly after an error; report the actual result.

Report success only when post-update verification succeeds. Include the video title and link. If
the user declines, make no write. Never publish, delete, change visibility, alter audience
settings, thumbnails, or channel settings.
