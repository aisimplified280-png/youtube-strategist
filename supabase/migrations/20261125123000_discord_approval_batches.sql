-- Short-lived, owner-scoped snapshot of exactly the proposals shown in a Discord review.
-- The random batch UUID is used by the Approve All button; the worker still performs
-- the normal stale-metadata check and verified YouTube publish.
create table if not exists public.discord_approval_batches (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  proposal_refs jsonb not null default '[]'::jsonb,
  status text not null default 'pending'
    check (status in ('pending', 'completed', 'expired')),
  expires_at timestamptz not null,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists discord_approval_batches_owner_status_idx
  on public.discord_approval_batches (owner_key, status, expires_at);

alter table public.discord_approval_batches enable row level security;
revoke all on public.discord_approval_batches from public, anon, authenticated;
grant select, insert, update, delete on public.discord_approval_batches to service_role;
