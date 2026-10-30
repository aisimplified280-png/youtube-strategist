"""Persistent storage for Creator Growth Agent with owner-scoping and demo-isolation.

Authoritative local database for:
1. Daily briefings (with lock mechanisms for multi-tab/rerun idempotency and lease renewal)
2. Creator preferences, tone, and voice profile
3. Proposals history (accepted, declined, reasons)
4. Ideas & Production pipeline stages
5. AI research radar topics and links with full provenance
6. Channel experiments
"""

import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from core.demo_data import (
    DEMO_CREATOR_PREFERENCES,
    DEMO_PIPELINE_ITEMS,
    DEMO_RADAR_ITEMS,
    DEMO_PROPOSAL_HISTORY,
)

DEFAULT_OWNER_ID = "default_owner"


def _migrate_legacy_db_file_if_needed(target_path: Path):
    """If target DB file does not exist, check for legacy DB files and copy."""
    try:
        if target_path.exists():
            return
        legacy_repo_path = Path(__file__).resolve().parent.parent / "data" / "creator_growth_memory.sqlite3"
        if legacy_repo_path.exists() and legacy_repo_path.stat().st_size > 0:
            import shutil
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_repo_path, target_path)
    except Exception:
        pass


def get_db_path() -> Path:
    """Return managed database path honoring environment overrides and secrets.
    
    NOTE: On ephemeral platforms (e.g. Streamlit Community Cloud), local container
    storage resets across redeployments. Set CREATOR_GROWTH_DB_PATH to a mounted
    persistent storage volume path or external database for durability.
    """
    env_path = os.environ.get("CREATOR_GROWTH_DB_PATH")
    if not env_path:
        try:
            import streamlit as st
            env_path = getattr(st, "secrets", {}).get("CREATOR_GROWTH_DB_PATH")
        except Exception:
            env_path = None

    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _migrate_legacy_db_file_if_needed(p)
        return p

    durable_dir = Path.home() / ".creator_growth_agent"
    try:
        durable_dir.mkdir(parents=True, exist_ok=True)
        p = durable_dir / "creator_growth_memory.sqlite3"
        _migrate_legacy_db_file_if_needed(p)
        return p
    except Exception:
        fallback = Path(__file__).resolve().parent.parent / "data" / "creator_growth_memory.sqlite3"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


class StorageBackend:
    """Pluggable persistence backend interface for development and hosted environments."""
    def get_connection(self):
        raise NotImplementedError
    
    def is_remote(self) -> bool:
        return False
    
    @property
    def name(self) -> str:
        return "abstract"


