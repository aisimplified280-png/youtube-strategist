"""Field-level YouTube metadata editor with persisted manual approval."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import streamlit as st

import youtube_analytics_connector as connector
from core.metadata.resource import (
    SUPPORTED_PARTS,
    apply_approved_resource_proposal,
    fetch_video_resource,
    normalize_video_resource,
    _validate_resource,
)


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SECTIONS = ["Core", "Localization", "Status", "Recording", "Advanced"]


def _parse_video_id(value: str) -> str | None:
    raw = str(value or "").strip()
    if VIDEO_ID_RE.fullmatch(raw):
        return raw
    parsed = urlparse(raw)
    candidates = []
    if parsed.hostname and parsed.hostname.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        candidates.append(parse_qs(parsed.query).get("v", [""])[0])
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            candidates.append(parts[1])
    elif parsed.hostname and parsed.hostname.lower() in {"youtu.be", "www.youtu.be"}:
        candidates.append(parsed.path.strip("/").split("/")[0])
    return next((item for item in candidates if VIDEO_ID_RE.fullmatch(item or "")), None)


def _changed_parts(before: dict, draft: dict) -> list[str]:
    return [part for part in SUPPORTED_PARTS if before.get(part, {}) != draft.get(part, {})]


def _stage_draft(store, video_id: str, before: dict, draft: dict, audit_id: str | None):
    _validate_resource(draft)
    changed = _changed_parts(before, draft)
    if not changed:
        st.info("There are no metadata changes to stage.")
        return audit_id
    digest = connector.metadata_proposal_digest(video_id, before, draft)
    title = (draft.get("snippet") or {}).get("title") or ""
    values = {
        "video_title": title,
        "proposed_metadata": draft,
        "updated_parts": changed,
        "approval_digest": digest,
        "status": "pending_review",
        "approved_by": None,
        "approved_at": None,
        "after_metadata": None,
        "result": {},
        "error": None,
        "published_at": None,
    }
    if audit_id:
        row = store.update_metadata_editor_audit(audit_id, values)
        if not row:
            raise RuntimeError("The saved metadata draft could not be updated. Reload the video.")
        return audit_id
    row = store.create_metadata_editor_audit({
        "video_id": video_id,
        "before_metadata": before,
        **values,
    })
    return row["id"]


def _resume_pending_draft(store, video_id: str, current: dict):
    rows = store.list_metadata_editor_audits(status="pending_review", video_id=video_id, limit=5)
    for item in rows:
        full = store.get_metadata_editor_audit(item["id"])
        if not full:
            continue
        before = normalize_video_resource(full.get("before_metadata") or {})
        proposed = normalize_video_resource(full.get("proposed_metadata") or {})
        digest = connector.metadata_proposal_digest(video_id, before, proposed)
        if before == current and digest == full.get("approval_digest"):
            return full
    return None


def _render_core_form(video_id: str, before: dict, draft: dict, store, audit_id):
    snippet = draft.get("snippet") or {}
    with st.form(f"metadata_core_form_{video_id}"):
        title = st.text_input("Title", value=snippet.get("title", ""), max_chars=100)
        description = st.text_area("Description", value=snippet.get("description", ""), height=220)
        tags_text = st.text_area(
            "Tags (comma-separated)",
            value=", ".join(snippet.get("tags") or []),
            help="YouTube tags are entered as separate values. Commas separate tags.",
        )
        category_id = st.text_input("Category ID", value=str(snippet.get("categoryId") or ""))
        col1, col2 = st.columns(2)
        with col1:
            default_language = st.text_input("Metadata language", value=snippet.get("defaultLanguage", ""))
        with col2:
            default_audio_language = st.text_input("Audio language", value=snippet.get("defaultAudioLanguage", ""))
        submitted = st.form_submit_button("Stage core metadata changes", type="primary")
    if submitted:
        updated = copy.deepcopy(draft)
        updated.setdefault("snippet", {})
        updated["snippet"].update({
            "title": title,
            "description": description,
            "tags": [tag.strip() for tag in re.split(r"[,\n]", tags_text) if tag.strip()],
            "categoryId": category_id.strip(),
        })
        for key, value in (
            ("defaultLanguage", default_language.strip()),
            ("defaultAudioLanguage", default_audio_language.strip()),
        ):
            if value:
                updated["snippet"][key] = value
            else:
                updated["snippet"].pop(key, None)
        try:
            audit_id = _stage_draft(store, video_id, before, updated, audit_id)
            st.session_state["_metadata_editor_draft"] = updated
            st.session_state["_metadata_editor_audit_id"] = audit_id
            st.rerun()
        except Exception as error:
            st.error(f"Could not stage core metadata: {error}")


def _render_localization_form(video_id: str, before: dict, draft: dict, store, audit_id):
    localizations = draft.get("localizations") or {}
    with st.form(f"metadata_localization_form_{video_id}"):
        raw = st.text_area(
            "Localized titles and descriptions (JSON)",
            value=json.dumps(localizations, ensure_ascii=False, indent=2),
            height=260,
            help='Example: {"te": {"title": "...", "description": "..."}}',
        )
        submitted = st.form_submit_button("Stage localization changes")
    if submitted:
        try:
            parsed = json.loads(raw or "{}")
            if not isinstance(parsed, dict):
                raise ValueError("Localization data must be a JSON object keyed by language code.")
            updated = copy.deepcopy(draft)
            updated["localizations"] = parsed
            audit_id = _stage_draft(store, video_id, before, updated, audit_id)
            st.session_state["_metadata_editor_draft"] = updated
            st.session_state["_metadata_editor_audit_id"] = audit_id
            st.rerun()
        except Exception as error:
            st.error(f"Could not stage localization changes: {error}")


def _render_status_form(video_id: str, before: dict, draft: dict, store, audit_id):
    status = draft.get("status") or {}
    original_status = before.get("status") or {}
    st.warning(
        "These settings can change visibility, audience declarations or distribution. "
        "They are never AI-published automatically and require your explicit approval."
    )
    with st.form(f"metadata_status_form_{video_id}"):
        privacy = st.selectbox(
            "Visibility",
            ["public", "unlisted", "private"],
            index=["public", "unlisted", "private"].index(status.get("privacyStatus", "public"))
            if status.get("privacyStatus", "public") in {"public", "unlisted", "private"} else 0,
        )
        license_value = st.selectbox(
            "License",
            ["youtube", "creativeCommon"],
            index=1 if status.get("license") == "creativeCommon" else 0,
        )
        col1, col2 = st.columns(2)
        with col1:
            embeddable = st.checkbox("Allow embedding", value=bool(status.get("embeddable", False)))
            public_stats = st.checkbox("Show public statistics", value=bool(status.get("publicStatsViewable", False)))
        with col2:
            made_for_kids = st.checkbox(
                "Made for kids declaration",
                value=bool(status.get("selfDeclaredMadeForKids", False)),
            )
            synthetic = st.checkbox(
                "Contains realistic altered or synthetic media",
                value=bool(status.get("containsSyntheticMedia", False)),
            )
        publish_at = st.text_input("Scheduled publish time (ISO-8601, optional)", value=status.get("publishAt", ""))
        submitted = st.form_submit_button("Stage status changes")
    if submitted:
        updated = copy.deepcopy(draft)
        updated_status = copy.deepcopy(status)
        updated_status.update({
            "privacyStatus": privacy,
            "license": license_value,
            "embeddable": embeddable,
            "publicStatsViewable": public_stats,
        })
        for key, value in (
            ("selfDeclaredMadeForKids", made_for_kids),
            ("containsSyntheticMedia", synthetic),
        ):
            if key in original_status or value:
                updated_status[key] = value
        if publish_at.strip():
            updated_status["publishAt"] = publish_at.strip()
        else:
            updated_status.pop("publishAt", None)
        updated["status"] = updated_status
        try:
            audit_id = _stage_draft(store, video_id, before, updated, audit_id)
            st.session_state["_metadata_editor_draft"] = updated
            st.session_state["_metadata_editor_audit_id"] = audit_id
            st.rerun()
        except Exception as error:
            st.error(f"Could not stage status changes: {error}")


def _render_recording_form(video_id: str, before: dict, draft: dict, store, audit_id):
    recording = draft.get("recordingDetails") or {}
    with st.form(f"metadata_recording_form_{video_id}"):
        recording_date = st.text_input(
            "Recording date (ISO-8601)",
            value=recording.get("recordingDate", ""),
        )
        st.caption("Deprecated recording-location fields are intentionally excluded.")
        submitted = st.form_submit_button("Stage recording details")
    if submitted:
        updated = copy.deepcopy(draft)
        updated["recordingDetails"] = (
            {"recordingDate": recording_date.strip()} if recording_date.strip() else {}
        )
        try:
            audit_id = _stage_draft(store, video_id, before, updated, audit_id)
            st.session_state["_metadata_editor_draft"] = updated
            st.session_state["_metadata_editor_audit_id"] = audit_id
            st.rerun()
        except Exception as error:
            st.error(f"Could not stage recording details: {error}")


def _render_advanced_form(video_id: str, before: dict, draft: dict, store, audit_id):
    partner = draft.get("brandPartner") or {}
    placement = draft.get("paidProductPlacementDetails") or {}
    with st.form(f"metadata_advanced_form_{video_id}"):
        partner_channel = st.text_input("Brand partner channel ID", value=partner.get("channelId", ""))
        paid_placement = st.checkbox(
            "Video contains paid product placement",
            value=bool(placement.get("hasPaidProductPlacement", False)),
        )
        submitted = st.form_submit_button("Stage advanced metadata")
    if submitted:
        updated = copy.deepcopy(draft)
        updated["brandPartner"] = {"channelId": partner_channel.strip()} if partner_channel.strip() else {}
        if "hasPaidProductPlacement" in placement or paid_placement:
            updated["paidProductPlacementDetails"] = {
                "hasPaidProductPlacement": paid_placement
            }
        else:
            updated["paidProductPlacementDetails"] = {}
        try:
            audit_id = _stage_draft(store, video_id, before, updated, audit_id)
            st.session_state["_metadata_editor_draft"] = updated
            st.session_state["_metadata_editor_audit_id"] = audit_id
            st.rerun()
        except Exception as error:
            st.error(f"Could not stage advanced metadata: {error}")


def _render_pending_review(youtube, store, owner_subject: str, video_id: str, before: dict, draft: dict, audit_id: str):
    if not audit_id:
        return
    audit = store.get_metadata_editor_audit(audit_id)
    if not audit or audit.get("status") != "pending_review":
        return
    digest = audit.get("approval_digest") or ""
    changed = audit.get("updated_parts") or []
    st.divider()
    st.subheader("Review and publish")
    st.caption("Only the fields shown below will be sent to YouTube. Other metadata parts remain untouched.")
    st.write("Changed resource parts:", ", ".join(changed))
    with st.expander("Compare current and proposed metadata", expanded=True):
        for part in changed:
            st.markdown(f"**{part}**")
            left, right = st.columns(2)
            with left:
                st.caption("Current")
                st.json(before.get(part, {}))
            with right:
                st.caption("Proposed")
                st.json(draft.get(part, {}))
    confirmed = st.checkbox(
        "I reviewed these exact changes and authorize publishing them to YouTube.",
        key=f"metadata_editor_approve_{audit_id}_{digest[:10]}",
    )
    if st.button(
        "Approve & Publish metadata",
        type="primary",
        disabled=not confirmed,
        key=f"metadata_editor_publish_{audit_id}_{digest[:10]}",
    ):
        now = datetime.now(timezone.utc).isoformat()
        try:
            saved = store.update_metadata_editor_audit(audit_id, {
                "status": "approved",
                "approved_by": owner_subject,
                "approved_at": now,
                "error": None,
            })
            if not saved or saved.get("approval_digest") != digest:
                raise RuntimeError("The saved approval changed. Reload and review the proposal again.")
            store.update_metadata_editor_audit(audit_id, {"status": "publishing"})
            result = apply_approved_resource_proposal(
                youtube, video_id, before, draft, digest,
            )
            after = result.get("resource") or draft
            store.update_metadata_editor_audit(audit_id, {
                "status": "approved_published",
                "after_metadata": after,
                "result": result,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            })
            st.session_state["_metadata_editor_before"] = after
            st.session_state["_metadata_editor_draft"] = after
            st.session_state["_metadata_editor_audit_id"] = None
            st.success("Metadata updated and verified with YouTube.")
            st.rerun()
        except connector.MetadataVerificationUnavailableError as error:
            store.update_metadata_editor_audit(audit_id, {
                "status": "verification_unavailable",
                "error": str(error)[:1000],
            })
            st.error(f"Update may have reached YouTube, but verification was unavailable. Do not retry blindly. {error}")
        except RuntimeError as error:
            status = "stale" if "changed after review" in str(error).lower() else "failed"
            store.update_metadata_editor_audit(audit_id, {"status": status, "error": str(error)[:1000]})
            st.error(str(error))
        except Exception as error:
            store.update_metadata_editor_audit(audit_id, {"status": "failed", "error": str(error)[:1000]})
            st.error(f"Metadata publishing failed: {error}")


def render_metadata_editor(youtube, persistent_store, owner_subject: str):
    st.header("Full Metadata Editor")
    st.caption("Edit supported YouTube metadata fields independently. Only explicitly approved changes are published.")
    if youtube is None:
        st.info("Connect the channel owner's YouTube account to load video metadata.")
        return
    if persistent_store is None:
        st.error("Persistent storage is unavailable. Editing is disabled so drafts and approvals cannot be lost.")
        return

    with st.form("metadata_editor_load_form"):
        reference = st.text_input(
            "YouTube video URL or ID",
            value=st.session_state.get("_metadata_editor_reference", ""),
            placeholder="Paste a YouTube video URL or ID",
        )
        load_clicked = st.form_submit_button("Load video metadata", type="primary")
    if load_clicked:
        video_id = _parse_video_id(reference)
        if not video_id:
            st.error("Enter a valid YouTube video URL or 11-character video ID.")
        else:
            try:
                current = fetch_video_resource(youtube, video_id)
                st.session_state["_metadata_editor_reference"] = reference
                st.session_state["_metadata_editor_video_id"] = video_id
                st.session_state["_metadata_editor_before"] = current
                st.session_state["_metadata_editor_draft"] = copy.deepcopy(current)
                st.session_state["_metadata_editor_audit_id"] = None
                pending = _resume_pending_draft(persistent_store, video_id, current)
                if pending:
                    st.session_state["_metadata_editor_draft"] = pending["proposed_metadata"]
                    st.session_state["_metadata_editor_audit_id"] = pending["id"]
                st.rerun()
            except Exception as error:
                st.error(f"Could not load video metadata: {error}")
                return

    video_id = st.session_state.get("_metadata_editor_video_id")
    before = st.session_state.get("_metadata_editor_before")
    draft = st.session_state.get("_metadata_editor_draft")
    if not video_id or not isinstance(before, dict) or not isinstance(draft, dict):
        st.info("Load a video to begin editing its metadata.")
        return

    st.markdown(f"**Video:** https://www.youtube.com/watch?v={video_id}")
    st.caption(f"Currently published title: {(before.get('snippet') or {}).get('title', 'Unavailable')}")
    section = st.segmented_control(
        "Metadata section",
        options=SECTIONS,
        default="Core",
        key="metadata_editor_section",
        label_visibility="collapsed",
        width="stretch",
    ) or "Core"
    audit_id = st.session_state.get("_metadata_editor_audit_id")

    if section == "Core":
        _render_core_form(video_id, before, draft, persistent_store, audit_id)
    elif section == "Localization":
        _render_localization_form(video_id, before, draft, persistent_store, audit_id)
    elif section == "Status":
        _render_status_form(video_id, before, draft, persistent_store, audit_id)
    elif section == "Recording":
        _render_recording_form(video_id, before, draft, persistent_store, audit_id)
    elif section == "Advanced":
        _render_advanced_form(video_id, before, draft, persistent_store, audit_id)

    audit_id = st.session_state.get("_metadata_editor_audit_id")
    if audit_id:
        _render_pending_review(
            youtube, persistent_store, owner_subject, video_id, before, draft, audit_id
        )

    if st.button("Discard current draft", key=f"metadata_editor_discard_{video_id}"):
        if audit_id:
            persistent_store.update_metadata_editor_audit(audit_id, {"status": "rejected"})
        st.session_state["_metadata_editor_draft"] = copy.deepcopy(before)
        st.session_state["_metadata_editor_audit_id"] = None
        st.rerun()

    if st.button("Show recent metadata activity", key=f"metadata_editor_history_{video_id}"):
        st.session_state["_metadata_editor_show_history"] = True
    if st.session_state.get("_metadata_editor_show_history"):
        history = persistent_store.list_metadata_editor_audits(video_id=video_id, limit=10)
        if history:
            st.dataframe(history, use_container_width=True, hide_index=True)
        else:
            st.caption("No metadata changes have been recorded for this video yet.")
