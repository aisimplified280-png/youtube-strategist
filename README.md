# youtube-strategist

## The YouTube agent skills

Twelve Claude and VS Code agent skills for running a YouTube channel. Free, MIT, no signup, no API
key for the standalone tools. The optional analytics and metadata connector uses your own Google
OAuth credentials.

One of them writes your script off 21 hook formulas and scores the hook before you waste a take on
it. One lints the title and the thumbnail as a single pairing, because writing them separately is
why half of your click surface says the same thing twice. One reads your audience-retention export
and tells you the exact second people left and what you were saying when they did. One turns a
transcript into an edit decision list. One finds the Shorts already hiding inside a long video. One
goes and finds what is working in your niche and ranks it by how far each video beat its own
channel, not by how big the channel is.

**Nothing gets published until you do it.** These skills write. You upload.

## Install

Paste this into Claude:

```
https://github.com/Jakeschincariol/youtube-agent-skill

Install this skill, then confirm /yt-script works.
```

Or do it yourself, in Claude Code:

```bash
git clone https://github.com/Jakeschincariol/youtube-agent-skill.git
cp -r youtube-agent-skill/skills/yt-* ~/.claude/skills/
```

Or as a plugin:

```
/plugin marketplace add Jakeschincariol/youtube-agent-skill
/plugin install youtube-agent
```

Project-local instead of global: copy the same folders into your repo's `.claude/skills/`. No Claude
Code at all? Paste any single `SKILL.md` at the top of a chat and it runs as a mode. You lose the six
Python tools, which is most of the point of `/yt-script`, `/yt-retention` and `/yt-edit`, but the
rest works.

Then spend ten minutes on [`templates/voice.md`](templates/voice.md). Copy it to
`~/.claude/youtube/voice.md` and fill it in, or send Claude three of your own videos and say "write
my voice.md from these". Every skill reads that file. It matters more here than anywhere else,
because you have to say the words out loud.

## The twelve

| command | what it does |
| --- | --- |
| `/yt-script` | One idea into a script. Five hooks off [21 formulas](skills/yt-script/hooks.json), scored, then the spoken script with the retention beats marked. |
| `/yt-package` | Title and thumbnail as one pairing, linted for truncation, duplication and vagueness. |
| `/yt-edit` | A transcript into an edit decision list: dead air, filler cues, retakes, with timecodes. |
| `/yt-comment` | The comment section triaged into four piles, then replies in your voice. Says which one to pin. |
| `/yt-plan` | A week that fits the hours you actually have. One anchor, one cheap one, three Shorts. |
| `/yt-viral` | What is working in your niche, ranked by multiple over each channel's own median. |
| `/yt-retention` | Your retention export read properly: hook leak, the cliffs, the slide, and what to change. |
| `/yt-shorts` | The Shorts already inside a long video, with a new first line written for each. |
| `/yt-seo` | The description, the tags that are worth having, and the three queries this should win. |
| `/yt-chapters` | Chapters from a transcript, validated against YouTube's own rules so they render. |
| `/yt-audit` | The whole channel, ending in ONE fix rather than twenty. |
| `/yt-optimize` | Analyze channel performance, propose video metadata, then update only after approval of the exact proposal. |

## The six tools

Every one of these runs on a clean Python 3 with no dependencies. They are the reason the skills are
not just prompts.

```bash
python3 skills/yt-script/hookscore.py --hook "one line"        # 5-property hook panel
python3 skills/yt-package/title.py --title "..." --thumb "..." # title + thumbnail linter
python3 skills/yt-edit/deadair.py transcript.srt               # edit decision list
python3 skills/yt-chapters/chapters.py transcript.srt          # validated chapters
python3 skills/yt-retention/retention.py retention.csv         # where they left, and why
python3 skills/yt-viral/swipe.py collected.json --min 2.0      # outliers by own-channel multiple
```

## The fine print

The part most packs leave out.

