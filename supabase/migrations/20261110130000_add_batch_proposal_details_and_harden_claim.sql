-- Preserve all generated recommendation material for durable review.
-- Correct the queue retry predicate in the repository migration history as well.

alter table public.batch_job_rows
  add column if not exists proposal_details jsonb not null default '{}'::jsonb;

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
    where (
      status = 'queued'
      or (status = 'running' and lease_expires_at < now())
    )
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
