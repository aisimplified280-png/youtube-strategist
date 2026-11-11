"""In-app review and audit panel for the autonomous channel strategist."""

from __future__ import annotations

import streamlit as st

import youtube_analytics_connector as connector


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
        after = connector.fetch_video_snippet(youtube, video_id)
        after_metadata = {
            "title": after.get("title") or "",
            "description": after.get("description") or "",
            "tags": list(after.get("tags") or []),
            "categoryId": after.get("categoryId") or "",
        }
        store.update_strategy_action(action_id, {
            "status": "approved_published",
            "after_metadata": after,
            "result": {"result": result.get("result"), "url": result.get("url")},
            "error": None,
            "executed_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return True, "Approved metadata update was published and verified."
    except connector.MetadataVerificationUnavailableError as error:
        store.update_strategy_action(action_id, {
            "status": "verification_unavailable",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return False, "The update may have reached YouTube, but read-back verification was unavailable. Reload the video before retrying."
    except Exception as error:
        store.update_strategy_action(action_id, {
            "status": "failed",
            "error": f"{type(error).__name__}: {str(error)[:1500]}",
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })
        return False, f"Publishing failed safely: {type(error).__name__}: {str(error)[:500]}"


def render_strategy_activity(store, youtube=None):
    """Render strategist automation controls, pending major proposals, and recent audit records."""
    if not store:
        return

    st.divider()
    st.markdown("### Channel Strategist")
    st.caption(
        "Daily analytics scan · Gemini reasoning · guarded minor updates · major changes require approval"
    )

    try:
        settings = store.get_strategy_settings()
    except Exception as error:
        st.warning(f"Strategist settings are unavailable: {error}")
        return

    with st.expander("Automation controls", expanded=False):
        auto_minor = st.checkbox(
            "Automatically publish strictly low-risk metadata fixes",
            value=bool(settings.get("auto_minor_enabled", True)),
            key="strategy_auto_minor_enabled",
            help="Only one-field meaning-preserving edits qualify. Word-level title or description changes always require approval.",
        )
        max_daily = st.slider(
            "Maximum automatic minor updates per day",
            min_value=0,
            max_value=10,
            value=max(0, min(int(settings.get("max_daily_minor_publishes", 3)), 10)),
            key="strategy_auto_minor_daily_cap",
        )
        max_review = st.slider(
            "Maximum metadata proposals prepared per daily scan",
            min_value=0,
            max_value=10,
            value=max(0, min(int(settings.get("max_metadata_reviews_per_run", 3)), 10)),
            key="strategy_metadata_review_cap",
        )
        if st.button("Save strategist settings", key="strategy_save_settings", type="primary"):
            try:
                store.set_strategy_settings({
                    "auto_minor_enabled": auto_minor,
                    "max_daily_minor_publishes": max_daily,
                    "max_metadata_reviews_per_run": max_review,
                })
                st.success("Strategist settings saved.")
                st.rerun()
            except Exception as error:
                st.error(f"Could not save strategist settings: {error}")

    try:
        runs = store.list_strategy_runs(limit=8)
        pending = store.list_strategy_actions(status="pending_review", limit=20)
        recent_actions = store.list_strategy_actions(limit=20)
    except Exception as error:
        st.warning(f"Could not load strategist activity logs: {error}")
        return

    if runs:
        latest = runs[0]
        metrics_period = latest.get("metrics_period") or {}
        result = latest.get("result") or {}
        cols = st.columns(4)
        cols[0].metric("Latest run", _pretty_status(latest.get("status")))
        cols[1].metric("Auto-published", int(result.get("auto_published") or 0))
        cols[2].metric("Awaiting review", int(result.get("pending_review") or 0))
        cols[3].metric("Issues", int(result.get("failed") or 0) + int(result.get("verification_unavailable") or 0))
        st.caption(
            f"Last run: {latest.get('started_at', 'Unknown')} · "
            f"Analytics period: {metrics_period.get('current_start', '?')} to {metrics_period.get('current_end', '?')} · "
            f"Model: {latest.get('model_name') or 'not recorded'}"
        )
        if latest.get("error"):
            st.warning(str(latest["error"]))
    else:
        st.info("No scheduled strategy run has been recorded yet. The daily trigger will populate this panel after its first successful run.")

    if pending:
        st.markdown("#### Major metadata changes awaiting approval")
        st.caption("These suggestions can change wording or meaning. Nothing in this queue is published automatically.")
        for index, action in enumerate(pending, 1):
            action_id = str(action.get("id") or f"pending-{index}")
            title = action.get("video_title") or "Untitled video"
            with st.expander(f"{index}. {title} · {_pretty_status(action.get('classification'))}", expanded=index == 1):
                video_id = action.get("video_id") or ""
                if video_id:
                    st.markdown(f"[Open video on YouTube](https://www.youtube.com/watch?v={video_id})")
                st.write(action.get("reason") or "No rationale was recorded.")
                evidence = action.get("evidence") or {}
                reasons = evidence.get("reasons") or []
                for reason in reasons:
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
                        height=160,
                        disabled=True,
                        key=f"strategy_before_desc_{action_id}",
                    )
                    st.caption("Current tags: " + ", ".join(before.get("tags") or []))
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
                            store.update_strategy_action(action_id, {
                                "status": "rejected",
                                "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                            })
                            st.toast("Proposal skipped. YouTube metadata was not changed.")
                            st.rerun()
                        except Exception as error:
                            st.error(f"Could not update the action log: {error}")
    else:
        st.caption("No major metadata proposals are waiting for review.")

    with st.expander("Recent strategist audit log", expanded=False):
        if not recent_actions:
            st.caption("No metadata actions have been recorded yet.")
        else:
            for action in recent_actions:
                title = action.get("video_title") or "Untitled video"
                status = _pretty_status(action.get("status"))
                st.markdown(f"**{title}** · {status}")
                st.caption(
                    f"{action.get('created_at', '')} · {action.get('classification', 'unknown')} · "
                    f"{action.get('reason', '')}"
                )
                if action.get("error"):
                    st.error(str(action["error"]))
                audit = action.get("audit_log") or {}
                if audit:
                    st.json({
                        "result": audit.get("result"),
                        "timestamp": audit.get("timestamp"),
                        "before": audit.get("before"),
                        "after": audit.get("after"),
                    }, expanded=False)
                st.divider()