**It does not publish videos.** The optional `/yt-optimize` connector can update only a video's
title, description, and tags after you approve the exact proposal in chat. It cannot publish or
delete videos, change visibility, alter audience settings, or modify channel settings. The other
skills still produce material for you to review and use.

### YouTube analytics and metadata connector

`youtube_analytics_connector.py` keeps the existing read-only Analytics exports. The
`/yt-optimize` skill uses its read-only analysis and exact-proposal approval flow to update
**only** a video's title, description, and tags. It never publishes a video or changes its
privacy, publication status, audience settings, thumbnails, or channel settings.

The agent analyzes a URL, ID, or title directly in chat. To inspect the latest short-duration
candidates, it can request:

```bash
python youtube_analytics_connector.py --latest-short-candidates 10
```

The YouTube Data API does not expose the Shorts designation or video orientation. Videos up to
three minutes are therefore labelled **Short candidates**, not confirmed Shorts. The connected
Analytics API provides views, watch time, average view duration, likes, comments, shares,
subscriber changes, and traffic sources where available. Impressions and thumbnail CTR are
explicitly unavailable from this report endpoint; no scores or performance forecasts are
fabricated. If metadata alone cannot support factual optimization, the agent asks for content
context rather than inventing claims.

After showing the current and proposed title, complete description, hashtags, separate tags, and
diff, the skill stages the exact proposal locally and asks for approval. A revised proposal
invalidates the prior approval ID. After approval, the connector checks that YouTube has not
changed since review, updates only approved metadata, and refetches to verify the result. Audit
records are stored in
`~/.ai-simplified-youtube/metadata_audit.jsonl` (on Windows:
`%USERPROFILE%\.ai-simplified-youtube\metadata_audit.jsonl`). OAuth tokens are never included in
the audit log. Proposal state is kept outside the repository in the same private user directory.

The OAuth authorization includes `youtube.force-ssl` for metadata changes. If saved authorization
needs refreshing, the connector tries the refresh token first and starts Google's normal browser
sign-in flow if refresh fails. Keep the OAuth client file and token in
`~/.ai-simplified-youtube/`; never add or share them.

### Streamlit Community Cloud dashboard

