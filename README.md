# YouTube Strategist

> **A private, owner-only AI co-pilot for YouTube creators.**  
> It reads real channel telemetry (YouTube Analytics, Reporting API reach metrics, Studio CSV/Excel exports), surfaces the **ONE most valuable packaging action to take next**, helps craft better titles/descriptions/tags in a conversational editor, and applies changes to YouTube **only after explicit approval of the exact proposal**.

---

## Architecture Overview

This repository contains two complementary components:
1. **The Streamlit Web Application (`app.py`, `streamlit_app.py`)**: A private, hosted web app deployed on Streamlit Community Cloud with Google OpenID Connect auth, separate OAuth flows, Studio export parser, and live proposal diffs.
2. **The Agent Skills Pack (`skills/`)**: 12 local agent skills with 6 stdlib-only Python command-line tools for scripting, chapters, dead air trimming, retention curve analysis, hook scoring, and viral packaging.

---

# Part 1: Streamlit Dashboard

## What It Does

- **Home ("What should I do today?")**: Delivers 5-second clarity. Features a single hero card with the highest-leverage optimization candidate, a 3-step setup strip, channel pulse tiles with period-over-period deltas, and recent verified experiments.
- **Find Videos & Studio Export Analysis**: Drag-and-drop multiple YouTube Studio CSV, TSV, or `.xlsx` workbooks. Detects report types, recognized vs ignored columns, and surfaces candidates one at a time with evidence chips (CTR vs baseline, APV, retention cliff timestamp) and Skip / Undo Skip buttons.
- **Strategist Co-Pilot**: Split-screen conversational editor. Chat on the left, live proposal on the right. Shows visual word-level diffs, real-time character counters (desktop 60-char cutoff, mobile 40-char cutoff, tag budget 500 chars), packaging warnings, and an approval gate.
- **Channel Dashboard**: Date range presets (7d, 28d, 90d, custom), period-over-period comparison, sortable/filterable video performance table, and an Evidence Drawer detailing peer grouping rules (minimum 3 peers, duration format, age bucket).
- **Memory & Experiments**: Timeline of verified applied metadata changes (recorded only after YouTube confirms the write) and weekly scan snapshots.
- **Settings & Connections**: Health status cards for Google sign-in, YouTube, Google Sheets memory, AI providers, diagnostics, and a Demo Mode toggle.

---

## Non-Negotiable Safety & Privacy Rules

1. **Owner-Only Gate:** Strict Google OpenID Connect email and verified-email check against `secrets.access.owner_email` occurs *before* any API client is instantiated or private channel data is shown.
2. **Strictly Decoupled OAuth Flows:** Google rejects combined YouTube and Google Drive requests. YouTube Analytics/Metadata (`yt-analytics.readonly`, `youtube.force-ssl`) and private Sheets memory (`drive.file`) use separate OAuth grants and encrypted vaults.
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
- **Demo Mode:** Toggleable in Settings (or from the landing page) when no channel credentials are connected. Renders realistic sample data across all pages with zero external network requests.

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
