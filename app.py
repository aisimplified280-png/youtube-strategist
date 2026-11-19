"""Main application shell for AI Simplified Lab — YouTube Metadata Copilot.

Single chat assistant for optimizing existing YouTube videos and Shorts:
1. Pastes YouTube link / ID -> automatically loads video details, real metrics, and transcript.
2. Formulates metadata improvements (Title with 3 distinct angles, Description with mobile hook, Hashtags, Tags).
3. Conversational revisions that invalidate previous approval digests.
4. Exact-proposal approval gate with pre-write stale check and post-write verification.
"""

import json
import time
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from core.auth import verify_owner_access
from core.creator_db import claim_legacy_owner_records
from core.oauth import test_or_refresh_credentials, build_flow, YOUTUBE_SCOPES, MEMORY_SCOPES, get_auth_url
from ui.theme import inject_theme
import oauth_token_store
import dashboard_security
from core.persistent_store import SupabasePersistentStore
from strategist_chat import render_copilot_chat
from ui.strategy_activity import render_strategy_activity
from ui.metadata_editor import render_metadata_editor
from ui.comments_inbox import render_comments_inbox


def _read_secrets_safe():
    try:
        if bool(st.secrets):
            return st.secrets
        return {}
    except Exception:
        return {}


def _handle_oauth_callback(secrets, subject, token_store):
    """Handle incoming OAuth redirect codes from YouTube or Memory flow."""
    query = st.query_params
    code = query.get("code")
    returned_state = query.get("state")
    if not code or not returned_state:
        return

    signing_secret = secrets.get("security", {}).get("state_secret", "")
    for purpose, pending_key in (("youtube", "pending_youtube_oauth_state"), ("memory", "pending_memory_oauth_state")):
        expected = st.session_state.get(pending_key)
        purpose_secret = dashboard_security.oauth_purpose_secret(signing_secret, purpose)
        if dashboard_security.consume_oauth_state(returned_state, expected, subject, purpose_secret):
            try:
                oauth_cfg = secrets.get(f"{purpose}_oauth") or secrets.get("youtube_oauth", {})
                client_id = oauth_cfg.get("client_id", "")
                client_secret = oauth_cfg.get("client_secret", "")
                redirect_uri = oauth_cfg.get("redirect_uri", "")

                scopes = YOUTUBE_SCOPES if purpose == "youtube" else MEMORY_SCOPES
                verifier = st.session_state.get(f"pending_{purpose}_code_verifier")
                if not verifier:
                    verifier = dashboard_security.create_oauth_code_verifier(returned_state, purpose_secret)

                flow = build_flow(
                    client_id,
                    client_secret,
                    redirect_uri,
                    scopes=scopes,
                    state=returned_state,
                    code_verifier=verifier,
                )
                flow.fetch_token(code=code)
                creds = flow.credentials
                creds_json = creds.to_json()
                if token_store:
                    token_store.save_credentials_json(subject, purpose, creds_json)
                st.session_state[f"{purpose}_credentials_json"] = creds_json
                st.toast(f"{purpose.title()} connected successfully!")
            except Exception as e:
                st.error(f"OAuth error: {str(e)}")
            finally:
                st.session_state.pop(pending_key, None)
                st.session_state.pop(f"pending_{purpose}_code_verifier", None)
                query.clear()
                st.rerun()


