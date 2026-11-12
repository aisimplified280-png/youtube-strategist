# YouTube Strategist

> **A private, owner-only YouTube Metadata Copilot for AI Simplified Lab.**  
> One conversational interface to scan recent channel performance, find videos worth optimizing, formulate evidence-based title options, improve descriptions and tags, and publish only after explicit approval of the exact proposal.

---

## Architecture Overview

This repository uses one owner-authenticated Streamlit application as the control centre. GitHub Actions supplies reliable scheduled execution because Streamlit Community Cloud is not an always-on worker. Supabase stores encrypted OAuth grants, run snapshots, proposals, and an audit trail. No second dashboard is required.

# Part 1: Streamlit Copilot and Autonomous YouTube Strategist

## What It Does

- **One owner-only Streamlit workspace:** Google sign-in, YouTube OAuth, conversational metadata optimization, strategist settings, proposal approvals, performance evaluation, and run history stay in the existing app.
- **Three monitoring and optimization cadences:**
  - **Daily monitoring (`30 2 * * *` UTC / 08:00 AM IST):** rapid detection of emerging packaging underperformance, recent upload health, steady maintenance.
  - **Weekly optimization (`30 3 * * 0` UTC / Sun 09:00 AM IST):** cross-video pattern recognition, weekly upload optimization sweep, cohort comparisons, systematic topic gaps.
  - **Monthly strategic review (`30 4 5 * *` UTC / Day 5 10:00 AM IST):** strategic macro review, subscriber conversion compounding, back-catalogue packaging refreshes, evergreen vs decay analysis.
- **Full catalogue pagination beyond 500 uploads:** upload playlist discovery scans the entire catalogue with resumable pagination without artificial ceilings.
- **Evidence-first diagnosis:** evaluates views, watch time, average percentage viewed, subscriber conversion per 1,000 views, traffic sources, impressions, and impression CTR from the YouTube Reporting API. Missing metrics remain unavailable; uploads under 180 seconds stay unclassified and are excluded from peer-underperformance ranking and automatic metadata candidates until orientation can be confirmed.
- **Gemini as the central decision maker:** Gemini 3.1 Pro Preview is the primary model through the Gemini Developer API (`gemini-3.1-pro-preview`), with fallbacks to `gemini-3.8-flash` and `gemini-2.5-flash`. Gemini produces structured decisions with diagnosis, proposed fields, confidence score (0.0 to 1.0), and factual evidence.
- **Three Automation Modes:**
  - **Paused:** Scanning and briefings run; no proposals are published automatically.
  - **Shadow mode:** Runs full analysis and proposal generation; logs `shadow_published` records with before/proposed diffs without writing to YouTube.
  - **Autonomous mode:** High-confidence updates ($\ge$ threshold, default 0.85) are automatically published and verified after passing cooldown and safety gates.
- **Global Emergency Pause:** Immediate toggle halts all autonomous writes across all cadences.
- **Learning & Performance Feedback Loop:** Evaluates published metadata changes at 7-day, 14-day, and 28-day observation windows against matched pre-change baselines, classifying outcomes into `improved`, `no_change`, or `declined`. Empirical results feed back into Gemini prompts for future runs.
- **Safe 1-Click Rollback:** Allows restoring original pre-optimization metadata (`before_metadata`) directly from the audit log with read-back verification.

## Scheduled execution

| Job | UTC schedule | India time | Purpose |
|---|---:|---:|---|
| Daily metadata snapshots | 02:15 | 07:45 | Maintain seven-day change ledger for owner uploads |
| Daily channel strategist | 02:30 | 08:00 | Emerging underperformance scan, mature 28d comparison |
| Weekly channel strategist | 03:30 (Sun) | 09:00 (Sun) | Cross-video pattern recognition, weekly upload optimization sweep |
| Monthly channel strategist | 04:30 (Day 5) | 10:00 (Day 5)| Strategic macro review, subscriber conversion compounding, catalogue refresh |
| Batch metadata worker | Every 5 min | Every 5 min | Process manually submitted batch instructions and save proposals |

The strategy workflow can also be started manually with GitHub Actions **workflow_dispatch**. Scheduled execution requires these GitHub Actions repository secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `YOUTUBE_STRATEGIST_STATE_SECRET`, and `GEMINI_API_KEY`. `GROQ_API_KEY` is optional for the daily strategist but preferred for metadata batches. The background-worker workflow sets the batch model via `GEMINI_MODEL` and `GEMINI_FALLBACK_MODELS`; the channel-strategy workflow remains on Gemini 3.1 Pro Preview.

## Owner controls in Streamlit

The **Channel Strategist** panel provides an enable/disable switch for low-risk automatic publishing, a daily auto-publish cap, a per-run metadata proposal cap, a major-change approval queue, and recent audit records. Changes are written only to the owner-scoped Supabase records. The default minor auto-publish cap is three per day; the seven-day per-video cooldown remains a separate safeguard.

The old external ChatGPT/monitor publisher workflow is not part of this Streamlit-only workflow. The legacy API bridge files may remain in the repository for compatibility, but the scheduled external publisher is disabled and no ChatGPT/monitor token is required for the Streamlit strategist.

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

# Daily strategist reasoning model
[gemini]
api_key = "AIza_YOUR_GEMINI_API_KEY"
model = "gemini-3.1-pro-preview"
fallback_models = ["gemini-3.8-flash"]
# Batch-only low-cost fallback; Groq is attempted first for metadata batches.
batch_model = "gemini-3.5-flash-lite"
batch_fallback_models = ["gemini-3.5-flash", "gemini-3.6-flash"]

# Groq is the primary provider for metadata batches.
[groq]
api_key = "gsk_YOUR_GROQ_API_KEY"
model = "openai/gpt-oss-120b"
fallback_models = []
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
