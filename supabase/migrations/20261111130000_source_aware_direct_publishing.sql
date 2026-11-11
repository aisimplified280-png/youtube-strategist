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


-- Atomically enforce the monitor policy, idempotency, and per-day request cap.
create or replace function public.enqueue_monitor_publish_request(
  p_owner_key text,
  p_video_id text,
  p_before_metadata jsonb,
  p_proposed_metadata jsonb,
  p_authorized_by text,
  p_idempotency_key text default null
)
returns setof public.assistant_publish_requests
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_enabled boolean;
  v_cap integer;
  v_count integer;
  v_existing public.assistant_publish_requests;
  v_created public.assistant_publish_requests;
begin
  if coalesce(trim(p_owner_key), '') = '' then
    raise exception 'verified owner key is required';
  end if;
  if p_video_id is null or p_video_id !~ '^[A-Za-z0-9_-]{11}$' then
    raise exception 'valid 11-character YouTube video ID is required';
  end if;
  if jsonb_typeof(p_before_metadata) <> 'object'
     or jsonb_typeof(p_proposed_metadata) <> 'object'
     or p_proposed_metadata = '{}'::jsonb then
    raise exception 'before and proposed metadata must be non-empty objects';
  end if;
  if coalesce(trim(p_authorized_by), '') = '' then
    raise exception 'monitor authorization identity is required';
  end if;
  if p_proposed_metadata - array['title','description','tags'] <> '{}'::jsonb then
    raise exception 'only title, description, and tags may be changed';
  end if;

  if p_idempotency_key is not null then
    select * into v_existing
      from public.assistant_publish_requests
     where owner_key = p_owner_key and idempotency_key = p_idempotency_key
     limit 1;
    if found then
      return next v_existing;
      return;
    end if;
  end if;

  select monitor_auto_publish_enabled, max_daily_publishes
    into v_enabled, v_cap
    from public.owner_publish_policies
   where owner_key = p_owner_key;
  if not found then
    v_enabled := true;
    v_cap := 5;
  end if;
  if v_enabled is distinct from true then
    raise exception 'autonomous monitor publishing is disabled for this owner';
  end if;
  v_cap := greatest(1, least(coalesce(v_cap, 5), 25));

  select count(*) into v_count
    from public.assistant_publish_requests
   where owner_key = p_owner_key
     and request_source = 'monitor'
     and created_at >= date_trunc('day', now() at time zone 'UTC') at time zone 'UTC';
  if v_count >= v_cap then
    raise exception 'monitor daily publishing limit reached (% per UTC day)', v_cap;
  end if;

  insert into public.assistant_publish_requests (
    owner_key, video_id, before_metadata, proposed_metadata,
    request_source, authorization_mode, authorized_by, authorized_at,
    idempotency_key, status
  ) values (
    p_owner_key, p_video_id, p_before_metadata, p_proposed_metadata,
    'monitor', 'monitor_policy', left(p_authorized_by, 160), now(),
    p_idempotency_key, 'queued'
  )
  returning * into v_created;

  return next v_created;
end;
$$;

revoke all on function public.enqueue_monitor_publish_request(text,text,jsonb,jsonb,text,text)
  from public, anon, authenticated;
grant execute on function public.enqueue_monitor_publish_request(text,text,jsonb,jsonb,text,text)
  to service_role;
