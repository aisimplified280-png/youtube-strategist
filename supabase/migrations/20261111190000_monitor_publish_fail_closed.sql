-- Fail closed for autonomous monitor publishing and harden daily-attempt accounting.
-- Existing owner policies are switched off as a safety measure; an owner must
-- explicitly opt in again after reviewing the monitor integration.
alter table public.owner_publish_policies
  alter column monitor_auto_publish_enabled set default false;

update public.owner_publish_policies
   set monitor_auto_publish_enabled = false,
       updated_at = now();

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

  perform pg_advisory_xact_lock(hashtextextended(p_owner_key, 0));

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
    v_enabled := false;
    v_cap := 5;
  end if;
  if v_enabled is distinct from true then
    raise exception 'autonomous monitor publishing is disabled for this owner';
  end if;
  v_cap := greatest(1, least(coalesce(v_cap, 5), 25));

  -- Every enqueued monitor request consumes the UTC-day budget regardless of
  -- terminal status. Failed or verification-uncertain attempts are not refunded.
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
