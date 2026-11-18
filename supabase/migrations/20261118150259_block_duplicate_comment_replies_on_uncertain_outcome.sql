-- An uncertain insert outcome must block a second reply to the same parent.
-- The owner must verify the first reply in YouTube before another draft is allowed.

drop index if exists public.comment_reply_drafts_one_active_per_parent_idx;
create unique index comment_reply_drafts_one_active_per_parent_idx
  on public.comment_reply_drafts (owner_key, parent_comment_id)
  where status in ('pending_review', 'approved', 'publishing', 'verification_unavailable');
