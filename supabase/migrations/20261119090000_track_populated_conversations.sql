-- Distinguish a genuinely active conversation from an empty chat draft.
-- Empty chats remain available to the current app session but are not part of history.

alter table public.copilot_conversations
  add column if not exists last_message_at timestamptz;

update public.copilot_conversations as conversations
set last_message_at = messages.last_message_at
from (
  select conversation_id, max(created_at) as last_message_at
  from public.copilot_messages
  group by conversation_id
) as messages
where conversations.id = messages.conversation_id
  and conversations.last_message_at is null;

create index if not exists copilot_conversations_owner_last_message_idx
  on public.copilot_conversations (owner_key, last_message_at desc)
  where archived_at is null and last_message_at is not null;
