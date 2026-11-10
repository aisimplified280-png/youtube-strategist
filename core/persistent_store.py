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
            "owner_key": f"eq.{self.owner_key}", "select": "*",
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
            "select": "*", "order": "row_number.asc",
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

