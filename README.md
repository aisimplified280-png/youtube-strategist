# YouTube Strategist

> **A private, owner-only YouTube Metadata Copilot for AI Simplified Lab.**  
> One conversational interface to scan recent channel performance, find videos worth optimizing, formulate evidence-based title options, improve descriptions and tags, and publish only after explicit approval of the exact proposal.

---

## Architecture Overview

This repository contains two complementary components:
1. **The Streamlit Web Application (`app.py`, `streamlit_app.py`)**: A private, hosted web app deployed on Streamlit Community Cloud with Google OpenID Connect auth, separate OAuth flows, Studio export parser, and live proposal diffs.
2. **The Agent Skills Pack (`skills/`)**: 12 local agent skills with 6 stdlib-only Python command-line tools for scripting, chapters, dead air trimming, retention curve analysis, hook scoring, and viral packaging.

---

# Part 1: Streamlit Metadata Copilot

## What It Does

- **Single conversational workspace:** The owner signs in with the configured Google account. There is no sample-channel mode or simulated publishing path.
- **Channel opportunity scan:** The landing action scans up to 200 recent channel uploads and reviews the last 28 complete days of available YouTube Analytics data. It ranks below-benchmark videos using format-aware channel comparisons.
- **Video-specific optimization:** Paste a YouTube video or Short link to load current metadata, public metrics, and available captions. The assistant prepares title, description, and separate-tag recommendations grounded in the supplied content.
- **Three structured title angles:** Curiosity, direct value, and search. Missing or invalid options are rejected rather than filled with generic titles. Description-only and tag-only edits preserve the current title.
- **Studio evidence:** Attach CSV, TSV, TXT, or XLSX exports. Impressions and CTR are used only when both values are present together in matching video-level evidence from the same report period; CTR is never guessed from views.
- **Approval-gated publishing:** The exact proposed metadata is shown for editing and comparison. A real YouTube write is available only for the verified owner channel, after explicit approval, a stale-metadata check, and post-write verification.
- **AI providers:** Groq is primary and Gemini is the configured fallback. No other pages or dashboards are exposed by the app entry point.

---

## Non-Negotiable Safety & Privacy Rules

1. **Owner-Only Gate:** Strict Google OpenID Connect email and verified-email check against `secrets.access.owner_email` occurs *before* any API client is instantiated or private channel data is shown.
2. **Strictly Decoupled OAuth Flows:** Google rejects combined YouTube and Google Drive requests. YouTube channel discovery (`youtube.readonly`), Analytics (`yt-analytics.readonly`), and approved metadata writes (`youtube.force-ssl`) use a YouTube grant; private Sheets memory (`drive.file`) remains a separate grant and encrypted vault.
3. **Exact Approval Gate:** Nothing is written to YouTube without explicit user approval of the exact title, full description, and tags shown. Editing a proposal invalidates prior approvals. Stale metadata checks re-fetch live data before saving, and post-write verification checks the result.
4. **Zero Metric Hallucination:** If impressions, CTR, or transcripts are unavailable, the app clearly displays "Unavailable" with the reason. No guessed CTR, no causal claims, and videos $\le$180s are labelled as *Short candidates*, not confirmed Shorts.
5. **No Secrets or Tokens in Git:** Secrets are loaded only via Streamlit Secrets. Tokens are encrypted at rest with Fernet.

---

## Free Deployment Guide (Streamlit Community Cloud)

### 1. Google Cloud Console Setup
1. Create a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable these three APIs:
   - **YouTube Data API v3**
   - **YouTube Analytics API**
   - **YouTube Reporting API**
3. Configure the **OAuth Consent Screen** (User Type: External, Publishing Status: Testing). Add your personal Google account email as a Test User.
4. Create an OAuth Client ID with Application Type: **Web application**.
5. Add the following **Authorized redirect URIs** (replace `<app-name>` with your Streamlit Cloud subdomain):
   - `https://<app-name>.streamlit.app/~/+/oauth2callback` (Streamlit built-in sign-in)
   - `https://<app-name>.streamlit.app/~/+/` (App YouTube and Memory OAuth callback)
   - *For local development:* `http://localhost:8501/oauth2callback` and `http://localhost:8501/`

