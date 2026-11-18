-- Keep empty draft chats out of the saved-history list while retaining the active draft.
-- Backfill existing conversations from their actual persisted message rows.

alter table public.copilot_conversations
  add column if not exists last_message_at timestamptz;

update public.copilot_conversations c
set last_message_at = (
  select max(m.created_at)
  from public.copilot_messages m
  where m.conversation_id = c.id
    and m.owner_key = c.owner_key
)
where c.last_message_at is null;

create index if not exists copilot_conversations_owner_last_message_idx
  on public.copilot_conversations (owner_key, last_message_at desc)
  where archived_at is null and last_message_at is not null;
