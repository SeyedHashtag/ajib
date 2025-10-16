from __future__ import annotations

"""
Database layer for AJIB bot (SQLite-based)

This module provides minimal async database operations for:
- User registration and tracking (telegram_id, language, status)
- User subscriptions/configs tracking
- Test config eligibility
- Broadcast history (optional)

Uses aiosqlite for async SQLite operations.

Example:

    from ajib.bot.core.database import Database

    async def example():
        async with Database.from_config(cfg) as db:
            await db.init_schema()
            user = await db.get_or_create_user(telegram_id=123456789)
            await db.set_user_language(telegram_id=123456789, language="fa")
"""

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiosqlite
except ImportError:
    # Allow import but will fail at runtime if used
    aiosqlite = None  # type: ignore[assignment]

from .config import Config

__all__ = [
    "Database",
    "User",
    "UserSubscription",
]


# ============
# Data Models
# ============


@dataclass
class User:
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language: str = "en"
    status: str = "active"  # active, expired, test, banned
    test_used: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class UserSubscription:
    id: int
    telegram_id: int
    plan_id: str
    backend_config_id: Optional[str] = None
    status: str = "active"  # active, expired, cancelled
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ============
# Database
# ============


class Database:
    """
    Minimal async SQLite database wrapper for AJIB.

    Create it from config:
        db = await Database.from_config(cfg)
        await db.init_schema()

    Or use as async context manager:
        async with Database.from_config(cfg) as db:
            await db.init_schema()
            user = await db.get_or_create_user(telegram_id=123)
    """

    def __init__(self, db_path: Path) -> None:
        if aiosqlite is None:
            raise RuntimeError(
                "aiosqlite is required for database operations. Install with: pip install aiosqlite"
            )
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    @classmethod
    def from_config(cls, cfg: Config) -> "Database":
        """Create Database instance from Config."""
        return cls(db_path=cfg.sqlite_path)

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open database connection."""
        if self._conn is None:
            # Ensure parent directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            # Enable foreign keys
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _ensure_conn(self) -> aiosqlite.Connection:
        """Ensure connection is open, raise if not."""
        if self._conn is None:
            raise RuntimeError("Database connection not open. Call connect() first.")
        return self._conn

    # ------------
    # Schema
    # ------------

    async def init_schema(self) -> None:
        """
        Create tables if they don't exist.
        Safe to call multiple times (idempotent).
        """
        conn = self._ensure_conn()

        # Users table
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'active',
                test_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # User subscriptions (configs)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                plan_id TEXT NOT NULL,
                backend_config_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
            """
        )

        # Broadcast history (optional, for tracking sent broadcasts)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audience TEXT NOT NULL,
                message_text TEXT NOT NULL,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Create indexes
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_telegram_id ON user_subscriptions(telegram_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_status ON user_subscriptions(status)"
        )

        await conn.commit()

    # ------------
    # Users
    # ------------

    async def get_user(self, telegram_id: int) -> Optional[User]:
        """Get user by telegram_id."""
        conn = self._ensure_conn()
        async with conn.execute(
            """
            SELECT telegram_id, username, first_name, last_name, language, status,
                   test_used, created_at, updated_at
            FROM users WHERE telegram_id = ?
            """,
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return User(
                telegram_id=row[0],
                username=row[1],
                first_name=row[2],
                last_name=row[3],
                language=row[4],
                status=row[5],
                test_used=bool(row[6]),
                created_at=row[7],
                updated_at=row[8],
            )

    async def create_user(
        self,
        telegram_id: int,
        *,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        language: str = "en",
    ) -> User:
        """Create a new user."""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, language, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, username, first_name, last_name, language, now, now),
        )
        await conn.commit()
        user = await self.get_user(telegram_id)
        if user is None:
            raise RuntimeError(f"Failed to create user {telegram_id}")
        return user

    async def get_or_create_user(
        self,
        telegram_id: int,
        *,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        language: str = "en",
    ) -> User:
        """Get existing user or create if not exists."""
        user = await self.get_user(telegram_id)
        if user is not None:
            return user
        return await self.create_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language=language,
        )

    async def update_user(
        self,
        telegram_id: int,
        *,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        language: Optional[str] = None,
        status: Optional[str] = None,
        test_used: Optional[bool] = None,
    ) -> None:
        """Update user fields."""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        # Build update query dynamically
        fields: List[str] = ["updated_at = ?"]
        values: List[Any] = [now]

        if username is not None:
            fields.append("username = ?")
            values.append(username)
        if first_name is not None:
            fields.append("first_name = ?")
            values.append(first_name)
        if last_name is not None:
            fields.append("last_name = ?")
            values.append(last_name)
        if language is not None:
            fields.append("language = ?")
            values.append(language)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if test_used is not None:
            fields.append("test_used = ?")
            values.append(1 if test_used else 0)

        values.append(telegram_id)

        query = f"UPDATE users SET {', '.join(fields)} WHERE telegram_id = ?"
        await conn.execute(query, tuple(values))
        await conn.commit()

    async def set_user_language(self, telegram_id: int, language: str) -> None:
        """Update user language preference."""
        await self.update_user(telegram_id=telegram_id, language=language)

    async def mark_test_used(self, telegram_id: int) -> None:
        """Mark that user has used their test config."""
        await self.update_user(telegram_id=telegram_id, test_used=True)

    async def has_used_test(self, telegram_id: int) -> bool:
        """Check if user has already used test config."""
        user = await self.get_user(telegram_id)
        return user.test_used if user else False

    # ------------
    # Subscriptions
    # ------------

    async def create_subscription(
        self,
        telegram_id: int,
        plan_id: str,
        *,
        backend_config_id: Optional[str] = None,
        expires_at: Optional[str] = None,
        status: str = "active",
    ) -> UserSubscription:
        """Create a new subscription for user."""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()
        cursor = await conn.execute(
            """
            INSERT INTO user_subscriptions (telegram_id, plan_id, backend_config_id, status, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, plan_id, backend_config_id, status, expires_at, now, now),
        )
        await conn.commit()
        sub_id = cursor.lastrowid
        return UserSubscription(
            id=sub_id,
            telegram_id=telegram_id,
            plan_id=plan_id,
            backend_config_id=backend_config_id,
            status=status,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )

    async def get_user_subscriptions(
        self, telegram_id: int, status: Optional[str] = None
    ) -> List[UserSubscription]:
        """Get all subscriptions for a user, optionally filtered by status."""
        conn = self._ensure_conn()
        if status:
            query = """
                SELECT id, telegram_id, plan_id, backend_config_id, status, expires_at, created_at, updated_at
                FROM user_subscriptions WHERE telegram_id = ? AND status = ?
                ORDER BY created_at DESC
            """
            params: Tuple[Any, ...] = (telegram_id, status)
        else:
            query = """
                SELECT id, telegram_id, plan_id, backend_config_id, status, expires_at, created_at, updated_at
                FROM user_subscriptions WHERE telegram_id = ?
                ORDER BY created_at DESC
            """
            params = (telegram_id,)

        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                UserSubscription(
                    id=row[0],
                    telegram_id=row[1],
                    plan_id=row[2],
                    backend_config_id=row[3],
                    status=row[4],
                    expires_at=row[5],
                    created_at=row[6],
                    updated_at=row[7],
                )
                for row in rows
            ]

    async def update_subscription_status(self, subscription_id: int, status: str) -> None:
        """Update subscription status."""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()
        await conn.execute(
            "UPDATE user_subscriptions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, subscription_id),
        )
        await conn.commit()

    # ------------
    # User Queries (for broadcast)
    # ------------

    async def get_users_by_status(self, status: str) -> List[int]:
        """Get telegram_ids of users with given status."""
        conn = self._ensure_conn()
        async with conn.execute(
            "SELECT telegram_id FROM users WHERE status = ?", (status,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_all_user_ids(self) -> List[int]:
        """Get all telegram_ids."""
        conn = self._ensure_conn()
        async with conn.execute("SELECT telegram_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_test_users(self) -> List[int]:
        """Get telegram_ids of users who used test config."""
        conn = self._ensure_conn()
        async with conn.execute("SELECT telegram_id FROM users WHERE test_used = 1") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    # ------------
    # Broadcast History
    # ------------

    async def create_broadcast_record(self, audience: str, message_text: str) -> int:
        """Create a broadcast history record and return its ID."""
        conn = self._ensure_conn()
        cursor = await conn.execute(
            """
            INSERT INTO broadcast_history (audience, message_text, created_at)
            VALUES (?, ?, ?)
            """,
            (audience, message_text, datetime.utcnow().isoformat()),
        )
        await conn.commit()
        return cursor.lastrowid

    async def update_broadcast_stats(
        self, broadcast_id: int, sent_count: int, failed_count: int
    ) -> None:
        """Update broadcast send statistics."""
        conn = self._ensure_conn()
        await conn.execute(
            """
            UPDATE broadcast_history
            SET sent_count = ?, failed_count = ?
            WHERE id = ?
            """,
            (sent_count, failed_count, broadcast_id),
        )
        await conn.commit()

    # ------------
    # Utilities
    # ------------

    async def execute_raw(self, query: str, params: Tuple[Any, ...] = ()) -> Any:
        """Execute raw SQL query. Use with caution."""
        conn = self._ensure_conn()
        cursor = await conn.execute(query, params)
        await conn.commit()
        return cursor

    async def fetchall_raw(self, query: str, params: Tuple[Any, ...] = ()) -> List[Tuple]:
        """Execute raw SELECT query and return all rows."""
        conn = self._ensure_conn()
        async with conn.execute(query, params) as cursor:
            return await cursor.fetchall()
