"""Server-side Supabase persistence for chat history and resumable batch jobs.

Only use with a service-role key from Streamlit Secrets or worker environment.
Never send this key to browser/client-side code or log it.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote

import requests


class PersistentStoreError(RuntimeError):
    pass


class SupabasePersistentStore:
    def __init__(self, url: str, service_role_key: str, owner_subject: str, timeout: int = 20):
        if not url or not service_role_key or not owner_subject:
            raise ValueError("Supabase URL, service-role key, and verified owner subject are required.")
        self.url = url.rstrip("/")
        self.key = service_role_key
        self.owner_key = hashlib.sha256(owner_subject.encode("utf-8")).hexdigest()
        self.timeout = timeout

    def _request(self, method: str, path: str, *, params=None, payload=None, prefer=None):
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        try:
            response = requests.request(
                method, f"{self.url}/rest/v1/{path}", headers=headers,
                params=params, json=payload, timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            if getattr(exc, "response", None) is not None:
                detail = (exc.response.text or "")[:500]
            raise PersistentStoreError(f"Supabase persistence request failed: {detail or type(exc).__name__}") from exc
        if not response.content:
            return []
        try:
            return response.json()
        except ValueError:
            return []

    def create_conversation(self, title: str = "New chat") -> Dict[str, Any]:
        rows = self._request("POST", "copilot_conversations",
            payload={"owner_key": self.owner_key, "title": (title or "New chat")[:160]},
            prefer="return=representation")
        if not rows:
            raise PersistentStoreError("Supabase did not return the created conversation.")
        return rows[0]

    def list_conversations(self, limit: int = 30) -> list[Dict[str, Any]]:
        return self._request("GET", "copilot_conversations", params={
            "owner_key": f"eq.{self.owner_key}", "archived_at": "is.null",
            "select": "id,title,created_at,updated_at", "order": "updated_at.desc",
            "limit": str(max(1, min(int(limit), 100))),
        })

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        rows = self._request("GET", "copilot_conversations", params={
            "id": f"eq.{conversation_id}", "owner_key": f"eq.{self.owner_key}",
            "select": "*", "limit": "1",
        })
        return rows[0] if rows else None

    def load_messages(self, conversation_id: str, limit: int = 100) -> list[Dict[str, Any]]:
        return self._request("GET", "copilot_messages", params={
            "conversation_id": f"eq.{conversation_id}", "owner_key": f"eq.{self.owner_key}",
            "select": "id,role,content,metadata,created_at", "order": "created_at.asc",
            "limit": str(max(1, min(int(limit), 500))),
        })

    def append_message(self, conversation_id: str, role: str, content: str, metadata=None):
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Unsupported chat message role.")
        rows = self._request("POST", "copilot_messages", payload={
            "conversation_id": conversation_id, "owner_key": self.owner_key,
            "role": role, "content": str(content), "metadata": metadata or {},
        }, prefer="return=representation")
        self._request("PATCH", "copilot_conversations", params={
            "id": f"eq.{conversation_id}", "owner_key": f"eq.{self.owner_key}",
        }, payload={"updated_at": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")
        return rows[0] if rows else None

    def enqueue_batch(self, conversation_id: Optional[str], instruction: str,
                      parsed_batch: Dict[str, Any], source_type: str = "text") -> Dict[str, Any]:
        input_rows = parsed_batch.get("rows") or []
        job_rows = self._request("POST", "batch_jobs", payload={
            "owner_key": self.owner_key, "conversation_id": conversation_id,
            "status": "queued", "source_type": source_type,
            "instruction_text": instruction or "",
            "input_payload": parsed_batch,
            "total_count": len(input_rows),
        }, prefer="return=representation")
        if not job_rows:
            raise PersistentStoreError("Supabase did not return the queued batch.")
        job = job_rows[0]
        rows_payload = [{
            "job_id": job["id"], "owner_key": self.owner_key,
            "row_number": int(row.get("row_number") or index),
            "video_id": row.get("video_id"),
            "video_title": row.get("context_title") or row.get("reference") or None,
            "status": "queued" if row.get("status") == "parsed" else "invalid",
            "input_payload": row,
            "errors": row.get("errors") or [],
        } for index, row in enumerate(input_rows, 1)]
        if rows_payload:
            self._request("POST", "batch_job_rows", payload=rows_payload, prefer="return=minimal")
        return job

    def list_jobs(self, limit: int = 30) -> list[Dict[str, Any]]:
        return self._request("GET", "batch_jobs", params={
            "owner_key": f"eq.{self.owner_key}",
            "select": "id,conversation_id,created_at,source_type,status,total_count,processed_count,failed_count,last_error,started_at,finished_at,updated_at",
            "order": "created_at.desc", "limit": str(max(1, min(int(limit), 100))),
        })

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        rows = self._request("GET", "batch_jobs", params={
            "id": f"eq.{job_id}", "owner_key": f"eq.{self.owner_key}",
            "select": "*", "limit": "1",
        })
        return rows[0] if rows else None

    def get_job_rows(self, job_id: str) -> list[Dict[str, Any]]:
        return self._request("GET", "batch_job_rows", params={
            "job_id": f"eq.{job_id}", "owner_key": f"eq.{self.owner_key}",
            "select": "id,video_id,video_title,status,before_metadata,proposed_metadata,proposal_details,approval_digest,errors,attempt_count",
            "order": "row_number.asc",
        })

    def update_job(self, job_id: str, values: Dict[str, Any]):
        safe = {k: v for k, v in values.items() if k in {
            "status", "processed_count", "failed_count", "last_error",
            "worker_id", "lease_expires_at", "started_at", "finished_at", "updated_at",
        }}
        return self._request("PATCH", "batch_jobs", params={
            "id": f"eq.{job_id}", "owner_key": f"eq.{self.owner_key}",
        }, payload=safe, prefer="return=representation")

    def update_job_row(self, row_id: str, values: Dict[str, Any]):
        safe = {k: v for k, v in values.items() if k in {
            "status", "video_title", "before_metadata", "proposed_metadata",
            "errors", "approval_digest", "proposal_details", "started_at", "finished_at", "updated_at",
        }}
        return self._request("PATCH", "batch_job_rows", params={
            "id": f"eq.{row_id}", "owner_key": f"eq.{self.owner_key}",
        }, payload=safe, prefer="return=representation")

    def get_encrypted_credentials(self, purpose: str) -> Optional[str]:
        rows = self._request("GET", "owner_oauth_credentials", params={
            "owner_key": f"eq.{self.owner_key}", "purpose": f"eq.{purpose}",
            "select": "encrypted_payload,revoked", "limit": "1",
        })
        if not rows or rows[0].get("revoked"):
            return None
        return rows[0].get("encrypted_payload")

    def save_encrypted_credentials(self, purpose: str, encrypted_payload: str):
        if purpose not in {"youtube", "memory"}:
            raise ValueError("Unsupported OAuth purpose.")
        return self._request("POST", "owner_oauth_credentials", params={
            "on_conflict": "owner_key,purpose",
        }, payload={
            "owner_key": self.owner_key, "purpose": purpose,
            "encrypted_payload": encrypted_payload, "revoked": False,
        }, prefer="resolution=merge-duplicates,return=minimal")

    def revoke_credentials(self, purpose: str):
        return self._request("PATCH", "owner_oauth_credentials", params={
            "owner_key": f"eq.{self.owner_key}", "purpose": f"eq.{purpose}",
        }, payload={"revoked": True}, prefer="return=minimal")

    @classmethod
    def worker_client(cls, url: str, service_role_key: str):
        """Create a worker-only REST client without an owner filter."""
        instance = object.__new__(cls)
        instance.url = url.rstrip("/")
        instance.key = service_role_key
        instance.owner_key = ""
        instance.timeout = 20
        return instance

    def claim_next_job(self, worker_id: str, lease_seconds: int = 240):
        rows = self._request("POST", "rpc/claim_next_batch_job", payload={
            "p_worker_id": worker_id, "p_lease_seconds": lease_seconds,
        })
        return rows[0] if rows else None



    # --- Autonomous channel strategist -------------------------------------------------

    def get_strategy_settings(self, owner_key: Optional[str] = None) -> Dict[str, Any]:
        owner = owner_key or self.owner_key
        if not owner:
            raise PersistentStoreError("A verified owner is required for strategy settings.")
        rows = self._request("GET", "owner_strategy_settings", params={
            "owner_key": f"eq.{owner}",
            "select": "owner_key,auto_minor_enabled,max_daily_minor_publishes,max_metadata_reviews_per_run,automation_mode,emergency_pause,min_confidence_auto_publish,max_weekly_publishes,max_monthly_publishes,updated_at",
            "limit": "1",
        })
        if rows:
            row = rows[0]
            # Ensure defaults for newly added fields if existing row lacks them
            row.setdefault("automation_mode", "shadow")
            row.setdefault("emergency_pause", False)
            row.setdefault("min_confidence_auto_publish", 0.85)
            row.setdefault("max_weekly_publishes", 5)
            row.setdefault("max_monthly_publishes", 10)
            return row
        # Conservative defaults for new owner
        return {
            "owner_key": owner,
            "automation_mode": "shadow",
            "emergency_pause": False,
            "min_confidence_auto_publish": 0.85,
            "auto_minor_enabled": True,
            "max_daily_minor_publishes": 3,
            "max_weekly_publishes": 5,
            "max_monthly_publishes": 10,
            "max_metadata_reviews_per_run": 3,
        }

    def set_strategy_settings(self, values: Dict[str, Any], owner_key: Optional[str] = None):
        owner = owner_key or self.owner_key
        if not owner:
            raise PersistentStoreError("A verified owner is required for strategy settings.")
        enabled = values.get("auto_minor_enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("auto_minor_enabled must be a boolean.")
        automation_mode = str(values.get("automation_mode", "shadow")).strip().lower()
        if automation_mode not in {"paused", "shadow", "autonomous"}:
            raise ValueError("automation_mode must be 'paused', 'shadow', or 'autonomous'.")
        emergency_pause = bool(values.get("emergency_pause", False))
        min_conf = float(values.get("min_confidence_auto_publish", 0.85))
        min_conf = max(0.50, min(min_conf, 1.00))
        daily_cap = max(0, min(int(values.get("max_daily_minor_publishes", 3)), 10))
        weekly_cap = max(0, min(int(values.get("max_weekly_publishes", 5)), 20))
        monthly_cap = max(0, min(int(values.get("max_monthly_publishes", 10)), 50))
        review_cap = max(0, min(int(values.get("max_metadata_reviews_per_run", 3)), 10))

        return self._request("POST", "owner_strategy_settings", params={
            "on_conflict": "owner_key",
        }, payload={
            "owner_key": owner,
            "auto_minor_enabled": enabled,
            "automation_mode": automation_mode,
            "emergency_pause": emergency_pause,
            "min_confidence_auto_publish": min_conf,
            "max_daily_minor_publishes": daily_cap,
            "max_weekly_publishes": weekly_cap,
            "max_monthly_publishes": monthly_cap,
            "max_metadata_reviews_per_run": review_cap,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, prefer="resolution=merge-duplicates,return=representation")

    def create_strategy_run(self, owner_key: str, values: Dict[str, Any]) -> Dict[str, Any]:
        if not owner_key:
            raise ValueError("A channel strategy run requires an owner key.")
        safe = {key: value for key, value in values.items() if key in {
            "channel_id", "trigger_source", "cadence", "dedup_key", "status",
            "metrics_period", "metrics_snapshot", "strategy_report", "result",
            "model_name", "error", "started_at", "finished_at", "updated_at",
        }}
        safe["owner_key"] = owner_key
        safe.setdefault("cadence", "daily")
        rows = self._request("POST", "channel_strategy_runs", payload=safe, prefer="return=representation")
        if not rows:
            raise PersistentStoreError("Supabase did not return the created strategy run.")
        return rows[0]

    def update_strategy_run(self, run_id: str, owner_key: str, values: Dict[str, Any]):
        safe = {key: value for key, value in values.items() if key in {
            "status", "metrics_period", "metrics_snapshot", "strategy_report",
            "result", "model_name", "error", "finished_at", "updated_at",
        }}
        return self._request("PATCH", "channel_strategy_runs", params={
            "id": f"eq.{run_id}", "owner_key": f"eq.{owner_key}",
        }, payload=safe, prefer="return=representation")

    def create_strategy_action(self, owner_key: str, values: Dict[str, Any]) -> Dict[str, Any]:
        if not owner_key:
            raise ValueError("A channel strategy action requires an owner key.")
        safe = {key: value for key, value in values.items() if key in {
            "run_id", "video_id", "video_title", "priority", "action_type",
            "classification", "status", "cadence", "confidence", "diagnosis",
            "rollback_of", "reason", "evidence", "before_metadata",
            "proposed_metadata", "after_metadata", "approval_digest", "model_name",
            "audit_log", "result", "error", "created_at", "updated_at", "executed_at",
        }}
        safe["owner_key"] = owner_key
        safe.setdefault("cadence", "daily")
        rows = self._request("POST", "channel_strategy_actions", payload=safe, prefer="return=representation")
        if not rows:
            raise PersistentStoreError("Supabase did not return the created strategy action.")
        return rows[0]

    def update_strategy_action(self, action_id: str, owner_key: str, values: Dict[str, Any]):
        safe = {key: value for key, value in values.items() if key in {
            "status", "confidence", "diagnosis", "reason", "evidence", "before_metadata",
            "proposed_metadata", "after_metadata", "approval_digest", "audit_log",
            "result", "error", "rollback_of", "updated_at", "executed_at",
        }}
        return self._request("PATCH", "channel_strategy_actions", params={
            "id": f"eq.{action_id}", "owner_key": f"eq.{owner_key}",
        }, payload=safe, prefer="return=representation")

    def get_strategy_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        rows = self._request("GET", "channel_strategy_actions", params={
            "id": f"eq.{action_id}", "owner_key": f"eq.{self.owner_key}",
            "select": "*", "limit": "1",
        })
        return rows[0] if rows else None

    def list_strategy_runs(self, limit: int = 10, cadence: Optional[str] = None) -> list[Dict[str, Any]]:
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        params = {
            "owner_key": f"eq.{self.owner_key}",
            "select": "id,channel_id,trigger_source,cadence,dedup_key,status,metrics_period,metrics_snapshot,strategy_report,result,model_name,error,started_at,finished_at,created_at",
            "order": "created_at.desc",
            "limit": str(max(1, min(int(limit), 50))),
        }
        if cadence:
            params["cadence"] = f"eq.{cadence}"
        return self._request("GET", "channel_strategy_runs", params=params)

    def list_strategy_watchdog_events(self, limit: int = 12) -> list[Dict[str, Any]]:
        """Return recent system-level backup-trigger checks (service-role only)."""
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        return self._request("GET", "channel_strategy_watchdog_events", params={
            "select": "run_date,status,detail,request_id,http_status_code,response_excerpt,created_at",
            "order": "created_at.desc",
            "limit": str(max(1, min(int(limit), 50))),
        })

    def list_strategy_actions(self, *, status: Optional[str] = None, cadence: Optional[str] = None, limit: int = 20) -> list[Dict[str, Any]]:
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        params = {
            "owner_key": f"eq.{self.owner_key}",
            "select": "id,run_id,video_id,video_title,priority,action_type,classification,status,cadence,confidence,diagnosis,rollback_of,reason,evidence,before_metadata,proposed_metadata,after_metadata,approval_digest,model_name,error,result,created_at,updated_at,executed_at",
            "order": "created_at.desc",
            "limit": str(max(1, min(int(limit), 100))),
        }
        if status:
            params["status"] = f"eq.{status}"
        if cadence:
            params["cadence"] = f"eq.{cadence}"
        return self._request("GET", "channel_strategy_actions", params=params)

    # --- Strategy action evaluations ---------------------------------------------------

    def create_action_evaluation(self, owner_key: str, values: Dict[str, Any]) -> Dict[str, Any]:
        if not owner_key:
            raise ValueError("An action evaluation requires an owner key.")
        safe = {key: value for key, value in values.items() if key in {
            "action_id", "video_id", "observation_days", "baseline_metrics",
            "post_metrics", "metric_deltas", "outcome", "evaluated_at", "created_at",
        }}
        safe["owner_key"] = owner_key
        rows = self._request("POST", "strategy_action_evaluations", payload=safe, prefer="return=representation")
        if not rows:
            raise PersistentStoreError("Supabase did not return the created evaluation.")
        return rows[0]

    def list_action_evaluations(self, *, action_id: Optional[str] = None, video_id: Optional[str] = None, limit: int = 50) -> list[Dict[str, Any]]:
        owner = self.owner_key
        if not owner:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        params = {
            "owner_key": f"eq.{owner}",
            "select": "id,action_id,video_id,observation_days,baseline_metrics,post_metrics,metric_deltas,outcome,evaluated_at,created_at",
            "order": "evaluated_at.desc",
            "limit": str(max(1, min(int(limit), 100))),
        }
        if action_id:
            params["action_id"] = f"eq.{action_id}"
        if video_id:
            params["video_id"] = f"eq.{video_id}"
        return self._request("GET", "strategy_action_evaluations", params=params)

    def _enqueue_publish_request(self, *, video_id: str, before_metadata: Dict[str, Any],
                                 proposed_metadata: Dict[str, Any], request_source: str,
                                 authorization_mode: str, authorized_by: str,
                                 idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        import re

        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")):
            raise ValueError("A valid 11-character YouTube video ID is required.")
        if not isinstance(before_metadata, dict) or not isinstance(proposed_metadata, dict):
            raise ValueError("Before metadata and proposed metadata must be objects.")
        if not proposed_metadata or set(proposed_metadata) - {"title", "description", "tags"}:
            raise ValueError("Only non-empty title, description, and tags changes can be queued.")
        if not str(authorized_by or "").strip():
            raise ValueError("A trusted authorization identity is required.")
        if request_source not in {"chatgpt", "monitor"}:
            raise ValueError("Unsupported publishing request source.")
        if request_source == "chatgpt" and authorization_mode != "explicit_user_command":
            raise ValueError("ChatGPT publishing requires an explicit user publish command.")
        if request_source == "monitor" and authorization_mode != "monitor_policy":
            raise ValueError("Monitor publishing requires the configured autonomous policy.")

        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "owner_key": self.owner_key,
            "video_id": video_id,
            "before_metadata": before_metadata,
            "proposed_metadata": proposed_metadata,
            "request_source": request_source,
            "authorization_mode": authorization_mode,
            "authorized_by": str(authorized_by)[:160],
            "authorized_at": now,
            # Retain the legacy fields for compatibility with existing audit/reporting.
            "approved_by": str(authorized_by)[:160] if request_source == "chatgpt" else None,
            "approved_at": now if request_source == "chatgpt" else None,
            "status": "queued",
        }
        if idempotency_key:
            payload["idempotency_key"] = str(idempotency_key)[:160]
        rows = self._request(
            "POST", "assistant_publish_requests", payload=payload,
            prefer="return=representation",
        )
        if not rows:
            raise PersistentStoreError("Supabase did not return the publishing queue record.")
        return rows[0]

    def enqueue_chat_publish_request(self, *, video_id: str, before_metadata: Dict[str, Any],
                                     proposed_metadata: Dict[str, Any], publish_confirmed: bool,
                                     authorized_by: str = "chatgpt_user",
                                     idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Queue a direct ChatGPT-originated write only after an explicit publish command."""
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        if publish_confirmed is not True:
            raise ValueError("No publishing request was queued: explicit publish confirmation is required.")
        return self._enqueue_publish_request(
            video_id=video_id, before_metadata=before_metadata,
            proposed_metadata=proposed_metadata, request_source="chatgpt",
            authorization_mode="explicit_user_command", authorized_by=authorized_by,
            idempotency_key=idempotency_key,
        )

    def get_monitor_publish_policy(self) -> Dict[str, Any]:
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        rows = self._request("GET", "owner_publish_policies", params={
            "owner_key": f"eq.{self.owner_key}",
            "select": "monitor_auto_publish_enabled,max_daily_publishes",
            "limit": "1",
        })
        if rows:
            return rows[0]
        # No stored policy means no explicit owner opt-in. Always fail closed.
        return {
            "monitor_auto_publish_enabled": False,
            "max_daily_publishes": 5,
        }

    def enqueue_monitor_publish_request(self, *, video_id: str,
                                        before_metadata: Dict[str, Any],
                                        proposed_metadata: Dict[str, Any],
                                        authorized_by: str = "authorized_monitor",
                                        idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Queue a monitor optimization under the owner's autonomous-publishing policy."""
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(video_id or "")):
            raise ValueError("A valid 11-character YouTube video ID is required.")
        if not isinstance(before_metadata, dict) or not isinstance(proposed_metadata, dict):
            raise ValueError("Before metadata and proposed metadata must be objects.")
        if not proposed_metadata or set(proposed_metadata) - {"title", "description", "tags"}:
            raise ValueError("Only non-empty title, description, and tags changes can be queued.")
        rows = self._request("POST", "rpc/enqueue_monitor_publish_request", payload={
            "p_owner_key": self.owner_key,
            "p_video_id": video_id,
            "p_before_metadata": before_metadata,
            "p_proposed_metadata": proposed_metadata,
            "p_authorized_by": str(authorized_by)[:160],
            "p_idempotency_key": idempotency_key,
        })
        if not rows:
            raise PersistentStoreError("Supabase did not return the monitor publishing queue record.")
        return rows[0]


    def get_publish_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Read one publishing request, scoped to the authenticated owner."""
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        rows = self._request("GET", "assistant_publish_requests", params={
            "id": f"eq.{request_id}", "owner_key": f"eq.{self.owner_key}",
            "select": "id,video_id,request_source,status,result,created_at,finished_at",
            "limit": "1",
        })
        return rows[0] if rows else None


    def set_monitor_publish_policy(self, enabled: bool, max_daily_publishes: int = 5):
        """Persist the owner's explicit autonomous-monitor publishing preference."""
        if not self.owner_key:
            raise PersistentStoreError("A verified owner-scoped store is required.")
        if not isinstance(enabled, bool):
            raise ValueError("Monitor publishing enabled must be a boolean.")
        cap = max(1, min(int(max_daily_publishes), 25))
        return self._request("POST", "owner_publish_policies", params={
            "on_conflict": "owner_key",
        }, payload={
            "owner_key": self.owner_key,
            "monitor_auto_publish_enabled": enabled,
            "max_daily_publishes": cap,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, prefer="resolution=merge-duplicates,return=representation")