### 2. Streamlit Community Cloud Setup
1. Push this repository to your GitHub account.
2. In [Streamlit Community Cloud](https://share.streamlit.io/), create a new app:
   - **Repository:** your-repo
   - **Main file path:** `streamlit_app.py` (or `app.py`)
   - **Python version:** 3.12 (in Advanced settings)
3. Under **App settings → Secrets**, paste the structure from `.streamlit/secrets.toml.example` and fill in your keys:

```toml
[auth]
redirect_uri = "https://<app-name>.streamlit.app/~/+/oauth2callback"
cookie_secret = "REPLACE_WITH_A_RANDOM_STRING_AT_LEAST_32_CHARS"
client_id = "YOUR_GOOGLE_WEB_CLIENT_ID"
client_secret = "YOUR_GOOGLE_WEB_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
client_kwargs = { scope = "openid email profile", prompt = "select_account" }

[access]
owner_email = "your-account@gmail.com"

[youtube_oauth]
client_id = "YOUR_GOOGLE_WEB_CLIENT_ID"
client_secret = "YOUR_GOOGLE_WEB_CLIENT_SECRET"
redirect_uri = "https://<app-name>.streamlit.app/~/+/"

[security]
state_secret = "REPLACE_WITH_ANOTHER_RANDOM_STRING_AT_LEAST_32_CHARS"

# Primary AI Provider
[groq]
api_key = "gsk_YOUR_GROQ_API_KEY"
model = "openai/gpt-oss-120b"
fallback_models = []

# Fallback AI Provider
[gemini]
api_key = "AIzaSy_YOUR_GEMINI_API_KEY"
model = "gemini-2.5-flash"
fallback_models = ["gemini-2.0-flash", "gemini-1.5-flash"]
```

---

## The Fine Print (Honest Technical Boundaries)

- **Audit & History Persistence:** In the hosted dashboard, active chat turns and session audit records are stored in server-side session state. Verified experiments and weekly scans can be saved to your private Google Sheet ("Strategist Memory") using the `drive.file` scope. The command-line script writes local files to `~/.ai-simplified-youtube/metadata_audit.jsonl`.
- **Google OAuth Testing Refresh Token Expiry:** Google specifies that refresh tokens in "Testing" mode expire after 7 days when accessing sensitive YouTube scopes. When this occurs, the app displays a clear "Reconnect needed" status pill and button rather than crashing.
- **Reporting API Reach Data Delay:** Thumbnail impressions and CTR are sourced from the asynchronous `channel_reach_basic_a1` report. When first enabled, Google can take up to 48 hours to generate the first daily report file.
- **Owner-only operation:** The hosted Copilot has no Demo Mode, synthetic channel fixtures, or simulated approval. If Google or YouTube credentials are disconnected, it asks the owner to sign in or reconnect before accessing channel data.

---

# Part 2: Agent Skills Pack (`skills/`)

The repository also includes 12 standalone agent skills and 6 Python stdlib CLI tools:

| Skill | CLI Tool | Description |
| :--- | :--- | :--- |
| `/yt-package` | `skills/yt-package/title.py` | Lints YouTube title and thumbnail pairing for duplication, mobile cutoff (40 chars), and desktop cutoff (60 chars). |
| `/yt-script` | `skills/yt-script/hookscore.py` | Scores video hooks across 5 weighted dimensions (specificity, address, stakes, curiosity, brevity). |
| `/yt-retention` | `skills/yt-retention/retention.py` | Detects audience retention cliffs and hook leaks from Studio retention exports. |
| `/yt-chapters` | `skills/yt-chapters/chapters.py` | Generates semantic video timestamps from transcripts. |
| `/yt-edit` | `skills/yt-edit/deadair.py` | Detects pauses, filler words, and dead air in scripts or captions. |
| `/yt-viral` | `skills/yt-viral/swipe.py` | Classifies title hooks and structures against viral formula patterns. |
| `/yt-optimize` | — | Comprehensive metadata optimization workflow. |
| `/yt-seo` | — | Grounded search term and tag discovery without keyword stuffing. |
| `/yt-shorts` | — | Short-form script pacing, loop structure, and hook design. |
| `/yt-audit` | — | Channel-wide audit for underperforming uploads. |
| `/yt-comment` | — | Comment triage and community engagement prompts. |
| `/yt-plan` | — | Content calendar planning based on audience demand signals. |

### Running the CLI Tools
All 6 CLI tools use only Python standard library modules (`sys`, `re`, `json`, `csv`, `math`) and have zero external dependencies:
```bash
python skills/yt-package/title.py --title "Cut LLM Latency by 80%" --thumb "PROMPT CACHING"
python skills/yt-script/hookscore.py --hook "Are you wasting 3 hours every day on video editing?"
```

---

## Testing

Run all unit tests across the connector, security gate, token store, chat co-pilot, studio analyzer, linter, and LLM router:
```bash
python -m unittest discover tests
```

## Licence

MIT. Use it, change it, ship it.
