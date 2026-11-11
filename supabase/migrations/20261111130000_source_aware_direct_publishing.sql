-- Source-aware trusted publishing queue.
-- ChatGPT requests require an explicit user publish command. Monitor requests
-- require the owner's autonomous monitor policy and remain rate-limited.
alter table public.assistant_publish_requests
  add column if not exists request_source text not null default 'chatgpt',
  add column if not exists authorization_mode text not null default 'explicit_user_command',
  add column if not exists authorized_by text,
  add column if not exists authorized_at timestamptz,
  add column if not exists idempotency_key text;

update public.assistant_publish_requests
   set authorized_by = coalesce(nullif(approved_by, ''), 'legacy_explicit_approval'),
       authorized_at = coalesce(approved_at, created_at)
 where authorized_by is null or authorized_at is null;

alter table public.assistant_publish_requests
  alter column approved_by drop not null,
  alter column approved_at drop not null,
  alter column authorized_by set not null,
  alter column authorized_at set not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'assistant_publish_requests_source_check'
       and conrelid = 'public.assistant_publish_requests'::regclass
  ) then
    alter table public.assistant_publish_requests
      add constraint assistant_publish_requests_source_check
      check (request_source in ('chatgpt', 'monitor'));
  end if;
  if not exists (
    select 1 from pg_constraint
     where conname = 'assistant_publish_requests_authorization_check'
       and conrelid = 'public.assistant_publish_requests'::regclass
  ) then
    alter table public.assistant_publish_requests
      add constraint assistant_publish_requests_authorization_check
      check (
        (request_source = 'chatgpt' and authorization_mode = 'explicit_user_command'
          and approved_by is not null and approved_at is not null)
        or
        (request_source = 'monitor' and authorization_mode = 'monitor_policy')
      );
  end if;
end $$;

create unique index if not exists assistant_publish_requests_idempotency_idx
  on public.assistant_publish_requests (owner_key, idempotency_key)
  where idempotency_key is not null;

create table if not exists public.owner_publish_policies (
  owner_key text primary key,
  monitor_auto_publish_enabled boolean not null default true,
  max_daily_publishes integer not null default 5
    check (max_daily_publishes between 1 and 25),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.owner_publish_policies enable row level security;
revoke all on public.owner_publish_policies from public, anon, authenticated;
grant all on public.owner_publish_policies to service_role;
