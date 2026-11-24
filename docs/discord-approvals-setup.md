# Discord metadata approvals

This integration adds an owner-only `/approvals` command to the existing Discord application. The existing `DISCORD_WEBHOOK_URL` notifications are unchanged.

## Flow

1. Discord sends an interaction to the Supabase Edge Function.
2. The function verifies Discord's Ed25519 signature and rejects timestamps outside a five-minute window.
3. Only the exact configured Discord user ID is authorised.
4. `/approvals` lists up to five pending metadata-editor proposals, with video links and proposal-bound Approve & publish / Reject buttons.
5. A button can transition only its exact pending audit row, matching the digest embedded in the button. A conditional update makes duplicate/concurrent clicks no-ops.
6. Approve sets the existing audit status to `approved`. The existing GitHub Actions background worker (scheduled every five minutes) claims Discord-sourced approved rows, rechecks the exact proposal against live YouTube metadata, writes it using the already-connected owner OAuth grant, and verifies the read-back. Reject never writes to YouTube.

The Edge Function does not hold YouTube OAuth credentials and cannot publish directly. The publishing worker remains the only component that performs YouTube writes.

## Required Edge Function secrets (configure only after code is merged)

- `DISCORD_APPLICATION_PUBLIC_KEY`: Discord Developer Portal → General Information → Public Key.
- `DISCORD_OWNER_USER_ID`: your personal numeric Discord user ID (not the bot ID).
- `SUPABASE_URL`: existing project URL.
- `SUPABASE_SERVICE_ROLE_KEY`: service-role key. Keep it server-side only.

The bot token is used only by the GitHub Actions command-registration workflow. Store it as the GitHub Actions secret `DISCORD_BOT_TOKEN`; do not add it to Supabase Edge Function secrets. Never put any credentials in Git.

## Deploy and configure

1. Deploy `supabase/functions/discord-interactions/index.ts` as `discord-interactions`.
2. Disable Supabase JWT verification for this function. Discord signs the raw request body; Supabase JWT authentication is not used.
3. Set the secrets listed above in Supabase Edge Function secrets.
4. In Discord Developer Portal → General Information, set Interactions Endpoint URL to:
   `https://<project-ref>.supabase.co/functions/v1/discord-interactions`
5. Add these two repository secrets in GitHub → Settings → Secrets and variables → Actions:
   - `DISCORD_APPLICATION_ID`: Discord Developer Portal → General Information → Application ID.
   - `DISCORD_BOT_TOKEN`: Discord Developer Portal → Bot → Token.
   Never paste either value into chat or commit it to Git.
6. Open GitHub → Actions → **Register Discord slash commands** → **Run workflow**. The workflow registers the global `/approvals` command using those secrets; no local PowerShell command is needed. Global command propagation can take a short while.
7. Test `/approvals` with one pending metadata proposal before normal use.

## Security / behaviour

- The function resolves the only active YouTube owner credential from the server-side OAuth table and fails closed if it finds zero or multiple active owners. It verifies the signature against the exact raw request body before parsing it.
- Old signed requests are rejected; the pending-to-approved/rejected status transition prevents repeat decisions, and the interaction ID is retained in the audit result.
- The button includes the proposal digest prefix and the database update checks the full stored digest.
- The worker claims only rows with both `status=approved` and `result.source=discord`; it then rechecks live metadata and the full proposal digest before writing. Existing Streamlit approval execution is not intercepted.
- No comment-reply actions are exposed. Comment replies remain manual approval only.
- The existing webhook notification path is not modified.
