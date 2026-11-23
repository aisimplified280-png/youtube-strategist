"""Field-level YouTube metadata editor with persisted manual approval."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import streamlit as st


import youtube_analytics_connector as connector
from core.metadata.insights import (
    fetch_video_performance,
    generate_metadata_insights,
    resource_fingerprint,
)
from strategist_chat import generate_metadata_optimization
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


def _metadata_growth_review(resource: dict) -> dict:
    """Create grounded field-level recommendations from the video's current metadata."""
    snippet = resource.get("snippet") or {}
    title = str(snippet.get("title") or "").strip()
    description = str(snippet.get("description") or "").strip()
    tags = [str(item).strip() for item in (snippet.get("tags") or []) if str(item).strip()]
    hashtags = re.findall(r"(?<!\\w)#([A-Za-z0-9_]+)", description)

    recommendations = []
    if not title:
        recommendations.append(("Fix", "Title is missing", "Add a clear title that accurately describes the video's main subject and viewer outcome."))
    elif len(title) > 100:
        recommendations.append(("Fix", f"Title is {len(title)} characters", "YouTube titles are limited to 100 characters. Tighten the wording without losing the main topic."))
    else:
        recommendations.append(("Good", f"Title length: {len(title)}/100", "Keep the main topic and value clear near the beginning; avoid repeating keywords."))

    description_bytes = len(description.encode("utf-8"))
    if not description:
        recommendations.append(("Improve", "Description is empty", "Write a unique description that explains the video and gives viewers a useful next step."))
    elif len(description) > 5000 or description_bytes > 5000:
        recommendations.append((
            "Fix",
            f"Description uses {len(description)} characters / {description_bytes} UTF-8 bytes",
            "The YouTube Data API accepts at most 5,000 UTF-8 bytes for the description. Telugu and other multi-byte scripts can reach this byte limit before reaching 5,000 characters.",
        ))
    else:
        opening = " ".join(description.splitlines()[:2]).strip()
        title_terms = {
            token.casefold() for token in re.findall(r"[A-Za-z0-9]+", title)
            if len(token) > 3 and token.casefold() not in {"with", "from", "your", "this", "that", "what", "when"}
        }
        opening_terms = {token.casefold() for token in re.findall(r"[A-Za-z0-9]+", opening)}
        if len(opening) < 80:
            recommendations.append(("Improve", "Strengthen the opening lines", "Use the first two lines to state what viewers will learn; this text appears before the description is expanded."))
        elif title_terms and not (title_terms & opening_terms):
            recommendations.append(("Improve", "Align the description opening with the title", "Naturally include the video's main topic in the opening lines so the title and description reinforce the same promise."))
        else:
            recommendations.append(("Good", f"Description length: {len(description)}/5,000", "Keep the opening specific and unique to this video; put supporting links and context below it."))

    tag_characters, tag_bytes = connector.calculate_youtube_tag_usage(tags)
    if tag_characters > 500 or tag_bytes > 500:
        recommendations.append((
            "Fix",
            f"Tags use {tag_characters} characters / {tag_bytes} UTF-8 bytes",
            "The Data API budget is 500 characters and 500 UTF-8 bytes. Commas and quotation marks around multi-word tags count toward the limit.",
        ))
    elif not tags:
        recommendations.append(("Optional", "No tags are set", "Add only a few highly relevant terms or common misspellings. Tags usually play a smaller discovery role than the title, thumbnail and description."))
    else:
        recommendations.append(("Good", f"Tags: {len(tags)} · {tag_characters} chars / {tag_bytes} bytes", "Keep tags tightly related to this video's topic; do not add broad or unrelated terms."))

    if len(hashtags) > 60:
        recommendations.append(("Fix", f"{len(hashtags)} hashtags found", "YouTube ignores all hashtags when a video has more than 60. Remove excess and unrelated hashtags."))
    elif len(hashtags) > 3:
        recommendations.append(("Improve", f"{len(hashtags)} hashtags found", "Keep hashtags relevant and selective. YouTube may show up to three above the title."))
    elif hashtags:
        recommendations.append(("Good", f"{len(hashtags)} relevant-looking hashtags found", "Check each hashtag is directly related to the video's subject."))
    return {
        "recommendations": recommendations,
        "title_length": len(title),
        "description_length": len(description),
        "description_bytes": description_bytes,
        "tag_characters": tag_characters,
        "tag_bytes": tag_bytes,
        "hashtag_count": len(hashtags),
        "fixes": sum(1 for level, _, _ in recommendations if level == "Fix"),
        "improvements": sum(1 for level, _, _ in recommendations if level == "Improve"),
    }


