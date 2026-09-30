#!/usr/bin/env python3
"""SQLite-backed activity database for PCControlAgent.
Stores window foreground sessions, durations, and app timestamps with high-performance indexing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("activity_db")

_parent = Path(__file__).resolve().parent
BASE_DIR = _parent.parent if _parent.name == "core" else _parent
DATA_DIR = BASE_DIR / "data"
_data_db = DATA_DIR / "activity.db"
_root_db = BASE_DIR / "activity.db"
DB_PATH = _data_db if _data_db.exists() or not _root_db.exists() else _root_db



class ActivityDB:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __del__(self) -> None:
        self.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS app_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        app_name TEXT NOT NULL,
                        window_title TEXT,
                        exe_path TEXT,
                        started_at REAL NOT NULL,
                        ended_at REAL NOT NULL,
                        duration_sec REAL NOT NULL,
                        started_iso TEXT NOT NULL,
                        ended_iso TEXT NOT NULL,
                        date_str TEXT NOT NULL,
                        time_str TEXT NOT NULL,
                        emoji_id TEXT
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_date ON app_sessions(date_str);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_time_range ON app_sessions(started_at, ended_at);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_app ON app_sessions(app_name);")

    def record_session(
        self,
        app_name: str,
        window_title: Optional[str],
        exe_path: Optional[str],
        started_at: float,
        ended_at: float,
        emoji_id: Optional[str] = None
    ) -> Optional[int]:
        duration = max(0.0, round(ended_at - started_at, 2))
        if duration < 0.2:
            return None  # Filter out transient flickers

        start_dt = datetime.fromtimestamp(started_at).astimezone()
        end_dt = datetime.fromtimestamp(ended_at).astimezone()

        date_str = start_dt.strftime("%Y-%m-%d")
        time_str = start_dt.strftime("%H:%M:%S")
        started_iso = start_dt.isoformat(timespec="seconds")
        ended_iso = end_dt.isoformat(timespec="seconds")

        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO app_sessions (
                            app_name, window_title, exe_path, started_at, ended_at,
                            duration_sec, started_iso, ended_iso, date_str, time_str, emoji_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            app_name,
                            window_title or "",
                            exe_path or "",
                            started_at,
                            ended_at,
                            duration,
                            started_iso,
                            ended_iso,
                            date_str,
                            time_str,
                            emoji_id or ""
                        )
                    )
                    return cursor.lastrowid
            except Exception as e:
                LOG.exception("Failed to record session for %s: %s", app_name, e)
                return None

    def get_activity_at(self, target_ts: float) -> Optional[Dict[str, Any]]:
        """Find the active app and window at an exact timestamp."""
        with self._lock:
            conn = self._get_connection()
            row = conn.execute(
                """
                SELECT * FROM app_sessions
                WHERE started_at <= ? AND ended_at >= ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (target_ts, target_ts)
            ).fetchone()
            return dict(row) if row else None

    def get_day_summary(self, date_str: str) -> List[Dict[str, Any]]:
        """Get aggregate usage per app for a specific day."""
        with self._lock:
            conn = self._get_connection()
            rows = conn.execute(
                """
                SELECT 
                    app_name,
                    ROUND(SUM(duration_sec), 1) as total_duration,
                    COUNT(*) as sessions_count,
                    COALESCE(MAX(CASE WHEN emoji_id != '' THEN emoji_id END), '') as emoji_id
                FROM app_sessions
                WHERE date_str = ?
                GROUP BY app_name
                ORDER BY total_duration DESC
                """,
                (date_str,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_day_timeline(self, date_str: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get chronological list of window switches on a specific day."""
        with self._lock:
            conn = self._get_connection()
            rows = conn.execute(
                """
                SELECT * FROM app_sessions
                WHERE date_str = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (date_str, limit)
            ).fetchall()
            return [dict(r) for r in rows]


# Global singleton instance
_GLOBAL_DB: Optional[ActivityDB] = None

def get_activity_db() -> ActivityDB:
    global _GLOBAL_DB
    if _GLOBAL_DB is None:
        _GLOBAL_DB = ActivityDB()
    return _GLOBAL_DB
