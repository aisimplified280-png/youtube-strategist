create or replace function public.touch_copilot_conversation_last_message()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  update public.copilot_conversations
  set last_message_at = greatest(coalesce(last_message_at, new.created_at), new.created_at),
      updated_at = greatest(coalesce(updated_at, new.created_at), new.created_at)
  where id = new.conversation_id
    and owner_key = new.owner_key;
  return new;
end;
$$;

drop trigger if exists copilot_message_touch_conversation on public.copilot_messages;
create trigger copilot_message_touch_conversation
after insert on public.copilot_messages
for each row
execute function public.touch_copilot_conversation_last_message();

revoke all on function public.touch_copilot_conversation_last_message() from public, anon, authenticated;
grant execute on function public.touch_copilot_conversation_last_message() to service_role;
