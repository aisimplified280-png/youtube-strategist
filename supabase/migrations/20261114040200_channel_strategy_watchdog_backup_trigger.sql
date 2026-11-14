-- Supabase-backed watchdog for the daily Channel Strategist.
-- GitHub remains the execution host; this Postgres cron only dispatches the workflow
-- when no daily run has been recorded for the current IST date.

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net;

grant usage on schema cron to postgres;
grant all privileges on all tables in schema cron to postgres;

alter table public.channel_strategy_runs
  drop constraint if exists channel_strategy_runs_trigger_source_check;

alter table public.channel_strategy_runs
  add constraint channel_strategy_runs_trigger_source_check
  check (trigger_source in ('schedule', 'manual', 'workflow_dispatch', 'supabase_watchdog'));

create table if not exists public.channel_strategy_watchdog_events (
  id bigserial primary key,
  run_date date not null,
  status text not null check (status in (
    'dispatch_submitted',
    'dispatch_accepted',
    'dispatch_failed',
    'waiting_for_response',
    'waiting_for_run',
    'run_detected',
    'secret_missing'
  )),
  detail text not null default '',
  request_id bigint,
  http_status_code integer,
  response_excerpt text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists channel_strategy_watchdog_events_run_date_idx
  on public.channel_strategy_watchdog_events (run_date, created_at desc);

alter table public.channel_strategy_watchdog_events enable row level security;
revoke all on public.channel_strategy_watchdog_events from public, anon, authenticated;
grant select, insert, update, delete on public.channel_strategy_watchdog_events to service_role;
grant usage, select on sequence public.channel_strategy_watchdog_events_id_seq to service_role;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;
grant usage on schema private to postgres;

create or replace function private.channel_strategy_watchdog_tick()
returns void
language plpgsql
security definer
set search_path = ''
as $function$
declare
  v_today date := (pg_catalog.now() at time zone 'Asia/Kolkata')::date;
  v_local_time time := (pg_catalog.now() at time zone 'Asia/Kolkata')::time;
  v_day_start timestamptz;
  v_day_end timestamptz;
  v_run_status text;
  v_last_status text;
  v_last_created timestamptz;
  v_attempts integer;
  v_token text;
  v_request_id bigint;
begin
  if v_local_time < time '08:15' or v_local_time > time '09:00' then
    return;
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtext('ai-simplified-lab-daily-strategist-watchdog')
  );

  v_day_start := v_today::timestamp at time zone 'Asia/Kolkata';
  v_day_end := (v_today + 1)::timestamp at time zone 'Asia/Kolkata';

  update public.channel_strategy_watchdog_events e
     set status = case
           when r.status_code between 200 and 299 then 'dispatch_accepted'
           else 'dispatch_failed'
         end,
         http_status_code = r.status_code,
         response_excerpt = pg_catalog.left(
           pg_catalog.coalesce(r.content, r.error_msg, ''), 700
         ),
         detail = case
           when r.status_code between 200 and 299 then 'GitHub accepted the workflow dispatch request.'
           else 'GitHub dispatch did not succeed; see the HTTP status and response excerpt.'
         end,
         updated_at = pg_catalog.now()
    from net._http_response r
   where e.request_id = r.id
     and e.run_date = v_today
     and e.status = 'dispatch_submitted';

  select r.status
    into v_run_status
    from public.channel_strategy_runs r
   where r.channel_id = 'UCS6Sb5rfaEnWTVPGyQlnHFQ'
     and r.cadence = 'daily'
     and r.started_at >= v_day_start
     and r.started_at < v_day_end
   order by r.started_at desc
   limit 1;

  if v_run_status is not null then
    insert into public.channel_strategy_watchdog_events
      (run_date, status, detail)
    values
      (v_today, 'run_detected',
       'A daily strategist run is already recorded for today (status: ' || v_run_status || '). Backup dispatch skipped.');
    return;
  end if;

  select count(*)
    into v_attempts
    from public.channel_strategy_watchdog_events e
   where e.run_date = v_today
     and e.request_id is not null;

  select e.status, e.created_at
    into v_last_status, v_last_created
    from public.channel_strategy_watchdog_events e
   where e.run_date = v_today
     and e.request_id is not null
   order by e.created_at desc
   limit 1;

  if v_last_status = 'dispatch_submitted'
     and v_last_created > pg_catalog.now() - interval '8 minutes' then
    insert into public.channel_strategy_watchdog_events (run_date, status, detail)
    values (v_today, 'waiting_for_response', 'Waiting for the GitHub API dispatch response.');
    return;
  end if;

  if v_last_status = 'dispatch_accepted'
     and v_last_created > pg_catalog.now() - interval '20 minutes' then
    insert into public.channel_strategy_watchdog_events (run_date, status, detail)
    values (v_today, 'waiting_for_run', 'GitHub accepted the dispatch; waiting for the worker to record its run in Supabase.');
    return;
  end if;

  if v_last_status = 'dispatch_failed'
     and v_last_created > pg_catalog.now() - interval '5 minutes' then
    return;
  end if;

  if v_attempts >= 3 then
    insert into public.channel_strategy_watchdog_events (run_date, status, detail)
    values (v_today, 'waiting_for_run', 'Backup dispatch retry limit reached (3 attempts). Manual investigation is required.');
    return;
  end if;

  select s.decrypted_secret
    into v_token
    from vault.decrypted_secrets s
   where s.name = 'github_strategist_watchdog_token'
   limit 1;

  if v_token is null or pg_catalog.length(v_token) < 20 then
    if not exists (
      select 1
        from public.channel_strategy_watchdog_events e
       where e.run_date = v_today
         and e.status = 'secret_missing'
    ) then
      insert into public.channel_strategy_watchdog_events (run_date, status, detail)
      values (v_today, 'secret_missing',
        'Vault secret github_strategist_watchdog_token is missing. Add a GitHub fine-grained token with Actions: read/write permission for this repository.');
    end if;
    return;
  end if;

  v_request_id := net.http_post(
    url := 'https://api.github.com/repos/aisimplified280-png/youtube-strategist/actions/workflows/channel-strategy-agent.yml/dispatches',
    body := pg_catalog.jsonb_build_object(
      'ref', 'main',
      'inputs', pg_catalog.jsonb_build_object('trigger_source', 'supabase_watchdog')
    ),
    headers := pg_catalog.jsonb_build_object(
      'Accept', 'application/vnd.github+json',
      'Authorization', 'Bearer ' || v_token,
      'Content-Type', 'application/json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'AI-Simplified-Lab-Strategist-Watchdog'
    ),
    timeout_milliseconds := 10000
  );

  insert into public.channel_strategy_watchdog_events
    (run_date, status, detail, request_id)
  values
    (v_today, 'dispatch_submitted',
     'Supabase watchdog submitted a backup workflow_dispatch because no daily run was recorded.',
     v_request_id);

exception
  when others then
    insert into public.channel_strategy_watchdog_events
      (run_date, status, detail)
    values
      (v_today, 'dispatch_failed',
       'Watchdog error ' || sqlstate || ': request could not be queued. Check Supabase Cron and pg_net logs.');
end;
$function$;

revoke all on function private.channel_strategy_watchdog_tick() from public, anon, authenticated;
grant execute on function private.channel_strategy_watchdog_tick() to postgres;

select cron.schedule(
  'ai-simplified-lab-daily-strategist-watchdog',
  '*/5 2-3 * * *',
  $$select private.channel_strategy_watchdog_tick();$$
);
