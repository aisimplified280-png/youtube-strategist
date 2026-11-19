-- Workspace-level autonomous modes prepare drafts, never bypass owner approval.
alter table public.owner_strategy_settings
  add column if not exists metadata_editor_mode text not null default 'assisted'
    check (metadata_editor_mode in ('assisted', 'autonomous_drafts')),
  add column if not exists comments_mode text not null default 'assisted'
    check (comments_mode in ('assisted', 'autonomous_drafts'));

comment on column public.owner_strategy_settings.metadata_editor_mode is
  'assisted: recommendations on demand; autonomous_drafts: prepare a proposed metadata draft when a video is loaded. Publishing still requires owner approval.';
comment on column public.owner_strategy_settings.comments_mode is
  'assisted: analyze selected comments; autonomous_drafts: prepare reply drafts for up to five unanswered comments per loaded page. Publishing always requires owner approval.';