class SQLiteBackend(StorageBackend):
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def get_connection(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        _init_db(conn)
        return conn

    def is_remote(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return "sqlite"


class PostgresBackend(StorageBackend):
    def __init__(self, connection_url: str):
        self.connection_url = connection_url
        self._driver = None
        for mod in ("psycopg2", "psycopg", "pg8000"):
            try:
                self._driver = __import__(mod)
                break
            except ImportError:
                continue
        if self._driver is None:
            raise ImportError(
                "Remote PostgreSQL connection configured but no PostgreSQL driver (psycopg2, psycopg, pg8000) is installed."
            )

    def get_connection(self):
        conn = self._driver.connect(self.connection_url)
        _init_db(conn)
        return conn

    def is_remote(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "postgres"


_CURRENT_BACKEND: Optional[StorageBackend] = None


def get_storage_backend(secrets: Optional[Dict[str, Any]] = None) -> StorageBackend:
    """Resolve authoritative persistent database backend for local or hosted execution."""
    global _CURRENT_BACKEND
    if _CURRENT_BACKEND is not None:
        return _CURRENT_BACKEND

    # Check for remote database URL in secrets or environment
    remote_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not remote_url and secrets:
        remote_url = secrets.get("database", {}).get("url") or secrets.get("postgres", {}).get("url")
    if not remote_url:
        try:
            import streamlit as st
            remote_url = getattr(st, "secrets", {}).get("DATABASE_URL")
            if not remote_url:
                db_cfg = getattr(st, "secrets", {}).get("database", {})
                if isinstance(db_cfg, dict):
                    remote_url = db_cfg.get("url")
        except Exception:
            pass

    if remote_url and (remote_url.startswith("postgres://") or remote_url.startswith("postgresql://")):
        try:
            return PostgresBackend(remote_url)
        except Exception:
            pass

    # Default to durable managed SQLite
    p = get_db_path()
    return SQLiteBackend(p)


def set_storage_backend(backend: Optional[StorageBackend]) -> None:
    """Set or override active backend (primarily for tests)."""
    global _CURRENT_BACKEND
    _CURRENT_BACKEND = backend


def get_db_connection(secrets: Optional[Dict[str, Any]] = None):
    """Get a thread-safe connection to the persistent database backend."""
    backend = get_storage_backend(secrets=secrets)
    return backend.get_connection()


def _ensure_columns(conn: sqlite3.Connection, table_name: str, required_columns: Dict[str, str]):
    """Add any missing columns to existing tables for backwards-compatible migration."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col_name, col_def in required_columns.items():
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


def _init_db(conn: sqlite3.Connection):
    """Initialize database schemas and migrate columns if not present."""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_briefings (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'default_owner',
                channel_id TEXT NOT NULL,
                run_date TEXT NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                briefing_json TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        _ensure_columns(
            conn,
            "daily_briefings",
            {
                "owner_id": "TEXT NOT NULL DEFAULT 'default_owner'",
                "is_demo": "INTEGER NOT NULL DEFAULT 0",
            },
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_locks (
                lock_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'default_owner',
                acquired_at_utc TEXT NOT NULL,
                expires_at_epoch REAL NOT NULL,
                owner_token TEXT NOT NULL
            );
            """
        )
        _ensure_columns(
            conn,
            "briefing_locks",
            {"owner_id": "TEXT NOT NULL DEFAULT 'default_owner'"},
        )

        # Check if creator_preferences exists without owner_id column (legacy migration)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(creator_preferences)")
        pref_cols = {row[1] for row in cur.fetchall()}
        if pref_cols and "owner_id" not in pref_cols:
            conn.execute("ALTER TABLE creator_preferences RENAME TO _legacy_creator_preferences")
            conn.execute(
                """
                CREATE TABLE creator_preferences (
                    owner_id TEXT NOT NULL DEFAULT 'default_owner',
                    pref_key TEXT NOT NULL,
                    pref_value TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (owner_id, pref_key)
                );
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO creator_preferences (owner_id, pref_key, pref_value, updated_at_utc)
                SELECT 'default_owner', pref_key, pref_value, updated_at_utc FROM _legacy_creator_preferences;
                """
            )
            conn.execute("DROP TABLE _legacy_creator_preferences;")
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creator_preferences (
                    owner_id TEXT NOT NULL DEFAULT 'default_owner',
                    pref_key TEXT NOT NULL,
                    pref_value TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (owner_id, pref_key)
                );
                """
            )
        _ensure_columns(
            conn,
            "creator_preferences",
            {"owner_id": "TEXT NOT NULL DEFAULT 'default_owner'"},
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL DEFAULT 'default_owner',
                video_id TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                feedback TEXT,
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        _ensure_columns(
            conn,
            "proposal_history",
            {"owner_id": "TEXT NOT NULL DEFAULT 'default_owner'"},
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS production_pipeline (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'default_owner',
                title TEXT NOT NULL,
                stage TEXT NOT NULL,
                target_format TEXT NOT NULL,
                notes TEXT,
                concepts_json TEXT,
                hook_text TEXT,
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        _ensure_columns(
            conn,
            "production_pipeline",
            {"owner_id": "TEXT NOT NULL DEFAULT 'default_owner'"},
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_radar (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'default_owner',
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                category TEXT NOT NULL,
                source_date TEXT,
                retrieved_at_utc TEXT NOT NULL,
                channel_relevance TEXT NOT NULL,
                discovered_date TEXT NOT NULL,
                summary TEXT NOT NULL,
                video_angles_json TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                source_type TEXT DEFAULT 'curated_reference',
                is_live_ingested INTEGER DEFAULT 0
            );
            """
        )
        _ensure_columns(
            conn,
            "research_radar",
            {
                "owner_id": "TEXT NOT NULL DEFAULT 'default_owner'",
                "source_date": "TEXT",
                "retrieved_at_utc": "TEXT DEFAULT ''",
                "channel_relevance": "TEXT DEFAULT ''",
                "discovered_date": "TEXT DEFAULT ''",
                "source_type": "TEXT DEFAULT 'curated_reference'",
                "is_live_ingested": "INTEGER DEFAULT 0",
            },
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT 'default_owner',
                video_id TEXT NOT NULL,
                video_title TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                applied_at_utc TEXT NOT NULL,
                baseline_metrics_json TEXT NOT NULL,
                post_metrics_json TEXT,
                hypothesis TEXT,
                outcome_notes TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_metadata (
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_credentials (
                app_key TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                purpose TEXT NOT NULL,
                encrypted_value BLOB NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (app_key, subject_key, purpose)
            );
            """
        )
        _ensure_columns(
            conn,
            "oauth_credentials",
            {"revoked": "INTEGER NOT NULL DEFAULT 0"},
        )

        # Backfill radar source dates and URLs for authoritative releases
        conn.execute(
            """
            UPDATE research_radar 
            SET source_date = '2024-10-11', 
                source_url = 'https://blog.crewai.com/announcing-crewai-flows/'
            WHERE id = 'radar-crewai-flows';
            """
        )
        conn.execute(
            """
            UPDATE research_radar 
            SET source_date = '2024-09-25'
            WHERE id = 'radar-llama-3.2-edge' AND source_date = '2026-09-25';
            """
        )
        conn.execute(
            """
            UPDATE research_radar 
            SET source_date = '2024-10-03'
            WHERE id = 'radar-gemini-1.5-flash-8b' AND source_date = '2026-10-03';
            """
        )


# --- Legacy Owner Migration ---

def claim_legacy_owner_records(authenticated_owner_id: str) -> Dict[str, Any]:
    """Safely migrate legacy 'default_owner' records to the verified authenticated owner.

    Ensures:
    - Never maps records to demo or unauthenticated visitors.
    - One-time safe operation: once claimed, does not re-map to other owners.
    - Migrates preferences, proposal history, pipeline, radar, briefings, experiments, and locks.
    """
    if not authenticated_owner_id or authenticated_owner_id in (DEFAULT_OWNER_ID, "demo", "demo_owner", "anonymous"):
        return {}

    conn = get_db_connection()
    migrated_counts: Dict[str, Any] = {}
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT meta_value FROM system_metadata WHERE meta_key = 'legacy_owner_claimed'")
            row = cur.fetchone()
            if row:
                return {"success": False, "already_claimed_by": row[0]}

            # 1. creator_preferences: migrate default_owner preferences if authenticated owner doesn't have them
            cur.execute("SELECT pref_key, pref_value, updated_at_utc FROM creator_preferences WHERE owner_id = ?", (DEFAULT_OWNER_ID,))
            default_prefs = cur.fetchall()
            pref_count = 0
            for p_key, p_val, p_upd in default_prefs:
                cur.execute("SELECT 1 FROM creator_preferences WHERE owner_id = ? AND pref_key = ?", (authenticated_owner_id, p_key))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT OR REPLACE INTO creator_preferences (owner_id, pref_key, pref_value, updated_at_utc) VALUES (?, ?, ?, ?)",
                        (authenticated_owner_id, p_key, p_val, p_upd)
                    )
                    pref_count += 1
            cur.execute("DELETE FROM creator_preferences WHERE owner_id = ?", (DEFAULT_OWNER_ID,))
            migrated_counts["creator_preferences"] = pref_count

            # 2. proposal_history
            cur.execute("UPDATE proposal_history SET owner_id = ? WHERE owner_id = ?", (authenticated_owner_id, DEFAULT_OWNER_ID))
            migrated_counts["proposal_history"] = cur.rowcount

            # 3. production_pipeline
            cur.execute("UPDATE production_pipeline SET owner_id = ? WHERE owner_id = ?", (authenticated_owner_id, DEFAULT_OWNER_ID))
            migrated_counts["production_pipeline"] = cur.rowcount

            # 4. research_radar
            cur.execute("UPDATE research_radar SET owner_id = ? WHERE owner_id = ?", (authenticated_owner_id, DEFAULT_OWNER_ID))
            migrated_counts["research_radar"] = cur.rowcount

            # 5. daily_briefings
            cur.execute("UPDATE daily_briefings SET owner_id = ? WHERE owner_id = ?", (authenticated_owner_id, DEFAULT_OWNER_ID))
            migrated_counts["daily_briefings"] = cur.rowcount

            # 6. experiments
            cur.execute("UPDATE experiments SET owner_id = ? WHERE owner_id = ?", (authenticated_owner_id, DEFAULT_OWNER_ID))
            migrated_counts["experiments"] = cur.rowcount

            # 7. briefing_locks
            cur.execute("UPDATE briefing_locks SET owner_id = ? WHERE owner_id = ?", (authenticated_owner_id, DEFAULT_OWNER_ID))
            migrated_counts["briefing_locks"] = cur.rowcount

            # Record that migration was completed
            cur.execute(
                "INSERT OR REPLACE INTO system_metadata (meta_key, meta_value, updated_at_utc) VALUES ('legacy_owner_claimed', ?, ?)",
                (authenticated_owner_id, now_utc)
            )
            migrated_counts["success"] = True
            migrated_counts["claimed_by"] = authenticated_owner_id
        return migrated_counts
    finally:
        conn.close()


# --- Mutex Lock with Safe Lease Renewal ---

def build_briefing_lock_key(
    channel_id: str,
    run_date: str,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> str:
    """Create isolated cache and lock key scoped by identity, mode, and date."""
    mode = "demo" if is_demo else "prod"
    owner = "demo" if is_demo else (owner_id or DEFAULT_OWNER_ID)
    return f"briefing_{owner}_{channel_id}_{mode}_{run_date}"


def acquire_briefing_lock(
    lock_key: str = "daily_briefing_lock",
    ttl_seconds: int = 45,
    owner_token: str = "",
    owner_id: Optional[str] = None,
) -> bool:
    """Acquire a temporary mutex lock in SQLite to prevent duplicate runs across reruns/tabs."""
    conn = get_db_connection()
    now_epoch = time.time()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    oid = owner_id or DEFAULT_OWNER_ID
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT expires_at_epoch, owner_token FROM briefing_locks WHERE lock_key = ?", (lock_key,))
            row = cur.fetchone()
            if row:
                expires_at, current_owner = row
                if now_epoch < expires_at:
                    if owner_token and current_owner == owner_token:
                        # Refresh existing lock for same owner
                        cur.execute(
                            "UPDATE briefing_locks SET expires_at_epoch = ?, acquired_at_utc = ?, owner_id = ? WHERE lock_key = ?",
                            (now_epoch + ttl_seconds, now_utc, oid, lock_key)
                        )
                        return True
                    return False
                # Expired lock, replace it
                cur.execute(
                    "UPDATE briefing_locks SET acquired_at_utc = ?, expires_at_epoch = ?, owner_token = ?, owner_id = ? WHERE lock_key = ?",
                    (now_utc, now_epoch + ttl_seconds, owner_token, oid, lock_key)
                )
                return True
            else:
                cur.execute(
                    "INSERT INTO briefing_locks (lock_key, owner_id, acquired_at_utc, expires_at_epoch, owner_token) VALUES (?, ?, ?, ?, ?)",
                    (lock_key, oid, now_utc, now_epoch + ttl_seconds, owner_token)
                )
                return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def extend_briefing_lock(
    lock_key: str = "daily_briefing_lock",
    owner_token: str = "",
    extend_seconds: int = 60,
) -> bool:
    """Renew or safely extend the active lock during long operations."""
    if not owner_token:
        return False
    conn = get_db_connection()
    now_epoch = time.time()
    try:
        with conn:
            cur = conn.cursor()
            cur.execute("SELECT expires_at_epoch, owner_token FROM briefing_locks WHERE lock_key = ?", (lock_key,))
            row = cur.fetchone()
            if row:
                expires_at, current_owner = row
                if current_owner == owner_token and now_epoch < expires_at:
                    new_expires = now_epoch + extend_seconds
                    cur.execute(
                        "UPDATE briefing_locks SET expires_at_epoch = ? WHERE lock_key = ?",
                        (new_expires, lock_key)
                    )
                    return True
        return False
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def release_briefing_lock(lock_key: str = "daily_briefing_lock", owner_token: str = "") -> None:
    """Release briefing lock."""
    conn = get_db_connection()
    try:
        with conn:
            if owner_token:
                conn.execute("DELETE FROM briefing_locks WHERE lock_key = ? AND owner_token = ?", (lock_key, owner_token))
            else:
                conn.execute("DELETE FROM briefing_locks WHERE lock_key = ?", (lock_key,))
    except sqlite3.Error:
        pass
    finally:
        conn.close()


# --- Daily Briefings Persistence ---

def get_latest_daily_briefing(
    channel_id: str,
    run_date: Optional[str] = None,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> Optional[Dict[str, Any]]:
    """Retrieve the latest successfully generated daily briefing.
    
    Ensures demo briefings cannot be returned to production sessions.
    Demo mode never reads owner's creator database.
    """
    if is_demo:
        return None

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if run_date:
            cur.execute(
                """
                SELECT briefing_json FROM daily_briefings 
                WHERE channel_id = ? AND owner_id = ? AND run_date = ? AND is_demo = 0 AND status = 'ready' 
                ORDER BY created_at_utc DESC LIMIT 1
                """,
                (channel_id, oid, run_date)
            )
        else:
            cur.execute(
                """
                SELECT briefing_json FROM daily_briefings 
                WHERE channel_id = ? AND owner_id = ? AND is_demo = 0 AND status = 'ready' 
                ORDER BY created_at_utc DESC LIMIT 1
                """,
                (channel_id, oid)
            )
        row = cur.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None
    except Exception:
        return None
    finally:
        conn.close()


def save_daily_briefing(
    briefing_id: str,
    channel_id: str,
    run_date: str,
    briefing_dict: Dict[str, Any],
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> None:
    """Save completed daily briefing to SQLite.
    
    Demo mode must never write to the owner's database.
    """
    if is_demo:
        # Isolated: Demo briefings are not stored in owner's database
        return

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_briefings (id, owner_id, channel_id, run_date, is_demo, created_at_utc, briefing_json, status)
                VALUES (?, ?, ?, ?, 0, ?, ?, 'ready')
                """,
                (briefing_id, oid, channel_id, run_date, now_utc, json.dumps(briefing_dict))
            )
    finally:
        conn.close()


# --- Creator Preferences & Voice Profile ---

def get_creator_preferences(
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> Dict[str, Any]:
    """Load creator preferences and voice guidelines.
    
    In Demo Mode, returns isolated demo preferences without reading owner data.
    """
    if is_demo:
        return DEMO_CREATOR_PREFERENCES.copy()

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    prefs: Dict[str, Any] = DEMO_CREATOR_PREFERENCES.copy()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pref_key, pref_value FROM creator_preferences WHERE owner_id = ?", (oid,))
        for k, v in cur.fetchall():
            prefs[k] = v
        return prefs
    finally:
        conn.close()


def set_creator_preference(
    key: str,
    value: str,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> None:
    """Store or update an owner's creator preference.
    
    Demo Mode must never modify owner preferences.
    """
    if is_demo:
        raise PermissionError("Demo Mode cannot modify owner creator preferences.")

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO creator_preferences (owner_id, pref_key, pref_value, updated_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (owner_id, pref_key) DO UPDATE SET
                    pref_value = excluded.pref_value,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (oid, key, value, now_utc)
            )
    finally:
        conn.close()


# --- Proposal History (Accepted/Declined Tracking) ---

def record_proposal_decision(
    video_id: str,
    proposal_digest: str,
    status: str,
    proposal_dict: Dict[str, Any],
    feedback: str = "",
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> None:
    """Record an owner acceptance, decline, or modification with reason.
    
    Demo Mode must never write to owner proposal history.
    """
    if is_demo:
        raise PermissionError("Demo Mode cannot record proposal decisions to creator database.")

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO proposal_history (owner_id, video_id, proposal_digest, status, proposal_json, feedback, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (oid, video_id, proposal_digest, status, json.dumps(proposal_dict), feedback, now_utc)
            )
    finally:
        conn.close()


def get_recent_proposal_history(
    limit: int = 20,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> List[Dict[str, Any]]:
    """Retrieve history of proposals and creator responses.
    
    Demo Mode returns static in-memory fixtures.
    """
    if is_demo:
        return [item.copy() for item in DEMO_PROPOSAL_HISTORY[:limit]]

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, video_id, proposal_digest, status, proposal_json, feedback, updated_at_utc
            FROM proposal_history WHERE owner_id = ? ORDER BY id DESC LIMIT ?
            """,
            (oid, limit)
        )
        rows = cur.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0],
                "video_id": r[1],
                "proposal_digest": r[2],
                "status": r[3],
                "proposal": json.loads(r[4]) if r[4] else {},
                "feedback": r[5] or "",
                "updated_at": r[6],
            })
        return items
    finally:
        conn.close()


# --- Production Pipeline ---

def get_pipeline_items(
    stage: Optional[str] = None,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> List[Dict[str, Any]]:
    """Get content items in production pipeline.
    
    Demo Mode returns static in-memory fixtures without reading owner data.
    """
    if is_demo:
        items = [item.copy() for item in DEMO_PIPELINE_ITEMS]
        if stage:
            items = [it for it in items if it.get("stage") == stage]
        return items

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if stage:
            cur.execute(
                """
                SELECT id, title, stage, target_format, notes, concepts_json, hook_text, updated_at_utc 
                FROM production_pipeline WHERE owner_id = ? AND stage = ? ORDER BY updated_at_utc DESC
                """,
                (oid, stage)
            )
        else:
            cur.execute(
                """
                SELECT id, title, stage, target_format, notes, concepts_json, hook_text, updated_at_utc 
                FROM production_pipeline WHERE owner_id = ? ORDER BY updated_at_utc DESC
                """,
                (oid,)
            )
        rows = cur.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0],
                "title": r[1],
                "stage": r[2],
                "target_format": r[3],
                "notes": r[4] or "",
                "concepts": json.loads(r[5]) if r[5] else [],
                "hook_text": r[6] or "",
                "updated_at": r[7],
            })
        return items
    finally:
        conn.close()


def save_pipeline_item(
    item_id: str,
    title: str,
    stage: str,
    target_format: str = "long",
    notes: str = "",
    concepts: Optional[List[Dict[str, Any]]] = None,
    hook_text: str = "",
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> None:
    """Insert or update a pipeline item.
    
    Demo Mode must never write to owner pipeline storage.
    """
    if is_demo:
        raise PermissionError("Demo Mode cannot modify production pipeline storage.")

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO production_pipeline 
                (id, owner_id, title, stage, target_format, notes, concepts_json, hook_text, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, oid, title, stage, target_format, notes, json.dumps(concepts or []), hook_text, now_utc)
            )
    finally:
        conn.close()


def update_pipeline_stage(
    item_id: str,
    new_stage: str,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> None:
    """Move pipeline item to new stage.
    
    Demo Mode must never modify owner pipeline storage.
    """
    if is_demo:
        raise PermissionError("Demo Mode cannot modify production pipeline storage.")

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                "UPDATE production_pipeline SET stage = ?, updated_at_utc = ? WHERE id = ? AND owner_id = ?",
                (new_stage, now_utc, item_id, oid)
            )
    finally:
        conn.close()


# --- AI Research Radar ---

def get_radar_items(
    status: str = "active",
    limit: int = 15,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> List[Dict[str, Any]]:
    """Retrieve trending radar items.
    
    In Demo Mode, returns isolated in-memory radar items.
    Items retrieved from database are explicitly marked with is_cached=True.
    """
    if is_demo:
        return [item.copy() for item in DEMO_RADAR_ITEMS[:limit]]

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, source_url, category, source_date, retrieved_at_utc, channel_relevance, summary, video_angles_json, status, discovered_date, source_type, is_live_ingested 
            FROM research_radar WHERE owner_id = ? AND status = ? ORDER BY discovered_date DESC LIMIT ?
            """,
            (oid, status, limit)
        )
        rows = cur.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0],
                "title": r[1],
                "source_url": r[2],
                "category": r[3],
                "source_date": r[4],
                "retrieved_at_utc": r[5],
                "channel_relevance": r[6],
                "summary": r[7],
                "video_angles": json.loads(r[8]) if r[8] else [],
                "status": r[9],
                "discovered_date": r[10] if len(r) > 10 else "",
                "source_type": r[11] if len(r) > 11 and r[11] else "curated_reference",
                "is_live_ingested": bool(r[12]) if len(r) > 12 and r[12] is not None else False,
                "is_cached": True,
            })
        return items
    finally:
        conn.close()


def save_radar_item(
    item_id: str,
    title: str,
    source_url: str,
    category: str,
    summary: str,
    video_angles: List[Dict[str, Any]],
    discovered_date: Optional[str] = None,
    source_date: Optional[str] = None,
    retrieved_at_utc: Optional[str] = None,
    channel_relevance: str = "",
    source_type: str = "curated_reference",
    is_live_ingested: bool = False,
    owner_id: Optional[str] = None,
    is_demo: bool = False,
) -> None:
    """Add item to research radar with full provenance.
    
    Demo Mode must never modify owner research records.
    """
    if is_demo:
        raise PermissionError("Demo Mode cannot modify research radar storage.")

    oid = owner_id or DEFAULT_OWNER_ID
    conn = get_db_connection()
    date_str = discovered_date or datetime.date.today().isoformat()
    now_utc = retrieved_at_utc or datetime.datetime.now(datetime.timezone.utc).isoformat()
    src_date = source_date or date_str
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO research_radar 
                (id, owner_id, title, source_url, category, source_date, retrieved_at_utc, channel_relevance, discovered_date, summary, video_angles_json, status, source_type, is_live_ingested)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (item_id, oid, title, source_url, category, src_date, now_utc, channel_relevance, date_str, summary, json.dumps(video_angles), source_type, 1 if is_live_ingested else 0)
            )
    finally:
        conn.close()
