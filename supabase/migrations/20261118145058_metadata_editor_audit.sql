-- Owner-scoped audit and approval history for the full metadata editor.
-- Access is server-side only through the service_role; browser roles receive no access.

create table if not exists public.metadata_editor_audits (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  video_id text not null check (video_id ~ '^[A-Za-z0-9_-]{11}$'),
  video_title text not null default '',
  before_metadata jsonb not null default '{}'::jsonb,
  proposed_metadata jsonb not null default '{}'::jsonb,
  updated_parts text[] not null default '{}'::text[],
  approval_digest text not null,
  status text not null default 'pending_review'
    check (status in ('pending_review', 'approved_published', 'rejected', 'failed', 'stale', 'verification_unavailable')),
  approved_by text,
  approved_at timestamptz,
  after_metadata jsonb,
  result jsonb not null default '{}'::jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz
);

create index if not exists metadata_editor_audits_owner_updated_idx
  on public.metadata_editor_audits (owner_key, updated_at desc);
create index if not exists metadata_editor_audits_owner_video_idx
  on public.metadata_editor_audits (owner_key, video_id, created_at desc);

alter table public.metadata_editor_audits enable row level security;
revoke all on public.metadata_editor_audits from public, anon, authenticated;
grant select, insert, update, delete on public.metadata_editor_audits to service_role;