def _stage_draft(store, video_id: str, before: dict, draft: dict, audit_id: str | None):
    _validate_resource(draft)
    changed = _changed_parts(before, draft)
    if not changed:
        if audit_id:
            store.update_metadata_editor_audit(audit_id, {
                "status": "rejected",
                "error": "Draft reset to the currently published metadata.",
            })
        st.info("There are no metadata changes to stage.")
        return None
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
        bool_options = ["Not set", "Yes", "No"]
        embeddable_current = (
            "Yes" if status.get("embeddable") is True
            else "No" if status.get("embeddable") is False
            else "Not set"
        )
        public_stats_current = (
            "Yes" if status.get("publicStatsViewable") is True
            else "No" if status.get("publicStatsViewable") is False
            else "Not set"
        )
        col1, col2 = st.columns(2)
        with col1:
            embeddable_choice = st.selectbox(
                "Allow embedding", bool_options, index=bool_options.index(embeddable_current)
            )
            public_stats_choice = st.selectbox(
                "Show public statistics", bool_options, index=bool_options.index(public_stats_current)
            )
        with col2:
            audience_options = ["Not set", "Made for kids", "Not made for kids"]
            audience_current = (
                "Made for kids" if status.get("selfDeclaredMadeForKids") is True
                else "Not made for kids" if status.get("selfDeclaredMadeForKids") is False
                else "Not set"
            )
            audience_choice = st.selectbox(
                "Audience declaration",
                audience_options,
                index=audience_options.index(audience_current),
            )
            synthetic_options = ["Not set", "Contains realistic altered or synthetic media", "Does not contain realistic altered or synthetic media"]
            synthetic_current = (
                synthetic_options[1] if status.get("containsSyntheticMedia") is True
                else synthetic_options[2] if status.get("containsSyntheticMedia") is False
                else synthetic_options[0]
            )
            synthetic_choice = st.selectbox(
                "Altered or synthetic media declaration",
                synthetic_options,
                index=synthetic_options.index(synthetic_current),
            )
        publish_at = st.text_input("Scheduled publish time (ISO-8601, optional)", value=status.get("publishAt", ""))
        submitted = st.form_submit_button("Stage status changes")
    if submitted:
        updated = copy.deepcopy(draft)
        updated_status = copy.deepcopy(status)
        updated_status.update({
            "privacyStatus": privacy,
            "license": license_value,
        })
        for key, choice in (
            ("embeddable", embeddable_choice),
            ("publicStatsViewable", public_stats_choice),
        ):
            if choice == "Yes":
                updated_status[key] = True
            elif choice == "No":
                updated_status[key] = False
            else:
                updated_status.pop(key, None)
        if audience_choice == "Made for kids":
            updated_status["selfDeclaredMadeForKids"] = True
        elif audience_choice == "Not made for kids":
            updated_status["selfDeclaredMadeForKids"] = False
        else:
            updated_status.pop("selfDeclaredMadeForKids", None)
        if synthetic_choice == synthetic_options[1]:
            updated_status["containsSyntheticMedia"] = True
        elif synthetic_choice == synthetic_options[2]:
            updated_status["containsSyntheticMedia"] = False
        else:
            updated_status.pop("containsSyntheticMedia", None)
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
    paid_options = ["Not set", "Yes", "No"]
    paid_current = (
        "Yes" if placement.get("hasPaidProductPlacement") is True
        else "No" if placement.get("hasPaidProductPlacement") is False
        else "Not set"
    )
    with st.form(f"metadata_advanced_form_{video_id}"):
        partner_channel = st.text_input("Brand partner channel ID", value=partner.get("channelId", ""))
        paid_choice = st.selectbox(
            "Paid product placement declaration",
            paid_options,
            index=paid_options.index(paid_current),
        )
        submitted = st.form_submit_button("Stage advanced metadata")
    if submitted:
        updated = copy.deepcopy(draft)
        updated["brandPartner"] = {"channelId": partner_channel.strip()} if partner_channel.strip() else {}
        if paid_choice == "Yes":
            updated["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
        elif paid_choice == "No":
            updated["paidProductPlacementDetails"] = {"hasPaidProductPlacement": False}
        else:
            updated["paidProductPlacementDetails"] = {}
        try:
            audit_id = _stage_draft(store, video_id, before, updated, audit_id)
            st.session_state["_metadata_editor_draft"] = updated
            st.session_state["_metadata_editor_audit_id"] = audit_id
            st.rerun()
        except Exception as error:
            st.error(f"Could not stage advanced metadata: {error}")


def _render_metadata_insight_summary(
    youtube, analytics, secrets: dict, persistent_store, video_id: str,
    before: dict, draft: dict, audit_id: str | None,
):
    """Show current compliance and growth findings before the editable fields."""
    cache_key = f"_metadata_insights_{video_id}_{resource_fingerprint(before)}"
    if st.button("Refresh metadata health analysis", key=f"metadata_insight_refresh_{video_id}"):
        st.session_state.pop(cache_key, None)
    insight = st.session_state.get(cache_key)
    if insight is None:
        with st.spinner("Checking YouTube metadata standards and recent video performance..."):
            performance = fetch_video_performance(analytics, video_id)
            insight = generate_metadata_insights(secrets, before, performance)
            st.session_state[cache_key] = insight

    with st.container(border=True):
        st.subheader("Metadata health & channel-growth insights")
        st.write(insight.get("summary") or "Metadata analysis completed.")
        source_perf = insight.get("performance") or {}
        if source_perf.get("current_period"):
            start, end = source_perf["current_period"]
            prior = source_perf.get("previous_period") or ["?", "?"]
            st.caption(
                f"Video Analytics comparison: {start} to {end} vs {prior[0]} to {prior[1]}. "
                "The latest three calendar days are excluded because YouTube Analytics is delayed."
            )
        elif source_perf.get("reason"):
            st.caption(f"Video Analytics unavailable: {source_perf['reason']}")

        metrics = source_perf.get("metrics") or {}
        if metrics:
            columns = st.columns(4)
            for col, metric, label, suffix in (
                (columns[0], "views", "Views", ""),
                (columns[1], "estimatedMinutesWatched", "Watch time", " min"),
                (columns[2], "averageViewPercentage", "Avg. viewed", "%"),
                (columns[3], "subscribersGained", "Subscribers gained", ""),
            ):
                value = (metrics.get(metric) or {}).get("current")
                delta = (metrics.get(metric) or {}).get("delta_percent")
                shown = f"{value:,.1f}{suffix}" if isinstance(value, (int, float)) else "Unavailable"
                delta_label = f"{delta:+.1f}% vs prior period" if isinstance(delta, (int, float)) else None
                col.metric(label, shown, delta=delta_label)

        if source_perf.get("reach_metrics_note"):
            st.caption(source_perf["reach_metrics_note"])

        checks = insight.get("standard_checks") or []
        if checks:
            st.markdown("**YouTube metadata standards**")
            for item in checks:
                marker = {"error": "❌", "warning": "⚠️", "pass": "✓", "info": "ℹ️"}.get(item.get("status"), "•")
                st.caption(f"{marker} **{item.get('field', 'Field').title()}** — {item.get('detail', '')}")

        findings = insight.get("findings") or []
        if findings:
            st.markdown("**Growth and packaging observations**")
            for item in findings:
                st.markdown(f"**{str(item.get('field') or 'Metadata').title()} · {str(item.get('severity') or 'info').title()}**")
                st.write(item.get("issue") or item.get("recommendation") or "")
                if item.get("evidence"):
                    st.caption(f"Evidence: {item['evidence']}")

        recommendations = insight.get("recommendations") or []
        if recommendations:
            st.markdown("**Suggested improvements**")
            for index, item in enumerate(recommendations, 1):
                field = item["field"]
                with st.container(border=True):
                    st.markdown(
                        f"**{index}. {field.title()}** · "
                        f"confidence {float(item.get('confidence') or 0):.0%}"
                    )
                    if item.get("issue"):
                        st.write(item["issue"])
                    if item.get("evidence"):
                        st.caption(f"Evidence: {item['evidence']}")
                    if item.get("recommendation"):
                        st.write(item["recommendation"])
                    proposed_value = item.get("suggested_value")
                    st.markdown("**Suggested value**")
                    st.code(
                        "\\n".join(proposed_value) if isinstance(proposed_value, list)
                        else str(proposed_value),
                        language="text",
                    )
                    if st.button(
                        f"Stage this {field} suggestion",
                        key=f"metadata_stage_insight_{video_id}_{index}",
                    ):
                        updated = copy.deepcopy(draft)
                        updated.setdefault("snippet", {})[field] = copy.deepcopy(proposed_value)
                        try:
                            saved_id = _stage_draft(
                                persistent_store, video_id, before, updated, audit_id
                            )
                            st.session_state["_metadata_editor_draft"] = updated
                            st.session_state["_metadata_editor_audit_id"] = saved_id
                            st.success("Suggestion staged in Review & Publishing. YouTube has not been changed.")
                            st.rerun()
                        except Exception as error:
                            st.error(f"Could not stage this suggestion: {error}")
        elif insight.get("analysis_status") == "ai_and_rules":
            st.success("No additional evidence-backed text changes were recommended.")
        elif insight.get("analysis_status") == "rules_only":
            st.info(
                "Only deterministic compliance checks are available right now. "
                "AI suggestions will appear when a configured provider is available."
            )
        if insight.get("provider"):
            st.caption(f"Analysis: {insight['provider']} · {insight.get('model') or ''}")
        st.caption(
            "This is decision support, not a promise of growth. YouTube says titles and descriptions matter more for discovery than tags; tags mainly help with common misspellings. "
            "[YouTube metadata limits](https://developers.google.com/youtube/data/reference/rest/v3/videos) · "
            "[YouTube tags guidance](https://support.google.com/youtube/answer/146402)"
        )
    return insight


def render_metadata_editor(youtube, persistent_store, owner_subject: str, secrets: dict | None = None, analytics=None):
    st.header("Full Metadata Editor")
    st.caption("Edit supported YouTube metadata fields independently. Only explicitly approved changes are published.")
    if youtube is None:
        st.info("Connect the channel owner's YouTube account to load video metadata.")
        return
    if persistent_store is None:
        st.error("Persistent storage is unavailable. Editing is disabled so drafts and approvals cannot be lost.")
        return

    if st.session_state.pop("_metadata_editor_autoload", False):
        reference = st.session_state.get("_metadata_editor_reference", "")
        video_id = _parse_video_id(reference)
        if video_id:
            try:
                current = fetch_video_resource(youtube, video_id)
                st.session_state["_metadata_editor_video_id"] = video_id
                st.session_state["_metadata_editor_before"] = current
                st.session_state["_metadata_editor_draft"] = copy.deepcopy(current)
                st.session_state["_metadata_editor_audit_id"] = None
                st.session_state.pop("_metadata_editor_ai_proposal", None)
                pending = _resume_pending_draft(persistent_store, video_id, current)
                if pending:
                    st.session_state["_metadata_editor_draft"] = pending["proposed_metadata"]
                    st.session_state["_metadata_editor_audit_id"] = pending["id"]
                st.rerun()
            except Exception as error:
                st.error(f"Could not open the selected metadata draft: {error}")
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
                st.session_state.pop("_metadata_editor_ai_proposal", None)
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
    _render_metadata_insight_summary(
        youtube, analytics, secrets or {}, persistent_store, video_id,
        before, draft, st.session_state.get("_metadata_editor_audit_id"),
    )

    quality = _metadata_growth_review(before)
    with st.container(border=True):
        st.markdown("### Metadata health & growth suggestions")
        st.caption(
            "Review of the currently published fields. These are recommendations only; "
            "nothing is changed or staged automatically."
        )
        metric_cols = st.columns(4)
        metric_cols[0].metric("Title", f"{quality['title_length']}/100")
        metric_cols[1].metric("Description bytes", f"{quality['description_bytes']:,}/5,000")
        metric_cols[2].metric("Tag budget", f"{quality['tag_bytes']:,}/500 bytes")
        metric_cols[3].metric("Hashtags", quality["hashtag_count"])
        st.caption(
            f"Description: {quality['description_length']:,} characters · "
            f"{quality['description_bytes']:,} UTF-8 bytes. "
            f"Tags: {quality['tag_characters']:,} characters · {quality['tag_bytes']:,} UTF-8 bytes."
        )
        for level, heading, detail in quality["recommendations"]:
            icon = "✓" if level == "Good" else "!" if level == "Fix" else "↗"
            st.markdown(f"**{icon} {level}: {heading}**")
            st.caption(detail)
        st.caption(
            "Growth focus: make the title, thumbnail and first description lines communicate one clear, "
            "accurate promise. Tags are secondary; this editor does not have video-level CTR or retention "
            "for this specific upload, so it will not claim a predicted view or CTR lift."
        )
        if st.button(
            "Generate optimizations", type="primary",
            key=f"metadata_generate_optimizations_{video_id}",
            help="Generate three title angles, a revised description and relevant tags using current metadata and available captions.",
        ):
            try:
                with st.spinner("Generating and validating metadata options..."):
                    generated = generate_metadata_optimization(youtube, secrets or {}, video_id, before)
                st.session_state["_metadata_editor_ai_proposal"] = {"video_id": video_id, **generated}
                st.rerun()
            except Exception as error:
                st.error(f"Could not generate metadata optimizations: {error}")

    ai_proposal = st.session_state.get("_metadata_editor_ai_proposal")
    if ai_proposal and str(ai_proposal.get("video_id")) == str(video_id):
        with st.container(border=True):
            st.markdown("### AI optimization proposal")
            st.caption(
                f"{ai_proposal.get('provider', 'AI')} · {ai_proposal.get('model', 'model')} · "
                f"{ai_proposal.get('transcript_status', 'Transcript status unavailable')}"
            )
            st.write(ai_proposal.get("assistant_message") or "")
            angles = ai_proposal.get("title_angles") or []
            labels = [f"{item.get('label') or 'Option'}: {item.get('title') or ''}" for item in angles]
            title_by_label = {label: str(item.get("title") or "") for label, item in zip(labels, angles)}
            recommended = str((ai_proposal.get("proposed") or {}).get("title") or "")
            default_index = next((i for i, label in enumerate(labels) if title_by_label[label] == recommended), 0)
            with st.form(f"metadata_ai_proposal_form_{video_id}"):
                selected_label = st.radio("Choose the title to stage", options=labels, index=default_index)
                edited_description = st.text_area(
                    "Optimized description (including hashtags)",
                    value=str((ai_proposal.get("proposed") or {}).get("description") or ""), height=220,
                )
                edited_tags = st.text_area(
                    "Separate YouTube tags (comma-separated)",
                    value=", ".join((ai_proposal.get("proposed") or {}).get("tags") or []), height=90,
                )
                stage_optimization = st.form_submit_button("Stage optimization for review", type="primary")
            if ai_proposal.get("rationale"):
                with st.expander("Why these optimizations were recommended", expanded=False):
                    for item in ai_proposal["rationale"]:
                        st.write(f"• {item}")
            if stage_optimization:
                updated = copy.deepcopy(before)
                updated.setdefault("snippet", {})
                updated["snippet"].update({
                    "title": title_by_label.get(selected_label, recommended),
                    "description": edited_description,
                    "tags": [tag.strip() for tag in re.split(r"[,\n]", edited_tags) if tag.strip()],
                })
                try:
                    staged_id = _stage_draft(
                        persistent_store, video_id, before, updated,
                        st.session_state.get("_metadata_editor_audit_id"),
                    )
                    st.session_state["_metadata_editor_draft"] = updated
                    st.session_state["_metadata_editor_audit_id"] = staged_id
                    st.session_state.pop("_metadata_editor_ai_proposal", None)
                    st.success("Optimization staged in the central Review & Publishing queue. Nothing has been published.")
                    st.rerun()
                except Exception as error:
                    st.error(f"Could not stage this optimization: {error}")

    section = st.segmented_control(
        "Metadata section",
        options=SECTIONS,
        default="Core",
        key="metadata_editor_section",
        label_visibility="collapsed",
        width="stretch",
    ) or "Core"
    audit_id = st.session_state.get("_metadata_editor_audit_id")
    st.caption("Review the metadata health notes, then edit and stage any changes you want to review.")

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
        audit = persistent_store.get_metadata_editor_audit(audit_id)
        if audit and audit.get("status") == "pending_review":
            st.info("Your draft is saved in the central Review & Publishing queue. Publishing is handled there so all proposals can be reviewed together.")
            if st.button("Open Review & Publishing", type="primary", key=f"metadata_open_review_{audit_id}"):
                st.session_state["active_workspace"] = "Channel Strategist"
                st.session_state["strategy_active_section"] = "Review queue"
                st.rerun()

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
