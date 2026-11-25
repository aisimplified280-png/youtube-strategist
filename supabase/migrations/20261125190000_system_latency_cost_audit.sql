-- Target the two foreign-key lookups identified by Supabase performance advisors.
-- Keep these simple indexes available to the planner for joins and FK checks.
create index if not exists batch_jobs_conversation_id_idx
  on public.batch_jobs (conversation_id);

create index if not exists channel_strategy_actions_rollback_of_idx
  on public.channel_strategy_actions (rollback_of);
