-- Durable, explicitly approved manual metadata publishing requests from the assistant.
-- This queue is independent of the monitor's seven-day eligibility ledger.
create table if not exists public.assistant_publish_requests (
    id uuid primary key default gen_random_uuid(),
    owner_key text not null,
    video_id text not null check (video_id ~ '^[A-Za-z0-9_-]{11}$'),
    before_metadata jsonb not null,
    proposed_metadata jsonb not null,
    approved_by text not null check (length(trim(approved_by)) > 0),
    approved_at timestamptz not null,
    approval_digest text,
    status text not null default 'queued'
      check (status in ('queued','processing','verified','failed','stale','verification_unavailable')),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    worker_id text,
    lease_expires_at timestamptz,
    result jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists assistant_publish_requests_claim_idx
  on public.assistant_publish_requests (status, created_at);

alter table public.assistant_publish_requests enable row level security;
revoke all on public.assistant_publish_requests from public, anon, authenticated;
grant all on public.assistant_publish_requests to service_role;

create or replace function public.claim_next_assistant_publish_request(
    p_worker_id text,
    p_lease_seconds integer default 300
)
returns setof public.assistant_publish_requests
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_id uuid;
begin
    if coalesce(trim(p_worker_id), '') = '' then
        raise exception 'worker id is required';
    end if;

    update public.assistant_publish_requests
       set status = 'failed',
           result = jsonb_build_object('message', 'Publishing lease expired after the maximum number of attempts.'),
           worker_id = null,
           lease_expires_at = null,
           finished_at = now(),
           updated_at = now()
     where status = 'processing'
       and lease_expires_at < now()
       and attempt_count >= 3;

    with candidate as (
        select id
          from public.assistant_publish_requests
         where status = 'queued'
            or (status = 'processing' and lease_expires_at < now() and attempt_count < 3)
         order by created_at
         for update skip locked
         limit 1
    )
    update public.assistant_publish_requests r
       set status = 'processing',
           worker_id = p_worker_id,
           lease_expires_at = now() + make_interval(secs => greatest(30, least(p_lease_seconds, 900))),
           attempt_count = r.attempt_count + 1,
           started_at = coalesce(r.started_at, now()),
           updated_at = now()
      from candidate
     where r.id = candidate.id
     returning r.id into v_id;

    if v_id is not null then
        return query select * from public.assistant_publish_requests where id = v_id;
    end if;
    return;
end;
$$;

revoke all on function public.claim_next_assistant_publish_request(text, integer) from public, anon, authenticated;
grant execute on function public.claim_next_assistant_publish_request(text, integer) to service_role;
