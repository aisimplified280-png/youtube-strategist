-- Durable chat history, background batch jobs, and encrypted worker credentials.
-- Access is server-side only. Never expose the service-role key to browser code.

create extension if not exists pgcrypto;

create table if not exists public.copilot_conversations (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  title text not null default 'New chat',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  archived_at timestamptz
);
create index if not exists copilot_conversations_owner_updated_idx
  on public.copilot_conversations (owner_key, updated_at desc);

create table if not exists public.copilot_messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.copilot_conversations(id) on delete cascade,
  owner_key text not null,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists copilot_messages_conversation_created_idx
  on public.copilot_messages (conversation_id, created_at);

create table if not exists public.batch_jobs (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  conversation_id uuid references public.copilot_conversations(id) on delete set null,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'review', 'completed', 'failed', 'cancelled')),
  source_type text not null default 'text'
    check (source_type in ('text', 'spreadsheet')),
  instruction_text text not null default '',
  input_payload jsonb not null default '{}'::jsonb,
  total_count integer not null default 0 check (total_count >= 0),
  processed_count integer not null default 0 check (processed_count >= 0),
  failed_count integer not null default 0 check (failed_count >= 0),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  worker_id text,
  lease_expires_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz not null default now()
);
create index if not exists batch_jobs_owner_created_idx
  on public.batch_jobs (owner_key, created_at desc);
create index if not exists batch_jobs_queue_idx
  on public.batch_jobs (status, lease_expires_at, created_at);

create table if not exists public.batch_job_rows (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references public.batch_jobs(id) on delete cascade,
  owner_key text not null,
  row_number integer not null,
  video_id text,
  video_title text,
  status text not null default 'queued'
    check (status in ('queued', 'running', 'ready', 'verified', 'no_change', 'invalid', 'failed', 'verification_unavailable')),
  input_payload jsonb not null default '{}'::jsonb,
  before_metadata jsonb not null default '{}'::jsonb,
  proposed_metadata jsonb not null default '{}'::jsonb,
  errors jsonb not null default '[]'::jsonb,
  approval_digest text,
  attempt_count integer not null default 0,
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (job_id, row_number)
);
create index if not exists batch_job_rows_job_status_idx
  on public.batch_job_rows (job_id, status, row_number);

create table if not exists public.owner_oauth_credentials (
  owner_key text not null,
  purpose text not null check (purpose in ('youtube', 'memory')),
  encrypted_payload text not null,
  revoked boolean not null default false,
  updated_at timestamptz not null default now(),
  primary key (owner_key, purpose)
);

alter table public.copilot_conversations enable row level security;
alter table public.copilot_messages enable row level security;
alter table public.batch_jobs enable row level security;
alter table public.batch_job_rows enable row level security;
alter table public.owner_oauth_credentials enable row level security;

revoke all on public.copilot_conversations, public.copilot_messages,
  public.batch_jobs, public.batch_job_rows, public.owner_oauth_credentials
  from anon, authenticated;
grant all on public.copilot_conversations, public.copilot_messages,
  public.batch_jobs, public.batch_job_rows, public.owner_oauth_credentials
  to service_role;

create or replace function public.claim_next_batch_job(
  p_worker_id text,
  p_lease_seconds integer default 240
)
returns setof public.batch_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  claimed_id uuid;
begin
  if p_worker_id is null or length(trim(p_worker_id)) = 0 then
    raise exception 'worker id is required';
  end if;

  with candidate as (
    select id
    from public.batch_jobs
    where status = 'queued'
       or (status = 'running' and lease_expires_at < now())
    and attempt_count < 5
    order by created_at
    for update skip locked
    limit 1
  )
  update public.batch_jobs as jobs
  set status = 'running',
      worker_id = p_worker_id,
      lease_expires_at = now() + make_interval(secs => greatest(30, least(p_lease_seconds, 900))),
      attempt_count = jobs.attempt_count + 1,
      started_at = coalesce(jobs.started_at, now()),
      updated_at = now()
  from candidate
  where jobs.id = candidate.id
  returning jobs.id into claimed_id;

  if claimed_id is not null then
    return query select * from public.batch_jobs where id = claimed_id;
  end if;
  return;
end;
$$;

revoke all on function public.claim_next_batch_job(text, integer) from public, anon, authenticated;
grant execute on function public.claim_next_batch_job(text, integer) to service_role;
