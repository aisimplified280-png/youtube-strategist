-- Allow the Discord interaction to explicitly approve a strategy action.
-- The background worker claims this state and performs the existing stale-checked,
-- read-back-verified YouTube update. No client or webhook writes to YouTube.
alter table public.channel_strategy_actions
  drop constraint if exists channel_strategy_actions_status_check;

alter table public.channel_strategy_actions
  add constraint channel_strategy_actions_status_check
  check (status in (
    'pending_review', 'approved', 'auto_publishing', 'auto_published',
    'approved_published', 'rejected', 'skipped', 'failed',
    'verification_unavailable', 'no_change'
  ));