`streamlit_app.py` is the browser dashboard entry point. It queries YouTube Analytics directly
for channel performance and uses the YouTube Reporting API's `channel_reach_basic_a1` report for
thumbnail impressions (`video_thumbnail_impressions`) and click-through rate
(`video_thumbnail_impressions_ctr`). These reach metrics are not requested from Analytics
`reports.query`, where `impressions` is an unsupported identifier. Reach values are displayed only
when the downloaded report includes the metric; missing, incomplete, or unsupported data is shown
as unavailable with a reason. No views-based substitute or estimated CTR is used. The daily report
includes date and video dimensions and is filtered to the chosen UTC date range. Reach totals sum
only returned video/date rows; missing rows are not turned into zero values. Google lists this
report under [channel reach reports](https://developers.google.com/youtube/reporting/v1/reports/channel_reports)
and documents the [reach metrics](https://developers.google.com/youtube/reporting/v1/reports/metrics#video_thumbnail_impressions)
and [date/video dimensions](https://developers.google.com/youtube/reporting/v1/reports/dimensions).

Reporting API jobs must be created before YouTube generates report files. The first file may take
up to 48 hours to become available, and the API does not promise historical report files for dates
before the job was created. Later daily files are asynchronous too. Use **Refresh channel reports**
to retry after Google has generated new data. The app explains delayed or partial report coverage.
Enable **YouTube Reporting API** in the same Google Cloud project as YouTube Data API v3 and
YouTube Analytics API. The connector's existing `yt-analytics.readonly` OAuth scope is sufficient
for reading these reports; no extra metadata-write scope is used for reach retrieval.

The authorized local Reporting API read attempt returned HTTP 403, so live report availability
for this channel is **not verified**. Check API enablement, OAuth consent/scopes, channel
authorization, and Google project access after deployment. Mocked report parsing tests do not
prove the live API configuration is working.

Optimization ranking compares measured views only within the selected date range, format inferred
from duration, and a similar-upload-age group. It requires at least three measured peers and states
the comparison evidence; it does not infer a metadata cause or forecast results. Videos up to 180
seconds are labelled **short-duration candidates**, not confirmed Shorts, because the Data API
does not return the Shorts designation or orientation. The catalogue is capped at the selected
number of most recent uploads (up to 250). The metadata suggestion generator is deliberately
conservative and uses only topic phrases in the current title and description; it is not an AI
transcription service. Edit or reject any suggestion that does not fit the video.

#### Deploy for free

1. Push this repository to a GitHub account that you can connect to Streamlit Community Cloud.
   Do not commit OAuth client JSON, token files, CSV exports, `.env` files, or Streamlit secrets.
   The current local `client_secret.json` is an **installed/desktop** OAuth client and cannot be
   used for the hosted callback.
2. In Google Cloud Console, select or create a project and enable **YouTube Data API v3**,
   **YouTube Analytics API**, and **YouTube Reporting API**. Configure the OAuth consent screen
   with the owner account as a test user while the app is in testing mode.
3. Create an OAuth client of type **Web application**. After deploying the app and learning its
   `https://<app-name>.streamlit.app` address, add both authorized redirect URIs to that client:
   - `https://<app-name>.streamlit.app/oauth2callback` for Streamlit's Google sign-in.
   - `https://<app-name>.streamlit.app/` for the YouTube Analytics/Data API authorization callback.

   The same web-client ID and secret are used for both flows. Register `http://localhost:8501/oauth2callback`
   and `http://localhost:8501/` too only if you will test the app locally.
4. In Streamlit Community Cloud, create an app from your GitHub repository and set the app file
   path to `streamlit_app.py`. Select Python 3.12 in the deployment's advanced settings. The
   dependencies are in [requirements.txt](requirements.txt).
5. In the app's **Settings → Secrets**, paste the structure from
   [.streamlit/secrets.toml.example](.streamlit/secrets.toml.example), replacing every placeholder:
   - Set `auth.redirect_uri` to the deployed `/oauth2callback` URI.
   - Set `youtube_oauth.redirect_uri` to the deployed root URI, including its trailing `/`.
   - Put the Web OAuth client ID and secret in both sections.
   - Set `access.owner_email` to the single Google account allowed into the dashboard.
   - Generate independent, random values for `auth.cookie_secret` and `security.state_secret`.
6. Open the app and sign in as the configured owner. Select **Authorize YouTube Analytics and
   metadata access**, consent to Analytics read access and YouTube metadata access, and return to
   the dashboard. The app checks Google's verified email against the configured owner before it
   creates API clients or fetches channel data. An unapproved account sees no dashboard data or
   YouTube actions. The Streamlit URL itself is not secret; keep the Google owner check enabled.
7. Select a date range, inspect the performance table and comparison evidence, then select a
   listed video or enter its YouTube URL, ID, or title in the video lookup. Review or edit its
   proposal, and press **Approve and apply this exact proposal** only when the shown title,
   complete description, and tags are correct. Saving edits invalidates the prior proposal. The
   connector refetches the full snippet before writing, rejects stale proposals, updates only
   title/description/tags, verifies the saved values, and reports the result.

The hosted flow uses Google OpenID Connect for identity plus a separate Google Web OAuth
authorization-code flow for YouTube access. OAuth state is signed, expires after 10 minutes,
is bound to the authenticated Google account, and must match the pending state held in that
browser session; the callback consumes it before token exchange. API credentials and tokens are
held in Streamlit Secrets/server-side session state; they are not logged or written to the cloud
filesystem.

**Google OAuth testing-mode limitation:** Google documents that refresh tokens for an external
OAuth consent screen left in **Testing** expire after seven days when non-basic scopes (such as
YouTube scopes) are requested. Reconnect/consent again when that happens. Do not treat this free
setup as unattended long-term authorization. Broader use or production publishing of the OAuth
app may require Google verification and compliance review for the requested YouTube scopes.

**Audit and approval state:** Approval proposals, tokens, and audit entries are held in the
current authenticated Streamlit session only. The dashboard shows session audit outcomes but
does not claim durable audit history; sessions/restarts can lose that state. Add an access-
controlled persistent database before relying on long-term history or integrating Slack.

**Streamlit Community Cloud access:** The app URL is reachable, but Google sign-in and the verified
owner-email check in the app are the access-control barrier. Verify this by opening the deployed
URL in a signed-out/private browser and with a non-owner Google account; neither should see channel
data or receive the YouTube API authorization link.

**`hookscore.py` is a heuristic, not a predictor.** The panel was built and calibrated against 74
real short-form hooks (the first 15 seconds of auto-captions, top-8 and bottom-8 by views across
five channels). It separates deliberately bad hooks from real ones well. It separates a given
creator's hits from their own misses barely at all. A low score is a reason to look again; a high
score is not a promise.

**The formula classifier is about the words, not the result.** When `/yt-viral` says a title used
The Statistic, that is a judgement about the title you can see, not a claim about why the video got
its views.

**`/yt-viral` reads, it does not scrape.** Public listings only. It never logs in as you and never
touches your credentials.

**Tags barely matter** and this pack says so instead of selling you a tag generator.

**Nothing invents a number.** If a skill wants a figure it does not have, it asks you for it or
writes the line without it.

## Licence

MIT. Use it, change it, ship it.


### Conversational YouTube Strategist (Grok first, Gemini backup)

The Streamlit dashboard includes a conversational metadata editor that uses xAI Grok as its primary provider and Gemini only as a fallback. It accepts a video URL or ID from the configured owner channel and a public reference video URL. It retrieves available public reference metadata and attempts to retrieve a public transcript; if no transcript is available, it clearly falls back to metadata only. It does not claim to inspect video visuals from a URL.

Add both providers to Streamlit Community Cloud → Manage app → Settings → Secrets. Keep both keys private and never commit them to the repository:

```toml
[xai]
api_key = "YOUR_XAI_API_KEY"
model = "grok-4.6"
fallback_models = []

[gemini]
api_key = "YOUR_GEMINI_API_KEY"
model = "gemini-3.8-flash"
fallback_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
```

Create the Grok key in [the xAI Console](https://console.x.ai/) and the Gemini key in [Google AI Studio](https://aistudio.google.com/apikey). Grok is always tried first. If it hits a model quota/rate limit (HTTP 429), is unavailable (HTTP 404), returns a transient server error (HTTP 5xx), times out, or returns an unusable response, the app tries any configured Grok fallback models and then switches to Gemini. Gemini fallback models are tried in order for the same kinds of recoverable failures. The UI shows the provider and model that answered. Authentication/permission failures also allow the other provider to be attempted, since the other provider has separate credentials. If all configured models fail, the app reports that clearly. Fallback is best-effort: project-wide quota limits or invalid keys for both providers can still block a request. API usage and billing are separate for each provider.

Model names are configurable. The chat sends the target video's title, description, tags, the reference video's public metadata, available reference transcript, and your chat instructions to whichever provider handles the request: Grok first, Gemini only if Grok fails. xAI API usage and Gemini API usage/billing are separate; Gemini API limits are also separate from a Google AI Pro consumer subscription.

Every response is a proposal only. The app displays current and proposed title, description and tags. You must explicitly click **Approve and apply this exact proposal** before a change is sent to YouTube. The existing connector re-checks that the target metadata has not changed since review, validates the proposal, applies only supported snippet metadata, and verifies the update. Changing the proposal invalidates the prior approval digest. Transcript retrieval is best-effort; transcript-disabled, private, age-restricted, or otherwise unavailable videos may fall back to public metadata.

The chat history and approval audit are held in the current Streamlit session. They are not durable across session expiry. Do not enter secrets or other sensitive information in the chat.
