-- pg_net is non-relocatable. Recreate it in extensions, as recommended by Supabase.
-- Verified before this migration: no queued requests or stored responses exist.
drop extension if exists pg_net;
create schema if not exists extensions;
create extension pg_net with schema extensions;
