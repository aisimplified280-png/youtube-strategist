# Zero-billing operating policy

This project is designed to run on free plans. **It cannot be guaranteed free forever**: providers can change quotas, free-plan eligibility, or terms. The reliable zero-bill strategy is to keep every paid billing path disabled and accept that a free quota may temporarily stop a feature.

## Current deployment map

| Component | Current use | Zero-bill condition | What can stop working |
|---|---|---|---|
| GitHub Actions | Daily/weekly/monthly strategy jobs and a five-minute approval worker | Repository remains public and workflows use standard GitHub-hosted runners | Actions policy, abuse limits, or repository visibility changes |
| Streamlit Community Cloud | Owner-only dashboard | Stay on Community Cloud free deployment | App sleeps after 12 hours without traffic and can be throttled at resource limits |
| Supabase | Durable queue, approval state, OAuth grants, audit trail | Keep the organization on Free and usage within free quotas | Free project can pause after one week of inactivity; database/egress/storage caps apply |
| Gemini Developer API | Strategy analysis and fallback reasoning | Keep the API project on the Free tier; do not link Cloud Billing or buy API credits | Model availability and request/token limits can change; requests can return quota errors |
| Groq API | Optional first-choice batch metadata generation | Use only the free developer tier; do not add a paid billing method | Rate limits/model availability can change |
| YouTube APIs | Channel analytics, video metadata reads/writes after approval | Use the configured Google Cloud project quota; no paid YouTube product is used | API quota exhaustion blocks reads/writes until quota resets |
| Discord | Slash commands, approval cards and notifications | Standard Discord application/webhook usage | Platform rate limits and API policy can change |

## Non-negotiable spend safeguards

1. **Do not attach a Google Cloud Billing account to the Gemini API project.** A Google AI Pro consumer subscription is not the same as Gemini Developer API billing. Keep the key on the API Free tier. If the quota is exhausted, the strategy should fail closed or use a free fallback; it must not silently switch to a paid model.
2. **Do not upgrade the Supabase organization.** Keep the project on Free and watch database size, egress, and storage. Do not enable paid add-ons or create paid branches.
3. **Keep GitHub Actions on a public repository and standard runners.** Never move to a larger runner. Public source code is visible to everyone, so all credentials must remain in GitHub Secrets or Supabase server-side secrets.
4. **Keep Groq optional.** A missing or exhausted Groq free quota must fall back to the configured free Gemini API path or leave a proposal ungenerated. Never introduce a paid provider automatically.
5. **No paid storage, paid monitoring, paid queues, or paid media APIs are part of the required path.** Keep notifications best-effort so notification failures never trigger repeated model or YouTube calls.

## Current operational guardrails

- Metadata writes remain owner-approved or pass the existing conservative autonomous gate; Discord is only an approval interface.
- YouTube publishing remains in the GitHub background worker, which compares live metadata to the reviewed snapshot and verifies read-back.
- The Discord review batch is short-lived (15 minutes), owner-scoped, digest-bound, and pruned after its retention period.
- The channel strategist is scheduled daily, weekly, and monthly; the five-minute worker only processes queued work and exits after one idle poll.
- The application has no mechanism that can promise a zero-cost service if an administrator later links a billing account, upgrades a provider plan, or opts into paid models. Provider billing settings remain the final hard stop.

## Free-plan caveats checked on 25 November 2026

- GitHub documents standard GitHub-hosted runners as free and unlimited for public repositories.
- Streamlit Community Cloud is free, but idle apps hibernate after 12 hours; resource caps can cause throttling.
- Supabase Free currently includes 500 MB database, 5 GB uncached egress plus 5 GB cached egress, and 1 GB file storage; free projects can pause after a week of inactivity.
- Gemini Developer API offers a limited Free tier for selected models. Consumer Google AI subscriptions do not automatically include API billing credits.

These are provider-controlled policies, not promises of perpetual free service. Re-check their pricing pages before making plan changes.
