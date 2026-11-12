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


def render_strategy_activity(store, youtube=None):
    """Render strategist automation controls, pending proposals, learning feedback, and audit records."""
    if not store:
        return

    st.divider()
    st.markdown("### Autonomous YouTube Strategist")
    st.caption(
        "Cadence monitoring (Daily · Weekly · Monthly) · Gemini reasoning · Autonomous/Shadow policy · 7/14/28d evaluation loop"
    )

    try:
        settings = store.get_strategy_settings()
    except Exception as error:
        st.warning(f"Strategist settings are unavailable: {error}")
        return

    # Check emergency pause banner
    is_emergency_paused = bool(settings.get("emergency_pause", False))
    if is_emergency_paused:
        st.error("🚨 **GLOBAL EMERGENCY PAUSE ACTIVE** — All automated YouTube publishing is paused across all cadences.")

    with st.expander("Automation Controls & Policy Settings", expanded=False):
        emergency_pause = st.toggle(
            "Global Emergency Pause",
            value=is_emergency_paused,
            key="strategy_emergency_pause_toggle",
            help="Immediately halts all autonomous publishing across all cadences. All proposals hold for review.",
        )

        current_mode = str(settings.get("automation_mode", "shadow")).strip().lower()
        mode_map = {"paused": "Paused", "shadow": "Shadow mode", "autonomous": "Autonomous mode"}
        mode_index = ["paused", "shadow", "autonomous"].index(current_mode) if current_mode in ["paused", "shadow", "autonomous"] else 1

        selected_mode_label = st.radio(
            "Automation Mode",
            options=["Paused", "Shadow mode", "Autonomous mode"],
            index=mode_index,
            key="strategy_automation_mode_radio",
            help=(
                "• Paused: Analysis and briefings run; no proposals are published automatically.\n"
                "• Shadow mode: Full analysis and proposal generation runs; records shadow_published logs without writing to YouTube.\n"
                "• Autonomous mode: High-confidence updates (>= threshold) publish automatically after passing cooldown and safety gates."
            ),
        )
        selected_mode = {"Paused": "paused", "Shadow mode": "shadow", "Autonomous mode": "autonomous"}[selected_mode_label]

        min_confidence = st.slider(
            "Minimum confidence for autonomous publishing",
            min_value=0.70,
            max_value=0.95,
            value=float(settings.get("min_confidence_auto_publish", 0.85)),
            step=0.01,
            key="strategy_min_confidence_slider",
            help="Gemini confidence score required to auto-publish in Autonomous mode. Lower-confidence proposals queue for review.",
        )

        cap_cols = st.columns(4)
        with cap_cols[0]:
            max_daily = st.number_input(
                "Max daily updates",
                min_value=0, max_value=10,
                value=int(settings.get("max_daily_minor_publishes", 3)),
                key="strategy_daily_cap",
            )
        with cap_cols[1]:
            max_weekly = st.number_input(
                "Max weekly updates",
                min_value=0, max_value=20,
                value=int(settings.get("max_weekly_publishes", 5)),
                key="strategy_weekly_cap",
            )
        with cap_cols[2]:
            max_monthly = st.number_input(
                "Max monthly updates",
                min_value=0, max_value=50,
                value=int(settings.get("max_monthly_publishes", 10)),
                key="strategy_monthly_cap",
            )
        with cap_cols[3]:
            max_review = st.number_input(
                "Max review candidates/run",
                min_value=0, max_value=10,
                value=int(settings.get("max_metadata_reviews_per_run", 3)),
                key="strategy_review_cap",
            )

        if st.button("Save Strategist Settings", key="strategy_save_settings", type="primary"):
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
                st.success("Strategist settings saved successfully.")
                st.rerun()
            except Exception as error:
                st.error(f"Could not save strategist settings: {error}")

    # Cadence filter selection
    cadence_filter_tab = st.radio(
        "Monitoring Cadence",
        options=["All Cadences", "Daily", "Weekly", "Monthly"],
        horizontal=True,
        key="strategy_cadence_filter_radio",
    )
    selected_cadence = None if cadence_filter_tab == "All Cadences" else cadence_filter_tab.lower()

    try:
        runs = store.list_strategy_runs(limit=10, cadence=selected_cadence)
        pending = store.list_strategy_actions(status="pending_review", cadence=selected_cadence, limit=100)
        recent_actions = store.list_strategy_actions(cadence=selected_cadence, limit=30)
        evaluations = store.list_action_evaluations(limit=25)
    except Exception as error:
        st.warning(f"Could not load strategist activity logs: {error}")
        return

    if runs:
        latest = runs[0]
        metrics_period = latest.get("metrics_period") or {}
        result = latest.get("result") or {}
        cols = st.columns(5)
        cols[0].metric("Latest Run", _pretty_status(latest.get("status")), help=f"Cadence: {latest.get('cadence', 'daily').title()}")
        cols[1].metric("Auto-published", int(result.get("auto_published") or 0))
        cols[2].metric("Shadow-published", int(result.get("shadow_published") or 0))
        cols[3].metric("Awaiting review", int(result.get("pending_review") or 0))
        cols[4].metric("Issues", int(result.get("failed") or 0) + int(result.get("verification_unavailable") or 0))

        st.caption(
            f"Cadence: **{latest.get('cadence', 'daily').title()}** · "
            f"Last run: {latest.get('started_at', 'Unknown')} · "
            f"Analytics period: {metrics_period.get('current_start', '?')} to {metrics_period.get('current_end', '?')} · "
            f"Model: {latest.get('model_name') or 'not recorded'}"
        )
        if latest.get("error"):
            st.warning(str(latest["error"]))
        snapshot = latest.get("metrics_snapshot") or {}
        channel_metrics = snapshot.get("channel_metrics") or {}
        deltas = snapshot.get("period_deltas_percent") or {}
        kpi_values = [
            ("Period views", channel_metrics.get("views"), deltas.get("views")),
            ("Watch time (min)", channel_metrics.get("estimatedMinutesWatched"), deltas.get("estimatedMinutesWatched")),
            ("Subscribers gained", channel_metrics.get("subscribersGained"), deltas.get("subscribersGained")),
            ("Subscribers lost", channel_metrics.get("subscribersLost"), None),
        ]
        kpi_cols = st.columns(4)
        for col, (label, value, delta) in zip(kpi_cols, kpi_values):
            number = f"{value:,.0f}" if isinstance(value, (int, float)) else "Unavailable"
            delta_text = f"{delta:+.1f}% vs prior period" if isinstance(delta, (int, float)) else None
            col.metric(label, number, delta=delta_text)

        report = latest.get("strategy_report") or {}
        if report:
            st.markdown("#### Gemini Strategic Growth Briefing")
            if report.get("executive_summary"):
                st.write(report["executive_summary"])
            patterns = report.get("patterns") or []
            if patterns:
                with st.expander("Detected Channel Patterns", expanded=True):
                    for item in patterns:
                        st.markdown(f"**{item.get('pattern', 'Channel pattern')}**")
                        st.write(item.get("evidence") or "")
                        if item.get("implication"):
                            st.caption(f"Implication: {item.get('implication')} · Confidence: {item.get('confidence', 'Unstated')}")
            actions = report.get("growth_actions") or []
            if actions:
                with st.expander("Prioritized Growth Actions", expanded=True):
                    for item in actions:
                        st.markdown(f"**{item.get('priority', 'Action')}: {item.get('action', 'Review channel')}**")
                        st.write(item.get("reason") or "")
            ideas = report.get("content_ideas") or []
            if ideas:
                with st.expander("Content Opportunities & Hooks", expanded=False):
                    for item in ideas:
                        st.markdown(f"**{item.get('topic', 'Topic idea')}** · {item.get('format', 'Format unconfirmed')}")
                        if item.get("hook"):
                            st.write(f"Hook: {item.get('hook')}")
                        st.caption(item.get("evidence") or "")
    else:
        st.info("No strategy run recorded for the selected cadence. Daily, Weekly, and Monthly workers will populate this panel.")

    # Performance Evaluation & Feedback Loop
    if evaluations:
        with st.expander("📊 Optimization Feedback Loop (7d / 14d / 28d Post-Publish Evaluations)", expanded=False):
            st.caption("Empirical measurements tracking whether published metadata changes improved, held, or declined CTR and views.")
            for ev in evaluations:
                video_id = ev.get("video_id")
                days = ev.get("observation_days")
                outcome = str(ev.get("outcome") or "inconclusive").upper()
                badge_color = "green" if outcome == "IMPROVED" else ("red" if outcome == "DECLINED" else "gray")
                deltas = ev.get("metric_deltas") or {}
                ctr_d = deltas.get("ctr_delta_absolute")
                views_g = deltas.get("views_growth_percent")

                ctr_txt = f"{ctr_d:+.2f}% CTR" if isinstance(ctr_d, (int, float)) else "CTR N/A"
                views_txt = f"{views_g:+.1f}% views" if isinstance(views_g, (int, float)) else "views N/A"

                st.markdown(
                    f"**Video `{video_id}`** · {days}-day observation window: "
                    f":{badge_color}[**{outcome}**] ({ctr_txt}, {views_txt})"
                )
                base = ev.get("baseline_metrics") or {}
                post = ev.get("post_metrics") or {}
                st.caption(
                    f"Baseline ({base.get('period', '?')}): {base.get('views', 0):,} views, CTR {base.get('ctr_percent', '?')}% | "
                    f"Post ({post.get('period', '?')}): {post.get('views', 0):,} views, CTR {post.get('ctr_percent', '?')}%"
                )
                st.divider()

    if pending:
        st.markdown("#### Metadata proposals awaiting owner approval")
        st.caption("Proposals with confidence < auto threshold, structural rewrites, or held by emergency pause appear here.")
        for index, action in enumerate(pending, 1):
            action_id = str(action.get("id") or f"pending-{index}")
            title = action.get("video_title") or "Untitled video"
            conf = action.get("confidence")
            conf_str = f"Confidence {float(conf):.2f}" if conf is not None else ""
            diag_str = f"Diagnosis: {action.get('diagnosis', 'packaging')}"
            header_str = f"{index}. {title} · {_pretty_status(action.get('classification'))} ({action.get('cadence', 'daily').title()})"
            if conf_str:
                header_str += f" · {conf_str}"

            with st.expander(header_str, expanded=index == 1):
                video_id = action.get("video_id") or ""
                if video_id:
                    st.markdown(f"[Open video on YouTube](https://www.youtube.com/watch?v={video_id})")
                st.write(action.get("reason") or "No rationale was recorded.")
                st.caption(diag_str)

                evidence = action.get("evidence") or {}
                reasons = evidence.get("reasons") or []
                for reason in reasons:
                    st.caption(str(reason))
                before = action.get("before_metadata") or {}
                proposed = action.get("proposed_metadata") or {}
                left, right = st.columns(2)
                with left:
                    st.markdown("**Current Metadata**")
                    st.text_area(
                        "Current title",
                        value=str(before.get("title") or ""),
                        disabled=True,
                        key=f"strategy_before_title_{action_id}",
                    )
                    st.text_area(
                        "Current description",
                        value=str(before.get("description") or ""),
                        height=160,
                        disabled=True,
                        key=f"strategy_before_desc_{action_id}",
                    )
                    st.caption("Current tags: " + ", ".join(before.get("tags") or []))
                with right:
                    st.markdown("**Proposed Metadata**")
                    st.text_area(
                        "Proposed title",
                        value=str(proposed.get("title") or ""),
                        disabled=True,
                        key=f"strategy_after_title_{action_id}",
                    )
                    st.text_area(
                        "Proposed description",
                        value=str(proposed.get("description") or ""),
                        height=160,
                        disabled=True,
                        key=f"strategy_after_desc_{action_id}",
                    )
                    st.caption("Proposed tags: " + ", ".join(proposed.get("tags") or []))
                st.caption("Changes: " + ", ".join(evidence.get("changed_fields") or []))
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
                        with st.spinner("Checking live metadata, publishing, and verifying…"):
                            success, message = _publish_review_action(store, youtube, action)
                        if success:
                            st.success(message)
                        else:
                            st.error(message)
                        st.rerun()
                with reject_col:
                    if st.button("Skip this proposal", key=f"strategy_skip_{action_id}", use_container_width=True):
                        try:
                            store.update_strategy_action(action_id, store.owner_key, {
                                "status": "rejected",
                                "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                            })
                            st.toast("Proposal skipped. YouTube metadata was not changed.")
                            st.rerun()
                        except Exception as error:
                            st.error(f"Could not update the action log: {error}")
    else:
        st.caption("No metadata proposals are currently waiting for review.")

    with st.expander("Recent Strategist Audit Log & Rollback", expanded=False):
        if not recent_actions:
            st.caption("No metadata actions recorded yet.")
        else:
            for action in recent_actions:
                act_id = action.get("id")
                title = action.get("video_title") or "Untitled video"
                status_raw = action.get("status")
                status = _pretty_status(status_raw)
                cadence = (action.get("cadence") or "daily").title()
                st.markdown(f"**{title}** · {status} · `{cadence}`")
                st.caption(
                    f"{action.get('created_at', '')} · {action.get('classification', 'unknown')} · "
                    f"{action.get('reason', '')}"
                )
                if action.get("error"):
                    st.error(str(action["error"]))

                # Rollback action button if published
                if status_raw in {"auto_published", "approved_published", "shadow_published"} and youtube is not None:
                    col_roll, _ = st.columns([1, 3])
                    with col_roll:
                        if st.button(f"Rollback to original", key=f"strategy_rollback_{act_id}", help="Restore original before_metadata"):
                            with st.spinner("Rolling back metadata..."):
                                success, message = rollback_strategy_action(store, youtube, store.owner_key, act_id)
                            if success:
                                st.success(message)
                                st.rerun()
                            else:
                                st.error(message)

                audit = action.get("audit_log") or {}
                if audit:
                    st.json({
                        "result": audit.get("result"),
                        "timestamp": audit.get("timestamp"),
                        "before": audit.get("before"),
                        "after": audit.get("after"),
                    }, expanded=False)
                st.divider()
