-- Persist the manual approval and in-progress states for metadata editor publishing.
alter table public.metadata_editor_audits
  drop constraint if exists metadata_editor_audits_status_check;
alter table public.metadata_editor_audits
  add constraint metadata_editor_audits_status_check
  check (status in (
    'pending_review', 'approved', 'publishing', 'approved_published',
    'rejected', 'failed', 'stale', 'verification_unavailable'
  ));