def render_app():
    # Inject visual styling
    inject_theme()

    secrets = _read_secrets_safe()
    # Discard stale flags from older app versions. They never bypass owner authorization.
    st.session_state.pop("demo_mode", None)

    # Access Gate
    authorized, subject, error_msg = verify_owner_access(secrets)

    # Signed-out or unauthorized visitor landing
    if not authorized:
        st.markdown(
            """
            <div style="text-align: center; max-width: 650px; margin: 40px auto;">
                <div style="display: inline-block; background: #FF1E44; color: white; width: 44px; height: 44px; border-radius: 12px; font-size: 22px; font-weight: 900; line-height: 44px; margin-bottom: 16px;">▶</div>
                <h1 style="font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 8px;">AI Simplified Lab</h1>
                <div style="font-size: 1.15rem; font-weight: 600; color: #E2E8F0; margin-bottom: 12px;">YouTube Metadata Copilot</div>
                <p style="color: #94A3B8; font-size: 1.05rem; line-height: 1.6; margin-bottom: 24px;">
                    A conversational YouTube metadata optimization assistant for the owner's existing YouTube videos and Shorts.
                    Paste any video or Short link to review content, formulate 3 distinct title angles, refine mobile descriptions, and apply approved metadata directly.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            st.markdown("### Channel Owner Sign-in")
            st.caption("Sign in with your configured Google Account to access channel telemetry and metadata co-pilot.")
            if error_msg:
                st.error(error_msg)

            try:
                st.button("Sign in with Google", type="primary", on_click=st.login, use_container_width=True)
            except Exception:
                st.caption("Streamlit auth provider not configured in local environment.")
        return

    # One-time owner-record migration; do not repeat database work on every Streamlit rerun.
    migration_key = f"_legacy_owner_claimed_{subject}"
    if authorized and subject and not st.session_state.get(migration_key):
        try:
            claim_legacy_owner_records(subject)
            st.session_state[migration_key] = True
        except Exception:
            # Retry on a later run if the migration call failed.
            pass

    # Use the last selected workspace to avoid initializing unrelated API clients.
    active_workspace_hint = st.session_state.get("active_workspace", "Metadata Copilot")

    # Vault & Token Management
    state_secret = secrets.get("security", {}).get("state_secret", "")
    token_store = None
    local_token_store = None
    supabase_cfg = secrets.get("supabase", {}) if hasattr(secrets, "get") else {}
    supabase_url = str(supabase_cfg.get("url") or secrets.get("SUPABASE_URL", "")).strip()
    supabase_service_key = str(
        supabase_cfg.get("service_role_key")
        or secrets.get("SUPABASE_SERVICE_ROLE_KEY", "")
        or secrets.get("service_role_key", "")
    ).strip()
    persistent_store = None
    if supabase_url and supabase_service_key and subject:
        try:
            cached_store = st.session_state.get("_copilot_persistent_store")
            cached_owner = st.session_state.get("_copilot_persistent_store_owner")
            if cached_store is None or cached_owner != subject:
                cached_store = SupabasePersistentStore(supabase_url, supabase_service_key, subject)
                st.session_state["_copilot_persistent_store"] = cached_store
                st.session_state["_copilot_persistent_store_owner"] = subject
            persistent_store = cached_store
        except Exception as error:
            st.session_state.pop("_copilot_persistent_store", None)
            st.session_state.pop("_copilot_persistent_store_owner", None)
            st.warning(f"Persistent history is temporarily unavailable: {error}")
    else:
        st.session_state.pop("_copilot_persistent_store", None)
        st.session_state.pop("_copilot_persistent_store_owner", None)

    if len(state_secret) >= 32:
        try:
            local_token_store = oauth_token_store.OAuthTokenStore(state_secret)
            if supabase_url and supabase_service_key:
                token_store = oauth_token_store.SupabaseOAuthTokenStore(
                    state_secret, supabase_url, supabase_service_key
                )
            else:
                token_store = local_token_store
        except Exception:
            token_store = local_token_store

    # Migrate the existing encrypted local OAuth grant into the shared vault once.
    migration_key = f"_oauth_vault_migrated_{subject}"
    if (
        subject and token_store and local_token_store
        and token_store is not local_token_store
        and not st.session_state.get(migration_key)
    ):
        try:
            for purpose in ("youtube", "memory"):
                if token_store.get_credentials_record(subject, purpose).get("status") == "missing":
                    local_record = local_token_store.get_credentials_record(subject, purpose)
                    if local_record.get("status") == "active":
                        token_store.save_credentials_json(subject, purpose, local_record["credentials_json"])
            st.session_state[migration_key] = True
        except Exception:
            # Retry a failed one-time migration on a later run.
            pass

    # Handle incoming OAuth callback
    if subject and token_store:
        _handle_oauth_callback(secrets, subject, token_store)

    # Restore only populated conversations. Empty drafts remain active in the current
    # session but are excluded from the persisted history list.
    if persistent_store:
        try:
            now = time.monotonic()
            cache = st.session_state.get("_copilot_conversations_cache") or {}
            if now - float(cache.get("loaded_at", 0)) >= 45:
                conversations = persistent_store.list_conversations(limit=15)
                st.session_state["_copilot_conversations_cache"] = {
                    "loaded_at": now,
                    "items": conversations,
                    "has_more": len(conversations) == 15,
                    "next_cursor": conversations[-1].get("updated_at") if conversations else None,
                }
            else:
                conversations = cache.get("items") or []

            previous_selected_id = st.session_state.get("_copilot_conversation_id")
            selected_id = previous_selected_id
            conversation_by_id = {
                str(item["id"]): item for item in conversations if item.get("id")
            }

            selected_conversation = None
            if selected_id and str(selected_id) not in conversation_by_id:
                selected_conversation = persistent_store.get_conversation(selected_id)
                if selected_conversation and not selected_conversation.get("archived_at"):
                    # Retain the active empty chat in the selector, but never add
                    # unrelated empty drafts to the recent-history list.
                    if selected_conversation.get("last_message_at"):
                        conversations = [selected_conversation, *conversations]
                    conversation_by_id[str(selected_id)] = selected_conversation
                else:
                    selected_id = None

            if not selected_id:
                if conversations:
                    selected_id = str(conversations[0]["id"])
                else:
                    created = persistent_store.create_conversation()
                    selected_id = str(created["id"])
                    selected_conversation = created
                st.session_state["_copilot_conversation_id"] = selected_id
                st.session_state["_copilot_history_loaded"] = False
                st.session_state["strategist_chat_history"] = []

            if str(previous_selected_id or "") != str(selected_id or ""):
                st.session_state["_copilot_history_loaded"] = False
                st.session_state["strategist_chat_history"] = []

            if selected_id and not st.session_state.get("_copilot_history_loaded"):
                messages = persistent_store.load_messages(selected_id, limit=50)
                st.session_state["strategist_chat_history"] = [
                    {"role": row["role"], "content": row["content"]} for row in messages
                ]
                st.session_state["_copilot_history_loaded"] = True

            # Keep the current empty chat selectable without listing old empty drafts.
            if selected_id and str(selected_id) not in conversation_by_id:
                selected_conversation = persistent_store.get_conversation(selected_id)
                if selected_conversation:
                    conversation_by_id[str(selected_id)] = selected_conversation

            if conversation_by_id:
                with st.sidebar:
                    st.markdown("### Conversations")
                    st.caption("Recent chats · empty drafts are hidden")
                    conversation_ids = list(conversation_by_id)
                    selected_from_sidebar = st.selectbox(
                        "Open conversation",
                        options=conversation_ids,
                        index=(
                            conversation_ids.index(str(selected_id))
                            if str(selected_id) in conversation_ids else 0
                        ),
                        format_func=lambda conversation_id: (
                            (
                                "New chat"
                                if not conversation_by_id[conversation_id].get("last_message_at")
                                else str(conversation_by_id[conversation_id].get("title") or "Untitled chat")[:48]
                            )
                            + "  ·  "
                            + str(
                                conversation_by_id[conversation_id].get("updated_at")
                                or conversation_by_id[conversation_id].get("created_at")
                                or ""
                            )[:10]
                        ),
                        label_visibility="collapsed",
                        key=f"_copilot_history_selector_{selected_id}",
                    )
                    if selected_from_sidebar and str(selected_from_sidebar) != str(selected_id):
                        st.session_state["_copilot_conversation_id"] = selected_from_sidebar
                        st.session_state["_copilot_history_loaded"] = False
                        st.session_state["strategist_chat_history"] = []
                        for key in (
                            "strategist_target_reference", "strategist_chat_proposal",
                            "strategist_chat_digest", "batch_metadata_review",
                            "batch_metadata_payload", "batch_flow_step",
                        ):
                            st.session_state.pop(key, None)
                        st.rerun()

                    current_cache = st.session_state.get("_copilot_conversations_cache") or {}
                    if current_cache.get("has_more") and st.button(
                        "Load more chats", key="copilot_load_more_chats", use_container_width=True
                    ):
                        cursor = current_cache.get("next_cursor") or (
                            conversations[-1].get("updated_at") if conversations else None
                        )
                        if cursor:
                            next_page = persistent_store.list_conversations(limit=15, before=cursor)
                            existing = {
                                str(item.get("id")) for item in current_cache.get("items", [])
                            }
                            merged = list(current_cache.get("items", [])) + [
                                item for item in next_page
                                if str(item.get("id")) not in existing
                            ]
                            st.session_state["_copilot_conversations_cache"] = {
                                "loaded_at": time.monotonic(),
                                "items": merged,
                                "has_more": len(next_page) == 15,
                                "next_cursor": next_page[-1].get("updated_at") if next_page else None,
                            }
                            st.rerun()
                    populated_count = len([
                        item for item in conversation_by_id.values()
                        if item.get("last_message_at")
                    ])
                    st.caption(f"Showing {populated_count} saved conversations")
        except Exception as error:
            st.session_state["_copilot_persistence_warning"] = (
                "Could not load saved chat history. Your current session remains available."
            )

    # Hydrate stored credentials
    yt_creds = None
    memory_creds = None
    if subject and token_store:
        yt_json = st.session_state.get("youtube_credentials_json")
        if not yt_json:
            yt_rec = token_store.get_credentials_record(subject, "youtube")
            if yt_rec.get("status") == "active":
                yt_json = yt_rec.get("credentials_json")
                st.session_state["youtube_credentials_json"] = yt_json

        if yt_json:
            try:
                raw_creds = Credentials.from_authorized_user_info(json.loads(yt_json))
                if not raw_creds.has_scopes(YOUTUBE_SCOPES):
                    st.session_state["youtube_needs_reconnect"] = True
                else:
                    yt_creds, status = test_or_refresh_credentials(raw_creds)
                    if status == "needs_reconnect":
                        st.session_state["youtube_needs_reconnect"] = True
                    else:
                        st.session_state.pop("youtube_needs_reconnect", None)
            except Exception:
                st.session_state["youtube_needs_reconnect"] = True

        mem_json = st.session_state.get("memory_credentials_json")
        if not mem_json:
            mem_rec = token_store.get_credentials_record(subject, "memory")
            if mem_rec.get("status") == "active":
                mem_json = mem_rec.get("credentials_json")
                st.session_state["memory_credentials_json"] = mem_json
        if mem_json:
            try:
                raw_mem = Credentials.from_authorized_user_info(json.loads(mem_json))
                memory_creds, _ = test_or_refresh_credentials(raw_mem)
            except Exception:
                pass

    # Build only the clients required by the active workspace. The reporting API
    # client was never consumed by the current chat or strategist renderers.
    youtube = analytics = reporting = None
    if yt_creds:
        try:
            cache = st.session_state.get("_youtube_api_client_cache") or {}
            cache_token = (getattr(yt_creds, "token", None), str(getattr(yt_creds, "expiry", None)))
            if cache.get("credential_fingerprint") == cache_token and cache.get("youtube") is not None:
                youtube = cache["youtube"]
                analytics = cache.get("analytics") if active_workspace_hint == "Metadata Copilot" else None
            else:
                youtube = build("youtube", "v3", credentials=yt_creds, cache_discovery=False)
                analytics = (
                    build("youtubeAnalytics", "v2", credentials=yt_creds, cache_discovery=False)
                    if active_workspace_hint == "Metadata Copilot" else None
                )
                st.session_state["_youtube_api_client_cache"] = {
                    "credential_fingerprint": cache_token,
                    "youtube": youtube,
                    "analytics": analytics,
                }
            if active_workspace_hint == "Metadata Copilot" and analytics is None:
                analytics = build("youtubeAnalytics", "v2", credentials=yt_creds, cache_discovery=False)
                st.session_state["_youtube_api_client_cache"]["analytics"] = analytics
        except Exception:
            youtube = None
            analytics = None

    # Persistent chat header: New chat + one direct YouTube connection control.
    col_brand, col_new_chat, col_youtube = st.columns([3.8, 1.2, 1.7], vertical_alignment="center")
    with col_brand:
        st.markdown(
            """
            <div style="display: flex; align-items: center; gap: 10px; padding: 4px 0;">
                <div style="background: #FF1E44; color: white; width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 15px;">▶</div>
                <div>
                    <span style="font-weight: 800; font-size: 1.1rem; letter-spacing: -0.02em; color: #FFFFFF;">AI Simplified Lab</span>
                    <span style="color: #64748B; margin: 0 6px;">/</span>
                    <span style="color: #94A3B8; font-size: 0.95rem; font-weight: 500;">Metadata &amp; Engagement Copilot</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_new_chat:
        if st.button("＋ New chat", key="btn_new_chat", use_container_width=True):
            for key in (
                "strategist_chat_history", "strategist_target_reference", "strategist_chat_proposal",
                "strategist_chat_digest", "copilot_proposal", "copilot_approval_digest",
                "strategist_pending_instruction", "copilot_initial_optimization",
                "copilot_channel_opportunities", "batch_metadata_review", "batch_metadata_payload",
                "batch_metadata_file_name", "batch_text_instructions_input", "batch_text_input_area",
                "batch_metadata_uploader", "batch_selected_video_ids", "batch_review_index",
                "batch_flow_step", "show_batch_metadata",
            ):
                st.session_state.pop(key, None)
            if persistent_store:
                try:
                    created = persistent_store.create_conversation()
                    st.session_state["_copilot_conversation_id"] = created["id"]
                    st.session_state["_copilot_history_loaded"] = True
                    st.session_state["strategist_chat_history"] = []
                    st.session_state.pop("_copilot_conversations_cache", None)
                except Exception as error:
                    st.warning(f"Could not create a saved conversation: {error}")
            st.rerun()

    with col_youtube:
        if yt_creds:
            st.button("✓ YouTube Connected", key="youtube_connected_button", disabled=True, use_container_width=True)
        else:
            yt_auth_url, yt_err = get_auth_url("youtube", subject, secrets)
            if yt_auth_url:
                btn_text = "↻ Reconnect YouTube" if st.session_state.get("youtube_needs_reconnect") else "▶ Connect YouTube"
                st.link_button(btn_text, yt_auth_url, type="primary", use_container_width=True)
            else:
                st.button("Connect YouTube", key="youtube_connect_unavailable", disabled=True, use_container_width=True)
                if yt_err:
                    st.caption(yt_err)

    # Render only the active workspace. st.tabs executes every tab body on each
    # rerun, which previously made chat interactions redraw the full strategist UI.
    active_workspace = st.segmented_control(
        "Workspace",
        options=["Metadata Copilot", "Metadata Editor", "Comments Inbox", "Channel Strategist"],
        default="Metadata Copilot",
        key="active_workspace",
        label_visibility="collapsed",
        width="stretch",
    )
    if active_workspace == "Metadata Copilot":
        render_copilot_chat(
            youtube=youtube,
            secrets=secrets,
            credentials=yt_creds,
            memory_credentials=memory_creds,
            analytics=analytics,
            reporting=reporting,
            subject=subject,
        )
    elif active_workspace == "Metadata Editor":
        render_metadata_editor(youtube, persistent_store, subject)
    elif active_workspace == "Comments Inbox":
        render_comments_inbox(youtube, persistent_store, subject, secrets)
    elif active_workspace == "Channel Strategist":
        render_strategy_activity(persistent_store, youtube=youtube)


def main():
    """Run one explicitly registered page and suppress Streamlit's legacy pages/ discovery."""
    st.set_page_config(
        page_title="AI Simplified Lab — YouTube Metadata Copilot",
        page_icon="▶",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    page = st.navigation(
        [st.Page(render_app, title="Metadata Copilot", icon="▶", default=True)],
        position="hidden",
    )
    page.run()


if __name__ == "__main__":
    main()
