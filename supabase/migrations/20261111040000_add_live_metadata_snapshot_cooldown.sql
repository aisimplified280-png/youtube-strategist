-- Track observed live YouTube metadata and enforce a conservative seven-day cooldown.
-- YouTube Data API does not expose a reliable metadata last-modified timestamp.
create table if not exists public.video_metadata_snapshots (
    owner_key text not null,
    video_id text not null check (video_id ~ '^[A-Za-z0-9_-]{11}$'),
    metadata_snapshot jsonb not null,
    metadata_hash text not null check (length(metadata_hash) = 32),
    first_observed_at timestamptz not null default now(),
    last_change_detected_at timestamptz not null default now(),
    last_checked_at timestamptz not null default now(),
    last_change_source text not null default 'baseline',
    change_count integer not null default 0 check (change_count >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (owner_key, video_id)
);

alter table public.video_metadata_snapshots enable row level security;
revoke all on public.video_metadata_snapshots from public, anon, authenticated;
grant all on public.video_metadata_snapshots to service_role;

create or replace function public.record_video_metadata_snapshot(
    p_owner_key text,
    p_video_id text,
    p_title text,
    p_description text,
    p_tags jsonb,
    p_source text default 'monitor'
)
returns table (
    video_id text,
    first_observed_at timestamptz,
    last_change_detected_at timestamptz,
    last_checked_at timestamptz,
    eligible_after timestamptz,
    eligible boolean,
    is_baseline boolean,
    changed_since_previous boolean,
    change_count integer
)
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
    v_snapshot jsonb;
    v_hash text;
    v_existing public.video_metadata_snapshots%rowtype;
    v_now timestamptz := clock_timestamp();
    v_is_baseline boolean := false;
    v_changed boolean := false;
begin
    if coalesce(trim(p_owner_key), '') = '' then
        raise exception 'owner_key is required';
    end if;
    if p_video_id is null or p_video_id !~ '^[A-Za-z0-9_-]{11}$' then
        raise exception 'A valid 11-character YouTube video ID is required';
    end if;
    if p_source not in ('monitor', 'copilot', 'worker', 'manual') then
        raise exception 'Unsupported metadata snapshot source';
    end if;
    if p_tags is not null and jsonb_typeof(p_tags) <> 'array' then
        raise exception 'tags must be a JSON array';
    end if;

    v_snapshot := jsonb_build_object(
        'title', coalesce(p_title, ''),
        'description', coalesce(p_description, ''),
        'tags', coalesce(p_tags, '[]'::jsonb)
    );
    v_hash := md5(v_snapshot::text);

    select * into v_existing
      from public.video_metadata_snapshots s
     where s.owner_key = p_owner_key and s.video_id = p_video_id
     for update;

    if not found then
        v_is_baseline := true;
        insert into public.video_metadata_snapshots (
            owner_key, video_id, metadata_snapshot, metadata_hash,
            first_observed_at, last_change_detected_at, last_checked_at,
            last_change_source, change_count, created_at, updated_at
        ) values (
            p_owner_key, p_video_id, v_snapshot, v_hash,
            v_now, v_now, v_now, 'baseline', 0, v_now, v_now
        )
        on conflict (owner_key, video_id) do nothing;

        -- A concurrent first observation may have inserted the row first.
        select * into v_existing
          from public.video_metadata_snapshots s
         where s.owner_key = p_owner_key and s.video_id = p_video_id
         for update;
        if v_existing.metadata_hash is distinct from v_hash then
            v_changed := true;
            update public.video_metadata_snapshots s
               set metadata_snapshot = v_snapshot,
                   metadata_hash = v_hash,
                   last_change_detected_at = v_now,
                   last_checked_at = v_now,
                   last_change_source = p_source,
                   change_count = s.change_count + 1,
                   updated_at = v_now
             where s.owner_key = p_owner_key and s.video_id = p_video_id
             returning * into v_existing;
        else
            update public.video_metadata_snapshots s
               set last_checked_at = v_now, updated_at = v_now
             where s.owner_key = p_owner_key and s.video_id = p_video_id
             returning * into v_existing;
        end if;
    else
        v_changed := v_existing.metadata_hash is distinct from v_hash;
        update public.video_metadata_snapshots s
           set metadata_snapshot = v_snapshot,
               metadata_hash = v_hash,
               last_change_detected_at = case when v_changed then v_now else s.last_change_detected_at end,
               last_checked_at = v_now,
               last_change_source = case when v_changed then p_source else s.last_change_source end,
               change_count = s.change_count + case when v_changed then 1 else 0 end,
               updated_at = v_now
         where s.owner_key = p_owner_key and s.video_id = p_video_id
         returning * into v_existing;
    end if;

    return query select
        v_existing.video_id,
        v_existing.first_observed_at,
        v_existing.last_change_detected_at,
        v_existing.last_checked_at,
        greatest(v_existing.first_observed_at, v_existing.last_change_detected_at) + interval '7 days',
        v_now >= greatest(v_existing.first_observed_at, v_existing.last_change_detected_at) + interval '7 days',
        v_is_baseline,
        v_changed,
        v_existing.change_count;
end;
$$;

revoke all on function public.record_video_metadata_snapshot(text, text, text, text, jsonb, text) from public, anon, authenticated;
grant execute on function public.record_video_metadata_snapshot(text, text, text, text, jsonb, text) to service_role;
