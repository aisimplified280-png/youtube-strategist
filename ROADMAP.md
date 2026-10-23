# Roadmap: YouTube Strategist

This roadmap documents intentional scope boundaries, future enhancements, and items explicitly not built in the core Streamlit Community Cloud app.

---

## Intentionally Not Built (Scope Boundaries)

1. **Durable Database for Full Audit & Conversation History:**
   - *Rationale:* The app runs on Streamlit Community Cloud using server-side session state and an encrypted temporary SQLite vault. Scans and verified metadata experiments can be persisted to Google Sheets via `drive.file` scope ("Strategist Memory"). Full long-term conversation logs and persistent audit DB require an external managed database (e.g., Supabase, Neon, or Cloud SQL) with strict user access controls.
2. **Scheduled Alerts & Autonomous Background Scanning:**
   - *Rationale:* Streamlit apps execute reactively when an active session is open in a browser tab. Background daemon schedules, weekly email digests, or cron scans require an external worker service (e.g., GitHub Actions, Cloud Run job, or AWS Lambda) with separate OAuth service credentials.
3. **Slack / Discord Integration:**
   - *Rationale:* Kept strictly private and owner-bound within the Streamlit workspace to maintain tight authorization and avoid leaking private reach telemetry into team channels without explicit channel permissions.
4. **Direct Microsoft OneDrive / Excel Online Browsing:**
   - *Rationale:* Direct cloud drive access requires a separate Microsoft Entra ID OAuth flow and tenant permissions. Creators can easily export or download their `.xlsx` workbook from OneDrive and upload it via drag-and-drop into the in-memory Studio Analyzer.
5. **Direct Video File Uploads & Automatic Publishing:**
   - *Rationale:* The app is strictly an optimization co-pilot for existing uploads and packaging metadata. It never publishes unreviewed videos or alters visibility/audience settings.
6. **Automatic Unapproved Metadata Writes:**
   - *Rationale:* Non-negotiable safety invariant: No changes are ever applied to YouTube without explicit user approval of the exact proposal, fresh stale-check refetch, and post-write verification.

---

## Future Candidate Enhancements (Under Evaluation)

- [ ] Multi-title variant voting: Generate 3 distinct packaging angles with direct 1-click promotion into the active proposal.
- [ ] Export analysis bundle download: One-click export of cleaned per-video comparison metrics into CSV.
- [ ] Direct thumbnail concept preview: Visual preview of thumbnail aspect ratio and mobile feed crop mockups alongside proposed titles.
- [ ] Webhook triggers: Optional outgoing webhook dispatched only after an approved metadata change is verified on YouTube.
