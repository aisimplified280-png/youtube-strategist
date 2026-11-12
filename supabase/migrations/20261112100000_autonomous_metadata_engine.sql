-- Forward-only migration: Autonomous YouTube Metadata Optimization Engine
-- Expands strategy runs with cadences (daily, weekly, monthly), adds full automation modes
-- (paused, shadow, autonomous), confidence tracking, multi-field publishing, and 7/14/28d evaluation loop.

alter table public.owner_strategy_settings
  add column if not exists automation_mode text not null default 'shadow'
    check (automation_mode in ('paused', 'shadow', 'autonomous')),
  add column if not exists emergency_pause boolean not null default false,
  add column if not exists min_confidence_auto_publish numeric not null default 0.85
    check (min_confidence_auto_publish between 0.50 and 1.00),
  add column if not exists max_weekly_publishes integer not null default 5
    check (max_weekly_publishes between 0 and 20),
  add column if not exists max_monthly_publishes integer not null default 10
    check (max_monthly_publishes between 0 and 50);

alter table public.channel_strategy_runs
  add column if not exists cadence text not null default 'daily'
    check (cadence in ('daily', 'weekly', 'monthly')),
  add column if not exists dedup_key text;

create unique index if not exists channel_strategy_runs_dedup_idx
  on public.channel_strategy_runs (owner_key, dedup_key)
  where dedup_key is not null;

-- Expand action statuses to include shadow publishing and rollback
alter table public.channel_strategy_actions
  drop constraint if exists channel_strategy_actions_status_check;

alter table public.channel_strategy_actions
  add constraint channel_strategy_actions_status_check
  check (status in (
    'pending_review', 'auto_publishing', 'auto_published',
    'approved_published', 'shadow_published', 'rolled_back',
    'rejected', 'skipped', 'failed', 'verification_unavailable', 'no_change'
  ));

alter table public.channel_strategy_actions
  add column if not exists cadence text not null default 'daily'
    check (cadence in ('daily', 'weekly', 'monthly')),
  add column if not exists confidence numeric
    check (confidence is null or (confidence >= 0.0 and confidence <= 1.0)),
  add column if not exists diagnosis text,
  add column if not exists rollback_of uuid references public.channel_strategy_actions(id);

create table if not exists public.strategy_action_evaluations (
  id uuid primary key default gen_random_uuid(),
  action_id uuid not null references public.channel_strategy_actions(id) on delete cascade,
  owner_key text not null,
  video_id text not null check (video_id ~ '^[A-Za-z0-9_-]{11}$'),
  observation_days integer not null check (observation_days in (7, 14, 28)),
  baseline_metrics jsonb not null default '{}'::jsonb,
  post_metrics jsonb not null default '{}'::jsonb,
  metric_deltas jsonb not null default '{}'::jsonb,
  outcome text not null default 'pending'
    check (outcome in ('improved', 'no_change', 'declined', 'inconclusive', 'pending')),
  evaluated_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create unique index if not exists strategy_action_evaluations_action_days_idx
  on public.strategy_action_evaluations (action_id, observation_days);

create index if not exists strategy_action_evaluations_owner_idx
  on public.strategy_action_evaluations (owner_key, evaluated_at desc);

create index if not exists strategy_action_evaluations_video_idx
  on public.strategy_action_evaluations (video_id, evaluated_at desc);

alter table public.strategy_action_evaluations enable row level security;

revoke all on public.strategy_action_evaluations from public, anon, authenticated;
grant select, insert, update, delete on public.strategy_action_evaluations to service_role;
