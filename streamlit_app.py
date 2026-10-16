"""Streamlit Community Cloud entry point for AI Simplified Lab YouTube Strategist."""

import copy
import difflib
import json
from datetime import date, timedelta

import httplib2
import requests
import streamlit as st
from google.auth.exceptions import GoogleAuthError, OAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from streamlit.errors import StreamlitSecretNotFoundError

import dashboard_security
import youtube_analytics_connector as connector
import youtube_dashboard
import strategist_chat

st.set_page_config(
    page_title="AI Simplified Lab YouTube Strategist",
    page_icon="📊",
    layout="wide",
)

st.title("AI Simplified Lab")
st.caption("Your YouTube channel, improved through conversation.")


def _read_secrets():
    required = {
        "auth": ("client_id", "client_secret", "cookie_secret", "redirect_uri",
                 "server_metadata_url"),
        "access": ("owner_email",),
        "youtube_oauth": ("client_id", "client_secret", "redirect_uri"),
        "security": ("state_secret",),
    }
    try:
        secret_store = st.secrets
        tuple(secret_store)
    except StreamlitSecretNotFoundError:
        st.error(
            "Streamlit Secrets are not configured. Add the required Web OAuth, "
            "owner identity, and security settings before using this dashboard."
        )
        st.stop()

    missing = []
    for section, keys in required.items():
        if section not in secret_store:
            missing.append(f"[{section}]")
            continue
        missing.extend(
            f"[{section}].{key}"
            for key in keys
            if not isinstance(secret_store[section].get(key), str)
            or not secret_store[section].get(key).strip()
        )
    if (
        "auth" in secret_store
        and "youtube_oauth" in secret_store
        and secret_store["auth"].get("client_id")
        != secret_store["youtube_oauth"].get("client_id")
    ):
        missing.append("matching OAuth client IDs in [auth] and [youtube_oauth]")
    if (
        "auth" in secret_store
        and "youtube_oauth" in secret_store
        and secret_store["auth"].get("client_secret")
        != secret_store["youtube_oauth"].get("client_secret")
    ):
        missing.append("matching OAuth client secrets in [auth] and [youtube_oauth]")
    if "auth" in secret_store:
        cookie_secret = secret_store["auth"].get("cookie_secret", "")
        if isinstance(cookie_secret, str) and len(cookie_secret) < 32:
            missing.append("[auth].cookie_secret (at least 32 characters)")
    if "security" in secret_store:
        state_secret = secret_store["security"].get("state_secret", "")
        if isinstance(state_secret, str) and len(state_secret) < 32:
            missing.append("[security].state_secret (at least 32 characters)")
    if missing:
        st.error(
            "Dashboard secrets are incomplete or inconsistent. Check "
            "`.streamlit/secrets.toml.example`; missing/invalid settings: "
            + ", ".join(missing)
        )
        st.stop()
    return secret_store


def _owner_identity(secrets):
    user = st.user
    email = getattr(user, "email", "")
    verified = getattr(user, "email_verified", False)
    expected_owner = secrets["access"].get("owner_email", "")
    if not dashboard_security.owner_is_authorized(email, verified, expected_owner):
        st.error(
            "Access denied. Sign in with the configured, verified Google owner account."
        )
        if getattr(user, "is_logged_in", False):
            st.button("Sign out", on_click=st.logout)
        st.stop()
    subject = getattr(user, "sub", "")
    if not subject:
        st.error("Google did not provide a stable account subject; access is blocked.")
        st.stop()
    return subject


