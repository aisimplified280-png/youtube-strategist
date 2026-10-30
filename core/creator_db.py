"""Local SQLite persistent storage for Creator Growth Agent.

Authoritative local database for:
1. Daily briefings (with lock mechanisms for multi-tab/rerun idempotency)
2. Creator preferences, tone, and voice profile
3. Proposals history (accepted, declined, reasons)
4. Ideas & Production pipeline stages
5. AI research radar topics and links
6. Channel experiments
"""

import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "creator_growth_memory.sqlite3"


def get_db_connection() -> sqlite3.Connection:
    """Get a thread-safe connection to the local creator SQLite database."""
    _DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_FILE), timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=15000;")
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection):
    """Initialize database schemas if not present."""
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_briefings (
                id TEXT PRIMARY KEY,
                run_date TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                briefing_json TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS briefing_locks (
                lock_key TEXT PRIMARY KEY,
                acquired_at_utc TEXT NOT NULL,
                expires_at_epoch REAL NOT NULL,
                owner_token TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS creator_preferences (
                pref_key TEXT PRIMARY KEY,
                pref_value TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                status TEXT NOT NULL, -- 'approved', 'declined', 'staged', 'applied'
                proposal_json TEXT NOT NULL,
                feedback TEXT,
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS production_pipeline (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                stage TEXT NOT NULL, -- 'idea', 'scripting', 'recording', 'published', 'archived'
                target_format TEXT NOT NULL, -- 'long', 'short'
                notes TEXT,
                concepts_json TEXT,
                hook_text TEXT,
                updated_at_utc TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS research_radar (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                category TEXT NOT NULL, -- 'model_release', 'agent_tool', 'research_paper', 'tutorial'
                discovered_date TEXT NOT NULL,
                summary TEXT NOT NULL,
                video_angles_json TEXT NOT NULL,
                status TEXT DEFAULT 'active' -- 'active', 'used', 'dismissed'
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,
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


# --- Mutex Lock for Streamlit Reruns / Multi-Tab ---

def acquire_briefing_lock(lock_key: str = "daily_briefing_lock", ttl_seconds: int = 45, owner_token: str = "") -> bool:
    """Acquire a temporary mutex lock in SQLite to prevent duplicate runs across reruns/tabs."""
    conn = get_db_connection()
    now_epoch = time.time()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
                            "UPDATE briefing_locks SET expires_at_epoch = ?, acquired_at_utc = ? WHERE lock_key = ?",
                            (now_epoch + ttl_seconds, now_utc, lock_key)
                        )
                        return True
                    return False
                # Expired lock, replace it
                cur.execute(
                    "UPDATE briefing_locks SET acquired_at_utc = ?, expires_at_epoch = ?, owner_token = ? WHERE lock_key = ?",
                    (now_utc, now_epoch + ttl_seconds, owner_token, lock_key)
                )
                return True
            else:
                cur.execute(
                    "INSERT INTO briefing_locks (lock_key, acquired_at_utc, expires_at_epoch, owner_token) VALUES (?, ?, ?, ?)",
                    (lock_key, now_utc, now_epoch + ttl_seconds, owner_token)
                )
                return True
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

def get_latest_daily_briefing(channel_id: str, run_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieve the latest successfully generated daily briefing."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if run_date:
            cur.execute(
                "SELECT briefing_json FROM daily_briefings WHERE channel_id = ? AND run_date = ? AND status = 'ready' ORDER BY created_at_utc DESC LIMIT 1",
                (channel_id, run_date)
            )
        else:
            cur.execute(
                "SELECT briefing_json FROM daily_briefings WHERE channel_id = ? AND status = 'ready' ORDER BY created_at_utc DESC LIMIT 1",
                (channel_id,)
            )
        row = cur.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None
    except Exception:
        return None
    finally:
        conn.close()


def save_daily_briefing(briefing_id: str, channel_id: str, run_date: str, briefing_dict: Dict[str, Any]) -> None:
    """Save completed daily briefing to SQLite."""
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_briefings (id, run_date, created_at_utc, channel_id, briefing_json, status)
                VALUES (?, ?, ?, ?, ?, 'ready')
                """,
                (briefing_id, run_date, now_utc, channel_id, json.dumps(briefing_dict))
            )
    finally:
        conn.close()


# --- Creator Preferences & Voice Profile ---

def get_creator_preferences() -> Dict[str, Any]:
    """Load all saved creator preferences and voice guidelines."""
    conn = get_db_connection()
    prefs: Dict[str, Any] = {
        "voice_tone": "Conversational, direct, technical depth without hype, practical code focus.",
        "prohibited_words": "insane, mind-blowing, game-changer, secret trick, guaranteed",
        "target_audience": "Software developers, AI engineers, Python builders, tech curious makers",
        "primary_format": "Long-form tutorials (10-20 mins) + fast technical Shorts (<60s)",
    }
    try:
        cur = conn.cursor()
        cur.execute("SELECT pref_key, pref_value FROM creator_preferences")
        for k, v in cur.fetchall():
            prefs[k] = v
        return prefs
    finally:
        conn.close()


def set_creator_preference(key: str, value: str) -> None:
    """Store or update a creator preference."""
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO creator_preferences (pref_key, pref_value, updated_at_utc) VALUES (?, ?, ?)",
                (key, value, now_utc)
            )
    finally:
        conn.close()


# --- Proposal History (Accepted/Declined Tracking) ---

def record_proposal_decision(video_id: str, proposal_digest: str, status: str, proposal_dict: Dict[str, Any], feedback: str = "") -> None:
    """Record an owner acceptance, decline, or modification with reason."""
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO proposal_history (video_id, proposal_digest, status, proposal_json, feedback, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (video_id, proposal_digest, status, json.dumps(proposal_dict), feedback, now_utc)
            )
    finally:
        conn.close()


def get_recent_proposal_history(limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieve history of proposals and creator responses."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, video_id, proposal_digest, status, proposal_json, feedback, updated_at_utc
            FROM proposal_history ORDER BY id DESC LIMIT ?
            """,
            (limit,)
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

def get_pipeline_items(stage: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get content items in production pipeline."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if stage:
            cur.execute("SELECT id, title, stage, target_format, notes, concepts_json, hook_text, updated_at_utc FROM production_pipeline WHERE stage = ? ORDER BY updated_at_utc DESC", (stage,))
        else:
            cur.execute("SELECT id, title, stage, target_format, notes, concepts_json, hook_text, updated_at_utc FROM production_pipeline ORDER BY updated_at_utc DESC")
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


def save_pipeline_item(item_id: str, title: str, stage: str, target_format: str = "long", notes: str = "", concepts: Optional[List[Dict[str, Any]]] = None, hook_text: str = "") -> None:
    """Insert or update a pipeline item."""
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO production_pipeline (id, title, stage, target_format, notes, concepts_json, hook_text, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, title, stage, target_format, notes, json.dumps(concepts or []), hook_text, now_utc)
            )
    finally:
        conn.close()


def update_pipeline_stage(item_id: str, new_stage: str) -> None:
    """Move pipeline item to new stage."""
    conn = get_db_connection()
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        with conn:
            conn.execute("UPDATE production_pipeline SET stage = ?, updated_at_utc = ? WHERE id = ?", (new_stage, now_utc, item_id))
    finally:
        conn.close()


# --- AI Research Radar ---

def get_radar_items(status: str = "active", limit: int = 15) -> List[Dict[str, Any]]:
    """Retrieve trending radar items."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, source_url, category, discovered_date, summary, video_angles_json, status FROM research_radar WHERE status = ? ORDER BY discovered_date DESC LIMIT ?",
            (status, limit)
        )
        rows = cur.fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0],
                "title": r[1],
                "source_url": r[2],
                "category": r[3],
                "discovered_date": r[4],
                "summary": r[5],
                "video_angles": json.loads(r[6]) if r[6] else [],
                "status": r[7],
            })
        return items
    finally:
        conn.close()


def save_radar_item(item_id: str, title: str, source_url: str, category: str, summary: str, video_angles: List[Dict[str, Any]], discovered_date: Optional[str] = None) -> None:
    """Add item to research radar."""
    conn = get_db_connection()
    date_str = discovered_date or datetime.date.today().isoformat()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO research_radar (id, title, source_url, category, discovered_date, summary, video_angles_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (item_id, title, source_url, category, date_str, summary, json.dumps(video_angles))
            )
    finally:
        conn.close()
