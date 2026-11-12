"""In-app review and audit panel for the autonomous channel strategist."""

from __future__ import annotations

import streamlit as st

import youtube_analytics_connector as connector
from core.strategy_learning import rollback_strategy_action


def _pretty_status(value: str) -> str:
    return str(value or "unknown").replace("_", " ").title()


def _publish_review_action(store, youtube, action: dict) -> tuple[bool, str]:
    action_id = action.get("id")
    video_id = action.get("video_id")
    before = action.get("before_metadata") or {}
    proposed = action.get("proposed_metadata") or {}
    digest = action.get("approval_digest") or ""
    if youtube is None:
        return False, "Reconnect YouTube before publishing an approved proposal."
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

    try:
        settings = store.get_strategy_settings()
        runs = store.list_strategy_runs(limit=30)
        pending = store.list_strategy_actions(status="pending_review", limit=100)
        recent_actions = store.list_strategy_actions(limit=40)
        evaluations = store.list_action_evaluations(limit=50)
    except Exception as error:
        st.warning(f"Could not load strategist activity: {error}")
        return

    is_emergency_paused = bool(settings.get("emergency_pause", False))
    if is_emergency_paused:
        st.error(
            "GLOBAL EMERGENCY PAUSE IS ACTIVE — automated publishing is blocked across all monitoring schedules."
        )

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

    overview_tab, review_tab, history_tab, learning_tab, settings_tab = st.tabs(
        ["Overview", f"Review queue ({len(pending)})", "Run & action history", "Learning", "Settings"]
    )

    with overview_tab:
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

    with review_tab:
        st.markdown("### Metadata proposals")
        st.caption(
            "Review the exact original and proposed metadata. Nothing in this queue is published "
            "until you approve the individual proposal."
        )
        if not pending:
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

                evidence = action.get("evidence") or {}
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
                            st.toast("Proposal skipped. YouTube metadata was not changed.")
                            st.rerun()
                        except Exception as error:
                            st.error(f"Could not update the action log: {error}")

    with history_tab:
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

    with learning_tab:
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

    with settings_tab:
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
                "Autonomous: only eligible high-confidence changes can publish after all safety gates."
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
        st.markdown(
            "**Primary:** Gemini 3.8 Flash  
"
            "**Fallbacks:** Gemini 3.6 Flash → Gemini 3.5 Flash → "
            "Gemini 3.5 Flash-Lite → Gemini 3.1 Flash-Lite"
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
                st.success("Strategist settings saved.")
                st.rerun()
            except Exception as error:
                st.error(f"Could not save strategist settings: {error}")
