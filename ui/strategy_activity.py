"""In-app review and audit panel for the autonomous channel strategist."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
import streamlit as st

import youtube_analytics_connector as connector
from core.strategy_learning import rollback_strategy_action
from core.metadata.resource import apply_approved_resource_proposal, normalize_video_resource


def _pretty_status(value: str) -> str:
    return str(value or "unknown").replace("_", " ").title()


def _invalidate_activity_cache(store) -> None:
    prefix = f"_strategy_activity_cache_{store.owner_key}_"
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefix):
            st.session_state.pop(key, None)


def _load_activity_snapshot(store, active_section: str = "Overview") -> dict:
    """Load and cache only the read-only data required by the active section."""
    section = str(active_section or "Overview")
    key = f"_strategy_activity_cache_{store.owner_key}_{section.casefold().replace(' ', '_')}"
    now = time.monotonic()
    cached = st.session_state.get(key) or {}
    if now - float(cached.get("loaded_at", 0)) < 15:
        return cached.get("data") or {}

    data = {"settings": store.get_strategy_settings()}
    if section in {"Overview", "Run & action history"}:
        data["runs"] = store.list_strategy_runs(limit=30)
    if section == "Overview":
        data["watchdog_events"] = store.list_strategy_watchdog_events(limit=12)
    if section == "Run & action history":
        data["recent_actions"] = store.list_strategy_actions(limit=40)
    if section == "Learning":
        data["evaluations"] = store.list_action_evaluations(limit=50)

    st.session_state[key] = {"loaded_at": now, "data": data}
    return data


def _publish_review_action(store, youtube, action: dict) -> tuple[bool, str]:
    action_id = action.get("id")
    video_id = action.get("video_id")
    before = action.get("before_metadata") or {}
    proposed = action.get("proposed_metadata") or {}
    digest = action.get("approval_digest") or ""
    if youtube is None:
        return False, "Reconnect YouTube before publishing an approved proposal."
    if action.get("status") != "pending_review":
        return False, "This proposal is no longer pending review. Refresh the queue before publishing."
    if not action_id or not video_id or not before or not proposed or not digest:
        return False, "This saved action is incomplete. It was not published; run a fresh strategy scan."

    def audit_callback(entry):
        store.update_strategy_action(action_id, store.owner_key, {
            "audit_log": entry,
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })

    try:
        result = connector.apply_approved_dashboard_proposal(
            youtube, video_id, before, proposed, digest,
            audit_callback=audit_callback,
        )
        if not isinstance(result, dict) or result.get("result") != "success":
            raise RuntimeError("YouTube did not confirm the metadata update.")
        after_metadata = None
        after_snapshot_warning = None
        try:
            after = connector.fetch_video_snippet(youtube, video_id)
            after_metadata = {
                "title": after.get("title") or "",
                "description": after.get("description") or "",
                "tags": list(after.get("tags") or []),
                "categoryId": after.get("categoryId") or "",
            }
        except Exception as snapshot_error:
            # apply_approved_dashboard_proposal has already verified the write.
            # This second fetch only enriches the audit record.
            after_snapshot_warning = f"{type(snapshot_error).__name__}: {str(snapshot_error)[:800]}"
        store.update_strategy_action(action_id, store.owner_key, {
            "status": "approved_published",
            "after_metadata": after_metadata,
            "result": {
                "result": result.get("result"),
                "url": result.get("url"),
                "post_write_snapshot_warning": after_snapshot_warning,
            },
            "error": None,
            "executed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        message = "Approved metadata update was published and verified."
        if after_snapshot_warning:
            message += " The extra after-snapshot could not be collected; the publisher verification is retained in the audit log."
        return True, message
    except connector.MetadataVerificationUnavailableError as error:
        store.update_strategy_action(action_id, store.owner_key, {
            "status": "verification_unavailable",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return False, "The update may have reached YouTube, but read-back verification was unavailable. Reload the video before retrying."
    except Exception as error:
        store.update_strategy_action(action_id, store.owner_key, {
            "status": "failed",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return False, f"Publishing failed safely: {type(error).__name__}: {str(error)[:500]}"


def _publish_metadata_editor_audit(store, youtube, audit: dict) -> tuple[bool, str]:
    """Publish one full-metadata draft from the central review queue."""
    audit_id = str(audit.get("id") or "")
    video_id = str(audit.get("video_id") or "")
    if youtube is None:
        return False, "Reconnect YouTube before publishing this metadata proposal."
    if not audit_id or not video_id or audit.get("status") != "pending_review":
        return False, "This metadata proposal is incomplete or no longer pending review."
    try:
        before = normalize_video_resource(audit.get("before_metadata") or {})
        proposed = normalize_video_resource(audit.get("proposed_metadata") or {})
        digest = str(audit.get("approval_digest") or "")
        expected = connector.metadata_proposal_digest(video_id, before, proposed)
        if not digest or not hmac.compare_digest(expected, digest):
            store.update_metadata_editor_audit(audit_id, {
                "status": "stale",
                "error": "The saved proposal digest no longer matches its exact before/after metadata.",
            })
            return False, "The saved proposal changed after review. Reopen the video and stage a fresh draft."

        approved = store.update_metadata_editor_audit(audit_id, {
            "status": "approved",
            "approved_by": store.owner_key,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        })
        if not approved:
            return False, "Could not persist the owner approval."
        claimed = store.claim_metadata_editor_audit(audit_id)
        if not claimed:
            return False, "This proposal was already claimed or is no longer approved."

        result = apply_approved_resource_proposal(
            youtube, video_id, before, proposed, digest
        )
        if result.get("result") != "success":
            store.update_metadata_editor_audit(audit_id, {
                "status": "rejected",
                "result": result,
                "error": "The proposal contained no metadata changes.",
            })
            return False, "There are no metadata changes to publish."

        store.update_metadata_editor_audit(audit_id, {
            "status": "approved_published",
            "after_metadata": result.get("resource") or proposed,
            "result": result,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        })
        return True, "Metadata update was published and verified."
    except connector.MetadataVerificationUnavailableError as error:
        store.update_metadata_editor_audit(audit_id, {
            "status": "verification_unavailable",
            "error": str(error)[:1500],
        })
        return False, "The update may have reached YouTube, but verification is unavailable. Reload the video before retrying."
    except RuntimeError as error:
        status = "stale" if "changed after review" in str(error).casefold() else "failed"
        store.update_metadata_editor_audit(audit_id, {
            "status": status,
            "error": str(error)[:1500],
        })
        return False, str(error)
    except Exception as error:
        store.update_metadata_editor_audit(audit_id, {
            "status": "failed",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
        })
        return False, f"Publishing failed safely: {type(error).__name__}: {str(error)[:500]}"


def _friendly_gemini_error(error: str) -> str:
    """Translate noisy provider failures into clear, actionable UI messages."""
    message = str(error or "")
    lowered = message.casefold()
    if "http 429" in lowered or "resource_exhausted" in lowered:
        return (
            "Gemini API limits were reached for the models attempted. The worker tried its lower-cost "
            "Flash fallbacks, then saved the analytics briefing without model-generated recommendations. "
            "Check AI Studio → Usage and the API project's billing/quota; your Google AI Pro app plan "
            "does not automatically raise this API key's quota."
        )
    if "http 503" in lowered or "service unavailable" in lowered:
        return (
            "Gemini was temporarily overloaded. The worker retried and moved through the stable Flash "
            "fallbacks; the channel metrics were still saved, but Gemini's analysis could not be completed."
        )
    if "http 404" in lowered or "not_found" in lowered:
        return (
            "A Gemini model ID was unavailable to this API project. The strategist now checks the "
            "model catalogue and skips unavailable IDs; run a fresh scan after the code update."
        )
    if "http 401" in lowered or "authentication failed" in lowered:
        return "Gemini rejected the API key. Check that the GitHub Actions GEMINI_API_KEY secret is current."
    if "http 403" in lowered or "access is denied" in lowered:
        return (
            "Google denied Gemini API access for this project. Check the selected AI Studio project, "
            "API access and billing permissions."
        )
    return message[:1600]


def _short_timestamp(value: str) -> str:
    raw = str(value or "").replace("T", " ")
    if raw.endswith("+00:00"):
        raw = raw[:-6] + " UTC"
    return raw[:19] or "Not recorded"


def render_strategy_activity(store, youtube=None):
    """Render a separated, user-centred channel strategist workspace."""
    if not store:
        st.info("Connect the owner workspace to load the Channel Strategist.")
        return

    st.markdown("## Channel Strategist")
    st.caption(
        "A dedicated workspace for scheduled channel monitoring, Gemini analysis, "
        "metadata approvals and measured outcomes."
    )

    active_section = st.segmented_control(
        "Strategist section",
        options=["Overview", "Review queue", "Run & action history", "Learning", "Settings"],
        default="Overview",
        key="strategy_active_section",
        label_visibility="collapsed",
        width="stretch",
    ) or "Overview"

    try:
        snapshot = _load_activity_snapshot(store, active_section)
        settings = snapshot.get("settings") or {}
        runs = snapshot.get("runs") or []
        watchdog_events = snapshot.get("watchdog_events") or []
        # The mutable approval queue is fetched only when its section is open.
        pending = (
            store.list_strategy_actions(status="pending_review", limit=100)
            if active_section == "Review queue" else []
        )
        metadata_pending = (
            store.list_metadata_editor_audits(
                status="pending_review", limit=100, include_metadata=True
            )
            if active_section == "Review queue" else []
        )
        recent_actions = snapshot.get("recent_actions") or []
        evaluations = snapshot.get("evaluations") or []
    except Exception as error:
        st.warning(f"Could not load strategist activity: {error}")
        return

    is_emergency_paused = bool(settings.get("emergency_pause", False))
    if is_emergency_paused:
        st.error(
            "GLOBAL EMERGENCY PAUSE IS ACTIVE — automated publishing is blocked across all monitoring schedules."
        )

    if active_section == "Overview":
        # A quick status strip for the three schedules. No cadence filter or dropdown.
        latest_by_cadence = {}
        for cadence in ("daily", "weekly", "monthly"):
            latest_by_cadence[cadence] = next(
                (run for run in runs if str(run.get("cadence") or "daily").lower() == cadence),
                None,
            )
    
        cadence_cols = st.columns(3)
        cadence_descriptions = {
            "daily": "Daily monitoring · 08:00 IST",
            "weekly": "Weekly review · Sunday 09:00 IST",
            "monthly": "Monthly review · Day 5, 10:00 IST",
        }
        for col, cadence in zip(cadence_cols, ("daily", "weekly", "monthly")):
            run = latest_by_cadence[cadence]
            with col:
                with st.container(border=True):
                    st.markdown(f"**{cadence.title()}**")
                    st.caption(cadence_descriptions[cadence])
                    if run:
                        result = run.get("result") or {}
                        status = _pretty_status(run.get("status"))
                        st.markdown(f"**{status}**")
                        st.caption(_short_timestamp(run.get("started_at") or run.get("created_at")))
                        st.caption(
                            f"{int(result.get('proposals') or 0)} proposals · "
                            f"{int(result.get('shadow_published') or 0)} shadow · "
                            f"{int(result.get('pending_review') or 0)} review"
                        )
                    else:
                        st.caption("No run recorded yet")
    
        if watchdog_events:
            latest_watchdog = watchdog_events[0]
            st.markdown("### Backup trigger health")
            st.caption(
                f"Latest Supabase watchdog check: {_short_timestamp(latest_watchdog.get('created_at'))} · "
                f"{_pretty_status(latest_watchdog.get('status'))}"
            )
            st.caption(str(latest_watchdog.get("detail") or "No additional details recorded."))
            with st.expander("Recent backup-trigger checks"):
                status_labels = {
                    "dispatch_submitted": "Dispatch submitted",
                    "dispatch_accepted": "GitHub accepted",
                    "dispatch_failed": "Dispatch failed",
                    "waiting_for_response": "Waiting for API response",
                    "waiting_for_run": "Waiting for worker run",
                    "run_detected": "Daily run already exists",
                    "secret_missing": "GitHub token missing",
                }
                st.dataframe(
                    [{
                        "Checked (UTC)": _short_timestamp(item.get("created_at")),
                        "Outcome": status_labels.get(item.get("status"), _pretty_status(item.get("status"))),
                        "HTTP": item.get("http_status_code") or "",
                        "Details": item.get("detail") or "",
                    } for item in watchdog_events],
                    use_container_width=True,
                    hide_index=True,
                )
    
        latest = runs[0] if runs else None
        if not latest:
            st.info(
                "No strategist runs have been recorded yet. The scheduled GitHub Actions jobs "
                "will populate this overview after their first run."
            )
        else:
            metrics_period = latest.get("metrics_period") or {}
            result = latest.get("result") or {}
            st.markdown("### Latest channel scan")
            top_cols = st.columns(4)
            top_cols[0].metric("Run status", _pretty_status(latest.get("status")))
            top_cols[1].metric("Proposals", int(result.get("proposals") or 0))
            top_cols[2].metric("Shadow-tested", int(result.get("shadow_published") or 0))
            top_cols[3].metric("Needs review", int(result.get("pending_review") or 0))

            model_used = str(latest.get("model_name") or "Not recorded")
            st.caption(
                f"{str(latest.get('cadence') or 'daily').title()} cycle · "
                f"{_short_timestamp(latest.get('started_at'))} · "
                f"Analytics: {metrics_period.get('current_start', '?')} to "
                f"{metrics_period.get('current_end', '?')} · Model: {model_used}"
            )

            if latest.get("error"):
                st.warning(_friendly_gemini_error(latest.get("error")))
                with st.expander("Technical error details"):
                    st.code(str(latest.get("error"))[:4000])

            snapshot = latest.get("metrics_snapshot") or {}
            channel_metrics = snapshot.get("channel_metrics") or {}
            deltas = snapshot.get("period_deltas_percent") or {}
            st.markdown("### Channel performance")
            metric_cols = st.columns(4)
            metric_items = [
                ("Period views", channel_metrics.get("views"), deltas.get("views")),
                ("Watch time (min)", channel_metrics.get("estimatedMinutesWatched"), deltas.get("estimatedMinutesWatched")),
                ("Subscribers gained", channel_metrics.get("subscribersGained"), deltas.get("subscribersGained")),
                ("Subscribers lost", channel_metrics.get("subscribersLost"), None),
            ]
            for col, (label, value, delta) in zip(metric_cols, metric_items):
                number = f"{value:,.0f}" if isinstance(value, (int, float)) else "Unavailable"
                delta_text = f"{delta:+.1f}% vs prior period" if isinstance(delta, (int, float)) else None
                col.metric(label, number, delta=delta_text)

            report = latest.get("strategy_report") or {}
            st.markdown("### Gemini growth briefing")
            if report.get("executive_summary"):
                st.write(report["executive_summary"])
            if model_used == "deterministic":
                st.info(
                    "This run contains the deterministic analytics summary only. "
                    "No Gemini-generated strategy or metadata proposal was accepted."
                )

            patterns = report.get("patterns") or []
            actions = report.get("growth_actions") or []
            ideas = report.get("content_ideas") or []
            if patterns:
                with st.expander(f"Patterns detected ({len(patterns)})", expanded=True):
                    for item in patterns:
                        st.markdown(f"**{item.get('pattern', 'Channel pattern')}**")
                        st.write(item.get("evidence") or "No supporting evidence was recorded.")
                        if item.get("implication"):
                            st.caption(
                                f"Implication: {item.get('implication')} · "
                                f"Confidence: {item.get('confidence', 'Not stated')}"
                            )
            if actions:
                with st.expander(f"Recommended growth actions ({len(actions)})", expanded=True):
                    for item in actions:
                        st.markdown(
                            f"**{item.get('priority', 'Action')}: "
                            f"{item.get('action', 'Review channel')}**"
                        )
                        st.write(item.get("reason") or "")
                        related = item.get("related_video_ids") or []
                        if related:
                            st.caption("Related videos: " + ", ".join(str(value) for value in related))
            if ideas:
                with st.expander(f"Content opportunities ({len(ideas)})", expanded=False):
                    for item in ideas:
                        st.markdown(
                            f"**{item.get('topic', 'Topic idea')}** · "
                            f"{item.get('format', 'Format not stated')}"
                        )
                        if item.get("hook"):
                            st.write(f"Suggested hook: {item['hook']}")
                        st.caption(item.get("evidence") or "")

    elif active_section == "Review queue":
        st.markdown("### Metadata proposals")
        st.caption(
            "Review the exact original and proposed metadata. Bulk publishing requires a separate "
            "confirmation that you have reviewed every proposal. Each update is still checked against live YouTube metadata."
        )
        total_pending = len(pending) + len(metadata_pending)
        if total_pending:
            queue_ids = (
                [f"strategy:{item.get('id')}" for item in pending]
                + [f"editor:{item.get('id')}" for item in metadata_pending]
            )
            bulk_queue_fingerprint = hashlib.sha256(
                "|".join(sorted(queue_ids)).encode("utf-8")
            ).hexdigest()[:12]
            bulk_confirmed = st.checkbox(
                f"I reviewed all {total_pending} proposals and approve publishing every item in this queue.",
                key=f"strategy_bulk_publish_confirm_{bulk_queue_fingerprint}",
            )
            if st.button(
                f"Publish all {total_pending} pending proposals",
                key="strategy_publish_all",
                type="primary",
                disabled=not bulk_confirmed or youtube is None,
                use_container_width=True,
            ):
                # Re-read both queues immediately before publishing so a stale UI
                # snapshot cannot publish a proposal already handled elsewhere.
                try:
                    fresh_pending = store.list_strategy_actions(status="pending_review", limit=100)
                    fresh_metadata = store.list_metadata_editor_audits(
                        status="pending_review", limit=100, include_metadata=True
                    )
                    fresh_strategy_by_id = {str(item.get("id")): item for item in fresh_pending}
                    fresh_metadata_by_id = {str(item.get("id")): item for item in fresh_metadata}
                    eligible_strategy = [
                        fresh_strategy_by_id[str(item.get("id"))]
                        for item in pending
                        if str(item.get("id")) in fresh_strategy_by_id
                    ]
                    eligible_metadata = [
                        fresh_metadata_by_id[str(item.get("id"))]
                        for item in metadata_pending
                        if str(item.get("id")) in fresh_metadata_by_id
                    ]
                    eligible_count = len(eligible_strategy) + len(eligible_metadata)
                    if not eligible_count:
                        st.info("No proposals remain pending. The queue has been refreshed.")
                    else:
                        progress = st.progress(0, text="Publishing approved metadata proposals…")
                        successes = 0
                        failures = []
                        position = 0
                        for item in eligible_strategy:
                            position += 1
                            ok, message = _publish_review_action(store, youtube, item)
                            if ok:
                                successes += 1
                            else:
                                failures.append(f"{item.get('video_title') or item.get('video_id')}: {message}")
                            progress.progress(position / eligible_count, text=f"Processed {position} of {eligible_count}")
                        for item in eligible_metadata:
                            position += 1
                            ok, message = _publish_metadata_editor_audit(store, youtube, item)
                            if ok:
                                successes += 1
                            else:
                                failures.append(f"{item.get('video_title') or item.get('video_id')}: {message}")
                            progress.progress(position / eligible_count, text=f"Processed {position} of {eligible_count}")
                        _invalidate_activity_cache(store)
                        st.success(f"Published and verified {successes} of {eligible_count} proposals.")
                        if failures:
                            st.warning("Some proposals were not published:\\n" + "\\n".join(failures))
                        st.rerun()
                except Exception as error:
                    _invalidate_activity_cache(store)
                    st.error(f"Bulk publish stopped safely: {error}")
        else:
            st.success("The review queue is clear.")
        for index, action in enumerate(pending, 1):
            action_id = str(action.get("id") or f"pending-{index}")
            title = action.get("video_title") or "Untitled video"
            confidence = action.get("confidence")
            confidence_label = (
                f" · confidence {float(confidence):.2f}" if confidence is not None else ""
            )
            with st.container(border=True):
                st.markdown(f"**{index}. {title}**")
                st.caption(
                    f"{_pretty_status(action.get('classification'))} · "
                    f"{str(action.get('cadence') or 'daily').title()}"
                    f"{confidence_label}"
                )
                video_id = action.get("video_id") or ""
                if video_id:
                    st.markdown(f"[Open video on YouTube](https://www.youtube.com/watch?v={video_id})")
                st.write(action.get("reason") or "No rationale was recorded.")
                st.caption(f"Diagnosis: {action.get('diagnosis') or 'Not recorded'}")

                # Make the reason for a review hold visible instead of making
                # Autonomous mode look as though it failed to activate.
                confidence_value = action.get("confidence")
                try:
                    confidence_numeric = float(confidence_value)
                except (TypeError, ValueError):
                    confidence_numeric = None
                threshold = float(settings.get("min_confidence_auto_publish", 0.85))
                if action.get("diagnosis") == "metadata_quality":
                    st.info("Review-only metadata audit: this proposal is intentionally excluded from autonomous publishing.")
                elif confidence_numeric is not None and confidence_numeric < threshold:
                    st.info(
                        f"Review required: confidence {confidence_numeric:.2f} is below your "
                        f"autonomous publishing threshold of {threshold:.2f}."
                    )
                elif "model requested review" in str(action.get("reason") or "").casefold():
                    st.info("Review required: the strategy model explicitly requested owner review.")
                evidence = action.get("evidence") or {}
                if evidence.get("cooldown_blocked"):
                    st.caption(
                        evidence.get("cooldown_reason")
                        or "The seven-day metadata baseline/cooldown is not satisfied."
                    )
                for reason in evidence.get("reasons") or []:
                    st.caption(str(reason))

                before = action.get("before_metadata") or {}
                proposed = action.get("proposed_metadata") or {}
                left, right = st.columns(2)
                with left:
                    st.markdown("**Current metadata**")
                    st.text_area(
                        "Current title",
                        value=str(before.get("title") or ""),
                        disabled=True,
                        key=f"strategy_before_title_{action_id}",
                    )
                    st.text_area(
                        "Current description",
                        value=str(before.get("description") or ""),
                        height=140,
                        disabled=True,
                        key=f"strategy_before_desc_{action_id}",
                    )
                    st.caption("Tags: " + ", ".join(before.get("tags") or []))
                with right:
                    st.markdown("**Proposed metadata**")
                    st.text_area(
                        "Proposed title",
                        value=str(proposed.get("title") or ""),
                        disabled=True,
                        key=f"strategy_after_title_{action_id}",
                    )
                    st.text_area(
                        "Proposed description",
                        value=str(proposed.get("description") or ""),
                        height=140,
                        disabled=True,
                        key=f"strategy_after_desc_{action_id}",
                    )
                    st.caption("Tags: " + ", ".join(proposed.get("tags") or []))

                changed_fields = evidence.get("changed_fields") or []
                st.caption("Fields changing: " + (", ".join(changed_fields) or "None"))
                reviewed = st.checkbox(
                    "I reviewed the exact before/after metadata and approve this update.",
                    value=False,
                    key=f"strategy_approve_check_{action_id}",
                )
                approve_col, reject_col = st.columns(2)
                with approve_col:
                    if st.button(
                        "Approve & publish",
                        key=f"strategy_approve_{action_id}",
                        type="primary",
                        disabled=not reviewed or youtube is None,
                        use_container_width=True,
                    ):
                        with st.spinner("Checking live metadata, publishing and verifying…"):
                            success, message = _publish_review_action(store, youtube, action)
                        if success:
                            st.success(message)
                        else:
                            st.error(message)
                        _invalidate_activity_cache(store)
                        st.rerun()
                with reject_col:
                    if st.button(
                        "Skip proposal",
                        key=f"strategy_skip_{action_id}",
                        use_container_width=True,
                    ):
                        try:
                            import datetime as _datetime
                            store.update_strategy_action(action_id, store.owner_key, {
                                "status": "rejected",
                                "updated_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                            })
                            _invalidate_activity_cache(store)
                            st.toast("Proposal skipped. YouTube metadata was not changed.")
                            st.rerun()
                        except Exception as error:
                            st.error(f"Could not update the action log: {error}")

        if metadata_pending:
            st.divider()
            st.markdown("### Full metadata editor proposals")
            st.caption("These drafts were created in Metadata Editor. They are now reviewed and published from this central queue.")
            for audit in metadata_pending:
                audit_id = str(audit.get("id") or "")
                video_id = str(audit.get("video_id") or "")
                parts = audit.get("updated_parts") or []
                before_resource = audit.get("before_metadata") or {}
                proposed_resource = audit.get("proposed_metadata") or {}
                title = audit.get("video_title") or video_id or "Untitled video"
                with st.expander(f"{title} · {', '.join(parts) or 'Metadata'}", expanded=False):
                    st.caption(f"Video ID: {video_id}")
                    for part in parts:
                        st.markdown(f"**Changed resource: {part}**")
                        old_value = before_resource.get(part, {})
                        new_value = proposed_resource.get(part, {})
                        old_col, new_col = st.columns(2)
                        with old_col:
                            st.caption("Currently published")
                            st.json(old_value)
                        with new_col:
                            st.caption("Proposed")
                            st.json(new_value)
                    open_col, status_col = st.columns([1, 2])
                    with open_col:
                        if st.button("Edit in Metadata Editor", key=f"metadata_review_edit_{audit_id}"):
                            st.session_state["_metadata_editor_reference"] = (
                                f"https://www.youtube.com/watch?v={video_id}"
                            )
                            st.session_state["_metadata_editor_autoload"] = True
                            st.session_state["active_workspace"] = "Metadata Editor"
                            st.rerun()
                    with status_col:
                        st.caption("No metadata is changed until you approve publishing.")
                    reviewed = st.checkbox(
                        "I reviewed this exact before/after metadata and approve publishing.",
                        key=f"metadata_review_approve_{audit_id}_{str(audit.get('approval_digest') or '')[:10]}",
                    )
                    publish_col, reject_col = st.columns(2)
                    with publish_col:
                        if st.button(
                            "Approve & publish metadata",
                            type="primary",
                            disabled=not reviewed or youtube is None,
                            key=f"metadata_review_publish_{audit_id}",
                            use_container_width=True,
                        ):
                            success, message = _publish_metadata_editor_audit(
                                store, youtube, audit
                            )
                            if success:
                                st.success(message)
                            else:
                                st.error(message)
                            _invalidate_activity_cache(store)
                            st.rerun()
                    with reject_col:
                        if st.button(
                            "Reject draft",
                            key=f"metadata_review_reject_{audit_id}",
                            use_container_width=True,
                        ):
                            store.update_metadata_editor_audit(audit_id, {
                                "status": "rejected",
                                "error": "Rejected by channel owner in Review & Publishing.",
                            })
                            _invalidate_activity_cache(store)
                            st.toast("Metadata draft rejected. YouTube was not changed.")
                            st.rerun()
        elif not pending:
            st.caption("No full metadata editor drafts are waiting for review.")

    elif active_section == "Run & action history":
        st.markdown("### Monitoring run history")
        if not runs:
            st.caption("No strategy runs recorded yet.")
        else:
            run_rows = []
            for run in runs:
                run_result = run.get("result") or {}
                run_rows.append({
                    "Cadence": str(run.get("cadence") or "daily").title(),
                    "Status": _pretty_status(run.get("status")),
                    "Started": _short_timestamp(run.get("started_at")),
                    "Model": run.get("model_name") or "Not recorded",
                    "Proposals": int(run_result.get("proposals") or 0),
                    "Shadow": int(run_result.get("shadow_published") or 0),
                    "Review": int(run_result.get("pending_review") or 0),
                    "Issues": int(run_result.get("failed") or 0) + int(run_result.get("verification_unavailable") or 0),
                })
            st.dataframe(run_rows, use_container_width=True, hide_index=True)

        st.markdown("### Recent metadata actions")
        if not recent_actions:
            st.caption("No metadata actions recorded yet.")
        for action in recent_actions:
            action_id = action.get("id")
            title = action.get("video_title") or "Untitled video"
            status_raw = action.get("status")
            with st.expander(
                f"{title} · {_pretty_status(status_raw)} · "
                f"{str(action.get('cadence') or 'daily').title()}"
            ):
                st.caption(
                    f"{_short_timestamp(action.get('created_at'))} · "
                    f"{action.get('classification') or 'unknown'} · "
                    f"{action.get('reason') or ''}"
                )
                if action.get("error"):
                    st.error(_friendly_gemini_error(action.get("error")))
                    st.code(str(action.get("error"))[:2500])
                audit = action.get("audit_log") or {}
                if audit:
                    st.json({
                        "result": audit.get("result"),
                        "timestamp": audit.get("timestamp"),
                        "before": audit.get("before"),
                        "after": audit.get("after"),
                    }, expanded=False)
                if status_raw in {"auto_published", "approved_published"} and youtube is not None:
                    if st.button(
                        "Rollback to original",
                        key=f"strategy_rollback_{action_id}",
                    ):
                        with st.spinner("Rolling back metadata…"):
                            success, message = rollback_strategy_action(
                                store, youtube, store.owner_key, action_id
                            )
                        if success:
                            st.success(message)
                        else:
                            st.error(message)
                        st.rerun()

    elif active_section == "Learning":
        st.markdown("### Metadata performance feedback")
        st.caption(
            "The worker compares eligible published changes against their baselines at 7, 14 "
            "and 28 days. These observations help tune future Gemini decisions."
        )
        if not evaluations:
            st.info("No post-publish evaluations are available yet.")
        else:
            evaluation_rows = []
            for item in evaluations:
                deltas = item.get("metric_deltas") or {}
                ctr_delta = deltas.get("ctr_delta_absolute")
                views_delta = deltas.get("views_growth_percent")
                evaluation_rows.append({
                    "Video ID": item.get("video_id"),
                    "Window": f"{item.get('observation_days')} days",
                    "Outcome": str(item.get("outcome") or "inconclusive").title(),
                    "CTR change": f"{ctr_delta:+.2f} pp" if isinstance(ctr_delta, (int, float)) else "N/A",
                    "Views change": f"{views_delta:+.1f}%" if isinstance(views_delta, (int, float)) else "N/A",
                    "Evaluated": _short_timestamp(item.get("evaluated_at")),
                })
            st.dataframe(evaluation_rows, use_container_width=True, hide_index=True)

    elif active_section == "Settings":
        st.markdown("### Automation policy")
        st.caption(
            "These controls govern future scheduled runs. Shadow mode records what Gemini would "
            "change without writing to YouTube."
        )
        emergency_pause = st.toggle(
            "Global emergency pause",
            value=is_emergency_paused,
            key="strategy_emergency_pause_toggle",
            help="Blocks automated publishing across daily, weekly and monthly cycles.",
        )
        current_mode = str(settings.get("automation_mode", "shadow")).strip().lower()
        mode_options = ["Paused", "Shadow mode", "Autonomous mode"]
        mode_lookup = {"paused": "Paused", "shadow": "Shadow mode", "autonomous": "Autonomous mode"}
        selected_mode_label = st.radio(
            "Automation mode",
            options=mode_options,
            index=mode_options.index(mode_lookup.get(current_mode, "Shadow mode")),
            horizontal=True,
            key="strategy_automation_mode_radio",
            help=(
                "Paused: analysis only. Shadow: proposals are logged without YouTube writes. "
                "Autonomous: only measured packaging candidates that the model explicitly marks autonomous, "
            "meet the confidence threshold, pass cooldown and quota checks, and satisfy every safety gate can publish. "
            "Metadata-quality audits and model-requested reviews always stay in this queue."
            ),
        )
        selected_mode = {
            "Paused": "paused",
            "Shadow mode": "shadow",
            "Autonomous mode": "autonomous",
        }[selected_mode_label]

        min_confidence = st.slider(
            "Minimum confidence for autonomous publishing",
            min_value=0.70,
            max_value=0.95,
            value=max(0.70, min(float(settings.get("min_confidence_auto_publish", 0.85)), 0.95)),
            step=0.01,
            key="strategy_min_confidence_slider",
        )
        cap_cols = st.columns(4)
        with cap_cols[0]:
            max_daily = st.number_input(
                "Max daily updates", min_value=0, max_value=10,
                value=int(settings.get("max_daily_minor_publishes", 3)),
                key="strategy_daily_cap",
            )
        with cap_cols[1]:
            max_weekly = st.number_input(
                "Max weekly updates", min_value=0, max_value=20,
                value=int(settings.get("max_weekly_publishes", 5)),
                key="strategy_weekly_cap",
            )
        with cap_cols[2]:
            max_monthly = st.number_input(
                "Max monthly updates", min_value=0, max_value=50,
                value=int(settings.get("max_monthly_publishes", 10)),
                key="strategy_monthly_cap",
            )
        with cap_cols[3]:
            max_review = st.number_input(
                "Max review candidates/run", min_value=0, max_value=10,
                value=int(settings.get("max_metadata_reviews_per_run", 3)),
                key="strategy_review_cap",
            )

        st.divider()
        st.markdown("### Gemini model routing")
        st.markdown("**Primary:** Gemini 3.6 Flash")
        st.markdown(
            "**Fallbacks:** Gemini 3.5 Flash-Lite → Gemini Flash-Lite Latest → "
            "Gemini 3 Flash Preview"
        )
        st.caption(
            "The worker checks which configured models advertise generateContent for the API key, "
            "skips unavailable model IDs, retries temporary overloads, and moves to the next model "
            "when a quota is exhausted. These are Developer API model limits, separate from Gemini "
            "app subscription limits."
        )

        if st.button("Save strategist settings", key="strategy_save_settings", type="primary"):
            try:
                store.set_strategy_settings({
                    "emergency_pause": emergency_pause,
                    "automation_mode": selected_mode,
                    "min_confidence_auto_publish": min_confidence,
                    "max_daily_minor_publishes": max_daily,
                    "max_weekly_publishes": max_weekly,
                    "max_monthly_publishes": max_monthly,
                    "max_metadata_reviews_per_run": max_review,
                })
                _invalidate_activity_cache(store)
                st.success("Strategist settings saved.")
                st.rerun()
            except Exception as error:
                st.error(f"Could not save strategist settings: {error}")
