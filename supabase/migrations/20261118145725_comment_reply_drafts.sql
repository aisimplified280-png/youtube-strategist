-- Persist creator-approved YouTube comment reply drafts and publication audit.
-- Public and authenticated roles receive no access; the verified owner app uses service_role server-side.

create table if not exists public.comment_reply_drafts (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  channel_id text not null,
  video_id text check (video_id is null or video_id ~ '^[A-Za-z0-9_-]{11}$'),
  video_title text not null default '',
  thread_id text not null,
  parent_comment_id text not null,
  comment_text_snapshot text not null,
  comment_author_display_name text not null default '',
  can_reply boolean not null default false,
  draft_text text not null check (length(trim(draft_text)) between 1 and 10000),
  approval_digest text not null,
  status text not null default 'pending_review'
    check (status in (
      'pending_review', 'approved', 'publishing', 'published',
      'failed', 'verification_unavailable', 'stale', 'rejected'
    )),
  approved_by text,
  approved_at timestamptz,
  published_comment_id text,
  result jsonb not null default '{}'::jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz
);

create index if not exists comment_reply_drafts_owner_status_idx
  on public.comment_reply_drafts (owner_key, status, updated_at desc);
create index if not exists comment_reply_drafts_owner_thread_idx
  on public.comment_reply_drafts (owner_key, thread_id, created_at desc);
create unique index if not exists comment_reply_drafts_one_active_per_parent_idx
  on public.comment_reply_drafts (owner_key, parent_comment_id)
  where status in ('pending_review', 'approved', 'publishing');

alter table public.comment_reply_drafts enable row level security;
revoke all on public.comment_reply_drafts from public, anon, authenticated;
grant select, insert, update, delete on public.comment_reply_drafts to service_role;