def _flow_config(secrets):
    oauth = secrets["youtube_oauth"]
    return {
        "web": {
            "client_id": oauth["client_id"],
            "client_secret": oauth["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [oauth["redirect_uri"]],
        }
    }


def _new_google_flow(secrets, *, state=None, code_verifier=None):
    oauth = secrets["youtube_oauth"]
    return Flow.from_client_config(
        _flow_config(secrets),
        scopes=connector.SCOPES,
        state=state,
        redirect_uri=oauth["redirect_uri"],
        code_verifier=code_verifier,
    )


def _handle_youtube_callback(secrets, subject):
    query = st.query_params
    code = query.get("code")
    returned_state = query.get("state")
    oauth_error = query.get("error")
    if oauth_error:
        st.session_state.pop("pending_youtube_oauth_state", None)
        query.clear()
        st.error(
            "Google authorization was not completed. Reconnect YouTube when you are ready."
        )
        return
    if not code and not returned_state:
        return
    if not code or not returned_state:
        st.session_state.pop("pending_youtube_oauth_state", None)
        query.clear()
        st.error("Google returned an incomplete authorization response.")
        return

    expected_state = st.session_state.pop("pending_youtube_oauth_state", None)
    valid_state = dashboard_security.consume_oauth_state(
        returned_state,
        expected_state,
        subject,
        secrets["security"]["state_secret"],
    )
    if not valid_state:
        query.clear()
        st.error(
            "The authorization response expired or did not match the signed-in account. "
            "Start YouTube authorization again."
        )
        return

    code_verifier = dashboard_security.create_oauth_code_verifier(
        returned_state, secrets["security"]["state_secret"]
    )
    try:
        flow = _new_google_flow(
            secrets, state=returned_state, code_verifier=code_verifier
        )
        # The Flow already stores the configured redirect URI from _new_google_flow().
        # Do not pass it again to fetch_token(): requests-oauthlib supplies self.redirect_uri.
        flow.fetch_token(code=code)
    except (
        ValueError,
        GoogleAuthError,
        OAuthError,
        OAuth2Error,
        requests.RequestException,
    ) as error:
        # Show only a sanitized OAuth error code; never render the callback URL,
        # authorization code, client secret, or raw HTTP response in the UI.
        error_code = getattr(error, "error", None)
        if not isinstance(error_code, str) and isinstance(error, requests.RequestException):
            response = getattr(error, "response", None)
            if response is not None:
                try:
                    response_body = response.json()
                except (ValueError, TypeError):
                    response_body = {}
                if isinstance(response_body, dict):
                    error_code = response_body.get("error")
        safe_codes = {
            "invalid_client": "Google rejected the OAuth client credentials. Verify that the Web application client ID and secret in Streamlit Secrets belong to the same Google Cloud OAuth client.",
            "invalid_grant": "Google rejected this authorization code or its PKCE verifier. Start a fresh authorization attempt and verify the redirect URI exactly matches the configured URI.",
            "redirect_uri_mismatch": "The redirect URI does not match. Verify the authorized redirect URI in Google Cloud and the [youtube_oauth].redirect_uri value in Streamlit Secrets.",
            "unauthorized_client": "This OAuth client is not permitted to use the requested flow. Verify that it is a Web application client and the YouTube APIs/scopes are configured.",
        }
        query.clear()
        if not (
            isinstance(error_code, str)
            and 0 < len(error_code) <= 64
            and all(
                character.isascii()
                and (character.isalnum() or character in "._-")
                for character in error_code
            )
        ):
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            error_code = (
                f"HTTP {status}"
                if isinstance(status, int)
                else type(error).__name__
            )
        st.error(
            "Google authorization exchange failed. "
            + safe_codes.get(
                error_code,
                "Verify the hosted Web OAuth client ID/secret and exact redirect URI, then start a fresh authorization attempt."
            )
        )
        category = error_code if error_code in safe_codes else type(error).__name__
        st.caption(f"Diagnostic category: {category}")
        return
    except HttpError as error:
        query.clear()
        status = getattr(error.resp, "status", "unknown")
        st.error(f"Google authorization exchange failed (HTTP {status}).")
        return

    credentials = flow.credentials
    if not credentials.has_scopes(connector.SCOPES):
        query.clear()
        st.error(
            "The granted authorization is missing required YouTube scopes. "
            "Reconnect and approve YouTube Analytics read access and YouTube metadata access."
        )
        return
    st.session_state["youtube_credentials_json"] = credentials.to_json()
    query.clear()
    st.session_state["oauth_connected_message"] = True
    st.rerun()


def _credentials_from_session():
    credentials_json = st.session_state.get("youtube_credentials_json")
    if not credentials_json:
        return None
    try:
        credentials = Credentials.from_authorized_user_info(
            json.loads(credentials_json),
            connector.SCOPES,
        )
    except (ValueError, KeyError, TypeError):
        st.session_state.pop("youtube_credentials_json", None)
        return None
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            st.session_state["youtube_credentials_json"] = credentials.to_json()
            return credentials
        except (GoogleAuthError, requests.RequestException):
            st.session_state.pop("youtube_credentials_json", None)
            st.session_state["youtube_auth_warning"] = (
                "Saved YouTube authorization could not be refreshed. Authorize YouTube "
                "again for this session."
            )
            return None
    if credentials.expired:
        st.session_state.pop("youtube_credentials_json", None)
        st.session_state["youtube_auth_warning"] = (
            "Saved YouTube authorization expired without a refresh token. Authorize "
            "YouTube again for this session."
        )
        return None
    st.session_state.pop("youtube_credentials_json", None)
    return None


def _start_youtube_authorization(secrets, subject):
    signing_secret = secrets["security"]["state_secret"]
    pending_state = st.session_state.get("pending_youtube_oauth_state")
    state = dashboard_security.reuse_or_create_oauth_state(
        pending_state,
        subject,
        signing_secret,
    )
    st.session_state["pending_youtube_oauth_state"] = state
    code_verifier = dashboard_security.create_oauth_code_verifier(
        state, signing_secret
    )
    flow = _new_google_flow(
        secrets, state=state, code_verifier=code_verifier
    )
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    st.link_button(
        "Authorize YouTube Analytics and metadata access",
        authorization_url,
        type="primary",
        help=(
            "Google will return to the configured Streamlit app URL. "
            "This grants read-only Analytics access and snippet metadata editing."
        ),
    )
    st.caption(
        "The Google OAuth client is held in Streamlit Secrets. Tokens remain in this "
        "owner's server-side Streamlit session and are never displayed or written to disk."
    )


def _display_metric(value):
    return "Unavailable" if value is None else f"{value:,}"


def _visible_video_rows(rows, format_filter):
    if format_filter == "All":
        return rows
    return [row for row in rows if row["format"] == format_filter]


def _proposal_digest(video_id, before, proposed):
    return connector.metadata_proposal_digest(video_id, before, proposed)


def _append_session_audit(entry):
    records = st.session_state.setdefault("session_audit_records", [])
    records.insert(0, entry)
    del records[20:]


def _selected_analytics_details(analytics, video_id, start_date, end_date):
    key = (video_id, start_date.isoformat(), end_date.isoformat())
    cached = st.session_state.get("selected_analytics_details")
    if cached and cached["key"] == key:
        return cached
    traffic_rows, traffic_status = youtube_dashboard.fetch_video_traffic_sources(
        analytics,
        video_id,
        start_date,
        end_date,
    )
    audience_rows, audience_status = (
        youtube_dashboard.fetch_video_audience_demographics(
            analytics,
            video_id,
            start_date,
            end_date,
        )
    )
    details = {
        "key": key,
        "traffic_rows": traffic_rows,
        "traffic_status": traffic_status,
        "audience_rows": audience_rows,
        "audience_status": audience_status,
    }
    st.session_state["selected_analytics_details"] = details
    return details


def _render_proposal(video_row, youtube):
    video_id = video_row["video_id"]
    snippet = video_row["snippet"]
    rejected_video_ids = st.session_state.setdefault("rejected_video_ids", set())
    if video_id in rejected_video_ids:
        st.info("This video's proposal was rejected. No YouTube changes were made.")
        if st.button(
            "Prepare a new proposal",
            key=f"new_proposal_{video_id}",
        ):
            rejected_video_ids.remove(video_id)
            st.rerun()
        return

    pending = st.session_state.get("pending_metadata_proposal")
    if pending and pending["video_id"] != video_id:
        st.session_state.pop("pending_metadata_proposal", None)
        pending = None
    if pending is None:
        try:
            generated = youtube_dashboard.generate_metadata_proposal(snippet)
        except connector.MetadataValidationError as error:
            st.warning(
                "The current metadata did not allow a safe automatic suggestion: "
                f"{error} You can edit a proposal below; it must pass validation "
                "before it can be approved."
            )
            generated = {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "tags": snippet.get("tags", []),
                "explanation": "No automatic changes were suggested.",
            }
        before = copy.deepcopy(snippet)
        proposed = copy.deepcopy(before)
        for field in ("title", "description", "tags"):
            proposed[field] = generated[field]
        pending = {
            "video_id": video_id,
            "before": before,
            "proposed": proposed,
            "explanation": generated["explanation"],
            "digest": _proposal_digest(video_id, before, proposed),
        }
        st.session_state["pending_metadata_proposal"] = pending

    proposed = pending["proposed"]
    st.subheader("Metadata proposal")
    st.caption(pending["explanation"])
    current_column, proposed_column = st.columns(2)
    with current_column:
        st.markdown("**Current metadata**")
        st.text_area(
            "Current title",
            pending["before"].get("title", ""),
            disabled=True,
            key=f"current_title_{video_id}",
        )
        st.text_area(
            "Current description",
            pending["before"].get("description", ""),
            disabled=True,
            key=f"current_description_{video_id}",
        )
        st.text_area(
            "Current YouTube tags (one per line)",
            "\n".join(pending["before"].get("tags", [])),
            disabled=True,
            key=f"current_tags_{video_id}",
        )
    with proposed_column:
        st.markdown("**Proposed metadata — edit these fields, then save before approval**")
        title_key = f"proposed_title_{video_id}"
        description_key = f"proposed_description_{video_id}"
        tags_key = f"proposed_tags_{video_id}"
        st.session_state.setdefault(title_key, proposed.get("title", ""))
        st.session_state.setdefault(
            description_key, proposed.get("description", "")
        )
        st.session_state.setdefault(tags_key, "\n".join(proposed.get("tags", [])))
        title_value = st.text_area("Proposed title", key=title_key, height=80)
        description_value = st.text_area(
            "Proposed complete description", key=description_key, height=240
        )
        tags_value = st.text_area(
            "Proposed YouTube tags (one per line)", key=tags_key, height=120
        )

    proposed_tags = [
        tag.strip()
        for tag in tags_value.splitlines()
        if tag.strip()
    ]
    live_proposed = copy.deepcopy(pending["before"])
    live_proposed.update({
        "title": title_value,
        "description": description_value,
        "tags": proposed_tags,
    })
    live_digest = _proposal_digest(video_id, pending["before"], live_proposed)
    has_unsaved_edits = live_digest != pending["digest"]
    validation_error = None
    try:
        connector._validate_metadata(live_proposed)
    except connector.MetadataValidationError as error:
        validation_error = str(error)
        st.warning(f"Proposal validation: {validation_error}")
    if has_unsaved_edits:
        st.warning(
            "The displayed proposal has been edited. Save the edits to create a new "
            "proposal; any previous approval is invalidated."
        )

    with st.expander("Readable metadata diff", expanded=True):
        before_text = json.dumps(
            connector._metadata_snapshot(pending["before"]),
            indent=2,
            ensure_ascii=False,
        ).splitlines()
        after_text = json.dumps(
            connector._metadata_snapshot(live_proposed),
            indent=2,
            ensure_ascii=False,
        ).splitlines()
        diff = difflib.unified_diff(
            before_text,
            after_text,
            fromfile="current",
            tofile="proposed",
            lineterm="",
        )
        st.code("\n".join(diff) or "(no metadata changes)", language="diff")

    if st.button(
        "Save edits as a new proposal",
        key=f"save_proposal_{video_id}",
        disabled=not has_unsaved_edits,
    ):
        try:
            connector._validate_metadata(live_proposed)
        except connector.MetadataValidationError as error:
            st.error(str(error))
        else:
            pending["proposed"] = live_proposed
            pending["digest"] = live_digest
            pending["explanation"] = (
                "Edited by the owner. Re-review this exact proposal before applying."
            )
            st.session_state["pending_metadata_proposal"] = pending
            st.rerun()

    approve_column, reject_column = st.columns(2)
    with approve_column:
        approved = st.button(
            "Approve and apply this exact proposal",
            type="primary",
            key=f"approve_proposal_{video_id}",
            disabled=has_unsaved_edits or validation_error is not None,
        )
    with reject_column:
        rejected = st.button(
            "Reject proposal",
            key=f"reject_proposal_{video_id}",
        )

    if rejected:
        st.session_state.pop("pending_metadata_proposal", None)
        rejected_video_ids.add(video_id)
        for key in (title_key, description_key, tags_key):
            st.session_state.pop(key, None)
        st.info("Proposal rejected. No YouTube changes were made.")
        st.rerun()

    if approved:
        if live_digest != pending["digest"]:
            st.error("The proposal changed. Save and review it before approving.")
            return
        try:
            result = connector.apply_approved_dashboard_proposal(
                youtube,
                video_id,
                pending["before"],
                pending["proposed"],
                pending["digest"],
                audit_callback=_append_session_audit,
            )
        except HttpError as error:
            status = getattr(error.resp, "status", "unknown")
            st.error(
                f"YouTube API rejected the approved update (HTTP {status}). "
                "Check access, OAuth scopes, or quota; no success is claimed."
            )
        except (
            GoogleAuthError,
            httplib2.HttpLib2Error,
            OSError,
            requests.RequestException,
        ):
            st.error(
                "The YouTube update could not be completed because the Google API "
                "request failed. No automatic retry was attempted."
            )
        except (
            connector.MetadataValidationError,
            connector.VideoNotFoundError,
            RuntimeError,
            ValueError,
        ) as error:
            st.error(str(error))
        else:
            st.session_state["pending_metadata_proposal"] = None
            st.session_state["last_update_result"] = result
            st.session_state.pop("dashboard_data_key", None)
            st.rerun()


def main():
    secrets = _read_secrets()
    if not getattr(st.user, "is_logged_in", False):
        st.info("Sign in with the configured Google account to access this dashboard.")
        st.button("Sign in with Google", on_click=st.login)
        return

    subject = _owner_identity(secrets)
    if st.session_state.pop("oauth_connected_message", False):
        st.success("YouTube authorization connected for this session.")

    _handle_youtube_callback(secrets, subject)
    credentials = _credentials_from_session()
    auth_warning = st.session_state.pop("youtube_auth_warning", None)
    if auth_warning:
        st.warning(auth_warning)

    with st.sidebar:
        st.caption(f"Signed in as {st.user.email}")
        st.button("Sign out", on_click=st.logout)
        if credentials:
            if st.button("Disconnect YouTube for this session"):
                st.session_state.pop("youtube_credentials_json", None)
                st.session_state.pop("dashboard_data", None)
                st.rerun()

    if not credentials:
        st.info(
            "Connect YouTube to grant read-only Analytics access and permission to edit "
            "approved video titles, descriptions, and tags."
        )
        _start_youtube_authorization(secrets, subject)
        return

    if not st.session_state.get("session_access_warning_shown"):
        st.warning(
            "Approvals and audit entries are held only in this authenticated browser "
            "session. This free deployment does not provide durable audit storage."
        )
        st.session_state["session_access_warning_shown"] = True

    try:
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        analytics = build(
            "youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False
        )
        reporting = build(
            "youtubereporting", "v1", credentials=credentials, cache_discovery=False
        )
    except (ValueError, HttpError):
        st.error("Could not initialize Google's YouTube API clients.")
        return

    with st.container(border=True):
        strategist_chat.render_chat(youtube, secrets)

    with st.expander("Channel analytics (optional)", expanded=False):
        st.caption("Read-only performance reports. Request all metadata changes in the Strategist chat above.")
        yesterday = date.today() - timedelta(days=1)
        default_start, default_end = youtube_dashboard.default_date_range()
        with st.sidebar:
            st.header("Analytics range")
            start_date = st.date_input(
                "Start date",
                value=default_start,
                max_value=yesterday,
                key="dashboard_start_date",
            )
            end_date = st.date_input(
                "End date",
                value=default_end,
                max_value=yesterday,
                key="dashboard_end_date",
            )
            video_limit = st.selectbox(
                "Recent uploads to analyze",
                options=(25, 50, 100, 200, 250),
                index=2,
            )
            refresh_reports = st.button("Refresh channel reports")
        if refresh_reports:
            st.session_state.pop("dashboard_data_key", None)
        if start_date > end_date:
            st.error("Choose a start date on or before the end date.")
            return
        if end_date >= date.today():
            st.error("YouTube Analytics data is queried only through yesterday.")
            return

        data_key = (start_date.isoformat(), end_date.isoformat(), video_limit)
        if st.session_state.get("dashboard_data_key") != data_key:
            with st.spinner("Fetching channel reports and recent video performance..."):
                try:
                    dashboard_data = youtube_dashboard.load_dashboard_data(
                        youtube,
                        analytics,
                        start_date,
                        end_date,
                        video_limit=video_limit,
                        reporting=reporting,
                        credentials=credentials,
                    )
                except HttpError as error:
                    status = getattr(error.resp, "status", "unknown")
                    if status == 403:
                        st.error(
                            "Google denied an Analytics or Data API request (HTTP 403). "
                            "Check the authorized Google account, API enablement, scopes, "
                            "and quota."
                        )
                    else:
                        st.error(f"YouTube API request failed (HTTP {status}).")
                    return
                except (
                    GoogleAuthError,
                    httplib2.HttpLib2Error,
                    OSError,
                    requests.RequestException,
                ):
                    st.error(
                        "The Google API request failed. Check the network and reconnect "
                        "YouTube if the authorization expired."
                    )
                    return
                except (
                    connector.VideoNotFoundError,
                    connector.MetadataValidationError,
                    RuntimeError,
                    ValueError,
                ) as error:
                    st.error(str(error))
                    return
            st.session_state["dashboard_data"] = dashboard_data
            st.session_state["dashboard_data_key"] = data_key
            st.session_state.pop("pending_metadata_proposal", None)
            st.session_state.pop("requested_video_row", None)

        data = st.session_state["dashboard_data"]
        st.caption(
            f"Channel `{data['channel_id']}` · {data['period']['start']} through "
            f"{data['period']['end']}"
        )
        if data["summary"] is None:
            st.warning(f"Channel summary: {data['summary_status']}")
        else:
            summary = data["summary"]
            columns = st.columns(8)
            for column, metric in zip(columns, youtube_dashboard.CORE_METRICS):
                column.metric(
                    youtube_dashboard.METRIC_LABELS[metric],
                    _display_metric(summary.get(metric)),
                )

        with st.expander("Analytics coverage and limitations"):
            st.write("Thumbnail reach report:", data["reach_status"])
            st.write(
                "The YouTube Reporting API reach report supplies thumbnail impressions "
                "and CTR independently of Analytics reports.query. Daily reports are "
                "asynchronous; first availability may take up to 48 hours. Reach values "
                "sum returned video/date rows only; missing rows are not converted to zero."
            )
            st.write("Daily report:", data["daily_status"])
            st.write("Traffic-source report:", data["traffic_status"])
            st.write("Video catalogue:", data["catalogue_note"])
            st.write(
                "Format is inferred from duration only: <=180 seconds is a short-duration "
                "candidate, not a confirmed Short; video orientation is not exposed here."
            )
            for warning in data["api_warnings"]:
                st.warning(warning)

        st.subheader("Channel views by day")
        if data["daily_rows"]:
            st.line_chart({
                row["day"]: row["views"]
                for row in data["daily_rows"]
                if row.get("day") is not None and row.get("views") is not None
            })
        else:
            st.caption(data["daily_status"])

        with st.expander("Channel traffic sources"):
            if data["traffic_sources"]:
                st.dataframe(data["traffic_sources"], hide_index=True, use_container_width=True)
            else:
                st.caption(data["traffic_status"])

        st.subheader("Video and short-duration candidate performance")
        format_filter = st.selectbox(
            "Format filter",
            options=(
                "All",
                "Short-duration candidate (<=180 sec)",
                "Long-form (>180 sec)",
            ),
        )
        visible_rows = _visible_video_rows(data["video_rows"], format_filter)
        table_columns = (
            "title",
            "published_at",
            "format",
            "duration_seconds",
            *youtube_dashboard.CORE_METRICS,
            "impressions",
            "ctr",
        )
        st.dataframe(
            [
                {
                    column: (
                        f"{row[column]:,}"
                        if column == "impressions" and row.get(column) is not None
                        else f"{row[column]:g}%"
                        if column == "ctr" and row.get(column) is not None
                        else "Unavailable"
                        if column in {"impressions", "ctr"}
                        else row.get(column)
                    )
                    for column in table_columns
                }
                for row in visible_rows
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "title": st.column_config.TextColumn("Video"),
                "published_at": st.column_config.TextColumn("Published"),
                "format": st.column_config.TextColumn("Format (duration-based)"),
                "duration_seconds": st.column_config.NumberColumn("Duration (sec)"),
                "averageViewDuration": st.column_config.NumberColumn(
                    "Avg. view duration (sec)"
                ),
                "estimatedMinutesWatched": st.column_config.NumberColumn(
                    "Watch time (min)"
                ),
                "impressions": st.column_config.TextColumn("Impressions"),
                "ctr": st.column_config.TextColumn("CTR"),
            },
        )
        st.caption(
            "Blank Analytics cells mean the report returned no row for that video/date range; "
            "they are not treated as zero. CTR is YouTube's reported percentage, not an estimate."
        )
        with st.expander("Impressions and CTR availability by video"):
            for row in visible_rows:
                if row.get("impressions") is None or row.get("ctr") is None:
                    st.markdown(f"**{row['title']}**")
                    if row.get("impressions") is None:
                        st.write("Impressions:", row["impressions_status"])
                    if row.get("ctr") is None:
                        st.write("CTR:", row["ctr_status"])

        st.subheader("Metadata optimization candidates")
        ranked = youtube_dashboard.rank_optimization_candidates(data["video_rows"])
        if ranked:
            for candidate in ranked:
                st.markdown(
                    f"- [{candidate['title']}]({candidate['url']}) — "
                    f"{candidate['recommendation_evidence']}"
                )
        else:
            st.info(
                "No measured video has at least three age- and format-matched peers with a "
                "lower view count than its peer median in this date range. No opportunity "
                "is inferred from missing data."
            )

        if not visible_rows:
            st.info("No videos are available in this catalogue/filter.")

        with st.expander("This session's metadata audit entries"):
            audit_entries = st.session_state.get("session_audit_records", [])
            if audit_entries:
                safe_entries = [
                    {
                        "timestamp": entry["timestamp"],
                        "video_id": entry["video_id"],
                        "url": entry["video_url"],
                        "proposal_id": entry["proposal_id"],
                        "result": entry["result"],
                    }
                    for entry in audit_entries
                ]
                st.dataframe(safe_entries, hide_index=True, use_container_width=True)
            else:
                st.caption("No metadata update has been approved in this session.")
            st.caption(
                "Audit entries are in-memory session state only and are lost when this "
                "session expires. Configure durable private storage before relying on audit history."
            )

        last_result = st.session_state.pop("last_update_result", None)
        if last_result:
            st.success(
                f"Verified update for **{last_result['title']}**: "
                f"{last_result['url']}"
            )


main()
