-- Persistent owner-scoped autonomous channel strategy runs and metadata action audit trail.
-- All reads/writes are server-side through the service_role; no browser role receives access.

create extension if not exists pgcrypto;

create table if not exists public.owner_strategy_settings (
  owner_key text primary key,
  auto_minor_enabled boolean not null default true,
  max_daily_minor_publishes integer not null default 3
    check (max_daily_minor_publishes between 0 and 10),
  max_metadata_reviews_per_run integer not null default 3
    check (max_metadata_reviews_per_run between 0 and 10),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.channel_strategy_runs (
  id uuid primary key default gen_random_uuid(),
  owner_key text not null,
  channel_id text not null,
  trigger_source text not null default 'schedule'
    check (trigger_source in ('schedule', 'manual', 'workflow_dispatch')),
  status text not null default 'running'
    check (status in ('running', 'completed', 'partial', 'failed')),
  metrics_period jsonb not null default '{}'::jsonb,
  metrics_snapshot jsonb not null default '{}'::jsonb,
  strategy_report jsonb not null default '{}'::jsonb,
  result jsonb not null default '{}'::jsonb,
  model_name text,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists channel_strategy_runs_owner_created_idx
  on public.channel_strategy_runs (owner_key, created_at desc);

create table if not exists public.channel_strategy_actions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.channel_strategy_runs(id) on delete cascade,
  owner_key text not null,
  video_id text not null check (video_id ~ '^[A-Za-z0-9_-]{11}$'),
  video_title text not null default '',
  priority integer not null default 0,
  action_type text not null default 'metadata_optimization'
    check (action_type in ('metadata_optimization')),
  classification text not null
    check (classification in ('minor', 'major', 'none')),
  status text not null
    check (status in (
      'pending_review', 'auto_publishing', 'auto_published',
      'approved_published', 'rejected', 'skipped', 'failed',
      'verification_unavailable', 'no_change'
    )),
  reason text not null default '',
  evidence jsonb not null default '{}'::jsonb,
  before_metadata jsonb not null default '{}'::jsonb,
  proposed_metadata jsonb not null default '{}'::jsonb,
  after_metadata jsonb,
  approval_digest text,
  model_name text,
  audit_log jsonb,
  result jsonb not null default '{}'::jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  executed_at timestamptz
);

create index if not exists channel_strategy_actions_owner_created_idx
  on public.channel_strategy_actions (owner_key, created_at desc);
create index if not exists channel_strategy_actions_owner_status_idx
  on public.channel_strategy_actions (owner_key, status, created_at desc);
create index if not exists channel_strategy_actions_run_idx
  on public.channel_strategy_actions (run_id, priority desc);

alter table public.owner_strategy_settings enable row level security;
alter table public.channel_strategy_runs enable row level security;
alter table public.channel_strategy_actions enable row level security;

revoke all on public.owner_strategy_settings, public.channel_strategy_runs,
  public.channel_strategy_actions from public, anon, authenticated;
grant select, insert, update, delete on public.owner_strategy_settings,
  public.channel_strategy_runs, public.channel_strategy_actions to service_role;
