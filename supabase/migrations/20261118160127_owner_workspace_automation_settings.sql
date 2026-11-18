-- Per-workspace automation preferences with conservative shadow defaults.
-- Autonomous publishing remains opt-in and is separate for metadata and comments.

create table if not exists public.owner_workspace_automation_settings (
  owner_key text primary key,
  metadata_mode text not null default 'shadow'
    check (metadata_mode in ('paused', 'shadow', 'review', 'autonomous')),
  comments_mode text not null default 'shadow'
    check (comments_mode in ('paused', 'shadow', 'review', 'autonomous')),
  metadata_daily_cap integer not null default 1
    check (metadata_daily_cap between 0 and 5),
  comments_daily_cap integer not null default 3
    check (comments_daily_cap between 0 and 10),
  minimum_confidence numeric not null default 0.92
    check (minimum_confidence between 0.85 and 1.00),
  emergency_pause boolean not null default false,
  updated_at timestamptz not null default now()
);

alter table public.owner_workspace_automation_settings enable row level security;
revoke all on public.owner_workspace_automation_settings from public, anon, authenticated;
grant select, insert, update, delete on public.owner_workspace_automation_settings to service_role;
